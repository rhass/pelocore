"""Shared fixtures and fakes for the pelocore test suite."""

from __future__ import annotations

import hashlib
import pathlib
import threading
from typing import Any

import pytest

from pelocore.config import Settings
from pelocore.coros import ActivityItem, CorosAmbiguousMatchError, UploadResult
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
        details: dict[str, PelotonWorkout] | None = None,
        list_error: Exception | None = None,
        perf_error: Exception | None = None,
    ):
        self.workouts = list(workouts or [])
        self.performances = dict(performances or {})
        self.plans = dict(plans or {})
        self.details = dict(details or {})
        self.list_error = list_error
        self.perf_error = perf_error

    def workouts_since(self, days: int) -> list[PelotonWorkout]:
        if self.list_error:
            raise self.list_error
        return list(self.workouts)

    def workout_by_id(self, workout_id: str) -> PelotonWorkout | None:
        if workout_id in self.details:
            return self.details[workout_id]
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
        self.renames: list[tuple[int, int | None, str]] = []
        self.purged: list[str] = []

    def all_activities(self) -> list[ActivityItem]:
        return list(self.activities)

    def delete_activity(self, label_id: str) -> None:
        self.deleted.append(label_id)
        self.activities = [a for a in self.activities if a.label_id != label_id]

    def find_activity(
        self, start_time: int, sport_hint: int | None = None
    ) -> ActivityItem | None:
        matches = [a for a in self.activities if abs(a.start_time - start_time) <= 60]
        if len(matches) > 1:
            raise CorosAmbiguousMatchError(f"{len(matches)} candidates")
        return matches[0] if matches else None

    def imported_filenames(self) -> set[str]:
        return set(self.imported)

    def import_jobs(self, size: int = 50) -> list[Any]:
        return []

    def remove_from_import_list(self, import_id: str) -> None:
        self.purged.append(import_id)

    def imported_versions(self, size: int = 100) -> dict[str, int]:
        """workout_id -> version, derived from the imported filenames."""
        out: dict[str, int] = {}
        for filename in self.imported:
            if not filename.startswith("peloton-") or not filename.endswith(".fit"):
                continue
            stem = filename[len("peloton-") : -4]
            workout_id, _, version = stem.partition(".v")
            version_num = int(version) if version.isdigit() else 1
            if workout_id and version_num > out.get(workout_id, 0):
                out[workout_id] = version_num
        return out

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

    def rename_after_import(
        self,
        start_time: int,
        sport_hint: int | None,
        name: str,
        *,
        timeout_s: float = 90.0,
        interval_s: float = 10.0,
    ) -> bool:
        self.renames.append((start_time, sport_hint, name))
        return True


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
