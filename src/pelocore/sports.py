"""Peloton fitness discipline → FIT sport/subsport mapping."""

from __future__ import annotations

from dataclasses import dataclass

from fit_tool.profile.profile_type import Sport, SubSport


@dataclass(frozen=True)
class SportMapping:
    sport: Sport
    sub_sport: SubSport | None
    label: str


#: Base mapping by Peloton ``fitness_discipline`` value. Sub-sport may be
#: refined per-workout for indoor/outdoor distinction.
DISCIPLINE_MAP: dict[str, SportMapping] = {
    "cycling": SportMapping(Sport.CYCLING, SubSport.INDOOR_CYCLING, "Cycling"),
    "bike_bootcamp": SportMapping(Sport.CYCLING, SubSport.INDOOR_CYCLING, "Bike bootcamp"),
    "running": SportMapping(Sport.RUNNING, None, "Running"),
    "walking": SportMapping(Sport.WALKING, None, "Walking"),
    "rowing": SportMapping(Sport.ROWING, SubSport.INDOOR_ROWING, "Rowing"),
    "strength": SportMapping(Sport.TRAINING, SubSport.STRENGTH_TRAINING, "Strength"),
    "yoga": SportMapping(Sport.TRAINING, SubSport.YOGA, "Yoga"),
    "stretching": SportMapping(Sport.TRAINING, SubSport.FLEXIBILITY_TRAINING, "Stretching"),
    "meditation": SportMapping(Sport.MEDITATION, None, "Meditation"),
    "cardio": SportMapping(Sport.TRAINING, SubSport.CARDIO_TRAINING, "Cardio"),
    "circuit": SportMapping(Sport.HIIT, SubSport.HIIT, "Circuit"),
    "caesar": SportMapping(Sport.HIIT, SubSport.HIIT, "Bootcamp"),
    "bootcamp": SportMapping(Sport.HIIT, SubSport.HIIT, "Bootcamp"),
    "tread_bootcamp": SportMapping(Sport.RUNNING, SubSport.TREADMILL, "Tread bootcamp"),
    "row_bootcamp": SportMapping(Sport.ROWING, SubSport.INDOOR_ROWING, "Row bootcamp"),
    "elliptical": SportMapping(Sport.FITNESS_EQUIPMENT, SubSport.ELLIPTICAL, "Elliptical"),
}

DEFAULT_MAPPING = SportMapping(Sport.TRAINING, SubSport.GENERIC, "Training")


def mapping_for(fitness_discipline: str, *, is_outdoor: bool = False) -> SportMapping:
    """Resolve the FIT sport/subsport for a Peloton workout."""
    name = fitness_discipline.strip().lower()
    base = DISCIPLINE_MAP.get(name, SportMapping(Sport.TRAINING, SubSport.GENERIC, "Training"))
    sub = base.sub_sport
    if name == "running":
        sub = None if is_outdoor else SubSport.TREADMILL
    elif name == "walking":
        sub = None if is_outdoor else SubSport.INDOOR_WALKING
    elif name == "cycling" and is_outdoor:
        sub = None  # outdoor cycling: leave sub-sport unset
    return SportMapping(base.sport, sub, base.label)
