"""
  POST /occurrences/{id}/submit        lock in the completed submission (immutable)
  POST /submissions/{id}/evidence      attach an evidence file to a submission
  GET  /evidence/{file_id}             download an evidence file (facility-scoped)
"""
from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from api.auth_routes import require_user
from services.audit import log_action
from services.database import get_pool

router = APIRouter()

UPLOAD_DIR = Path("/opt/readinessos/uploads")
ALLOWED_UPLOAD_TYPES = {
    "application/pdf", "image/png", "image/jpeg", "image/heic", "image/webp",
    "text/plain", "text/csv",
}
MAX_UPLOAD_BYTES = 15_000_000

# A client-supplied Content-Type header is just a claim -- this confirms
# the file's actual bytes for the types that have a reliable magic number,
# so a file can't be stored under (and later served back as) a type it
# isn't. text/plain and text/csv have no reliable magic number and are
# intentionally not checked here beyond the extension allowlist above.
_MAGIC_CHECKS = {
    "application/pdf": lambda b: b.startswith(b"%PDF-"),
    "image/png": lambda b: b.startswith(b"\x89PNG\r\n\x1a\n"),
    "image/jpeg": lambda b: b.startswith(b"\xff\xd8\xff"),
    "image/webp": lambda b: b[:4] == b"RIFF" and b[8:12] == b"WEBP",
    "image/heic": lambda b: b[4:12] in (b"ftypheic", b"ftypheix", b"ftypmif1", b"ftypheim", b"ftypheis"),
}


class SubmitRequest(BaseModel):
    answers: dict
    notes: str | None = None
    signature_name: str


@router.post("/occurrences/{occurrence_id}/submit")
async def submit_occurrence(occurrence_id: str, body: SubmitRequest, user: dict = Depends(require_user)):
    if not body.signature_name.strip():
        raise HTTPException(400, "signature_name is required.")
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            occ = await conn.fetchrow(
                "SELECT * FROM occurrences WHERE occurrence_id = $1 AND facility_id = $2 FOR UPDATE",
                occurrence_id, user["facility_id"],
            )
            if not occ:
                raise HTTPException(404, "Occurrence not found.")
            if occ["status"] == "complete":
                raise HTTPException(409, "This occurrence has already been submitted.")
            submission = await conn.fetchrow(
                """
                INSERT INTO submissions (occurrence_id, facility_id, submitted_by, answers, notes, signature_name)
                VALUES ($1, $2, $3, $4::jsonb, $5, $6)
                RETURNING *
                """,
                occurrence_id, user["facility_id"], user["user_id"],
                json.dumps(body.answers), body.notes, body.signature_name.strip(),
            )
            await conn.execute(
                "UPDATE occurrences SET status = 'complete', draft = NULL, updated_at = now() WHERE occurrence_id = $1",
                occurrence_id,
            )
            actor_email = await conn.fetchval("SELECT email FROM users WHERE user_id = $1", user["user_id"])
            await log_action(
                conn, facility_id=user["facility_id"], actor_id=user["user_id"], actor_email=actor_email,
                action="occurrence_submitted", entity_type="occurrence", entity_id=occurrence_id,
                detail={"submission_id": str(submission["submission_id"])},
            )
            return dict(submission)


@router.post("/submissions/{submission_id}/evidence")
async def upload_evidence(submission_id: str, file: UploadFile = File(...), user: dict = Depends(require_user)):
    if file.content_type not in ALLOWED_UPLOAD_TYPES:
        raise HTTPException(400, f"Unsupported file type: {file.content_type}")
    contents = await file.read()
    if len(contents) > MAX_UPLOAD_BYTES:
        raise HTTPException(400, "File too large (max 15 MB).")
    magic_check = _MAGIC_CHECKS.get(file.content_type)
    if magic_check and not magic_check(contents):
        raise HTTPException(400, "File content does not match its declared type.")

    pool = get_pool()
    async with pool.acquire() as conn:
        submission = await conn.fetchrow(
            "SELECT * FROM submissions WHERE submission_id = $1 AND facility_id = $2",
            submission_id, user["facility_id"],
        )
        if not submission:
            raise HTTPException(404, "Submission not found.")

        facility_dir = UPLOAD_DIR / user["facility_id"]
        facility_dir.mkdir(parents=True, exist_ok=True)
        stored_name = f"{uuid.uuid4().hex}_{file.filename}"
        stored_path = facility_dir / stored_name
        stored_path.write_bytes(contents)

        row = await conn.fetchrow(
            """
            INSERT INTO evidence_files
                (submission_id, facility_id, original_filename, stored_path, content_type, size_bytes, sha256, uploaded_by)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            RETURNING *
            """,
            submission_id, user["facility_id"], file.filename, str(stored_path),
            file.content_type, len(contents), hashlib.sha256(contents).hexdigest(), user["user_id"],
        )
        actor_email = await conn.fetchval("SELECT email FROM users WHERE user_id = $1", user["user_id"])
        await log_action(
            conn, facility_id=user["facility_id"], actor_id=user["user_id"], actor_email=actor_email,
            action="evidence_uploaded", entity_type="evidence_file", entity_id=str(row["file_id"]),
            detail={"filename": file.filename, "submission_id": submission_id},
        )
        result = dict(row)
        result.pop("stored_path", None)
        return result


@router.get("/evidence/{file_id}")
async def download_evidence(file_id: str, user: dict = Depends(require_user)):
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM evidence_files WHERE file_id = $1 AND facility_id = $2",
            file_id, user["facility_id"],
        )
        if not row:
            raise HTTPException(404, "File not found.")
        path = Path(row["stored_path"])
        if not path.exists():
            raise HTTPException(410, "File is no longer available on disk.")
        return FileResponse(path, media_type=row["content_type"], filename=row["original_filename"])
