"""Runtime configuration. Values are read from env vars on every call so tests can flip them."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION = "1.0.0"


def db_path() -> str:
    return os.environ.get("SANDBOX_DB", str(ROOT / "data" / "sandbox.db"))


def secret_key() -> str:
    return os.environ.get("SANDBOX_SECRET", "dev-secret-change-me")


def token_ttl() -> int:
    return int(os.environ.get("SANDBOX_TOKEN_TTL", "3600"))


def vulns_enabled() -> bool:
    """Intentionally vulnerable code paths (SQLi, IDOR, XSS...). Local practice only."""
    return os.environ.get("SANDBOX_VULNS", "0") == "1"


def bugs_enabled() -> bool:
    """Planted business-logic bugs for data-validation practice."""
    return os.environ.get("SANDBOX_BUGS", "0") == "1"
