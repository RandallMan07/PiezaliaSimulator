"""Runtime configuration shared by all database callers."""

import os

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
