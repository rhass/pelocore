"""Tests for pelocore.status renderers."""

from __future__ import annotations

from pelocore import status
from pelocore.state import CycleError, CycleReport, StateStore
from pelocore.sync import fit_filename


def _populated_store(store: StateStore) -> StateStore:
    store.record_uploaded("w1", md5="m", import_id="j", fit_filename=fit_filename("w1"),
                          title="Ride", instructor="Coach", discipline="cycling")
    store.record_failure("w2", "boom <b>", title="Title <script>", instructor="Someone",
                         discipline="running")
    store.append_cycle(CycleReport(
        started_at="2026-10-06T00:00:00+00:00",
        outcome="partial",
        fetched=2,
        uploaded=1,
        skipped=0,
        failed=1,
        errors=[CycleError(error="boom <b>", at="t", workout_id="w2", title="Title <script>",
                           instructor="Someone", discipline="running")],
    ))
    return store


def test_payload_shape(store: StateStore) -> None:
    payload = status.build_payload(
        version="9.9.9",
        store=_populated_store(store),
        uptime_seconds=12.5,
        next_sync_in_seconds=42.0,
        sync_running=False,
    )
    assert payload["service"] == "pelocore"
    assert payload["version"] == "9.9.9"
    assert payload["counts"] == {"synced": 1, "failed": 1}
    assert payload["last_cycle"]["outcome"] == "partial"
    assert payload["last_cycle"]["errors"][0]["error"] == "boom <b>"
    assert payload["next_sync_in_seconds"] == 42
    assert len(payload["history"]) == 1


def test_html_escapes_user_data(store: StateStore) -> None:
    payload = status.build_payload(
        version="0.1.0",
        store=_populated_store(store),
        uptime_seconds=0,
        next_sync_in_seconds=None,
        sync_running=False,
    )
    html = status.render_html(payload)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "boom &lt;b&gt;" in html
    assert "pelocore" in html
    assert "partial" in html


def test_html_when_never_synced(store: StateStore) -> None:
    payload = status.build_payload(
        version="0.1.0", store=store, uptime_seconds=0,
        next_sync_in_seconds=None, sync_running=False,
    )
    html = status.render_html(payload)
    assert "no sync has run yet" in html


def test_metrics_format() -> None:
    body = status.render_metrics(
        cycles_total=3,
        uploaded_total=5,
        failed_total=1,
        last_success_timestamp=1234.5,
        uptime_seconds=12.0,
        last_outcome="ok",
    )
    assert "pelocore_sync_cycles_total 3" in body
    assert "pelocore_sync_workouts_uploaded_total 5" in body
    assert "pelocore_sync_workouts_failed_total 1" in body
    assert "pelocore_last_sync_success_timestamp_seconds 1234.5" in body
    assert 'pelocore_last_cycle_outcome{outcome="ok"} 1' in body
    assert 'pelocore_last_cycle_outcome{outcome="failed"} 0' in body
    assert body.endswith("\n")


def test_metrics_none_timestamp() -> None:
    body = status.render_metrics(
        cycles_total=0,
        uploaded_total=0,
        failed_total=0,
        last_success_timestamp=None,
        uptime_seconds=0.0,
        last_outcome=None,
    )
    assert "pelocore_last_sync_success_timestamp_seconds 0" in body


def test_json_render(store: StateStore) -> None:
    import json

    payload = status.build_payload(
        version="0.1.0", store=store, uptime_seconds=1, next_sync_in_seconds=5, sync_running=True
    )
    parsed = json.loads(status.render_json(payload))
    assert parsed["sync_running"] is True
    assert parsed["next_sync_in_seconds"] == 5
