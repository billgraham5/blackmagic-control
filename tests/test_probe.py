"""Probe behaviour against a camera that behaves like real embedded hardware."""

from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse

from bmc.app import create_app
from bmc.config import Settings
from tests.conftest import Background, free_port


class CountingCamera:
    """Records how many requests are in flight at once, and can fail on demand."""

    def __init__(
        self,
        fail_first: set[str] | None = None,
        always_fail: set[str] | None = None,
    ) -> None:
        self.in_flight = 0
        self.max_in_flight = 0
        self.fail_first = fail_first or set()
        self.always_fail = always_fail or set()
        self.seen: dict[str, int] = {}
        self.app = FastAPI()
        self.app.get("/control/api/v1/{path:path}")(self._read)

    async def _read(self, path: str) -> Response:
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(0.01)
            key = f"/{path}"
            self.seen[key] = self.seen.get(key, 0) + 1
            if key in self.always_fail:
                return Response(status_code=503)
            if key in self.fail_first and self.seen[key] == 1:
                return Response(status_code=503)
            if key == "/event/list":
                return JSONResponse({"events": []})
            if key in ("/video/iso", "/video/shutter", "/system"):
                return JSONResponse({"iso": 400} if key == "/video/iso" else {})
            return JSONResponse({"error": "not supported"}, status_code=404)
        finally:
            self.in_flight -= 1


async def _run(camera: CountingCamera):
    camera_port, service_port = free_port(), free_port()
    settings = Settings(
        camera_host=f"127.0.0.1:{camera_port}", camera_scheme="http", poll_interval=5
    )
    async with Background(camera.app, camera_port):
        async with Background(create_app(settings), service_port):
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{service_port}", timeout=20, trust_env=False
            ) as client:
                for _ in range(400):
                    if (await client.get("/api/state")).json().get("connected"):
                        break
                    await asyncio.sleep(0.05)
                else:
                    raise AssertionError("never connected")
                yield_state = (await client.get("/api/state")).json()
                diagnostics = (await client.get("/api/diagnostics")).text
                return yield_state, diagnostics


async def test_probe_never_opens_parallel_connections():
    """The camera is a small embedded server; concurrent probes make it drop requests."""
    camera = CountingCamera()
    await _run(camera)
    assert camera.max_in_flight == 1, (
        f"probed {camera.max_in_flight} endpoints at once; "
        "an embedded camera drops requests under that load"
    )


async def test_a_dropped_request_is_retried_not_treated_as_unsupported():
    """One flaky response must not cost the user a control for the whole session."""
    camera = CountingCamera(fail_first={"/video/iso"})
    state, diagnostics = await _run(camera)
    assert "/video/iso" in state["supported"]
    assert camera.seen["/video/iso"] == 2  # failed once, retried, succeeded


async def test_a_persistent_5xx_is_retried_then_reported():
    """A 503 that survives every retry must be visible, not silently dropped."""
    camera = CountingCamera(always_fail={"/lens/iris"})
    state, diagnostics = await _run(camera)
    assert "/lens/iris" not in state["supported"]
    assert camera.seen["/lens/iris"] == 3  # retried, not given up on immediately
    assert "503" in diagnostics and "/lens/iris" in diagnostics


async def test_a_404_is_taken_at_face_value():
    """Definitive answers must not be retried -- that would triple startup time."""
    camera = CountingCamera()
    await _run(camera)
    assert camera.seen["/camera/tallyStatus"] == 1


class QuirkyCamera:
    """A camera answering the way real hardware does, not the way docs suggest.

    Taken from a real Micro Studio 4K G2: /system answers 204 with no body,
    /media/active answers 204 when no disk is mounted, /system/videoFormat
    answers 501, and /event/list returns a bare array rather than an object.
    """

    def __init__(self, event_list) -> None:
        self.event_list = event_list
        self.app = FastAPI()
        self.app.get("/control/api/v1/{path:path}")(self._read)

    async def _read(self, path: str) -> Response:
        key = f"/{path}"
        if key == "/event/list":
            return JSONResponse(self.event_list)
        if key in ("/system", "/media/active"):
            return Response(status_code=204)
        if key in ("/system/videoFormat", "/system/codecFormat", "/video/ndFilter"):
            return Response(status_code=501)
        if key in ("/video/iso", "/transports/0/record"):
            return JSONResponse({"iso": 400} if key == "/video/iso" else {"recording": False})
        return JSONResponse({"error": "not supported"}, status_code=404)


async def test_a_204_means_supported_with_nothing_to_report():
    """No disk mounted is not the same as no media endpoint."""
    camera = QuirkyCamera(event_list=[])
    state, diagnostics = await _run(camera)
    supported = set(state["supported"])
    assert "/system" in supported
    assert "/media/active" in supported
    assert "/video/ndFilter" not in supported  # 501 is a real "no"
    assert "UNEXPECTED" not in diagnostics


async def test_event_list_as_a_bare_array_still_drives_subscriptions():
    """Firmware returns either {"events": [...]} or a plain list."""
    camera = QuirkyCamera(event_list=["/transports/0/record", "/system"])
    state, _ = await _run(camera)
    assert "/transports/0/record" in state["pushed"]


async def test_event_list_as_an_object_still_drives_subscriptions():
    camera = QuirkyCamera(event_list={"events": ["/transports/0/record"]})
    state, _ = await _run(camera)
    assert "/transports/0/record" in state["pushed"]
