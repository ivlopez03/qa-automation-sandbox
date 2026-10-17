"""Extract (ingest raw payloads) -> Transform (normalize/validate/score) -> Load (leads table)."""
import json
import random
import threading
import time
import uuid

from . import services
from .db import get_conn, now_iso

_job_lock = threading.Lock()

FIRST = ["ana", "ben", "carla", "diego", "elena", "farid", "grace", "hector", "ines", "jamal",
         "kim", "luis", "maya", "noah", "olga", "pablo", "quinn", "rosa", "sam", "tara"]
LAST = ["garcia", "smith", "nguyen", "lopez", "khan", "brown", "silva", "muller", "rossi", "kim"]
COMPANIES = ["Acme Corp", "Globex", "Initech", "Umbrella LLC", "Stark Legal", "Wayne & Co", "", ""]
DOMAINS = ["gmail.com", "yahoo.com", "acme.com", "globex.io", "initech.com", "lawfirm.org"]
SOURCES = ["web", "referral", "ads", "partner", "event"]


def generate_feed(count: int, seed: int | None = None, dirty_rate: float = 0.15) -> list[dict]:
    """Simulates a vendor lead feed. ~dirty_rate of records are broken in a realistic way."""
    rnd = random.Random(seed)
    out: list[dict] = []
    for i in range(count):
        first, last = rnd.choice(FIRST), rnd.choice(LAST)
        tag = rnd.randint(1000, 999_999)
        rec = {
            "first_name": rnd.choice([first, first.upper(), f"  {first} "]),
            "last_name": last,
            "email": f"{first}.{last}{tag}@{rnd.choice(DOMAINS)}",
            "phone": rnd.choice(["", f"({rnd.randint(200, 999)}) {rnd.randint(200, 999)}-{rnd.randint(1000, 9999)}",
                                 f"1-{rnd.randint(200, 999)}-{rnd.randint(200, 999)}-{rnd.randint(1000, 9999)}"]),
            "company": rnd.choice(COMPANIES),
            "source": rnd.choice(SOURCES),
            "vendor_ref": f"V-{seed if seed is not None else 'x'}-{i}",
        }
        if rnd.random() < 0.3:
            rec["email"] = "  " + rec["email"].upper() + " "     # fixable by transform
        if rnd.random() < dirty_rate:
            problem = rnd.choice(["bad_email", "no_name", "bad_phone", "bad_source", "duplicate"])
            if problem == "bad_email":
                rec["email"] = rec["email"].strip().replace("@", "#")
            elif problem == "no_name":
                rec["first_name"] = ""
            elif problem == "bad_phone":
                rec["phone"] = "555-12"
            elif problem == "bad_source":
                rec["source"] = "carrier-pigeon"
            elif problem == "duplicate" and out:
                rec["email"] = rnd.choice(out)["email"]
        out.append(rec)
    return out


def ingest(conn, payloads: list, channel: str, received_at: str | None = None) -> dict:
    batch_id = "b_" + uuid.uuid4().hex[:12]
    ts = received_at or now_iso()
    ids = []
    for p in payloads:
        cur = conn.execute(
            "INSERT INTO raw_leads (batch_id, channel, payload, status, received_at) VALUES (?,?,?,'pending',?)",
            (batch_id, channel, json.dumps(p), ts))
        ids.append(cur.lastrowid)
    return {"batch_id": batch_id, "received": len(ids), "raw_ids": ids}


def process_raw(conn, raw) -> str:
    """Transforms and loads one raw row. Returns resulting raw status."""
    try:
        payload = json.loads(raw["payload"])
    except json.JSONDecodeError:
        payload = None
    clean, errors = services.normalize(payload)
    src = services.get_source(conn, clean["source"]) if clean.get("source") else None
    if clean.get("source") and not src:
        errors.append(f"unknown source '{clean['source']}'")
    ts = now_iso()
    if errors:
        conn.execute("UPDATE raw_leads SET status='rejected', error=?, processed_at=? WHERE id=?",
                     ("; ".join(errors), ts, raw["id"]))
        return "rejected"
    if conn.execute("SELECT 1 FROM leads WHERE email = ?", (clean["email"],)).fetchone():
        conn.execute("UPDATE raw_leads SET status='duplicate', error=?, processed_at=? WHERE id=?",
                     (f"email {clean['email']} already exists", ts, raw["id"]))
        return "duplicate"
    lead_id = services.insert_lead(conn, clean, src, raw_lead_id=raw["id"], created_at=raw["received_at"])
    conn.execute("UPDATE raw_leads SET status='processed', lead_id=?, processed_at=? WHERE id=?",
                 (lead_id, ts, raw["id"]))
    return "processed"


def process_pending_sync(conn) -> dict:
    """Used by seeding and tests: processes everything pending in the given connection."""
    stats = {"processed": 0, "rejected": 0, "duplicate": 0}
    for raw in conn.execute("SELECT * FROM raw_leads WHERE status='pending' ORDER BY id").fetchall():
        stats[process_raw(conn, raw)] += 1
    return stats


def start_job(actor_id: int) -> dict:
    """Queues an async ETL run. Only one job may run at a time."""
    if not _job_lock.acquire(blocking=False):
        raise services.ServiceError(409, "an ETL job is already running")
    job_id = "job_" + uuid.uuid4().hex[:10]
    try:
        with get_conn() as conn:
            total = conn.execute("SELECT COUNT(*) FROM raw_leads WHERE status='pending'").fetchone()[0]
            conn.execute("INSERT INTO etl_jobs (id, job_type, status, requested_by, total, created_at) "
                         "VALUES (?, 'process_pending', 'queued', ?, ?, ?)", (job_id, actor_id, total, now_iso()))
    except Exception:
        _job_lock.release()
        raise
    threading.Thread(target=_run_job, args=(job_id,), daemon=True).start()
    return get_job(job_id)


def _run_job(job_id: str) -> None:
    counters = {"processed": 0, "rejected": 0, "duplicate": 0}
    try:
        with get_conn() as conn:
            conn.execute("UPDATE etl_jobs SET status='running', started_at=? WHERE id=?", (now_iso(), job_id))
            settings = services.typed_settings(conn)
            pending = [r["id"] for r in conn.execute("SELECT id FROM raw_leads WHERE status='pending' ORDER BY id")]
        for raw_id in pending:
            if settings["etl_delay_ms"]:
                time.sleep(settings["etl_delay_ms"] / 1000)
            if random.random() < settings["etl_failure_rate"]:
                raise RuntimeError(f"simulated crash while processing raw_lead {raw_id}")
            with get_conn() as conn:   # one transaction per record -> partial loads are possible
                raw = conn.execute("SELECT * FROM raw_leads WHERE id=? AND status='pending'", (raw_id,)).fetchone()
                if raw:
                    counters[process_raw(conn, raw)] += 1
                conn.execute("UPDATE etl_jobs SET processed=?, rejected=?, duplicates=? WHERE id=?",
                             (counters["processed"], counters["rejected"], counters["duplicate"], job_id))
        status, error = "succeeded", None
    except Exception as exc:  # noqa: BLE001
        status, error = "failed", str(exc)
    finally:
        try:
            with get_conn() as conn:
                conn.execute("UPDATE etl_jobs SET status=?, error=?, finished_at=?, processed=?, rejected=?, "
                             "duplicates=? WHERE id=?",
                             (status, error, now_iso(), counters["processed"], counters["rejected"],
                              counters["duplicate"], job_id))
        finally:
            _job_lock.release()


def job_running() -> bool:
    return _job_lock.locked()


def get_job(job_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM etl_jobs WHERE id = ?", (job_id,)).fetchone()
    return dict(row) if row else None
