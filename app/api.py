"""REST API, mounted at /api/v1. Interactive docs: /docs (Swagger) and /redoc."""
import csv
import io
import json
from typing import Literal

from fastapi import APIRouter, Depends, File, Header, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from . import config, etl, reports, seed, services
from .auth import create_token, get_current_user, hash_password, login_limiter, require_admin, verify_password
from .db import audit, get_conn, now_iso, rows

router = APIRouter(prefix="/api/v1")


def _svc(fn, *args, **kwargs):
    """Runs a service call and maps ServiceError -> HTTPException."""
    try:
        return fn(*args, **kwargs)
    except services.ServiceError as e:
        raise HTTPException(e.status_code, e.detail) from e


# ------------------------------------------------------------------ models
class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LoginIn(Strict):
    username: str
    password: str


class LeadIn(Strict):
    first_name: str
    last_name: str = ""
    email: str
    phone: str | None = None
    company: str | None = None
    source: str


class LeadReplace(LeadIn):
    owner_id: int | None


class LeadPatch(Strict):
    first_name: str | None = None
    last_name: str | None = None
    phone: str | None = None
    company: str | None = None
    owner_id: int | None = None


class TransitionIn(Strict):
    to_status: str
    amount: float | None = None
    note: str | None = Field(default=None, max_length=500)


class UserIn(Strict):
    username: str = Field(min_length=3, max_length=30, pattern=r"^[a-z0-9_]+$")
    email: str
    full_name: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=8)
    role: Literal["admin", "user"] = "user"


class UserPatch(Strict):
    full_name: str | None = Field(default=None, min_length=1, max_length=80)
    password: str | None = Field(default=None, min_length=8)
    role: Literal["admin", "user"] | None = None
    is_active: bool | None = None


class MePatch(Strict):
    full_name: str | None = Field(default=None, min_length=1, max_length=80)
    password: str | None = Field(default=None, min_length=8)


# ------------------------------------------------------------------ system
@router.get("/health", tags=["system"])
def health():
    with get_conn() as conn:
        conn.execute("SELECT 1")
    return {"status": "ok", "db": "ok", "version": config.VERSION, "etl_running": etl.job_running(),
            "vulns": config.vulns_enabled(), "bugs": config.bugs_enabled()}


# ------------------------------------------------------------------ auth
@router.post("/auth/login", tags=["auth"])
def login(body: LoginIn, request: Request, response: Response):
    ip = request.client.host if request.client else "unknown"
    limited = not config.vulns_enabled()   # vuln mode: unlimited brute force
    if limited:
        wait = login_limiter.retry_after(ip)
        if wait:
            raise HTTPException(429, "Too many failed login attempts", headers={"Retry-After": str(wait)})
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE username = ?", (body.username.strip().lower(),)).fetchone()
        if not row or not verify_password(body.password, row["password_hash"]):
            audit(conn, None, "login_failed", "user", body.username)
            conn.commit()   # keep the audit row even though we raise
            if limited:
                login_limiter.hit(ip)
            raise HTTPException(401, "Invalid username or password")
        if not row["is_active"]:
            raise HTTPException(403, "User is deactivated")
        audit(conn, row["id"], "login", "user", row["id"])
    user = dict(row)
    token = create_token(user)
    response.set_cookie("token", token, httponly=True, samesite="lax", max_age=config.token_ttl())
    return {"access_token": token, "token_type": "bearer", "expires_in": config.token_ttl(),
            "user": {k: user[k] for k in ("id", "username", "full_name", "role")}}


@router.post("/auth/logout", tags=["auth"], status_code=204)
def logout(response: Response):
    response.delete_cookie("token")


@router.get("/me", tags=["auth"])
def me(user=Depends(get_current_user)):
    return user


@router.patch("/me", tags=["auth"])
async def update_me(request: Request, user=Depends(get_current_user)):
    body = await request.json()
    if not config.vulns_enabled():
        try:
            body = MePatch.model_validate(body).model_dump(exclude_none=True)
        except ValidationError as e:   # unknown fields such as "role" -> 422
            raise HTTPException(422, [{"loc": list(err["loc"]), "msg": err["msg"], "type": err["type"]}
                                      for err in e.errors()])
    allowed = {"full_name", "password", "role", "is_active"} if config.vulns_enabled() else {"full_name", "password"}
    with get_conn() as conn:
        for key, value in body.items():
            if key not in allowed:
                continue
            if key == "password":
                conn.execute("UPDATE users SET password_hash=? WHERE id=?", (hash_password(value), user["id"]))
            else:   # vuln mode: mass assignment lets a user set their own role
                conn.execute(f"UPDATE users SET {key}=? WHERE id=?", (value, user["id"]))
        audit(conn, user["id"], "update", "user", user["id"], "self")
        row = conn.execute("SELECT id, username, email, full_name, role, is_active, created_at FROM users "
                           "WHERE id=?", (user["id"],)).fetchone()
    return dict(row)


# ------------------------------------------------------------------ leads
@router.get("/leads", tags=["leads"])
def list_leads(response: Response, user=Depends(get_current_user),
               status: str | None = None, source: str | None = None, owner_id: int | None = None,
               q: str | None = Query(None, max_length=100), min_score: int | None = Query(None, ge=0, le=100),
               sort: str = "-created_at", page: int = Query(1, ge=1),
               page_size: int = Query(20, ge=1, le=100)):
    with get_conn() as conn:
        result = _svc(services.list_leads, conn, user, status, source, owner_id, q, min_score, sort, page, page_size)
    response.headers["X-Total-Count"] = str(result["total"])
    return result


@router.get("/leads/search", tags=["leads"])
def search_leads(q: str = Query(..., min_length=1), user=Depends(get_current_user)):
    """Quick search by email/company. In vuln mode this is SQL-injectable and ignores ownership."""
    with get_conn() as conn:
        if config.vulns_enabled():
            sql = (f"SELECT id, first_name, last_name, email, company, status, owner_id FROM leads "
                   f"WHERE email LIKE '%{q}%' OR company LIKE '%{q}%' LIMIT 50")
            return {"items": rows(conn.execute(sql))}
        sql = ("SELECT id, first_name, last_name, email, company, status, owner_id FROM leads "
               "WHERE (email LIKE ? OR company LIKE ?)")
        params = [f"%{q}%", f"%{q}%"]
        if user["role"] != "admin":
            sql += " AND owner_id = ?"
            params.append(user["id"])
        return {"items": rows(conn.execute(sql + " LIMIT 50", params))}


@router.get("/leads/export.csv", tags=["leads"], response_class=PlainTextResponse)
def export_leads(user=Depends(require_admin)):
    """Full export of every lead. Intentionally heavy: a good performance-test target."""
    with get_conn() as conn:
        data = rows(conn.execute(services.LEAD_SELECT + " ORDER BY l.id"))
    return PlainTextResponse(reports.to_csv(data), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=leads.csv"})


@router.post("/leads", tags=["leads"], status_code=201)
def create_lead(body: LeadIn, response: Response, user=Depends(get_current_user)):
    with get_conn() as conn:
        lead = _svc(services.create_lead, conn, body.model_dump(), user)
    response.headers["Location"] = f"/api/v1/leads/{lead['id']}"
    return lead


@router.get("/leads/{lead_id}", tags=["leads"])
def get_lead(lead_id: int, user=Depends(get_current_user)):
    with get_conn() as conn:
        return _svc(services.get_lead_for, conn, lead_id, user)


@router.put("/leads/{lead_id}", tags=["leads"])
def replace_lead(lead_id: int, body: LeadReplace, admin=Depends(require_admin)):
    with get_conn() as conn:
        lead = _svc(services.get_lead_for, conn, lead_id, admin)
        return _svc(services.replace_lead, conn, lead, body.model_dump(), admin)


@router.patch("/leads/{lead_id}", tags=["leads"])
def patch_lead(lead_id: int, body: LeadPatch, user=Depends(get_current_user)):
    changes = body.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(422, "no fields to update")
    with get_conn() as conn:
        lead = _svc(services.get_lead_for, conn, lead_id, user)
        return _svc(services.patch_lead, conn, lead, changes, user)


@router.post("/leads/{lead_id}/transition", tags=["leads"])
def transition_lead(lead_id: int, body: TransitionIn, user=Depends(get_current_user)):
    with get_conn() as conn:
        lead = _svc(services.get_lead_for, conn, lead_id, user)
        return _svc(services.transition, conn, lead, body.to_status, user, body.amount, body.note)


@router.delete("/leads/{lead_id}", tags=["leads"], status_code=204)
def delete_lead(lead_id: int, admin=Depends(require_admin)):
    with get_conn() as conn:
        if not conn.execute("SELECT 1 FROM leads WHERE id=?", (lead_id,)).fetchone():
            raise HTTPException(404, "Lead not found")
        conn.execute("DELETE FROM leads WHERE id=?", (lead_id,))
        audit(conn, admin["id"], "delete", "lead", lead_id)
    return Response(status_code=204)


@router.get("/leads/{lead_id}/events", tags=["leads"])
def get_events(lead_id: int, user=Depends(get_current_user)):
    with get_conn() as conn:
        _svc(services.get_lead_for, conn, lead_id, user)
        return {"items": services.lead_events(conn, lead_id)}


# ------------------------------------------------------------------ ingestion / ETL
def _idempotent(key: str | None, produce):
    """Replays the stored response when the same Idempotency-Key is sent twice."""
    if key:
        with get_conn() as conn:
            hit = conn.execute("SELECT status_code, response FROM idempotency_keys WHERE key=?", (key,)).fetchone()
        if hit:
            return JSONResponse(json.loads(hit["response"]), status_code=hit["status_code"],
                                headers={"Idempotent-Replay": "true"})
    body = produce()
    if key:
        with get_conn() as conn:
            conn.execute("INSERT OR IGNORE INTO idempotency_keys (key, status_code, response, created_at) "
                         "VALUES (?, 202, ?, ?)", (key, json.dumps(body), now_iso()))
    return JSONResponse(body, status_code=202)


@router.post("/leads/ingest", tags=["etl"], status_code=202)
async def ingest_json(request: Request, user=Depends(require_admin),
                      idempotency_key: str | None = Header(None)):
    """Accepts one JSON object or an array (max 1000). Rows land in raw_leads as 'pending'."""
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(400, "body must be valid JSON")
    payloads = data if isinstance(data, list) else [data]
    if not payloads or len(payloads) > 1000:
        raise HTTPException(422, "send between 1 and 1000 records")

    def produce():
        with get_conn() as conn:
            res = etl.ingest(conn, payloads, "api")
            audit(conn, user["id"], "ingest", "raw_leads", res["batch_id"], f"{res['received']} records")
            return res
    return _idempotent(idempotency_key, produce)


@router.post("/leads/ingest/csv", tags=["etl"], status_code=202)
async def ingest_csv(file: UploadFile = File(...), user=Depends(require_admin)):
    if not (file.filename or "").lower().endswith(".csv"):
        raise HTTPException(415, "upload a .csv file")
    content = (await file.read()).decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(content))
    required = {"first_name", "email", "source"}
    if not reader.fieldnames or not required.issubset({f.strip() for f in reader.fieldnames}):
        raise HTTPException(422, f"CSV header must include {sorted(required)}")
    payloads = [{k.strip(): v for k, v in row.items() if k} for row in reader]
    if not payloads:
        raise HTTPException(422, "CSV has no data rows")
    with get_conn() as conn:
        res = etl.ingest(conn, payloads, "csv")
        audit(conn, user["id"], "ingest", "raw_leads", res["batch_id"], f"csv {file.filename}")
    return res


@router.get("/feed/leads", tags=["etl"])
def vendor_feed(count: int = Query(10, ge=1, le=500), seed_value: int | None = Query(None, alias="seed"),
                user=Depends(get_current_user)):
    """Simulated external vendor feed (dirty data included). Same seed -> same records."""
    return {"items": etl.generate_feed(count, seed=seed_value)}


@router.post("/etl/pull-feed", tags=["etl"], status_code=202)
def pull_feed(count: int = Query(25, ge=1, le=500), seed_value: int | None = Query(None, alias="seed"),
              user=Depends(require_admin)):
    with get_conn() as conn:
        res = etl.ingest(conn, etl.generate_feed(count, seed=seed_value), "feed")
        audit(conn, user["id"], "ingest", "raw_leads", res["batch_id"], f"feed {count}")
    return res


@router.post("/etl/run", tags=["etl"], status_code=202)
def run_etl(response: Response, user=Depends(require_admin)):
    job = _svc(etl.start_job, user["id"])
    response.headers["Location"] = f"/api/v1/etl/jobs/{job['id']}"
    return job


@router.get("/etl/jobs", tags=["etl"])
def list_jobs(limit: int = Query(20, ge=1, le=100), user=Depends(require_admin)):
    with get_conn() as conn:
        return {"items": rows(conn.execute("SELECT * FROM etl_jobs ORDER BY created_at DESC, rowid DESC LIMIT ?",
                                           (limit,)))}


@router.get("/etl/jobs/{job_id}", tags=["etl"])
def job_status(job_id: str, user=Depends(require_admin)):
    job = etl.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return job


@router.get("/raw-leads", tags=["etl"])
def raw_leads(status: Literal["pending", "processed", "rejected", "duplicate"] | None = None,
              batch_id: str | None = None, page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200),
              user=Depends(require_admin)):
    where, params = [], []
    if status:
        where.append("status = ?")
        params.append(status)
    if batch_id:
        where.append("batch_id = ?")
        params.append(batch_id)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    with get_conn() as conn:
        total = conn.execute("SELECT COUNT(*) FROM raw_leads" + clause, params).fetchone()[0]
        items = rows(conn.execute(f"SELECT * FROM raw_leads{clause} ORDER BY id LIMIT ? OFFSET ?",
                                  params + [page_size, (page - 1) * page_size]))
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# ------------------------------------------------------------------ reports
def _report(data, fmt: str, name: str):
    if fmt == "csv":
        records = data if isinstance(data, list) else [data]
        return PlainTextResponse(reports.to_csv(records), media_type="text/csv",
                                 headers={"Content-Disposition": f"attachment; filename={name}.csv"})
    return data


Fmt = Literal["json", "csv"]


@router.get("/reports/summary", tags=["reports"])
def report_summary(user=Depends(get_current_user)):
    with get_conn() as conn:
        return reports.summary(conn, user)


@router.get("/reports/by-source", tags=["reports"])
def report_by_source(format: Fmt = "json", user=Depends(get_current_user)):
    with get_conn() as conn:
        return _report(reports.by_source(conn, user), format, "by_source")


@router.get("/reports/by-owner", tags=["reports"])
def report_by_owner(format: Fmt = "json", user=Depends(get_current_user)):
    with get_conn() as conn:
        return _report(reports.by_owner(conn, user), format, "by_owner")


@router.get("/reports/daily-ingestion", tags=["reports"])
def report_daily(date_from: str | None = Query(None, alias="from"), date_to: str | None = Query(None, alias="to"),
                 format: Fmt = "json", user=Depends(require_admin)):
    with get_conn() as conn:
        return _report(_svc(reports.daily_ingestion, conn, date_from, date_to), format, "daily_ingestion")


@router.get("/reports/etl-reconciliation", tags=["reports"])
def report_reconciliation(user=Depends(require_admin)):
    with get_conn() as conn:
        return reports.etl_reconciliation(conn)


# ------------------------------------------------------------------ users (admin)
@router.get("/users", tags=["users"])
def list_users(admin=Depends(require_admin)):
    with get_conn() as conn:
        return {"items": rows(conn.execute(
            "SELECT id, username, email, full_name, role, is_active, created_at FROM users ORDER BY id"))}


@router.post("/users", tags=["users"], status_code=201)
def create_user(body: UserIn, admin=Depends(require_admin)):
    if not services.EMAIL_RE.match(body.email):
        raise HTTPException(422, "email is invalid")
    with get_conn() as conn:
        if conn.execute("SELECT 1 FROM users WHERE username=? OR email=?",
                        (body.username, body.email.lower())).fetchone():
            raise HTTPException(409, "username or email already taken")
        cur = conn.execute("INSERT INTO users (username, email, full_name, password_hash, role, is_active, created_at)"
                           " VALUES (?,?,?,?,?,1,?)", (body.username, body.email.lower(), body.full_name,
                                                       hash_password(body.password), body.role, now_iso()))
        audit(conn, admin["id"], "create", "user", cur.lastrowid)
        return dict(conn.execute("SELECT id, username, email, full_name, role, is_active, created_at FROM users "
                                 "WHERE id=?", (cur.lastrowid,)).fetchone())


@router.patch("/users/{user_id}", tags=["users"])
def update_user(user_id: int, body: UserPatch, admin=Depends(require_admin)):
    changes = body.model_dump(exclude_none=True)
    if user_id == admin["id"] and (changes.get("is_active") is False or changes.get("role") == "user"):
        raise HTTPException(409, "admins cannot deactivate or demote themselves")
    with get_conn() as conn:
        if not conn.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone():
            raise HTTPException(404, "User not found")
        for key, value in changes.items():
            if key == "password":
                conn.execute("UPDATE users SET password_hash=? WHERE id=?", (hash_password(value), user_id))
            else:
                conn.execute(f"UPDATE users SET {key}=? WHERE id=?", (int(value) if key == "is_active" else value,
                                                                       user_id))
        audit(conn, admin["id"], "update", "user", user_id, json.dumps({k: v for k, v in changes.items()
                                                                        if k != "password"}))
        return dict(conn.execute("SELECT id, username, email, full_name, role, is_active, created_at FROM users "
                                 "WHERE id=?", (user_id,)).fetchone())


# ------------------------------------------------------------------ admin
@router.get("/admin/settings", tags=["admin"])
def get_settings(admin=Depends(require_admin)):
    with get_conn() as conn:
        return services.typed_settings(conn)


@router.put("/admin/settings", tags=["admin"])
async def put_settings(request: Request, admin=Depends(require_admin)):
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(422, "body must be an object of setting -> value")
    with get_conn() as conn:
        return _svc(services.update_settings, conn, body, admin["id"])


@router.post("/admin/reset", tags=["admin"])
def reset_db(leads: int = Query(300, ge=0, le=20_000), seed_value: int = Query(42, alias="seed"),
             admin=Depends(require_admin)):
    """Drops and reseeds everything. Same seed -> identical data. Re-login afterwards."""
    if etl.job_running():
        raise HTTPException(409, "an ETL job is running; try again when it finishes")
    login_limiter.reset()
    return {"reset": True, "counts": seed.reset(leads, seed_value)}


@router.get("/admin/audit", tags=["admin"])
def audit_log(limit: int = Query(50, ge=1, le=500), action: str | None = None, admin=Depends(require_admin)):
    sql, params = "SELECT * FROM audit_log", []
    if action:
        sql += " WHERE action = ?"
        params.append(action)
    with get_conn() as conn:
        return {"items": rows(conn.execute(sql + " ORDER BY id DESC LIMIT ?", params + [limit]))}
