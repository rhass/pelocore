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
    "pilates": SportMapping(Sport.TRAINING, SubSport.PILATES, "Pilates"),
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

#: Built-in discipline remaps for platform compatibility. COROS has no
#: stretching activity type and buckets unknown TRAINING files into Strength,
#: so stretching maps to Yoga - the closest supported category.
BUILTIN_REMAPS: dict[str, str] = {
    "stretching": "yoga",
}


def parse_remaps(raw: str) -> dict[str, str]:
    """Parse ``"stretching=yoga,meditation=yoga"`` into a remap dict."""
    remaps: dict[str, str] = {}
    for pair in (raw or "").split(","):
        pair = pair.strip()
        if not pair:
            continue
        if "=" not in pair:
            continue
        source, target = pair.split("=", 1)
        source, target = source.strip().lower(), target.strip().lower()
        if source and target:
            remaps[source] = target
    return remaps


def remap_discipline(fitness_discipline: str, extra: dict[str, str] | None = None) -> str:
    """Apply built-in then user-configured remaps to a discipline name."""
    remaps = {**BUILTIN_REMAPS, **(extra or {})}
    return remaps.get(fitness_discipline.strip().lower(), fitness_discipline)


def mapping_for(fitness_discipline: str, *, is_outdoor: bool = False) -> SportMapping:
    """Resolve the FIT sport/subsport for a (possibly remapped) discipline."""
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
