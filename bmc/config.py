"""Runtime configuration, from environment variables or the command line."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    """Everything the service needs to find and talk to a camera."""

    camera_host: str = field(
        default_factory=lambda: os.environ.get("BMC_CAMERA", "micro-studio-g2.local")
    )
    camera_scheme: str = field(
        default_factory=lambda: os.environ.get("BMC_SCHEME", "http")
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
