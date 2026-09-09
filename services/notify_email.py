"""
ReadinessOS reminder/escalation email. Mailgun HTTPS API, mirrors
/opt/clg/services/customer_notifications.py::_send_email exactly (proven
pattern -- SMTP is confirmed network-blocked on this droplet). Fail-soft:
a failed/misconfigured send must never break the caller's flow, but every
attempt (success or failure) is recorded by the caller into `notifications`.
"""
from __future__ import annotations

import base64
import logging
import os
import urllib.parse
import urllib.request

_logger = logging.getLogger("rxos.notify_email")


def send_email(to_email: str, to_name: str, subject: str, body: str) -> tuple[bool, str | None]:
    """Returns (sent, error). Never raises."""
    try:
        api_key = os.environ.get("MAILGUN_API_KEY", "")
        domain = os.environ.get("MAILGUN_DOMAIN", "")
        base_url = os.environ.get("MAILGUN_BASE_URL", "https://api.mailgun.net")
        if not api_key or not domain or not to_email:
            _logger.warning("email skipped -- Mailgun not configured or no recipient")
            return False, "Mailgun not configured or no recipient"
        data = urllib.parse.urlencode({
            "from": f"ReadinessOS <postmaster@{domain}>",
            "to": f"{to_name} <{to_email}>" if to_name else to_email,
            "subject": subject,
            "text": body,
        }).encode()
        req = urllib.request.Request(f"{base_url}/v3/{domain}/messages", data=data, method="POST")
        req.add_header("Authorization", "Basic " + base64.b64encode(f"api:{api_key}".encode()).decode())
        with urllib.request.urlopen(req, timeout=15):
            return True, None
    except Exception as e:
        _logger.warning("email send failed: %s", e)
        return False, str(e)[:300]
