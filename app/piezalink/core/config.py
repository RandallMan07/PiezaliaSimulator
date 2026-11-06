"""Runtime configuration shared by all database callers."""

import os
from pathlib import Path
from dataclasses import dataclass
from math import isfinite
from urllib.parse import urlsplit

from dotenv import dotenv_values, find_dotenv


def get_database_url(dsn=None):
    """Resolve explicit DSN, process environment, then nearest .env from cwd."""
    if dsn:
        return dsn
    if value := os.environ.get("DATABASE_URL"):
        return value
    env_path = find_dotenv(usecwd=True)
    value = dotenv_values(env_path).get("DATABASE_URL") if env_path else None
    if not value:
        raise ValueError("Set DATABASE_URL in your environment or .env, or supply a DSN")
    return value


def get_accounts_path(path=None):
    """Locate bootstrap accounts explicitly, via configuration, or above cwd."""
    if path is not None:
        return Path(path).expanduser()
    if value := os.environ.get("ACCOUNTS_CSV"):
        return Path(value).expanduser()
    env_path = find_dotenv(usecwd=True)
    if env_path:
        value = dotenv_values(env_path).get("ACCOUNTS_CSV")
        if value:
            candidate = Path(value).expanduser()
            return candidate if candidate.is_absolute() else Path(env_path).parent / candidate
    current = Path.cwd()
    for directory in (current, *current.parents):
        candidate = directory / "accounts.csv"
        if candidate.is_file():
            return candidate
    raise ValueError("accounts.csv not found; set ACCOUNTS_CSV or pass accounts_path to init_db")


@dataclass(frozen=True)
class SyncSettings:
    api_url: str = "http://localhost:8080"
    timeout: float = 10.0
    attempts: int = 5
    retry_base: float = 1.0
    retry_cap: float = 30.0
    retry_budget: float = 120.0


def get_sync_settings():
    path = find_dotenv(usecwd=True)
    values = {**(dotenv_values(path) if path else {}), **os.environ}
    settings = SyncSettings(
        api_url=values.get("PORTAL_BASE_URL", "http://localhost:8080"),
        timeout=float(values.get("PORTAL_TIMEOUT_SECONDS", "10")),
        attempts=int(values.get("SYNC_RETRY_ATTEMPTS", "5")),
        retry_base=float(values.get("SYNC_RETRY_BASE_SECONDS", "1")),
        retry_cap=float(values.get("SYNC_RETRY_CAP_SECONDS", "30")),
        retry_budget=float(values.get("SYNC_RETRY_BUDGET_SECONDS", "120")),
    )
    url = urlsplit(settings.api_url)
    try:
        url.port
    except ValueError:
        raise ValueError("PORTAL_BASE_URL has an invalid port") from None
    if url.scheme not in ("http", "https") or not url.hostname or url.username or url.query or url.fragment:
        raise ValueError("PORTAL_BASE_URL must be an HTTP(S) URL without credentials, query, or fragment")
    if settings.attempts < 1 or settings.attempts > 20 or not isfinite(settings.timeout) or settings.timeout <= 0:
        raise ValueError("Use a positive portal timeout and 1 to 20 retry attempts")
    if any(not isfinite(v) or v < 0 for v in (settings.retry_base, settings.retry_cap, settings.retry_budget)):
        raise ValueError("Retry delays and budget must be finite nonnegative seconds")
    return settings
