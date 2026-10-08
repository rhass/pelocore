"""Shared fixtures and fakes for the pelocore test suite."""

from __future__ import annotations

import hashlib
import pathlib
import threading

import pytest

from pelocore.config import Settings
from pelocore.coros import ActivityItem, UploadResult
from pelocore.peloton import (
    ExerciseBlock,
    PelotonWorkout,
    PerformanceSample,
    WorkoutPerformance,
)
from pelocore.state import CycleReport, StateStore


@pytest.fixture
def settings(tmp_path: pathlib.Path) -> Settings:
    return Settings(
        state_path=tmp_path / "state.json",
        backfill_days=7,
        import_poll_seconds=0.0,
        sync_interval_seconds=3600,
    )


class FakePeloton:
    """In-memory PelotonSource."""

    def __init__(
        self,
        workouts: list[PelotonWorkout] | None = None,
        performances: dict[str, WorkoutPerformance] | None = None,
        plans: dict[str, list[ExerciseBlock]] | None = None,
        list_error: Exception | None = None,
        perf_error: Exception | None = None,
    ):
        self.workouts = list(workouts or [])
        self.performances = dict(performances or {})
        self.plans = dict(plans or {})
        self.list_error = list_error
        self.perf_error = perf_error

    def workouts_since(self, days: int) -> list[PelotonWorkout]:
        if self.list_error:
            raise self.list_error
        return list(self.workouts)

    def workout_by_id(self, workout_id: str) -> PelotonWorkout | None:
        return next((w for w in self.workouts if w.id == workout_id), None)

    def class_plan(self, ride_id: str) -> list[ExerciseBlock]:
        return list(self.plans.get(ride_id, []))

    def performance(self, workout_id: str) -> WorkoutPerformance:
        if self.perf_error:
            raise self.perf_error
        if workout_id not in self.performances:
            raise KeyError(workout_id)
        return self.performances[workout_id]


class FakeCoros:
    """In-memory CorosUploader."""

    def __init__(
        self,
        imported: set[str] | None = None,
        upload_error: Exception | None = None,
        activities: list[ActivityItem] | None = None,
    ):
        self.imported = set(imported or ())
        self.upload_error = upload_error
        self.activities = list(activities or [])
        self.deleted: list[str] = []
        self.uploads: list[tuple[bytes, str]] = []
        self.polls: list[tuple[str, float]] = []

    def all_activities(self) -> list[ActivityItem]:
        return list(self.activities)

    def delete_activity(self, label_id: str) -> None:
        self.deleted.append(label_id)
        self.activities = [a for a in self.activities if a.label_id != label_id]

    def imported_filenames(self) -> set[str]:
        return set(self.imported)

    def upload_fit(self, fit_bytes: bytes, filename: str) -> UploadResult:
        if self.upload_error:
            raise self.upload_error
        self.uploads.append((fit_bytes, filename))
        return UploadResult(
            import_id="job-1", filename=filename, md5=hashlib.md5(fit_bytes).hexdigest()
        )

    def wait_for_import(self, import_id: str, *, timeout_s: float) -> None:
        self.polls.append((import_id, timeout_s))
        return


class StubEngine:
    """Stand-in for SyncEngine used by server tests."""

    def __init__(
        self,
        report: CycleReport | None = None,
        *,
        block: bool = False,
        store: StateStore | None = None,
    ):
        self.calls: list[str] = []
        self.report = report
        self.block = block
        self.store = store
        self._gate = threading.Event()
        self._released = threading.Event()

    def run_cycle(self, *, trigger: str) -> CycleReport | None:
        self.calls.append(trigger)
        if self.block:
            self._gate.wait(timeout=5)
        if self.store is not None and self.report is not None:
            self.store.append_cycle(self.report)
        return self.report

    def release(self) -> None:
        self._gate.set()
        self._released.wait(timeout=5)

    @property
    def released(self) -> threading.Event:
        return self._gate


def make_workout(
    workout_id: str = "w1",
    status: str = "COMPLETE",
    discipline: str = "cycling",
    outdoor: bool = False,
    title: str = "Power Zone Ride",
    instructor: str | None = "Denis Morton",
    ride_id: str | None = "ride-1",
) -> PelotonWorkout:
    return PelotonWorkout(
        id=workout_id,
        status=status,
        fitness_discipline=discipline,
        is_outdoor=outdoor,
        start_time=1_700_000_000,
        end_time=1_700_000_900,
        title=title,
        instructor=instructor,
        ride_id=ride_id,
        total_work=586.0,
        ftp=250.0,
    )



def cycling_performance() -> WorkoutPerformance:
    return WorkoutPerformance(
        duration_s=10,
        samples=[
            PerformanceSample(offset=i, power=100 + i, cadence=85, heart_rate=120 + i)
            for i in range(10)
        ],
        locations=[],
    )


@pytest.fixture
def store(tmp_path: pathlib.Path) -> StateStore:
    return StateStore(tmp_path / "state.json")
