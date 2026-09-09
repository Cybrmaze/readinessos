"""
  GET   /facility     current facility's identity (any authenticated user)
  PATCH /facility      update name/timezone/admin contact (admin only)
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr

from api.auth_routes import require_admin, require_user
from services.audit import log_action
from services.database import get_pool

router = APIRouter()


@router.get("/facility")
async def get_facility(user: dict = Depends(require_user)):
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM facilities WHERE facility_id = $1", user["facility_id"])
        if not row:
            raise HTTPException(404, "Facility not found.")
        return dict(row)


class FacilityUpdate(BaseModel):
    name: Optional[str] = None
    timezone: Optional[str] = None
    admin_name: Optional[str] = None
    admin_email: Optional[EmailStr] = None
    admin_phone: Optional[str] = None


@router.patch("/facility")
async def update_facility(body: FacilityUpdate, user: dict = Depends(require_admin)):
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(400, "No fields to update.")
    pool = get_pool()
    async with pool.acquire() as conn:
        set_clauses = [f"{k} = ${i+1}" for i, k in enumerate(fields)]
        values = list(fields.values()) + [user["facility_id"]]
        row = await conn.fetchrow(
            f"UPDATE facilities SET {', '.join(set_clauses)}, updated_at = now() "
            f"WHERE facility_id = ${len(values)} RETURNING *",
            *values,
        )
        actor_email = await conn.fetchval("SELECT email FROM users WHERE user_id = $1", user["user_id"])
        await log_action(
            conn, facility_id=user["facility_id"], actor_id=user["user_id"], actor_email=actor_email,
            action="facility_updated", entity_type="facility", entity_id=user["facility_id"], detail=fields,
        )
        return dict(row)
