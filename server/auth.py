"""
auth.py — users, households, password hashing, JWT, and the request dependency
that resolves a caller's household and scopes every downstream query.

Security model (replaces master's "tailnet membership = auth"):
- bcrypt password hashing.
- HS256 JWT carrying user_id + household_id, signed with config.effective_secret().
- Every /api/* except /api/auth/* depends on `current_ctx` → 401 without a valid token.
"""
import logging
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from fastapi import Depends, Header, HTTPException

import config

log = logging.getLogger("plantcart.auth")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def hash_password(pw: str) -> str:
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()


def verify_password(pw: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(pw.encode(), hashed.encode())
    except (ValueError, TypeError):
        return False


def make_token(user_id: str, household_id: str) -> str:
    payload = {
        "sub": user_id,
        "hh": household_id,
        "iat": datetime.now(timezone.utc),
        "exp": datetime.now(timezone.utc) + timedelta(days=config.TOKEN_TTL_DAYS),
    }
    return jwt.encode(payload, config.effective_secret(), algorithm="HS256")


def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, config.effective_secret(), algorithms=["HS256"])
    except jwt.PyJWTError as e:
        raise HTTPException(401, f"invalid token: {e}")


def new_invite_code() -> str:
    # 6 chars, unambiguous alphabet (no O/0/I/1) — easy to read aloud to a spouse
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(6))


# ---- user / household creation (called by the register/join endpoints) ----

def create_user(conn, email: str, password: str, display_name: str) -> str:
    email = email.strip().lower()
    if conn.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
        raise HTTPException(409, "email already registered")
    uid = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO users(id, email, pw_hash, display_name, created_at) VALUES(?,?,?,?,?)",
        (uid, email, hash_password(password), display_name.strip() or email.split("@")[0], now_iso()),
    )
    return uid


def create_household(conn, name: str, owner_id: str) -> str:
    hid = str(uuid.uuid4())
    code = new_invite_code()
    while conn.execute("SELECT 1 FROM households WHERE invite_code=?", (code,)).fetchone():
        code = new_invite_code()
    conn.execute(
        "INSERT INTO households(id, name, invite_code, revision, created_at) VALUES(?,?,?,0,?)",
        (hid, name, code, now_iso()),
    )
    conn.execute(
        "INSERT INTO household_members(household_id, user_id, role, joined_at) VALUES(?,?,?,?)",
        (hid, owner_id, "owner", now_iso()),
    )
    return hid


def join_household(conn, invite_code: str, user_id: str) -> str:
    row = conn.execute(
        "SELECT id FROM households WHERE invite_code=?", (invite_code.strip().upper(),)
    ).fetchone()
    if not row:
        raise HTTPException(404, "invalid invite code")
    hid = row["id"]
    conn.execute(
        "INSERT OR IGNORE INTO household_members(household_id, user_id, role, joined_at) "
        "VALUES(?,?,?,?)",
        (hid, user_id, "member", now_iso()),
    )
    return hid


def household_summary(conn, hid: str) -> dict:
    hh = conn.execute("SELECT id, name, invite_code FROM households WHERE id=?", (hid,)).fetchone()
    members = [
        {"user_id": r["user_id"], "display_name": r["display_name"], "role": r["role"]}
        for r in conn.execute(
            """SELECT m.user_id, m.role, u.display_name
               FROM household_members m JOIN users u ON u.id = m.user_id
               WHERE m.household_id=? ORDER BY m.joined_at""",
            (hid,),
        )
    ]
    return {"id": hh["id"], "name": hh["name"], "invite_code": hh["invite_code"], "members": members}


# ---- the request dependency: token -> (user_id, household_id) ----

class Ctx:
    __slots__ = ("user_id", "household_id")

    def __init__(self, user_id: str, household_id: str):
        self.user_id = user_id
        self.household_id = household_id


def _resolve(conn, token: str) -> Ctx:
    data = decode_token(token)
    uid, hid = data.get("sub"), data.get("hh")
    if not uid or not hid:
        raise HTTPException(401, "malformed token")
    # A 30-day JWT outlives account deletion — the signature alone is not enough.
    # Re-check the USER still exists AND is still a member of this household.
    if not conn.execute("SELECT 1 FROM users WHERE id=?", (uid,)).fetchone():
        raise HTTPException(401, "account no longer exists")
    if not conn.execute(
        "SELECT 1 FROM household_members WHERE household_id=? AND user_id=?", (hid, uid)
    ).fetchone():
        raise HTTPException(403, "not a member of this household")
    return Ctx(uid, hid)


def ctx_from_header(conn):
    """FastAPI dependency factory bound to the app's db connection."""

    def dep(authorization: str = Header(default="")) -> Ctx:
        if not authorization.lower().startswith("bearer "):
            raise HTTPException(401, "missing bearer token")
        return _resolve(conn, authorization[7:].strip())

    return dep


def ctx_from_token(conn, token: str) -> Ctx:
    """For the WebSocket path (token arrives as a ?query= param, not a header)."""
    return _resolve(conn, token)
