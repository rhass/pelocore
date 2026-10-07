"""Tests for pelocore.server (real HTTP over loopback with a stub engine)."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from typing import cast

import pytest
from pydantic import SecretStr

import pelocore.server as server_module
from pelocore.config import Settings
from pelocore.server import BridgeServer
from pelocore.state import CycleReport, StateStore
from pelocore.sync import SyncEngine
from tests.conftest import StubEngine


@pytest.fixture
def http(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patches the HTTP listener to bind an ephemeral port."""

    real = server_module._make_http_server

    def _ephemeral(host: str, port: int, bridge: BridgeServer) -> ThreadingHTTPServer:
        return real(host, 0, bridge)

    monkeypatch.setattr(server_module, "_make_http_server", _ephemeral)


def _make(settings: Settings, engine: StubEngine, store: StateStore) -> BridgeServer:
    return BridgeServer(settings, cast(SyncEngine, engine), store, version="test")


def _get(url: str, *, headers: dict[str, str] | None = None) -> tuple[int, str]:
    request = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode()


def _post(url: str, *, headers: dict[str, str] | None = None) -> tuple[int, str]:
    request = urllib.request.Request(url, headers=headers or {}, data=b"", method="POST")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode()


def test_status_endpoints(settings: Settings, store: StateStore, http: None) -> None:
    engine = StubEngine(
        report=CycleReport(started_at="t0", uploaded=1, outcome="ok"), store=store
    )
    bridge = _make(settings, engine, store)
    bridge.start()
    try:
        base = f"http://127.0.0.1:{bridge.bound_port}"
        code, html = _get(f"{base}/")
        assert code == 200
        assert "pelocore" in html

        code, body = _get(f"{base}/api/status")
        assert code == 200
        payload = json.loads(body)
        assert payload["version"] == "test"
        assert payload["last_cycle"]["uploaded"] == 1

        code, body = _get(f"{base}/metrics")
        assert code == 200
        assert "pelocore_sync_cycles_total 1" in body

        code, body = _get(f"{base}/healthz")
        assert code == 200
        code, _ = _get(f"{base}/readyz")
        assert code == 200

        code, _ = _get(f"{base}/nope")
        assert code == 404
    finally:
        bridge.stop()


def test_post_sync_triggers_manual_cycle(settings: Settings, store: StateStore, http: None) -> None:
    engine = StubEngine(report=CycleReport(started_at="t0"))
    bridge = _make(settings, engine, store)
    bridge.start()
    try:
        base = f"http://127.0.0.1:{bridge.bound_port}"
        # the scheduler also runs at startup; allow either 202 or 409 for the
        # first POST, but the response must be one of the two
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not engine.calls:
            time.sleep(0.05)
        assert engine.calls  # scheduled cycle ran
        code, body = _post(f"{base}/sync")
        assert code == 202
        assert json.loads(body)["accepted"] is True
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and "manual" not in engine.calls:
            time.sleep(0.05)
        assert "manual" in engine.calls
    finally:
        bridge.stop()


def test_post_sync_busy_returns_409(settings: Settings, store: StateStore, http: None) -> None:
    engine = StubEngine(report=CycleReport(started_at="t0"), block=True)
    bridge = _make(settings, engine, store)
    bridge.start()
    try:
        base = f"http://127.0.0.1:{bridge.bound_port}"
        code, _ = _post(f"{base}/sync")
        assert code == 409
        code, _ = _post(f"{base}/sync")
        assert code == 409
    finally:
        engine.release()
        bridge.stop()


def test_token_gate(settings: Settings, store: StateStore, http: None) -> None:
    tokenized = Settings(
        state_path=settings.state_path,
        sync_interval_seconds=3600,
        status_token=SecretStr("sekrit"),
    )
    engine = StubEngine(report=CycleReport(started_at="t0"))
    bridge = _make(tokenized, engine, store)
    bridge.start()
    try:
        base = f"http://127.0.0.1:{bridge.bound_port}"
        code, _ = _get(f"{base}/")
        assert code == 401
        code, _ = _get(f"{base}/api/status", headers={"Authorization": "Bearer wrong"})
        assert code == 401
        code, _ = _get(f"{base}/api/status", headers={"Authorization": "Bearer sekrit"})
        assert code == 200
        # probes stay open
        code, _ = _get(f"{base}/healthz")
        assert code == 200
        code, _ = _post(f"{base}/sync", headers={"Authorization": "Bearer sekrit"})
        assert code == 202
    finally:
        bridge.stop()
