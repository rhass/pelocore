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
    "bike_bootcamp": SportMapping(Sport.TRAINING, SubSport.STRENGTH_TRAINING, "Bike bootcamp"),
    "running": SportMapping(Sport.RUNNING, None, "Running"),
    "walking": SportMapping(Sport.WALKING, None, "Walking"),
    "rowing": SportMapping(Sport.ROWING, SubSport.INDOOR_ROWING, "Rowing"),
    "strength": SportMapping(Sport.TRAINING, SubSport.STRENGTH_TRAINING, "Strength"),
    "yoga": SportMapping(Sport.GENERIC, SubSport.YOGA, "Yoga"),
    "pilates": SportMapping(Sport.FITNESS_EQUIPMENT, SubSport.PILATES, "Pilates"),
    "stretching": SportMapping(Sport.GENERIC, SubSport.YOGA, "Stretching"),
    "meditation": SportMapping(Sport.GENERIC, None, "Meditation"),
    "cardio": SportMapping(Sport.TRAINING, SubSport.CARDIO_TRAINING, "Cardio"),
    "circuit": SportMapping(Sport.TRAINING, SubSport.STRENGTH_TRAINING, "Circuit"),
    "caesar": SportMapping(Sport.TRAINING, SubSport.STRENGTH_TRAINING, "Bootcamp"),
    "bootcamp": SportMapping(Sport.TRAINING, SubSport.STRENGTH_TRAINING, "Bootcamp"),
    "tread_bootcamp": SportMapping(Sport.TRAINING, SubSport.STRENGTH_TRAINING, "Tread bootcamp"),
    "row_bootcamp": SportMapping(Sport.TRAINING, SubSport.STRENGTH_TRAINING, "Row bootcamp"),
    "elliptical": SportMapping(Sport.FITNESS_EQUIPMENT, SubSport.ELLIPTICAL, "Elliptical"),
}

DEFAULT_MAPPING = SportMapping(Sport.TRAINING, SubSport.GENERIC, "Training")

#: Mapping notes backed by import experiments
#: (scripts/experiment_stretch_sport.py, 2026-10-11):
#: - yoga/stretching/meditation use GENERIC: the COROS importer has no path
#:   to Yoga (904) from any FIT encoding, and GENERIC lands as Gym Cardio
#:   (400), the user-preferred category over Strength.
#: - pilates rides the FITNESS_EQUIPMENT+PILATES pair to its real 905
#:   Pilates category (proven by re-importing a byte-exact watch export).
#: - every bootcamp variant maps to TRAINING/STRENGTH_TRAINING: neither
#:   Hybrid Fitness (1200, no watch mode exists to learn an encoding) nor
#:   MULTISPORT (import errors, errorSize=1) is reachable, and HIIT is
#:   unmapped (imports dropped). Strength is the user-chosen fallback.

#: User-configurable discipline remaps (``PELOCORE_SPORT_REMAPS``). The
#: built-in table is empty: stretching/yoga/meditation carry their GENERIC
#: mapping directly.
BUILTIN_REMAPS: dict[str, str] = {}


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


#: (FIT primary sport, FIT sub_sport) -> COROS ``sportType`` pair overrides.
#: The importer consults these before the primary-sport table. Mapped
#: empirically (scripts/experiment_stretch_sport.py, 2026-10-11): the
#: FITNESS_EQUIPMENT family honors PILATES (905, proven by re-importing a
#: byte-exact COROS watch export) and ELLIPTICAL (903); unknown subsports in
#: that family (YOGA, FLEXIBILITY_TRAINING) ERROR the import instead of
#: falling back, so never emit them.
COROS_PAIR_CODES: dict[tuple[str, str], int] = {
    ("FITNESS_EQUIPMENT", "PILATES"): 905,
    ("FITNESS_EQUIPMENT", "ELLIPTICAL"): 903,
}

#: FIT primary sport name -> COROS ``activity/query`` ``sportType`` code for
#: IMPORTED files, mapped empirically by upload experiments
#: (scripts/experiment_stretch_sport.py, 2026-10-11). The importer defaults
#: each family to its indoor variant; ``WALKING``, ``MEDITATION``, ``DANCE``,
#: ``MOBILITY``, ``HIIT`` and ``MULTISPORT`` are unmapped (HIIT and
#: MULTISPORT imports error out entirely; walking once landed as
#: uncategorized 65535) - pelocore's discipline map avoids all of them.
#: Yoga (904) exists only on the watch path: a COROS watch writes yoga as
#: ``TRAINING``+``YOGA`` and syncs it as 904, but the same bytes through the
#: importer land as 402 - re-importing the byte-exact watch export proves
#: it. COROS multisport exports are ZIPs of per-leg single-sport FITs, and
#: re-importing that byte-exact bundle creates separate activities (201 +
#: 402), never a grouped 10001.
COROS_IMPORT_CODES: dict[str, int] = {
    "RUNNING": 101,
    "CYCLING": 201,
    "ROWING": 701,
    "FITNESS_EQUIPMENT": 903,
    "TRAINING": 402,
    "GENERIC": 400,
    "JUMP_ROPE": 901,
    "FLOOR_CLIMBING": 902,
    "BOXING": 906,
}


def coros_sport_code(fitness_discipline: str) -> int | None:
    """COROS ``sportType`` code an imported file for this discipline receives,
    or None when the importer does not map it. Remaps are applied first so the
    hint matches the file build_activity_fit actually produces."""
    effective = remap_discipline(fitness_discipline)
    mapping = mapping_for(effective)
    if mapping.sub_sport is not None:
        pair = COROS_PAIR_CODES.get((mapping.sport.name, mapping.sub_sport.name))
        if pair is not None:
            return pair
    return COROS_IMPORT_CODES.get(mapping.sport.name)
