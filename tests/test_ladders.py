"""The stepping logic, which is where off-by-one errors would hide."""

import pytest

from bmc import ladders


def test_step_moves_one_rung():
    assert ladders.step(400, ladders.ISO_LADDER, 1) == 800
    assert ladders.step(400, ladders.ISO_LADDER, -1) == 200
    assert ladders.step(400, ladders.ISO_LADDER, 2) == 1600


def test_step_clamps_rather_than_wrapping():
    """A held-down Stream Deck key must stop at the end, not roll around."""
    assert ladders.step(25600, ladders.ISO_LADDER, 1) == 25600
    assert ladders.step(100, ladders.ISO_LADDER, -1) == 100
    assert ladders.step(100, ladders.ISO_LADDER, -99) == 100


def test_step_snaps_an_off_ladder_value_first():
    """Someone turned a dial on the camera body; 1250 is not one of our rungs."""
    assert ladders.nearest(1250, ladders.ISO_LADDER) == 1600
    assert ladders.step(1250, ladders.ISO_LADDER, 1) == 3200


def test_nearest_picks_the_lower_rung_on_a_tie():
    assert ladders.nearest(300, (200, 400)) == 200


def test_nearest_rejects_an_empty_ladder():
    with pytest.raises(ValueError):
        ladders.nearest(400, ())


@pytest.mark.parametrize(
    ("gain_db", "iso"),
    [(-12, 100), (0, 400), (18, 3200), (36, 25600)],
)
def test_gain_and_iso_are_the_same_control(gain_db, iso):
    assert ladders.gain_db_to_iso(gain_db) == iso
    assert round(ladders.iso_to_gain_db(iso)) == gain_db


def test_normalised_nudge_clamps_to_the_unit_range():
    assert ladders.step_normalised(0.5, 5) == 0.55
    assert ladders.step_normalised(0.98, 5) == 1.0
    assert ladders.step_normalised(0.02, -5) == 0.0


def test_shutter_angle_is_rendered_from_hundredths_of_a_degree():
    assert ladders.describe_shutter_angle(18000) == "180deg"
    assert ladders.describe_shutter_angle(17280) == "172.8deg"


# ---------------------------------------------------------------- aperture

@pytest.mark.parametrize(
    ("fnumber", "apex"),
    [(2.0, 2.0), (2.8, 2.97), (4.0, 4.0), (5.6, 4.97), (8.0, 6.0), (16.0, 8.0)],
)
def test_fnumber_and_apex_convert_both_ways(fnumber, apex):
    """Blackmagic reports aperture as an APEX value, where f = sqrt(2**stop)."""
    assert round(ladders.fnumber_to_aperture_stop(fnumber), 2) == apex
    assert round(ladders.aperture_stop_to_fnumber(apex), 1) == round(fnumber, 1)


def test_apex_and_fnumber_only_coincide_at_f4():
    """Which is why treating the raw stop as an f-number looks right at first."""
    assert ladders.aperture_stop_to_fnumber(4.0) == 4.0
    assert ladders.aperture_stop_to_fnumber(6.0) == 8.0  # not f/6
    assert ladders.aperture_stop_to_fnumber(2.0) == 2.0


def test_fnumber_must_be_positive():
    with pytest.raises(ValueError):
        ladders.fnumber_to_aperture_stop(0)


def test_fnumbers_render_like_a_lens_barrel():
    assert ladders.format_fnumber(2.8284) == "f/2.8"
    assert ladders.format_fnumber(4.0) == "f/4"
    assert ladders.format_fnumber(11.03) == "f/11"


def test_aperture_units_are_told_apart_by_magnitude():
    """Real lenses top out near APEX 8, or near f/16-f/22 stated directly."""
    assert ladders.looks_like_apex(8.0) is True
    assert ladders.looks_like_apex(9.0) is True
    assert ladders.looks_like_apex(16.0) is False
    assert ladders.looks_like_apex(22.0) is False
