"""End-to-end ETL: ingest a known batch, run the async job, verify every record's outcome."""
import json

import pytest

pytestmark = pytest.mark.data


def test_etl_batch_outcomes(admin_api, db, unique_email):
    good = unique_email("good")
    batch = [
        {"first_name": "ana", "email": good, "phone": "(602) 555-0100", "company": "Acme", "source": "web"},
        {"first_name": "ben", "email": unique_email("free").replace("example-corp.com", "gmail.com"), "source": "ads"},
        {"first_name": "bad", "email": "no-at-sign.com", "source": "web"},
        {"first_name": "src", "email": unique_email(), "source": "fax"},
        {"first_name": "dup", "email": good.upper(), "source": "event"},
    ]
    ingest = admin_api.post("/api/v1/leads/ingest", json=batch)
    assert ingest.status_code == 202 and ingest.json()["received"] == 5
    batch_id = ingest.json()["batch_id"]

    run = admin_api.post("/api/v1/etl/run")
    assert run.status_code == 202
    job = admin_api.wait_for_job(run.json()["id"])
    assert job["status"] == "succeeded", job

    raw = admin_api.get("/api/v1/raw-leads", params={"batch_id": batch_id}).json()["items"]
    assert [r["status"] for r in raw] == ["processed", "processed", "rejected", "rejected", "duplicate"]
    assert "email is invalid" in raw[2]["error"]
    assert "unknown source 'fax'" in raw[3]["error"]

    # verify the transformed rows straight from the DB
    lead = db.execute("SELECT l.*, s.code FROM leads l JOIN sources s ON s.id = l.source_id WHERE l.raw_lead_id = ?",
                      (raw[0]["id"],)).fetchone()
    assert lead["email"] == good and lead["first_name"] == "Ana" and lead["phone"] == "+16025550100"
    assert lead["score"] == 20 + 20 + 10 + 20          # web + company + phone + business domain
    assert json.loads(raw[0]["payload"])["email"] == good   # raw payload kept untouched


def test_only_one_etl_job_at_a_time(admin_api):
    admin_api.put("/api/v1/admin/settings", json={"etl_delay_ms": 200})
    try:
        admin_api.post("/api/v1/etl/pull-feed", params={"count": 10, "seed": 7})
        first = admin_api.post("/api/v1/etl/run")
        second = admin_api.post("/api/v1/etl/run")
        assert first.status_code == 202
        assert second.status_code == 409
        admin_api.wait_for_job(first.json()["id"])
    finally:
        admin_api.put("/api/v1/admin/settings", json={"etl_delay_ms": 20})
