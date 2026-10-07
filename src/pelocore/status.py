"""Status page payloads: HTML, JSON, and Prometheus text renderers.

All renderers are pure functions over the state store + server metadata, so
they are trivially testable. User-controlled strings are always escaped.
"""

from __future__ import annotations

import html
from typing import Any

from pelocore.state import CycleReport, StateStore


def build_payload(
    *,
    version: str,
    store: StateStore,
    uptime_seconds: float,
    next_sync_in_seconds: float | None,
    sync_running: bool,
) -> dict[str, Any]:
    last = store.last_cycle
    counts = store.counts
    return {
        "service": "pelocore",
        "version": version,
        "uptime_seconds": round(uptime_seconds, 1),
        "sync_running": sync_running,
        "next_sync_in_seconds": (
            round(max(next_sync_in_seconds, 0), 0) if next_sync_in_seconds is not None else None
        ),
        "counts": counts,
        "last_cycle": _cycle_dict(last) if last is not None else None,
        "history": [_cycle_dict(c) for c in store.history[-10:]],
    }


def _cycle_dict(report: CycleReport) -> dict[str, Any]:
    return {
        "started_at": report.started_at,
        "finished_at": report.finished_at,
        "outcome": report.outcome,
        "trigger": report.trigger,
        "fetched": report.fetched,
        "uploaded": report.uploaded,
        "skipped": report.skipped,
        "failed": report.failed,
        "errors": [
            {
                "workout_id": e.workout_id,
                "title": e.title,
                "instructor": e.instructor,
                "discipline": e.discipline,
                "source": e.source,
                "error": e.error,
                "at": e.at,
            }
            for e in report.errors
        ],
    }


def render_json(payload: dict[str, Any]) -> str:
    import json

    return json.dumps(payload, indent=2)


def render_html(payload: dict[str, Any]) -> str:
    esc = html.escape
    last = payload.get("last_cycle")
    counts = payload.get("counts", {})
    next_in = payload.get("next_sync_in_seconds")

    outcome = last["outcome"] if last else "none"
    badge_class = {"ok": "ok", "partial": "warn", "failed": "bad", "none": "idle"}.get(
        outcome, "idle"
    )

    rows = []
    for cycle in reversed(payload.get("history", [])):
        rows.append(
            "<tr>"
            f"<td>{esc(str(cycle['started_at']))}</td>"
            f"<td><span class='badge {esc(_badge_class(cycle['outcome']))}'>"
            f"{esc(str(cycle['outcome']))}</span></td>"
            f"<td>{esc(str(cycle['trigger']))}</td>"
            f"<td>{cycle['fetched']}</td><td>{cycle['uploaded']}</td>"
            f"<td>{cycle['skipped']}</td><td>{cycle['failed']}</td>"
            "</tr>"
        )

    error_rows = []
    for error in (last or {}).get("errors", []):
        error_rows.append(
            "<tr>"
            f"<td>{esc(str(error.get('workout_id') or '—'))}</td>"
            f"<td>{esc(str(error.get('title') or '—'))}</td>"
            f"<td>{esc(str(error.get('instructor') or '—'))}</td>"
            f"<td>{esc(str(error.get('discipline') or '—'))}</td>"
            f"<td>{esc(str(error.get('source') or '—'))}</td>"
            f"<td>{esc(str(error.get('error')))}</td>"
            "</tr>"
        )

    if payload.get("sync_running"):
        next_sync = "running now…"
    elif next_in is not None:
        next_sync = f"in {int(next_in)}s"
    else:
        next_sync = "—"

    return f"""<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>pelocore</title>
<style>
body {{ font-family: system-ui, sans-serif; margin: 2rem; color: #1c1c1c; }}
h1 {{ font-size: 1.4rem; }}
table {{ border-collapse: collapse; margin: 1rem 0; width: 100%; }}
th, td {{ border: 1px solid #ccc; padding: 4px 10px; text-align: left; font-size: 0.9rem; }}
th {{ background: #f2f2f2; }}
.badge {{ padding: 2px 8px; border-radius: 4px; color: #fff; font-size: 0.8rem; }}
.ok {{ background: #2e7d32; }} .warn {{ background: #ef6c00; }}
.bad {{ background: #c62828; }} .idle {{ background: #616161; }}
.muted {{ color: #666; }}
</style></head>
<body>
<h1>pelocore <span class="muted">v{esc(str(payload["version"]))}</span></h1>
<p>uptime {payload["uptime_seconds"]}s · next sync {esc(next_sync)} ·
state: {counts.get("synced", 0)} synced, {counts.get("failed", 0)} failed</p>
<h2>Last sync</h2>
<p><span class="badge {badge_class}">{esc(outcome)}</span>
{esc(_summary(last))}</p>
<h2>Recent cycles</h2>
<table><tr><th>started</th><th>outcome</th><th>trigger</th><th>fetched</th><th>uploaded</th><th>skipped</th><th>failed</th></tr>
{''.join(rows) or '<tr><td colspan="7">no cycles yet</td></tr>'}</table>
<h2>Errors (last cycle)</h2>
<table><tr><th>workout</th><th>title</th><th>instructor</th><th>discipline</th><th>source</th><th>error</th></tr>
{''.join(error_rows) or '<tr><td colspan="6">no errors</td></tr>'}</table>
</body></html>"""


def _badge_class(outcome: str) -> str:
    return {"ok": "ok", "partial": "warn", "failed": "bad"}.get(outcome, "idle")


def _summary(last: dict[str, Any] | None) -> str:
    if not last:
        return "no sync has run yet"
    return (
        f"fetched {last['fetched']}, uploaded {last['uploaded']}, "
        f"skipped {last['skipped']}, failed {last['failed']}"
    )


def render_metrics(
    *,
    cycles_total: int,
    uploaded_total: int,
    failed_total: int,
    last_success_timestamp: float | None,
    uptime_seconds: float,
    last_outcome: str | None,
) -> str:
    lines = [
        "# HELP pelocore_sync_cycles_total Number of sync cycles attempted.",
        "# TYPE pelocore_sync_cycles_total counter",
        f"pelocore_sync_cycles_total {cycles_total}",
        "# HELP pelocore_sync_workouts_uploaded_total Workouts uploaded to COROS.",
        "# TYPE pelocore_sync_workouts_uploaded_total counter",
        f"pelocore_sync_workouts_uploaded_total {uploaded_total}",
        "# HELP pelocore_sync_workouts_failed_total Workout uploads that failed.",
        "# TYPE pelocore_sync_workouts_failed_total counter",
        f"pelocore_sync_workouts_failed_total {failed_total}",
        "# HELP pelocore_last_sync_success_timestamp_seconds",
        "#       Unix time of last fully successful cycle.",
        "# TYPE pelocore_last_sync_success_timestamp_seconds gauge",
        "pelocore_last_sync_success_timestamp_seconds %s"
        % (last_success_timestamp if last_success_timestamp is not None else 0),
        "# HELP pelocore_uptime_seconds Server uptime in seconds.",
        "# TYPE pelocore_uptime_seconds gauge",
        f"pelocore_uptime_seconds {uptime_seconds:.0f}",
        "# HELP pelocore_last_cycle_outcome Outcome of the last cycle (1 for matching label).",
        "# TYPE pelocore_last_cycle_outcome gauge",
    ]
    for outcome in ("ok", "partial", "failed"):
        value = 1 if last_outcome == outcome else 0
        lines.append(f'pelocore_last_cycle_outcome{{outcome="{outcome}"}} {value}')
    return "\n".join(lines) + "\n"
