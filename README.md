# blackmagic-control

Notes and tooling for controlling a **Blackmagic Micro Studio Camera 4K G2** over the
Camera Control REST API — from an Elgato Stream Deck and from a local web page.

This branch currently contains the **feasibility assessment** and a discovery script.
No application code yet.

## TL;DR

| Question | Answer |
| --- | --- |
| Is the Micro Studio 4K G2 supported by the REST API? | **Yes** — it is on Blackmagic's official compatibility list. |
| Is there an official app to drive it? | **No.** Blackmagic ships the spec and a demo page, not a product. |
| Is there an official sample? | **Yes**, but it is a gated download from blackmagicdesign.com, *not* on GitHub — which is why it's hard to find. |
| Stream Deck support out of the box? | **No** native plugin from Elgato or Blackmagic. Community plugin exists but is embryonic. |
| Web page out of the box? | **Yes** — a mature community web UI works today with zero build step. |
| Biggest gotcha | The G2 has **one USB-C port** and **no Ethernet**. Networking needs a USB-C→Ethernet adapter on that same port. |

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
  `/video/whiteBalanceTint`, `/video/whiteBalance/doAuto`, `/video/autoExposure`,
  `/video/ndFilter`
- **Lens** (active MFT only) — `/lens/iris`, `/lens/zoom`, `/lens/focus`,
  `/lens/focus/doAutoFocus`
- **Colour** — `/colorCorrection/{lift,gamma,gain,offset,contrast,color,lumaContribution}`
- **Transport** — `/transports/0/{record,play,stop,playback,timecode}`
- **System** — `/system`, `/system/videoFormat`, `/system/codecFormat`, supported-format lists
- **Presets** — `/presets`, `/presets/active`, `/presets/{name}`
- **Audio** — per-channel input, level, phantom power, padding, low-cut filter
- **Media** — `/media/workingset`, `/media/active`, format operations
- **Events** — `/event/list` plus a websocket at
  `ws://<camera>/control/api/v1/event/websocket` for push updates

Full detail and the caveats are in [`docs/rest-api-notes.md`](docs/rest-api-notes.md).

Endpoints in the manual that this body does **not** expose (they belong to URSA Cine /
Studio Camera bodies): `/livestreams/*`, `/cloud/*`, `/slates/*`, `/immersive/*`,
and much of `/monitoring/*` and `/camera/*`.

Endpoint coverage varies by firmware. Run `scripts/probe-camera.sh` against your own
camera to get the authoritative list for your unit.

## Discovering your camera's exact API

```sh
./scripts/probe-camera.sh micro-studio-g2.local
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

## Recommended architecture

Rather than pointing the Stream Deck straight at the camera, run one small local service
that owns the camera connection:

```
Stream Deck ──HTTP──┐
                    ├──> local control service ──REST+WS──> Micro Studio 4K G2
Phone / browser ────┘
```

Why:

- **State.** Toggle-record, relative ISO/shutter stepping, and button feedback all need to
  know the camera's current state. The service holds a live websocket subscription; the
  Stream Deck just fires stateless HTTP.
- **One integration.** The web page and the Stream Deck consume the same endpoints.
- **No plugin to write initially.** A generic HTTP-request Stream Deck plugin, or Bitfocus
  Companion's generic HTTP module, can drive it on day one. A native plugin becomes a
  later polish step, not a prerequisite.
- **Insulation.** Firmware differences, HTTPS/self-signed certs, and camera reboots get
  handled in one place.

The camera does send permissive CORS headers — a browser page on a different origin can
call it directly — so a direct-to-camera web page is viable too. The proxy is about state
and the Stream Deck, not about CORS.

## References

- [REST API for Blackmagic Cameras (PDF, Aug 2025)](https://documents.blackmagicdesign.com/DeveloperManuals/RESTAPIforBlackmagicCameras.pdf)
- [Micro Studio Camera 4K G2 tech specs](https://www.blackmagicdesign.com/products/blackmagicmicrostudiocamera/techspecs/W-CIN-31)
- Your camera's own spec: `http://<camera>/control/documentation.html`
