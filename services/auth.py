"""
ReadinessOS auth. Standalone, mirrors clg-bf's services/auth.py isolation
pattern exactly: own JWT secret (RXOS_JWT_SECRET), own token type, no shared
signing material with CLG_JWT_SECRET or BF_JWT_SECRET. ReadinessOS is a
separate product sold alongside JUSTINE, not merged into it -- same "single
owner, not shared trust" principle applied to this product boundary.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt

RXOS_JWT_ALGORITHM = "HS256"
RXOS_JWT_TOKEN_TYPE = "rxos_user"
RXOS_ACCESS_TOKEN_TTL = timedelta(hours=12)

# Precomputed once at import so a login attempt against a nonexistent email
# still runs a real bcrypt comparison (same cost as a real user) instead of
# short-circuiting instantly -- otherwise response time alone reveals
# whether an email is registered.
DUMMY_PASSWORD_HASH = bcrypt.hashpw(b"rxos-timing-safety-dummy", bcrypt.gensalt(rounds=12)).decode()


class RXOSAuthError(Exception):
    """Raised on any token verification failure -- callers convert to 401."""


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt(rounds=12)).decode()


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode(), hashed.encode())
    except ValueError:
        return False


def _secret() -> str:
    secret = os.getenv("RXOS_JWT_SECRET")
    if not secret:
        raise RuntimeError(
            "RXOS_JWT_SECRET is not set -- auth cannot issue "
            "or verify tokens until it is configured."
        )
    return secret


def create_access_token(user_id: str, facility_id: str, role: str, workspace: str | None) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "type": RXOS_JWT_TOKEN_TYPE,
        "user_id": user_id,
        "facility_id": facility_id,
        "role": role,
        "workspace": workspace,
        "iat": int(now.timestamp()),
        "exp": int((now + RXOS_ACCESS_TOKEN_TTL).timestamp()),
    }
    return jwt.encode(payload, _secret(), algorithm=RXOS_JWT_ALGORITHM)


def verify_access_token(token: str) -> dict:
    try:
        payload = jwt.decode(token, _secret(), algorithms=[RXOS_JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise RXOSAuthError("Token expired.")
    except jwt.InvalidTokenError as e:
        raise RXOSAuthError(f"Invalid token: {e}")
    if payload.get("type") != RXOS_JWT_TOKEN_TYPE:
        raise RXOSAuthError("Wrong token type.")
    return payload
