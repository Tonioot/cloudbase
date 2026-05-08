"""
Authentication module for Cloudbase.

- Users are stored in the SQLite database with bcrypt-hashed passwords.
- JWTs (HS256) carry username + role, expire after 1 hour.
- Tokens are delivered as httpOnly, SameSite=Strict cookies.
- Roles: "admin" (full access) | "viewer" (read-only).
"""

import os
import secrets
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from typing import Optional

import bcrypt as _bcrypt
from fastapi import Cookie, Depends, HTTPException, Request, status
from jose import JWTError, jwt
import config as _cfg

# ── Config ────────────────────────────────────────────────────────────────────
CREDENTIALS_FILE = os.path.expanduser("~/.cloudbase/credentials")
ALGORITHM = "HS256"


def get_token_expire_seconds() -> int:
    return _cfg.get_auth("token_expire_seconds")


# Legacy constant kept for cookie max_age references; resolved at call time via get_token_expire_seconds().
TOKEN_EXPIRE_SECONDS = 3600

# ── Secret key (generated once, stored alongside credentials) ─────────────────
_SECRET_KEY_FILE = os.path.expanduser("~/.cloudbase/secret_key")


def _load_secret_key() -> str:
    os.makedirs(os.path.dirname(_SECRET_KEY_FILE), exist_ok=True)
    if os.path.exists(_SECRET_KEY_FILE):
        with open(_SECRET_KEY_FILE) as f:
            return f.read().strip()
    key = secrets.token_hex(48)
    with open(_SECRET_KEY_FILE, "w") as f:
        f.write(key)
    os.chmod(_SECRET_KEY_FILE, 0o600)
    return key


SECRET_KEY: str = _load_secret_key()


# ── Password helpers ──────────────────────────────────────────────────────────
def hash_password(plain: str) -> str:
    return _bcrypt.hashpw(plain.encode(), _bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return _bcrypt.checkpw(plain.encode(), hashed.encode())
    except Exception:
        return False


def load_hashed_password() -> Optional[str]:
    if not os.path.exists(CREDENTIALS_FILE):
        return None
    with open(CREDENTIALS_FILE) as f:
        return f.read().strip() or None


def save_hashed_password(hashed: str) -> None:
    os.makedirs(os.path.dirname(CREDENTIALS_FILE), exist_ok=True)
    with open(CREDENTIALS_FILE, "w") as f:
        f.write(hashed)
    os.chmod(CREDENTIALS_FILE, 0o600)


# ── JWT helpers ───────────────────────────────────────────────────────────────
def create_access_token(username: str, role: str) -> str:
    expire_seconds = get_token_expire_seconds()
    expire = datetime.now(timezone.utc) + timedelta(seconds=expire_seconds)
    payload = {
        "sub": username,
        "role": role,
        "exp": expire,
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


# ── Internal agent token (for node_agent.py to authenticate locally) ──────────
_AGENT_TOKEN_FILE = os.path.expanduser("~/.cloudbase/agent_token")


def get_or_create_agent_token() -> str:
    """Return existing agent token or create a new one."""
    os.makedirs(os.path.dirname(_AGENT_TOKEN_FILE), exist_ok=True)
    if os.path.exists(_AGENT_TOKEN_FILE):
        with open(_AGENT_TOKEN_FILE) as f:
            tok = f.read().strip()
        if tok:
            return tok
    tok = secrets.token_hex(32)
    with open(_AGENT_TOKEN_FILE, "w") as f:
        f.write(tok)
    os.chmod(_AGENT_TOKEN_FILE, 0o600)
    return tok


def verify_agent_token(token: str) -> bool:
    """Return True if the token matches the stored agent token."""
    if not token:
        return False
    try:
        if not os.path.exists(_AGENT_TOKEN_FILE):
            return False
        with open(_AGENT_TOKEN_FILE) as f:
            stored = f.read().strip()
        return secrets.compare_digest(token, stored)
    except Exception:
        return False


def _decode_payload(token: str) -> Optional[dict]:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        return None


def decode_token(token: str) -> Optional[dict]:
    """Return {"username": str, "role": str} if valid, else None.
    Legacy tokens with sub=="admin" and no role field are treated as admin."""
    payload = _decode_payload(token)
    if payload is None:
        return None
    username = payload.get("sub")
    if not username:
        return None
    role = payload.get("role", "admin" if username == "admin" else "viewer")
    return {"username": username, "role": role}


def get_token_expires_in(token: str) -> Optional[int]:
    """Return seconds remaining until token expires, or None if invalid."""
    payload = _decode_payload(token)
    if payload is None or not payload.get("sub"):
        return None
    exp = payload.get("exp")
    if exp is None:
        return None
    remaining = int(exp - datetime.now(timezone.utc).timestamp())
    return max(0, remaining)


# ── Rate limiter (in-memory, per IP) ─────────────────────────────────────────
_login_attempts: dict[str, list[float]] = defaultdict(list)
MAX_ATTEMPTS = 5
WINDOW_SECONDS = 60


def _check_rate_limit(ip: str) -> None:
    now = time.time()
    attempts = _login_attempts[ip]
    attempts[:] = [t for t in attempts if now - t < WINDOW_SECONDS]
    if len(attempts) >= MAX_ATTEMPTS:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many login attempts. Try again in {WINDOW_SECONDS} seconds.",
        )
    attempts.append(now)


# ── FastAPI dependencies ──────────────────────────────────────────────────────
_COOKIE_NAME = "pdm_token"


def require_auth(pdm_token: Optional[str] = Cookie(default=None)) -> dict:
    """Require a valid session. Returns {"username": str, "role": str}."""
    if not pdm_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    user = decode_token(pdm_token)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    return user


async def _get_db_role(username: str) -> Optional[str]:
    """Look up the current role from the database (bypasses the JWT cache)."""
    from database import AsyncSessionLocal
    from models import User
    from sqlalchemy import select
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(User).where(User.username == username))
            user = result.scalar_one_or_none()
            return user.role if user else None
    except Exception:
        return None


async def require_admin(pdm_token: Optional[str] = Cookie(default=None)) -> dict:
    """Require admin role, validated live against the database so role changes take effect immediately."""
    if not pdm_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    user = decode_token(pdm_token)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    # Always re-check role from DB so a downgraded admin can no longer act
    db_role = await _get_db_role(user["username"])
    if db_role is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    if db_role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    user["role"] = db_role
    return user


async def require_superadmin(pdm_token: Optional[str] = Cookie(default=None)) -> dict:
    """Require the built-in 'admin' superuser account. Only this account may manage users."""
    user = await require_admin(pdm_token)
    if user.get("username") != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only the superadmin account can manage users")
    return user


def get_current_actor(pdm_token: Optional[str] = Cookie(default=None)) -> str:
    """Return the username of the current session, or 'system' for agent/unauthenticated calls."""
    if not pdm_token:
        return "system"
    user = decode_token(pdm_token)
    return user["username"] if user else "system"
