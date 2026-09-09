"""
Run with the real service environment loaded, e.g.:
  cd /opt/readinessos && set -a && source .env && set +a && venv/bin/pytest tests/ -v
"""
from __future__ import annotations

import asyncio
import uuid

import pytest


def auth_headers(token):
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------- Auth + facility setup

def test_setup_facility_creates_working_admin(client, facility):
    resp = client.get("/auth/me", headers=auth_headers(facility["token"]))
    assert resp.status_code == 200
    assert resp.json()["role"] == "admin"


def test_login_wrong_password_rejected(client, facility):
    resp = client.post("/auth/login", json={"email": facility["admin_email"], "password": "wrongpassword"})
    assert resp.status_code == 401


def test_login_nonexistent_email_same_error_as_wrong_password(client):
    resp = client.post("/auth/login", json={"email": "nobody@pytest.readinessos.example", "password": "whatever123"})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid email or password."


def test_setup_facility_duplicate_email_rejected(client, facility):
    resp = client.post("/auth/setup-facility", json={
        "facility_name": "PYTEST-dup", "admin_name": "X", "admin_email": facility["admin_email"], "password": "password123",
    })
    assert resp.status_code == 409


# ---------------------------------------------------------------- Deactivation takes effect immediately

def test_deactivated_user_token_immediately_rejected(client, facility, unique_email):
    staff = client.post("/users", headers=auth_headers(facility["token"]), json={
        "name": "Staff One", "email": unique_email, "password": "staffpassword1", "role": "officer", "workspace": "IC",
    })
    assert staff.status_code == 200
    staff_token = client.post("/auth/login", json={"email": unique_email, "password": "staffpassword1"}).json()["access_token"]
    assert client.get("/auth/me", headers=auth_headers(staff_token)).status_code == 200

    staff_id = staff.json()["user_id"]
    client.patch(f"/users/{staff_id}", headers=auth_headers(facility["token"]), json={"active": False})
    resp = client.get("/auth/me", headers=auth_headers(staff_token))
    assert resp.status_code == 401


# ---------------------------------------------------------------- Last-admin protection

def test_cannot_demote_sole_admin(client, facility):
    resp = client.patch(f"/users/{facility['user_id']}", headers=auth_headers(facility["token"]), json={"role": "officer", "workspace": "IC"})
    assert resp.status_code == 409


def test_cannot_deactivate_sole_admin(client, facility):
    resp = client.patch(f"/users/{facility['user_id']}", headers=auth_headers(facility["token"]), json={"active": False})
    assert resp.status_code == 409


def test_can_demote_admin_when_another_admin_exists(client, facility, unique_email):
    second = client.post("/users", headers=auth_headers(facility["token"]), json={
        "name": "Second Admin", "email": unique_email, "password": "secondpassword1", "role": "admin",
    })
    assert second.status_code == 200
    resp = client.patch(f"/users/{facility['user_id']}", headers=auth_headers(facility["token"]), json={"role": "officer", "workspace": "IC"})
    assert resp.status_code == 200


# ---------------------------------------------------------------- Password reset lifecycle

def test_forgot_password_same_response_for_real_and_fake_email(client, facility):
    real = client.post("/auth/forgot-password", json={"email": facility["admin_email"]})
    fake = client.post("/auth/forgot-password", json={"email": "definitely-nobody@pytest.readinessos.example"})
    assert real.status_code == 200 and fake.status_code == 200
    assert real.json() == fake.json()


def test_password_reset_link_is_single_use(client, facility):
    from services.magic_links import create_magic_link

    async def make_token():
        conn = await _connect()
        try:
            return await create_magic_link(
                conn, user_id=facility["user_id"], facility_id=facility["facility_id"], purpose="password_reset",
            )
        finally:
            await conn.close()

    token = _run(make_token())

    first = client.post("/auth/reset-password", json={"token": token, "new_password": "resetpassword123"})
    assert first.status_code == 200

    second = client.post("/auth/reset-password", json={"token": token, "new_password": "anotherpassword456"})
    assert second.status_code == 401

    ok = client.post("/auth/login", json={"email": facility["admin_email"], "password": "resetpassword123"})
    assert ok.status_code == 200


def test_change_password_requires_correct_current_password(client, facility):
    resp = client.post("/auth/change-password", headers=auth_headers(facility["token"]), json={
        "current_password": "wrongcurrent", "new_password": "newpassword999",
    })
    assert resp.status_code == 401


def test_admin_can_reset_a_staff_password(client, facility, unique_email):
    staff = client.post("/users", headers=auth_headers(facility["token"]), json={
        "name": "Reset Target", "email": unique_email, "password": "originalpassword1", "role": "officer", "workspace": "EOC",
    }).json()
    client.patch(f"/users/{staff['user_id']}", headers=auth_headers(facility["token"]), json={"new_password": "adminsetpassword1"})
    resp = client.post("/auth/login", json={"email": unique_email, "password": "adminsetpassword1"})
    assert resp.status_code == 200


# ---------------------------------------------------------------- RBAC / facility scoping

def test_officer_only_sees_own_workspace_occurrences(client, facility, unique_email):
    client.post("/activities", headers=auth_headers(facility["token"]), json={
        "workspace": "IC", "title": "IC task", "cadence": "weekly", "checklist": ["Q1"],
    })
    client.post("/activities", headers=auth_headers(facility["token"]), json={
        "workspace": "EOC", "title": "EOC task", "cadence": "weekly", "checklist": ["Q1"],
    })
    officer_token = client.post("/users", headers=auth_headers(facility["token"]), json={
        "name": "IC Officer", "email": unique_email, "password": "officerpassword1", "role": "officer", "workspace": "IC",
    })
    officer_token = client.post("/auth/login", json={"email": unique_email, "password": "officerpassword1"}).json()["access_token"]

    occs = client.get("/occurrences", headers=auth_headers(officer_token)).json()
    assert all(o["workspace"] == "IC" for o in occs)
    assert len(occs) > 0


def test_cross_facility_occurrence_access_returns_404(client, facility, unique_email):
    other = client.post("/auth/setup-facility", json={
        "facility_name": f"PYTEST-{uuid.uuid4().hex[:8]}", "admin_name": "Other Admin",
        "admin_email": unique_email, "password": "otherpassword123",
    }).json()
    act = client.post("/activities", headers=auth_headers(facility["token"]), json={
        "workspace": "PI", "title": "Facility A task", "cadence": "monthly", "checklist": ["Q1"],
    }).json()
    occ = client.get("/occurrences", headers=auth_headers(facility["token"])).json()
    occ_id = [o for o in occ if o["activity_id"] == act["activity_id"]][0]["occurrence_id"]

    resp = client.get(f"/occurrences/{occ_id}", headers=auth_headers(other["access_token"]))
    assert resp.status_code == 404


# ---------------------------------------------------------------- Occurrence lifecycle + immutability

def test_submit_locks_occurrence_and_blocks_double_submit(client, facility):
    act = client.post("/activities", headers=auth_headers(facility["token"]), json={
        "workspace": "MM", "title": "Lock test", "cadence": "monthly", "checklist": ["Q1", "Q2"],
    }).json()
    occs = client.get("/occurrences", headers=auth_headers(facility["token"])).json()
    occ_id = [o for o in occs if o["activity_id"] == act["activity_id"]][0]["occurrence_id"]

    submit = client.post(f"/occurrences/{occ_id}/submit", headers=auth_headers(facility["token"]), json={
        "answers": {"0": "Yes", "1": "No"}, "signature_name": "Pytest Admin",
    })
    assert submit.status_code == 200

    again = client.post(f"/occurrences/{occ_id}/submit", headers=auth_headers(facility["token"]), json={
        "answers": {"0": "Yes"}, "signature_name": "Pytest Admin",
    })
    assert again.status_code == 409


def test_submissions_table_rejects_direct_sql_mutation():
    async def attempt():
        import asyncpg
        conn = await _connect()
        try:
            try:
                await conn.execute("UPDATE submissions SET notes = 'tampered' WHERE true")
                return "no error raised"
            except asyncpg.RaiseError as e:
                return str(e)
        finally:
            await conn.close()

    result = _run(attempt())
    assert "append-only" in result


def test_evidence_upload_rejects_spoofed_content_type(client, facility):
    act = client.post("/activities", headers=auth_headers(facility["token"]), json={
        "workspace": "IC", "title": "Evidence test", "cadence": "weekly", "checklist": ["Q1"],
    }).json()
    occs = client.get("/occurrences", headers=auth_headers(facility["token"])).json()
    occ_id = [o for o in occs if o["activity_id"] == act["activity_id"]][0]["occurrence_id"]
    sub = client.post(f"/occurrences/{occ_id}/submit", headers=auth_headers(facility["token"]), json={
        "answers": {"0": "Yes"}, "signature_name": "Pytest Admin",
    }).json()

    resp = client.post(
        f"/submissions/{sub['submission_id']}/evidence", headers=auth_headers(facility["token"]),
        files={"file": ("fake.png", b"not a real png", "image/png")},
    )
    assert resp.status_code == 400

    real_png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20
    resp2 = client.post(
        f"/submissions/{sub['submission_id']}/evidence", headers=auth_headers(facility["token"]),
        files={"file": ("real.png", real_png, "image/png")},
    )
    assert resp2.status_code == 200


# ---------------------------------------------------------------- Rate limiting

def test_login_rate_limit_triggers_429(client, monkeypatch):
    # This is the one test that needs the limiter actually armed -- every
    # other test relies on it being disabled (see conftest.py).
    monkeypatch.setenv("RXOS_DISABLE_RATE_LIMIT", "0")
    email = f"{uuid.uuid4().hex[:12]}@pytest.readinessos.example"
    for _ in range(10):
        client.post("/auth/login", json={"email": email, "password": "wrongpassword"})
    resp = client.post("/auth/login", json={"email": email, "password": "wrongpassword"})
    assert resp.status_code == 429


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _connect():
    """A standalone connection, deliberately not services.database.get_pool()
    -- that pool is bound to the event loop TestClient's app runs its
    lifespan in, which is not the freshly created loop _run() uses here."""
    import os

    import asyncpg

    return await asyncpg.connect(
        host=os.environ["RXOS_DB_HOST"], port=int(os.environ["RXOS_DB_PORT"]),
        user=os.environ["RXOS_DB_USER"], password=os.environ["RXOS_DB_PASSWORD"],
        database=os.environ["RXOS_DB_NAME"],
    )
