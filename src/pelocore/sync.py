"""Sync orchestration: one cycle from Peloton listing to COROS import."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Protocol

from pelocore.config import Settings
from pelocore.coros import ActivityItem, CorosAmbiguousMatchError, ImportJob, UploadResult
from pelocore.fitbuild import CONVERTER_VERSION, FitBuildResult, build_activity_fit
from pelocore.peloton import ExerciseBlock, PelotonSource, PelotonWorkout, display_name
from pelocore.sports import coros_sport_code, parse_remaps
from pelocore.state import CycleError, CycleReport, StateStore

logger = logging.getLogger(__name__)

COMPLETE_STATUS = "COMPLETE"


def fit_filename(workout_id: str, *, version: int = 1) -> str:
    """The name COROS sees for this workout; used for cross-run dedupe.

    Version 1 files are unsuffixed; later converter versions carry a
    ``.v<n>`` suffix so the import list doubles as the version record in
    stateless mode.
    """
    suffix = "" if version <= 1 else f".v{version}"
    return f"peloton-{workout_id}{suffix}.fit"


class CorosUploader(Protocol):
    def imported_filenames(self) -> set[str]: ...
    def imported_versions(self, size: int = 100) -> dict[str, int]: ...
    def find_activity(
        self, start_time: int, sport_hint: int | None = None
    ) -> ActivityItem | None: ...
    def upload_fit(self, fit_bytes: bytes, filename: str) -> UploadResult: ...
    def wait_for_import(self, import_id: str, *, timeout_s: float) -> ImportJob | None: ...
    def delete_activity(self, label_id: str) -> None: ...
    def import_jobs(self, size: int = 50) -> list[ImportJob]: ...
    def remove_from_import_list(self, import_id: str) -> None: ...
    def rename_after_import(
        self,
        start_time: int,
        sport_hint: int | None,
        name: str,
        *,
        timeout_s: float = 90.0,
        interval_s: float = 10.0,
    ) -> bool: ...


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
        self._remaps = parse_remaps(settings.sport_remaps)

    def sync_workout_by_id(self, workout_id: str, *, force: bool = False) -> CycleReport:
        """Targeted import of a single workout, bypassing the backfill window."""
        report = CycleReport(started_at=_now_iso(), trigger="manual")
        try:
            workout = self._peloton.workout_by_id(workout_id)
        except Exception as exc:
            report.outcome = "failed"
            report.errors.append(
                CycleError(
                    error=f"fetching workout {workout_id}: {exc}",
                    at=_now_iso(),
                    source="peloton",
                )
            )
            self._finish(report)
            return report
        if workout is None:
            report.outcome = "failed"
            report.errors.append(
                CycleError(
                    error=f"workout {workout_id} not found",
                    at=_now_iso(),
                    source="peloton",
                )
            )
            self._finish(report)
            return report
        report.fetched = 1
        imported_versions = {} if force else self._reconcile_imported()
        decision, _recorded = self._sync_decision(workout, imported_versions)
        if not force and decision == "skip":
            report.skipped += 1
            self._finish(report)
            return report
        built = self._hydrate(workout, report)
        if built is not None:
            self._upload(workout, built.data, report)
        report.outcome = "ok" if report.failed == 0 else "failed"
        self._finish(report)
        return report

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

        imported_versions = self._reconcile_imported()

        # Classify first: new / skip / upgrade. Drift detection is version
        # stamp based (state record or import-list filename), so deciding
        # costs no rebuilds and no refetches.
        actions: list[tuple[str, PelotonWorkout, int]] = []
        for workout in candidates:
            decision, recorded = self._sync_decision(workout, imported_versions)
            if decision == "skip":
                report.skipped += 1
                logger.debug("skipping workout %s (version %s)", workout.id, recorded)
            else:
                actions.append((decision, workout, recorded))

        # Phase 1: hydrate everything from Peloton first - fetch performance
        # data and build the FIT files before touching COROS, so Peloton-side
        # failures never leave a half-synced batch.
        hydrated: list[tuple[str, PelotonWorkout, bytes, int]] = []
        for decision, workout, recorded in actions:
            built = self._hydrate(workout, report)
            if built is not None:
                hydrated.append((decision, workout, built.data, recorded))

        # Phase 2: upload the fully hydrated batch to COROS.
        for decision, workout, fit_bytes, recorded in hydrated:
            if decision == "upgrade":
                if not self._upgrade(workout, recorded, report):
                    continue
                report.upgraded += 1
            self._upload(workout, fit_bytes, report)

        if report.failed == 0:
            report.outcome = "ok"
        elif report.uploaded > 0:
            report.outcome = "partial"
        else:
            report.outcome = "failed"
        self._finish(report)
        return report

    def _reconcile_imported(self) -> dict[str, int]:
        """COROS-side dedupe source: workout_id -> imported converter version.

        Failures are non-fatal (fall back to state only).
        """
        try:
            versions: dict[str, int] = self._coros.imported_versions()
            return versions
        except Exception as exc:
            logger.warning("COROS reconcile failed (%s); relying on local state", exc)
            return {}

    def _sync_decision(
        self, workout: PelotonWorkout, imported_versions: dict[str, int]
    ) -> tuple[str, int]:
        """Decide new/skip/upgrade for one candidate.

        Drift (recorded converter version < current) triggers an upgrade
        when auto-upgrade is on; otherwise the workout is skipped. Zero API
        cost beyond the import-list read the cycle performs anyway.
        """
        record = self._store.get(workout.id)
        if record is not None and record.status == "uploaded":
            recorded = record.converter_version or 1
        else:
            recorded = imported_versions.get(workout.id, 0)
        if recorded >= CONVERTER_VERSION:
            return "skip", recorded
        if recorded > 0 and self._settings.auto_upgrade:
            return "upgrade", recorded
        if recorded > 0:
            return "skip", recorded  # stale but auto-upgrade disabled
        return "new", 0

    def _upgrade(
        self, workout: PelotonWorkout, recorded_version: int, report: CycleReport
    ) -> bool:
        """Delete the stale COROS activity + import entries for one workout.

        Conservative: the old activity must resolve uniquely (start time
        window, sport-hinted with unique time fallback); ambiguous or
        missing targets are skipped with a warning rather than deleted
        blind. Returns True when the slot is clear for re-upload.
        """
        try:
            code = coros_sport_code(
                workout.fitness_discipline, is_outdoor=workout.is_outdoor
            )
            old = self._coros.find_activity(workout.start_time, code)
        except CorosAmbiguousMatchError as exc:
            report.errors.append(
                CycleError(
                    error=f"upgrade skipped: ambiguous old activity ({exc})",
                    at=_now_iso(),
                    source="coros",
                    workout_id=workout.id,
                    title=workout.title,
                )
            )
            report.skipped += 1
            return False
        except Exception as exc:
            logger.warning("upgrade lookup failed for %s: %s", workout.id, exc)
            report.errors.append(
                CycleError(
                    error=f"upgrade skipped: {exc}",
                    at=_now_iso(),
                    source="coros",
                    workout_id=workout.id,
                    title=workout.title,
                )
            )
            report.skipped += 1
            return False
        try:
            if old is None:
                # the stale activity is already gone (deleted manually);
                # purging import entries is safe and re-upload proceeds
                self._purge_import_entries(workout.id)
                logger.info("upgrade: no old activity for %s; re-uploading", workout.id)
                return True
            self._coros.delete_activity(old.label_id)
            self._purge_import_entries(workout.id)
            logger.info(
                "upgrade: deleted stale activity %s for %s (was v%s)",
                old.label_id,
                workout.id,
                recorded_version,
            )
            return True
        except Exception as exc:
            logger.warning("upgrade failed for %s: %s", workout.id, exc)
            report.errors.append(
                CycleError(
                    error=f"upgrade skipped: {exc}",
                    at=_now_iso(),
                    source="coros",
                    workout_id=workout.id,
                    title=workout.title,
                )
            )
            report.skipped += 1
            return False

    def _purge_import_entries(self, workout_id: str) -> None:
        try:
            for job in self._coros.import_jobs(size=100):
                if (job.original_filename or "").startswith(f"peloton-{workout_id}."):
                    self._coros.remove_from_import_list(job.id)
        except Exception as exc:
            logger.warning("import-entry purge failed for %s: %s", workout_id, exc)

    def _hydrate(
        self, workout: PelotonWorkout, report: CycleReport
    ) -> FitBuildResult | None:
        """Fetch performance data (and the class plan for strength) and build
        the FIT file; None on failure."""
        try:
            detail = self._peloton.workout_by_id(workout.id)
            if detail is not None:
                workout = detail  # detail payload carries the class title
            perf = self._peloton.performance(workout.id)
            plan: list[ExerciseBlock] | None = None
            if not perf.samples and not perf.locations and workout.ride_id:
                plan = self._peloton.class_plan(workout.ride_id) or None
            built = build_activity_fit(
                workout, perf, plan=plan, remaps=self._remaps
            )
            logger.debug(
                "hydrated %s (%s) - %d records",
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
        filename = fit_filename(workout.id, version=CONVERTER_VERSION)
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
                start_time=workout.start_time,
                converter_version=CONVERTER_VERSION,
            )
            report.uploaded += 1
            logger.info("uploaded %s (%s)", workout.id, workout.title)
        except Exception as exc:
            self._record_error(workout, str(exc) or exc.__class__.__name__, report)
            return
        self._rename(workout, report)

    def _rename(self, workout: PelotonWorkout, report: CycleReport) -> None:
        """Rename the just-imported activity to the Peloton title.

        The COROS importer ignores FIT-provided names; the labelId only
        exists after import processing, so this resolves it by polling.
        Failures are non-fatal: the upload itself succeeded.
        """
        try:
            renamed = self._coros.rename_after_import(
                workout.start_time,
                coros_sport_code(workout.fitness_discipline, is_outdoor=workout.is_outdoor),
                display_name(workout.title, workout.instructor),
                timeout_s=self._settings.import_poll_seconds,
            )
        except Exception as exc:
            logger.warning("rename after upload failed for %s: %s", workout.id, exc)
            return
        if renamed:
            logger.info(
                "renamed %s to %r",
                workout.id,
                display_name(workout.title, workout.instructor),
            )
        else:
            logger.warning(
                "could not resolve labelId for %s within the poll window; "
                "run `pelocore rename` later",
                workout.id,
            )

    def rename_uploaded(self) -> int:
        """Retroactively rename every uploaded workout in state."""
        renamed = 0
        for workout_id, rec in self._store.uploaded().items():
            if not rec.start_time:
                logger.warning("no start_time recorded for %s; skipping", workout_id)
                continue
            name = display_name(rec.title, rec.instructor)
            try:
                detail = self._peloton.workout_by_id(workout_id)
            except Exception as exc:
                logger.warning("could not refresh title for %s: %s", workout_id, exc)
                detail = None
            if detail is not None:
                name = display_name(detail.title, detail.instructor)
            try:
                done = self._coros.rename_after_import(
                    rec.start_time,
                    coros_sport_code((rec.discipline or "").strip().lower()),
                    name,
                    timeout_s=15.0,
                    interval_s=5.0,
                )
            except Exception as exc:
                logger.warning("rename failed for %s: %s", workout_id, exc)
                continue
            if done:
                renamed += 1
                print(f"renamed {workout_id} -> {name!r}")
            else:
                print(f"no matching COROS activity for {workout_id}")
        return renamed

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
