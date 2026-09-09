"""Cadence stepping + occurrence generation. Turns an activity's recurring
schedule into concrete due `occurrences` rows, one step ahead at a time so
editing an activity's cadence never rewrites already-generated history."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import asyncpg

# Generate occurrences this far into the future from "now" on every run.
HORIZON_DAYS = 90

_STEP_DAYS = {
    "daily": 1,
    "weekly": 7,
    "biweekly": 14,
    "monthly": 30,
    "quarterly": 91,
    "semiannual": 182,
    "annual": 365,
}


def step_forward(dt: datetime, cadence: str) -> datetime:
    days = _STEP_DAYS.get(cadence)
    if days is None:
        raise ValueError(f"Unknown cadence: {cadence}")
    return dt + timedelta(days=days)


async def generate_occurrences_for_activity(conn: asyncpg.Connection, activity: asyncpg.Record) -> int:
    """Fills occurrences forward from the latest existing one (or now, for a
    brand-new activity) out to HORIZON_DAYS. Returns count created."""
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(days=HORIZON_DAYS)

    latest = await conn.fetchval(
        "SELECT max(due_at) FROM occurrences WHERE activity_id = $1", activity["activity_id"]
    )
    next_due = step_forward(latest, activity["cadence"]) if latest else now

    created = 0
    while next_due <= horizon:
        await conn.execute(
            """
            INSERT INTO occurrences (activity_id, facility_id, due_at)
            VALUES ($1, $2, $3)
            ON CONFLICT (activity_id, due_at) DO NOTHING
            """,
            activity["activity_id"], activity["facility_id"], next_due,
        )
        created += 1
        next_due = step_forward(next_due, activity["cadence"])
    return created


async def generate_all_occurrences(conn: asyncpg.Connection) -> int:
    """Run for every active activity -- called by the scheduled job so
    recurring activities keep producing future occurrences without any
    manual action."""
    activities = await conn.fetch("SELECT * FROM activities WHERE active = true")
    total = 0
    for activity in activities:
        total += await generate_occurrences_for_activity(conn, activity)
    return total


async def refresh_overdue_statuses(conn: asyncpg.Connection) -> dict:
    """status transitions, all mechanical (due_at vs now), never touching a
    submitted (status='complete') occurrence."""
    now = datetime.now(timezone.utc)
    to_due = await conn.fetch(
        "UPDATE occurrences SET status = 'due', updated_at = now() "
        "WHERE status = 'upcoming' AND due_at <= $1 RETURNING occurrence_id",
        now,
    )
    to_overdue = await conn.fetch(
        "UPDATE occurrences SET status = 'overdue', updated_at = now() "
        "WHERE status = 'due' AND due_at <= $1 RETURNING occurrence_id",
        now - timedelta(days=1),
    )
    return {"marked_due": len(to_due), "marked_overdue": len(to_overdue)}
