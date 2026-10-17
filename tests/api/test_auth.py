import httpx
import pytest

pytestmark = pytest.mark.api


def test_login_returns_token_and_user(app_url):
    r = httpx.post(f"{app_url}/api/v1/auth/login", json={"username": "admin", "password": "Admin123!"})
    assert r.status_code == 200
    body = r.json()
    assert body["token_type"] == "bearer"
    assert body["user"] == {"id": 1, "username": "admin", "full_name": "Ada Admin", "role": "admin"}
    assert "token" in r.cookies


@pytest.mark.parametrize("username,password,status", [
    ("admin", "wrong-password", 401),
    ("nobody", "whatever123", 401),
    ("dave", "User123!", 403),          # deactivated account
])
def test_login_rejections(app_url, username, password, status):
    r = httpx.post(f"{app_url}/api/v1/auth/login", json={"username": username, "password": password})
    assert r.status_code == status


def test_login_validates_body(app_url):
    r = httpx.post(f"{app_url}/api/v1/auth/login", json={"username": "admin"})
    assert r.status_code == 422
    assert r.json()["detail"][0]["loc"] == ["body", "password"]


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer not-a-token"}])
def test_protected_endpoint_requires_valid_token(app_url, headers):
    assert httpx.get(f"{app_url}/api/v1/me", headers=headers).status_code == 401


def test_tampered_token_is_rejected(app_url, alice_api):
    token = alice_api.headers["Authorization"].split()[1]
    body, sig = token.split(".")
    forged = body[:-2] + ("AA" if body[-2:] != "AA" else "BB") + "." + sig
    assert httpx.get(f"{app_url}/api/v1/me", headers={"Authorization": f"Bearer {forged}"}).status_code == 401


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/v1/users"),
    ("GET", "/api/v1/admin/settings"),
    ("POST", "/api/v1/etl/run"),
    ("GET", "/api/v1/reports/etl-reconciliation"),
    ("DELETE", "/api/v1/leads/1"),
])
def test_user_role_cannot_call_admin_endpoints(alice_api, method, path):
    assert alice_api.request(method, path).status_code == 403


def test_me_patch_rejects_role_escalation(alice_api):
    r = alice_api.patch("/api/v1/me", json={"role": "admin"})
    assert r.status_code == 422      # unknown field; with SANDBOX_VULNS=1 this becomes a mass-assignment bug
    assert alice_api.get("/api/v1/me").json()["role"] == "user"


def test_failed_logins_are_rate_limited(app_url, admin_api):
    try:
        codes = [httpx.post(f"{app_url}/api/v1/auth/login",
                            json={"username": "alice", "password": "nope"}).status_code for _ in range(12)]
        assert 429 in codes
        assert codes.index(429) <= 10
    finally:
        admin_api.post("/api/v1/admin/reset")   # also clears the limiter
