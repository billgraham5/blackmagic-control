# Driving this from a Stream Deck

Every `/deck/*` endpoint is a plain `GET` that returns a short line of text. That
makes it usable from any generic HTTP button without writing a Stream Deck plugin:

- **Stream Deck** — an HTTP-request plugin from the Elgato Marketplace (search for
  "API Request" or "Web Requests"). Set the method to `GET` and paste the URL.
- **Bitfocus Companion** — the built-in *Generic HTTP* module. There is still no
  official Blackmagic camera REST module, so this is the working route.

Assuming the service runs on the machine your Stream Deck is attached to, the base
URL is `http://localhost:8080`. From another machine, use the service host's IP.

## Why the response text matters

The response body is deliberately short — `ISO 800`, `REC`, `5600K`, `2h21m` — so a
plugin that can display the response can put it straight on the key. Errors come back
as plain text too (`camera rejected /video/iso with 400: iso out of range`) rather
than JSON, for the same reason.

## Endpoints

### Status

| URL | Returns |
| --- | --- |
| `/deck/status` | `ISO 800  1/50  5600K  f/4  REC` |
| `/deck/media` | `2h21m` remaining on the active disk |

### Recording

| URL | Notes |
| --- | --- |
| `/deck/record/toggle` | Returns `REC` or `IDLE` |
| `/deck/record/start` | Add `?clip=take-07` to name the clip |
| `/deck/record/stop` | |

### ISO

| URL | Notes |
| --- | --- |
| `/deck/iso/up` | One rung up the native ladder |
| `/deck/iso/down` | One rung down |
| `/deck/iso/800` | Any value; the camera snaps to its nearest legal ISO |

Stepping snaps an off-ladder value first, so it behaves sensibly even after someone
has changed ISO on the camera body.

### Shutter

| URL | Notes |
| --- | --- |
| `/deck/shutter/up` · `/deck/shutter/down` | Steps in whichever unit the camera reports |
| `/deck/shutter/50` | Speed as a denominator — `50` is 1/50 s |
| `/deck/shutter/angle/18000` | Angle in hundredths of a degree — `18000` is 180° |

### White balance

| URL | Notes |
| --- | --- |
| `/deck/wb/5600` | Kelvin, clamped to 2500–10000 |
| `/deck/wb/preset/daylight` | `tungsten` `fluorescent` `mixed` `daylight` `cloudy` `shade` |
| `/deck/wb/warmer` · `/deck/wb/cooler` | ±500 K, or `?by=250` |
| `/deck/wb/auto` | One-shot auto white balance |
| `/deck/tint/-10` | −50 to +50 |

### Lens

Active MFT lenses only.

| URL | Notes |
| --- | --- |
| `/deck/iris/fstop/2.8` | Set aperture by f-number, clamped to the lens's range |
| `/deck/iris/open` · `/deck/iris/close` | ±5%, or `?by=2` |
| `/deck/focus/auto` | One-shot autofocus |

### Monitoring overlays

Available on current firmware. Overlays are per output, and a Micro Studio 4K G2 has
three: `MainSDI`, `HDMI` and `FrontUSBC`. Without `?display=`, the endpoint acts on the
first output the camera lists — `MainSDI` — which is very likely not the one you are
looking at. Name the output explicitly:

```
/deck/monitor/zebra/toggle?display=HDMI
/deck/monitor/cleanFeed/on?display=FrontUSBC
```

`GET /api/state` lists the outputs your camera reports under `displays`.

| URL | Notes |
| --- | --- |
| `/deck/monitor/zebra/toggle` | Also `on` / `off` instead of `toggle` |
| `/deck/monitor/falseColor/toggle` | |
| `/deck/monitor/focusAssist/toggle` | |
| `/deck/monitor/frameGuide/toggle` | |
| `/deck/monitor/frameGrids/toggle` | |
| `/deck/monitor/safeArea/toggle` | |
| `/deck/monitor/cleanFeed/toggle` | |
| `/deck/monitor/displayLUT/toggle` | |
| `/deck/colorbars/toggle` | |
| `/deck/tally` | Reads tally state |

Toggling preserves the overlay's other settings — zebra keeps its level, focus assist its
mode and colour, frame guide its ratio.

### Exposure mode, presets, colour

| URL | Notes |
| --- | --- |
| `/deck/ae/toggle` | Add `?type=Iris` to drive iris instead of shutter |
| `/deck/ae/off` | |
| `/deck/preset/Studio A` | Recall a whole camera state — the best value per key |
| `/deck/preset/save/Studio A` | Save current state under that name |
| `/deck/saturation/0` | 0.0–2.0; `0` gives a mono look |

## A starter layout

| Key | URL |
| --- | --- |
| Record | `/deck/record/toggle` |
| Status readout | `/deck/status` |
| Look: studio | `/deck/preset/Studio%20A` |
| Look: interview | `/deck/preset/Interview` |
| Auto WB | `/deck/wb/auto` |
| Autofocus | `/deck/focus/auto` |
| ISO − / ISO + | `/deck/iso/down` · `/deck/iso/up` |
| Tungsten / Daylight | `/deck/wb/3200` · `/deck/wb/5600` |
| Shutter 1/50 | `/deck/shutter/50` |
| AE toggle | `/deck/ae/toggle` |
| Disk remaining | `/deck/media` |
| False colour | `/deck/monitor/falseColor/toggle` |
| Zebra | `/deck/monitor/zebra/toggle` |
| Clean feed | `/deck/monitor/cleanFeed/toggle` |

Spaces in preset names need URL encoding (`Studio%20A`); most plugins will do this
for you if you paste the name with a space.

## Polling for button state

If your plugin supports a periodic status request, point it at `/deck/status` or
`/deck/media` on a few-second interval. The service already holds a live websocket to
the camera, so these reads are answered from cache and cost the camera nothing.

Note the firmware caveat from [`rest-api-notes.md`](rest-api-notes.md): on older
firmware the camera only pushes transport state, so record status is live while
exposure values are refreshed by the service's poll loop (default every second,
`--poll-interval` to change).
