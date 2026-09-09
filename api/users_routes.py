"""
  GET   /users             list this facility's users (admin only)
  POST  /users             create a staff/officer/admin account (admin only)
  PATCH /users/{id}        edit role/workspace/active/contact prefs (admin only)
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr, Field

from api.auth_routes import require_admin
from services.audit import log_action
from services.auth import hash_password
from services.database import get_pool
from services.notify_email import send_email

router = APIRouter()

ROLES = {"admin", "officer", "staff"}
WORKSPACES = {"IC", "EOC", "EAP", "MM", "PI"}


@router.get("/users")
async def list_users(user: dict = Depends(require_admin)):
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT user_id, name, email, phone, role, workspace, email_opt_in, sms_opt_in, active, created_at "
            "FROM users WHERE facility_id = $1 ORDER BY role, name",
            user["facility_id"],
        )
        return [dict(r) for r in rows]


class UserCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    email: EmailStr
    phone: Optional[str] = None
    password: str = Field(min_length=8, max_length=200)
    role: str
    workspace: Optional[str] = None
    sms_opt_in: bool = False


@router.post("/users")
async def create_user(body: UserCreate, user: dict = Depends(require_admin)):
    if body.role not in ROLES:
        raise HTTPException(400, f"role must be one of {sorted(ROLES)}")
    if body.workspace is not None and body.workspace not in WORKSPACES:
        raise HTTPException(400, f"workspace must be one of {sorted(WORKSPACES)}")
    if body.role == "officer" and not body.workspace:
        raise HTTPException(400, "officer accounts require a workspace.")
    pool = get_pool()
    async with pool.acquire() as conn:
        existing = await conn.fetchval("SELECT 1 FROM users WHERE email = $1", body.email.lower())
        if existing:
            raise HTTPException(409, "An account with this email already exists.")
        row = await conn.fetchrow(
            """
            INSERT INTO users (facility_id, name, email, phone, password_hash, role, workspace, sms_opt_in)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            RETURNING user_id, name, email, phone, role, workspace, active, created_at
            """,
            user["facility_id"], body.name, body.email.lower(), body.phone,
            hash_password(body.password), body.role, body.workspace, body.sms_opt_in,
        )
        actor_email = await conn.fetchval("SELECT email FROM users WHERE user_id = $1", user["user_id"])
        await log_action(
            conn, facility_id=user["facility_id"], actor_id=user["user_id"], actor_email=actor_email,
            action="user_created", entity_type="user", entity_id=str(row["user_id"]),
            detail={"role": body.role, "workspace": body.workspace},
        )
        return dict(row)


class UserUpdate(BaseModel):
    role: Optional[str] = None
    workspace: Optional[str] = None
    active: Optional[bool] = None
    email_opt_in: Optional[bool] = None
    sms_opt_in: Optional[bool] = None
    phone: Optional[str] = None
    new_password: Optional[str] = Field(default=None, min_length=8, max_length=200)


async def _would_remove_last_admin(conn, facility_id: str, user_id: str, fields: dict) -> bool:
    """True if this update would leave the facility with zero active
    admins -- e.g. demoting or deactivating the only remaining one, which
    would permanently lock every user out of Setup/user management with
    no recovery path (there is no platform-operator override for a
    single-tenant facility admin role)."""
    losing_admin = fields.get("role") not in (None, "admin") or fields.get("active") is False
    if not losing_admin:
        return False
    target = await conn.fetchrow("SELECT role, active FROM users WHERE user_id = $1", user_id)
    if not (target and target["role"] == "admin" and target["active"]):
        return False  # target isn't currently a live admin, so nothing to lose
    other_admins = await conn.fetchval(
        "SELECT count(*) FROM users WHERE facility_id = $1 AND role = 'admin' AND active = true AND user_id != $2",
        facility_id, user_id,
    )
    return other_admins == 0


@router.patch("/users/{user_id}")
async def update_user(user_id: str, body: UserUpdate, user: dict = Depends(require_admin)):
    if body.role is not None and body.role not in ROLES:
        raise HTTPException(400, f"role must be one of {sorted(ROLES)}")
    if body.workspace is not None and body.workspace not in WORKSPACES:
        raise HTTPException(400, f"workspace must be one of {sorted(WORKSPACES)}")
    pool = get_pool()
    async with pool.acquire() as conn:
        existing = await conn.fetchrow(
            "SELECT * FROM users WHERE user_id = $1 AND facility_id = $2", user_id, user["facility_id"]
        )
        if not existing:
            raise HTTPException(404, "User not found.")
        fields = body.model_dump(exclude_unset=True, exclude={"new_password"})
        new_password = body.new_password
        if not fields and not new_password:
            return {"user_id": user_id}
        if fields and await _would_remove_last_admin(conn, user["facility_id"], user_id, fields):
            raise HTTPException(409, "This facility must always have at least one active administrator.")
        row = dict(existing)
        if fields:
            set_clauses = [f"{k} = ${i+1}" for i, k in enumerate(fields)]
            values = list(fields.values()) + [user_id]
            row = await conn.fetchrow(
                f"UPDATE users SET {', '.join(set_clauses)} WHERE user_id = ${len(values)} "
                f"RETURNING user_id, name, email, phone, role, workspace, active, email_opt_in, sms_opt_in",
                *values,
            )
            row = dict(row)
        if new_password:
            await conn.execute("UPDATE users SET password_hash = $1 WHERE user_id = $2", hash_password(new_password), user_id)
        actor_email = await conn.fetchval("SELECT email FROM users WHERE user_id = $1", user["user_id"])
        log_detail = dict(fields)
        if new_password:
            log_detail["password_reset_by_admin"] = True
        await log_action(
            conn, facility_id=user["facility_id"], actor_id=user["user_id"], actor_email=actor_email,
            action="user_updated", entity_type="user", entity_id=user_id, detail=log_detail,
        )
        if new_password:
            send_email(
                existing["email"], existing["name"], "Your ReadinessOS password was reset",
                f"An administrator reset your ReadinessOS password. If you did not expect this, "
                f"contact your facility administrator immediately.",
            )
        return {k: v for k, v in row.items() if k != "password_hash"}
