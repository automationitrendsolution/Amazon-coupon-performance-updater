"""Settings from environment variables (the project's .env is loaded first).

``SPAPI_*``: the SP-API app and marketplace; ``DB_*`` / ``COUPON_TABLE``: the MySQL table;
``COUPON_*``: which campaigns are requested and how long to wait for Amazon. Error messages
name variables, never their values.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

from .exceptions import ConfigurationError

PROJECT_ROOT = Path(__file__).resolve().parent.parent

REPORT_TYPE = "GET_COUPON_PERFORMANCE_REPORT"

# Marketplace ID -> SP-API regional endpoint.
MARKETPLACE_ENDPOINTS = {
    # North America
    "ATVPDKIKX0DER": "https://sellingpartnerapi-na.amazon.com",  # US
    "A2EUQ1WTGCTBG2": "https://sellingpartnerapi-na.amazon.com",  # CA
    "A1AM78C64UM0Y8": "https://sellingpartnerapi-na.amazon.com",  # MX
    "A2Q3Y263D00KWC": "https://sellingpartnerapi-na.amazon.com",  # BR
    # Europe / Middle East / India
    "A1F83G8C2ARO7P": "https://sellingpartnerapi-eu.amazon.com",  # UK
    "A1PA6795UKMFR9": "https://sellingpartnerapi-eu.amazon.com",  # DE
    "A13V1IB3VIYZZH": "https://sellingpartnerapi-eu.amazon.com",  # FR
    "APJ6JRA9NG5V4": "https://sellingpartnerapi-eu.amazon.com",  # IT
    "A1RKKUPIHCS9HS": "https://sellingpartnerapi-eu.amazon.com",  # ES
    "A1805IZSGTT6HS": "https://sellingpartnerapi-eu.amazon.com",  # NL
    "A2NODRKZP88ZB9": "https://sellingpartnerapi-eu.amazon.com",  # SE
    "A1C3SOZRARQ6R3": "https://sellingpartnerapi-eu.amazon.com",  # PL
    "A33AVAJ2PDY3EV": "https://sellingpartnerapi-eu.amazon.com",  # TR
    "A2VIGQ35RCS4UG": "https://sellingpartnerapi-eu.amazon.com",  # AE
    "A17E79C6D8DWNP": "https://sellingpartnerapi-eu.amazon.com",  # SA
    "A21TJRUUN4KGV": "https://sellingpartnerapi-eu.amazon.com",  # IN
    # Far East
    "A1VC38T7YXB528": "https://sellingpartnerapi-fe.amazon.com",  # JP
    "A39IBJ37TRP1C6": "https://sellingpartnerapi-fe.amazon.com",  # AU
    "A19VAU5U5O7RUS": "https://sellingpartnerapi-fe.amazon.com",  # SG
}


def load_env(path: Path | None = None) -> None:
    """Load .env without overriding variables already set in the shell."""
    load_dotenv(path or PROJECT_ROOT / ".env", override=False)


def is_placeholder(value: str) -> bool:
    return "replace-me" in value or "xxxxxxxx" in value


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None or not value.strip() or is_placeholder(value):
        return default
    return value.strip()


def _db_env(suffix: str, default: str | None = None) -> str | None:
    """DB_<suffix>, else POWERBI_DB_<suffix> (the names the amazon_daily_data project uses)."""
    return _env(f"DB_{suffix}") or _env(f"POWERBI_DB_{suffix}", default)


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ConfigurationError(f"{name} must be an integer") from None


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    log_dir: Path
    log_level: str
    # SP-API / Login with Amazon
    lwa_client_id: str | None = None
    lwa_client_secret: str | None = field(default=None, repr=False)
    lwa_refresh_token: str | None = field(default=None, repr=False)
    marketplace_id: str | None = None
    sp_api_endpoint: str | None = None
    # MySQL
    db_host: str | None = None
    db_port: int = 3306
    db_user: str | None = None
    db_password: str | None = field(default=None, repr=False)
    db_name: str = "powerbidb"
    db_table: str = "api_coupon_data"
    # Behaviour
    lookback_days: int = 30
    lookahead_days: int = 10
    poll_interval_seconds: int = 60
    poll_timeout_seconds: int = 2 * 60 * 60
    max_retries: int = 6

    @property
    def report_dir(self) -> Path:
        """Raw report JSON as Amazon sent it, and the rows as CSV."""
        return self.data_dir / "reports"

    @property
    def state_dir(self) -> Path:
        return self.data_dir / "state"

    def ensure_directories(self) -> None:
        for path in (self.report_dir, self.state_dir, self.log_dir):
            path.mkdir(parents=True, exist_ok=True)

    def require_db(self) -> None:
        missing = [name for name, value in (("DB_HOST", self.db_host),
                                            ("DB_USER", self.db_user),
                                            ("DB_PASSWORD", self.db_password)) if not value]
        if missing:
            raise ConfigurationError("Missing MySQL settings: " + ", ".join(missing) + " (or POWERBI_DB_*)")

    def require_spapi(self) -> None:
        missing = [name for name, value in (("SPAPI_LWA_CLIENT_ID", self.lwa_client_id),
                                            ("SPAPI_LWA_CLIENT_SECRET", self.lwa_client_secret),
                                            ("SPAPI_LWA_REFRESH_TOKEN", self.lwa_refresh_token),
                                            ("SPAPI_MARKETPLACE_ID", self.marketplace_id)) if not value]
        if missing:
            raise ConfigurationError("Missing SP-API settings: " + ", ".join(missing))
        if not self.sp_api_endpoint:
            raise ConfigurationError(f"Unknown marketplace {self.marketplace_id!r}; set SPAPI_ENDPOINT explicitly")


def load_settings(env_file: Path | None = None) -> Settings:
    load_env(env_file)
    marketplace_id = _env("SPAPI_MARKETPLACE_ID")
    endpoint = _env("SPAPI_ENDPOINT") or MARKETPLACE_ENDPOINTS.get(marketplace_id or "")
    lookback, lookahead = _env_int("COUPON_LOOKBACK_DAYS", 30), _env_int("COUPON_LOOKAHEAD_DAYS", 10)
    if lookback < 0 or lookahead < 0:
        raise ConfigurationError("COUPON_LOOKBACK_DAYS and COUPON_LOOKAHEAD_DAYS must not be negative")
    return Settings(
        data_dir=Path(_env("COUPON_DATA_DIR", str(PROJECT_ROOT / "data"))),
        log_dir=Path(_env("COUPON_LOG_DIR", str(PROJECT_ROOT / "logs"))),
        log_level=_env("LOG_LEVEL", "INFO").upper(),
        lwa_client_id=_env("SPAPI_LWA_CLIENT_ID"),
        lwa_client_secret=_env("SPAPI_LWA_CLIENT_SECRET"),
        lwa_refresh_token=_env("SPAPI_LWA_REFRESH_TOKEN"),
        marketplace_id=marketplace_id,
        sp_api_endpoint=endpoint.rstrip("/") if endpoint else None,
        db_host=_db_env("HOST"),
        db_port=_env_int("DB_PORT", 3306) if _env("DB_PORT") else _env_int("POWERBI_DB_PORT", 3306),
        db_user=_db_env("USER"),
        db_password=_db_env("PASSWORD"),
        db_name=_db_env("NAME", "powerbidb"),
        db_table=_env("COUPON_TABLE", "api_coupon_data"),
        lookback_days=lookback,
        lookahead_days=lookahead,
        poll_interval_seconds=_env_int("COUPON_POLL_INTERVAL_SECONDS", 60),
        poll_timeout_seconds=_env_int("COUPON_POLL_TIMEOUT_SECONDS", 2 * 60 * 60),
        max_retries=_env_int("COUPON_MAX_RETRIES", 6),
    )
