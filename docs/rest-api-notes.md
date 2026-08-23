# What you can control on a Micro Studio Camera 4K G2

Derived from the OpenAPI specification served by an actual Micro Studio Camera 4K G2 at
`/control/documentation.html`, cross-checked against *REST API for Blackmagic Cameras*
(August 2025) and the camera's published hardware specs.

> **Two caveats that matter throughout.**
>
> 1. **The spec is generated from shared firmware code, so it advertises hardware this
>    body does not have.** ND filter endpoints and XLR audio inputs both appear in the
>    G2's own documentation; the camera has neither. Presence in the spec is not proof of
>    a working feature.
> 2. **Endpoint coverage grows with firmware.** The dump below came from a 2024-era
>    firmware. Newer releases add endpoints. Run `scripts/probe-camera.sh` for ground truth
>    on your unit.

## Not in the REST API at all

**Dynamic range** (Film / Extended Video / Video), and the rest of what Blackmagic calls
video mode, has no REST endpoint. It is absent from the manual and from the 110
subscribable properties a firmware 9.6.2 Micro Studio 4K G2 reports. It is reachable over
the SDI/Bluetooth Camera Control Protocol, which is a different protocol documented in
the *Blackmagic Camera Control Manual*, not this one.

The same goes for anything else you cannot find below: the way to settle it for your body
and firmware is `GET /control/documentation.html`, which serves the camera's own OpenAPI
documents, and `GET /control/api/v1/event/list`.

## Connecting

| Item | Value |
| --- | --- |
| Base URL | `https://Micro-Studio-Camera-4K-G2.local/control/api/v1/` |
| Plain HTTP | `http://Micro-Studio-Camera-4K-G2.local/control/api/v1/` — available unless secure web media manager is enforced |
| OpenAPI docs | `https://Micro-Studio-Camera-4K-G2.local/control/documentation.html` |
| Websocket | `wss://Micro-Studio-Camera-4K-G2.local/control/api/v1/event/websocket` (`ws://` on plain HTTP) |
| Web media manager | `https://Micro-Studio-Camera-4K-G2.local/` |
| Auth | None declared — access control is network-level |
| CORS | Permissive; browser pages on other origins can call the API directly |

**The web media manager must be enabled** under *network access* in Blackmagic Camera
Setup — the REST API is served by that same service. Renaming the camera changes its
mDNS hostname.

HTTPS means the camera has a certificate generated in Blackmagic Camera Setup. It is
self-signed and issued to the mDNS name, so anything talking to it must skip
verification — a browser needs the warning overridden once, and clients need hostname
checking disabled. `wss://` needs the same treatment as `https://`.

Mixed content rule: an HTTPS page cannot call `http://` on the camera, and vice versa
for websockets. Keep the page and the camera on the same scheme.

Conventions: `GET` returns a JSON object with the property wrapped by name
(`{"iso": 400}`). `PUT` takes the same shape and returns `200`/`204`; `400` on a rejected
value; `501` where the device does not implement it.

---

## Exposure — the core of what you want

### ISO — `GET|PUT /video/iso`
```json
{"iso": 400}
```
Spec declares a 32-bit range, but the hardware ladder is **100–25,600**, dual native
**400** and **3200**. Off-ladder values are rejected. This is the single most useful
Stream Deck control on the camera.

### Gain — `GET|PUT /video/gain`
```json
{"gain": 0}
```
Integer dB. Hardware range is **−12 dB (ISO 100) to +36 dB (ISO 25,600)**. Same physical
control as ISO, expressed differently — pick one and stay consistent.

### Shutter — `GET|PUT /video/shutter`
```json
{"shutterSpeed": 50}
{"shutterAngle": 18000}
```
Returns whichever the camera's *shutter measurement* setting is configured for, plus
`continuousShutterAutoExposure` telling you if auto exposure owns it.

- `shutterSpeed` — denominator of a fraction of a second, max 50000. `50` = 1/50.
  Floor is the sensor frame rate.
- `shutterAngle` — hundredths of a degree, 100–36000. `18000` = 180°.

On `PUT`, send **one**. If both, `shutterSpeed` wins.

### White balance — `GET|PUT /video/whiteBalance`
```json
{"whiteBalance": 5600}
```
Kelvin, **2500–10000**. Ideal for preset Stream Deck keys (3200 tungsten, 4500 fluoro,
5600 daylight, 7500 shade).

### Tint — `GET|PUT /video/whiteBalanceTint`
```json
{"whiteBalanceTint": 0}
```
**−50 to +50.**

### Auto white balance — `PUT /video/whiteBalance/doAuto`
No body. One-shot AWB off the current frame. Excellent single-button action.

### Auto exposure — `GET|PUT /video/autoExposure`
```json
{"mode": {"mode": "Continuous", "type": "Shutter"}}
```
- `mode`: `Off` · `Continuous` · `OneShot`
- `type`: `""` · `Iris` · `Shutter` · `Iris,Shutter` · `Shutter,Iris`

The comma forms set priority order. `Iris` modes need an active MFT lens.

### ND filter — `GET|PUT /video/ndFilter` ⚠️
Advertised (`stop` 0.0–15.0, plus `/video/ndFilter/displayMode` of
`Stop`/`Number`/`Fraction`). **The Micro Studio 4K G2 has no built-in ND filter.** Ignore
these unless probing proves otherwise.

---

## Lens — active MFT glass only

Passive or fully manual lenses will not respond. All positional values are normalised
`0.0–1.0` unless the lens reports native units.

### Iris — `GET|PUT /lens/iris`
```json
{"normalised": 0.5}
{"apertureStop": 6.0}
{"apertureNumber": 8}
```
`GET` also returns `continuousApertureAutoExposure`. On `PUT` send one; priority is
`apertureStop` > `normalised` > `apertureNumber`.

**`apertureStop` is an APEX value, not an f-number**: `f = sqrt(2**stop)`, so APEX 6.0 is
f/8 and APEX 8.0 is f/16. The two coincide at f/4, which makes treating the raw value as
an f-number look correct until you open or close a stop.

**`apertureNumber` is an integer** — an ordinal index into the lens's own aperture steps,
not an f-number. It cannot express f/2.8. Use `apertureStop` for real values.

`GET /lens/iris/description` gives `controllable` and `apertureStop.min` / `.max`, which
is the lens's actual range — worth reading before offering the user a value to set. The
manual does not state the units of those bounds, but magnitude settles it: a real lens
tops out near APEX 8 (f/16) or states f/16–f/22 directly, and nothing sits in both ranges.

### Zoom — `GET|PUT /lens/zoom`
```json
{"normalised": 0.25}
{"focalLength": 24}
```
`focalLength` in mm, and it wins over `normalised`. Servo zoom lens required.

### Focus — `GET|PUT /lens/focus`
```json
{"focus": 0.42}
```
Normalised 0.0–1.0. This is your focus-pull slider.

### Autofocus — `PUT /lens/focus/doAutoFocus`
No body. One-shot AF.

---

## Colour correction — full DaVinci-style primaries

Live in-camera grading, baked into the SDI/HDMI output and recordings.

| Endpoint | Fields | Range | Default |
| --- | --- | --- | --- |
| `/colorCorrection/lift` | red, green, blue, luma | −2.0 – 2.0 | 0.0 |
| `/colorCorrection/gamma` | red, green, blue, luma | −4.0 – 4.0 | 0.0 |
| `/colorCorrection/gain` | red, green, blue, luma | 0.0 – 16.0 | 0.0 |
| `/colorCorrection/offset` | red, green, blue, luma | −8.0 – 8.0 | 0.0 |
| `/colorCorrection/contrast` | pivot / adjust | 0.0–1.0 / 0.0–2.0 | 0.5 / 1.0 |
| `/colorCorrection/color` | hue / saturation | −1.0–1.0 / 0.0–2.0 | 0.0 / 1.0 |
| `/colorCorrection/lumaContribution` | lumaContribution | 0.0 – 1.0 | 1.0 |

All `GET|PUT`. Example:
```json
{"red": 0.0, "green": 0.0, "blue": 0.0, "luma": 0.0}
```

A one-key "saturation to 0 for a mono look" or a matched-camera grade push is trivial here.

---

## Presets — the highest-value Stream Deck target

```
GET    /presets              -> {"presets": ["Studio A", "Interview"]}
POST   /presets              upload a .cameraPreset file (octet-stream)
GET    /presets/active       -> {"preset": "Studio A"}
PUT    /presets/active       {"preset": "Studio A"}
GET    /presets/{name}       download the preset file
PUT    /presets/{name}       save current camera state under this name
DELETE /presets/{name}       delete
```

One `PUT /presets/active` recalls an entire camera state — exposure, WB, colour, format.
This is by far the best way to get a complete look onto a single Stream Deck key, and it
sidesteps having to sequence a dozen individual calls.

`PUT /presets/{name}` snapshotting current state means you can build "save this look"
buttons too.

---

## Recording and transport

```
GET|PUT /transports/0            {"mode": "InputPreview" | "Output"}
GET|PUT /transports/0/record     {"recording": true, "clipName": "take-01"}
GET|PUT /transports/0/play
GET|PUT /transports/0/stop
GET|PUT /transports/0/playback
GET     /transports/0/timecode
GET     /transports/0/timecode/source
```

- `GET /transports/0/record` returns `{"recording": bool}` — drives a lit record button.
- `PUT` accepts an optional **`clipName`**, so you can name takes from the Stream Deck.
- Transport modes: `InputPreview` (live), `InputRecord`, `Output` (playback).
- `playback` carries `type` (`Play`/`Jog`/`Shuttle`/`Var`), `loop`, `singleClip`,
  `speed`, `position`.
- Timecode is BCD-encoded — decode before display.

**Requires external media on the USB-C port**, which is the same port your Ethernet
adapter occupies. See the hardware note in the README.

---

## Format and codec

```
GET     /system                        current codec + video format
GET|PUT /system/codecFormat            {"codec": "BRaw:8_1", "container": "MOV"}
GET|PUT /system/videoFormat            {"name": "3840x2160p29.97", "frameRate": "29.97",
                                        "width": 3840, "height": 2160, "interlaced": false}
GET|PUT /system/format                 codec + frame rate + off-speed + resolutions
GET     /system/supportedCodecFormats
GET     /system/supportedVideoFormats
GET     /system/supportedFormats
```

Frame rates are strings from a fixed enum: `23.98` `24` `25` `29.97` `30` `47.95` `48`
`50` `59.94` `60` `119.88` `120` (plus `.00` variants).

`/system/format` also exposes **off-speed (variable frame rate) recording** —
`offSpeedEnabled`, `offSpeedFrameRate`, and the min/max the current mode allows.

Always read the `supported*` endpoints rather than hardcoding; the legal set depends on
current sensor mode and resolution.

---

## Audio

```
GET|PUT /audio/channel/{i}/input          {"input": "3.5mm Left - Line"}
GET     /audio/channel/{i}/input/description
GET     /audio/channel/{i}/supportedInputs
GET|PUT /audio/channel/{i}/level          {"gain": -6.0} or {"normalised": 0.7}
GET|PUT /audio/channel/{i}/phantomPower   ⚠️
GET|PUT /audio/channel/{i}/padding
GET|PUT /audio/channel/{i}/lowCutFilter
GET     /audio/channel/{i}/available
```

The input enum includes `XLR1 - Mic`, `XLR2 - Line`, etc. **The G2 has no XLR** — only
built-in mics and a 3.5 mm jack. Realistic values here are `Camera - Left/Right/Mono` and
the `3.5mm *` variants. Phantom power is meaningless on this body.

`/input/description` reports the actual `gainRange` and `capabilities` for the selected
input — query it rather than assuming.

On `PUT /level`, `gain` (dB) wins over `normalised`.

---

## Media management

```
GET     /media/workingset      volumes, free space, remaining record time, clip count
GET|PUT /media/active          {"workingsetIndex": 0}
GET     /media/devices/{name}  state: None|Scanning|Mounted|Uninitialised|Formatting|RaidComponent
GET     /media/devices/{name}/doformat            fetch a format key
PUT     /media/devices/{name}/doformat            {"key": "...", "filesystem": "ExFat", "volume": "..."}
GET     /media/devices/doformatSupportedFilesystems
```

`remainingRecordTime` (seconds) and `remainingSpace` are the useful bits — a "disk nearly
full" warning on a Stream Deck key is easy. Formatting is deliberately two-step: `GET` a
key, then `PUT` it back.

---

## Playback timeline

```
GET    /timelines/0        {"clips": [{"clipUniqueId": 1, "frameCount": 90000}]}
DELETE /timelines/0        clear
POST   /timelines/0/add    {"clips": 1} or {"clips": [1, 2, 3]}
```

Build a playback queue from clip IDs. Niche for a studio camera, but present.

---

## Live state via websocket

Connect to `wss://Micro-Studio-Camera-4K-G2.local/control/api/v1/event/websocket`.

Subscribe:
```json
{"type": "request", "id": 1,
 "data": {"action": "subscribe", "properties": ["/transports/0/record", "/video/iso"]}}
```

Actions: `subscribe` · `unsubscribe` · `listSubscriptions` · `listProperties`.

`GET /event/list` returns the subscribable set, as either `{"events": [...]}` or a bare
array depending on firmware — handle both.

Pushes arrive as:
```json
{"type": "event",
 "data": {"action": "propertyValueChanged",
          "property": "/video/iso",
          "value": {"iso": 800}}}
```

**Version caveat, and it matters for button feedback.** The firmware dump examined here
only declares websocket subscriptions for `/media/*`, `/system/*`, `/transports/*` and
`/timelines/0`. The current published manual lists a far wider set including `/video/iso`,
`/video/shutter`, `/video/whiteBalance`, `/lens/iris`, `/lens/focus`, `/colorCorrection/*`
and `/presets/active`.

So on older firmware you get push updates for record state but must **poll** for exposure
values. `GET /event/list` on your camera returns the authoritative list — check it before
designing button feedback, and update firmware if the wider set matters to you.

Subscriptions also catch changes made on the camera body or from another client, which is
what keeps a Stream Deck honest.

---

## Monitoring and camera body — present on current firmware

The 2024-era spec dump this document was first built from had none of these, and an
earlier revision wrongly recorded them as absent from this body. Confirmed working on a
Micro Studio 4K G2 on current firmware:

```
GET|PUT  /camera/colorBars
GET      /camera/tallyStatus
GET|PUT  /monitoring/focusAssist
GET|PUT  /monitoring/frameGuideRatio
GET|PUT  /monitoring/frameGrids
GET|PUT  /monitoring/safeAreaPercent
GET|PUT  /monitoring/{display}/zebra          also falseColor, focusAssist,
GET|PUT  /monitoring/{display}/cleanFeed      frameGuide, frameGrids, safeArea,
GET|PUT  /monitoring/{display}/displayLUT     and displayLUT
```

Per-display overlays are addressed by display name, and the API does not document which
names a body uses. `GET /monitoring/display` may enumerate them; otherwise probe the
usual candidates (`hdmi`, `sdi`, `lcd`, `viewfinder`, `main`, `front`, `preview`).

Overlay bodies carry more than the on/off flag — zebra has a `level`, focus assist a
`mode` and `color`, frame guide a `ratio` — so read before writing and merge, or you will
silently reset them.

Also confirmed on current firmware, and absent from the older dump:
`/video/supportedISOs` · `/video/supportedGains` · `/video/supportedShutters` ·
`/transports/0/play` · `/transports/0/stop` · `/system/format`

## Not on this body

Expect `404` or `501`:

`/livestreams/*` · `/cloud/*` · `/slates/*` · `/immersive/*` · `/clips` ·
`/video/ndFilter` (no ND filter hardware) ·
`/system/codecFormat` and `/system/videoFormat` — `501` on this camera; use
`/system/format` instead

---

## Suggested Stream Deck mapping

Ordered by value-per-key:

| Key | Call |
| --- | --- |
| Recall look | `PUT /presets/active` |
| Record toggle | `PUT /transports/0/record` + websocket feedback |
| Auto white balance | `PUT /video/whiteBalance/doAuto` |
| Autofocus | `PUT /lens/focus/doAutoFocus` |
| WB presets (3200/4500/5600/7500) | `PUT /video/whiteBalance` |
| ISO step up/down | `GET` then `PUT /video/iso` along the native ladder |
| Shutter presets (1/50, 1/100, 180°) | `PUT /video/shutter` |
| Iris nudge | `GET` then `PUT /lens/iris` normalised ± step |
| Auto exposure on/off | `PUT /video/autoExposure` |
| Saturation kill | `PUT /colorCorrection/color` |
| Disk remaining | `GET /media/workingset` on a dial/display key |

The stepping actions (ISO, iris, shutter) need read-modify-write against current state,
which is exactly why the local control service in the README earns its place.
