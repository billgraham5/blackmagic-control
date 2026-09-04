"""Client for the Blackmagic Camera Control REST API.

Owns the single connection to the camera and keeps a live cache of its state,
so callers (the web UI and the Stream Deck endpoints) never have to care whether
a value arrived over the websocket or from a poll.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
from typing import Any, AsyncIterator, Iterable

import httpx
import websockets

from .config import Settings
from .discovery import Endpoint, expand_templates, fetch_specs

log = logging.getLogger("bmc.camera")

#: Properties worth tracking. Anything the camera does not implement is dropped
#: during the capability probe, so this list can stay optimistic.
TRACKED: tuple[str, ...] = (
    "/system",
    "/system/codecFormat",
    "/system/videoFormat",
    "/system/format",
    "/transports/0",
    "/transports/0/record",
    "/transports/0/play",
    "/transports/0/stop",
    "/transports/0/timecode",
    "/video/iso",
    "/video/gain",
    "/video/shutter",
    "/video/whiteBalance",
    "/video/whiteBalanceTint",
    "/video/autoExposure",
    "/lens/iris",
    "/lens/zoom",
    "/lens/focus",
    "/colorCorrection/lift",
    "/colorCorrection/gamma",
    "/colorCorrection/gain",
    "/colorCorrection/offset",
    "/colorCorrection/contrast",
    "/colorCorrection/color",
    "/colorCorrection/lumaContribution",
    "/presets",
    "/presets/active",
    "/media/active",
    "/media/workingset",
    "/audio/channel/0/level",
    "/audio/channel/1/level",
)

#: Endpoints that exist only on some bodies or some firmware. The Micro Studio
#: 4K G2's own OpenAPI documentation lists an ND filter and XLR audio it does not
#: have, while newer firmware adds monitoring and camera endpoints that the
#: published spec dumps predate. Both directions are settled by probing.
OPTIONAL: tuple[str, ...] = (
    # exposure detail
    "/video/ndFilter",
    "/video/ndFilter/displayMode",
    "/video/ndFilterSelectable",
    "/video/supportedNDFilters",
    "/video/supportedISOs",
    "/video/supportedGains",
    "/video/supportedShutters",
    "/video/shutter/measurement",
    "/video/flickerFreeShutters",
    "/video/detailSharpening",
    "/video/detailSharpeningLevel",
    # lens capability description
    "/lens/iris/description",
    "/lens/focus/description",
    "/lens/zoom/description",
    "/lens/opticalImageStabilization",
    # camera body
    "/camera/colorBars",
    "/camera/tallyStatus",
    "/camera/power",
    "/camera/power/displayMode",
    "/camera/programFeedDisplay",
    "/camera/timingReferenceLock",
    # monitoring, global
    "/monitoring/display",
    "/monitoring/focusAssist",
    "/monitoring/frameGuideRatio",
    "/monitoring/frameGuideRatio/presets",
    "/monitoring/frameGrids",
    "/monitoring/safeAreaPercent",
    # system and transport detail
    "/system/product",
    "/system/supportedFormats",
    "/system/supportedCodecFormats",
    "/system/supportedVideoFormats",
    "/transports/0/clipIndex",
    "/transports/0/timecode/source",
    "/timelines/0",
    "/clips",
    # audio detail
    "/audio/channels",
    "/audio/supportedInputs",
    "/audio/channel/0/input",
    "/audio/channel/0/input/description",
    "/audio/channel/0/supportedInputs",
    "/audio/channel/0/available",
    "/audio/channel/0/lowCutFilter",
    "/audio/channel/0/padding",
    "/audio/channel/0/phantomPower",
    "/audio/channel/1/input",
    "/audio/channel/1/available",
    "/media/devices/doformatSupportedFilesystems",
)

#: Per-display monitoring overlays. The API addresses these by display name and
#: does not document which names a given body uses, so they are discovered.
MONITORING_PER_DISPLAY: tuple[str, ...] = (
    "zebra",
    "falseColor",
    "focusAssist",
    "frameGuide",
    "frameGrids",
    "safeArea",
    "cleanFeed",
    "displayLUT",
)

#: Display names to try when the camera does not enumerate them itself.
DISPLAY_CANDIDATES: tuple[str, ...] = (
    "hdmi", "sdi", "lcd", "viewfinder", "main", "front", "preview",
)


class CameraError(RuntimeError):
    """The camera rejected a request."""


class CameraUnavailable(CameraError):
    """The camera could not be reached at all."""


class Camera:
    """A live connection to one camera."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client: httpx.AsyncClient | None = None
        self._state: dict[str, Any] = {}
        self._supported: set[str] = set()
        self._pushed: set[str] = set()
        self._listeners: set[asyncio.Queue[dict[str, Any]]] = set()
        self._tasks: list[asyncio.Task[None]] = []
        self._connected = False
        self._identity: dict[str, Any] = {}
        self._probe_results: dict[str, int | None] = {}
        self._event_list: Any = None
        self._displays: list[str] = []
        self._endpoints: dict[str, Endpoint] = {}
        self._root_client: httpx.AsyncClient | None = None

    # ---------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        context = self.settings.ssl_context()
        self._root_client = httpx.AsyncClient(
            timeout=self.settings.request_timeout,
            verify=context if context is not None else True,
            trust_env=False,
        )
        self._client = httpx.AsyncClient(
            base_url=self.settings.api_base,
            timeout=self.settings.request_timeout,
            verify=context if context is not None else True,
            # The camera is on the local network. httpx would otherwise honour
            # HTTP_PROXY/HTTPS_PROXY and send requests for a .local mDNS name to
            # a proxy that cannot possibly resolve it.
            trust_env=False,
        )
        try:
            await self._probe()
        except Exception:
            await self._client.aclose()
            self._client = None
            raise
        self._tasks = [
            asyncio.create_task(self._websocket_loop(), name="bmc-websocket"),
            asyncio.create_task(self._poll_loop(), name="bmc-poll"),
        ]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()
        for client in (self._client, self._root_client):
            if client is not None:
                await client.aclose()
        self._client = self._root_client = None

    # ------------------------------------------------------------------- probe

    async def _probe(self) -> None:
        """Work out what this camera and firmware actually implement.

        The published spec covers a whole camera family, so the only reliable
        answer comes from asking this body directly.

        Probing is sequential on purpose. The camera runs a small embedded HTTP
        server; firing several dozen simultaneous requests at it makes most of
        them fail, and a failed probe is indistinguishable from an unsupported
        endpoint. Slow and correct beats fast and wrong -- this runs once at
        startup.
        """
        # One quick question before the full sweep. Probing is sequential, so an
        # unreachable camera -- or the wrong scheme -- would otherwise burn a
        # timeout per endpoint before admitting defeat.
        status, _ = await self._probe_one("/system", attempts=1)
        if status is None:
            raise CameraUnavailable(
                f"No response from {self.settings.api_base}. Run "
                f"`python -m bmc --camera {self.settings.camera_host} --diagnose` to see "
                "which step fails: the name, the port, TLS, or the API itself while the "
                "camera's web server is still answering."
            )

        # Ask the camera what it implements before assuming anything. The
        # built-in list is only a floor -- firmware 9.6 serves endpoints that
        # post-date every published spec dump.
        self._endpoints = await self._fetch_documentation()
        await self._discover_displays()
        self._event_list = await self.get("/event/list")

        candidates = self._candidate_paths()
        self._probe_results = {}
        reachable = False

        for path in candidates:
            status, value = await self._probe_one(path)
            self._probe_results[path] = status
            if status is None:
                continue
            reachable = True
            if 200 <= status < 300:
                self._supported.add(path)
                if value is not None:
                    self._state[path] = value

        if not reachable:
            raise CameraUnavailable(
                f"No response from {self.settings.api_base}. Run "
                f"`python -m bmc --camera {self.settings.camera_host} --diagnose` to see "
                "which step fails: the name, the port, TLS, or the API itself while the "
                "camera's web server is still answering."
            )

        self._connected = True
        self._identity = self._state.get("/system") or self._state.get("/system/product") or {}

        # Which of the supported properties the camera will push to us. Older
        # firmware only pushes media/system/transport, so the rest must be polled.
        available = _event_names(self._event_list)
        self._pushed = {p for p in self._supported if p in available}
        if self._supported and not self._pushed:
            log.warning(
                "the camera lists %d subscribable properties, none matching the %d "
                "endpoints in use; everything will be polled. /event/list returned: %r",
                len(available),
                len(self._supported),
                self._event_list,
            )

        log.info(
            "camera ready: %d endpoints supported, %d pushed over websocket, %d polled",
            len(self._supported),
            len(self._pushed),
            len(self._supported) - len(self._pushed),
        )
        unexpected = {
            path: status
            for path, status in self._probe_results.items()
            if not (status is not None and (200 <= status < 300 or status in (404, 501)))
        }
        if unexpected:
            # Neither "here it is" nor "not supported" -- worth surfacing, since
            # it silently costs the user a control.
            log.warning(
                "%d endpoints answered unexpectedly: %s",
                len(unexpected),
                ", ".join(f"{p} -> {s or 'no response'}" for p, s in sorted(unexpected.items())),
            )

    async def _fetch_documentation(self) -> dict[str, Endpoint]:
        assert self._root_client is not None
        root = f"{self.settings.camera_scheme}://{self.settings.camera_host}"
        try:
            return await fetch_specs(self._root_client, root)
        except Exception as exc:  # noqa: BLE001 - discovery is best effort
            log.debug("could not read camera documentation: %s", exc)
            return {}

    def _candidate_paths(self) -> list[str]:
        """Everything worth probing, from every source we have.

        Order matters only for readability of the log; duplicates are collapsed.
        """
        channels = self._audio_channels()
        candidates: list[str] = []

        for path in self._endpoints:
            candidates.extend(expand_templates(path, self._displays, channels))

        for name in sorted(_event_names(self._event_list)):
            candidates.extend(expand_templates(name, self._displays, channels))

        candidates.extend(TRACKED)
        candidates.extend(OPTIONAL)
        for display in self._displays:
            candidates.extend(
                f"/monitoring/{display}/{overlay}" for overlay in MONITORING_PER_DISPLAY
            )

        # Endpoints that take a name we would have to invent, and the websocket
        # itself, are not readable state.
        skip = {"/event/websocket", "/"}
        return [
            path
            for path in dict.fromkeys(candidates)
            if path.startswith("/") and "{" not in path and path not in skip
        ]

    def _audio_channels(self) -> list[int]:
        reported = self._state.get("/audio/channels")
        if isinstance(reported, dict):
            for value in reported.values():
                if isinstance(value, int) and 0 < value <= 16:
                    return list(range(value))
        return [0, 1]

    async def _discover_displays(self) -> None:
        """Find the monitoring outputs this body exposes.

        Overlays like zebra and false colour are addressed per display, and the
        API does not document which names a given camera uses. Ask the camera
        first; fall back to trying the usual names with one cheap endpoint each,
        then probe the full set only for displays that answered.
        """
        # Fetched rather than read from the cache: display discovery now runs
        # before the main sweep, because expanding /monitoring/{displayName}/...
        # into real paths needs the names first.
        status, listed = await self._probe_one("/monitoring/display")
        self._probe_results["/monitoring/display"] = status
        if status is not None and 200 <= status < 300:
            self._supported.add("/monitoring/display")
            if listed is not None:
                self._state["/monitoring/display"] = listed
        names: list[str] = []
        if isinstance(listed, dict):
            for key in ("displays", "display", "names"):
                value = listed.get(key)
                if isinstance(value, list):
                    names = [str(item) for item in value]
                    break
            if not names and isinstance(listed.get("display"), str):
                names = [listed["display"]]
        elif isinstance(listed, list):
            names = [str(item) for item in listed]

        candidates = names or list(DISPLAY_CANDIDATES)
        for name in candidates:
            status, value = await self._probe_one(f"/monitoring/{name}/zebra", attempts=1)
            self._probe_results[f"/monitoring/{name}/zebra"] = status
            if status is None or not (200 <= status < 300):
                continue
            self._displays.append(name)
            self._supported.add(f"/monitoring/{name}/zebra")
            if value is not None:
                self._state[f"/monitoring/{name}/zebra"] = value

        for name in self._displays:
            for overlay in MONITORING_PER_DISPLAY:
                if overlay == "zebra":
                    continue
                path = f"/monitoring/{name}/{overlay}"
                status, value = await self._probe_one(path)
                self._probe_results[path] = status
                if status is not None and 200 <= status < 300:
                    self._supported.add(path)
                    if value is not None:
                        self._state[path] = value

        if self._displays:
            log.info("monitoring displays: %s", ", ".join(self._displays))

    async def _probe_one(self, path: str, attempts: int = 3) -> tuple[int | None, Any]:
        """Read one endpoint, returning (status, value).

        A status of ``None`` means the request never completed.

        Dropped connections and 5xx responses are retried: both mean the camera
        was momentarily overwhelmed, and neither says anything about whether the
        endpoint exists. 404 and 501 are definitive, so they are taken at face
        value. Getting this wrong costs the user a control for the lifetime of
        the process, which is a bad trade against a couple of extra requests.
        """
        assert self._client is not None
        status: int | None = None
        for attempt in range(attempts):
            if attempt:
                await asyncio.sleep(0.2 * attempt)
            try:
                response = await self._client.get(path)
            except httpx.HTTPError as exc:
                log.debug("probe %s attempt %d failed: %s", path, attempt + 1, exc)
                status = None
                continue
            status = response.status_code
            if 200 <= status < 300:
                # 204 is a real answer: the endpoint exists, it just has nothing
                # to report right now (no disk mounted, no value set).
                return status, _decode(response)
            if status < 500:
                return status, None
        return status, None

    @property
    def endpoints(self) -> dict[str, Endpoint]:
        """What the camera's own documentation says about each path."""
        return dict(self._endpoints)

    def write_schema(self, path: str) -> dict[str, Any]:
        """The fields a PUT to ``path`` accepts, if the camera documented them."""
        endpoint = self._endpoints.get(path)
        if endpoint is not None and endpoint.write_schema:
            return endpoint.write_schema
        # Templated paths keep their placeholder in the documentation.
        for candidate, endpoint in self._endpoints.items():
            if "{" not in candidate:
                continue
            pattern = re.escape(candidate)
            pattern = re.sub(r"\\\{\w+\\\}", r"[^/]+", pattern)
            if re.fullmatch(pattern, path) and endpoint.write_schema:
                return endpoint.write_schema
        return {}

    @property
    def has_documentation(self) -> bool:
        """Whether the camera told us what it implements."""
        return bool(self._endpoints)

    def is_writable(self, path: str) -> bool:
        endpoint = self._endpoints.get(path)
        if endpoint is not None:
            return endpoint.writable
        if self.write_schema(path):
            return True
        # No documentation for this path. If the camera served none at all we
        # have nothing better than the value's own shape; if it did, silence
        # about this path means it is not something to write to.
        return not self.has_documentation

    @property
    def event_list(self) -> Any:
        """Whatever ``/event/list`` returned, verbatim, for diagnostics."""
        return self._event_list

    @property
    def displays(self) -> list[str]:
        """Monitoring outputs this camera exposes, discovered at startup."""
        return list(self._displays)

    @property
    def probe_results(self) -> dict[str, int | None]:
        """Every probed endpoint and the status it returned, for diagnostics."""
        return dict(self._probe_results)

    # ---------------------------------------------------------------- requests

    async def get(self, path: str) -> Any | None:
        """Read a property. Returns ``None`` if the camera does not implement it."""
        if self._client is None:
            raise CameraUnavailable("camera client is not started")
        try:
            response = await self._client.get(path)
        except httpx.HTTPError as exc:
            raise CameraUnavailable(f"GET {path} failed: {exc}") from exc
        if response.status_code in (404, 501):
            return None
        if response.is_error:
            raise CameraError(f"GET {path} returned {response.status_code}")
        return _decode(response)

    async def put(self, path: str, body: Any | None = None) -> None:
        """Write a property, updating the local cache on success."""
        if self._client is None:
            raise CameraUnavailable("camera client is not started")
        try:
            response = await self._client.put(path, json=body if body is not None else {})
        except httpx.HTTPError as exc:
            raise CameraUnavailable(f"PUT {path} failed: {exc}") from exc
        if response.status_code in (404, 501):
            raise CameraError(f"{path} is not supported by this camera")
        if response.is_error:
            raise CameraError(
                f"camera rejected {path} with {response.status_code}: "
                f"{response.text.strip()[:200] or 'no detail'}"
            )
        # Read back rather than trusting the value we sent: the camera clamps and
        # snaps to its own legal set, so what we asked for is not always what stuck.
        if path in self._supported:
            with contextlib.suppress(CameraError, CameraUnavailable):
                refreshed = await self.get(path)
                if refreshed is not None:
                    self._update(path, refreshed)

    # ------------------------------------------------------------------- state

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def identity(self) -> dict[str, Any]:
        return dict(self._identity)

    @property
    def supported(self) -> set[str]:
        return set(self._supported)

    @property
    def pushed(self) -> set[str]:
        return set(self._pushed)

    def snapshot(self) -> dict[str, Any]:
        return dict(self._state)

    def value(self, path: str) -> Any | None:
        return self._state.get(path)

    def supports(self, *paths: str) -> bool:
        return all(path in self._supported for path in paths)

    def _update(self, path: str, value: Any) -> None:
        if self._state.get(path) == value:
            return
        self._state[path] = value
        self._broadcast({"type": "state", "property": path, "value": value})

    # --------------------------------------------------------------- listeners

    @contextlib.asynccontextmanager
    async def listen(self) -> AsyncIterator[asyncio.Queue[dict[str, Any]]]:
        """Subscribe to state changes. Yields a queue of update messages."""
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=256)
        self._listeners.add(queue)
        try:
            yield queue
        finally:
            self._listeners.discard(queue)

    def snapshot_message(self) -> dict[str, Any]:
        """A complete state message, used on connect and to resynchronise."""
        return {
            "type": "snapshot",
            "connected": self._connected,
            "supported": sorted(self._supported),
            "state": dict(self._state),
        }

    def _broadcast(self, message: dict[str, Any]) -> None:
        for queue in list(self._listeners):
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                # Never stop updating a client. Dropping the listener leaves the
                # page connected and looking healthy while it silently shows
                # stale values until someone reloads it. Throw the backlog away
                # and queue one snapshot instead, so it catches up in a single
                # message no matter how far behind it fell.
                while not queue.empty():
                    try:
                        queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                try:
                    queue.put_nowait(self.snapshot_message())
                except asyncio.QueueFull:  # pragma: no cover - just drained it
                    pass

    # --------------------------------------------------------------- websocket

    async def _websocket_loop(self) -> None:
        """Keep a websocket open, resubscribing after every reconnect."""
        backoff = 1.0
        while True:
            try:
                async with websockets.connect(
                    self.settings.websocket_url,
                    ssl=self.settings.ssl_context(),
                    proxy=None,  # same reason as trust_env above
                ) as socket:
                    backoff = 1.0
                    await self._subscribe(socket)
                    async for raw in socket:
                        self._handle_event(raw)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - any failure means retry
                log.warning("websocket dropped (%s); retrying in %.0fs", exc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    async def _subscribe(self, socket: Any) -> None:
        if not self._pushed:
            return
        await socket.send(
            json.dumps(
                {
                    "type": "request",
                    "id": 1,
                    "data": {"action": "subscribe", "properties": sorted(self._pushed)},
                }
            )
        )

    def _handle_event(self, raw: str | bytes) -> None:
        try:
            message = json.loads(raw)
        except (ValueError, TypeError):
            return
        data = message.get("data") or {}
        if message.get("type") == "event" and data.get("action") == "propertyValueChanged":
            path, value = data.get("property"), data.get("value")
            if isinstance(path, str):
                self._update(path, value)
        elif message.get("type") == "response" and data.get("action") == "subscribe":
            for path, value in (data.get("values") or {}).items():
                self._update(path, value)

    # ----------------------------------------------------------------- polling

    async def _poll_loop(self) -> None:
        """Refresh the properties this firmware will not push to us."""
        while True:
            await asyncio.sleep(self.settings.poll_interval)
            polled = self._supported - self._pushed
            if not polled:
                continue
            for path in sorted(polled):
                try:
                    value = await self.get(path)
                except (CameraError, CameraUnavailable):
                    continue
                if value is not None:
                    self._update(path, value)


def _event_names(payload: Any) -> set[str]:
    """Pull the subscribable property list out of whichever shape firmware used."""
    if isinstance(payload, dict):
        for key in ("events", "properties", "deviceProperties"):
            value = payload.get(key)
            if isinstance(value, list):
                return {str(item) for item in value}
        return set()
    if isinstance(payload, list):
        return {str(item) for item in payload}
    return set()


def _decode(response: httpx.Response) -> Any:
    if not response.content:
        return None
    try:
        return response.json()
    except ValueError:
        return response.text


def only_supported(camera: Camera, paths: Iterable[str]) -> list[str]:
    return [path for path in paths if path in camera.supported]
