"""Durable sync state: per-workout records plus capped cycle history.

The state file is JSON written atomically (write to a temporary file in the
same directory, then ``os.replace``), so a crash never leaves a torn file.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

STATE_VERSION = 1
HISTORY_LIMIT = 50


def _utcnow_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class WorkoutRecord:
    workout_id: str
    status: str  # "uploaded" | "failed"
    synced_at: str
    md5: str | None = None
    import_id: str | None = None
    fit_filename: str | None = None
    title: str | None = None
    instructor: str | None = None
    discipline: str | None = None
    attempts: int = 1
    last_error: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WorkoutRecord:
        return cls(
            workout_id=data["workout_id"],
            status=data["status"],
            synced_at=data["synced_at"],
            md5=data.get("md5"),
            import_id=data.get("import_id"),
            fit_filename=data.get("fit_filename"),
            title=data.get("title"),
            instructor=data.get("instructor"),
            discipline=data.get("discipline"),
            attempts=int(data.get("attempts", 1)),
            last_error=data.get("last_error"),
        )


@dataclass
class CycleError:
    """One per-workout (or cycle-level, when ``workout_id`` is None) failure."""

    error: str
    at: str
    source: str | None = None  # "peloton" | "coros" | None
    workout_id: str | None = None
    title: str | None = None
    instructor: str | None = None
    discipline: str | None = None
    attempts: int = 1

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CycleError:
        return cls(
            error=data["error"],
            at=data["at"],
            source=data.get("source"),
            workout_id=data.get("workout_id"),
            title=data.get("title"),
            instructor=data.get("instructor"),
            discipline=data.get("discipline"),
            attempts=int(data.get("attempts", 1)),
        )


@dataclass
class CycleReport:
    started_at: str
    finished_at: str | None = None
    outcome: str = "ok"  # "ok" | "partial" | "failed"
    trigger: str = "scheduled"  # "scheduled" | "manual" | "cli"
    fetched: int = 0
    uploaded: int = 0
    skipped: int = 0
    failed: int = 0
    errors: list[CycleError] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CycleReport:
        return cls(
            started_at=data["started_at"],
            finished_at=data.get("finished_at"),
            outcome=data.get("outcome", "ok"),
            trigger=data.get("trigger", "scheduled"),
            fetched=int(data.get("fetched", 0)),
            uploaded=int(data.get("uploaded", 0)),
            skipped=int(data.get("skipped", 0)),
            failed=int(data.get("failed", 0)),
            errors=[CycleError.from_dict(e) for e in data.get("errors", [])],
        )

    def summary_line(self) -> str:
        return (
            f"outcome={self.outcome} fetched={self.fetched} "
            f"uploaded={self.uploaded} skipped={self.skipped} failed={self.failed}"
        )


class StateStore:
    """Thread-safe state file holding workout records and cycle history."""

    def __init__(self, path: Path):
        self._path = Path(path)
        self._lock = threading.RLock()
        self._workouts: dict[str, WorkoutRecord] = {}
        self._history: list[CycleReport] = []
        self.load()

    # -- persistence --------------------------------------------------------

    def load(self) -> None:
        with self._lock:
            self._workouts = {}
            self._history = []
            try:
                raw = self._path.read_text(encoding="utf-8")
            except FileNotFoundError:
                return
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                return
            if data.get("version") != STATE_VERSION:
                return
            self._workouts = {
                wid: WorkoutRecord.from_dict(rec)
                for wid, rec in data.get("workouts", {}).items()
            }
            self._history = [CycleReport.from_dict(rep) for rep in data.get("history", [])]

    def save(self) -> None:
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "version": STATE_VERSION,
                "workouts": {wid: asdict(rec) for wid, rec in self._workouts.items()},
                "history": [asdict(rep) for rep in self._history],
            }
            fd, tmp_name = tempfile.mkstemp(
                dir=self._path.parent, prefix=f".{self._path.name}.", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh, indent=2, sort_keys=True)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.chmod(tmp_name, 0o600)
                os.replace(tmp_name, self._path)
            except BaseException:
                with _suppressed():
                    os.unlink(tmp_name)
                raise

    # -- workout records ----------------------------------------------------

    def is_synced(self, workout_id: str) -> bool:
        with self._lock:
            rec = self._workouts.get(workout_id)
            return rec is not None and rec.status == "uploaded"

    def get(self, workout_id: str) -> WorkoutRecord | None:
        with self._lock:
            return self._workouts.get(workout_id)

    def record_uploaded(
        self,
        workout_id: str,
        *,
        md5: str,
        import_id: str,
        fit_filename: str,
        title: str | None,
        instructor: str | None,
        discipline: str | None,
    ) -> WorkoutRecord:
        with self._lock:
            previous = self._workouts.get(workout_id)
            rec = WorkoutRecord(
                workout_id=workout_id,
                status="uploaded",
                synced_at=_utcnow_iso(),
                md5=md5,
                import_id=import_id,
                fit_filename=fit_filename,
                title=title,
                instructor=instructor,
                discipline=discipline,
                attempts=(previous.attempts if previous else 0) + 1,
            )
            self._workouts[workout_id] = rec
            return rec

    def record_failure(
        self,
        workout_id: str,
        error: str,
        *,
        title: str | None,
        instructor: str | None,
        discipline: str | None,
    ) -> WorkoutRecord:
        with self._lock:
            previous = self._workouts.get(workout_id)
            rec = WorkoutRecord(
                workout_id=workout_id,
                status="failed",
                synced_at=_utcnow_iso(),
                title=title,
                instructor=instructor,
                discipline=discipline,
                attempts=(previous.attempts if previous else 0) + 1,
                last_error=error,
            )
            self._workouts[workout_id] = rec
            return rec

    @property
    def counts(self) -> dict[str, int]:
        with self._lock:
            uploaded = sum(1 for r in self._workouts.values() if r.status == "uploaded")
            failed = len(self._workouts) - uploaded
            return {"synced": uploaded, "failed": failed}

    # -- cycle history ------------------------------------------------------

    def append_cycle(self, report: CycleReport) -> None:
        with self._lock:
            self._history.append(report)
            self._history = self._history[-HISTORY_LIMIT:]

    @property
    def history(self) -> list[CycleReport]:
        with self._lock:
            return list(self._history)

    @property
    def last_cycle(self) -> CycleReport | None:
        with self._lock:
            return self._history[-1] if self._history else None


class _suppressed:
    """Context manager that swallows exceptions (used for best-effort cleanup)."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc_info: object) -> bool:
        return True
