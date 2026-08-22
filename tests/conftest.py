"""Test fixtures: a real mock camera and a real service, both over HTTP.

Running both as actual servers rather than stubbing the transport means the
tests exercise the parts most likely to break -- the websocket, the capability
probe, and read-back after write.
"""

from __future__ import annotations

import asyncio
import socket
import sys
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bmc.app import create_app  # noqa: E402
from bmc.config import Settings  # noqa: E402
from tools import mock_camera  # noqa: E402


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class Background:
    """A uvicorn server running inside the test's event loop."""

    def __init__(self, app, port: int) -> None:
        self.port = port
        self.server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
        )
        self._task: asyncio.Task | None = None

    async def __aenter__(self) -> "Background":
        self._task = asyncio.create_task(self.server.serve())
        for _ in range(200):
            if self.server.started:
                return self
            await asyncio.sleep(0.02)
        raise RuntimeError("server did not start")

    async def __aexit__(self, *_) -> None:
        self.server.should_exit = True
        if self._task is not None:
            await asyncio.wait_for(self._task, timeout=10)


@pytest.fixture
def camera_state():
    """A fresh camera for every test."""
    mock_camera.camera = mock_camera.MockCamera()
    return mock_camera.camera


@pytest_asyncio.fixture
async def service(camera_state):
    """Yields an HTTP client pointed at the service, wired to a mock camera."""
    camera_port, service_port = free_port(), free_port()
    settings = Settings(
        camera_host=f"127.0.0.1:{camera_port}",
        camera_scheme="http",
        port=service_port,
        poll_interval=0.2,
    )
    async with Background(mock_camera.app, camera_port):
        async with Background(create_app(settings), service_port):
            base = f"http://127.0.0.1:{service_port}"
            async with httpx.AsyncClient(base_url=base, timeout=10) as client:
                for _ in range(200):
                    response = await client.get("/api/state")
                    if response.json().get("connected"):
                        break
                    await asyncio.sleep(0.05)
                else:
                    raise RuntimeError("service never connected to the mock camera")
                yield client
