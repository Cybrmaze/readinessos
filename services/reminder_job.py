"""
Scheduled job (run every 15 min by systemd timer, see readinessos-reminders.
timer): generates future occurrences, refreshes due/overdue statuses, and
sends every reminder/escalation exactly once per (occurrence, channel,
stage) -- enforced by the notifications table's own UNIQUE constraint, not
just in-process logic, so a retry or overlapping run can never double-send.

Stages:
  reminder_7d      -- 7 days before due, to the assignee (or workspace officer)
  due               -- the day it's due, to the assignee
  overdue_officer   -- 1+ day overdue, to the workspace officer
  overdue_admin     -- 3+ days overdue, to the facility admin(s)
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import os

import asyncpg

from services.magic_links import create_magic_link
from services.notify_email import send_email
from services.notify_sms import send_sms
from services.scheduling import generate_all_occurrences, refresh_overdue_statuses

_logger = logging.getLogger("rxos.reminder_job")


def _app_url() -> str:
    return os.environ.get("RXOS_APP_URL", "https://co.myclg.net")


async def _recipients_for_stage(conn: asyncpg.Connection, occurrence: dict, stage: str) -> list[dict]:
    facility_id = occurrence["facility_id"]
    workspace = occurrence["workspace"]
    if stage in ("reminder_7d", "due"):
        if occurrence["assignee_id"]:
            rows = await conn.fetch("SELECT * FROM users WHERE user_id = $1 AND active = true", occurrence["assignee_id"])
        else:
            rows = await conn.fetch(
                "SELECT * FROM users WHERE facility_id = $1 AND workspace = $2 AND role = 'officer' AND active = true",
                facility_id, workspace,
            )
    elif stage == "overdue_officer":
        rows = await conn.fetch(
            "SELECT * FROM users WHERE facility_id = $1 AND workspace = $2 AND role = 'officer' AND active = true",
            facility_id, workspace,
        )
    else:  # overdue_admin
        rows = await conn.fetch(
            "SELECT * FROM users WHERE facility_id = $1 AND role = 'admin' AND active = true", facility_id
        )
    return [dict(r) for r in rows]


def _message_for_stage(stage: str, occurrence: dict, link_url: str) -> tuple[str, str]:
    title, workspace, due_at = occurrence["title"], occurrence["workspace"], occurrence["due_at"]
    due_str = due_at.strftime("%Y-%m-%d")
    if stage == "reminder_7d":
        body = f"\"{title}\" ({workspace}) is due on {due_str}. Complete it here: {link_url}"
        return (f"ReadinessOS: {title} due {due_str}", body)
    if stage == "due":
        body = f"\"{title}\" ({workspace}) is due today ({due_str}). Complete it here: {link_url}"
        return (f"ReadinessOS: {title} is due today", body)
    if stage == "overdue_officer":
        body = (f"\"{title}\" ({workspace}) was due {due_str} and has not been completed. "
                f"Complete it here: {link_url}")
        return (f"ReadinessOS: {title} is OVERDUE", body)
    body = (f"\"{title}\" ({workspace}) was due {due_str} and remains incomplete. "
            f"This has been escalated to you as facility administrator. View it here: {link_url}")
    return (f"ReadinessOS: OVERDUE -- {title} ({workspace})", body)


async def _send_stage(conn: asyncpg.Connection, occurrence: dict, stage: str) -> int:
    sent_count = 0
    for recipient in await _recipients_for_stage(conn, occurrence, stage):
        wants_sms = bool(recipient.get("sms_opt_in") and recipient.get("phone"))

        already = await conn.fetchval(
            "SELECT 1 FROM notifications WHERE occurrence_id = $1 AND channel = 'email' AND stage = $2 AND recipient = $3",
            occurrence["occurrence_id"], stage, recipient["email"],
        )
        already_sms = None
        if wants_sms:
            already_sms = await conn.fetchval(
                "SELECT 1 FROM notifications WHERE occurrence_id = $1 AND channel = 'sms' AND stage = $2 AND recipient = $3",
                occurrence["occurrence_id"], stage, recipient["phone"],
            )
        if already and (not wants_sms or already_sms):
            continue  # every applicable channel already sent for this stage -- nothing to do, no link needed

        raw_token = await create_magic_link(
            conn,
            user_id=recipient["user_id"],
            facility_id=occurrence["facility_id"],
            occurrence_id=occurrence["occurrence_id"],
        )
        link_url = f"{_app_url()}/m?token={raw_token}"
        subject, body = _message_for_stage(stage, occurrence, link_url)

        if not already:
            ok, err = send_email(recipient["email"], recipient["name"], subject, body)
            await conn.execute(
                """
                INSERT INTO notifications (occurrence_id, facility_id, channel, stage, recipient, status, error)
                VALUES ($1, $2, 'email', $3, $4, $5, $6)
                ON CONFLICT (occurrence_id, channel, stage) DO NOTHING
                """,
                occurrence["occurrence_id"], occurrence["facility_id"], stage, recipient["email"],
                "sent" if ok else "failed", err,
            )
            if ok:
                sent_count += 1

        if wants_sms:
            if not already_sms:
                ok, provider_id, err = await send_sms(recipient["phone"], f"{subject}: {body}")
                await conn.execute(
                    """
                    INSERT INTO notifications (occurrence_id, facility_id, channel, stage, recipient, status, provider_id, error)
                    VALUES ($1, $2, 'sms', $3, $4, $5, $6, $7)
                    ON CONFLICT (occurrence_id, channel, stage) DO NOTHING
                    """,
                    occurrence["occurrence_id"], occurrence["facility_id"], stage, recipient["phone"],
                    "sent" if ok else "failed", provider_id, err,
                )
    return sent_count


async def run_reminder_cycle(pool: asyncpg.Pool) -> dict:
    now = datetime.now(timezone.utc)
    async with pool.acquire() as conn:
        created = await generate_all_occurrences(conn)
        status_changes = await refresh_overdue_statuses(conn)

        candidates = await conn.fetch(
            """
            SELECT o.*, a.title, a.workspace, a.assignee_id
            FROM occurrences o JOIN activities a ON a.activity_id = o.activity_id
            WHERE o.status != 'complete'
              AND (
                    (o.status = 'upcoming' AND o.due_at <= $1)
                 OR (o.status = 'due')
                 OR (o.status = 'overdue')
              )
            """,
            now + timedelta(days=7),
        )

        sent_total = 0
        for occ in candidates:
            occ = dict(occ)
            days_until_due = (occ["due_at"] - now).days
            days_overdue = (now - occ["due_at"]).days

            if occ["status"] == "upcoming" and 0 <= days_until_due <= 7:
                sent_total += await _send_stage(conn, occ, "reminder_7d")
            elif occ["status"] == "due":
                sent_total += await _send_stage(conn, occ, "due")
            elif occ["status"] == "overdue":
                sent_total += await _send_stage(conn, occ, "overdue_officer")
                if days_overdue >= 3:
                    sent_total += await _send_stage(conn, occ, "overdue_admin")

        return {"occurrences_created": created, **status_changes, "notifications_sent": sent_total}
