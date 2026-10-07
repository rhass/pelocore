"""Tests for pelocore.config."""

from __future__ import annotations

from pathlib import Path

import pytest

from pelocore.config import Settings


def test_defaults() -> None:
    s = Settings()
    assert s.backfill_days == 7
    assert s.coros_region == "en"
    assert s.state_path == Path("data/state.json")
    assert s.server_port == 8080
    assert s.coros_token_or_none is None


def test_prefixed_aliases(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PELOCORE_BACKFILL_DAYS", "3")
    monkeypatch.setenv("PELOCORE_SYNC_INTERVAL_SECONDS", "60")
    monkeypatch.setenv("PELOCORE_STATE_PATH", "/tmp/pelocore/state.json")
    monkeypatch.setenv("PELOCORE_STATUS_TOKEN", "tok")
    s = Settings()
    assert s.backfill_days == 3
    assert s.sync_interval_seconds == 60
    assert s.state_path == Path("/tmp/pelocore/state.json")
    assert s.status_token.get_secret_value() == "tok"


def test_credential_env_names(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PELOTON_USERNAME", "ryder")
    monkeypatch.setenv("PELOTON_PASSWORD", "pw")
    monkeypatch.setenv("COROS_EMAIL", "me@example.com")
    monkeypatch.setenv("COROS_PASSWORD", "cpw")
    monkeypatch.setenv("COROS_ACCESS_TOKEN", "ctok")
    monkeypatch.setenv("COROS_REGION", "eu")
    s = Settings()
    assert s.peloton_username == "ryder"
    assert s.peloton_password.get_secret_value() == "pw"
    assert s.coros_email == "me@example.com"
    assert s.coros_token_or_none == "ctok"
    assert s.coros_region == "eu"


def test_timezone_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PELOCORE_TIMEZONE_QUARTERS", "32")
    s = Settings()
    assert s.coros_timezone_quarters == 32
