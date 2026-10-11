#!/usr/bin/env python3
"""One-off: recategorize historical bootcamp sessions in COROS.

The pre-0.2.0 converter mapped ``bike_bootcamp`` to CYCLING (COROS Indoor
Bike) and ``bootcamp``/``caesar``/``circuit`` to HIIT, whose imports the
COROS importer drops entirely. This script re-uploads each bootcamp-family
session's FIT rebuilt with the current converter so the fold re-derives the
category to Strength (402), renames the activity to its Peloton title, and
updates the state record to the current converter version.

- Sessions with an existing COROS activity: the re-upload folds in place
  (same labelId, no duplicates, data reprocessed).
- Sessions with no activity (the dropped HIIT imports): the upload creates
  a fresh activity.
- Import-list entries are intentionally KEPT: they are the version record
  for stateless drift detection.

Idempotent: records already at the current converter version are skipped.

Usage: op run -- uv run python scripts/recategorize_bootcamps.py [--yes]
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pelocore.config import Settings  # noqa: E402
from pelocore.coros import CorosClient  # noqa: E402
from pelocore.fitbuild import CONVERTER_VERSION, build_activity_fit  # noqa: E402
from pelocore.peloton import PylotonClient, display_name  # noqa: E402
from pelocore.state import StateStore  # noqa: E402
from pelocore.sync import fit_filename  # noqa: E402

BOOTCAMPS = {"bootcamp", "caesar", "circuit", "bike_bootcamp", "tread_bootcamp", "row_bootcamp"}
TARGET_CODE = 402  # Strength: the category every bootcamp variant lands in


def window_activities(client: CorosClient, start_time: int) -> dict[str, object]:
    day = datetime.fromtimestamp(start_time, tz=UTC)
    start_day = (day - timedelta(days=1)).strftime("%Y%m%d")
    end_day = (day + timedelta(days=1)).strftime("%Y%m%d")
    return {
        item.label_id: item
        for page in (1, 2)
        for item in client.list_activities(page=page, size=50, start_day=start_day, end_day=end_day)
        if abs(item.start_time - start_time) <= 60
    }


def process(workout_id: str, rec, client: CorosClient, peloton: PylotonClient, store: StateStore) -> str:
    workout = peloton.workout_by_id(workout_id)
    if workout is None:
        return "peloton workout not found"
    perf = peloton.performance(workout.id)
    plan = None
    if not perf.samples and not perf.locations and workout.ride_id:
        plan = peloton.class_plan(workout.ride_id) or None
    built = build_activity_fit(workout, perf, plan=plan)
    filename = fit_filename(workout.id, version=CONVERTER_VERSION)

    before = window_activities(client, workout.start_time)
    old = list(before.values())
    if len(old) > 1:
        return f"ambiguous window ({len(old)} activities); skipping"

    upload = client.upload_fit(built.data, filename)
    job = client.wait_for_import(upload.import_id, timeout_s=60, interval_s=5)
    if job is not None and job.error_size > 0:
        return f"import error (errorSize={job.error_size})"

    deadline = time.monotonic() + 150
    label_id: str | None = None
    while time.monotonic() < deadline:
        current = window_activities(client, workout.start_time)
        fresh = [item for lid, item in current.items() if lid not in before]
        if fresh:
            label_id = fresh[0].label_id
            break
        for item in current.values():
            if item.sport_type == TARGET_CODE:
                label_id = item.label_id
                break
        if label_id is not None:
            break
        time.sleep(10)
    if label_id is None:
        return "no activity appeared"

    name = display_name(workout.title, workout.instructor)
    try:
        client.rename_activity(label_id, name)
    except Exception as exc:
        print(f"    (rename failed: {exc})")

    store.record_uploaded(
        workout.id,
        md5=upload.md5,
        import_id=upload.import_id,
        fit_filename=filename,
        title=workout.title,
        instructor=workout.instructor,
        discipline=workout.fitness_discipline,
        start_time=workout.start_time,
        converter_version=CONVERTER_VERSION,
    )
    return f"ok labelId={label_id} ({'created' if label_id not in before else 'folded'})"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--yes", action="store_true", help="actually re-upload")
    args = parser.parse_args()

    settings = Settings()
    client = CorosClient(
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
    store = StateStore(settings.state_path)

    targets = {
        wid: rec
        for wid, rec in store.uploaded().items()
        if (rec.discipline or "").strip().lower() in BOOTCAMPS
    }
    print(f"{len(targets)} bootcamp-family records in state")
    for wid, rec in sorted(targets.items(), key=lambda kv: kv[1].start_time or 0):
        if (rec.converter_version or 0) >= CONVERTER_VERSION:
            print(f"  SKIP {wid[:8]} ({rec.title}) already at v{rec.converter_version}")
            continue
        print(f"  {wid[:8]} ({rec.title}) v{rec.converter_version} -> v{CONVERTER_VERSION}")
        if not args.yes:
            continue
        try:
            outcome = process(wid, rec, client, peloton, store)
        except Exception as exc:
            outcome = f"failed: {exc}"
        print(f"    -> {outcome}")
    if args.yes:
        store.save()
        print("state saved")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
