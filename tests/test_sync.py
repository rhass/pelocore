"""Tests for pelocore.sync (engine with fakes)."""

from __future__ import annotations

from pelocore.config import Settings
from pelocore.coros import UploadResult
from pelocore.peloton import WorkoutPerformance
from pelocore.state import StateStore
from pelocore.sync import SyncEngine, fit_filename
from tests.conftest import FakeCoros, FakePeloton, cycling_performance, make_workout


def build_engine(
    peloton: FakePeloton, coros: FakeCoros, store: StateStore, settings: Settings
) -> SyncEngine:
    return SyncEngine(peloton, coros, store, settings)


def test_two_workouts_uploaded_then_skipped(store: StateStore, settings: Settings) -> None:
    peloton = FakePeloton(workouts=[make_workout("w1"), make_workout("w2")], performances={
        "w1": cycling_performance(), "w2": cycling_performance(),
    })
    coros = FakeCoros()
    engine = build_engine(peloton, coros, store, settings)

    report = engine.run_cycle(trigger="cli")
    assert report.fetched == 2
    assert report.uploaded == 2
    assert report.skipped == 0
    assert report.failed == 0
    assert report.outcome == "ok"
    assert len(coros.uploads) == 2
    assert [f for _, f in coros.uploads] == [fit_filename("w1"), fit_filename("w2")]

    # second cycle: local state dedupes
    report2 = engine.run_cycle(trigger="cli")
    assert report2.uploaded == 0
    assert report2.skipped == 2


def test_reconcile_dedupe_on_fresh_state(store: StateStore, settings: Settings) -> None:
    peloton = FakePeloton(workouts=[make_workout("w1")], performances={"w1": cycling_performance()})
    coros = FakeCoros(imported={fit_filename("w1")})
    engine = build_engine(peloton, coros, store, settings)
    report = engine.run_cycle()
    assert report.uploaded == 0
    assert report.skipped == 1


def test_non_complete_workouts_filtered(store: StateStore, settings: Settings) -> None:
    peloton = FakePeloton(workouts=[
        make_workout("w1", status="IN_PROGRESS"),
        make_workout("w2", status="COMPLETE"),
    ], performances={"w2": cycling_performance()})
    coros = FakeCoros()
    engine = build_engine(peloton, coros, store, settings)
    report = engine.run_cycle()
    assert report.fetched == 2
    assert report.uploaded == 1  # only w2
    assert store.get("w1") is None  # non-COMPLETE workout never entered state


def test_upload_failure_isolated_and_recorded(store: StateStore, settings: Settings) -> None:
    peloton = FakePeloton(
        workouts=[make_workout("w1"), make_workout("w2")],
        performances={"w1": cycling_performance(), "w2": cycling_performance()},
    )
    coros = FakeCoros(upload_error=RuntimeError("s3 down"))
    engine = build_engine(peloton, coros, store, settings)
    report = engine.run_cycle()
    assert report.uploaded == 0
    assert report.failed == 2
    assert report.outcome == "failed"
    assert len(report.errors) == 2
    assert report.errors[0].workout_id == "w1"
    rec = store.get("w1")
    assert rec is not None and rec.status == "failed" and rec.attempts == 1

    # next cycle retries: failure clears, uploads succeed
    coros2 = FakeCoros()
    engine2 = SyncEngine(peloton, coros2, store, settings)
    report2 = engine2.run_cycle()
    assert report2.uploaded == 2
    assert report2.outcome == "ok"


def test_listing_failure_is_cycle_level(store: StateStore, settings: Settings) -> None:
    peloton = FakePeloton(list_error=RuntimeError("peloton 500"))
    coros = FakeCoros()
    engine = build_engine(peloton, coros, store, settings)
    report = engine.run_cycle()
    assert report.outcome == "failed"
    assert report.fetched == 0
    assert report.errors[0].source == "peloton"


def test_reconcile_failure_non_fatal(store: StateStore, settings: Settings) -> None:
    class BrokenCoros(FakeCoros):
        def imported_filenames(self) -> set[str]:
            raise RuntimeError("coros down")

    peloton = FakePeloton(workouts=[make_workout("w1")], performances={"w1": cycling_performance()})
    engine = build_engine(peloton, BrokenCoros(), store, settings)
    report = engine.run_cycle()
    assert report.uploaded == 1  # proceeds with empty reconcile set
    assert report.outcome == "ok"


def test_poll_is_called_with_configured_timeout(store: StateStore, settings: Settings) -> None:
    peloton = FakePeloton(workouts=[make_workout("w1")], performances={"w1": cycling_performance()})
    coros = FakeCoros()
    engine = build_engine(peloton, coros, store, settings)
    engine.run_cycle()
    assert coros.polls == [("job-1", settings.import_poll_seconds)]


def test_hydration_failure_blocks_upload_for_that_workout(
    store: StateStore, settings: Settings
) -> None:
    """Phase 1 (hydrate) failures must not reach the upload phase."""
    peloton = FakePeloton(
        workouts=[make_workout("w1"), make_workout("w2")],
        performances={"w1": cycling_performance()},  # w2 has no performance data
        perf_error=RuntimeError("peloton perf 500"),
    )
    # FakePeloton.performance raises perf_error for ANY id; make it selective.
    class SelectivePeloton(FakePeloton):
        def performance(self, workout_id: str) -> WorkoutPerformance:
            if workout_id == "w2":
                raise RuntimeError("peloton perf 500")
            return super().performance(workout_id)

    coros = FakeCoros()
    engine = SyncEngine(
        SelectivePeloton(peloton.workouts, {"w1": cycling_performance()}), coros, store, settings
    )
    report = engine.run_cycle()

    assert report.failed == 1
    assert report.uploaded == 1  # w1 still uploads; only w2's hydration failed
    assert report.errors[0].source == "peloton"
    assert report.errors[0].workout_id == "w2"
    assert len(coros.uploads) == 1  # w2 never reached the upload phase


def test_upload_phase_isolated_from_hydration_phase(
    store: StateStore, settings: Settings
) -> None:
    """All hydration happens before any upload: a Peloton outage mid-batch
    cannot strand already-built workouts unhydrated."""
    calls: list[str] = []

    class OrderedPeloton(FakePeloton):
        def performance(self, workout_id: str) -> WorkoutPerformance:
            calls.append(f"perf:{workout_id}")
            return super().performance(workout_id)

    class OrderedCoros(FakeCoros):
        def upload_fit(self, fit_bytes: bytes, filename: str) -> UploadResult:
            calls.append(f"upload:{filename}")
            return super().upload_fit(fit_bytes, filename)

    peloton = OrderedPeloton(
        workouts=[make_workout("w1"), make_workout("w2")],
        performances={"w1": cycling_performance(), "w2": cycling_performance()},
    )
    engine = SyncEngine(peloton, OrderedCoros(), store, settings)
    report = engine.run_cycle()

    assert report.uploaded == 2
    perf_calls = [c for c in calls if c.startswith("perf:")]
    upload_calls = [c for c in calls if c.startswith("upload:")]
    assert calls.index(perf_calls[-1]) < calls.index(upload_calls[0]), (
        "all performance fetches must complete before the first upload"
    )


def test_successful_upload_triggers_rename(
    store: StateStore, settings: Settings
) -> None:
    peloton = FakePeloton(workouts=[make_workout("w1")], performances={"w1": cycling_performance()})
    coros = FakeCoros()
    engine = build_engine(peloton, coros, store, settings)
    report = engine.run_cycle()
    assert report.uploaded == 1
    assert coros.renames == [(1_700_000_000, 201, "Power Zone Ride")]


def test_rename_failure_does_not_fail_cycle(
    store: StateStore, settings: Settings
) -> None:
    class RenamingCoros(FakeCoros):
        def rename_after_import(
            self,
            start_time: int,
            sport_hint: int | None,
            name: str,
            *,
            timeout_s: float = 90.0,
            interval_s: float = 10.0,
        ) -> bool:
            raise RuntimeError("rename endpoint down")

    peloton = FakePeloton(workouts=[make_workout("w1")], performances={"w1": cycling_performance()})
    engine = SyncEngine(peloton, RenamingCoros(), store, settings)
    report = engine.run_cycle()
    assert report.outcome == "ok" and report.uploaded == 1
