"""Tests for pelocore.state."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from pelocore.state import CycleError, CycleReport, StateStore


def test_uploaded_roundtrip(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.json")
    store.record_uploaded(
        "w1",
        md5="m",
        import_id="j",
        fit_filename="peloton-w1.fit",
        title="Ride",
        instructor="Coach",
        discipline="cycling",
    )
    assert store.is_synced("w1")
    store.save()

    reloaded = StateStore(tmp_path / "state.json")
    assert reloaded.is_synced("w1")
    rec = reloaded.get("w1")
    assert rec is not None and rec.status == "uploaded" and rec.md5 == "m"


def test_failure_increments_attempts(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.json")
    store.record_failure("w1", "boom", title="Ride", instructor=None, discipline="cycling")
    store.record_failure("w1", "boom again", title="Ride", instructor=None, discipline="cycling")
    rec = store.get("w1")
    assert rec is not None
    assert rec.attempts == 2
    assert rec.last_error == "boom again"
    assert not store.is_synced("w1")


def test_counts(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.json")
    store.record_uploaded(
        "w1", md5="m", import_id="j", fit_filename="f", title=None, instructor=None, discipline=None
    )
    store.record_failure("w2", "err", title=None, instructor=None, discipline=None)
    assert store.counts == {"synced": 1, "failed": 1}


def test_history_cap(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.json")
    for i in range(60):
        store.append_cycle(CycleReport(started_at=str(i)))
    assert len(store.history) == 50
    last = store.last_cycle
    assert last is not None and last.started_at == "59"


def test_atomic_save_leaves_no_temp_files(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "state.json"
    store = StateStore(path)
    store.record_uploaded(
        "w1", md5="m", import_id="j", fit_filename="f", title=None, instructor=None, discipline=None
    )
    store.save()
    files = list((tmp_path / "nested").iterdir())
    assert files == [path]
    data = json.loads(path.read_text())
    assert data["version"] == 1
    assert path.stat().st_mode & 0o777 == 0o600


def test_load_tolerates_garbage(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text("{not json")
    store = StateStore(path)
    assert store.history == []
    assert store.counts == {"synced": 0, "failed": 0}
    # and can recover by saving
    store.record_uploaded(
        "w1", md5="m", import_id="j", fit_filename="f", title=None, instructor=None, discipline=None
    )
    store.save()
    assert StateStore(path).is_synced("w1")


def test_cycle_report_json_roundtrip() -> None:
    report = CycleReport(
        started_at="t0",
        outcome="partial",
        errors=[CycleError(error="x", at="t1", workout_id="w1")],
    )
    restored = CycleReport.from_dict(json.loads(json.dumps(_to_dict(report))))
    assert restored.outcome == "partial"
    assert restored.errors[0].workout_id == "w1"


def _to_dict(report: CycleReport) -> dict[str, Any]:
    return asdict(report)
