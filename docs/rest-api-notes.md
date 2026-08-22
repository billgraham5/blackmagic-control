# Camera Control REST API — working notes

Derived from *REST API for Blackmagic Cameras* (August 2025 revision) cross-referenced
against an OpenAPI dump taken from a real Micro Studio Camera 4K G2.

## Connecting

| Item | Value |
| --- | --- |
| Base URL | `http://<camera>.local/control/api/v1/` |
| Secure base URL | `https://<camera>.local/control/api/v1/` — requires *Generate Certificate* in Blackmagic Camera Setup; the cert is self-signed |
| OpenAPI docs | `http://<camera>.local/control/documentation.html` |
| Websocket | `ws://<camera>.local/control/api/v1/event/websocket` (`wss://` when secure) |
| Web media manager | `http://<camera>.local/` |
| Auth | None declared in the OpenAPI spec — access control is network-level |
| CORS | Permissive; browser pages on other origins can call the API directly |

**The web media manager must be enabled** in Blackmagic Camera Setup under *network
access*. The REST API is served by that same service.

Changing the camera name in Blackmagic Camera Setup changes the mDNS hostname.

Note the mixed-content rule: an HTTPS-hosted page cannot call `http://` on the camera.
Either serve your page over plain HTTP, or generate the camera certificate and trust it
once in the browser.

## Response conventions

- `GET` returns JSON; scalar properties come back wrapped, e.g. `{"iso": 400}`
- `PUT` returns `204 No Content` on success, `400` on a rejected value
- Endpoints ending in `/supported*` or `/description` enumerate the legal values and
  ranges for the sibling endpoint — query these rather than hardcoding

## Confirmed on Micro Studio Camera 4K G2

Taken from that body's own `/control/documentation.html`. Firmware adds endpoints over
time, so treat this as a floor, not a ceiling.

### Video / exposure
```
GET|PUT  /video/iso
GET|PUT  /video/gain
GET|PUT  /video/shutter
GET|PUT  /video/whiteBalance
     PUT /video/whiteBalance/doAuto
GET|PUT  /video/whiteBalanceTint
GET|PUT  /video/ndFilter
GET|PUT  /video/ndFilter/displayMode
GET|PUT  /video/autoExposure
```

### Lens — active MFT lenses only
```
GET|PUT  /lens/iris
GET|PUT  /lens/zoom
GET|PUT  /lens/focus
     PUT /lens/focus/doAutoFocus
```
Passive or manual glass will not respond. Iris/focus/zoom values are normalised 0.0–1.0
plus lens-native representations; read `/lens/iris` first to see the shape your lens reports.

### Colour correction
```
GET|PUT  /colorCorrection/lift
GET|PUT  /colorCorrection/gamma
GET|PUT  /colorCorrection/gain
GET|PUT  /colorCorrection/offset
GET|PUT  /colorCorrection/contrast
GET|PUT  /colorCorrection/color
GET|PUT  /colorCorrection/lumaContribution
```

### Transport
```
GET|PUT  /transports/0
GET|PUT  /transports/0/record
GET|PUT  /transports/0/play
GET|PUT  /transports/0/stop
GET|PUT  /transports/0/playback
GET      /transports/0/timecode
GET      /transports/0/timecode/source
```
Recording needs external media on the USB-C port — which is the same port the Ethernet
adapter occupies. See the hardware note in the README.

### System
```
GET      /system
GET|PUT  /system/videoFormat
GET|PUT  /system/codecFormat
GET|PUT  /system/format
GET      /system/supportedVideoFormats
GET      /system/supportedCodecFormats
GET      /system/supportedFormats
```

### Presets
```
GET      /presets
GET|PUT  /presets/active
GET|PUT  /presets/{presetName}
```
Presets are the cheapest way to get a multi-setting "look" onto one Stream Deck key.

### Audio
```
GET|PUT  /audio/channel/{channelIndex}/input
GET      /audio/channel/{channelIndex}/input/description
GET      /audio/channel/{channelIndex}/supportedInputs
GET|PUT  /audio/channel/{channelIndex}/level
GET|PUT  /audio/channel/{channelIndex}/phantomPower
GET|PUT  /audio/channel/{channelIndex}/padding
GET|PUT  /audio/channel/{channelIndex}/lowCutFilter
GET      /audio/channel/{channelIndex}/available
```
The G2's only audio input is a 3.5 mm stereo jack, so phantom power and padding are
unlikely to do anything useful here.

### Media
```
GET|PUT  /media/active
GET      /media/workingset
GET      /media/devices/{deviceName}
GET|PUT  /media/devices/{deviceName}/doformat
GET      /media/devices/doformatSupportedFilesystems
```

### Events
```
GET      /event/list
```

## Not present on this body

Documented in the PDF but belonging to other camera families — expect `404`:

`/livestreams/*` · `/cloud/*` · `/slates/*` · `/immersive/*` · `/timelines/0/clear` ·
most of `/monitoring/*` · most of `/camera/*` (`colorBars`, `tallyStatus`, `power`,
`programFeedDisplay`, `timingReferenceLock`)

Newer firmware may add some of these. Probe rather than assume.

## Websocket

Connect to `/control/api/v1/event/websocket`, then send a subscribe message naming the
properties you care about. Subscribable properties mirror the REST paths — `/video/iso`,
`/lens/iris`, `/transports/0/record`, `/presets/active`, and so on. `GET /event/list`
returns the list your firmware accepts.

Message actions: `subscribe`, `unsubscribe`, `listSubscriptions`, `listProperties`,
`websocketOpened`. Server pushes arrive as `{"type":"event","data":{...}}` with the
changed property and its new value.

This is what makes accurate Stream Deck button state possible — poll-free, and it catches
changes made on the camera body or from another client.

## Practical notes

- Query `/video/supportedShutters`-style endpoints where your firmware has them; the legal
  value sets are camera- and format-dependent.
- Shutter can be addressed as angle or speed — check `/video/shutter/measurement` if present.
- ISO is constrained to the dual-native ladder; arbitrary values are rejected with `400`.
- `PUT` bodies are JSON objects matching the `GET` shape, e.g. `{"iso": 1250}`.
