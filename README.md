# Leadflow Sandbox

A small but realistic CRM-style web app built to **practice test automation**: UI, API, data validation, ETL, end-to-end, performance, security and flaky-test handling.

Stack: **FastAPI + SQLite + server-rendered HTML + vanilla JS**. No external services.

---

## 1. Run it

### Option A: GitHub Codespaces (recommended)
1. Push this folder to a GitHub repo.
2. **Code → Codespaces → Create codespace**. The devcontainer installs Python 3.12, test deps, Playwright Chromium, Java and JMeter.
3. `make run`, then open the forwarded port **8000**.

### Option B: Local
```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
make install          # or: pip install -r requirements-dev.txt && python -m playwright install chromium
make run              # http://localhost:8000
```

### Option C: Docker
```bash
docker build -t leadflow . && docker run -p 8000:8000 leadflow
```

> **About GitHub hosting:** GitHub Pages only serves static files, so it can't run this app. Use Codespaces to run it and GitHub Actions for CI (`.github/workflows/tests.yml`). For an always-on public URL, deploy the Dockerfile to Render or Fly.io, **with vuln mode off**.

The database is created and seeded on first start at `data/sandbox.db` (override with `SANDBOX_DB`).

### Accounts

| Username | Password | Role | Notes |
|---|---|---|---|
| admin | Admin123! | admin | sees and manages everything |
| alice, bob, carol | User123! | user | see only their own leads |
| dave | User123! | user | deactivated (login → 403) |

### Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `SANDBOX_DB` | `data/sandbox.db` | SQLite file path |
| `SANDBOX_SECRET` | `dev-secret-change-me` | token signing key |
| `SANDBOX_TOKEN_TTL` | `3600` | token lifetime (seconds) |
| `SANDBOX_VULNS` | `0` | `1` = deliberate security flaws |
| `SANDBOX_BUGS` | `0` | `1` = planted report bugs |

---

## 2. Architecture and data flow

```
 JSON / CSV / vendor feed
          │  POST /leads/ingest, /leads/ingest/csv, /etl/pull-feed
          ▼
   raw_leads (status=pending, payload stored untouched)        ← EXTRACT
          │  POST /etl/run  → async job (202 + poll /etl/jobs/{id})
          ▼
   normalize → validate → de-duplicate → score → assign         ← TRANSFORM
          │
          ├─► raw_leads.status = rejected   (error saved)
          ├─► raw_leads.status = duplicate
          └─► leads (status=new) + lead_events                  ← LOAD
                    │  POST /leads/{id}/transition
                    ▼
     new → contacted → qualified → converted (+ deals row)
       └──────┴────────────┴──────→ lost
                    │
                    ▼
        reports (JSON / CSV / UI)  +  etl-reconciliation checks
```

### Tables

| Table | What it holds |
|---|---|
| `users` | accounts, role `admin`/`user`, `is_active` |
| `sources` | lead sources with `base_score` and `cost_per_lead` |
| `raw_leads` | staging: every received record, its status and rejection reason |
| `leads` | clean, normalized leads; `raw_lead_id` links back to staging |
| `lead_events` | history: created, status changes, reassignments |
| `deals` | one row per converted lead (amount) |
| `etl_jobs` | processing runs with counters and errors |
| `settings` | test-condition switches (chaos) |
| `audit_log` | logins, failed logins, every write |
| `idempotency_keys` | stored responses for repeated `Idempotency-Key` requests |

Inspect with `sqlite3 data/sandbox.db` or the SQLite Viewer extension in Codespaces.

---

## 3. Business rules (your test oracle)

Write expected values from these rules, not from what the app returns.

**Normalization**
- `first_name`, `last_name`: trimmed, Title Case. `first_name` required, max 50 chars.
- `email`: trimmed, lowercased, must match `name@domain.tld`. Required. Unique across leads.
- `phone`: optional. Non-digits stripped; 11 digits starting with `1` → drop the `1`; must end with exactly 10 digits; stored as `+1XXXXXXXXXX`.
- `company`: trimmed; empty → `null`.
- `source`: lowercased; must exist in `sources`.

**Scoring**: `score = base_score(source) + 20 if company + 10 if phone + 20 if email domain is not free`, capped at 100.

| Source | base_score | cost_per_lead |
|---|---|---|
| web | 20 | 4.50 |
| referral | 40 | 0.00 |
| ads | 10 | 12.75 |
| partner | 30 | 8.00 |
| event | 15 | 20.00 |

Free domains: gmail.com, yahoo.com, hotmail.com, outlook.com, aol.com. `estimated_value = score × 100`.

**Assignment**
- Leads created by an agent (role `user`) are owned by that agent.
- Imported or admin-created leads go to the active `user` with the fewest open leads (new/contacted/qualified); ties go to the lowest user id. No active agents → unassigned.

**Status transitions**: `new→contacted|lost`, `contacted→qualified|lost`, `qualified→converted|lost`. `converted` and `lost` are final. Anything else → **409**. Converting requires `amount > 0` (**422** otherwise) and creates a deal rounded to 2 decimals.

**ETL outcomes** per raw row: invalid → `rejected` with all errors joined by `; `; email already in `leads` → `duplicate`; otherwise `processed` with `lead_id` set. Records are committed one at a time, so a crashed job leaves a partial load and the next run picks up the remaining `pending` rows. Only one job runs at a time (second request → 409).

**Reports**
- `conversion_rate = converted × 100 / total_leads`, rounded to 2 decimals (0.0 when no leads).
- `avg_deal_size = total_revenue / deals`.
- `open_pipeline_value = Σ estimated_value` of open leads.
- By source: `cost = leads × cost_per_lead`, `roi_pct = (revenue − cost) × 100 / cost` (null when cost is 0).
- Agents see only their own numbers; daily ingestion and reconciliation are admin-only.

**Access rules**: other users' leads → **404** (not 403). Admin-only endpoints for a user → **403**. No or bad token → **401**. Deactivated user login → **403**. 10 failed logins per minute per IP → **429** with `Retry-After`.

---

## 4. API reference

Auth: `Authorization: Bearer <token>` from `POST /api/v1/auth/login` (the login also sets a `token` cookie for the UI). Full interactive docs at **`/docs`** and `/redoc`.

| Method | Path | Who | Notes |
|---|---|---|---|
| GET | `/api/v1/health` | anyone | status, version, flags |
| POST | `/api/v1/auth/login` | anyone | `{username, password}` |
| POST | `/api/v1/auth/logout` | anyone | clears cookie, 204 |
| GET / PATCH | `/api/v1/me` | user | PATCH: `full_name`, `password` |
| GET | `/api/v1/leads` | user | `status, source, owner_id, q, min_score, sort, page, page_size`; `X-Total-Count` header |
| GET | `/api/v1/leads/search?q=` | user | quick search (SQL-injectable in vuln mode) |
| GET | `/api/v1/leads/export.csv` | admin | full export, heavy on purpose |
| POST | `/api/v1/leads` | user | 201 + `Location` header |
| GET | `/api/v1/leads/{id}` | user | |
| PUT | `/api/v1/leads/{id}` | admin | full replace incl. email, source, owner_id |
| PATCH | `/api/v1/leads/{id}` | user | `owner_id` admin-only |
| POST | `/api/v1/leads/{id}/transition` | user | `{to_status, amount?, note?}` |
| DELETE | `/api/v1/leads/{id}` | admin | 204 |
| GET | `/api/v1/leads/{id}/events` | user | history |
| POST | `/api/v1/leads/ingest` | admin | object or array (≤1000), 202, supports `Idempotency-Key` |
| POST | `/api/v1/leads/ingest/csv` | admin | multipart `file`, `.csv` only |
| GET | `/api/v1/feed/leads?count=&seed=` | user | simulated vendor feed (dirty data) |
| POST | `/api/v1/etl/pull-feed?count=&seed=` | admin | ingest from the feed |
| POST | `/api/v1/etl/run` | admin | 202 + job; 409 if one is running |
| GET | `/api/v1/etl/jobs`, `/api/v1/etl/jobs/{id}` | admin | poll job status |
| GET | `/api/v1/raw-leads?status=&batch_id=` | admin | staging rows |
| GET | `/api/v1/reports/summary` | user | |
| GET | `/api/v1/reports/by-source?format=json\|csv` | user | |
| GET | `/api/v1/reports/by-owner?format=json\|csv` | user | |
| GET | `/api/v1/reports/daily-ingestion?from=&to=&format=` | admin | dates `YYYY-MM-DD` |
| GET | `/api/v1/reports/etl-reconciliation` | admin | built-in consistency checks |
| GET / POST | `/api/v1/users` | admin | |
| PATCH | `/api/v1/users/{id}` | admin | role, is_active, full_name, password |
| GET / PUT | `/api/v1/admin/settings` | admin | test conditions |
| POST | `/api/v1/admin/reset?leads=&seed=` | admin | drop and reseed (same seed = same data) |
| GET | `/api/v1/admin/audit?action=&limit=` | admin | audit log |

Every response carries `X-Request-ID` (send your own to correlate). Unknown body fields → 422.

---

## 5. Making it behave like production

### Test conditions (Admin → Test conditions, or `PUT /api/v1/admin/settings`)

| Setting | Effect |
|---|---|
| `chaos_latency_ms` | each API call waits a random 0–N ms |
| `chaos_error_rate` | share of API calls that return 503 + `Retry-After` |
| `ui_delay_ms` | dashboard numbers load late; the "Update status" button enables late |
| `ui_dynamic_ids` | element `id`s get random suffixes (`data-testid` stays stable) |
| `etl_delay_ms` | processing time per record (makes jobs slow enough to poll) |
| `etl_failure_rate` | chance per record that a job crashes halfway |

Send `X-Chaos-Disable: 1` on setup/teardown calls to skip API chaos. Login, health, settings and reset are always exempt.

```bash
curl -X PUT localhost:8000/api/v1/admin/settings -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" -d '{"chaos_error_rate": 0.2, "ui_delay_ms": 3000}'
```

### Vulnerable mode: `make run-vuln` (local only)

| Flaw | Where |
|---|---|
| SQL injection | `GET /api/v1/leads/search?q=` |
| IDOR | `GET /api/v1/leads/{id}` ignores ownership |
| Reflected XSS | `/leads?q=<script>…` |
| Mass assignment | `PATCH /api/v1/me` accepts `role`, `is_active` |
| Brute force | no login rate limit |
| Information leak | 500 responses include stack traces |

Also present in every mode (worth reporting): no CSRF token on cookie-authenticated writes, no security headers (CSP, X-Frame-Options).

### Planted bugs: `make run-bugs`
Three report calculations go wrong. Find them with data tests before reading `docs/planted-bugs-answers.md`.

---

## 6. Tests

Tests run against a live server. Start it in one terminal, run tests in another:

```bash
make run
make test          # everything + reports/report.html
make test-api      # pytest -m api
make test-data     # pytest -m data   (reads the DB directly; keep SANDBOX_DB the same as the server)
make test-ui HEADED=1
```

Each session starts with `POST /admin/reset`, so data is identical every run. UI failures save Playwright traces in `test-results/` (`playwright show-trace <zip>`).

### Performance (JMeter)
```bash
make perf THREADS=50 DURATION=120     # HTML dashboard: perf/results/dashboard/index.html
```
The plan logs in once per thread, then lists leads, opens one, creates one and pulls two reports. Seed bigger data first: `POST /api/v1/admin/reset?leads=20000`.

---

## 7. Practice ideas

**UI (Playwright)**: page objects per screen; filters and pagination on Leads; form validation messages; role-based navigation; run the suite with `ui_dynamic_ids=1` and `ui_delay_ms=4000` and make it stay green.

**API**: schema checks on every endpoint; the full status matrix (200/201/202/204/401/403/404/409/422/429/503); pagination edge cases; idempotency replays; PUT vs PATCH semantics; token expiry (`SANDBOX_TOKEN_TTL=5`).

**Data validation**: each report vs. raw SQL; report vs. report (by-source totals = summary); CSV vs. JSON; agent-scoped numbers; the daily ingestion counts per day.

**ETL**: ingest a crafted batch with one record per rejection reason and assert each outcome; dirty CSV in `samples/leads_sample.csv`; crash recovery with `etl_failure_rate=0.1` (re-run until succeeded, then reconciliation must pass); idempotent re-runs; concurrency (409).

**End-to-end**: import via API → process → log in as the assigned agent in the UI → convert → check the deal in reports and in `deals`.

**Flaky tests**: turn on chaos, watch naive tests fail, then fix them with retries for 503, polling instead of sleeps, Playwright auto-waits and `data-testid` locators. Try `pytest-rerunfailures` and compare.

**Performance**: baseline latency at 300 vs 20,000 leads; find the slowest endpoint (`/leads/export.csv`, reports); SQLite write contention with many `POST /leads`; latency under `chaos_latency_ms`.

**Security**: in vuln mode, exploit then write a regression test that fails in vuln mode and passes in normal mode; run OWASP ZAP baseline against it.

---

## 8. Layout

```
app/
  main.py        app setup, middleware (chaos, request id), error handling, UI routes
  api.py         REST API (/api/v1)
  services.py    business rules: normalize, score, assign, transitions, settings
  etl.py         ingestion, vendor feed simulator, async jobs
  reports.py     report queries, CSV export
  db.py          schema and connections
  auth.py        password hashing, signed tokens, rate limiter
  seed.py        deterministic seed (python -m app.seed --reset --leads 300 --seed 42)
  templates/     Jinja pages (every element has a data-testid)
  static/        CSS and JS
tests/           api/, data/, ui/ examples + conftest fixtures
perf/            JMeter plan and installer
samples/         dirty CSV for import tests
docs/            planted-bug answer key
```
