"""Append-only audit_log writer -- every state-changing action goes through
this single function so no route can forget to record one."""
from __future__ import annotations

import json
from typing import Any, Optional

import asyncpg


async def log_action(
    conn: asyncpg.Connection,
    *,
    facility_id: str,
    actor_id: Optional[str],
    actor_email: str,
    action: str,
    entity_type: str,
    entity_id: Optional[str] = None,
    detail: Optional[dict[str, Any]] = None,
) -> None:
    await conn.execute(
        """
        INSERT INTO audit_log (facility_id, actor_id, actor_email, action, entity_type, entity_id, detail)
        VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
        """,
        facility_id, actor_id, actor_email, action, entity_type, entity_id,
        json.dumps(detail or {}),
    )
