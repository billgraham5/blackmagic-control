"""A fake Micro Studio Camera 4K G2, for developing without hardware.

Modelled on the real thing including the awkward parts:

* endpoints the published spec advertises but this body lacks (ND filter,
  monitoring, tally) return 404
* values are clamped and snapped to the camera's own legal set, so a read-back
  does not always match what was written
* ``/event/list`` advertises only the narrow older-firmware property set, which
  forces the service down its polling path for exposure values

Run it with::

    python -m uvicorn tools.mock_camera:app --port 9000

then point the service at it::

    python -m bmc --camera localhost:9000
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, FastAPI, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

ISO_LADDER = (100, 200, 400, 800, 1600, 3200, 6400, 12800, 25600)

#: Only these push over the websocket, matching 2024-era firmware.
PUSHED = [
    "/media/workingset",
    "/media/active",
    "/system",
    "/system/codecFormat",
    "/system/videoFormat",
    "/system/format",
    "/timelines/0",
    "/transports/0",
    "/transports/0/stop",
    "/transports/0/play",
    "/transports/0/playback",
    "/transports/0/record",
    "/transports/0/timecode",
]


def _snap(value: float, ladder: tuple[int, ...]) -> int:
    return min(ladder, key=lambda rung: abs(rung - value))


class MockCamera:
    def __init__(self) -> None:
        self.state: dict[str, Any] = {
            "/system": {
                "codecFormat": {"codec": "BRaw:8_1", "container": "MOV"},
                "videoFormat": {
                    "name": "3840x2160p29.97", "frameRate": "29.97",
                    "width": 3840, "height": 2160, "interlaced": False,
                },
            },
            "/system/codecFormat": {"codec": "BRaw:8_1", "container": "MOV"},
            "/system/videoFormat": {
                "name": "3840x2160p29.97", "frameRate": "29.97",
                "width": 3840, "height": 2160, "interlaced": False,
            },
            "/system/format": {
                "codec": "BRaw:8_1", "frameRate": "29.97",
                "offSpeedEnabled": False, "offSpeedFrameRate": 30,
                "minOffSpeedFrameRate": 5, "maxOffSpeedFrameRate": 60,
                "recordResolution": {"width": 3840, "height": 2160},
                "sensorResolution": {"width": 3840, "height": 2160},
            },
            "/transports/0": {"mode": "InputPreview"},
            "/transports/0/record": {"recording": False},
            "/transports/0/timecode": {"timecode": 0, "clip": 0},
            "/video/iso": {"iso": 400},
            "/video/gain": {"gain": 0},
            "/video/shutter": {
                "continuousShutterAutoExposure": False, "shutterSpeed": 50,
            },
            "/video/whiteBalance": {"whiteBalance": 5600},
            "/video/whiteBalanceTint": {"whiteBalanceTint": 0},
            "/video/autoExposure": {"mode": {"mode": "Off", "type": ""}},
            # apertureStop is an APEX value: 4.0 is f/4, 6.0 is f/8.
            "/lens/iris": {
                "continuousApertureAutoExposure": False,
                "apertureStop": 4.0, "normalised": 0.5, "apertureNumber": 8,
            },
            # A 12-35mm f/2.8: APEX 2.97 to 8.0, i.e. f/2.8 to f/16.
            "/lens/iris/description": {
                "controllable": True,
                "apertureStop": {"min": 2.97, "max": 8.0},
            },
            "/lens/zoom": {"focalLength": 24, "normalised": 0.0},
            "/lens/focus": {"focus": 0.5},
            "/colorCorrection/lift": {"red": 0.0, "green": 0.0, "blue": 0.0, "luma": 0.0},
            "/colorCorrection/gamma": {"red": 0.0, "green": 0.0, "blue": 0.0, "luma": 0.0},
            "/colorCorrection/gain": {"red": 1.0, "green": 1.0, "blue": 1.0, "luma": 1.0},
            "/colorCorrection/offset": {"red": 0.0, "green": 0.0, "blue": 0.0, "luma": 0.0},
            "/colorCorrection/contrast": {"pivot": 0.5, "adjust": 1.0},
            "/colorCorrection/color": {"hue": 0.0, "saturation": 1.0},
            "/colorCorrection/lumaContribution": {"lumaContribution": 1.0},
            "/presets": {"presets": ["Studio A", "Interview", "Stage wide"]},
            "/presets/active": {"preset": "Studio A"},
            "/media/active": {"workingsetIndex": 0, "deviceName": "usb1"},
            "/media/workingset": {
                "size": 1,
                "workingset": [{
                    "index": 0, "activeDisk": True, "volume": "SAMSUNG-T7",
                    "deviceName": "usb1", "remainingRecordTime": 8460,
                    "totalSpace": 1000204886016, "remainingSpace": 743102443520,
                    "clipCount": 12,
                }],
            },
            "/audio/channel/0/level": {"gain": -6.0, "normalised": 0.7},
            "/audio/channel/1/level": {"gain": -6.0, "normalised": 0.7},
            # Reported as a bare list to exercise the other /supported* shape.
            "/video/supportedISOs": [100, 200, 400, 800, 1600, 3200, 6400, 12800, 25600],
            "/video/supportedShutters": {
                "shutters": [24, 25, 30, 50, 60, 100, 125, 250, 500, 1000],
            },
            "/camera/colorBars": {"enabled": False},
            "/camera/tallyStatus": {"tally": "off"},
            "/monitoring/display": {"displays": ["MainSDI", "HDMI", "FrontUSBC"]},
            "/monitoring/focusAssist": {"enabled": False, "mode": "Peak", "color": "Red"},
            # Zebra carries a level, so a naive toggle that sends only the flag
            # would drop it.
            "/monitoring/MainSDI/zebra": {"enabled": False, "level": 75},
            "/monitoring/MainSDI/falseColor": {"enabled": False},
            "/monitoring/MainSDI/cleanFeed": {"enabled": False},
            "/monitoring/MainSDI/focusAssist": {"enabled": False, "mode": "Peak"},
            "/monitoring/MainSDI/frameGuide": {"enabled": False, "ratio": 1.78},
            "/monitoring/MainSDI/safeArea": {"enabled": False},
            "/monitoring/MainSDI/frameGrids": {"enabled": False},
            "/monitoring/MainSDI/displayLUT": {"enabled": False},
            "/monitoring/HDMI/zebra": {"enabled": False, "level": 75},
            "/monitoring/HDMI/falseColor": {"enabled": False},
            "/monitoring/HDMI/cleanFeed": {"enabled": False},
            "/monitoring/FrontUSBC/zebra": {"enabled": False, "level": 75},
            "/monitoring/FrontUSBC/falseColor": {"enabled": False},
            "/monitoring/FrontUSBC/cleanFeed": {"enabled": False},
            "/event/list": {"events": PUSHED},
            # Present only in the documentation below, never in the service's
            # built-in list, so discovery is the only way to find them.
            "/camera/id": {"id": "studio-left"},
            "/media/slots": {"slots": [{"index": 0, "state": "Mounted"}]},
            "/monitoring/MainSDI/brightness": {"brightness": 0.5},
            "/monitoring/HDMI/brightness": {"brightness": 0.5},
            "/monitoring/FrontUSBC/brightness": {"brightness": 0.5},
        }
        self.listeners: set[asyncio.Queue[str]] = set()

    def set(self, path: str, value: Any) -> None:
        if self.state.get(path) == value:
            return
        self.state[path] = value
        message = json.dumps({
            "type": "event",
            "data": {"action": "propertyValueChanged", "property": path, "value": value},
        })
        for queue in list(self.listeners):
            with_suppress(queue, message)


def with_suppress(queue: asyncio.Queue[str], message: str) -> None:
    try:
        queue.put_nowait(message)
    except asyncio.QueueFull:
        pass


camera = MockCamera()
router = APIRouter(prefix="/control/api/v1")


#: Supported, but nothing to report right now -- as the real camera answers for
#: /media/active with no disk mounted.
EMPTY: set[str] = set()

#: Implemented by the API but not by this model, answered with 501 not 404.
NOT_IMPLEMENTED: set[str] = set()


@router.get("/{path:path}")
async def read(path: str) -> Response:
    key = f"/{path}"
    if key in NOT_IMPLEMENTED:
        return Response(status_code=501)
    if key in EMPTY:
        return Response(status_code=204)
    if key not in camera.state:
        return JSONResponse({"error": "not supported"}, status_code=404)
    return JSONResponse(camera.state[key])


@router.put("/video/iso")
async def put_iso(body: dict[str, Any]) -> Response:
    iso = body.get("iso")
    if not isinstance(iso, (int, float)):
        return JSONResponse({"error": "iso must be a number"}, status_code=400)
    if not 100 <= iso <= 25600:
        return JSONResponse({"error": "iso out of range"}, status_code=400)
    snapped = _snap(float(iso), ISO_LADDER)
    camera.set("/video/iso", {"iso": snapped})
    return Response(status_code=204)


@router.put("/video/whiteBalance")
async def put_wb(body: dict[str, Any]) -> Response:
    kelvin = body.get("whiteBalance")
    if not isinstance(kelvin, (int, float)):
        return JSONResponse({"error": "whiteBalance must be a number"}, status_code=400)
    if not 2500 <= kelvin <= 10000:
        return JSONResponse({"error": "whiteBalance out of range"}, status_code=400)
    camera.set("/video/whiteBalance", {"whiteBalance": int(kelvin)})
    return Response(status_code=204)


@router.put("/video/whiteBalance/doAuto")
async def put_wb_auto() -> Response:
    camera.set("/video/whiteBalance", {"whiteBalance": 5100})
    return Response(status_code=204)


@router.put("/video/shutter")
async def put_shutter(body: dict[str, Any]) -> Response:
    if isinstance(body.get("shutterSpeed"), (int, float)):
        camera.set("/video/shutter", {
            "continuousShutterAutoExposure": False,
            "shutterSpeed": int(body["shutterSpeed"]),
        })
        return Response(status_code=204)
    if isinstance(body.get("shutterAngle"), (int, float)):
        camera.set("/video/shutter", {
            "continuousShutterAutoExposure": False,
            "shutterAngle": int(body["shutterAngle"]),
        })
        return Response(status_code=204)
    return JSONResponse({"error": "need shutterSpeed or shutterAngle"}, status_code=400)


@router.put("/transports/0/record")
async def put_record(body: dict[str, Any]) -> Response:
    recording = bool(body.get("recording"))
    camera.set("/transports/0/record", {"recording": recording})
    camera.set("/transports/0", {"mode": "InputRecord" if recording else "InputPreview"})
    return Response(status_code=204)


@router.put("/lens/focus/doAutoFocus")
async def put_autofocus() -> Response:
    camera.set("/lens/focus", {"focus": 0.61})
    return Response(status_code=204)


#: Some overlays do not apply to some outputs. The camera answers 204 and
#: changes nothing, which is indistinguishable from a broken button unless the
#: service checks.
IGNORED_WRITES: set[str] = {"/monitoring/FrontUSBC/cleanFeed"}


@router.put("/monitoring/{display}/cleanFeed")
async def put_clean_feed(display: str, body: dict[str, Any]) -> Response:
    key = f"/monitoring/{display}/cleanFeed"
    if key not in camera.state:
        return JSONResponse({"error": "display not found"}, status_code=404)
    if key in IGNORED_WRITES:
        return Response(status_code=204)  # accepted, ignored
    merged = dict(camera.state[key])
    merged.update(body)
    camera.set(key, merged)
    return Response(status_code=204)


@router.put("/presets/active")
async def put_preset(body: dict[str, Any]) -> Response:
    name = body.get("preset")
    if name not in camera.state["/presets"]["presets"]:
        return JSONResponse({"error": f"no preset named {name!r}"}, status_code=400)
    camera.set("/presets/active", {"preset": name})
    return Response(status_code=204)


@router.put("/{path:path}")
async def write(path: str, request: Request) -> Response:
    """Generic setter for the remaining properties."""
    key = f"/{path}"
    if key not in camera.state:
        return JSONResponse({"error": "not supported"}, status_code=404)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"error": "body must be JSON"}, status_code=400)
    merged = dict(camera.state[key])
    if isinstance(body, dict):
        merged.update(body)
    camera.set(key, merged)
    return Response(status_code=204)


DOCUMENTATION = """<!doctype html><html><body>
<p>Camera Control REST API</p>
<script>const specs = ["MockControl.yaml"];</script>
</body></html>"""

SPEC = """
openapi: 3.0.1
info: {title: Mock Control API, version: 1.0.0}
servers: [{url: /control/api/v1}]
paths:
  /camera/id:
    get: {summary: Get the camera identifier}
    put:
      summary: Set the camera identifier
      requestBody:
        content:
          application/json:
            schema:
              type: object
              properties:
                id: {type: string, description: Camera identifier}
  /media/slots:
    get: {summary: Get media slot status}
  /monitoring/{displayName}/brightness:
    get: {summary: Get display brightness}
    put:
      summary: Set display brightness
      requestBody:
        content:
          application/json:
            schema:
              type: object
              properties:
                brightness: {type: number, minimum: 0.0, maximum: 1.0}
  /video/autoExposure:
    get: {summary: Get auto exposure mode}
    put:
      summary: Set auto exposure
      requestBody:
        content:
          application/json:
            schema:
              type: object
              properties:
                mode:
                  type: object
                  properties:
                    mode: {type: string, enum: [Off, Continuous, OneShot]}
                    type: {type: string, enum: ["", Iris, Shutter, "Iris,Shutter"]}
"""

app = FastAPI(title="Mock Blackmagic camera")


@app.get("/control/documentation.html")
async def documentation() -> Response:
    return Response(DOCUMENTATION, media_type="text/html")


@app.get("/control/MockControl.yaml")
async def spec() -> Response:
    return Response(SPEC, media_type="application/yaml")


app.include_router(router)


@app.websocket("/control/api/v1/event/websocket")
async def events(socket: WebSocket) -> None:
    await socket.accept()
    queue: asyncio.Queue[str] = asyncio.Queue(maxsize=128)
    camera.listeners.add(queue)
    await socket.send_json({"type": "event", "data": {"action": "websocketOpened"}})

    async def pump() -> None:
        while True:
            await socket.send_text(await queue.get())

    pump_task = asyncio.create_task(pump())
    try:
        while True:
            message = json.loads(await socket.receive_text())
            data = message.get("data") or {}
            action = data.get("action")
            if action == "subscribe":
                wanted = [p for p in (data.get("properties") or []) if p in PUSHED]
                await socket.send_json({
                    "type": "response",
                    "id": message.get("id"),
                    "data": {
                        "action": "subscribe",
                        "properties": wanted,
                        "values": {p: camera.state.get(p) for p in wanted},
                        "success": True,
                    },
                })
            elif action == "listProperties":
                await socket.send_json({
                    "type": "response", "id": message.get("id"),
                    "data": {"action": "listProperties", "properties": PUSHED,
                             "success": True},
                })
    except (WebSocketDisconnect, RuntimeError, ValueError):
        pass
    finally:
        pump_task.cancel()
        camera.listeners.discard(queue)
