"""
ReadinessOS auth + facility setup.

  POST /auth/setup-facility        create a new facility + its first admin user
  POST /auth/login                 user -> access token
  POST /auth/forgot-password       email a single-use password-reset link
  POST /auth/reset-password        consume that link's token + set a new password
  POST /auth/change-password       logged-in user changes their own password
  GET  /auth/me                    current user profile
"""
from __future__ import annotations

import os
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field

from services.audit import log_action
from services.auth import (
    DUMMY_PASSWORD_HASH,
    RXOSAuthError,
    create_access_token,
    hash_password,
    verify_access_token,
    verify_password,
)
from services.database import get_pool
from services.magic_links import MagicLinkError, consume_magic_link, create_magic_link
from services.notify_email import send_email
from services.rate_limit import check_rate_limit, client_ip

router = APIRouter()


async def require_user(request: Request) -> dict:
    """Re-verifies the user against the DB on every request (not just the
    JWT payload) -- a deactivated account, or a role/workspace change made
    by an admin after the token was issued, must take effect immediately,
    not up to RXOS_ACCESS_TOKEN_TTL later. The JWT is only trusted for
    identity (user_id/facility_id); authorization state is always live."""
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(401, "Missing or malformed Authorization header.")
    try:
        payload = verify_access_token(auth[len("Bearer "):])
    except RXOSAuthError as e:
        raise HTTPException(401, str(e))
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT user_id, facility_id, role, workspace, active FROM users WHERE user_id = $1",
            payload["user_id"],
        )
    if not row or not row["active"]:
        raise HTTPException(401, "This account is no longer active.")
    return {
        "user_id": str(row["user_id"]),
        "facility_id": str(row["facility_id"]),
        "role": row["role"],
        "workspace": row["workspace"],
    }


async def require_admin(user: dict = Depends(require_user)) -> dict:
    if user["role"] != "admin":
        raise HTTPException(403, "Administrator role required.")
    return user


class SetupFacilityRequest(BaseModel):
    facility_name: str = Field(min_length=1, max_length=255)
    timezone: str = "America/Los_Angeles"
    admin_name: str = Field(min_length=1, max_length=255)
    admin_email: EmailStr
    admin_phone: Optional[str] = None
    password: str = Field(min_length=8, max_length=200)


@router.post("/auth/setup-facility")
async def setup_facility(body: SetupFacilityRequest, request: Request):
    check_rate_limit("setup:" + client_ip(request), max_requests=5, window_seconds=3600)
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            existing = await conn.fetchval(
                "SELECT 1 FROM users WHERE email = $1", body.admin_email.lower()
            )
            if existing:
                raise HTTPException(409, "An account with this email already exists.")
            facility = await conn.fetchrow(
                """
                INSERT INTO facilities (name, timezone, admin_name, admin_email, admin_phone)
                VALUES ($1, $2, $3, $4, $5)
                RETURNING facility_id
                """,
                body.facility_name, body.timezone, body.admin_name,
                body.admin_email.lower(), body.admin_phone,
            )
            user = await conn.fetchrow(
                """
                INSERT INTO users (facility_id, name, email, phone, password_hash, role)
                VALUES ($1, $2, $3, $4, $5, 'admin')
                RETURNING user_id
                """,
                facility["facility_id"], body.admin_name, body.admin_email.lower(),
                body.admin_phone, hash_password(body.password),
            )
            await log_action(
                conn,
                facility_id=str(facility["facility_id"]),
                actor_id=str(user["user_id"]),
                actor_email=body.admin_email.lower(),
                action="facility_created",
                entity_type="facility",
                entity_id=str(facility["facility_id"]),
                detail={"facility_name": body.facility_name},
            )
    token = create_access_token(str(user["user_id"]), str(facility["facility_id"]), "admin", None)
    return {"access_token": token, "facility_id": str(facility["facility_id"]), "user_id": str(user["user_id"])}


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


@router.post("/auth/login")
async def login(body: LoginRequest, request: Request):
    email = body.email.lower()
    check_rate_limit("login-ip:" + client_ip(request), max_requests=20, window_seconds=900)
    check_rate_limit("login-email:" + email, max_requests=10, window_seconds=900)
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM users WHERE email = $1 AND active = true", email
        )
        # Always run a real bcrypt comparison, even for a nonexistent
        # account -- otherwise a fast rejection vs. a slow (real-hash)
        # rejection leaks which emails are registered.
        password_ok = verify_password(body.password, row["password_hash"] if row else DUMMY_PASSWORD_HASH)
        if not row or not password_ok:
            raise HTTPException(401, "Invalid email or password.")
        await log_action(
            conn,
            facility_id=str(row["facility_id"]),
            actor_id=str(row["user_id"]),
            actor_email=row["email"],
            action="login",
            entity_type="user",
            entity_id=str(row["user_id"]),
        )
    token = create_access_token(str(row["user_id"]), str(row["facility_id"]), row["role"], row["workspace"])
    return {
        "access_token": token,
        "user": {
            "user_id": str(row["user_id"]),
            "facility_id": str(row["facility_id"]),
            "name": row["name"],
            "email": row["email"],
            "role": row["role"],
            "workspace": row["workspace"],
        },
    }


class MagicLinkConsumeRequest(BaseModel):
    token: str


@router.post("/auth/magic-link/consume")
async def consume_magic_link_route(body: MagicLinkConsumeRequest, request: Request):
    """Called by the tiny landing page at co.myclg.net/m?token=... right
    after a staff member taps the link from a reminder SMS/email, or the
    password-reset link from /auth/forgot-password. Exchanges the
    one-time-issued opaque token for a normal access token (same shape the
    password-login path issues) plus the occurrence to jump straight into,
    if the link was scoped to one. The token itself has 256 bits of
    entropy (brute-forcing it is infeasible), so this limit exists only to
    blunt casual scraping/retry storms, not as the actual defense."""
    check_rate_limit("magiclink:" + client_ip(request), max_requests=30, window_seconds=3600)
    pool = get_pool()
    async with pool.acquire() as conn:
        try:
            claim = await consume_magic_link(conn, body.token)
        except MagicLinkError as e:
            raise HTTPException(401, str(e))
        actor_email = await conn.fetchval("SELECT email FROM users WHERE user_id = $1", claim["user_id"])
        await log_action(
            conn, facility_id=claim["facility_id"], actor_id=claim["user_id"], actor_email=actor_email,
            action="magic_link_used", entity_type="user", entity_id=claim["user_id"],
            detail={"occurrence_id": claim["occurrence_id"], "purpose": claim["purpose"]},
        )
    token = create_access_token(claim["user_id"], claim["facility_id"], claim["role"], claim["workspace"])
    return {"access_token": token, "occurrence_id": claim["occurrence_id"], "purpose": claim["purpose"]}


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


@router.post("/auth/forgot-password")
async def forgot_password(body: ForgotPasswordRequest, request: Request):
    """Always returns the same generic response regardless of whether the
    email is registered -- the response itself must never reveal account
    existence. If it is registered and active, a single-use, 1-hour
    password-reset magic link is emailed."""
    check_rate_limit("forgot:" + client_ip(request), max_requests=5, window_seconds=3600)
    email = body.email.lower()
    check_rate_limit("forgot-email:" + email, max_requests=3, window_seconds=3600)
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM users WHERE email = $1 AND active = true", email)
        if row:
            raw_token = await create_magic_link(
                conn, user_id=str(row["user_id"]), facility_id=str(row["facility_id"]), purpose="password_reset",
            )
            app_url = os.environ.get("RXOS_APP_URL", "https://co.myclg.net")
            link = f"{app_url}/reset-password?token={raw_token}"
            send_email(
                row["email"], row["name"], "Reset your ReadinessOS password",
                f"Click the link below to set a new password. This link expires in 1 hour and can only be used once.\n\n{link}\n\n"
                f"If you didn't request this, you can safely ignore this email.",
            )
            await log_action(
                conn, facility_id=str(row["facility_id"]), actor_id=str(row["user_id"]), actor_email=row["email"],
                action="password_reset_requested", entity_type="user", entity_id=str(row["user_id"]),
            )
    return {"message": "If that email is registered, a reset link has been sent."}


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str = Field(min_length=8, max_length=200)


@router.post("/auth/reset-password")
async def reset_password(body: ResetPasswordRequest):
    pool = get_pool()
    async with pool.acquire() as conn:
        try:
            claim = await consume_magic_link(conn, body.token)
        except MagicLinkError as e:
            raise HTTPException(401, str(e))
        if claim["purpose"] != "password_reset":
            raise HTTPException(400, "This link cannot be used to reset a password.")
        await conn.execute(
            "UPDATE users SET password_hash = $1 WHERE user_id = $2",
            hash_password(body.new_password), claim["user_id"],
        )
        actor_email = await conn.fetchval("SELECT email FROM users WHERE user_id = $1", claim["user_id"])
        await log_action(
            conn, facility_id=claim["facility_id"], actor_id=claim["user_id"], actor_email=actor_email,
            action="password_reset_completed", entity_type="user", entity_id=claim["user_id"],
        )
    token = create_access_token(claim["user_id"], claim["facility_id"], claim["role"], claim["workspace"])
    return {"access_token": token}


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8, max_length=200)


@router.post("/auth/change-password")
async def change_password(body: ChangePasswordRequest, user: dict = Depends(require_user)):
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", user["user_id"])
        if not row or not verify_password(body.current_password, row["password_hash"]):
            raise HTTPException(401, "Current password is incorrect.")
        await conn.execute(
            "UPDATE users SET password_hash = $1 WHERE user_id = $2",
            hash_password(body.new_password), user["user_id"],
        )
        await log_action(
            conn, facility_id=user["facility_id"], actor_id=user["user_id"], actor_email=row["email"],
            action="password_changed", entity_type="user", entity_id=user["user_id"],
        )
    return {"message": "Password updated."}


@router.get("/auth/me")
async def me(user: dict = Depends(require_user)):
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", user["user_id"])
        if not row:
            raise HTTPException(404, "User not found.")
        return {
            "user_id": str(row["user_id"]),
            "facility_id": str(row["facility_id"]),
            "name": row["name"],
            "email": row["email"],
            "role": row["role"],
            "workspace": row["workspace"],
        }
