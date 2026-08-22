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
