"""Layered connectivity check, for when the camera is reachable but the API is not.

"Cannot reach the API" covers several very different faults with very different
fixes: a name that will not resolve, a host that refuses the port, a TLS
handshake that fails, a web server that answers while the control API does not.
This walks the chain one step at a time and reports where it actually stops.

Runs standalone -- the service does not need to be up.
"""

from __future__ import annotations

import asyncio
import socket
import ssl
from dataclasses import dataclass

import httpx

from .config import Settings

#: Paths worth asking for, in the order that isolates the fault.
CHECKS: tuple[tuple[str, str], ...] = (
    ("/", "web media manager"),
    ("/control/documentation.html", "API documentation"),
    ("/control/api/v1/system", "control API"),
    ("/control/api/v1/event/list", "event list"),
)

DEFAULT_PORTS = {"https": 443, "http": 80}


@dataclass
class Step:
    label: str
    ok: bool
    detail: str

    def render(self) -> str:
        mark = "ok  " if self.ok else "FAIL"
        return f"  {mark}  {self.label:<34} {self.detail}"


def _split_host(host: str) -> tuple[str, int | None]:
    """Separate an explicit port, leaving IPv6 literals alone."""
    if host.startswith("["):
        bracket = host.find("]")
        if bracket != -1:
            rest = host[bracket + 1 :]
            port = int(rest[1:]) if rest.startswith(":") and rest[1:].isdigit() else None
            return host[1:bracket], port
    if host.count(":") == 1:
        name, _, port = host.partition(":")
        if port.isdigit():
            return name, int(port)
    return host, None


def resolve(host: str) -> Step:
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        return Step(
            "resolve name",
            False,
            f"{host} did not resolve ({exc.strerror or exc}). A .local name needs "
            "mDNS; try the camera's IP address instead.",
        )
    addresses = sorted({info[4][0] for info in infos})
    return Step("resolve name", True, f"{host} -> {', '.join(addresses)}")


async def tcp(host: str, port: int, timeout: float) -> Step:
    label = f"connect tcp/{port}"
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )
    except asyncio.TimeoutError:
        return Step(label, False, f"timed out after {timeout:.0f}s")
    except OSError as exc:
        return Step(label, False, f"{exc.strerror or exc}")
    writer.close()
    try:
        await writer.wait_closed()
    except OSError:
        pass
    return Step(label, True, "open")


async def tls(host: str, port: int, timeout: float) -> Step:
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, ssl=context), timeout=timeout
        )
    except asyncio.TimeoutError:
        return Step("tls handshake", False, f"timed out after {timeout:.0f}s")
    except (OSError, ssl.SSLError) as exc:
        return Step("tls handshake", False, str(exc))
    peer = writer.get_extra_info("ssl_object")
    detail = "connected"
    if peer is not None:
        cipher = peer.cipher()
        if cipher:
            detail = f"{peer.version()}, {cipher[0]}"
    writer.close()
    try:
        await writer.wait_closed()
    except (OSError, ssl.SSLError):
        pass
    return Step("tls handshake", True, detail)


async def http_checks(base: str, timeout: float) -> list[Step]:
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    steps: list[Step] = []
    async with httpx.AsyncClient(
        timeout=timeout, verify=context, trust_env=False, follow_redirects=True
    ) as client:
        for path, label in CHECKS:
            try:
                response = await client.get(f"{base}{path}")
            except httpx.HTTPError as exc:
                steps.append(Step(f"GET {path}", False, f"{label}: {exc}"))
                continue
            ok = response.status_code < 400
            body = response.text.strip().replace("\n", " ")[:70]
            steps.append(
                Step(f"GET {path}", ok, f"{label}: {response.status_code} {body}".rstrip())
            )
    return steps


def _conclude(steps: list[Step]) -> str:
    by_path = {step.label: step for step in steps}
    root = by_path.get("GET /")
    api = by_path.get("GET /control/api/v1/system")

    if root is not None and root.ok and api is not None and not api.ok:
        return (
            "The camera's web server is answering but the control API is not. Both are\n"
            "served by the same process, so this is not a configuration problem on your\n"
            "side: the API has stopped responding on the camera. Power-cycle the camera,\n"
            "and if it recurs, note the firmware version -- it is a firmware fault, not a\n"
            "network one."
        )
    if api is not None and api.ok:
        return "The control API is answering. Nothing wrong at this layer."
    if any(not step.ok and step.label.startswith("connect") for step in steps):
        return (
            "Nothing is listening on that port. The camera is off the network, on a\n"
            "different address, or the USB-C ethernet adapter has dropped out."
        )
    if any(not step.ok and step.label == "resolve name" for step in steps):
        return (
            "The name did not resolve. mDNS (.local) can be slow or unavailable; try the\n"
            "camera's IP address, which Blackmagic Camera Setup displays."
        )
    return "Could not reach the camera at all on either scheme."


async def diagnose(settings: Settings) -> str:
    """Walk the chain and return a report."""
    host, explicit_port = _split_host(settings.camera_host)
    timeout = settings.request_timeout

    lines = [f"Diagnosing {host}", ""]
    name_step = resolve(host)
    lines.append(name_step.render())

    conclusions: list[str] = []
    # Try the configured scheme first; an explicit port names a service, not a
    # scheme, so both are still worth trying against it.
    other = "http" if settings.camera_scheme == "https" else "https"
    for scheme in (settings.camera_scheme, other):
        port = explicit_port or DEFAULT_PORTS[scheme]
        base = f"{scheme}://{host}:{port}"
        lines += ["", f"{scheme} on port {port}"]

        steps = [await tcp(host, port, timeout)]
        if steps[0].ok and scheme == "https":
            steps.append(await tls(host, port, timeout))
        if steps[0].ok and (len(steps) == 1 or steps[-1].ok):
            steps += await http_checks(base, timeout)
        lines += [step.render() for step in steps]
        conclusions.append(_conclude(steps))

    lines += ["", "Conclusion", ""]
    lines.append(_pick(name_step, conclusions))
    return "\n".join(lines)


def _pick(name_step: Step, conclusions: list[str]) -> str:
    """Report the most specific finding, not the first one seen.

    Name resolution comes first because every later failure follows from it: a
    host that cannot be resolved cannot refuse a port either.
    """
    if not name_step.ok:
        return (
            "The name did not resolve, so nothing after that means anything. A .local\n"
            "name needs mDNS; use the IP address Blackmagic Camera Setup displays, or\n"
            "check the camera is on this network."
        )
    for prefix in ("The control API is answering", "The camera's web server"):
        for conclusion in conclusions:
            if conclusion.startswith(prefix):
                return conclusion
    return conclusions[0]
