"""Discrete value ladders and the stepping logic built on them.

Camera settings move in defined steps, not continuously, so "one stop up" means
"the next rung", not "multiply by two and hope". Keeping this pure makes the
awkward parts -- clamping at the ends, snapping an off-ladder value onto the
nearest rung -- testable without a camera.
"""

from __future__ import annotations

from bisect import bisect_left
from typing import Sequence

#: Full-stop ISO ladder for the Micro Studio Camera 4K G2.
#:
#: The camera reports gain in dB and ISO is the same control in other units:
#: ISO = 400 * 10**(gain/20), with dual native bases at 400 (0 dB) and 3200
#: (+18 dB). The hardware range is -12 dB (100) to +36 dB (25600).
#: Overridden at runtime by ``/video/supportedISOs`` where the firmware has it.
ISO_LADDER: tuple[int, ...] = (100, 200, 400, 800, 1600, 3200, 6400, 12800, 25600)

#: Shutter speed denominators: 50 means 1/50 s.
SHUTTER_SPEEDS: tuple[int, ...] = (
    24, 25, 30, 40, 50, 60, 75, 90, 100, 120, 144, 150,
    180, 200, 250, 300, 360, 400, 500, 750, 1000, 2000,
)

#: Shutter angles in hundredths of a degree, as the API expects them.
SHUTTER_ANGLES: tuple[int, ...] = (
    1125, 2250, 4500, 9000, 12000, 15000, 17280, 18000, 27000, 36000,
)

#: Named white balance presets in Kelvin, matching the camera's own menu.
WB_PRESETS: dict[str, int] = {
    "tungsten": 3200,
    "fluorescent": 4000,
    "mixed": 4500,
    "daylight": 5600,
    "cloudy": 6500,
    "shade": 7500,
}

WB_MIN, WB_MAX = 2500, 10000
TINT_MIN, TINT_MAX = -50, 50
GAIN_MIN_DB, GAIN_MAX_DB = -12, 36


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def nearest(value: float, ladder: Sequence[int]) -> int:
    """Snap ``value`` to the closest rung of ``ladder``.

    The camera may sit on a value we never set -- someone turned a dial on the
    body -- so we always snap before stepping.
    """
    if not ladder:
        raise ValueError("ladder is empty")
    rungs = sorted(ladder)
    idx = bisect_left(rungs, value)
    if idx == 0:
        return rungs[0]
    if idx == len(rungs):
        return rungs[-1]
    below, above = rungs[idx - 1], rungs[idx]
    return below if (value - below) <= (above - value) else above


def step(value: float, ladder: Sequence[int], delta: int) -> int:
    """Move ``delta`` rungs from wherever ``value`` currently sits.

    Clamps at both ends rather than wrapping: a Stream Deck key held down
    should stop at ISO 25600, not roll back around to 100.
    """
    if not ladder:
        raise ValueError("ladder is empty")
    rungs = sorted(ladder)
    current = nearest(value, rungs)
    idx = rungs.index(current)
    return rungs[int(clamp(idx + delta, 0, len(rungs) - 1))]


def iso_to_gain_db(iso: int) -> float:
    """Convert an ISO value to the equivalent gain in dB (400 ISO == 0 dB)."""
    from math import log10

    return 20.0 * log10(iso / 400.0)


def gain_db_to_iso(gain_db: float) -> int:
    """Convert gain in dB to the nearest ISO rung."""
    return nearest(400.0 * (10.0 ** (gain_db / 20.0)), ISO_LADDER)


def step_normalised(value: float, delta_percent: float) -> float:
    """Nudge a 0.0-1.0 normalised control (iris, focus, zoom) by a percentage."""
    return round(clamp(value + delta_percent / 100.0, 0.0, 1.0), 4)


#: The familiar full-stop series, for snapping a typed f-number to something sane.
FSTOP_SERIES: tuple[float, ...] = (
    1.0, 1.2, 1.4, 1.7, 2.0, 2.4, 2.8, 3.5, 4.0, 4.8,
    5.6, 6.7, 8.0, 9.5, 11.0, 13.0, 16.0, 19.0, 22.0,
)


def fnumber_to_aperture_stop(fnumber: float) -> float:
    """f/4 -> 4.0, f/8 -> 6.0.

    Blackmagic expresses aperture as an APEX value, where the f-number is
    ``sqrt(2**stop)``. The two coincide at f/4, which makes a naive
    implementation look correct right up until you open or close a stop.
    """
    from math import log2

    if fnumber <= 0:
        raise ValueError("f-number must be positive")
    return 2.0 * log2(fnumber)


def aperture_stop_to_fnumber(stop: float) -> float:
    """The inverse: APEX 6.0 -> f/8."""
    return 2.0 ** (stop / 2.0)


def format_fnumber(fnumber: float) -> str:
    """Render an f-number the way a lens barrel does: f/2.8, f/4, f/5.6."""
    if fnumber >= 10:
        return f"f/{fnumber:.0f}"
    text = f"{fnumber:.1f}".rstrip("0").rstrip(".")
    return f"f/{text}"


def looks_like_apex(maximum: float) -> bool:
    """Decide whether a reported aperture range is APEX values or f-numbers.

    The manual documents the field as an 'aperture stop value' without saying
    which. In APEX terms real lenses top out around 8-9 (f/16-f/22); as plain
    f-numbers the same lenses report 16-22. Nothing sits in both ranges, so the
    magnitude settles it.
    """
    return maximum <= 11.0


def describe_shutter_angle(hundredths: int) -> str:
    """Render an API shutter angle as a human string: 18000 -> '180deg'."""
    degrees = hundredths / 100.0
    text = f"{degrees:.2f}".rstrip("0").rstrip(".")
    return f"{text}deg"
