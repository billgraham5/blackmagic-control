"""The local control service.

Two surfaces over one camera connection:

* ``/deck/*`` -- plain-text GET endpoints. A generic HTTP Stream Deck plugin (or
  Bitfocus Companion's HTTP module) can call these directly, and the response
  body is short enough to use as a button title.
* ``/api/*`` -- JSON plus a websocket, for the web page.

Both go through :mod:`bmc.actions`, so a button and a slider that do the same
thing really do the same thing.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable

from fastapi import APIRouter, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from . import actions, ladders
from .camera import Camera, CameraError, CameraUnavailable
from .config import Settings

log = logging.getLogger("bmc.app")

WEB_ROOT = Path(__file__).parent / "web"


class Supervisor:
    """Keeps a camera connection alive, retrying if the camera is not there yet.

    The service is useful before the camera is: it can be started at boot, and
    the camera brought up later, without anyone having to restart anything.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.camera = Camera(settings)
        self.last_error: str | None = None
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._connect_loop(), name="bmc-supervisor")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        await self.camera.stop()

    async def _connect_loop(self) -> None:
        """Connect, falling back to the other scheme before giving up on a round.

        Whether the camera answers on HTTPS depends on a certificate having been
        generated in Blackmagic Camera Setup, which is a setting someone can turn
        off later. Trying both beats failing because of a stale flag.
        """
        candidates = [self.settings, self.settings.with_scheme(self.settings.other_scheme)]
        delay = 2.0
        first_failure = True
        while True:
            for settings in candidates:
                camera = Camera(settings)
                try:
                    await camera.start()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - try the next candidate
                    self.last_error = str(exc)
                    continue
                if settings.camera_scheme != self.settings.camera_scheme:
                    log.warning(
                        "%s did not answer; using %s instead",
                        self.settings.camera_scheme,
                        settings.camera_scheme,
                    )
                self.camera = camera
                self.settings = settings
                self.last_error = None
                log.info("connected to %s", settings.api_base)
                return
            # Explain the problem once, then keep the retries to one short line
            # so a camera that is simply switched off does not bury the console.
            if first_failure:
                log.warning("camera not reachable: %s", self.last_error)
                first_failure = False
            log.info("still waiting for %s, retrying in %.0fs", self.settings.camera_host, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 1.5, 30.0)

    def require(self) -> Camera:
        if not self.camera.connected:
            raise HTTPException(
                status_code=503,
                detail=self.last_error or "camera not connected",
            )
        return self.camera


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    supervisor = Supervisor(settings)

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI):
        await supervisor.start()
        try:
            yield
        finally:
            await supervisor.stop()

    app = FastAPI(title="Blackmagic camera control", lifespan=lifespan)
    app.state.supervisor = supervisor
    app.state.settings = settings

    @app.exception_handler(HTTPException)
    async def deck_errors(request: Request, exc: HTTPException):
        """Render /deck failures as plain text -- they land on a button title."""
        if request.url.path.startswith("/deck/"):
            detail = exc.detail if isinstance(exc.detail, str) else "error"
            return PlainTextResponse(detail, status_code=exc.status_code)
        return await http_exception_handler(request, exc)

    app.include_router(_deck_router(supervisor))
    app.include_router(_api_router(supervisor))

    if WEB_ROOT.is_dir():
        app.mount("/static", StaticFiles(directory=WEB_ROOT), name="static")

        @app.get("/", include_in_schema=False)
        async def index() -> FileResponse:
            return FileResponse(WEB_ROOT / "index.html")

    return app


# --------------------------------------------------------------------- deck

async def _text(fn: Callable[[], Awaitable[str]]) -> PlainTextResponse:
    """Run an action and render the result as a Stream Deck button title."""
    try:
        return PlainTextResponse(await fn())
    except CameraUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except CameraError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _deck_router(supervisor: Supervisor) -> APIRouter:
    """Plain-text endpoints for a control surface.

    Route order matters: literal paths are declared before their parameterised
    siblings so ``/deck/wb/auto`` is not parsed as a Kelvin value.
    """
    router = APIRouter(prefix="/deck", tags=["deck"])

    @router.get("/status", response_class=PlainTextResponse)
    async def status() -> str:
        camera = supervisor.require()
        return actions.status_line(camera)

    @router.get("/media", response_class=PlainTextResponse)
    async def media() -> str:
        return actions.media_summary(supervisor.require())

    # ---- record
    @router.get("/record/toggle", response_class=PlainTextResponse)
    async def record_toggle(clip: str | None = None):
        camera = supervisor.require()
        return await _text(lambda: actions.record_toggle(camera, clip))

    @router.get("/record/start", response_class=PlainTextResponse)
    async def record_start(clip: str | None = None):
        camera = supervisor.require()
        return await _text(lambda: actions.record_set(camera, True, clip))

    @router.get("/record/stop", response_class=PlainTextResponse)
    async def record_stop():
        camera = supervisor.require()
        return await _text(lambda: actions.record_set(camera, False))

    # ---- ISO
    @router.get("/iso/up", response_class=PlainTextResponse)
    async def iso_up():
        camera = supervisor.require()
        return await _text(lambda: actions.iso_step(camera, 1))

    @router.get("/iso/down", response_class=PlainTextResponse)
    async def iso_down():
        camera = supervisor.require()
        return await _text(lambda: actions.iso_step(camera, -1))

    @router.get("/iso/{value}", response_class=PlainTextResponse)
    async def iso_set(value: int):
        camera = supervisor.require()
        return await _text(lambda: actions.iso_set(camera, value))

    # ---- shutter
    @router.get("/shutter/up", response_class=PlainTextResponse)
    async def shutter_up():
        camera = supervisor.require()
        return await _text(lambda: actions.shutter_step(camera, 1))

    @router.get("/shutter/down", response_class=PlainTextResponse)
    async def shutter_down():
        camera = supervisor.require()
        return await _text(lambda: actions.shutter_step(camera, -1))

    @router.get("/shutter/angle/{value}", response_class=PlainTextResponse)
    async def shutter_angle(value: int):
        """Angle in hundredths of a degree: 18000 is 180 degrees."""
        camera = supervisor.require()
        return await _text(lambda: actions.shutter_set_angle(camera, value))

    @router.get("/shutter/{value}", response_class=PlainTextResponse)
    async def shutter_speed(value: int):
        """Speed as a denominator: 50 is 1/50 s."""
        camera = supervisor.require()
        return await _text(lambda: actions.shutter_set_speed(camera, value))

    # ---- white balance
    @router.get("/wb/auto", response_class=PlainTextResponse)
    async def wb_auto():
        camera = supervisor.require()
        return await _text(lambda: actions.wb_auto(camera))

    @router.get("/wb/warmer", response_class=PlainTextResponse)
    async def wb_warmer(by: int = 500):
        camera = supervisor.require()
        return await _text(lambda: actions.wb_step(camera, by))

    @router.get("/wb/cooler", response_class=PlainTextResponse)
    async def wb_cooler(by: int = 500):
        camera = supervisor.require()
        return await _text(lambda: actions.wb_step(camera, -by))

    @router.get("/wb/preset/{name}", response_class=PlainTextResponse)
    async def wb_preset(name: str):
        camera = supervisor.require()
        return await _text(lambda: actions.wb_preset(camera, name))

    @router.get("/wb/{kelvin}", response_class=PlainTextResponse)
    async def wb_set(kelvin: int):
        camera = supervisor.require()
        return await _text(lambda: actions.wb_set(camera, kelvin))

    @router.get("/tint/{value}", response_class=PlainTextResponse)
    async def tint_set(value: int):
        camera = supervisor.require()
        return await _text(lambda: actions.tint_set(camera, value))

    # ---- lens
    @router.get("/iris/open", response_class=PlainTextResponse)
    async def iris_open(by: float = 5.0):
        camera = supervisor.require()
        return await _text(lambda: actions.iris_nudge(camera, by))

    @router.get("/iris/close", response_class=PlainTextResponse)
    async def iris_close(by: float = 5.0):
        camera = supervisor.require()
        return await _text(lambda: actions.iris_nudge(camera, -by))

    @router.get("/focus/auto", response_class=PlainTextResponse)
    async def focus_auto():
        camera = supervisor.require()
        return await _text(lambda: actions.focus_auto(camera))

    # ---- auto exposure
    @router.get("/ae/toggle", response_class=PlainTextResponse)
    async def ae_toggle(type: str = "Shutter"):
        camera = supervisor.require()
        return await _text(lambda: actions.autoexposure_toggle(camera, type))

    @router.get("/ae/off", response_class=PlainTextResponse)
    async def ae_off():
        camera = supervisor.require()
        return await _text(lambda: actions.autoexposure_set(camera, "Off"))

    # ---- presets and colour
    @router.get("/preset/{name}", response_class=PlainTextResponse)
    async def preset_recall(name: str):
        camera = supervisor.require()
        return await _text(lambda: actions.preset_recall(camera, name))

    @router.get("/preset/save/{name}", response_class=PlainTextResponse)
    async def preset_save(name: str):
        camera = supervisor.require()
        return await _text(lambda: actions.preset_save(camera, name))

    # ---- monitoring overlays and camera body
    @router.get("/tally", response_class=PlainTextResponse)
    async def tally() -> str:
        return actions.tally_summary(supervisor.require())

    @router.get("/colorbars/toggle", response_class=PlainTextResponse)
    async def colorbars():
        camera = supervisor.require()
        return await _text(lambda: actions.flag_toggle(camera, "/camera/colorBars"))

    @router.get("/monitor/{overlay}/toggle", response_class=PlainTextResponse)
    async def monitor_toggle(overlay: str, display: str | None = None):
        """Toggle zebra, falseColor, focusAssist, frameGuide, cleanFeed and friends."""
        camera = supervisor.require()

        async def run() -> str:
            return await actions.flag_toggle(
                camera, actions.monitoring_path(camera, overlay, display)
            )

        return await _text(run)

    @router.get("/monitor/{overlay}/{state}", response_class=PlainTextResponse)
    async def monitor_set(overlay: str, state: str, display: str | None = None):
        if state not in ("on", "off"):
            raise HTTPException(status_code=404, detail="state must be 'on' or 'off'")
        camera = supervisor.require()

        async def run() -> str:
            return await actions.flag_set(
                camera, actions.monitoring_path(camera, overlay, display), state == "on"
            )

        return await _text(run)

    @router.get("/saturation/{value}", response_class=PlainTextResponse)
    async def saturation(value: float):
        camera = supervisor.require()
        return await _text(lambda: actions.saturation_set(camera, value))

    return router


# ---------------------------------------------------------------------- api

def _api_router(supervisor: Supervisor) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["api"])

    @router.get("/state")
    async def state() -> dict[str, Any]:
        camera = supervisor.camera
        return {
            "connected": camera.connected,
            "error": supervisor.last_error,
            "camera": {
                "host": supervisor.settings.camera_host,
                "apiBase": supervisor.settings.api_base,
                "mediaManager": supervisor.settings.media_manager_url,
                "identity": camera.identity,
            },
            "supported": sorted(camera.supported),
            "pushed": sorted(camera.pushed),
            "displays": camera.displays,
            "overlays": sorted(
                path for path in camera.supported
                if path.startswith("/monitoring/") or path == "/camera/colorBars"
            ),
            "presets": actions.preset_names(camera),
            "activePreset": actions.preset_active(camera),
            "wbPresets": dict(sorted(ladders.WB_PRESETS.items())),
            "state": camera.snapshot(),
        }

    @router.get("/diagnostics", response_class=PlainTextResponse)
    async def diagnostics() -> str:
        """Every probed endpoint and what the camera answered.

        This is the thing to look at when a control is missing from the UI:
        404 and 501 mean the camera genuinely does not have it, anything else
        means the probe failed and the control was lost for the wrong reason.
        """
        camera = supervisor.camera
        results = camera.probe_results
        if not results:
            return f"not connected: {supervisor.last_error or 'no camera'}"

        lines = [
            f"camera     {supervisor.settings.api_base}",
            f"connected  {camera.connected}",
            f"identity   {camera.identity or '(none reported)'}",
            "",
            f"{len(camera.supported)} supported, "
            f"{len(camera.pushed)} pushed over websocket, "
            f"{len(camera.supported) - len(camera.pushed)} polled",
            "",
        ]
        for path, status in sorted(results.items()):
            if status is not None and 200 <= status < 300:
                note = "ok" if status != 204 else "ok (supported, nothing to report)"
            elif status in (404, 501):
                note = "not supported by this camera"
            elif status is None:
                note = "NO RESPONSE - probe failed, control lost for the wrong reason"
            else:
                note = "UNEXPECTED - control lost for the wrong reason"
            lines.append(f"{str(status or '---'):>4}  {path:<44} {note}")

        if camera.displays:
            lines += ["", f"monitoring displays: {', '.join(camera.displays)}"]

        from .camera import _event_names

        names = sorted(_event_names(camera.event_list))
        lines += ["", f"/event/list returned: {camera.event_list!r}"]
        if names:
            lines += [
                "",
                f"{len(names)} websocket-subscribable properties reported "
                f"({len(camera.pushed)} of them in use):",
            ]
            lines += [
                f"      {'push' if name in camera.pushed else '    '}  {name}"
                for name in names
            ]
        else:
            lines += [
                "",
                "The camera reported no subscribable properties, so every value is",
                "polled. Button state still works, just on the poll interval rather",
                "than instantly.",
            ]
        return "\n".join(lines)

    @router.get("/set/{control}", response_class=PlainTextResponse)
    async def set_control(control: str, v: float = Query(...)):
        """Continuous controls, for the web page's sliders."""
        camera = supervisor.require()
        handlers: dict[str, Callable[[], Awaitable[str]]] = {
            "iso": lambda: actions.iso_set(camera, int(v)),
            "gain": lambda: actions.gain_set(camera, int(v)),
            "wb": lambda: actions.wb_set(camera, int(v)),
            "tint": lambda: actions.tint_set(camera, int(v)),
            "iris": lambda: actions.iris_set(camera, v),
            "focus": lambda: actions.focus_set(camera, v),
            "zoom": lambda: actions.zoom_set(camera, v),
            "saturation": lambda: actions.saturation_set(camera, v),
            "shutter": lambda: actions.shutter_set_speed(camera, int(v)),
        }
        if control not in handlers:
            raise HTTPException(status_code=404, detail=f"unknown control {control!r}")
        return await _text(handlers[control])

    @router.post("/raw", response_class=PlainTextResponse)
    async def raw(payload: dict[str, Any]):
        """Escape hatch: PUT any endpoint, for things the UI does not cover."""
        path = payload.get("path")
        if not isinstance(path, str) or not path.startswith("/"):
            raise HTTPException(status_code=400, detail="'path' must start with '/'")
        camera = supervisor.require()

        async def run() -> str:
            await camera.put(path, payload.get("body"))
            return f"PUT {path} ok"

        return await _text(run)

    @router.websocket("/ws")
    async def websocket(socket: WebSocket) -> None:
        await socket.accept()
        camera = supervisor.camera
        try:
            await socket.send_json(camera.snapshot_message())
            async with camera.listen() as queue:

                async def pump() -> None:
                    while True:
                        await socket.send_json(await queue.get())

                async def watch_for_close() -> None:
                    # A client can go away while we are idle between updates.
                    # Reading is the only way to notice, so race it against the
                    # pump and let whichever finishes first end the connection.
                    while True:
                        await socket.receive()

                await _race(pump(), watch_for_close())
        except (WebSocketDisconnect, asyncio.CancelledError):
            pass
        except RuntimeError:
            # Socket closed underneath us mid-send.
            pass

    return router


async def _race(*coros: Any) -> None:
    """Run coroutines until the first finishes, then cancel the rest."""
    tasks = [asyncio.ensure_future(coro) for coro in coros]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
