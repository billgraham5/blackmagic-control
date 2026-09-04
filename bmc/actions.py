"""High-level camera actions.

The REST API is a set of absolute setters. A control surface wants verbs --
"one stop up", "toggle record" -- which means reading current state, working out
the next value and writing it back. That read-modify-write lives here so the web
UI and the Stream Deck endpoints behave identically.

Every action returns a short string suitable for a Stream Deck button title.
"""

from __future__ import annotations

import asyncio
from typing import Any, Sequence

from .camera import Camera, CameraError
from . import ladders


# ------------------------------------------------------------------ helpers

def _reported_numbers(camera: Camera, path: str) -> list[int]:
    """Pull a list of numbers out of a ``/supported*`` endpoint.

    Firmware returns these as either a bare array or an object wrapping one
    under a name we cannot predict, so accept any list of numbers we find.
    """
    payload = camera.value(path)
    candidates: list[Any] = []
    if isinstance(payload, list):
        candidates = payload
    elif isinstance(payload, dict):
        for value in payload.values():
            if isinstance(value, list):
                candidates = value
                break
    numeric = [int(v) for v in candidates if isinstance(v, (int, float))]
    return sorted(set(numeric))


def _iso_ladder(camera: Camera) -> Sequence[int]:
    """Prefer the camera's own list of legal ISOs over our built-in ladder."""
    return _reported_numbers(camera, "/video/supportedISOs") or ladders.ISO_LADDER


def _shutter_ladder(camera: Camera) -> Sequence[int]:
    return _reported_numbers(camera, "/video/supportedShutters") or ladders.SHUTTER_SPEEDS


def _require(camera: Camera, path: str) -> None:
    if not camera.supports(path):
        raise CameraError(f"{path} is not available on this camera")


def _current_iso(camera: Camera) -> int:
    value = camera.value("/video/iso")
    if isinstance(value, dict) and isinstance(value.get("iso"), (int, float)):
        return int(value["iso"])
    gain = camera.value("/video/gain")
    if isinstance(gain, dict) and isinstance(gain.get("gain"), (int, float)):
        return ladders.gain_db_to_iso(float(gain["gain"]))
    raise CameraError("current ISO is unknown")


# ------------------------------------------------------------------- record

async def record_set(camera: Camera, recording: bool, clip_name: str | None = None) -> str:
    _require(camera, "/transports/0/record")
    body: dict[str, Any] = {"recording": recording}
    if recording and clip_name:
        body["clipName"] = clip_name
    await camera.put("/transports/0/record", body)
    return "REC" if recording else "IDLE"


async def record_toggle(camera: Camera, clip_name: str | None = None) -> str:
    return await record_set(camera, not is_recording(camera), clip_name)


def is_recording(camera: Camera) -> bool:
    value = camera.value("/transports/0/record")
    return bool(value.get("recording")) if isinstance(value, dict) else False


# ---------------------------------------------------------------------- ISO

async def iso_set(camera: Camera, iso: int) -> str:
    _require(camera, "/video/iso")
    await camera.put("/video/iso", {"iso": int(iso)})
    return f"ISO {_current_iso(camera)}"


async def iso_step(camera: Camera, delta: int) -> str:
    _require(camera, "/video/iso")
    target = ladders.step(_current_iso(camera), _iso_ladder(camera), delta)
    return await iso_set(camera, target)


async def gain_set(camera: Camera, gain_db: int) -> str:
    _require(camera, "/video/gain")
    clamped = int(ladders.clamp(gain_db, ladders.GAIN_MIN_DB, ladders.GAIN_MAX_DB))
    await camera.put("/video/gain", {"gain": clamped})
    return f"{clamped:+d} dB"


# ------------------------------------------------------------------ shutter

def shutter_summary(camera: Camera) -> str:
    value = camera.value("/video/shutter")
    if not isinstance(value, dict):
        return "--"
    if isinstance(value.get("shutterSpeed"), (int, float)) and value["shutterSpeed"]:
        return f"1/{int(value['shutterSpeed'])}"
    if isinstance(value.get("shutterAngle"), (int, float)) and value["shutterAngle"]:
        return ladders.describe_shutter_angle(int(value["shutterAngle"]))
    return "--"


def _uses_angle(camera: Camera) -> bool:
    """The camera reports whichever unit its shutter measurement is set to."""
    value = camera.value("/video/shutter")
    if not isinstance(value, dict):
        return False
    return bool(value.get("shutterAngle")) and not value.get("shutterSpeed")


async def shutter_set_speed(camera: Camera, denominator: int) -> str:
    _require(camera, "/video/shutter")
    await camera.put("/video/shutter", {"shutterSpeed": int(denominator)})
    return shutter_summary(camera)


async def shutter_set_angle(camera: Camera, hundredths_of_degree: int) -> str:
    _require(camera, "/video/shutter")
    await camera.put("/video/shutter", {"shutterAngle": int(hundredths_of_degree)})
    return shutter_summary(camera)


async def shutter_step(camera: Camera, delta: int) -> str:
    """Step the shutter in whichever unit the camera is currently reporting."""
    _require(camera, "/video/shutter")
    value = camera.value("/video/shutter") or {}
    if _uses_angle(camera):
        current = int(value.get("shutterAngle") or 18000)
        return await shutter_set_angle(
            camera, ladders.step(current, ladders.SHUTTER_ANGLES, delta)
        )
    current = int(value.get("shutterSpeed") or 50)
    return await shutter_set_speed(
        camera, ladders.step(current, _shutter_ladder(camera), delta)
    )


# ------------------------------------------------------------ white balance

async def wb_set(camera: Camera, kelvin: int) -> str:
    _require(camera, "/video/whiteBalance")
    clamped = int(ladders.clamp(kelvin, ladders.WB_MIN, ladders.WB_MAX))
    await camera.put("/video/whiteBalance", {"whiteBalance": clamped})
    return f"{wb_current(camera)}K"


async def wb_preset(camera: Camera, name: str) -> str:
    key = name.strip().lower()
    if key not in ladders.WB_PRESETS:
        raise CameraError(
            f"unknown white balance preset {name!r}; "
            f"try one of {', '.join(sorted(ladders.WB_PRESETS))}"
        )
    return await wb_set(camera, ladders.WB_PRESETS[key])


async def wb_step(camera: Camera, delta_kelvin: int) -> str:
    return await wb_set(camera, wb_current(camera) + delta_kelvin)


async def wb_auto(camera: Camera) -> str:
    _require(camera, "/video/whiteBalance")
    await camera.put("/video/whiteBalance/doAuto")
    return "AWB"


async def tint_set(camera: Camera, tint: int) -> str:
    _require(camera, "/video/whiteBalanceTint")
    clamped = int(ladders.clamp(tint, ladders.TINT_MIN, ladders.TINT_MAX))
    await camera.put("/video/whiteBalanceTint", {"whiteBalanceTint": clamped})
    return f"tint {clamped:+d}"


def wb_current(camera: Camera) -> int:
    value = camera.value("/video/whiteBalance")
    if isinstance(value, dict) and isinstance(value.get("whiteBalance"), (int, float)):
        return int(value["whiteBalance"])
    return ladders.WB_PRESETS["daylight"]


# --------------------------------------------------------------------- lens

def _normalised(camera: Camera, path: str, key: str, fallback: float = 0.5) -> float:
    value = camera.value(path)
    if isinstance(value, dict):
        for candidate in (key, "normalised"):
            if isinstance(value.get(candidate), (int, float)):
                return float(value[candidate])
    return fallback


async def iris_set(camera: Camera, normalised: float) -> str:
    _require(camera, "/lens/iris")
    await camera.put("/lens/iris", {"normalised": ladders.clamp(normalised, 0.0, 1.0)})
    return iris_summary(camera)


async def iris_nudge(camera: Camera, delta_percent: float) -> str:
    current = _normalised(camera, "/lens/iris", "normalised")
    return await iris_set(camera, ladders.step_normalised(current, delta_percent))


def _aperture_is_apex(camera: Camera) -> bool:
    """Whether this lens reports aperture as APEX values or plain f-numbers."""
    limits = _iris_limits(camera)
    return ladders.looks_like_apex(limits[1]) if limits else True


def _iris_limits(camera: Camera) -> tuple[float, float] | None:
    """The lens's aperture range, in whatever units it reports."""
    description = camera.value("/lens/iris/description")
    if not isinstance(description, dict):
        return None
    span = description.get("apertureStop")
    if not isinstance(span, dict):
        return None
    low, high = span.get("min"), span.get("max")
    if isinstance(low, (int, float)) and isinstance(high, (int, float)) and high > low:
        return float(low), float(high)
    return None


def iris_fstop(camera: Camera) -> float | None:
    """Current aperture as an f-number, or None if the lens does not report one."""
    value = camera.value("/lens/iris")
    if not isinstance(value, dict):
        return None
    stop = value.get("apertureStop")
    if not isinstance(stop, (int, float)):
        return None
    return ladders.aperture_stop_to_fnumber(float(stop)) if _aperture_is_apex(camera) else float(stop)


def iris_fstop_range(camera: Camera) -> tuple[float, float] | None:
    """The lens's aperture range as f-numbers, for bounding an input field."""
    limits = _iris_limits(camera)
    if limits is None:
        return None
    low, high = limits
    if ladders.looks_like_apex(high):
        return ladders.aperture_stop_to_fnumber(low), ladders.aperture_stop_to_fnumber(high)
    return low, high


async def iris_set_fstop(camera: Camera, fnumber: float) -> str:
    """Set the aperture from a typed f-number.

    Sent as ``apertureStop`` because that is the field the API takes a real
    value in -- ``apertureNumber`` is an integer ordinal into the lens's own
    steps, not an f-number.
    """
    _require(camera, "/lens/iris")
    if fnumber <= 0:
        raise CameraError("f-number must be greater than zero")

    limits = iris_fstop_range(camera)
    if limits:
        fnumber = ladders.clamp(fnumber, limits[0], limits[1])

    if _aperture_is_apex(camera):
        payload = ladders.fnumber_to_aperture_stop(fnumber)
    else:
        payload = fnumber
    await camera.put("/lens/iris", {"apertureStop": round(payload, 4)})
    return iris_summary(camera)


def iris_summary(camera: Camera) -> str:
    fnumber = iris_fstop(camera)
    if fnumber is not None:
        return ladders.format_fnumber(fnumber)
    return f"iris {_normalised(camera, '/lens/iris', 'normalised') * 100:.0f}%"


async def focus_set(camera: Camera, normalised: float) -> str:
    _require(camera, "/lens/focus")
    await camera.put("/lens/focus", {"focus": ladders.clamp(normalised, 0.0, 1.0)})
    return f"focus {_normalised(camera, '/lens/focus', 'focus') * 100:.0f}%"


async def focus_auto(camera: Camera) -> str:
    _require(camera, "/lens/focus")
    await camera.put("/lens/focus/doAutoFocus")
    return "AF"


async def zoom_set(camera: Camera, normalised: float) -> str:
    _require(camera, "/lens/zoom")
    await camera.put("/lens/zoom", {"normalised": ladders.clamp(normalised, 0.0, 1.0)})
    return f"zoom {_normalised(camera, '/lens/zoom', 'normalised') * 100:.0f}%"


# ----------------------------------------------------------- auto exposure

def autoexposure_mode(camera: Camera) -> str:
    value = camera.value("/video/autoExposure")
    if isinstance(value, dict):
        mode = value.get("mode")
        if isinstance(mode, dict):
            return str(mode.get("mode") or "Off")
        if isinstance(mode, str):
            return mode
    return "Off"


async def autoexposure_set(camera: Camera, mode: str, ae_type: str = "Shutter") -> str:
    _require(camera, "/video/autoExposure")
    await camera.put("/video/autoExposure", {"mode": {"mode": mode, "type": ae_type}})
    return f"AE {autoexposure_mode(camera)}"


async def autoexposure_toggle(camera: Camera, ae_type: str = "Shutter") -> str:
    is_off = autoexposure_mode(camera) == "Off"
    return await autoexposure_set(camera, "Continuous" if is_off else "Off", ae_type)


# ------------------------------------------------------------------ presets

def preset_names(camera: Camera) -> list[str]:
    value = camera.value("/presets")
    if isinstance(value, dict) and isinstance(value.get("presets"), list):
        return [str(name) for name in value["presets"]]
    return []


def preset_active(camera: Camera) -> str | None:
    value = camera.value("/presets/active")
    if isinstance(value, dict) and value.get("preset"):
        return str(value["preset"])
    return None


async def preset_recall(camera: Camera, name: str) -> str:
    _require(camera, "/presets/active")
    await camera.put("/presets/active", {"preset": name})
    return name


async def preset_save(camera: Camera, name: str) -> str:
    _require(camera, "/presets")
    await camera.put(f"/presets/{name}")
    return f"saved {name}"


# ------------------------------------------------------------------- colour

async def saturation_set(camera: Camera, saturation: float) -> str:
    _require(camera, "/colorCorrection/color")
    current = camera.value("/colorCorrection/color")
    hue = float(current.get("hue", 0.0)) if isinstance(current, dict) else 0.0
    value = ladders.clamp(saturation, 0.0, 2.0)
    await camera.put("/colorCorrection/color", {"hue": hue, "saturation": value})
    return f"sat {value:.2f}"


async def color_set(camera: Camera, wheel: str, **channels: float) -> str:
    """Set one colour wheel. ``wheel`` is lift, gamma, gain, offset, colour etc."""
    path = f"/colorCorrection/{wheel}"
    _require(camera, path)
    await camera.put(path, {k: float(v) for k, v in channels.items()})
    return wheel


# --------------------------------------------------- monitoring and overlays

def flag_state(camera: Camera, path: str, key: str = "enabled") -> bool:
    value = camera.value(path)
    return bool(value.get(key)) if isinstance(value, dict) else False


async def flag_set(camera: Camera, path: str, on: bool, key: str = "enabled") -> str:
    """Set one boolean overlay, preserving whatever else it carries.

    Zebra has a level, focus assist has a mode and colour, frame guide has a
    ratio. Sending only the flag would drop them, so merge into current state.
    """
    _require(camera, path)
    current = camera.value(path)
    body = dict(current) if isinstance(current, dict) else {}
    body[key] = on
    await camera.put(path, body)
    label = path.rsplit("/", 1)[-1]

    # A camera can answer 204 and change nothing: an overlay may not apply to
    # the output it was addressed to, or may conflict with another setting.
    # Reporting that as an unlit button looks like a broken control, so say it.
    if flag_state(camera, path, key) != on:
        await asyncio.sleep(0.2)  # allow for a camera that reports a beat late
        refreshed = await camera.get(path)
        if refreshed is not None:
            camera.remember(path, refreshed)
        if flag_state(camera, path, key) != on:
            raise CameraError(
                f"the camera accepted the request but left {label} "
                f"{'off' if on else 'on'} for {path.rsplit('/', 2)[-2]}. "
                "The overlay may not apply to that output."
            )
    return f"{label} {'on' if flag_state(camera, path, key) else 'off'}"


async def flag_toggle(camera: Camera, path: str, key: str = "enabled") -> str:
    return await flag_set(camera, path, not flag_state(camera, path, key), key)


def monitoring_path(camera: Camera, overlay: str, display: str | None = None) -> str:
    """Resolve an overlay to a path, defaulting to the first display found.

    Overlays are addressed per display and the names vary by body, so the
    caller should not have to know them.
    """
    if display:
        return f"/monitoring/{display}/{overlay}"
    for name in camera.displays:
        candidate = f"/monitoring/{name}/{overlay}"
        if camera.supports(candidate):
            return candidate
    # Focus assist also exists as a global setting on some firmware.
    if camera.supports(f"/monitoring/{overlay}"):
        return f"/monitoring/{overlay}"
    raise CameraError(f"{overlay} is not available on this camera")


def tally_summary(camera: Camera) -> str:
    value = camera.value("/camera/tallyStatus")
    if not isinstance(value, dict):
        return "--"
    for key in ("tally", "status", "state"):
        if key in value:
            return str(value[key])
    if value.get("program"):
        return "PROGRAM"
    if value.get("preview"):
        return "PREVIEW"
    return "off"


# ------------------------------------------------------------------- status

def media_summary(camera: Camera) -> str:
    """Remaining record time on the active disk, for an at-a-glance key."""
    workingset = camera.value("/media/workingset")
    if not isinstance(workingset, dict):
        return "no media"
    disks = workingset.get("workingset")
    if not isinstance(disks, list):
        return "no media"
    for disk in disks:
        if isinstance(disk, dict) and disk.get("activeDisk"):
            seconds = disk.get("remainingRecordTime")
            if isinstance(seconds, (int, float)):
                hours, remainder = divmod(int(seconds), 3600)
                return f"{hours}h{remainder // 60:02d}m"
            return str(disk.get("volume") or "mounted")
    return "no media"


def status_line(camera: Camera) -> str:
    """One-line summary for a Stream Deck display key."""
    parts = [f"ISO {_safe(lambda: _current_iso(camera), '--')}", shutter_summary(camera)]
    if camera.supports("/video/whiteBalance"):
        parts.append(f"{wb_current(camera)}K")
    if camera.supports("/lens/iris"):
        parts.append(iris_summary(camera))
    if is_recording(camera):
        parts.append("REC")
    return "  ".join(parts)


def _safe(fn: Any, default: str) -> Any:
    try:
        return fn()
    except Exception:  # noqa: BLE001 - a status line must never raise
        return default
