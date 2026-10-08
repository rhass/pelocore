"""Sync orchestration: one cycle from Peloton listing to COROS import."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Protocol

from pelocore.config import Settings
from pelocore.coros import ImportJob, UploadResult
from pelocore.fitbuild import FitBuildResult, build_activity_fit
from pelocore.peloton import PelotonSource, PelotonWorkout
from pelocore.state import CycleError, CycleReport, StateStore

logger = logging.getLogger(__name__)

COMPLETE_STATUS = "COMPLETE"


class CorosUploader(Protocol):
    def imported_filenames(self) -> set[str]: ...
    def upload_fit(self, fit_bytes: bytes, filename: str) -> UploadResult: ...
    def wait_for_import(self, import_id: str, *, timeout_s: float) -> ImportJob | None: ...


def fit_filename(workout_id: str) -> str:
    """The name COROS sees for this workout; used for cross-run dedupe."""
    return f"peloton-{workout_id}.fit"


class SyncEngine:
    """Runs sync cycles with per-workout error isolation."""

    def __init__(
        self,
        peloton: PelotonSource,
        coros: CorosUploader,
        store: StateStore,
        settings: Settings,
    ):
        self._peloton = peloton
        self._coros = coros
        self._store = store
        self._settings = settings

    def run_cycle(self, *, trigger: str = "scheduled") -> CycleReport:
        report = CycleReport(started_at=_now_iso(), trigger=trigger)
        try:
            workouts = self._peloton.workouts_since(self._settings.backfill_days)
        except Exception as exc:
            report.outcome = "failed"
            report.errors.append(
                CycleError(
                    error=f"listing Peloton workouts: {exc}",
                    at=_now_iso(),
                    source="peloton",
                )
            )
            self._finish(report)
            return report

        report.fetched = len(workouts)
        candidates = [w for w in workouts if w.status.upper() == COMPLETE_STATUS]
        logger.info(
            "fetched %d workouts in last %dd; %d eligible (COMPLETE)",
            report.fetched,
            self._settings.backfill_days,
            len(candidates),
        )

        imported = self._reconcile_imported()
        pending = [w for w in candidates if not self._already_synced(w, imported, report)]

        # Phase 1: hydrate everything from Peloton first — fetch performance
        # data and build the FIT files before touching COROS, so Peloton-side
        # failures never leave a half-synced batch.
        hydrated: list[tuple[PelotonWorkout, bytes]] = []
        for workout in pending:
            built = self._hydrate(workout, report)
            if built is not None:
                hydrated.append((workout, built.data))

        # Phase 2: upload the fully hydrated batch to COROS.
        for workout, fit_bytes in hydrated:
            self._upload(workout, fit_bytes, report)

        if report.failed == 0:
            report.outcome = "ok"
        elif report.uploaded > 0:
            report.outcome = "partial"
        else:
            report.outcome = "failed"
        self._finish(report)
        return report

    def _reconcile_imported(self) -> set[str] | None:
        """COROS-side dedupe; failures are non-fatal (fall back to state)."""
        try:
            return self._coros.imported_filenames()
        except Exception as exc:
            logger.warning("COROS reconcile failed (%s); relying on local state", exc)
            return None

    def _already_synced(
        self, workout: PelotonWorkout, imported: set[str] | None, report: CycleReport
    ) -> bool:
        filename = fit_filename(workout.id)
        if self._store.is_synced(workout.id) or (imported is not None and filename in imported):
            report.skipped += 1
            logger.debug("skipping already-synced workout %s", workout.id)
            return True
        return False

    def _hydrate(
        self, workout: PelotonWorkout, report: CycleReport
    ) -> FitBuildResult | None:
        """Fetch performance data and build the FIT file; None on failure."""
        try:
            perf = self._peloton.performance(workout.id)
            built = build_activity_fit(workout, perf)
            logger.debug(
                "hydrated %s (%s) — %d records",
                workout.id,
                workout.title,
                built.record_count,
            )
            return built
        except Exception as exc:
            self._record_error(
                workout, str(exc) or exc.__class__.__name__, report, source="peloton"
            )
            return None

    def _upload(
        self, workout: PelotonWorkout, fit_bytes: bytes, report: CycleReport
    ) -> None:
        filename = fit_filename(workout.id)
        try:
            upload = self._coros.upload_fit(fit_bytes, filename)
            self._coros.wait_for_import(
                upload.import_id,
                timeout_s=self._settings.import_poll_seconds,
            )
            self._store.record_uploaded(
                workout.id,
                md5=upload.md5,
                import_id=upload.import_id,
                fit_filename=filename,
                title=workout.title,
                instructor=workout.instructor,
                discipline=workout.fitness_discipline,
            )
            report.uploaded += 1
            logger.info("uploaded %s (%s)", workout.id, workout.title)
        except Exception as exc:
            self._record_error(workout, str(exc) or exc.__class__.__name__, report)

    def _record_error(
        self,
        workout: PelotonWorkout,
        message: str,
        report: CycleReport,
        *,
        source: str | None = None,
    ) -> None:
        self._store.record_failure(
            workout.id,
            message,
            title=workout.title,
            instructor=workout.instructor,
            discipline=workout.fitness_discipline,
        )
        report.failed += 1
        report.errors.append(
            CycleError(
                error=message,
                at=_now_iso(),
                source=source,
                workout_id=workout.id,
                title=workout.title,
                instructor=workout.instructor,
                discipline=workout.fitness_discipline,
            )
        )
        logger.error("failed to sync workout %s: %s", workout.id, message)

    def _finish(self, report: CycleReport) -> None:
        report.finished_at = _now_iso()
        self._store.append_cycle(report)
        try:
            self._store.save()
        except OSError:
            logger.exception("failed to persist state file")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
