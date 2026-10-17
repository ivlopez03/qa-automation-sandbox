"""Business rules. These are documented in README ("Business rules") so tests can use them as an oracle."""
import json
import re
import time

from . import config
from .db import audit, get_conn, now_iso, rows

STATUSES = ["new", "contacted", "qualified", "converted", "lost"]
OPEN_STATUSES = ("new", "contacted", "qualified")
TRANSITIONS = {
    "new": {"contacted", "lost"},
    "contacted": {"qualified", "lost"},
    "qualified": {"converted", "lost"},
    "converted": set(),
    "lost": set(),
}
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}$")
FREE_DOMAINS = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "aol.com"}
SORTABLE = {"id", "created_at", "updated_at", "score", "email", "status", "estimated_value"}


class ServiceError(Exception):
    def __init__(self, status_code: int, detail):
        self.status_code, self.detail = status_code, detail


# ---------------------------------------------------------------- settings
DEFAULT_SETTINGS = {
    "chaos_latency_ms": "0",     # random 0..N ms added to every /api/v1 call
    "chaos_error_rate": "0",     # 0.0-1.0 probability of a 503 on /api/v1 calls
    "ui_delay_ms": "0",          # random 0..N ms before dashboard widgets / buttons become ready
    "ui_dynamic_ids": "0",       # 1 = element ids get random suffixes (use data-testid!)
    "etl_delay_ms": "20",        # sleep per record during ETL jobs
    "etl_failure_rate": "0",     # 0.0-1.0 probability per record that the job crashes mid-run
}
SETTING_RULES = {
    "chaos_latency_ms": (int, 0, 10_000),
    "chaos_error_rate": (float, 0.0, 1.0),
    "ui_delay_ms": (int, 0, 10_000),
    "ui_dynamic_ids": (int, 0, 1),
    "etl_delay_ms": (int, 0, 5_000),
    "etl_failure_rate": (float, 0.0, 1.0),
}
_settings_cache = {"at": 0.0, "data": None}


def get_settings(conn) -> dict:
    if _settings_cache["data"] is not None and time.time() - _settings_cache["at"] < 1.0:
        return _settings_cache["data"]
    data = dict(DEFAULT_SETTINGS)
    data.update({r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings")})
    _settings_cache.update(at=time.time(), data=data)
    return data


def typed_settings(conn) -> dict:
    raw = get_settings(conn)
    return {k: SETTING_RULES[k][0](raw[k]) for k in SETTING_RULES}


def settings_now() -> dict:
    """Typed settings, served from the 1s cache when possible (used on every request)."""
    data = _settings_cache["data"]
    if data is None or time.time() - _settings_cache["at"] >= 1.0:
        with get_conn() as conn:
            data = get_settings(conn)
    return {k: SETTING_RULES[k][0](data[k]) for k in SETTING_RULES}


def update_settings(conn, changes: dict, actor_id: int) -> dict:
    errors = []
    for key, value in changes.items():
        if key not in SETTING_RULES:
            errors.append(f"unknown setting '{key}'")
            continue
        cast, lo, hi = SETTING_RULES[key]
        try:
            v = cast(value)
        except (TypeError, ValueError):
            errors.append(f"{key} must be {cast.__name__}")
            continue
        if not lo <= v <= hi:
            errors.append(f"{key} must be between {lo} and {hi}")
    if errors:
        raise ServiceError(422, errors)
    for key, value in changes.items():
        conn.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, str(value)))
    audit(conn, actor_id, "update", "settings", None, json.dumps(changes))
    _settings_cache["data"] = None
    return typed_settings(conn)


# ---------------------------------------------------------------- transform
def normalize(payload) -> tuple[dict, list[str]]:
    """Transform + validate one raw payload. Returns (clean_record, errors)."""
    if not isinstance(payload, dict):
        return {}, ["payload must be a JSON object"]
    errors: list[str] = []

    def text(key):
        v = payload.get(key)
        return v.strip() if isinstance(v, str) else ("" if v is None else str(v).strip())

    first = text("first_name")
    if not first:
        errors.append("first_name is required")
    elif len(first) > 50:
        errors.append("first_name must be at most 50 characters")

    email = text("email").lower()
    if not email:
        errors.append("email is required")
    elif not EMAIL_RE.match(email):
        errors.append("email is invalid")

    phone = None
    if text("phone"):
        digits = re.sub(r"\D", "", text("phone"))
        if len(digits) == 11 and digits.startswith("1"):
            digits = digits[1:]
        if len(digits) != 10:
            errors.append("phone must contain 10 digits")
        else:
            phone = "+1" + digits

    source = text("source").lower()
    if not source:
        errors.append("source is required")

    clean = {
        "first_name": first.title(),
        "last_name": text("last_name").title(),
        "email": email,
        "phone": phone,
        "company": text("company") or None,
        "source": source,
    }
    return clean, errors


def compute_score(clean: dict, base_score: int) -> int:
    score = base_score
    if clean.get("company"):
        score += 20
    if clean.get("phone"):
        score += 10
    if clean["email"].split("@")[-1] not in FREE_DOMAINS:
        score += 20
    return min(score, 100)


def pick_owner(conn) -> int | None:
    """Least-loaded active 'user' (fewest open leads); ties go to the lowest id."""
    row = conn.execute(
        """SELECT u.id FROM users u
           LEFT JOIN leads l ON l.owner_id = u.id AND l.status IN ('new','contacted','qualified')
           WHERE u.role = 'user' AND u.is_active = 1
           GROUP BY u.id ORDER BY COUNT(l.id) ASC, u.id ASC LIMIT 1"""
    ).fetchone()
    return row["id"] if row else None


def get_source(conn, code: str):
    return conn.execute("SELECT * FROM sources WHERE code = ?", (code,)).fetchone()


def insert_lead(conn, clean: dict, source_row, actor_id=None, raw_lead_id=None,
                owner_id="auto", created_at=None) -> int:
    score = compute_score(clean, source_row["base_score"])
    if owner_id == "auto":
        owner_id = pick_owner(conn)
    ts = created_at or now_iso()
    cur = conn.execute(
        """INSERT INTO leads (raw_lead_id, first_name, last_name, email, phone, company, source_id,
                              score, estimated_value, status, owner_id, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?, 'new', ?,?,?)""",
        (raw_lead_id, clean["first_name"], clean["last_name"], clean["email"], clean["phone"],
         clean["company"], source_row["id"], score, float(score * 100), owner_id, ts, ts),
    )
    lead_id = cur.lastrowid
    conn.execute("INSERT INTO lead_events (lead_id, event_type, to_status, actor_id, note, created_at) "
                 "VALUES (?, 'created', 'new', ?, ?, ?)",
                 (lead_id, actor_id, f"owner_id={owner_id}", ts))
    return lead_id


def create_lead(conn, payload: dict, actor: dict) -> dict:
    clean, errors = normalize(payload)
    src = get_source(conn, clean.get("source", "")) if clean.get("source") else None
    if clean.get("source") and not src:
        errors.append(f"unknown source '{clean['source']}'")
    if errors:
        raise ServiceError(422, errors)
    if conn.execute("SELECT 1 FROM leads WHERE email = ?", (clean["email"],)).fetchone():
        raise ServiceError(409, f"a lead with email {clean['email']} already exists")
    # agents own what they create; admin-created leads are auto-assigned like imported ones
    owner = actor["id"] if actor["role"] == "user" else "auto"
    lead_id = insert_lead(conn, clean, src, actor_id=actor["id"], owner_id=owner)
    audit(conn, actor["id"], "create", "lead", lead_id)
    return get_lead(conn, lead_id)


# ---------------------------------------------------------------- read
LEAD_SELECT = """
SELECT l.id, l.first_name, l.last_name, l.email, l.phone, l.company, s.code AS source,
       l.score, l.estimated_value, l.status, l.owner_id, u.username AS owner,
       l.raw_lead_id, l.created_at, l.updated_at
FROM leads l JOIN sources s ON s.id = l.source_id LEFT JOIN users u ON u.id = l.owner_id
"""


def get_lead(conn, lead_id: int) -> dict | None:
    row = conn.execute(LEAD_SELECT + " WHERE l.id = ?", (lead_id,)).fetchone()
    return dict(row) if row else None


def get_lead_for(conn, lead_id: int, user: dict) -> dict:
    lead = get_lead(conn, lead_id)
    if not lead:
        raise ServiceError(404, "Lead not found")
    if user["role"] != "admin" and lead["owner_id"] != user["id"] and not config.vulns_enabled():
        # 404 rather than 403 so ids of other users' leads are not disclosed
        raise ServiceError(404, "Lead not found")
    return lead


def list_leads(conn, user: dict, status=None, source=None, owner_id=None, q=None,
               min_score=None, sort="-created_at", page=1, page_size=20) -> dict:
    where, params = [], []
    if user["role"] != "admin":
        where.append("l.owner_id = ?")
        params.append(user["id"])
    elif owner_id is not None:
        where.append("l.owner_id = ?")
        params.append(owner_id)
    if status:
        if status not in STATUSES:
            raise ServiceError(422, f"status must be one of {STATUSES}")
        where.append("l.status = ?")
        params.append(status)
    if source:
        where.append("s.code = ?")
        params.append(source.lower())
    if min_score is not None:
        where.append("l.score >= ?")
        params.append(min_score)
    if q:
        where.append("(l.email LIKE ? OR l.first_name LIKE ? OR l.last_name LIKE ? OR l.company LIKE ?)")
        params += [f"%{q}%"] * 4
    field, desc = sort.lstrip("-"), sort.startswith("-")
    if field not in SORTABLE:
        raise ServiceError(422, f"sort must be one of {sorted(SORTABLE)} (prefix '-' for descending)")
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    total = conn.execute(
        "SELECT COUNT(*) FROM leads l JOIN sources s ON s.id = l.source_id" + clause, params
    ).fetchone()[0]
    items = rows(conn.execute(
        LEAD_SELECT + clause + f" ORDER BY l.{field} {'DESC' if desc else 'ASC'}, l.id ASC LIMIT ? OFFSET ?",
        params + [page_size, (page - 1) * page_size],
    ))
    return {"items": items, "total": total, "page": page, "page_size": page_size,
            "pages": (total + page_size - 1) // page_size}


def lead_events(conn, lead_id: int) -> list[dict]:
    return rows(conn.execute(
        """SELECT e.id, e.event_type, e.from_status, e.to_status, e.note, e.created_at,
                  u.username AS actor
           FROM lead_events e LEFT JOIN users u ON u.id = e.actor_id
           WHERE e.lead_id = ? ORDER BY e.id""", (lead_id,)))


# ---------------------------------------------------------------- write
def transition(conn, lead: dict, to_status: str, actor: dict, amount=None, note=None,
               at=None) -> dict:
    if to_status not in STATUSES:
        raise ServiceError(422, f"to_status must be one of {STATUSES}")
    allowed = TRANSITIONS[lead["status"]]
    if to_status not in allowed:
        raise ServiceError(409, f"cannot move lead from '{lead['status']}' to '{to_status}'"
                                f" (allowed: {sorted(allowed) or 'none'})")
    ts = at or now_iso()
    if to_status == "converted":
        if amount is None or float(amount) <= 0:
            raise ServiceError(422, "amount must be greater than 0 when converting a lead")
        conn.execute("INSERT INTO deals (lead_id, owner_id, amount, closed_at) VALUES (?,?,?,?)",
                     (lead["id"], lead["owner_id"], round(float(amount), 2), ts))
    conn.execute("UPDATE leads SET status = ?, updated_at = ? WHERE id = ?", (to_status, ts, lead["id"]))
    conn.execute("INSERT INTO lead_events (lead_id, event_type, from_status, to_status, actor_id, note, "
                 "created_at) VALUES (?, 'status_change', ?, ?, ?, ?, ?)",
                 (lead["id"], lead["status"], to_status, actor["id"], note, ts))
    audit(conn, actor["id"], "transition", "lead", lead["id"], f"{lead['status']}->{to_status}")
    return get_lead(conn, lead["id"])


def patch_lead(conn, lead: dict, changes: dict, actor: dict) -> dict:
    if "owner_id" in changes and actor["role"] != "admin":
        raise ServiceError(403, "only admins can reassign leads")
    if "owner_id" in changes and changes["owner_id"] is not None:
        owner = conn.execute("SELECT role, is_active FROM users WHERE id = ?",
                             (changes["owner_id"],)).fetchone()
        if not owner or owner["role"] != "user" or not owner["is_active"]:
            raise ServiceError(422, "owner_id must reference an active user with role 'user'")
    merged = {**lead, **{k: v for k, v in changes.items() if k != "owner_id"}}
    clean, errors = normalize({**merged, "source": lead["source"]})
    if errors:
        raise ServiceError(422, errors)
    src = get_source(conn, lead["source"])
    score = compute_score(clean, src["base_score"])
    owner_id = changes.get("owner_id", lead["owner_id"])
    conn.execute(
        """UPDATE leads SET first_name=?, last_name=?, phone=?, company=?, score=?, estimated_value=?,
                            owner_id=?, updated_at=? WHERE id=?""",
        (clean["first_name"], clean["last_name"], clean["phone"], clean["company"], score,
         float(score * 100), owner_id, now_iso(), lead["id"]),
    )
    if owner_id != lead["owner_id"]:
        conn.execute("INSERT INTO lead_events (lead_id, event_type, actor_id, note, created_at) "
                     "VALUES (?, 'reassigned', ?, ?, ?)",
                     (lead["id"], actor["id"], f"{lead['owner_id']} -> {owner_id}", now_iso()))
    audit(conn, actor["id"], "update", "lead", lead["id"], json.dumps(changes))
    return get_lead(conn, lead["id"])


def replace_lead(conn, lead: dict, body: dict, actor: dict) -> dict:
    """PUT semantics: every field is replaced, including email and source."""
    clean, errors = normalize(body)
    src = get_source(conn, clean.get("source", "")) if clean.get("source") else None
    if clean.get("source") and not src:
        errors.append(f"unknown source '{clean['source']}'")
    if errors:
        raise ServiceError(422, errors)
    clash = conn.execute("SELECT id FROM leads WHERE email = ? AND id != ?",
                         (clean["email"], lead["id"])).fetchone()
    if clash:
        raise ServiceError(409, f"a lead with email {clean['email']} already exists")
    lead_copy = {**lead, "source": src["code"]}
    conn.execute("UPDATE leads SET email = ?, source_id = ? WHERE id = ?",
                 (clean["email"], src["id"], lead["id"]))
    return patch_lead(conn, {**lead_copy, "email": clean["email"]},
                      {k: body.get(k) for k in ("first_name", "last_name", "phone", "company", "owner_id")},
                      actor)
