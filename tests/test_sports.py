"""Tests for pelocore.sports."""

from __future__ import annotations

from fit_tool.profile.profile_type import Sport, SubSport

from pelocore.sports import mapping_for


def test_cycling_indoor() -> None:
    m = mapping_for("cycling")
    assert m.sport == Sport.CYCLING
    assert m.sub_sport == SubSport.INDOOR_CYCLING


def test_cycling_outdoor_drops_subsport() -> None:
    assert mapping_for("cycling", is_outdoor=True).sub_sport is None


def test_running_indoor_is_treadmill() -> None:
    m = mapping_for("running", is_outdoor=False)
    assert m.sport == Sport.RUNNING
    assert m.sub_sport == SubSport.TREADMILL


def test_running_outdoor_has_no_subsport() -> None:
    assert mapping_for("running", is_outdoor=True).sub_sport is None


def test_walking_indoor_outdoor() -> None:
    assert mapping_for("walking").sub_sport == SubSport.INDOOR_WALKING
    assert mapping_for("walking", is_outdoor=True).sub_sport is None


def test_rowing() -> None:
    m = mapping_for("rowing")
    assert m.sport == Sport.ROWING
    assert m.sub_sport == SubSport.INDOOR_ROWING


def test_strength_yoga_stretching_meditation() -> None:
    assert mapping_for("strength").sub_sport == SubSport.STRENGTH_TRAINING
    assert mapping_for("yoga").sub_sport == SubSport.YOGA
    assert mapping_for("stretching").sub_sport == SubSport.FLEXIBILITY_TRAINING
    assert mapping_for("meditation").sport == Sport.MEDITATION


def test_bootcamp_variants() -> None:
    assert mapping_for("caesar").sport == Sport.HIIT
    assert mapping_for("tread_bootcamp").sub_sport == SubSport.TREADMILL
    assert mapping_for("row_bootcamp").sport == Sport.ROWING


def test_unknown_defaults_to_training() -> None:
    m = mapping_for("pilate-in-the-mist")
    assert m.sport == Sport.TRAINING
    assert m.sub_sport == SubSport.GENERIC
    assert m.label == "Training"


def test_pilates_explicit_mapping() -> None:
    m = mapping_for("pilates")
    assert m.sport == Sport.TRAINING
    assert m.sub_sport == SubSport.PILATES


def test_builtin_remap_stretching_to_yoga() -> None:
    from pelocore.sports import remap_discipline

    assert remap_discipline("stretching") == "yoga"
    m = mapping_for(remap_discipline("stretching"))
    assert m.sub_sport == SubSport.YOGA


def test_remap_extra_overrides_builtin() -> None:
    from pelocore.sports import remap_discipline

    assert remap_discipline("stretching", {"stretching": "stretching"}) == "stretching"
    assert remap_discipline("meditation", {"meditation": "yoga"}) == "yoga"
    assert remap_discipline("cycling", {"meditation": "yoga"}) == "cycling"


def test_parse_remaps() -> None:
    from pelocore.sports import parse_remaps

    assert parse_remaps("stretching=yoga, meditation=yoga") == {
        "stretching": "yoga",
        "meditation": "yoga",
    }
    assert parse_remaps("") == {}
    assert parse_remaps("garbage") == {}
