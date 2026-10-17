# Leadflow Sandbox: Architecture, Data Flow and What to Test

Oct 17, 2026 · @Ivan Lopez

## How it fits together

The UI has no logic of its own: every button calls the same `/api/v1` endpoints your tests call. A business-rule bug shows up in both API and UI tests; a failure in only one points at that page or endpoint.

| Layer | Lives in | Job |
| --- | --- | --- |
| Browser UI | `app/templates/`, `app/static/app.js` | pages; every action calls the REST API |
| FastAPI | `main.py`, `api.py`, `auth.py` | request id, chaos, auth and role checks, routes |
| Business rules | `services.py`, `etl.py`, `reports.py` | normalize, score, assign, transitions, reports |
| Storage | `data/sandbox.db` | SQLite, 10 tables |

&#91;embedded content: lead data flow · 5 stages and the tables each writes\]

A lead moves down the left column; the right column is where to look in SQLite after each step.

## Stage 1: Extract (ingestion)

Every record lands in `raw_leads` exactly as received, with status `pending` and a shared `batch_id`. Nothing is cleaned or validated yet, apart from checks on the request itself.

Three entry points, all admin-only:

| Endpoint | Input | Channel |
| --- | --- | --- |
| `POST /api/v1/leads/ingest` | one JSON object or an array (max 1000) | `api` |
| `POST /api/v1/leads/ingest/csv` | multipart `file`, `.csv` only | `csv` |
| `POST /api/v1/etl/pull-feed?count=&seed=` | simulated vendor feed with dirty data | `feed` |

**What to test**

- `received` equals records sent, and that many `pending` rows exist for the returned `batch_id`.
- The stored `payload` is exactly what you sent. Staging must never alter source data.
- Request errors: invalid JSON → 400, 0 or 1001 records → 422, a `.txt` upload → 415, a CSV without an `email` column → 422.
- The same `Idempotency-Key` sent twice returns the same `batch_id` with an `Idempotent-Replay: true` header and creates no new rows.

## Stage 2: Transform (the processing job)

`POST /api/v1/etl/run` returns **202** with a job id at once; the work runs in a background thread. Poll `GET /api/v1/etl/jobs/{id}` until `succeeded` or `failed`. A second run while one is active gets **409**.

For each pending row, in id order, the job:

1. Normalizes: trims, title-cases names, lowercases the email, reformats the phone to `+1XXXXXXXXXX`.
2. Validates: required fields, email format, 10-digit phone, known source.
3. Checks duplicates: is the email already in `leads`?
4. Scores the lead and assigns it to the agent with the fewest open leads.

Each row is committed on its own. If the job crashes on row 30, rows 1–29 stay loaded and the rest stay `pending` for the next run.

**What to test**

- One record per outcome: valid, bad email, bad phone, unknown source, missing name, duplicate. Assert each row's final status and error text.
- A normalization table, parametrized input → expected: `" Maria.Lopez@Initech.COM "` → `maria.lopez@initech.com`, `1 (602) 555-0142` → `+16025550142`.
- Scoring math calculated from the README rules, not from what the app returns.
- The second copy of an email in the same batch becomes `duplicate`.
- Assignment goes to the agent with the fewest open leads; ties to the lowest user id.
- Poll with a timeout instead of `sleep`; job counters add up to `total`.
- Crash recovery: set `etl_failure_rate=0.1`, run until `succeeded`, confirm nothing was lost or loaded twice.

## Stage 3: Load

A valid row becomes one `leads` row with status `new`, plus a `created` entry in `lead_events`. The raw row flips to `processed` with `lead_id` pointing at the new lead, and the lead's `created_at` equals the raw row's `received_at`.

| Raw outcome | `raw_leads` after the run | Rows written elsewhere |
| --- | --- | --- |
| processed | `lead_id` set, `error` empty | 1 `leads` row + 1 `lead_events` row |
| rejected | `lead_id` empty, `error` lists every failed rule | none |
| duplicate | `lead_id` empty, `error` names the existing email | none |

**What to test (mostly SQL)**

- Every `processed` raw row links to exactly one lead, and that lead's `raw_lead_id` points back.
- `rejected` and `duplicate` rows have no `lead_id` and do have an `error`.
- No two leads share an email.
- Every lead has exactly one `created` event.

```sql
SELECT COUNT(*) FROM raw_leads r
LEFT JOIN leads l ON l.id = r.lead_id AND l.raw_lead_id = r.id
WHERE r.status = 'processed' AND l.id IS NULL;   -- expect 0
```

## Stage 4: Lifecycle

`POST /api/v1/leads/{id}/transition` with `{to_status, amount?, note?}` moves a lead through a fixed state machine. Every move writes a `lead_events` row and an `audit_log` row.

&#91;embedded content: lead status transitions · 5 states\]

Converting without an amount returns 422; with one, it inserts a `deals` row rounded to 2 decimals.

| Action | Agent (role `user`) | Admin |
| --- | --- | --- |
| View or move own lead | allowed | allowed |
| View another agent's lead | 404 | allowed |
| Reassign (`PATCH owner_id`) | 403 | allowed |
| Delete | 403 | 204 |

**What to test**

- The full transition matrix: all 25 from → to pairs are either 200 or 409. Ideal for `pytest.mark.parametrize`.
- Converting: no amount → 422; with an amount → one `deals` row, rounded to 2 decimals.
- Nothing moves out of `converted` or `lost`.
- History: events appear in order with the right actor.
- Permissions as in the table above.
- UI: the amount field appears only for converted; the pipeline bar and history update after the move.

## Stage 5: Reports

Reports only read. Agents get numbers for their own leads; daily imports and reconciliation are admin-only. CSV and JSON come from the same query.

| Report | Endpoint | Reads from |
| --- | --- | --- |
| Summary | `/api/v1/reports/summary` | `leads`, `deals` |
| By source | `/api/v1/reports/by-source?format=json\|csv` | `sources`, `leads`, `deals` |
| By agent | `/api/v1/reports/by-owner?format=json\|csv` | `users`, `leads`, `deals` |
| Daily imports | `/api/v1/reports/daily-ingestion?from=&to=` | `raw_leads` |
| Reconciliation | `/api/v1/reports/etl-reconciliation` | `raw_leads`, `leads`, `deals` |

**What to test**

- Report vs database: recalculate every number with your own SQL.
- Report vs report: by-source totals and by-agent totals both add up to the summary.
- Format vs format: CSV rows match JSON rows exactly.
- Scope: Alice's summary equals `COUNT(*) WHERE owner_id = alice`.
- Reconciliation reports `all_passed: true` after every run, including crashed ones.

## Cross-cutting pieces

These run on every request, whatever the stage.

| Piece | What it does | What to test |
| --- | --- | --- |
| Auth | signed token or cookie, role check per route | 401 / 403 / 404 matrix, tampered token, deactivated user, 429 after 10 failed logins a minute |
| Chaos middleware | random latency and 503 on `/api/v1` | your retry logic; `X-Chaos-Disable: 1` bypasses it |
| Request id | `X-Request-ID` on every response | your own id is echoed back; ties a failure to server logs |
| Audit log | logins, failed logins, every write | each action leaves exactly one matching `audit_log` row |

## End-to-end test across every stage

One record followed from ingestion to the reports. It uses the `admin_api`, `db` and `unique_email` fixtures from `tests/conftest.py`.

```python
def test_lead_journey(admin_api, db, unique_email):
    email = unique_email()

    # 1. Extract
    batch = admin_api.post("/api/v1/leads/ingest", json=[
        {"first_name": " maria ", "email": email.upper(), "phone": "602-555-0142",
         "company": "Initech", "source": "web"}]).json()

    # 2. Transform
    job = admin_api.wait_for_job(admin_api.post("/api/v1/etl/run").json()["id"])
    assert job["status"] == "succeeded"

    # 3. Load: raw row is linked to a clean lead
    raw = db.execute("SELECT * FROM raw_leads WHERE batch_id = ?", (batch["batch_id"],)).fetchone()
    lead = db.execute("SELECT * FROM leads WHERE id = ?", (raw["lead_id"],)).fetchone()
    assert raw["status"] == "processed" and lead["raw_lead_id"] == raw["id"]
    assert (lead["first_name"], lead["email"], lead["phone"]) == ("Maria", email, "+16025550142")
    assert lead["score"] == 20 + 20 + 10 + 20   # web + company + phone + business domain

    # 4. Lifecycle
    before = admin_api.get("/api/v1/reports/summary").json()
    for step in ("contacted", "qualified"):
        admin_api.post(f"/api/v1/leads/{lead['id']}/transition", json={"to_status": step})
    admin_api.post(f"/api/v1/leads/{lead['id']}/transition",
                   json={"to_status": "converted", "amount": 4500})

    # 5. Reports reflect it exactly once
    after = admin_api.get("/api/v1/reports/summary").json()
    assert after["deals"] == before["deals"] + 1
    assert after["total_revenue"] == pytest.approx(before["total_revenue"] + 4500)
    assert admin_api.get("/api/v1/reports/etl-reconciliation").json()["all_passed"]
```

**Suggested build order**

1. One test per stage.
2. This end-to-end test.
3. Turn on chaos (latency, 503s, UI delay, random ids, ETL crashes) and make every test stay green.
