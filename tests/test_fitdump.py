"""Tests for pelocore.fitdump: round-trips against our own FIT builders."""

from __future__ import annotations

import json
from typing import Any

from pelocore.fitbuild import build_activity_fit
from pelocore.fitdump import FitWalker, dump_bytes, dump_file
from tests.conftest import cycling_performance, make_workout


def _session_values(data: bytes) -> dict[str, Any]:
    dump = FitWalker().walk(data)
    session = next(m for m in dump.messages if m.name == "session")
    return session.values


def test_roundtrip_cycling_counts_and_session() -> None:
    workout = make_workout()
    result = build_activity_fit(workout, cycling_performance())

    text = dump_bytes(result.data)
    assert "file_id=1" in text
    assert "session=1" in text
    assert "record=10" in text

    values = _session_values(result.data)
    assert values["field_5"] == "2 (cycling)"
    assert values["field_6"] == "6 (indoor_cycling)"


def test_roundtrip_strength_plan_messages() -> None:
    from pelocore.peloton import ExerciseBlock

    workout = make_workout(discipline="strength", title="Strength 30")
    plan = [
        ExerciseBlock(name="Squat Jumps", duration_s=45),
        ExerciseBlock(name="Push-ups", duration_s=45),
    ]
    result = build_activity_fit(workout, None, plan=plan)
    dump = FitWalker().walk(result.data)
    assert dump.counts.get("set") == 2
    assert dump.counts.get("exercise_title") == 2


def test_timestamps_render_as_iso() -> None:
    workout = make_workout()
    result = build_activity_fit(workout, cycling_performance())
    values = _session_values(result.data)
    # start_time = 1_700_000_000 epoch = 2023-11-14T22:13:20Z
    assert str(values["field_2"]).startswith("2023-11-14")


def test_json_output(tmp_path: Any) -> None:
    workout = make_workout()
    result = build_activity_fit(workout, cycling_performance())
    fit_path = tmp_path / "t.fit"
    fit_path.write_bytes(result.data)
    payload = json.loads(dump_file(fit_path, as_json=True))
    assert payload["counts"]["session"] == 1
    assert payload["header"]["magic_ok"] is True


def test_records_limit() -> None:
    workout = make_workout()
    result = build_activity_fit(workout, cycling_performance())
    text = dump_bytes(result.data, records_limit=2)
    assert "record=10" in text  # counted even when not dumped
    dumped = [line for line in text.splitlines() if line.startswith("-- record")]
    assert len(dumped) == 2


def test_invalid_values_become_none() -> None:
    workout = make_workout()
    result = build_activity_fit(workout, None)  # summary-only: anchor record
    dump = FitWalker().walk(result.data, records_limit=1)
    anchor = next(m for m in dump.messages if m.name == "record")
    assert anchor.values.get("field_6") is None  # no speed: invalid-encoded
