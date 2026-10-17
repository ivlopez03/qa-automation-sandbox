"""Leadflow Sandbox — run with:  uvicorn app.main:app --reload"""
import asyncio
import random
import secrets
import traceback
import uuid
from contextlib import asynccontextmanager
from urllib.parse import urlencode

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException
from markupsafe import Markup

from . import config, etl, reports, services
from .api import router
from .auth import optional_user
from .db import get_conn, rows
from .seed import ensure_seeded

CHAOS_EXEMPT = ("/api/v1/auth", "/api/v1/admin/settings", "/api/v1/admin/reset", "/api/v1/health")


@asynccontextmanager
async def lifespan(_app):
    ensure_seeded()
    yield


app = FastAPI(title="Leadflow Sandbox", version=config.VERSION, lifespan=lifespan,
              description="A deliberately realistic CRM-style app for practicing test automation.")
app.include_router(router)
app.mount("/static", StaticFiles(directory=str(config.ROOT / "app" / "static")), name="static")
templates = Jinja2Templates(directory=str(config.ROOT / "app" / "templates"))


# ------------------------------------------------------------------ middleware
@app.middleware("http")
async def chaos_and_request_id(request: Request, call_next):
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
    request.state.request_id = request_id
    path = request.url.path
    if path.startswith("/api/v1") and not path.startswith(CHAOS_EXEMPT) \
            and request.headers.get("x-chaos-disable") != "1":
        s = services.settings_now()
        if s["chaos_latency_ms"]:
            await asyncio.sleep(random.uniform(0, s["chaos_latency_ms"]) / 1000)
        if s["chaos_error_rate"] and random.random() < s["chaos_error_rate"]:
            return JSONResponse({"detail": "Service temporarily unavailable", "request_id": request_id},
                                status_code=503, headers={"Retry-After": "1", "X-Request-ID": request_id})
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response


# ------------------------------------------------------------------ errors
@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError):
    errors = [{"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]} for e in exc.errors()]
    return JSONResponse({"detail": errors}, status_code=422)


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    body = {"detail": "Internal server error", "request_id": getattr(request.state, "request_id", None)}
    if config.vulns_enabled():   # verbose errors leak internals
        body["trace"] = traceback.format_exc()
    return JSONResponse(body, status_code=500)


@app.exception_handler(StarletteHTTPException)
async def http_error(request: Request, exc: StarletteHTTPException):
    if request.url.path.startswith("/api/") or exc.status_code not in (401, 403, 404):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)
    if exc.status_code == 401:
        return RedirectResponse("/login?" + urlencode({"next": request.url.path}), status_code=303)
    return templates.TemplateResponse(request, "error.html",
                                      _ctx(request, optional_user(request), code=exc.status_code,
                                           message=exc.detail), status_code=exc.status_code)


# ------------------------------------------------------------------ UI helpers
def _ctx(request: Request, user, **extra) -> dict:
    s = services.settings_now()
    dynamic = bool(s["ui_dynamic_ids"])

    def rid(base: str) -> str:
        return f"{base}-{secrets.token_hex(3)}" if dynamic else base

    return {"request": request, "user": user, "rid": rid, "ui_delay_ms": s["ui_delay_ms"],
            "vulns": config.vulns_enabled(), "bugs": config.bugs_enabled(), "version": config.VERSION, **extra}


def _ui_user(request: Request, admin: bool = False) -> dict:
    user = optional_user(request)
    if not user:
        raise HTTPException(401)
    if admin and user["role"] != "admin":
        raise HTTPException(403, "This page is for admins only.")
    return user


def _page(request, name, user, **extra):
    return templates.TemplateResponse(request, name, _ctx(request, user, **extra))


# ------------------------------------------------------------------ UI routes
@app.get("/", include_in_schema=False)
def root(request: Request):
    return RedirectResponse("/dashboard" if optional_user(request) else "/login", status_code=303)


@app.get("/health", include_in_schema=False)
def health_plain():
    return {"status": "ok"}


@app.get("/login", response_class=HTMLResponse, include_in_schema=False)
def login_page(request: Request, next: str = "/dashboard"):
    if not next.startswith("/") or next.startswith("//"):
        next = "/dashboard"   # avoid open redirects
    return _page(request, "login.html", None, next=next)


@app.get("/logout", include_in_schema=False)
def logout_page():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie("token")
    return resp


@app.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
def dashboard(request: Request):
    user = _ui_user(request)
    with get_conn() as conn:
        recent = services.list_leads(conn, user, sort="-updated_at", page_size=6)["items"]
    return _page(request, "dashboard.html", user, recent=recent)


@app.get("/leads", response_class=HTMLResponse, include_in_schema=False)
def leads_page(request: Request, status: str = "", source: str = "", q: str = "", sort: str = "-created_at",
               page: int = 1):
    user = _ui_user(request)
    page = max(page, 1)
    with get_conn() as conn:
        try:
            result = services.list_leads(conn, user, status=status or None, source=source or None,
                                         q=q or None, sort=sort, page=page, page_size=15)
            error = None
        except services.ServiceError as e:
            result, error = {"items": [], "total": 0, "page": 1, "pages": 0}, e.detail
        sources = rows(conn.execute("SELECT code, name FROM sources ORDER BY code"))
    shown_q = Markup(q) if config.vulns_enabled() else q   # vuln mode: reflected XSS
    base = {"status": status, "source": source, "q": q, "sort": sort}
    return _page(request, "leads.html", user, result=result, sources=sources, statuses=services.STATUSES,
                 filters=base, shown_q=shown_q, error=error,
                 page_url=lambda p: "/leads?" + urlencode({**base, "page": p}))


@app.get("/leads/new", response_class=HTMLResponse, include_in_schema=False)
def new_lead_page(request: Request):
    user = _ui_user(request)
    with get_conn() as conn:
        sources = rows(conn.execute("SELECT code, name FROM sources ORDER BY code"))
    return _page(request, "lead_new.html", user, sources=sources)


@app.get("/leads/{lead_id}", response_class=HTMLResponse, include_in_schema=False)
def lead_detail(request: Request, lead_id: int):
    user = _ui_user(request)
    with get_conn() as conn:
        try:
            lead = services.get_lead_for(conn, lead_id, user)
        except services.ServiceError:
            raise HTTPException(404, "That lead doesn't exist or isn't assigned to you.")
        events = services.lead_events(conn, lead_id)
        deal = conn.execute("SELECT * FROM deals WHERE lead_id=?", (lead_id,)).fetchone()
        owners = rows(conn.execute("SELECT id, username FROM users WHERE role='user' AND is_active=1 ORDER BY id"))
    return _page(request, "lead_detail.html", user, lead=lead, events=events, deal=deal, owners=owners,
                 next_statuses=sorted(services.TRANSITIONS[lead["status"]]), steps=services.STATUSES[:4])


@app.get("/reports", response_class=HTMLResponse, include_in_schema=False)
def reports_page(request: Request):
    user = _ui_user(request)
    with get_conn() as conn:
        data = {"summary": reports.summary(conn, user), "by_source": reports.by_source(conn, user),
                "by_owner": reports.by_owner(conn, user)}
        if user["role"] == "admin":
            data["daily"] = reports.daily_ingestion(conn)[-14:]
            data["recon"] = reports.etl_reconciliation(conn)
    return _page(request, "reports.html", user, **data)


@app.get("/ingest", response_class=HTMLResponse, include_in_schema=False)
def ingest_page(request: Request):
    user = _ui_user(request, admin=True)
    with get_conn() as conn:
        jobs = rows(conn.execute("SELECT * FROM etl_jobs ORDER BY created_at DESC, rowid DESC LIMIT 8"))
        pending = conn.execute("SELECT COUNT(*) FROM raw_leads WHERE status='pending'").fetchone()[0]
        rejected = rows(conn.execute("SELECT id, batch_id, channel, payload, error FROM raw_leads "
                                     "WHERE status IN ('rejected','duplicate') ORDER BY id DESC LIMIT 8"))
    return _page(request, "ingest.html", user, jobs=jobs, pending=pending, rejected=rejected,
                 running=etl.job_running())


@app.get("/admin/users", response_class=HTMLResponse, include_in_schema=False)
def users_page(request: Request):
    user = _ui_user(request, admin=True)
    with get_conn() as conn:
        users = rows(conn.execute("SELECT id, username, email, full_name, role, is_active FROM users ORDER BY id"))
    return _page(request, "users.html", user, users=users)


@app.get("/admin/settings", response_class=HTMLResponse, include_in_schema=False)
def settings_page(request: Request):
    user = _ui_user(request, admin=True)
    with get_conn() as conn:
        s = services.typed_settings(conn)
    return _page(request, "settings.html", user, settings=s)
