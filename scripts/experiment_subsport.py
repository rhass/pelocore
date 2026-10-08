#!/usr/bin/env python3
"""One-off experiment matrix: what makes the COROS importer drop or accept
a file, and does it dedupe by content?

Runs (all self-cleaning: delete activity + purge import entry):
  A  DEVELOPMENT manufacturer, empty yoga      -> do empty files drop?
  B  DEVELOPMENT manufacturer, yoga +cal +HR  -> control (our normal path)
  C  COROS 294/822,            yoga +cal +HR  -> does spoofing cause drops?
  D  re-upload B's exact bytes                -> dedupe by content?

Usage: op run -- uv run python scripts/experiment_subsport.py [--runs A,B,C,D]
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pelocore.config import Settings  # noqa: E402
from pelocore.coros import CorosClient  # noqa: E402
from pelocore.fitbuild import build_activity_fit  # noqa: E402
from pelocore.peloton import (  # noqa: E402
    PelotonWorkout,
    PerformanceSample,
    PerformanceSummary,
    WorkoutPerformance,
)

COROS_MANUFACTURER = 294
COROS_PRODUCT = 822


def yoga_workout(tag: str) -> PelotonWorkout:
    start_time = int(time.time()) - 7200
    return PelotonWorkout(
        id=f"yogatest{tag}{int(time.time())}",
        status="COMPLETE",
        fitness_discipline="yoga",
        is_outdoor=False,
        start_time=start_time,
        end_time=start_time + 120,
        title=f"pelocore experiment {tag}",
        instructor=None,
        ride_id=None,
    )


def perf_with_content() -> WorkoutPerformance:
    return WorkoutPerformance(
        duration_s=120,
        summary=PerformanceSummary(total_calories=50.0),
        samples=[
            PerformanceSample(offset=i, heart_rate=100 + i % 30) for i in range(120)
        ],
        locations=[],
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", default="A,B,C,D")
    args = parser.parse_args()

    settings = Settings()
    client = CorosClient(
        region=settings.coros_region,
        email=settings.coros_email,
        password=settings.coros_password.get_secret_value(),
        access_token=settings.coros_token_or_none,
        timeout=settings.http_timeout_seconds,
    )

    runs = [r.strip().upper() for r in args.runs.split(",")]
    results: dict[str, str] = {}
    try:
        if "A" in runs:
            results["A"] = run_experiment(client, "A", empty=True)
        if "B" in runs:
            results["B"] = run_experiment(client, "B", empty=False)
        if "C" in runs:
            results["C"] = run_experiment(client, "C", empty=False, spoof=True)
        if "D" in runs and "B" in results:
            results["D"] = run_dedupe_test(client)
    finally:
        print("\n=== RESULTS ===")
        for key, value in results.items():
            print(f"  {key}: {value}")
    return 0


def run_experiment(
    client: CorosClient, tag: str, *, empty: bool, spoof: bool = False
) -> str:
    workout = yoga_workout(tag)
    perf = None if empty else perf_with_content()
    built = build_activity_fit(
        workout,
        perf,
        manufacturer=COROS_MANUFACTURER if spoof else None,
        product=COROS_PRODUCT if spoof else None,
    )
    label = f"{'294' if spoof else '255'}-{'empty' if empty else 'cal+HR'}"
    return upload_check_cleanup(client, workout, built.data, label)


def run_dedupe_test(client: CorosClient) -> str:
    """Upload identical bytes twice; count distinct activities created."""
    workout = yoga_workout("D")
    built = build_activity_fit(workout, perf_with_content())
    outcome1 = upload_check_cleanup(client, workout, built.data, "dedupe-first")
    # second upload of identical bytes, new filename (content unchanged)
    workout2 = PelotonWorkout(
        id=workout.id + "x2",
        status=workout.status,
        fitness_discipline=workout.fitness_discipline,
        is_outdoor=False,
        start_time=workout.start_time + 3600,  # 1h later: unique time window
        end_time=workout.end_time + 3600,
        title=workout.title,
        instructor=None,
        ride_id=None,
    )
    outcome2 = upload_check_cleanup(client, workout2, built.data, "dedupe-second")
    created = (outcome1 != "not-created") + (outcome2 != "not-created")
    return f"{outcome1} | {outcome2} | activities created: {created} -> {'NO dedupe' if created == 2 else 'deduped or dropped'}"


def upload_check_cleanup(
    client: CorosClient, workout: PelotonWorkout, fit_bytes: bytes, label: str
) -> str:
    print(f"--- run {workout.id} ({label}), {len(fit_bytes)} bytes")
    result = client.upload_fit(fit_bytes, f"peloton-{workout.id}.fit")
    print(f"    import_id={result.import_id}")
    job = client.wait_for_import(result.import_id, timeout_s=60, interval_s=5)
    status = job.status if job else None
    print(f"    import status={status}")
    time.sleep(10)

    found = None
    for _ in range(6):
        for activity in client.list_activities(size=50):
            if abs(activity.start_time - workout.start_time) <= 60:
                found = activity
                break
        if found:
            break
        time.sleep(5)

    outcome = "not-created"
    if found is not None:
        outcome = f"created sportType={found.sport_type}"
        print(f"    RESULT: {outcome} (904=yoga, 402=strength)")
        client.delete_activity(found.label_id)
        print(f"    deleted {found.label_id}")
        time.sleep(3)
    else:
        print("    RESULT: no activity created (dropped)")

    # purge the import entry so future reconciles stay clean
    try:
        client.remove_from_import_list(result.import_id)
    except Exception as exc:
        print(f"    (import-entry purge failed: {exc})")
    return outcome


if __name__ == "__main__":
    raise SystemExit(main())
