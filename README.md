# blackmagic-control

Control a Blackmagic camera over the Camera Control REST API — from a web page and from
an Elgato Stream Deck. Developed against a **Micro Studio Camera 4K G2**, and applicable
to any Blackmagic body that exposes the same API.

A small Python service holds the connection to the camera and serves two interfaces:

```
Stream Deck ──HTTP──┐
                    ├──> bmc service ──REST + websocket──> camera
Phone / browser ────┘
```

Both are built on one action layer, so a button and a slider that do the same thing
behave identically. The service keeps a live websocket subscription to the camera, which
is what makes toggles, relative stepping and lit button state possible: the REST API
itself offers only absolute setters.

## Requirements

- Python 3.10 or newer
- A camera on the network with **web media manager enabled**, under *network access* in
  Blackmagic Camera Setup. The REST API is served by that service; with it disabled,
  nothing responds.

On a Micro Studio Camera 4K G2 there is no Ethernet jack, no PoE and no Wi-Fi. Network
access comes from a USB-C to Ethernet adapter on the single USB-C port — the same port
used for external recording and for focus/zoom demands. Using the camera for control and
external recording at once requires a powered hub that provides both.

## Install and run

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m bmc
```

Open **http://localhost:8081/** for the web page. Stream Deck buttons point at
`http://localhost:8081/deck/...` — see [`docs/streamdeck.md`](docs/streamdeck.md).

The service starts whether or not the camera is reachable and retries until it answers,
so it can be launched at boot and the camera powered on afterwards.

## Configuration

`--camera` accepts a hostname, `host:port`, or the full URL shown in Blackmagic Camera
Setup:

```sh
.venv/bin/python -m bmc --camera https://camera-name.local
.venv/bin/python -m bmc --camera 192.168.1.42 --http
```

| Option | Purpose |
| --- | --- |
| `--camera` | Camera hostname, `host:port`, or URL |
| `--https` / `--http` | Force a scheme; otherwise the URL's scheme or the default is used |
| `--host`, `--port` | Where the service itself listens (default `0.0.0.0:8081`) |
| `--poll-interval` | Seconds between reads of properties the camera will not push |
| `--diagnose` | Check connectivity to the camera step by step and exit |
| `--verbose` | Debug logging |

The same settings can be supplied as `BMC_CAMERA`, `BMC_SCHEME`, `BMC_HOST`, `BMC_PORT`,
`BMC_POLL_INTERVAL` and `BMC_VERIFY_TLS`.

If the chosen scheme does not answer, the service tries the other before backing off,
since HTTPS depends on a certificate having been generated in Blackmagic Camera Setup.
That certificate is self-signed and issued to the camera's mDNS name, so TLS verification
is disabled by default; set `BMC_VERIFY_TLS=1` if the certificate has been installed
locally.

Requests to the camera never use `HTTP_PROXY` / `HTTPS_PROXY`. A `.local` name on the
local network cannot be resolved by a proxy.

## Capabilities

The service discovers what the camera supports at startup rather than assuming a fixed
list. It reads the OpenAPI documents the camera serves at `/control/documentation.html`,
combines them with the properties named by `/event/list`, expands templated paths against
the displays and audio channels the camera reports, and probes the result. A built-in
list serves only as a floor for firmware that publishes no documentation.

The web page has three views:

- **Control** — a curated layout of the settings used while shooting
- **Status** — every value the camera exposes, grouped and filterable, marked `live`
  where the camera pushes updates rather than the service polling
- **Configure** — every setting the camera reports as writable, rendered from its own
  schema: enums as menus, numbers bounded by their documented range, booleans as toggles

The Control view shows only what the connected camera implements:

| Area | Controls |
| --- | --- |
| Exposure | ISO with ladder stepping, shutter (speed or angle), white balance, tint, auto exposure |
| Lens | Iris as an f-number bounded by the lens's range, focus, zoom, one-shot autofocus — active lenses only |
| Colour | Saturation, plus a passthrough for the remaining DaVinci-style primaries |

Continuous values — white balance, tint, focus, zoom, saturation and iris — accept a
typed value as well as the slider, in the unit they are read in: kelvin, percent, or an
f-number. The camera snaps to its own legal steps, and the field shows the value it
actually took.
| Presets | Recall and save whole camera states |
| Record | Toggle with live state and optional clip naming |
| Monitoring | Zebra, false colour, focus assist, frame guides, clean feed, LUT, colour bars and tally read-back, selectable per output |
| Media | Remaining record time on the active disk |

Every `/deck/*` endpoint returns a short line of plain text suitable for a button title,
so a generic HTTP plugin or Bitfocus Companion's HTTP module can drive it without a
dedicated Stream Deck plugin.

## Troubleshooting

When the camera will not connect, check the chain step by step:

```sh
.venv/bin/python -m bmc --diagnose
```

This resolves the name, opens the port, completes the TLS handshake and requests the web
media manager, the API documentation and the control API in turn, reporting where it
stops. The faults it separates need different fixes: a `.local` name that will not
resolve is an mDNS problem, a refused port means the camera or its ethernet adapter is
off the network, and a web server that answers while `/control/api/v1/` returns 404 is a
fault on the camera itself — both are served by the same process, so no local
configuration will change it.

Once connected, the service reports what it found at startup and why:

```sh
curl http://localhost:8081/api/diagnostics
```

Each probed endpoint is listed with its status. `404` and `501` mean the camera does not
implement it. Any other status, or `NO RESPONSE`, means the probe itself failed and the
control was dropped for the wrong reason; restarting usually recovers it.

Probing is sequential, with retries on dropped connections and 5xx responses. The camera
runs a small embedded HTTP server that drops most of a large concurrent burst, and a
dropped probe is indistinguishable from an unsupported endpoint.

Page assets are served `no-cache` and their URLs carry a build tag derived from their
contents, so a browser will not run JavaScript cached before an upgrade.

`scripts/probe-camera.sh` performs the same discovery standalone, writing the camera's
own OpenAPI documents and a per-endpoint report to `camera-probe/`.

## Development

A mock camera is included, modelled on real hardware: it returns `404` and `501` for
endpoints the body lacks, `204` for endpoints with nothing to report, snaps ISO to the
native ladder, serves its own OpenAPI documentation, and pushes only a subset of
properties over its websocket so the polling fallback is exercised.

```sh
.venv/bin/python -m uvicorn tools.mock_camera:app --port 9000 &
.venv/bin/python -m bmc --camera http://localhost:9000
```

```sh
.venv/bin/pip install -e ".[dev]" && .venv/bin/python -m pytest
```

110 tests run the service against the mock over real HTTP and websockets, covering
capability discovery, ladder stepping and clamping, read-back after write, error
surfacing, the polling fallback and live websocket updates. Three generate a self-signed
certificate and repeat the connection over HTTPS and `wss://`.

| Path | Contents |
| --- | --- |
| `bmc/camera.py` | REST client, capability probe, websocket subscriber, state cache |
| `bmc/discovery.py` | Parses the camera's OpenAPI documents for paths and schemas |
| `bmc/diagnose.py` | Layered connectivity check behind `--diagnose` |
| `bmc/actions.py` | Semantic verbs (toggle, step, recall) built on absolute setters |
| `bmc/ladders.py` | Discrete value ladders, stepping and aperture conversion |
| `bmc/app.py` | FastAPI service: `/deck/*` plain text, `/api/*` JSON and websocket |
| `bmc/web/` | Web page — vanilla JavaScript, no build step |
| `tools/mock_camera.py` | Mock camera for development and tests |
| `scripts/probe-camera.sh` | Standalone capability probe |
| `docs/rest-api-notes.md` | API reference and hardware caveats |
| `docs/streamdeck.md` | Stream Deck and Companion setup, endpoint list |

## References

- [REST API for Blackmagic Cameras](https://documents.blackmagicdesign.com/DeveloperManuals/RESTAPIforBlackmagicCameras.pdf) (PDF)
- [Micro Studio Camera 4K G2 technical specifications](https://www.blackmagicdesign.com/products/blackmagicmicrostudiocamera/techspecs/W-CIN-31)
- The connected camera's own specification, at `/control/documentation.html`
