"""
In-memory sliding-window rate limiter. Deliberately not Redis-backed: this
service runs as a single uvicorn process (no --workers), so in-memory state
is consistent and this avoids adding a shared-infra dependency to a
product that is otherwise fully isolated from the rest of the CLG
platform. If this service is ever scaled to multiple worker processes,
this limiter would need to move to a shared store first.
"""
from __future__ import annotations

import os
import time
from collections import defaultdict

from fastapi import HTTPException

_hits: dict[str, list[float]] = defaultdict(list)


def check_rate_limit(key: str, max_requests: int, window_seconds: float) -> None:
    # Escape hatch for the automated test suite only -- never set in the
    # real .env. Without this, the test suite's own facility/login fixtures
    # (many calls in a tight loop) trip the same limits a real attacker
    # would, which would make the limiter untestable without weakening it.
    if os.environ.get("RXOS_DISABLE_RATE_LIMIT") == "1":
        return
    now = time.monotonic()
    bucket = _hits[key]
    cutoff = now - window_seconds
    while bucket and bucket[0] < cutoff:
        bucket.pop(0)
    if len(bucket) >= max_requests:
        if not bucket:
            del _hits[key]
        raise HTTPException(429, "Too many requests. Please wait and try again.")
    bucket.append(now)
    # bound unbounded growth from one-off distinct keys (e.g. many unique
    # IPs hitting a public endpoint once each) -- a real prune, not just a
    # size cap, since dropping the oldest key here could drop a key that's
    # still actively within its own window.
    if len(_hits) > 10_000:
        for k in [k for k, v in _hits.items() if not v or v[-1] < cutoff]:
            del _hits[k]


def client_ip(request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"
