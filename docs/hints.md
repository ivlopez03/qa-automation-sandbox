# Hints

Couple useful hints for libraries and fixtures you can user for your tests/ project

### libraries

- os
- sqlite3
- time
- uuid
- from pathlib import Path
- httpx
- pytest

### Env Variables

BASE_URL = os.environ.get("BASE_URL", "<http://127.0.0.1:8000>")
DB_PATH = os.environ.get("SANDBOX_DB", str(Path(**file**).resolve().parent.parent / "data" / "sandbox.db"))

### Global Variables

NO_CHAOS = {"X-Chaos-Disable": "1"}
ADMIN = ("admin", "Admin123!")
ALICE = ("alice", "User123!")
BOB = ("bob", "User123!")

### Fixtures

Sets of functions or fixture you can add to you project .

1. initial function for setting up headers and api client - baseurl

```python
def __init__(self, base_url: str, token: str | None = None):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self.client = httpx.Client(base_url=base_url, headers=headers, timeout=30)

```

1. Function to wait for jobs to complete:

```python
def wait_for_job(self, job_id: str, timeout: float = 60) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            job = self.get(f"/api/v1/etl/jobs/{job_id}", headers=NO_CHAOS).json()
            if job["status"] in ("succeeded", "failed"):
                return job
            time.sleep(0.25)
        raise TimeoutError(f"job {job_id} did not finish in {timeout}s")
```

1. Fixture for base url

```python
@pytest.fixture(scope="session")
def app_url() -> str:
    return BASE_URL
```

1. Fixture to refresh db , this is an optional fixture for setup or teardown to clean up db

```python
@pytest.fixture(scope="session", autouse=True)
def fresh_database(app_url):
    """Start every session from the same deterministic seed and normal conditions."""
    api = Api.login(app_url, *ADMIN)
    r = api.post("/api/v1/admin/reset", params={"leads": 300, "seed": 42})
    assert r.status_code == 200, r.text
    yield

```

1. db fixture to create read-only connection to DB

```python
@pytest.fixture
def db():
    """Read-only SQLite connection for data validation (needs the same SANDBOX_DB as the server)."""
    if not Path(DB_PATH).exists():
        pytest.skip(f"database not found at {DB_PATH}; set SANDBOX_DB")
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()
```
