"""Loop-mode HTTP server: scheduler thread + status page + metrics.

Endpoints (all stdlib ``http.server``):
- ``GET /`` HTML status page, ``GET /api/status`` JSON,
- ``GET /metrics`` Prometheus text format,
- ``GET /healthz`` / ``GET /readyz`` liveness/readiness probes,
- ``POST /sync`` manual cycle trigger (202 when accepted, 409 when busy).

When ``PELOCORE_STATUS_TOKEN`` is set, every endpoint except the probes
requires ``Authorization: Bearer <token>``.
"""

from __future__ import annotations

import hmac
import logging
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from pelocore import status
from pelocore.config import Settings
from pelocore.state import CycleReport, StateStore
from pelocore.sync import SyncEngine

logger = logging.getLogger(__name__)

OK = "ok"
READY = "ready\n"


@dataclass
class MetricsSnapshot:
    cycles_total: int = 0
    uploaded_total: int = 0
    failed_total: int = 0
    last_success_timestamp: float | None = None
    last_outcome: str | None = None


@dataclass
class MetricsCounters:
    """Thread-safe process-local counters backing the /metrics endpoint."""

    cycles_total: int = 0
    uploaded_total: int = 0
    failed_total: int = 0
    last_success_timestamp: float | None = None
    last_outcome: str | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def observe(self, report: CycleReport) -> None:
        with self._lock:
            self.cycles_total += 1
            self.uploaded_total += report.uploaded
            self.failed_total += report.failed
            self.last_outcome = report.outcome
            if report.outcome == OK:
                self.last_success_timestamp = time.time()

    def snapshot(self) -> MetricsSnapshot:
        with self._lock:
            return MetricsSnapshot(
                cycles_total=self.cycles_total,
                uploaded_total=self.uploaded_total,
                failed_total=self.failed_total,
                last_success_timestamp=self.last_success_timestamp,
                last_outcome=self.last_outcome,
            )


class BridgeServer:
    """Owns the scheduler thread and the HTTP server for loop mode."""

    def __init__(self, settings: Settings, engine: SyncEngine, store: StateStore, *, version: str):
        self.settings = settings
        self._engine = engine
        self._store = store
        self._version = version
        self._started_at = time.time()
        self._stop = threading.Event()
        self._cycle_lock = threading.Lock()
        self._metrics = MetricsCounters()
        self._next_sync_in: float | None = 0.0
        self._http: ThreadingHTTPServer | None = None
        self._threads: list[threading.Thread] = []

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        self._http = _make_http_server(self.settings.server_host, self.settings.server_port, self)
        self._http.daemon_threads = True
        worker = threading.Thread(target=self._worker_loop, name="sync-worker", daemon=True)
        http_thread = threading.Thread(target=self._http.serve_forever, name="http", daemon=True)
        self._threads = [worker, http_thread]
        worker.start()
        http_thread.start()
        logger.info(
            "listening on http://%s:%d (sync every %ds)",
            self.settings.server_host,
            self.settings.server_port,
            self.settings.sync_interval_seconds,
        )

    def stop(self) -> None:
        self._stop.set()
        if self._http is not None:
            self._http.shutdown()
            self._http.server_close()
        for thread in self._threads:
            thread.join(timeout=5)

    def wait(self) -> None:
        """Block until :meth:`stop` is called (or interrupted)."""
        try:
            while not self._stop.wait(timeout=1):
                pass
        except KeyboardInterrupt:
            self.stop()

    @property
    def bound_port(self) -> int:
        """The actual bound port (differs from settings when 0/ephemeral)."""
        if self._http is None:
            raise RuntimeError("server not started")
        return int(self._http.server_port)

    # -- scheduling ---------------------------------------------------------

    def _worker_loop(self) -> None:
        interval = self.settings.sync_interval_seconds
        while not self._stop.is_set():
            try:
                self.run_cycle(trigger="scheduled")
            except Exception:
                logger.exception("scheduled cycle crashed")
            if self._stop.is_set():
                return
            self._next_sync_in = float(interval)
            for _ in range(interval * 10):
                if self._stop.wait(0.1):
                    return
                self._next_sync_in = max(0.0, self._next_sync_in - 0.1)

    def run_cycle(self, *, trigger: str) -> CycleReport:
        with self._cycle_lock:
            report = self._engine.run_cycle(trigger=trigger)
            self._metrics.observe(report)
            return report

    def try_trigger_manual(self) -> bool:
        """Start a manual cycle in the background; False when one is running."""
        if not self._cycle_lock.acquire(blocking=False):
            return False
        self._cycle_lock.release()

        def _run() -> None:
            try:
                self.run_cycle(trigger="manual")
            except Exception:
                logger.exception("manual cycle crashed")

        threading.Thread(target=_run, name="manual-sync", daemon=True).start()
        return True

    def is_cycle_running(self) -> bool:
        if self._cycle_lock.acquire(blocking=False):
            self._cycle_lock.release()
            return False
        return True

    # -- request handling ---------------------------------------------------

    def handle_request(
        self, method: str, path: str, headers: dict[str, str]
    ) -> tuple[int, str, str]:
        """Route a request; returns (status, body, content_type)."""
        if path in ("/healthz", "/readyz"):
            return 200, READY, "text/plain; charset=utf-8"
        if not self._authorized(headers):
            return 401, "unauthorized\n", "text/plain; charset=utf-8"
        if method == "POST" and path == "/sync":
            if self.try_trigger_manual():
                return 202, '{"accepted": true}\n', "application/json"
            return 409, '{"error": "sync already running"}\n', "application/json"
        if method == "GET":
            if path == "/":
                payload = self._payload()
                return 200, status.render_html(payload), "text/html; charset=utf-8"
            if path == "/api/status":
                payload = self._payload()
                return 200, status.render_json(payload) + "\n", "application/json"
            if path == "/metrics":
                snap = self._metrics.snapshot()
                body = status.render_metrics(
                    cycles_total=snap.cycles_total,
                    uploaded_total=snap.uploaded_total,
                    failed_total=snap.failed_total,
                    last_success_timestamp=snap.last_success_timestamp,
                    uptime_seconds=time.time() - self._started_at,
                    last_outcome=snap.last_outcome,
                )
                return 200, body, "text/plain; version=0.0.4; charset=utf-8"
        return 404, "not found\n", "text/plain; charset=utf-8"

    def _authorized(self, headers: dict[str, str]) -> bool:
        token = self.settings.status_token.get_secret_value()
        if not token:
            return True
        provided = headers.get("Authorization", "")
        return hmac.compare_digest(provided, f"Bearer {token}")

    def _payload(self) -> dict[str, Any]:
        return status.build_payload(
            version=self._version,
            store=self._store,
            uptime_seconds=time.time() - self._started_at,
            next_sync_in_seconds=self._next_sync_in,
            sync_running=self.is_cycle_running(),
        )


def _as_float(value: object) -> float | None:
    return None if value is None else float(value)  # type: ignore[arg-type]


def _as_str(value: object) -> str | None:
    return None if value is None else str(value)


class _BridgeHTTPServer(ThreadingHTTPServer):
    bridge: BridgeServer


def _make_http_server(host: str, port: int, bridge: BridgeServer) -> ThreadingHTTPServer:
    httpd = _BridgeHTTPServer((host, port), _Handler)
    httpd.bridge = bridge
    return httpd


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        bridge: BridgeServer = self.server.bridge  # type: ignore[attr-defined]
        status_code, body, content_type = bridge.handle_request(method, path, dict(self.headers))
        self.send_response(status_code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body.encode("utf-8"))))
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def log_message(self, format: str, *args: object) -> None:
        logger.debug("http: " + format, *args)
