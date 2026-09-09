"""
  GET /admin/summary          dashboard counts by workspace/status
  GET /admin/audit-log        paginated audit trail (admin only)
  GET /admin/export.csv       flat CSV of all completed submissions
"""
from __future__ import annotations

import csv
import io
import json

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse

from api.auth_routes import require_admin
from services.database import get_pool

router = APIRouter()


@router.get("/admin/summary")
async def summary(user: dict = Depends(require_admin)):
    pool = get_pool()
    async with pool.acquire() as conn:
        by_status = await conn.fetch(
            """
            SELECT a.workspace, o.status, count(*) AS n
            FROM occurrences o JOIN activities a ON a.activity_id = o.activity_id
            WHERE o.facility_id = $1
            GROUP BY a.workspace, o.status
            """,
            user["facility_id"],
        )
        overdue = await conn.fetch(
            """
            SELECT o.occurrence_id, a.title, a.workspace, o.due_at
            FROM occurrences o JOIN activities a ON a.activity_id = o.activity_id
            WHERE o.facility_id = $1 AND o.status = 'overdue'
            ORDER BY o.due_at
            """,
            user["facility_id"],
        )
        return {
            "by_workspace_status": [dict(r) for r in by_status],
            "overdue": [dict(r) for r in overdue],
        }


@router.get("/admin/audit-log")
async def audit_log(
    limit: int = Query(100, le=500),
    offset: int = Query(0, ge=0),
    user: dict = Depends(require_admin),
):
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM audit_log WHERE facility_id = $1 ORDER BY created_at DESC LIMIT $2 OFFSET $3",
            user["facility_id"], limit, offset,
        )
        return [dict(r) for r in rows]


@router.get("/admin/export.csv")
async def export_csv(user: dict = Depends(require_admin)):
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT a.workspace, a.title, o.due_at, s.submitted_at, u.name AS submitted_by,
                   s.signature_name, s.answers, s.notes
            FROM submissions s
            JOIN occurrences o ON o.occurrence_id = s.occurrence_id
            JOIN activities a ON a.activity_id = o.activity_id
            JOIN users u ON u.user_id = s.submitted_by
            WHERE s.facility_id = $1
            ORDER BY s.submitted_at DESC
            """,
            user["facility_id"],
        )

    def generate():
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["workspace", "title", "due_at", "submitted_at", "submitted_by",
                          "signature_name", "answers", "notes"])
        yield buf.getvalue()
        for r in rows:
            buf.seek(0)
            buf.truncate(0)
            answers = r["answers"]
            if isinstance(answers, str):
                answers_str = answers
            else:
                answers_str = json.dumps(answers)
            writer.writerow([
                r["workspace"], r["title"], r["due_at"], r["submitted_at"], r["submitted_by"],
                r["signature_name"], answers_str, r["notes"] or "",
            ])
            yield buf.getvalue()

    return StreamingResponse(
        generate(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=readinessos_export.csv"},
    )
