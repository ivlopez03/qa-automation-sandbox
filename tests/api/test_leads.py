import pytest

pytestmark = pytest.mark.api


def new_lead(email, /, **overrides):
    body = {"first_name": "  jane ", "last_name": "DOE", "email": f"  {email.upper()} ",
            "phone": "1 (602) 555-0199", "company": "Acme Legal", "source": "partner"}
    body.update(overrides)
    return body


def test_create_lead_normalizes_scores_and_assigns(alice_api, unique_email):
    email = unique_email()
    r = alice_api.post("/api/v1/leads", json=new_lead(email))
    assert r.status_code == 201
    lead = r.json()
    assert r.headers["Location"] == f"/api/v1/leads/{lead['id']}"
    assert lead["first_name"] == "Jane" and lead["last_name"] == "Doe"
    assert lead["email"] == email.lower()
    assert lead["phone"] == "+16025550199"
    # partner base 30 + company 20 + phone 10 + business domain 20
    assert lead["score"] == 80
    assert lead["estimated_value"] == 8000.0
    assert lead["status"] == "new"
    assert lead["owner_id"] == alice_api.get("/api/v1/me").json()["id"]   # agents own what they create


@pytest.mark.parametrize("field,value,message", [
    ("email", "not-an-email", "email is invalid"),
    ("phone", "555-12", "phone must contain 10 digits"),
    ("source", "carrier-pigeon", "unknown source 'carrier-pigeon'"),
    ("first_name", "   ", "first_name is required"),
])
def test_create_lead_validation(admin_api, unique_email, field, value, message):
    r = admin_api.post("/api/v1/leads", json=new_lead(unique_email(), **{field: value}))
    assert r.status_code == 422
    assert message in r.json()["detail"]


def test_unknown_fields_are_rejected(admin_api, unique_email):
    r = admin_api.post("/api/v1/leads", json={**new_lead(unique_email()), "score": 100})
    assert r.status_code == 422


def test_duplicate_email_conflicts(admin_api, unique_email):
    email = unique_email()
    assert admin_api.post("/api/v1/leads", json=new_lead(email)).status_code == 201
    r = admin_api.post("/api/v1/leads", json=new_lead(email.upper()))
    assert r.status_code == 409


def test_full_lifecycle_to_converted(admin_api, unique_email):
    lead = admin_api.post("/api/v1/leads", json=new_lead(unique_email())).json()
    url = f"/api/v1/leads/{lead['id']}/transition"
    assert admin_api.post(url, json={"to_status": "contacted"}).json()["status"] == "contacted"
    assert admin_api.post(url, json={"to_status": "qualified"}).json()["status"] == "qualified"
    assert admin_api.post(url, json={"to_status": "converted"}).status_code == 422   # amount required
    r = admin_api.post(url, json={"to_status": "converted", "amount": 1234.567, "note": "signed"})
    assert r.status_code == 200 and r.json()["status"] == "converted"
    assert admin_api.post(url, json={"to_status": "lost"}).status_code == 409        # terminal
    events = admin_api.get(f"/api/v1/leads/{lead['id']}/events").json()["items"]
    assert [e["to_status"] for e in events] == ["new", "contacted", "qualified", "converted"]


def test_cannot_skip_pipeline_stages(admin_api, unique_email):
    lead = admin_api.post("/api/v1/leads", json=new_lead(unique_email())).json()
    r = admin_api.post(f"/api/v1/leads/{lead['id']}/transition", json={"to_status": "qualified"})
    assert r.status_code == 409


def test_user_sees_only_own_leads(alice_api, bob_api):
    alice_leads = alice_api.get("/api/v1/leads", params={"page_size": 100}).json()["items"]
    me = alice_api.get("/api/v1/me").json()
    assert alice_leads and all(l["owner_id"] == me["id"] for l in alice_leads)
    # bob asking for one of alice's leads gets 404 (not 403: ids are not disclosed)
    assert bob_api.get(f"/api/v1/leads/{alice_leads[0]['id']}").status_code == 404


def test_user_cannot_reassign(alice_api):
    lead = alice_api.get("/api/v1/leads", params={"page_size": 1}).json()["items"][0]
    assert alice_api.patch(f"/api/v1/leads/{lead['id']}", json={"owner_id": 3}).status_code == 403


@pytest.mark.parametrize("params", [{"page": 0}, {"page_size": 101}, {"sort": "password"}, {"status": "won"}])
def test_list_rejects_bad_query_params(admin_api, params):
    assert admin_api.get("/api/v1/leads", params=params).status_code == 422


def test_pagination_is_consistent(admin_api):
    first = admin_api.get("/api/v1/leads", params={"page_size": 10, "sort": "id"}).json()
    second = admin_api.get("/api/v1/leads", params={"page": 2, "page_size": 10, "sort": "id"}).json()
    ids = [l["id"] for l in first["items"] + second["items"]]
    assert ids == sorted(ids) and len(set(ids)) == 20
    assert first["total"] == second["total"]


def test_sort_by_score_desc(admin_api):
    scores = [l["score"] for l in admin_api.get("/api/v1/leads", params={"sort": "-score", "page_size": 50}).json()["items"]]
    assert scores == sorted(scores, reverse=True)


def test_admin_deletes_lead(admin_api, unique_email):
    lead = admin_api.post("/api/v1/leads", json=new_lead(unique_email())).json()
    assert admin_api.delete(f"/api/v1/leads/{lead['id']}").status_code == 204
    assert admin_api.get(f"/api/v1/leads/{lead['id']}").status_code == 404


def test_ingest_is_idempotent(admin_api, unique_email):
    body = [new_lead(unique_email())]
    headers = {"Idempotency-Key": unique_email("idem")}
    a = admin_api.post("/api/v1/leads/ingest", json=body, headers=headers)
    b = admin_api.post("/api/v1/leads/ingest", json=body, headers=headers)
    assert a.status_code == b.status_code == 202
    assert a.json()["batch_id"] == b.json()["batch_id"]
    assert b.headers.get("Idempotent-Replay") == "true"
