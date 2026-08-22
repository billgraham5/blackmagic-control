"""Command line entry point: ``python -m bmc --camera micro-studio-g2.local``."""

from __future__ import annotations

import argparse
import logging

import uvicorn

from .app import create_app
from .config import Settings, parse_camera


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="bmc",
        description="Local control service for a Blackmagic camera's REST API.",
    )
    defaults = Settings()
    parser.add_argument(
        "--camera",
        default=defaults.camera_host,
        help=(
            "camera hostname, host:port, or the full URL shown in Blackmagic "
            "Camera Setup (default: %(default)s)"
        ),
    )
    scheme_group = parser.add_mutually_exclusive_group()
    scheme_group.add_argument(
        "--https",
        dest="scheme",
        action="store_const",
        const="https",
        help="force HTTPS/WSS; needs a certificate generated in Blackmagic Camera Setup",
    )
    scheme_group.add_argument(
        "--http",
        dest="scheme",
        action="store_const",
        const="http",
        help="force plain HTTP/WS",
    )
    parser.add_argument("--host", default=defaults.host, help="bind address")
    parser.add_argument("--port", type=int, default=defaults.port, help="bind port")
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=defaults.poll_interval,
        help="seconds between reads of properties the camera will not push",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # The poll loop reads every unsubscribable property every interval, so httpx
    # at INFO would print tens of lines a second and bury anything useful.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    # A scheme in the URL beats the default; an explicit flag beats both.
    host, url_scheme = parse_camera(args.camera)
    settings = Settings(
        camera_host=host,
        camera_scheme=args.scheme or url_scheme or defaults.camera_scheme,
        host=args.host,
        port=args.port,
        poll_interval=args.poll_interval,
    )

    print(f"camera   {settings.api_base}")
    print(f"web UI   http://localhost:{settings.port}/")
    print(f"deck     http://localhost:{settings.port}/deck/status")

    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        log_level="debug" if args.verbose else "warning",
    )


if __name__ == "__main__":
    main()
