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
    assert mapping_for("yoga").sport == Sport.GENERIC
    assert mapping_for("yoga").sub_sport == SubSport.YOGA
    assert mapping_for("stretching").sport == Sport.GENERIC
    assert mapping_for("stretching").sub_sport == SubSport.YOGA
    assert mapping_for("meditation").sport == Sport.GENERIC
    assert mapping_for("meditation").sub_sport is None


def test_bootcamp_variants() -> None:
    names = (
        "bootcamp", "caesar", "circuit",
        "bike_bootcamp", "tread_bootcamp", "row_bootcamp",
    )
    for name in names:
        m = mapping_for(name)
        assert m.sport == Sport.TRAINING, name
        assert m.sub_sport == SubSport.STRENGTH_TRAINING, name


def test_unknown_defaults_to_training() -> None:
    m = mapping_for("pilate-in-the-mist")
    assert m.sport == Sport.TRAINING
    assert m.sub_sport == SubSport.GENERIC
    assert m.label == "Training"


def test_pilates_explicit_mapping() -> None:
    m = mapping_for("pilates")
    assert m.sport == Sport.FITNESS_EQUIPMENT
    assert m.sub_sport == SubSport.PILATES


def test_no_builtin_remaps() -> None:
    """stretching/yoga/meditation carry their GENERIC mapping directly; the
    builtin remap table is empty and exists only for user overrides."""
    from pelocore.sports import BUILTIN_REMAPS, remap_discipline

    assert BUILTIN_REMAPS == {}
    assert remap_discipline("stretching") == "stretching"
    assert remap_discipline("yoga") == "yoga"
    assert remap_discipline("meditation", {"meditation": "yoga"}) == "yoga"
    assert remap_discipline("cycling", {"meditation": "yoga"}) == "cycling"


def test_coros_sport_codes_match_importer_table() -> None:
    """Codes mapped empirically by scripts/experiment_stretch_sport.py: the
    importer consults (sport, sub_sport) pair overrides first, then defaults
    each family to its indoor variant. Yoga (904) is unreachable from imports
    (the TRAINING+YOGA pair is absent server-side), so yoga/stretching/
    meditation ship as GENERIC (Gym Cardio 400); pilates rides the
    FITNESS_EQUIPMENT+PILATES pair to 905; bootcamps land as Strength (402)
    because MULTISPORT/HIIT imports fail."""
    from pelocore.sports import coros_sport_code

    assert coros_sport_code("cycling") == 201
    assert coros_sport_code("running") == 101
    assert coros_sport_code("rowing") == 701
    assert coros_sport_code("strength") == 402
    assert coros_sport_code("yoga") == 400
    assert coros_sport_code("stretching") == 400
    assert coros_sport_code("meditation") == 400
    assert coros_sport_code("pilates") == 905
    assert coros_sport_code("cardio") == 402
    assert coros_sport_code("elliptical") == 903
    assert coros_sport_code("bootcamp") == 402
    assert coros_sport_code("circuit") == 402
    assert coros_sport_code("bike_bootcamp") == 402
    assert coros_sport_code("walking") is None
    assert coros_sport_code("stretching", remaps={"stretching": "strength"}) == 402


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
