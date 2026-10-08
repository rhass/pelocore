"""Environment-driven configuration.

Credentials use bare variable names (``PELOTON_*`` / ``COROS_*``); application
settings may be prefixed with ``PELOCORE_``. All variables are read from the
process environment (or a local ``.env`` file for development).
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Peloton credentials (password login) or a pre-issued refresh token.
    peloton_username: str = ""
    peloton_password: SecretStr = SecretStr("")
    peloton_refresh_token: SecretStr = SecretStr("")

    # COROS credentials: password login and/or a browser session token
    # (the value of the CPL-coros-token cookie).
    coros_email: str = ""
    coros_password: SecretStr = SecretStr("")
    coros_access_token: SecretStr = SecretStr("")
    coros_region: Literal["en", "eu", "cn"] = "en"
    # Quarter-hours east of UTC used for COROS imports. Defaults to the host
    # timezone offset; override in containers that run as UTC.
    coros_timezone_quarters: int | None = Field(
        default=None,
        ge=-48,
        le=48,
        validation_alias=AliasChoices("PELOCORE_TIMEZONE_QUARTERS", "coros_timezone_quarters"),
    )

    # Sync behaviour.
    sport_remaps: str = Field(
        default="",
        validation_alias=AliasChoices("PELOCORE_SPORT_REMAPS", "sport_remaps"),
    )
    backfill_days: int = Field(
        default=7,
        ge=1,
        le=365,
        validation_alias=AliasChoices("PELOCORE_BACKFILL_DAYS", "backfill_days"),
    )
    sync_interval_seconds: int = Field(
        default=900,
        ge=30,
        validation_alias=AliasChoices("PELOCORE_SYNC_INTERVAL_SECONDS", "sync_interval_seconds"),
    )
    import_poll_seconds: float = Field(
        default=60.0,
        ge=0,
        validation_alias=AliasChoices("PELOCORE_IMPORT_POLL_SECONDS", "import_poll_seconds"),
    )
    # Re-upload workouts whose FIT bytes would differ from what was uploaded
    # (conversion changed). Drift detection is version-stamp based: zero
    # extra API calls in steady state.
    auto_upgrade: bool = Field(
        default=True,
        validation_alias=AliasChoices("PELOCORE_AUTO_UPGRADE", "auto_upgrade"),
    )
    state_path: Path = Field(
        default=Path("data/state.json"),
        validation_alias=AliasChoices("PELOCORE_STATE_PATH", "state_path"),
    )

    # Status server.
    server_host: str = Field(
        default="0.0.0.0",
        validation_alias=AliasChoices("PELOCORE_SERVER_HOST", "server_host"),
    )
    server_port: int = Field(
        default=8080,
        ge=1,
        le=65535,
        validation_alias=AliasChoices("PELOCORE_SERVER_PORT", "server_port"),
    )
    status_token: SecretStr = Field(
        default=SecretStr(""),
        validation_alias=AliasChoices("PELOCORE_STATUS_TOKEN", "status_token"),
    )

    # HTTP + logging.
    http_timeout_seconds: float = Field(
        default=30.0,
        gt=0,
        validation_alias=AliasChoices("PELOCORE_HTTP_TIMEOUT_SECONDS", "http_timeout_seconds"),
    )
    log_level: str = Field(
        default="INFO",
        validation_alias=AliasChoices("PELOCORE_LOG_LEVEL", "log_level"),
    )

    @property
    def coros_token_or_none(self) -> str | None:
        token = self.coros_access_token.get_secret_value()
        return token or None
