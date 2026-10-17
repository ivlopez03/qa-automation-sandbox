"""Data validation: API reports vs. the database vs. each other."""
import csv
import io

import pytest

pytestmark = pytest.mark.data


def test_summary_matches_database(admin_api, db):
    summary = admin_api.get("/api/v1/reports/summary").json()
    total = db.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
    by_status = dict(db.execute("SELECT status, COUNT(*) FROM leads GROUP BY status").fetchall())
    revenue = db.execute("SELECT ROUND(COALESCE(SUM(amount),0), 2) FROM deals").fetchone()[0]
    assert summary["total_leads"] == total
    assert {k: v for k, v in summary["by_status"].items() if v} == by_status
    assert summary["total_revenue"] == pytest.approx(revenue, abs=0.01)
    assert summary["conversion_rate"] == round(by_status.get("converted", 0) * 100 / total, 2)


def test_by_source_rolls_up_to_summary(admin_api):
    summary = admin_api.get("/api/v1/reports/summary").json()
    rows = admin_api.get("/api/v1/reports/by-source").json()
    assert sum(r["leads"] for r in rows) == summary["total_leads"]
    assert sum(r["converted"] for r in rows) == summary["by_status"]["converted"]
    assert sum(r["revenue"] for r in rows) == pytest.approx(summary["total_revenue"], abs=0.05)
    for r in rows:
        expected = round(r["converted"] * 100 / r["leads"], 2) if r["leads"] else 0.0
        assert r["conversion_rate"] == expected, f"conversion rate wrong for {r['source']}"


def test_user_report_is_scoped(alice_api, db):
    me = alice_api.get("/api/v1/me").json()
    summary = alice_api.get("/api/v1/reports/summary").json()
    mine = db.execute("SELECT COUNT(*) FROM leads WHERE owner_id = ?", (me["id"],)).fetchone()[0]
    assert summary["total_leads"] == mine


def test_csv_export_matches_json(admin_api):
    as_json = admin_api.get("/api/v1/reports/by-owner").json()
    as_csv = list(csv.DictReader(io.StringIO(admin_api.get("/api/v1/reports/by-owner", params={"format": "csv"}).text)))
    assert len(as_csv) == len(as_json)
    for j, c in zip(as_json, as_csv):
        assert c["username"] == j["username"] and int(c["leads"]) == j["leads"]


def test_reconciliation_checks_pass(admin_api):
    recon = admin_api.get("/api/v1/reports/etl-reconciliation").json()
    failed = [c for c in recon["checks"] if not c["passed"]]
    assert not failed, failed
