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
from typing import Any, AsyncIterator, Iterable

import httpx
import websockets

from .config import Settings

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

#: Endpoints the spec advertises but which need a capability check before use:
#: the Micro Studio 4K G2's own OpenAPI documentation lists ND filter and XLR
#: audio despite having neither.
OPTIONAL: tuple[str, ...] = (
    "/video/ndFilter",
    "/video/supportedISOs",
    "/video/supportedShutters",
    "/video/supportedGains",
    "/camera/tallyStatus",
    "/camera/colorBars",
    "/monitoring/focusAssist",
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

    # ---------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        context = self.settings.ssl_context()
        self._client = httpx.AsyncClient(
            base_url=self.settings.api_base,
            timeout=self.settings.request_timeout,
            verify=context if context is not None else True,
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
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------- probe

    async def _probe(self) -> None:
        """Work out what this camera and firmware actually implement.

        The published spec covers a whole camera family, so the only reliable
        answer comes from asking this body directly.
        """
        candidates = list(dict.fromkeys(TRACKED + OPTIONAL))
        results = await asyncio.gather(
            *(self._probe_one(path) for path in candidates), return_exceptions=True
        )

        reachable = False
        for path, result in zip(candidates, results):
            if isinstance(result, Exception):
                continue
            reachable = True
            if result is not None:
                self._supported.add(path)
                self._state[path] = result

        if not reachable:
            raise CameraUnavailable(
                f"No response from {self.settings.api_base}. Check that the camera is on "
                "the network and that the web media manager is enabled in Blackmagic "
                "Camera Setup under 'network access'."
            )

        self._connected = True
        self._identity = self._state.get("/system") or {}

        # Which of the supported properties the camera will push to us. Older
        # firmware only pushes media/system/transport, so the rest must be polled.
        events = await self.get("/event/list")
        if isinstance(events, dict):
            available = set(events.get("events") or [])
            self._pushed = {p for p in self._supported if p in available}
        log.info(
            "camera ready: %d endpoints supported, %d pushed over websocket, %d polled",
            len(self._supported),
            len(self._pushed),
            len(self._supported) - len(self._pushed),
        )

    async def _probe_one(self, path: str) -> Any | None:
        assert self._client is not None
        response = await self._client.get(path)
        if response.status_code in (404, 501):
            return None
        response.raise_for_status()
        return _decode(response)

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

    def _broadcast(self, message: dict[str, Any]) -> None:
        for queue in list(self._listeners):
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                # A stalled client must not block the camera loop.
                self._listeners.discard(queue)

    # --------------------------------------------------------------- websocket

    async def _websocket_loop(self) -> None:
        """Keep a websocket open, resubscribing after every reconnect."""
        backoff = 1.0
        while True:
            try:
                async with websockets.connect(
                    self.settings.websocket_url,
                    ssl=self.settings.ssl_context(),
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


def _decode(response: httpx.Response) -> Any:
    if not response.content:
        return None
    try:
        return response.json()
    except ValueError:
        return response.text


def only_supported(camera: Camera, paths: Iterable[str]) -> list[str]:
    return [path for path in paths if path in camera.supported]
