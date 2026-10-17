"""Deterministic seed data.  python -m app.seed --reset --leads 300"""
import argparse
import random
from datetime import datetime, timedelta, timezone

from . import etl, services
from .auth import hash_password
from .db import drop_all, get_conn, init_db, now_iso

USERS = [
    # username, password, role, full name, active
    ("admin", "Admin123!", "admin", "Ada Admin", 1),
    ("alice", "User123!", "user", "Alice Agent", 1),
    ("bob", "User123!", "user", "Bob Broker", 1),
    ("carol", "User123!", "user", "Carol Closer", 1),
    ("dave", "User123!", "user", "Dave Dormant", 0),
]
SOURCES = [
    # code, name, base_score, cost_per_lead
    ("web", "Website form", 20, 4.50),
    ("referral", "Client referral", 40, 0.00),
    ("ads", "Paid ads", 10, 12.75),
    ("partner", "Partner network", 30, 8.00),
    ("event", "Events & webinars", 15, 20.00),
]


def seed(conn, leads: int = 300, rng_seed: int = 42) -> dict:
    init_db(conn)
    ts = now_iso()
    for username, pw, role, name, active in USERS:
        conn.execute("INSERT INTO users (username, email, full_name, password_hash, role, is_active, created_at) "
                     "VALUES (?,?,?,?,?,?,?)",
                     (username, f"{username}@leadflow.test", name, hash_password(pw), role, active, ts))
    conn.executemany("INSERT INTO sources (code, name, base_score, cost_per_lead) VALUES (?,?,?,?)", SOURCES)
    for k, v in services.DEFAULT_SETTINGS.items():
        conn.execute("INSERT INTO settings (key, value) VALUES (?, ?)", (k, v))
    services._settings_cache["data"] = None

    if leads:
        rnd = random.Random(rng_seed)
        today = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0)
        payloads = etl.generate_feed(leads, seed=rng_seed)
        # spread arrivals over the last 30 days, in daily batches
        by_day: dict[int, list] = {}
        for p in payloads:
            by_day.setdefault(rnd.randint(0, 29), []).append(p)
        for days_ago in sorted(by_day, reverse=True):
            when = (today - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
            etl.ingest(conn, by_day[days_ago], "seed", received_at=when)
            etl.process_pending_sync(conn)

        actor_cache = {}
        for lead in conn.execute("SELECT id FROM leads ORDER BY id").fetchall():
            lead_row = services.get_lead(conn, lead["id"])
            owner_id = lead_row["owner_id"]
            actor = actor_cache.setdefault(owner_id, {"id": owner_id})
            roll = rnd.random()
            if roll < 0.30:
                path = []
            elif roll < 0.50:
                path = ["contacted"]
            elif roll < 0.65:
                path = ["contacted", "qualified"]
            elif roll < 0.82:
                path = ["contacted", "qualified", "converted"]
            elif roll < 0.91:
                path = ["lost"]
            else:
                path = ["contacted", "lost"]
            created = datetime.strptime(lead_row["created_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            for step, status in enumerate(path, start=1):
                at = min(created + timedelta(hours=6 * step), datetime.now(timezone.utc))
                amount = round(lead_row["estimated_value"] * rnd.uniform(0.5, 1.5), 2) \
                    if status == "converted" else None
                lead_row = services.transition(conn, lead_row, status, actor, amount=amount,
                                               at=at.strftime("%Y-%m-%dT%H:%M:%SZ"))
    counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in ("users", "raw_leads", "leads", "deals")}
    return counts


def reset(leads: int = 300, rng_seed: int = 42) -> dict:
    with get_conn() as conn:
        drop_all(conn)
        return seed(conn, leads, rng_seed)


def ensure_seeded() -> None:
    with get_conn() as conn:
        init_db(conn)
        if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
            seed(conn)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Seed the sandbox database")
    ap.add_argument("--reset", action="store_true", help="drop all tables first")
    ap.add_argument("--leads", type=int, default=300, help="number of raw leads to generate")
    ap.add_argument("--seed", type=int, default=42, help="RNG seed (same seed = same data)")
    args = ap.parse_args()
    if args.reset:
        print(reset(args.leads, args.seed))
    else:
        ensure_seeded()
        print("database ready")
