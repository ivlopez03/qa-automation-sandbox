"""Password hashing (PBKDF2) and signed bearer tokens (HMAC), stdlib only."""
import base64
import hashlib
import hmac
import json
import os
import time
from collections import defaultdict, deque

from fastapi import Depends, HTTPException, Request

from . import config
from .db import get_conn

ITERATIONS = 60_000


def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or os.urandom(16).hex()
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), ITERATIONS)
    return f"pbkdf2${salt}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt, _ = stored.split("$")
    except ValueError:
        return False
    return hmac.compare_digest(hash_password(password, salt), stored)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _sign(body: str) -> str:
    return _b64(hmac.new(config.secret_key().encode(), body.encode(), hashlib.sha256).digest())


def create_token(user: dict) -> str:
    payload = {"sub": user["id"], "username": user["username"], "role": user["role"],
               "exp": int(time.time()) + config.token_ttl()}
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    return f"{body}.{_sign(body)}"


def decode_token(token: str) -> dict | None:
    try:
        body, sig = token.split(".")
        if not hmac.compare_digest(sig, _sign(body)):
            return None
        payload = json.loads(_unb64(body))
        return payload if payload["exp"] >= time.time() else None
    except Exception:
        return None


def _token_from_request(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return request.cookies.get("token")


def load_user(user_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, username, email, full_name, role, is_active, created_at FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
    return dict(row) if row else None


def optional_user(request: Request) -> dict | None:
    token = _token_from_request(request)
    payload = decode_token(token) if token else None
    if not payload:
        return None
    user = load_user(payload["sub"])
    return user if user and user["is_active"] else None


def get_current_user(request: Request) -> dict:
    token = _token_from_request(request)
    if not token:
        raise HTTPException(401, "Not authenticated", headers={"WWW-Authenticate": "Bearer"})
    payload = decode_token(token)
    if not payload:
        raise HTTPException(401, "Invalid or expired token", headers={"WWW-Authenticate": "Bearer"})
    user = load_user(payload["sub"])
    if not user:
        raise HTTPException(401, "User no longer exists")
    if not user["is_active"]:
        raise HTTPException(401, "User is deactivated")
    return user


def require_admin(user: dict = Depends(get_current_user)) -> dict:
    if user["role"] != "admin":
        raise HTTPException(403, "Admin role required")
    return user


class RateLimiter:
    """Sliding-window counter keyed by client IP (in-memory, single process).
    Used for failed logins: 10 failures per minute -> 429 until the window clears."""

    def __init__(self, limit: int, window_s: int):
        self.limit, self.window = limit, window_s
        self.hits: dict[str, deque] = defaultdict(deque)

    def retry_after(self, key: str) -> int | None:
        now = time.time()
        q = self.hits[key]
        while q and q[0] <= now - self.window:
            q.popleft()
        return int(self.window - (now - q[0])) + 1 if len(q) >= self.limit else None

    def hit(self, key: str) -> None:
        self.hits[key].append(time.time())

    def reset(self):
        self.hits.clear()


login_limiter = RateLimiter(limit=10, window_s=60)
