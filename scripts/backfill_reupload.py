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
from pelocore.sports import mapping_for  # noqa: E402
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

    uploaded = {
        wid: rec for wid, rec in store.workouts.items() if rec.status == "uploaded"
    }
    uploaded = {wid: rec for wid, rec in uploaded.items() if rec.start_time}
    if not uploaded:
        print("no uploaded workouts with start_time in state; nothing to do")
        return 0

    activities = coros.all_activities()
    print(f"{len(uploaded)} uploaded workouts, {len(activities)} COROS activities")

    plan: list[tuple[str, WorkoutRecord, ActivityItem | None, str]] = []
    for workout_id, rec in uploaded.items():
        match, reason = _match(rec, activities)
        plan.append((workout_id, rec, match, reason))

    deleted = 0
    for workout_id, rec, match, reason in plan:
        if match is None:
            print(f"  NO MATCH  {workout_id} ({rec.title}) — {reason}")
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
        print(f"deleted {deleted} activities; state cleared for them")
        print("now run: pelocore sync   (re-uploads with corrected FIT files)")
    else:
        print("dry run — pass --yes to delete")
    return 0


def _match(rec: WorkoutRecord, activities: list[ActivityItem]) -> tuple[ActivityItem | None, str]:
    assert rec.start_time is not None
    expected = mapping_for((rec.discipline or "").strip().lower()).sport.value
    time_candidates = [
        a for a in activities if abs(a.start_time - rec.start_time) <= TIME_TOLERANCE_S
    ]
    sport_matched = [a for a in time_candidates if a.sport_type == expected]
    if len(sport_matched) == 1:
        return sport_matched[0], "time+sport"
    if len(time_candidates) == 1:
        return time_candidates[0], "time only (sport mismatch)"
    return None, f"{len(time_candidates)} time candidates"


if __name__ == "__main__":
    raise SystemExit(main())
