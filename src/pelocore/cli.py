"""Command-line interface.

Subcommands:
- ``pelocore sync``   run a single sync cycle and exit (cron/one-shot mode)
- ``pelocore run``    long-running loop with the status server (service mode)
- ``pelocore status`` print the persisted cycle history from the state file
- ``pelocore doctor`` verify Peloton and COROS credentials/connectivity
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
from collections.abc import Sequence

from pelocore import __version__
from pelocore.config import Settings
from pelocore.coros import CorosClient
from pelocore.peloton import PylotonClient
from pelocore.server import BridgeServer
from pelocore.state import StateStore
from pelocore.sync import SyncEngine

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_FAILED = 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pelocore", description="Sync Peloton workouts to COROS as FIT files."
    )
    parser.add_argument("--version", action="version", version=f"pelocore {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    sync_parser = subparsers.add_parser("sync", help="run a single sync cycle and exit")
    sync_parser.add_argument(
        "--once", action="store_true", help="explicit one-shot mode (the default)"
    )
    sync_parser.add_argument(
        "--workout-id",
        metavar="ID",
        help="import a single Peloton workout by id, bypassing the backfill window",
    )
    sync_parser.add_argument(
        "--force", action="store_true", help="upload even if state/COROS says it is synced"
    )

    subparsers.add_parser("run", help="run the loop server with the status page")
    subparsers.add_parser("status", help="print recent sync history from the state file")
    subparsers.add_parser("doctor", help="check Peloton and COROS credentials")

    args = parser.parse_args(argv)

    settings = Settings()
    logging.basicConfig(
        level=os.environ.get("PELOCORE_LOG_LEVEL", settings.log_level).upper(),
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    )

    if args.command == "sync":
        return _cmd_sync(settings, args.workout_id, force=args.force)
    if args.command == "run":
        return _cmd_run(settings)
    if args.command == "status":
        return _cmd_status(settings)
    if args.command == "doctor":
        return _cmd_doctor(settings)
    parser.error(f"unknown command {args.command!r}")  # pragma: no cover
    return 2


def build_engine(settings: Settings) -> tuple[SyncEngine, StateStore]:
    """Wire concrete clients; one shared StateStore for engine and server."""
    peloton = PylotonClient(
        username=settings.peloton_username,
        password=settings.peloton_password.get_secret_value(),
        refresh_token=settings.peloton_refresh_token.get_secret_value(),
        timeout=settings.http_timeout_seconds,
    )
    coros = CorosClient(
        region=settings.coros_region,
        email=settings.coros_email,
        password=settings.coros_password.get_secret_value(),
        access_token=settings.coros_token_or_none,
        timezone_quarters_override=settings.coros_timezone_quarters,
        timeout=settings.http_timeout_seconds,
    )
    store = StateStore(settings.state_path)
    return SyncEngine(peloton, coros, store, settings), store


def _cmd_sync(settings: Settings, workout_id: str | None = None, *, force: bool = False) -> int:
    engine, _store = build_engine(settings)
    if workout_id:
        report = engine.sync_workout_by_id(workout_id, force=force)
    else:
        report = engine.run_cycle(trigger="cli")
    print(report.summary_line())
    for error in report.errors:
        print(f"  error: {error.workout_id or 'cycle'}: {error.error}", file=sys.stderr)
    return EXIT_OK if report.outcome != "failed" else EXIT_FAILED


def _cmd_run(settings: Settings) -> int:
    engine, store = build_engine(settings)
    server = BridgeServer(settings, engine, store, version=__version__)

    def _shutdown(signum: int, _frame: object) -> None:
        logger.info("received signal %d, shutting down", signum)
        server.stop()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    try:
        server.start()
    except OSError as exc:
        logger.error(
            "could not start status server on %s:%s (%s). "
            "Free the port or set PELOCORE_SERVER_PORT.",
            settings.server_host,
            settings.server_port,
            exc.strerror or exc,
        )
        return EXIT_FAILED
    server.wait()
    return EXIT_OK


def _cmd_status(settings: Settings) -> int:
    store = StateStore(settings.state_path)
    last = store.last_cycle
    if last is None:
        print("no sync has run yet")
        return EXIT_OK
    print(f"last cycle: {last.summary_line()} (trigger={last.trigger})")
    for error in last.errors:
        label = error.workout_id or "cycle"
        print(f"  error: {label}: {error.error}", file=sys.stderr)
    print(f"state totals: {store.counts['synced']} synced, {store.counts['failed']} failed")
    return EXIT_OK


def _cmd_doctor(settings: Settings) -> int:
    ok = True
    print("pelocore doctor")
    try:
        peloton = PylotonClient(
            username=settings.peloton_username,
            password=settings.peloton_password.get_secret_value(),
            refresh_token=settings.peloton_refresh_token.get_secret_value(),
            timeout=settings.http_timeout_seconds,
        )
        who = peloton.whoami()
        print(
            f"  [ok] Peloton: user {who['username']} ({who['user_id']}),"
            f" {who['total_workouts']} workouts"
        )
    except Exception as exc:
        ok = False
        print(f"  [FAIL] Peloton: {exc}")

    try:
        coros = CorosClient(
            region=settings.coros_region,
            email=settings.coros_email,
            password=settings.coros_password.get_secret_value(),
            access_token=settings.coros_token_or_none,
            timezone_quarters_override=settings.coros_timezone_quarters,
            timeout=settings.http_timeout_seconds,
        )
        account = coros.account()
        print(
            f"  [ok] COROS ({settings.coros_region}): user {account.nickname or account.user_id}"
        )
    except Exception as exc:
        ok = False
        print(f"  [FAIL] COROS: {exc}")

    return EXIT_OK if ok else EXIT_FAILED


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
