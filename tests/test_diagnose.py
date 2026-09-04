"""The connectivity diagnostic.

Its whole value is telling apart faults that all present as "cannot reach the
API" but have completely different fixes.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from bmc.config import Settings
from bmc.diagnose import _split_host, diagnose
from tests.conftest import Background, free_port
from tools import mock_camera


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("camera.local", ("camera.local", None)),
        ("camera.local:8080", ("camera.local", 8080)),
        ("192.168.1.42", ("192.168.1.42", None)),
        ("192.168.1.42:80", ("192.168.1.42", 80)),
        ("[fe80::1]:443", ("fe80::1", 443)),
        ("[fe80::1]", ("fe80::1", None)),
        # A bare IPv6 literal has colons but no port.
        ("fe80::1:2:3:4", ("fe80::1:2:3:4", None)),
    ],
)
def test_host_and_port_are_separated(value, expected):
    assert _split_host(value) == expected


async def test_a_working_camera_is_reported_as_working(camera_state):
    port = free_port()
    settings = Settings(camera_host=f"127.0.0.1:{port}", camera_scheme="http")
    async with Background(mock_camera.app, port):
        report = await diagnose(settings)
    assert "The control API is answering" in report
    assert "GET /control/api/v1/system" in report


async def test_a_web_server_without_an_api_is_named_as_such():
    """The reported symptom: storage reachable in a browser, API not answering.

    Both are served by the same process on the camera, so this is a fault on the
    camera rather than anything to reconfigure locally -- and the advice has to
    say so, not repeat "check the web media manager is enabled".
    """
    app = FastAPI()

    @app.get("/")
    async def root():
        return HTMLResponse("<h1>Web Media Manager</h1>")

    @app.get("/{path:path}")
    async def gone(path: str):
        return JSONResponse({"error": "not found"}, status_code=404)

    port = free_port()
    settings = Settings(camera_host=f"127.0.0.1:{port}", camera_scheme="http")
    async with Background(app, port):
        report = await diagnose(settings)

    assert "web server is answering but the control API is not" in report
    assert "Power-cycle the camera" in report
    assert "web media manager is enabled" not in report


async def test_nothing_listening_is_distinguished_from_a_bad_name():
    settings = Settings(
        camera_host=f"127.0.0.1:{free_port()}", camera_scheme="http", request_timeout=2
    )
    report = await diagnose(settings)
    assert "Nothing is listening" in report
    assert "did not resolve" not in report


async def test_a_name_that_does_not_resolve_says_so_first():
    """Every later step fails as a consequence, so it must not be blamed."""
    settings = Settings(
        camera_host="no-such-camera-here.invalid", camera_scheme="http", request_timeout=2
    )
    report = await diagnose(settings)
    assert "The name did not resolve" in report
    assert "Nothing is listening" not in report.split("Conclusion")[1]
