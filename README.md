# blackmagic-control

Control a **Blackmagic Micro Studio Camera 4K G2** over the Camera Control REST API —
from an Elgato Stream Deck and from a local web page.

A small Python service owns the camera connection and exposes two surfaces:

```
Stream Deck ──HTTP──┐
                    ├──> bmc service ──REST + websocket──> camera
Phone / browser ────┘
```

Both go through the same action layer, so a Stream Deck key and a slider that do the
same thing really do the same thing. The service holds a live websocket to the camera,
which is what makes toggles, relative stepping and lit button state possible — the REST
API only offers absolute setters.

## Running it

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m bmc
```

The default camera is `https://Micro-Studio-Camera-4K-G2.local`. To point at a
different one, `--camera` takes a hostname, `host:port`, or the full URL that
Blackmagic Camera Setup displays, pasted verbatim:

```sh
.venv/bin/python -m bmc --camera https://Micro-Studio-Camera-4K-G2.local
.venv/bin/python -m bmc --camera 192.168.1.42 --http
```

Then open **http://localhost:8080/** for the web page, and point Stream Deck buttons at
`http://localhost:8080/deck/...` — see [`docs/streamdeck.md`](docs/streamdeck.md).

A scheme in the URL wins over the default, and `--https` / `--http` wins over both.
If the chosen scheme does not answer, the service tries the other one before backing
off — HTTPS depends on a certificate having been generated in Blackmagic Camera Setup,
and that is a setting someone can turn off later.

Other options: `--port`, `--poll-interval`, `--verbose`. The same settings can come
from `BMC_CAMERA`, `BMC_SCHEME`, `BMC_PORT` and friends.

The camera's certificate is self-signed and issued to its mDNS name, so TLS
verification is off by default — there is no CA to check it against. Set
`BMC_VERIFY_TLS=1` if you have installed the camera's certificate yourself.

The service starts whether or not the camera is reachable and keeps retrying, so it is
safe to launch at boot and power the camera on afterwards.

**Before anything works:** enable the web media manager under *network access* in
Blackmagic Camera Setup. The REST API is served by that same service.

## When a control is missing

The service only shows what the camera answered for at startup. To see exactly what it
found and why:

```sh
curl http://localhost:8080/api/diagnostics
```

Every probed endpoint appears with its status. `404` and `501` mean the camera genuinely
does not have it. Anything else — or `NO RESPONSE` — means the probe failed and you lost
a control for the wrong reason; restart and it should recover.

Note that the camera is a small embedded HTTP server. Probing is deliberately sequential
with retries on dropped connections and 5xx, because firing requests at it in parallel
makes it drop most of them, and a dropped probe is indistinguishable from an unsupported
endpoint. Requests never go through `HTTP_PROXY`/`HTTPS_PROXY` either — a `.local` mDNS
name on your own LAN cannot be resolved by a proxy.

## Trying it without a camera

A mock Micro Studio 4K G2 is included, modelled on the real thing down to the quirks —
it 404s the endpoints this body lacks, snaps ISO to the native ladder, and only pushes
transport state over its websocket so the service has to fall back to polling:

```sh
.venv/bin/python -m uvicorn tools.mock_camera:app --port 9000 &
.venv/bin/python -m bmc --camera http://localhost:9000
```

## What it does

The web page shows only what your camera actually implements, discovered at startup:

- **Exposure** — ISO with ladder stepping, shutter (speed or angle), white balance with
  presets and auto, tint, auto exposure
- **Lens** — iris, focus, zoom, one-shot autofocus (active MFT lenses only)
- **Colour** — saturation, with a passthrough for the rest of the DaVinci-style primaries
- **Presets** — recall and save whole camera states
- **Record** — toggle with live state, optional clip naming
- **Media** — remaining record time on the active disk

## Tests

```sh
.venv/bin/pip install -e ".[dev]" && .venv/bin/python -m pytest
```

52 tests run the real service against the mock camera over real HTTP and websockets,
covering capability discovery, ladder stepping and clamping, read-back after write,
error surfacing, the polling fallback, and live websocket updates. Three of them
generate a self-signed certificate and repeat the connection over HTTPS and `wss://`,
which is how the camera is actually configured.

## Layout

| Path | What it is |
| --- | --- |
| `bmc/camera.py` | REST client, capability probe, websocket subscriber, state cache |
| `bmc/actions.py` | Semantic verbs (toggle, step, recall) built on absolute setters |
| `bmc/ladders.py` | Discrete value ladders and stepping — pure, heavily tested |
| `bmc/app.py` | FastAPI service: `/deck/*` plain text, `/api/*` JSON + websocket |
| `bmc/web/` | The web page — vanilla JS, no build step |
| `tools/mock_camera.py` | Fake camera for development and tests |
| `scripts/probe-camera.sh` | Discover exactly what your camera supports |
| `docs/rest-api-notes.md` | Capability reference for this camera body |
| `docs/streamdeck.md` | Stream Deck / Companion setup and endpoint list |

---

# Background

The assessment that led to the design above.

## The hardware constraint (read this first)

The Micro Studio Camera 4K G2 has:

- 1 × USB-C 3.1 Gen 1 port — used for external Blackmagic RAW recording, focus/zoom
  demand, software updates, **and** network connectivity
- **No** built-in Ethernet jack, no PoE, no Wi-Fi

The REST API is reachable only once the camera is on a network, and on this body that
means hanging a USB-C→Ethernet adapter off the single expansion port. If you also want
to record to an external SSD you need a powered USB-C hub providing both, and that
combination is worth bench-testing before you build anything on top of it.

Control-only (no external recording) is the straightforward case: adapter in, done.

## What the API gives you

Base URL: `http://<camera-name>.local/control/api/v1/` (HTTPS available after generating
a certificate in Blackmagic Camera Setup).

Prerequisite: **enable the web media manager** under *network access* in Blackmagic
Camera Setup. The REST API rides on that service; with it off, nothing responds.

The published spec defines ~187 operations. Confirmed present on a Micro Studio 4K G2:

- **Exposure** — `/video/iso`, `/video/gain`, `/video/shutter`, `/video/whiteBalance`,
  `/video/whiteBalanceTint`, `/video/whiteBalance/doAuto`, `/video/autoExposure`
- **Lens** (active MFT only) — `/lens/iris`, `/lens/zoom`, `/lens/focus`,
  `/lens/focus/doAutoFocus`
- **Colour** — `/colorCorrection/{lift,gamma,gain,offset,contrast,color,lumaContribution}`
- **Transport** — `/transports/0/{record,play,stop,playback,timecode}`
- **System** — `/system`, `/system/videoFormat`, `/system/codecFormat`, supported-format lists
- **Presets** — list, recall, save, upload and delete whole camera states
- **Audio** — per-channel input, level, phantom power, padding, low-cut filter
- **Media** — `/media/workingset`, `/media/active`, format operations
- **Events** — `/event/list` plus a websocket at
  `ws://<camera>/control/api/v1/event/websocket` for push updates

Full detail and the caveats are in [`docs/rest-api-notes.md`](docs/rest-api-notes.md).

Endpoints in the manual that this body does **not** expose (they belong to URSA Cine /
Studio Camera bodies): `/livestreams/*`, `/cloud/*`, `/slates/*`, `/immersive/*`,
and much of `/monitoring/*` and `/camera/*`.

Two caveats worth knowing before you build against this. The spec is generated from
shared firmware code, so it advertises hardware this body does not have — ND filter and
XLR audio endpoints both appear, and the G2 has neither. And endpoint coverage grows with
firmware. Run `scripts/probe-camera.sh` against your own camera for ground truth.

## Discovering your camera's exact API

```sh
./scripts/probe-camera.sh
```

This pulls `/control/documentation.html` (the camera's own OpenAPI documentation) and
probes the endpoints that matter, so you know exactly what your firmware supports rather
than what the PDF claims.

## Prior art

| Project | What it is | Verdict |
| --- | --- | --- |
| [DylanSpeiser/BM-Camera-Control-WebUI](https://github.com/DylanSpeiser/BM-Camera-Control-WebUI) | Vanilla JS web UI, ATEM Software Control styling, websocket sync, multi-camera | Best out-of-the-box web option. Explicitly lists Micro Studio 4K G2. AGPL-3.0. Author calls it a tech demo. |
| [DylanSpeiser/BM-API-Tutorial](https://github.com/DylanSpeiser/BM-API-Tutorial) | Step-by-step tutorial building the above; `BMDevice.js` is reusable standalone | Best learning resource. |
| [mgduk/bmd-cam-control](https://github.com/mgduk/bmd-cam-control) | Vite web app, built specifically against a Micro Studio G2 | Closest hardware match, but very early (2 commits). |
| [vaihkonen/BlackmagicStreamDeck](https://github.com/vaihkonen/BlackmagicStreamDeck) | Stream Deck plugin: record toggle, ISO, shutter, WB, autofocus | Only Stream Deck plugin found. ~10 commits, no license, unproven. |
| [GarthDB/blackmagic-camera-control](https://github.com/GarthDB/blackmagic-camera-control) | Node/TS SDK generated from the OpenAPI spec | Useful if building on Node. |
| [Bitfocus Companion](https://github.com/bitfocus/companion-module-requests/issues/1383) | Stream Deck control surface | **No official BMD camera REST module.** Request open since Jan 2024, unimplemented. Companion's generic HTTP module works as a stopgap. |

### The official sample you were looking for

It is not on GitHub. Blackmagic hosts it behind a registration form on
[Developer → Camera → SDK and Software](https://www.blackmagicdesign.com/developer/products/camera/sdk-and-software):

- **Blackmagic Camera and HyperDeck REST Control** (`BlackmagicRESTControlDemo-1.0.zip`) —
  a single-page REST control panel demonstrating status monitoring and transport control
- **Blackmagic Cameras Code Samples 1.2** (Mac) — configuration utility, USB/Bluetooth/web
  camera control, and a desktop camera control app. Predates the REST API (2023) and
  mostly covers the older SDI/Bluetooth control protocols.

Both require accepting the download form, so they can't be fetched programmatically.

## Why a local service rather than direct calls

The REST API offers absolute setters only. A control surface wants verbs — "one stop
up", "toggle record", "is it recording?" — and every one of those needs current state.
The service holds a live websocket subscription and does the read-modify-write, so the
Stream Deck can stay stateless and the web page and the deck cannot drift apart.

It also means no Stream Deck plugin had to be written: a generic HTTP button works on
day one, and a native plugin is a later polish step rather than a prerequisite.

The camera does send permissive CORS headers, so a browser page could call it directly.
The proxy is about state and the Stream Deck, not about CORS.

## References

- [REST API for Blackmagic Cameras (PDF, Aug 2025)](https://documents.blackmagicdesign.com/DeveloperManuals/RESTAPIforBlackmagicCameras.pdf)
- [Micro Studio Camera 4K G2 tech specs](https://www.blackmagicdesign.com/products/blackmagicmicrostudiocamera/techspecs/W-CIN-31)
- Your camera's own spec: `http://<camera>/control/documentation.html`
