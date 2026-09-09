"""
ReadinessOS reminder/escalation SMS -- Telnyx Messages API. Much simpler
than telnyx_fax.py (no media upload, no flatten, no poll-to-terminal loop
needed for an SMS): fire the send, record the provider's message id.

2026-09-08 status: Telnyx account is real and funded, but 10DLC campaign
registration for TELNYX_MESSAGING_PROFILE_ID has not cleared yet, so a send
attempted before approval will fail at Telnyx's end. This module still
performs a real send attempt and returns the real (sent/failed) outcome
either way -- it does not pretend to succeed. Callers must treat email as
the reliable channel and SMS as best-effort until Telnyx confirms 10DLC
approval.
"""
from __future__ import annotations

import logging
import os

import httpx

API_BASE = "https://api.telnyx.com/v2"
_logger = logging.getLogger("rxos.notify_sms")


async def send_sms(to_number: str, body: str) -> tuple[bool, str | None, str | None]:
    """Returns (sent, provider_message_id, error). Never raises."""
    api_key = os.environ.get("TELNYX_API_KEY", "")
    profile_id = os.environ.get("TELNYX_MESSAGING_PROFILE_ID", "")
    from_number = os.environ.get("TELNYX_FROM_NUMBER", "")
    if not api_key or not from_number or not to_number:
        _logger.warning("sms skipped -- Telnyx not configured or no recipient")
        return False, None, "Telnyx not configured or no recipient"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{API_BASE}/messages",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "from": from_number,
                    "to": to_number,
                    "text": body,
                    **({"messaging_profile_id": profile_id} if profile_id else {}),
                },
            )
            if resp.status_code not in (200, 202):
                return False, None, f"Telnyx send failed ({resp.status_code}): {resp.text[:300]}"
            msg_id = resp.json().get("data", {}).get("id")
            return True, msg_id, None
    except Exception as e:
        _logger.warning("sms send failed: %s", e)
        return False, None, str(e)[:300]
