"""Shared fixtures. Tests run against a live server: start it first with `make run`."""
import os
import sqlite3
import time
import uuid
from pathlib import Path

import httpx
import pytest

BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:8000")
DB_PATH = os.environ.get("SANDBOX_DB", str(Path(__file__).resolve().parent.parent / "data" / "sandbox.db"))
NO_CHAOS = {"X-Chaos-Disable": "1"}
ADMIN = ("admin", "Admin123!")
ALICE = ("alice", "User123!")
BOB = ("bob", "User123!")


class Api:
    """Thin wrapper over httpx with a bearer token and handy helpers."""

    def __init__(self, base_url: str, token: str | None = None):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self.client = httpx.Client(base_url=base_url, headers=headers, timeout=30)

    def __getattr__(self, name):          # api.get(...), api.post(...), ...
        return getattr(self.client, name)

    @classmethod
    def login(cls, base_url: str, username: str, password: str) -> "Api":
        r = httpx.post(f"{base_url}/api/v1/auth/login", json={"username": username, "password": password})
        assert r.status_code == 200, f"login failed for {username}: {r.status_code} {r.text}"
        return cls(base_url, r.json()["access_token"])

    def wait_for_job(self, job_id: str, timeout: float = 60) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            job = self.get(f"/api/v1/etl/jobs/{job_id}", headers=NO_CHAOS).json()
            if job["status"] in ("succeeded", "failed"):
                return job
            time.sleep(0.25)
        raise TimeoutError(f"job {job_id} did not finish in {timeout}s")


@pytest.fixture(scope="session")
def app_url() -> str:
    return BASE_URL


@pytest.fixture(scope="session")
def admin_api(app_url) -> Api:
    return Api.login(app_url, *ADMIN)


@pytest.fixture(scope="session")
def alice_api(app_url) -> Api:
    return Api.login(app_url, *ALICE)


@pytest.fixture(scope="session")
def bob_api(app_url) -> Api:
    return Api.login(app_url, *BOB)


@pytest.fixture(scope="session")
def anon_api(app_url) -> Api:
    return Api(app_url)


@pytest.fixture(scope="session", autouse=True)
def fresh_database(app_url):
    """Start every session from the same deterministic seed and normal conditions."""
    api = Api.login(app_url, *ADMIN)
    r = api.post("/api/v1/admin/reset", params={"leads": 300, "seed": 42})
    assert r.status_code == 200, r.text
    yield


@pytest.fixture
def db():
    """Read-only SQLite connection for data validation (needs the same SANDBOX_DB as the server)."""
    if not Path(DB_PATH).exists():
        pytest.skip(f"database not found at {DB_PATH}; set SANDBOX_DB")
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


@pytest.fixture
def unique_email():
    return lambda prefix="qa": f"{prefix}.{uuid.uuid4().hex[:10]}@example-corp.com"
