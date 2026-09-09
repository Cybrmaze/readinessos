"""
Real-DB test suite -- runs against the live readinessos database (same
pattern as the main CLG platform's own tests against clg), not a separate
test DB. All fixtures create clearly-tagged disposable data (facility
names prefixed PYTEST-, emails under @pytest.readinessos.example) and clean
up everything that is not append-only by design (submissions/audit_log
rows are immutable even to tests, by the same trigger that protects real
customer data -- a handful of leftover rows from test runs is expected
and harmless, exactly like real usage).
"""
from __future__ import annotations

import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Disabled by default for the whole suite -- the facility/login fixtures
# make many calls in a tight loop, which would trip the same limits a
# real attacker would. The one test that verifies the limiter itself
# (test_login_rate_limit_triggers_429) re-enables it for its own duration
# via monkeypatch. Set before `main` (and its routers) are imported.
os.environ["RXOS_DISABLE_RATE_LIMIT"] = "1"
os.environ.setdefault("RXOS_DB_HOST", "localhost")
os.environ.setdefault("RXOS_DB_PORT", "5433")
os.environ.setdefault("RXOS_DB_USER", "readinessos_app")
# Forced, not setdefault: tests must never run against the real
# "readinessos" database even if the real .env is sourced first (which is
# how DB host/port/user/password get here) -- audit_log and submissions
# are permanently append-only by design (see schema.sql), so any test data
# written to the real DB can never be cleaned up again. readinessos_test is
# a separate database, same schema, that tests are free to leave dirty.
os.environ["RXOS_DB_NAME"] = "readinessos_test"

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="session")
def client():
    from main import app
    with TestClient(app) as c:
        yield c


def make_email() -> str:
    """A callable, not a fixture -- a fixture's value is cached per test
    node and would hand out the SAME email to both the facility fixture's
    admin and a second user a test creates in its own body if both asked
    for it. Call this directly, as many times as distinct emails are
    needed within one test."""
    return f"{uuid.uuid4().hex[:12]}@pytest.readinessos.example"


@pytest.fixture
def unique_email():
    return make_email()


@pytest.fixture
def facility(client):
    """A fresh facility + admin user + access token for one test."""
    email = make_email()
    resp = client.post("/auth/setup-facility", json={
        "facility_name": f"PYTEST-{uuid.uuid4().hex[:8]}",
        "admin_name": "Pytest Admin",
        "admin_email": email,
        "password": "pytestpassword123",
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()
    return {
        "token": data["access_token"],
        "facility_id": data["facility_id"],
        "user_id": data["user_id"],
        "admin_email": email,
    }


def auth_headers(token):
    return {"Authorization": f"Bearer {token}"}
