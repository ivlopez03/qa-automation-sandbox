"""Reporting layer. With SANDBOX_BUGS=1 some calculations are deliberately wrong (see docs/)."""
import csv
import io
import re

from . import config
from .services import STATUSES, ServiceError
from .db import rows

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _scope(user):
    return (" AND l.owner_id = ?", [user["id"]]) if user["role"] != "admin" else ("", [])


def summary(conn, user) -> dict:
    sc, params = _scope(user)
    by_status = {s: 0 for s in STATUSES}
    for r in conn.execute(f"SELECT l.status, COUNT(*) c FROM leads l WHERE 1=1{sc} GROUP BY l.status", params):
        by_status[r["status"]] = r["c"]
    total = sum(by_status.values())
    if config.bugs_enabled():
        total -= by_status["lost"]
    deals = conn.execute(
        f"SELECT COUNT(d.id) n, COALESCE(SUM(d.amount),0) rev FROM deals d JOIN leads l ON l.id=d.lead_id "
        f"WHERE 1=1{sc}", params).fetchone()
    pipeline = conn.execute(
        f"SELECT COALESCE(SUM(l.estimated_value),0) FROM leads l "
        f"WHERE l.status IN ('new','contacted','qualified'){sc}", params).fetchone()[0]
    unassigned = conn.execute("SELECT COUNT(*) FROM leads WHERE owner_id IS NULL").fetchone()[0] \
        if user["role"] == "admin" else None
    return {
        "total_leads": total,
        "by_status": by_status,
        "conversion_rate": round(by_status["converted"] * 100.0 / total, 2) if total else 0.0,
        "deals": deals["n"],
        "total_revenue": round(deals["rev"], 2),
        "avg_deal_size": round(deals["rev"] / deals["n"], 2) if deals["n"] else 0.0,
        "open_pipeline_value": round(pipeline, 2),
        "unassigned_leads": unassigned,
    }


def by_source(conn, user) -> list[dict]:
    sc, params = _scope(user)
    out = []
    for r in conn.execute(
        f"""SELECT s.code AS source, s.name, s.cost_per_lead, COUNT(l.id) AS leads,
                   SUM(CASE WHEN l.status='converted' THEN 1 ELSE 0 END) AS converted,
                   COALESCE(SUM(d.amount), 0) AS revenue
            FROM sources s
            LEFT JOIN leads l ON l.source_id = s.id{sc}
            LEFT JOIN deals d ON d.lead_id = l.id
            GROUP BY s.id ORDER BY s.code""", params):
        leads, converted = r["leads"], r["converted"] or 0
        cost = round(leads * r["cost_per_lead"], 2)
        if config.bugs_enabled():
            rate = float(converted * 100 // leads) if leads else 0.0
        else:
            rate = round(converted * 100.0 / leads, 2) if leads else 0.0
        out.append({
            "source": r["source"], "name": r["name"], "leads": leads, "converted": converted,
            "conversion_rate": rate, "revenue": round(r["revenue"], 2), "cost": cost,
            "roi_pct": round((r["revenue"] - cost) * 100.0 / cost, 2) if cost else None,
        })
    return out


def by_owner(conn, user) -> list[dict]:
    extra, params = ("", []) if user["role"] == "admin" else (" AND u.id = ?", [user["id"]])
    return [
        {**r, "revenue": round(r["revenue"], 2)}
        for r in rows(conn.execute(
            f"""SELECT u.id AS owner_id, u.username, u.is_active, COUNT(l.id) AS leads,
                       SUM(CASE WHEN l.status IN ('new','contacted','qualified') THEN 1 ELSE 0 END) AS open_leads,
                       SUM(CASE WHEN l.status='converted' THEN 1 ELSE 0 END) AS converted,
                       COALESCE(SUM(d.amount), 0) AS revenue
                FROM users u
                LEFT JOIN leads l ON l.owner_id = u.id
                LEFT JOIN deals d ON d.lead_id = l.id
                WHERE u.role = 'user'{extra}
                GROUP BY u.id ORDER BY u.id""", params))
    ]


def daily_ingestion(conn, date_from=None, date_to=None) -> list[dict]:
    where, params = [], []
    for value, op in ((date_from, ">="), (date_to, "<=")):
        if value:
            if not DATE_RE.match(value):
                raise ServiceError(422, "dates must use YYYY-MM-DD")
            where.append(f"substr(received_at, 1, 10) {op} ?")
            params.append(value)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    return rows(conn.execute(
        f"""SELECT substr(received_at, 1, 10) AS day, COUNT(*) AS received,
                   SUM(status='processed') AS processed, SUM(status='rejected') AS rejected,
                   SUM(status='duplicate') AS duplicates, SUM(status='pending') AS pending
            FROM raw_leads{clause} GROUP BY day ORDER BY day""", params))


def etl_reconciliation(conn) -> dict:
    counts = {s: 0 for s in ("pending", "processed", "rejected", "duplicate")}
    for r in conn.execute("SELECT status, COUNT(*) c FROM raw_leads GROUP BY status"):
        counts[r["status"]] = r["c"]
    raw_total = conn.execute("SELECT COUNT(*) FROM raw_leads").fetchone()[0]
    leads_from_raw = conn.execute("SELECT COUNT(*) FROM leads WHERE raw_lead_id IS NOT NULL").fetchone()[0]
    processed_linked = conn.execute(
        "SELECT COUNT(*) FROM raw_leads WHERE status='processed' AND lead_id IS NOT NULL").fetchone()[0]
    converted = conn.execute("SELECT COUNT(*) FROM leads WHERE status='converted'").fetchone()[0]
    deals = conn.execute("SELECT COUNT(*) FROM deals").fetchone()[0]
    checks = [
        {"name": "raw rows fully classified", "expected": raw_total, "actual": sum(counts.values())},
        {"name": "processed raw rows linked to a lead", "expected": counts["processed"], "actual": processed_linked},
        {"name": "leads from ETL match linked raw rows", "expected": processed_linked, "actual": leads_from_raw},
        {"name": "one deal per converted lead", "expected": converted, "actual": deals},
    ]
    for c in checks:
        c["passed"] = c["expected"] == c["actual"]
    return {"raw_total": raw_total, "raw_by_status": counts, "leads_from_raw": leads_from_raw,
            "checks": checks, "all_passed": all(c["passed"] for c in checks)}


def to_csv(records: list[dict]) -> str:
    if not records:
        return ""
    if config.bugs_enabled():
        records = records[:-1]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(records[0].keys()), lineterminator="\n")
    writer.writeheader()
    writer.writerows(records)
    return buf.getvalue()
