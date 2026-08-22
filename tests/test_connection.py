"""Connection handling: URL parsing, TLS, and scheme fallback.

The camera serves HTTPS with a self-signed certificate issued to its mDNS name,
so every one of these paths has to work without a CA and without hostname
verification.
"""

from __future__ import annotations

import asyncio
import shutil
import ssl
import subprocess

import httpx
import pytest
import uvicorn

from bmc.app import create_app
from bmc.config import Settings, parse_camera
from tests.conftest import Background, free_port
from tools import mock_camera


# ------------------------------------------------------------------ parsing

@pytest.mark.parametrize(
    ("value", "expected"),
    [
        # The URL Blackmagic Camera Setup shows, pasted verbatim.
        ("https://Micro-Studio-Camera-4K-G2.local", ("Micro-Studio-Camera-4K-G2.local", "https")),
        ("http://cam.local/", ("cam.local", "http")),
        ("cam.local", ("cam.local", None)),
        ("192.168.1.42", ("192.168.1.42", None)),
        ("192.168.1.42:8080", ("192.168.1.42:8080", None)),
        ("  https://cam.local/  ", ("cam.local", "https")),
    ],
)
def test_parse_camera_accepts_hosts_and_urls(value, expected):
    assert parse_camera(value) == expected


def test_urls_are_built_from_the_scheme():
    settings = Settings(camera_host="Micro-Studio-Camera-4K-G2.local", camera_scheme="https")
    assert settings.api_base == "https://Micro-Studio-Camera-4K-G2.local/control/api/v1"
    assert settings.websocket_url.startswith("wss://")
    assert settings.with_scheme("http").websocket_url.startswith("ws://")
    assert settings.other_scheme == "http"


# ---------------------------------------------------------------------- TLS

def test_ssl_context_skips_verification_by_default():
    """There is no CA for a self-signed certificate to be checked against."""
    context = Settings(camera_scheme="https").ssl_context()
    assert context is not None
    assert context.check_hostname is False
    assert context.verify_mode == ssl.CERT_NONE


def test_ssl_context_can_be_made_strict():
    context = Settings(camera_scheme="https", verify_tls=True).ssl_context()
    assert context.check_hostname is True
    assert context.verify_mode == ssl.CERT_REQUIRED


def test_plain_http_needs_no_ssl_context():
    assert Settings(camera_scheme="http").ssl_context() is None


# -------------------------------------------------------------- integration

@pytest.fixture
def self_signed_cert(tmp_path):
    """A certificate issued to the camera's mDNS name, as the camera's own is."""
    if shutil.which("openssl") is None:
        pytest.skip("openssl not available")
    key, crt = tmp_path / "cam.key", tmp_path / "cam.crt"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
         "-keyout", str(key), "-out", str(crt),
         "-days", "1", "-subj", "/CN=Micro-Studio-Camera-4K-G2.local"],
        check=True, capture_output=True,
    )
    return key, crt


class TlsBackground(Background):
    def __init__(self, app, port, key, crt):
        super().__init__(app, port)
        self.server = uvicorn.Server(
            uvicorn.Config(
                app, host="127.0.0.1", port=port, log_level="error",
                ssl_keyfile=str(key), ssl_certfile=str(crt),
            )
        )


async def _await_connection(client: httpx.AsyncClient) -> dict:
    for _ in range(200):
        body = (await client.get("/api/state")).json()
        if body.get("connected"):
            return body
        await asyncio.sleep(0.05)
    raise AssertionError("service never connected")


async def test_connects_over_https_to_a_self_signed_camera(self_signed_cert, camera_state):
    """Connecting to 127.0.0.1 with a cert issued to the mDNS name must still work."""
    key, crt = self_signed_cert
    camera_port, service_port = free_port(), free_port()
    settings = Settings(
        camera_host=f"127.0.0.1:{camera_port}", camera_scheme="https", poll_interval=0.2
    )
    async with TlsBackground(mock_camera.app, camera_port, key, crt):
        async with Background(create_app(settings), service_port):
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{service_port}", timeout=10
            ) as client:
                body = await _await_connection(client)
                assert body["camera"]["apiBase"].startswith("https://")
                assert (await client.get("/deck/iso/800")).text == "ISO 800"


async def test_websocket_push_works_over_wss(self_signed_cert, camera_state):
    """Record state is never polled, so it can only arrive over the TLS websocket."""
    key, crt = self_signed_cert
    camera_port, service_port = free_port(), free_port()
    settings = Settings(
        camera_host=f"127.0.0.1:{camera_port}", camera_scheme="https", poll_interval=0.2
    )
    async with TlsBackground(mock_camera.app, camera_port, key, crt):
        async with Background(create_app(settings), service_port):
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{service_port}", timeout=10
            ) as client:
                await _await_connection(client)

                # Change it directly on the camera, bypassing the service.
                async with httpx.AsyncClient(verify=False, timeout=10) as direct:
                    await direct.put(
                        f"https://127.0.0.1:{camera_port}/control/api/v1/transports/0/record",
                        json={"recording": True},
                    )

                async def wait_for_push() -> None:
                    while True:
                        body = (await client.get("/api/state")).json()
                        record = body["state"].get("/transports/0/record") or {}
                        if record.get("recording"):
                            return
                        await asyncio.sleep(0.1)

                await asyncio.wait_for(wait_for_push(), timeout=15)


async def test_a_proxy_in_the_environment_is_ignored(monkeypatch, camera_state):
    """A camera on the LAN must never be reached through a proxy.

    httpx and websockets both honour HTTP_PROXY/HTTPS_PROXY by default, which
    sends requests for a .local mDNS name to a proxy that cannot resolve it.
    Pointing the proxy variables at a dead port proves we bypass them.
    """
    dead_proxy = f"http://127.0.0.1:{free_port()}"
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        monkeypatch.setenv(name, dead_proxy)
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)

    camera_port, service_port = free_port(), free_port()
    settings = Settings(
        camera_host=f"127.0.0.1:{camera_port}", camera_scheme="http", poll_interval=0.2
    )
    async with Background(mock_camera.app, camera_port):
        async with Background(create_app(settings), service_port):
            # trust_env=False here is about this test's own client reaching the
            # service, not about what the service does with the camera.
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{service_port}", timeout=10, trust_env=False
            ) as client:
                await _await_connection(client)
                assert (await client.get("/deck/iso/800")).text == "ISO 800"

                # The websocket must bypass the proxy too, so a push-only
                # property still arrives.
                async with httpx.AsyncClient(trust_env=False, timeout=10) as direct:
                    await direct.put(
                        f"http://127.0.0.1:{camera_port}/control/api/v1/transports/0/record",
                        json={"recording": True},
                    )

                async def wait_for_push() -> None:
                    while True:
                        body = (await client.get("/api/state")).json()
                        record = body["state"].get("/transports/0/record") or {}
                        if record.get("recording"):
                            return
                        await asyncio.sleep(0.1)

                await asyncio.wait_for(wait_for_push(), timeout=15)


async def test_falls_back_to_the_other_scheme(self_signed_cert, camera_state):
    """Asking for HTTP when only HTTPS answers should still connect."""
    key, crt = self_signed_cert
    camera_port, service_port = free_port(), free_port()
    settings = Settings(
        camera_host=f"127.0.0.1:{camera_port}", camera_scheme="http", poll_interval=0.2
    )
    async with TlsBackground(mock_camera.app, camera_port, key, crt):
        async with Background(create_app(settings), service_port):
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{service_port}", timeout=10
            ) as client:
                body = await _await_connection(client)
                assert body["camera"]["apiBase"].startswith("https://")
