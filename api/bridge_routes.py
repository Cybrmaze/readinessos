"""
Read-only server-to-server bridge for JUSTINE's binder integration.

JUSTINE's five JC binders (EC/EAP/IC/MM/PI) map to ReadinessOS's five
workspaces (EOC/EAP/IC/MM/PI) -- JUSTINE stores the policies, ReadinessOS
records proof the underlying survey/audit/drill work actually happened.
This bridge is the ONE place those two otherwise fully isolated products
touch: it exposes just enough (activity title/cadence/status and the
latest completed submission's date/signer/answers/evidence) for JUSTINE
to show real completion proof next to a policy, and nothing else about
the tenant.

Auth is a single shared secret (RXOS_BRIDGE_API_KEY, header X-Bridge-Key)
-- deliberately NOT a user JWT and NOT CLG_JWT_SECRET/BF_JWT_SECRET. This
is a server-to-server credential between exactly two backends for exactly
this one purpose; it authenticates "this call came from the CLG platform's
own backend," never an end user. The CLG-side facility_id is never sent
here -- only the readinessos_facility_id the CLG platform already stored
on its own facilities row via the one-time link (see auth_routes.py's
/facilities/{id}/identity on the CLG side, and the separate readinessos
link field).
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import FileResponse

from services.database import get_pool

router = APIRouter(prefix="/bridge")

# JUSTINE's binder id -> ReadinessOS's workspace id. Names differ
# (EC vs EOC) for historical reasons on each side; everything else matches.
JUSTINE_TO_WORKSPACE = {"EC": "EOC", "EAP": "EAP", "IC": "IC", "MM": "MM", "PI": "PI"}


def require_bridge_key(x_bridge_key: str = Header(default="")) -> None:
    expected = os.environ.get("RXOS_BRIDGE_API_KEY", "")
    if not expected or x_bridge_key != expected:
        raise HTTPException(401, "Invalid or missing bridge key.")


@router.get("/workspace-status", dependencies=[Depends(require_bridge_key)])
async def workspace_status(facility_id: str = Query(...)):
    """One call, all five workspaces -- simpler for the CLG-side caller
    than five round trips. Per activity: its cadence/title, the current
    occurrence's status, and (if one exists) the latest completed
    submission's date/signer/answers/evidence pointers."""
    pool = get_pool()
    async with pool.acquire() as conn:
        facility = await conn.fetchrow("SELECT facility_id, name FROM facilities WHERE facility_id = $1", facility_id)
        if not facility:
            raise HTTPException(404, "Facility not found.")

        activities = await conn.fetch(
            "SELECT * FROM activities WHERE facility_id = $1 AND active = true ORDER BY workspace, title",
            facility_id,
        )

        by_workspace: dict[str, list] = {ws: [] for ws in set(JUSTINE_TO_WORKSPACE.values())}
        for activity in activities:
            occurrences = await conn.fetch(
                "SELECT * FROM occurrences WHERE activity_id = $1 ORDER BY due_at", activity["activity_id"],
            )
            current = None
            for occ in occurrences:
                if occ["status"] != "complete":
                    current = occ
                    break
            if current is None and occurrences:
                current = occurrences[-1]  # most recent (by due_at) if all complete

            entry = {
                "activity_id": str(activity["activity_id"]),
                "title": activity["title"],
                "cadence": activity["cadence"],
                "status": current["status"] if current else "no_occurrences",
                "due_at": current["due_at"].isoformat() if current else None,
                "latest_submission": None,
            }

            # The most recent completed submission on record for this
            # activity -- independent of "current" above, which is the
            # next actionable occurrence and may already have moved past
            # the last completed one (e.g. a weekly audit due next week
            # still needs to show last week's real completion proof).
            latest_submission = await conn.fetchrow(
                "SELECT s.*, u.name AS submitted_by_name FROM submissions s "
                "JOIN occurrences o ON o.occurrence_id = s.occurrence_id "
                "JOIN users u ON u.user_id = s.submitted_by "
                "WHERE o.activity_id = $1 ORDER BY s.submitted_at DESC LIMIT 1",
                activity["activity_id"],
            )
            if latest_submission:
                evidence = await conn.fetch(
                    "SELECT file_id, original_filename, content_type FROM evidence_files WHERE submission_id = $1",
                    latest_submission["submission_id"],
                )
                entry["latest_submission"] = {
                    "submitted_at": latest_submission["submitted_at"].isoformat(),
                    "signature_name": latest_submission["signature_name"],
                    "submitted_by_name": latest_submission["submitted_by_name"],
                    "notes": latest_submission["notes"],
                    "evidence": [
                        {"file_id": str(e["file_id"]), "filename": e["original_filename"], "content_type": e["content_type"]}
                        for e in evidence
                    ],
                }

            by_workspace.setdefault(activity["workspace"], []).append(entry)

        return {
            "facility_id": str(facility["facility_id"]),
            "facility_name": facility["name"],
            "workspaces": by_workspace,
        }


@router.get("/evidence/{file_id}", dependencies=[Depends(require_bridge_key)])
async def bridge_evidence(file_id: str, facility_id: str = Query(...)):
    """Facility-scoped even for a bridge-authenticated caller -- a valid
    bridge key proves "this is the CLG platform," not "for any facility.\""""
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM evidence_files WHERE file_id = $1 AND facility_id = $2", file_id, facility_id,
        )
    if not row:
        raise HTTPException(404, "File not found.")
    path = Path(row["stored_path"])
    if not path.exists():
        raise HTTPException(410, "File is no longer available on disk.")
    return FileResponse(path, media_type=row["content_type"], filename=row["original_filename"])
