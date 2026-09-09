"""
Magic-link auth for reminder/escalation SMS and email. Staff on a phone tap
the link straight into the app for that day's task -- no password entry
required from a text message. Deliberately separate from the password+JWT
login path (services/auth.py) used for the admin/officer dashboard: the raw
token here is a single opaque secret, never a signed JWT, and only ever
lives in an SMS/email body and this table -- never in server logs (only its
hash is stored, same principle as a password reset token).

Two purposes, two different security postures:
  - "reminder" links (the default): multi-use until expiry -- a staff
    member re-opening the same text three days later, after their browser
    session/localStorage was cleared, must still get in without a
    password. TTL is wide enough to outlive the full reminder lifecycle
    for one occurrence (7-day-out reminder through overdue-admin
    escalation) so one link works for the whole window.
  - "password_reset" links: short TTL and single-use (consumed and
    invalidated on first use) -- these grant a fresh session specifically
    so the holder can set a new password; letting one sit around for two
    weeks or be replayed indefinitely would be a real credential-recovery
    attack surface a reminder link doesn't have.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

import asyncpg

MAGIC_LINK_TTL = timedelta(days=14)
PASSWORD_RESET_TTL = timedelta(hours=1)


class MagicLinkError(Exception):
    pass


def _hash(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode()).hexdigest()


async def create_magic_link(
    conn: asyncpg.Connection,
    *,
    user_id: str,
    facility_id: str,
    occurrence_id: Optional[str] = None,
    purpose: str = "reminder",
) -> str:
    """Returns the raw token -- callers must embed it in the SMS/email body
    immediately; it cannot be recovered afterward (only its hash is kept)."""
    raw_token = secrets.token_urlsafe(32)
    ttl = PASSWORD_RESET_TTL if purpose == "password_reset" else MAGIC_LINK_TTL
    expires_at = datetime.now(timezone.utc) + ttl
    await conn.execute(
        """
        INSERT INTO magic_links (facility_id, user_id, occurrence_id, token_hash, purpose, expires_at)
        VALUES ($1, $2, $3, $4, $5, $6)
        """,
        facility_id, user_id, occurrence_id, _hash(raw_token), purpose, expires_at,
    )
    return raw_token


async def consume_magic_link(conn: asyncpg.Connection, raw_token: str) -> dict:
    """Verifies the token and returns the user + scoped occurrence.
    "reminder" links are multi-use until their own expiry; "password_reset"
    links are invalidated immediately on first use -- see module
    docstring for why the two purposes need different postures."""
    row = await conn.fetchrow(
        """
        SELECT ml.*, u.role, u.workspace, u.active AS user_active
        FROM magic_links ml
        JOIN users u ON u.user_id = ml.user_id
        WHERE ml.token_hash = $1
        """,
        _hash(raw_token),
    )
    if not row:
        raise MagicLinkError("This link is invalid.")
    if not row["user_active"]:
        raise MagicLinkError("This account is no longer active.")
    if row["last_used_at"] is not None and row["purpose"] == "password_reset":
        raise MagicLinkError("This link has already been used. Request a new one.")
    if row["expires_at"] < datetime.now(timezone.utc):
        raise MagicLinkError("This link has expired. Ask your administrator for a new one.")
    await conn.execute(
        "UPDATE magic_links SET last_used_at = now() WHERE link_id = $1", row["link_id"]
    )
    return {
        "user_id": str(row["user_id"]),
        "facility_id": str(row["facility_id"]),
        "role": row["role"],
        "workspace": row["workspace"],
        "occurrence_id": str(row["occurrence_id"]) if row["occurrence_id"] else None,
        "purpose": row["purpose"],
    }
