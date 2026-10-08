#!/usr/bin/env python3
"""One-time backfill: delete previously uploaded COROS activities and clear
their state entries so the next sync re-uploads corrected FIT files.

Matching is conservative: a COROS activity matches a Peloton workout only
when start times are within +/-60 seconds AND the mapped sport type agrees;
a unique time-only match is accepted with a warning.

Usage (credentials via environment, e.g. wrapped in `op run --`):
    uv run python scripts/backfill_reupload.py            # dry run
    uv run python scripts/backfill_reupload.py --yes      # delete for real
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pelocore.config import Settings  # noqa: E402
from pelocore.coros import ActivityItem, CorosClient  # noqa: E402
from pelocore.peloton import PylotonClient  # noqa: E402
from pelocore.sports import coros_sport_code  # noqa: E402
from pelocore.state import StateStore, WorkoutRecord  # noqa: E402

TIME_TOLERANCE_S = 60


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--yes", action="store_true", help="actually delete")
    args = parser.parse_args()

    settings = Settings()
    store = StateStore(settings.state_path)
    coros = CorosClient(
        region=settings.coros_region,
        email=settings.coros_email,
        password=settings.coros_password.get_secret_value(),
        access_token=settings.coros_token_or_none,
        timeout=settings.http_timeout_seconds,
    )

    uploaded = store.uploaded()
    missing_start = [rec for rec in uploaded.values() if not rec.start_time]
    if missing_start:
        print(f"fetching start_time for {len(missing_start)} records from Peloton…")
        peloton = PylotonClient(
            username=settings.peloton_username,
            password=settings.peloton_password.get_secret_value(),
            refresh_token=settings.peloton_refresh_token.get_secret_value(),
        )
        for rec in missing_start:
            workout = peloton.workout_by_id(rec.workout_id)
            if workout is None:
                print(f"  WARN: {rec.workout_id} not found on Peloton; skipping")
                continue
            rec.start_time = workout.start_time
        store.save()

    uploaded = {wid: rec for wid, rec in store.uploaded().items() if rec.start_time}
    uploaded = {wid: rec for wid, rec in uploaded.items() if rec.start_time}
    if not uploaded:
        if args.yes:
            # State already cleared by a previous pass; just purge our stale
            # import-list entries so reconcile does not skip re-uploads.
            removed = 0
            for job in coros.import_jobs(size=50):
                if (job.original_filename or "").startswith("peloton-"):
                    coros.remove_from_import_list(job.id)
                    removed += 1
            print(f"cleared {removed} import-list entries; run pelocore sync")
            return 0
        print("no uploaded workouts with start_time in state; nothing to do")
        return 0

    from datetime import datetime, timedelta

    end = datetime.now()
    start = end - timedelta(days=30)
    activities = coros.all_activities(
        start_day=start.strftime("%Y%m%d"), end_day=end.strftime("%Y%m%d")
    )
    print(f"{len(uploaded)} uploaded workouts, {len(activities)} COROS activities")

    plan: list[tuple[str, WorkoutRecord, ActivityItem | None, str]] = []
    for workout_id, rec in uploaded.items():
        match, reason = _match(rec, activities)
        plan.append((workout_id, rec, match, reason))

    deleted = 0
    for workout_id, rec, match, reason in plan:
        if match is None:
            print(f"  NO MATCH  {workout_id} ({rec.title}) - {reason}")
            continue
        print(
            f"  MATCH     {workout_id} ({rec.title}) -> labelId={match.label_id} "
            f"start={match.start_time} [{reason}]"
        )
        if args.yes:
            coros.delete_activity(match.label_id)
            store.workouts_remove(workout_id)
            deleted += 1

    if args.yes:
        store.save()
        # Clear stale import-list entries too - reconcile matches on
        # originalFilename and would otherwise skip the re-upload.
        removed = 0
        for job in coros.import_jobs(size=50):
            if f"peloton-{job.md5}" == "never":
                continue
            if any(
                job.original_filename == f"peloton-{wid}.fit"
                for wid, _rec, _m, _r in plan
            ):
                coros.remove_from_import_list(job.id)
                removed += 1
        print(f"cleared {removed} import-list entries")
        print(f"deleted {deleted} activities; state cleared for them")
        print("now run: pelocore sync   (re-uploads with corrected FIT files)")
    else:
        print("dry run - pass --yes to delete")
    return 0


#: (FIT Sport, FIT SubSport) → COROS activity/query sportType codes
_COROS_SPORT_CODES = {
    ("CYCLING", "INDOOR_CYCLING"): 201,
    ("CYCLING", None): 200,
    ("RUNNING", "TREADMILL"): 101,
    ("RUNNING", None): 100,
    ("ROWING", "INDOOR_ROWING"): 701,
    ("ROWING", None): 700,
    ("TRAINING", "STRENGTH_TRAINING"): 402,
    ("TRAINING", "YOGA"): 904,
    ("TRAINING", "PILATES"): 905,
    ("WALKING", None): 900,
    ("WALKING", "INDOOR_WALKING"): 900,
}


def _expected_coros_sport(rec: WorkoutRecord) -> int | None:
    mapping = mapping_for((rec.discipline or "").strip().lower())
    code = _COROS_SPORT_CODES.get((mapping.sport.name, mapping.sub_sport.name if mapping.sub_sport else None))
    return code


def _match(rec: WorkoutRecord, activities: list[ActivityItem]) -> tuple[ActivityItem | None, str]:
    assert rec.start_time is not None
    expected = _expected_coros_sport(rec)
    time_candidates = [
        a for a in activities if abs(a.start_time - rec.start_time) <= TIME_TOLERANCE_S
    ]
    sport_matched = [a for a in time_candidates if expected is None or a.sport_type == expected]
    if len(sport_matched) == 1:
        return sport_matched[0], "time+sport"
    if len(time_candidates) == 1:
        return time_candidates[0], "time only (sport mismatch)"
    return None, f"{len(time_candidates)} time candidates"


if __name__ == "__main__":
    raise SystemExit(main())
