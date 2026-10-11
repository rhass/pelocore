#!/usr/bin/env python3
"""One-off experiment: can the COROS importer categorize a stretch session as
Yoga (sportType 904) instead of Strength (402)?

Re-uploads ONE real session - "5 min Post-Ride Stretch with Denis Morton"
(2026-10-10, Peloton afedf3e9eaaf4d308a349f1e9e711776) - with different FIT
encodings. Known importer behavior (verified 2026-10-11): a re-upload folds
into the existing activity with the same start time (same labelId) and
resets its name to the sport default; no second activity is created. So each
trial observes the existing activity's sportType after import, and deletes
ONLY brand-new labelIds if the importer ever creates one instead of folding.
The pre-existing activity itself is never deleted.

Trials:
  T1  native-clone: file_id 294/822 + device_info "COROS APEX 2 Pro",
      sport TRAINING + sub_sport YOGA (the encoding COROS watches write)
  T2  cardio-family probe: primary sport JUMP_ROPE (does the importer emit
      any 9xx Cardio-family code on import?)
  T3  generic + sub_sport YOGA
  T4-T18  primary-sport table mapping (running/cycling/rowing/equipment/
      meditation/dance/mobility/hiit/jump-rope/floor-climb/boxing)
  T19 fitness_equipment + sub_sport YOGA (import ERRORS - unknown
      subsports in the equipment family are rejected, not fallen back)
  T20 fitness_equipment + sub_sport FLEXIBILITY_TRAINING (also errors)
  T21 generic with NO sub_sport -> 400 Gym Cardio
  T22 primary sport MULTISPORT (import ERRORS)

Established conclusion: the importer consults (primary, sub_sport) pair
overrides first (FITNESS_EQUIPMENT+PILATES -> 905 proven by re-importing a
byte-exact watch export), then falls back to the primary sport's indoor
variant. No encoding reaches Yoga (904): re-importing the byte-exact native
yoga watch file lands as 402. COROS multisport exports are ZIPs of per-leg
single-sport FITs; re-importing that byte-exact bundle creates separate
per-leg activities (201 + 402), never a grouped 10001 Multisport.

Usage: op run -- uv run python scripts/experiment_stretch_sport.py
                 [--trials T2,T3] [--workout-id <id>] [--dry-run]
"""

from __future__ import annotations

import argparse
import dataclasses
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fit_tool.profile.profile_type import Sport, SubSport  # noqa: E402

from pelocore.config import Settings  # noqa: E402
from pelocore.coros import ActivityItem, CorosClient  # noqa: E402
from pelocore.fitbuild import build_activity_fit  # noqa: E402
from pelocore.peloton import PylotonClient, display_name  # noqa: E402
from pelocore.sports import SportMapping  # noqa: E402
import pelocore.sports as sports  # noqa: E402

DEFAULT_WORKOUT_ID = "afedf3e9eaaf4d308a349f1e9e711776"
COROS_MANUFACTURER = 294
COROS_PRODUCT = 822
WINDOW_S = 60


def synthetic_mappings() -> dict[str, SportMapping]:
    return {
        "expjumprope": SportMapping(Sport.JUMP_ROPE, None, "Jump Rope"),
        "expgenericyoga": SportMapping(Sport.GENERIC, SubSport.YOGA, "Generic Yoga"),
        "exprun": SportMapping(Sport.RUNNING, SubSport.TREADMILL, "Treadmill"),
        "expbike": SportMapping(Sport.CYCLING, None, "Cycling Outdoor"),
        "expmed": SportMapping(Sport.MEDITATION, None, "Meditation"),
        "expjryoga": SportMapping(Sport.JUMP_ROPE, SubSport.YOGA, "Jump Rope Yoga"),
        "expell": SportMapping(Sport.FITNESS_EQUIPMENT, SubSport.ELLIPTICAL, "Elliptical"),
        "expequip": SportMapping(Sport.FITNESS_EQUIPMENT, None, "Equipment"),
        "expbox": SportMapping(Sport.BOXING, None, "Boxing"),
        "expfloor": SportMapping(Sport.FLOOR_CLIMBING, None, "Floor Climb"),
        "expdance": SportMapping(Sport.DANCE, None, "Dance"),
        "expmobility": SportMapping(Sport.MOBILITY, None, "Mobility"),
        "exprunoutdoor": SportMapping(Sport.RUNNING, None, "Run Outdoor"),
        "exphiit": SportMapping(Sport.HIIT, SubSport.HIIT, "HIIT"),
        "exprow": SportMapping(Sport.ROWING, SubSport.INDOOR_ROWING, "Indoor Row"),
        "expeqyoga": SportMapping(Sport.FITNESS_EQUIPMENT, SubSport.YOGA, "Equipment Yoga"),
        "expeqflex": SportMapping(
            Sport.FITNESS_EQUIPMENT, SubSport.FLEXIBILITY_TRAINING, "Equipment Stretch"
        ),
        "expgeneric": SportMapping(Sport.GENERIC, None, "Gym Cardio"),
        "expmulti": SportMapping(Sport.MULTISPORT, None, "Multisport"),
    }


def build_trial(workout, perf, tag: str) -> tuple[str, bytes, str]:
    """Return (label, fit bytes, filename) for one trial."""
    # id suffix makes file_id.serial_number (and thus the md5) unique per
    # trial, so content dedupe cannot short-circuit a repeat trial
    tagged = dataclasses.replace(workout, id=f"{workout.id}-{tag.lower()}")
    filename = f"exp-stretch-{tag.lower()}.fit"
    if tag == "T1":
        built = build_activity_fit(
            tagged,
            perf,
            manufacturer=COROS_MANUFACTURER,
            product=COROS_PRODUCT,
            device_model="COROS APEX 2 Pro",
        )
        label = "T1 native-clone (294/822 + device_info, TRAINING+YOGA)"
    elif tag == "T2":
        sports.DISCIPLINE_MAP.update(synthetic_mappings())
        built = build_activity_fit(tagged, perf, remaps={"stretching": "expjumprope"})
        label = "T2 jump-rope probe (primary sport JUMP_ROPE)"
    elif tag == "T3":
        sports.DISCIPLINE_MAP.update(synthetic_mappings())
        built = build_activity_fit(tagged, perf, remaps={"stretching": "expgenericyoga"})
        label = "T3 generic + sub_sport YOGA"
    elif tag == "T4":
        sports.DISCIPLINE_MAP.update(synthetic_mappings())
        built = build_activity_fit(tagged, perf, remaps={"stretching": "exprun"})
        label = "T4 running + sub_sport TREADMILL (subsport refinement test)"
    elif tag == "T5":
        sports.DISCIPLINE_MAP.update(synthetic_mappings())
        built = build_activity_fit(tagged, perf, remaps={"stretching": "expbike"})
        label = "T5 cycling with NO sub_sport (indoor-default test)"
    elif tag == "T6":
        sports.DISCIPLINE_MAP.update(synthetic_mappings())
        built = build_activity_fit(tagged, perf, remaps={"stretching": "expmed"})
        label = "T6 primary sport MEDITATION"
    elif tag == "T7":
        sports.DISCIPLINE_MAP.update(synthetic_mappings())
        built = build_activity_fit(tagged, perf, remaps={"stretching": "expjryoga"})
        label = "T7 jump_rope + sub_sport YOGA (family refinement test)"
    elif tag == "T8":
        sports.DISCIPLINE_MAP.update(synthetic_mappings())
        built = build_activity_fit(tagged, perf, remaps={"stretching": "expell"})
        label = "T8 fitness_equipment + sub_sport ELLIPTICAL"
    elif tag == "T9":
        sports.DISCIPLINE_MAP.update(synthetic_mappings())
        built = build_activity_fit(tagged, perf, remaps={"stretching": "expequip"})
        label = "T9 fitness_equipment with NO sub_sport (was 903 refinement?)"
    elif tag == "T10":
        sports.DISCIPLINE_MAP.update(synthetic_mappings())
        named = dataclasses.replace(tagged, title="5 min Yoga Flow")
        built = build_activity_fit(named, perf, remaps={"stretching": "expgenericyoga"})
        label = "T10 generic + sport_profile_name containing 'Yoga' (keyword test)"
    elif tag == "T12":
        sports.DISCIPLINE_MAP.update(synthetic_mappings())
        built = build_activity_fit(tagged, perf, remaps={"stretching": "expbox"})
        label = "T12 primary sport BOXING"
    elif tag == "T13":
        sports.DISCIPLINE_MAP.update(synthetic_mappings())
        built = build_activity_fit(tagged, perf, remaps={"stretching": "expfloor"})
        label = "T13 primary sport FLOOR_CLIMBING"
    elif tag == "T14":
        sports.DISCIPLINE_MAP.update(synthetic_mappings())
        built = build_activity_fit(tagged, perf, remaps={"stretching": "expdance"})
        label = "T14 primary sport DANCE"
    elif tag == "T15":
        sports.DISCIPLINE_MAP.update(synthetic_mappings())
        built = build_activity_fit(tagged, perf, remaps={"stretching": "expmobility"})
        label = "T15 primary sport MOBILITY"
    elif tag == "T16":
        sports.DISCIPLINE_MAP.update(synthetic_mappings())
        built = build_activity_fit(tagged, perf, remaps={"stretching": "exprunoutdoor"})
        label = "T16 running with NO sub_sport (indoor-default test)"
    elif tag == "T17":
        sports.DISCIPLINE_MAP.update(synthetic_mappings())
        built = build_activity_fit(tagged, perf, remaps={"stretching": "exphiit"})
        label = "T17 primary sport HIIT (production bootcamp encoding)"
    elif tag == "T18":
        sports.DISCIPLINE_MAP.update(synthetic_mappings())
        built = build_activity_fit(tagged, perf, remaps={"stretching": "exprow"})
        label = "T18 rowing + sub_sport INDOOR_ROWING (production encoding)"
    elif tag == "T19":
        sports.DISCIPLINE_MAP.update(synthetic_mappings())
        built = build_activity_fit(tagged, perf, remaps={"stretching": "expeqyoga"})
        label = "T19 fitness_equipment + sub_sport YOGA (pair-table probe)"
    elif tag == "T20":
        sports.DISCIPLINE_MAP.update(synthetic_mappings())
        built = build_activity_fit(tagged, perf, remaps={"stretching": "expeqflex"})
        label = "T20 fitness_equipment + sub_sport FLEXIBILITY_TRAINING"
    elif tag == "T21":
        sports.DISCIPLINE_MAP.update(synthetic_mappings())
        built = build_activity_fit(tagged, perf, remaps={"stretching": "expgeneric"})
        label = "T21 generic with NO sub_sport (default for meditation)"
    elif tag == "T22":
        sports.DISCIPLINE_MAP.update(synthetic_mappings())
        built = build_activity_fit(tagged, perf, remaps={"stretching": "expmulti"})
        label = "T22 primary sport MULTISPORT (bootcamp candidate)"
    else:
        raise SystemExit(f"unknown trial {tag}")
    return label, built.data, filename


def window_activities(client: CorosClient, start_time: int) -> dict[str, ActivityItem]:
    """Activities within +/-WINDOW_S of start_time.

    A ``startDay == endDay`` query has been observed returning nothing;
    always span at least two days.
    """
    day = datetime.fromtimestamp(start_time, tz=UTC)
    start_day = (day - timedelta(days=1)).strftime("%Y%m%d")
    end_day = (day + timedelta(days=1)).strftime("%Y%m%d")
    items: dict[str, ActivityItem] = {}
    for page in (1, 2, 3):
        batch = client.list_activities(
            page=page, size=50, start_day=start_day, end_day=end_day
        )
        for item in batch:
            if abs(item.start_time - start_time) <= WINDOW_S:
                items[item.label_id] = item
        if len(batch) < 50:
            break
    return items


def run_trial(client: CorosClient, workout, perf, tag: str) -> str:
    label, fit_bytes, filename = build_trial(workout, perf, tag)
    print(f"--- {tag}: {label}")
    print(f"    {len(fit_bytes)} bytes -> {filename}")

    before = window_activities(client, workout.start_time)
    original_ids = set(before)
    baseline_sport = next(iter(before.values())).sport_type if before else None
    for item in before.values():
        print(
            f"    pre-existing: labelId={item.label_id} sportType={item.sport_type} "
            f"name={item.name!r}"
        )
    if not before:
        print("    WARNING: empty pre-window; cannot observe folds safely - aborting trial")
        return "aborted (empty pre-window)"

    upload = client.upload_fit(fit_bytes, filename)
    print(f"    import_id={upload.import_id}")
    job = client.wait_for_import(upload.import_id, timeout_s=60, interval_s=5)
    print(f"    import status={job.status if job else None} errors={job.error_size if job else '?'}")

    outcome = "no change observed"
    deadline = time.monotonic() + 150
    while time.monotonic() < deadline:
        after = window_activities(client, workout.start_time)
        fresh = [item for lid, item in after.items() if lid not in original_ids]
        if fresh:
            new = fresh[0]
            outcome = (
                f"NEW activity labelId={new.label_id} sportType={new.sport_type} "
                f"name={new.name!r}"
            )
            print(f"    RESULT {tag}: {outcome}")
            print(f"    cleaned up: deleting new activity {new.label_id}")
            client.delete_activity(new.label_id)
            time.sleep(3)
            break
        for item in after.values():
            if item.label_id in original_ids and item.sport_type != baseline_sport:
                outcome = (
                    f"folded into {item.label_id}: sportType {baseline_sport} -> "
                    f"{item.sport_type} name={item.name!r}"
                )
                print(f"    RESULT {tag}: {outcome}")
                break
        if outcome != "no change observed":
            break
        time.sleep(10)
    else:
        after = window_activities(client, workout.start_time)
        states = ", ".join(
            f"{item.label_id}=sport {item.sport_type} {item.name!r}" for item in after.values()
        )
        outcome = f"unchanged (sportType {baseline_sport}) after poll window (window now: {states})"
        print(f"    RESULT {tag}: {outcome}")

    try:
        client.remove_from_import_list(upload.import_id)
        print(f"    cleaned up: purged import entry {upload.import_id}")
    except Exception as exc:
        print(f"    (import-entry purge failed: {exc})")
    return outcome


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trials", default="T1,T2,T3")
    parser.add_argument("--workout-id", default=DEFAULT_WORKOUT_ID)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    settings = Settings()
    coros = CorosClient(
        region=settings.coros_region,
        email=settings.coros_email,
        password=settings.coros_password.get_secret_value(),
        access_token=settings.coros_token_or_none,
        timeout=settings.http_timeout_seconds,
    )
    peloton = PylotonClient(
        username=settings.peloton_username,
        password=settings.peloton_password.get_secret_value(),
        refresh_token=settings.peloton_refresh_token.get_secret_value(),
        timeout=settings.http_timeout_seconds,
    )

    workout = peloton.workout_by_id(args.workout_id)
    if workout is None:
        raise SystemExit(f"workout {args.workout_id} not found")
    perf = peloton.performance(workout.id)
    print(
        f"session: {display_name(workout.title, workout.instructor)!r} "
        f"({workout.fitness_discipline}, {len(perf.samples)} samples), "
        f"start={workout.start_time}"
    )

    results: dict[str, str] = {}
    try:
        for tag in [t.strip().upper() for t in args.trials.split(",")]:
            if args.dry_run:
                label, fit_bytes, filename = build_trial(workout, perf, tag)
                print(f"--- {tag} (dry-run): {label}, {len(fit_bytes)} bytes -> {filename}")
                continue
            results[tag] = run_trial(coros, workout, perf, tag)
            time.sleep(10)
    finally:
        print("\n=== RESULTS ===")
        for key, value in results.items():
            print(f"  {key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
