"""Tests for pelocore.sync (engine with fakes)."""

from __future__ import annotations

from pelocore.config import Settings
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
