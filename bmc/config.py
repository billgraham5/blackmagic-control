"""Runtime configuration, from environment variables or the command line."""

from __future__ import annotations

import os
import ssl
from dataclasses import dataclass, field, replace
from urllib.parse import urlparse


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    """Everything the service needs to find and talk to a camera."""

    camera_host: str = field(
        default_factory=lambda: os.environ.get(
            "BMC_CAMERA", "Micro-Studio-Camera-4K-G2.local"
        )
    )
    camera_scheme: str = field(
        default_factory=lambda: os.environ.get("BMC_SCHEME", "https")
    )
    verify_tls: bool = field(default_factory=lambda: _env_bool("BMC_VERIFY_TLS", False))
    host: str = field(default_factory=lambda: os.environ.get("BMC_HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: int(os.environ.get("BMC_PORT", "8080")))

    #: How often to re-read properties the camera will not push over its websocket.
    poll_interval: float = field(
        default_factory=lambda: float(os.environ.get("BMC_POLL_INTERVAL", "1.0"))
    )
    request_timeout: float = field(
        default_factory=lambda: float(os.environ.get("BMC_TIMEOUT", "5.0"))
    )

    @property
    def api_base(self) -> str:
        return f"{self.camera_scheme}://{self.camera_host}/control/api/v1"

    @property
    def websocket_url(self) -> str:
        ws_scheme = "wss" if self.camera_scheme == "https" else "ws"
        return f"{ws_scheme}://{self.camera_host}/control/api/v1/event/websocket"

    @property
    def media_manager_url(self) -> str:
        return f"{self.camera_scheme}://{self.camera_host}/"

    @property
    def other_scheme(self) -> str:
        return "http" if self.camera_scheme == "https" else "https"

    def with_scheme(self, scheme: str) -> "Settings":
        return replace(self, camera_scheme=scheme)

    def ssl_context(self) -> ssl.SSLContext | None:
        """TLS settings for the camera, or ``None`` when talking plain HTTP.

        The camera's certificate is self-signed and issued to its mDNS name, so
        verification is off by default -- there is no CA to check it against.
        Set ``BMC_VERIFY_TLS=1`` if you have installed the camera's certificate.
        """
        if self.camera_scheme != "https":
            return None
        context = ssl.create_default_context()
        if not self.verify_tls:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        return context


def parse_camera(value: str) -> tuple[str, str | None]:
    """Split a camera argument into (host, scheme).

    Accepts a bare hostname, ``host:port``, or the full URL that Blackmagic
    Camera Setup displays, so it can be pasted in verbatim.
    """
    value = value.strip().rstrip("/")
    if "://" in value:
        parsed = urlparse(value)
        if parsed.netloc:
            return parsed.netloc, parsed.scheme or None
    return value, None
