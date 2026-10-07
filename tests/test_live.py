"""Live smoke tests against real services.

Opt-in only: run with ``PELOCORE_LIVE=1`` plus real credentials in the
environment (or a ``.env`` file). Never executed in CI by default.
"""

from __future__ import annotations

import os

import pytest

from pelocore.cli import build_engine
from pelocore.config import Settings
from pelocore.coros import CorosClient
from pelocore.peloton import PylotonClient

pytestmark = pytest.mark.live

requires_live = pytest.mark.skipif(
    os.environ.get("PELOCORE_LIVE") != "1", reason="PELOCORE_LIVE not set"
)


@requires_live
def test_doctor_checks(settings: Settings) -> None:
    peloton = PylotonClient(
        username=settings.peloton_username,
        password=settings.peloton_password.get_secret_value(),
        refresh_token=settings.peloton_refresh_token.get_secret_value(),
    )
    who = peloton.whoami()
    assert who["user_id"]
    coros = CorosClient(
        region=settings.coros_region,
        email=settings.coros_email,
        password=settings.coros_password.get_secret_value(),
        access_token=settings.coros_token_or_none,
    )
    account = coros.account()
    assert account.user_id


@requires_live
def test_full_cycle(settings: Settings) -> None:
    engine, _store = build_engine(settings)
    report = engine.run_cycle(trigger="live")
    print(report.summary_line())
    assert report.fetched >= 0
