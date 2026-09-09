"""
  GET    /activities                list this facility's activity templates
  POST   /activities                create (admin only) -- also generates its first occurrences
  PATCH  /activities/{id}           edit (admin only)
  GET    /occurrences                list due-instances, optional ?status=&workspace=
  GET    /occurrences/{id}          one occurrence + its activity + any submission/evidence
  PUT    /occurrences/{id}/draft     save in-progress answers (mutable until submitted)
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from api.auth_routes import require_admin, require_user
from services.audit import log_action
from services.database import get_pool
from services.scheduling import generate_occurrences_for_activity

router = APIRouter()

WORKSPACES = {"IC", "EOC", "EAP", "MM", "PI"}
CADENCES = {"daily", "weekly", "biweekly", "monthly", "quarterly", "semiannual", "annual"}


def _scope_workspace(user: dict, requested: Optional[str]) -> Optional[str]:
    """Officers/staff assigned to one workspace can only ever see that
    workspace's activities/occurrences, regardless of what they ask for."""
    if user["role"] == "admin":
        return requested
    return user["workspace"]


class ActivityCreate(BaseModel):
    workspace: str
    title: str = Field(min_length=1, max_length=255)
    cadence: str
    assignee_id: Optional[str] = None
    checklist: list[str] = Field(default_factory=list)


@router.get("/activities")
async def list_activities(workspace: Optional[str] = Query(None), user: dict = Depends(require_user)):
    ws = _scope_workspace(user, workspace)
    pool = get_pool()
    async with pool.acquire() as conn:
        if ws:
            rows = await conn.fetch(
                "SELECT * FROM activities WHERE facility_id = $1 AND workspace = $2 AND active = true ORDER BY title",
                user["facility_id"], ws,
            )
        elif user["role"] != "admin":
            rows = await conn.fetch(
                "SELECT * FROM activities WHERE facility_id = $1 AND assignee_id = $2 AND active = true ORDER BY title",
                user["facility_id"], user["user_id"],
            )
        else:
            rows = await conn.fetch(
                "SELECT * FROM activities WHERE facility_id = $1 AND active = true ORDER BY workspace, title",
                user["facility_id"],
            )
        return [dict(r) for r in rows]


@router.post("/activities")
async def create_activity(body: ActivityCreate, user: dict = Depends(require_admin)):
    if body.workspace not in WORKSPACES:
        raise HTTPException(400, f"workspace must be one of {sorted(WORKSPACES)}")
    if body.cadence not in CADENCES:
        raise HTTPException(400, f"cadence must be one of {sorted(CADENCES)}")
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                INSERT INTO activities (facility_id, workspace, title, cadence, assignee_id, checklist, created_by)
                VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7)
                RETURNING *
                """,
                user["facility_id"], body.workspace, body.title, body.cadence,
                body.assignee_id, __import__("json").dumps(body.checklist), user["user_id"],
            )
            await generate_occurrences_for_activity(conn, row)
            await log_action(
                conn, facility_id=user["facility_id"], actor_id=user["user_id"],
                actor_email=(await conn.fetchval("SELECT email FROM users WHERE user_id = $1", user["user_id"])),
                action="activity_created", entity_type="activity", entity_id=str(row["activity_id"]),
                detail={"title": body.title, "workspace": body.workspace, "cadence": body.cadence},
            )
        return dict(row)


class ActivityUpdate(BaseModel):
    title: Optional[str] = None
    cadence: Optional[str] = None
    assignee_id: Optional[str] = None
    checklist: Optional[list[str]] = None
    active: Optional[bool] = None


@router.patch("/activities/{activity_id}")
async def update_activity(activity_id: str, body: ActivityUpdate, user: dict = Depends(require_admin)):
    if body.cadence is not None and body.cadence not in CADENCES:
        raise HTTPException(400, f"cadence must be one of {sorted(CADENCES)}")
    pool = get_pool()
    async with pool.acquire() as conn:
        existing = await conn.fetchrow(
            "SELECT * FROM activities WHERE activity_id = $1 AND facility_id = $2",
            activity_id, user["facility_id"],
        )
        if not existing:
            raise HTTPException(404, "Activity not found.")
        fields = body.model_dump(exclude_unset=True)
        if not fields:
            return dict(existing)
        import json as _json
        set_clauses, values = [], []
        for i, (k, v) in enumerate(fields.items(), start=1):
            if k == "checklist":
                v = _json.dumps(v)
                set_clauses.append(f"{k} = ${i}::jsonb")
            else:
                set_clauses.append(f"{k} = ${i}")
            values.append(v)
        set_clauses.append("updated_at = now()")
        values.append(activity_id)
        row = await conn.fetchrow(
            f"UPDATE activities SET {', '.join(set_clauses)} WHERE activity_id = ${len(values)} RETURNING *",
            *values,
        )
        await log_action(
            conn, facility_id=user["facility_id"], actor_id=user["user_id"],
            actor_email=(await conn.fetchval("SELECT email FROM users WHERE user_id = $1", user["user_id"])),
            action="activity_updated", entity_type="activity", entity_id=activity_id,
            detail=fields,
        )
        return dict(row)


@router.get("/occurrences")
async def list_occurrences(
    status: Optional[str] = Query(None),
    workspace: Optional[str] = Query(None),
    user: dict = Depends(require_user),
):
    ws = _scope_workspace(user, workspace)
    pool = get_pool()
    conditions = ["o.facility_id = $1"]
    params: list = [user["facility_id"]]
    if ws:
        params.append(ws)
        conditions.append(f"a.workspace = ${len(params)}")
    elif user["role"] != "admin":
        # staff with no workspace of their own only see occurrences on
        # activities individually assigned to them (see activities table's
        # own comment: "staff can be assigned individual activities without
        # owning a whole workspace").
        params.append(user["user_id"])
        conditions.append(f"a.assignee_id = ${len(params)}")
    if status:
        params.append(status)
        conditions.append(f"o.status = ${len(params)}")
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT o.*, a.title, a.workspace, a.cadence, a.checklist, a.assignee_id
            FROM occurrences o
            JOIN activities a ON a.activity_id = o.activity_id
            WHERE {' AND '.join(conditions)}
            ORDER BY o.due_at
            """,
            *params,
        )
        return [dict(r) for r in rows]


@router.get("/occurrences/{occurrence_id}")
async def get_occurrence(occurrence_id: str, user: dict = Depends(require_user)):
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT o.*, a.title, a.workspace, a.cadence, a.checklist, a.assignee_id
            FROM occurrences o
            JOIN activities a ON a.activity_id = o.activity_id
            WHERE o.occurrence_id = $1 AND o.facility_id = $2
            """,
            occurrence_id, user["facility_id"],
        )
        if not row:
            raise HTTPException(404, "Occurrence not found.")
        submission = await conn.fetchrow(
            "SELECT * FROM submissions WHERE occurrence_id = $1", occurrence_id
        )
        evidence = []
        if submission:
            evidence = await conn.fetch(
                "SELECT file_id, original_filename, content_type, size_bytes, uploaded_at "
                "FROM evidence_files WHERE submission_id = $1", submission["submission_id"]
            )
        result = dict(row)
        result["submission"] = dict(submission) if submission else None
        result["evidence"] = [dict(e) for e in evidence]
        return result


class DraftUpdate(BaseModel):
    draft: dict


@router.put("/occurrences/{occurrence_id}/draft")
async def save_draft(occurrence_id: str, body: DraftUpdate, user: dict = Depends(require_user)):
    pool = get_pool()
    async with pool.acquire() as conn:
        existing = await conn.fetchrow(
            "SELECT status FROM occurrences WHERE occurrence_id = $1 AND facility_id = $2",
            occurrence_id, user["facility_id"],
        )
        if not existing:
            raise HTTPException(404, "Occurrence not found.")
        if existing["status"] == "complete":
            raise HTTPException(409, "This occurrence is already submitted and locked.")
        row = await conn.fetchrow(
            "UPDATE occurrences SET draft = $1::jsonb, updated_at = now() WHERE occurrence_id = $2 RETURNING *",
            __import__("json").dumps(body.draft), occurrence_id,
        )
        return dict(row)
