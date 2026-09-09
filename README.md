# ReadinessOS

[![Tests](https://github.com/Cybrmaze/readinessos/actions/workflows/tests.yml/badge.svg)](https://github.com/Cybrmaze/readinessos/actions/workflows/tests.yml)

Facility compliance tracker for surveys, inspections, audits, reviews, and drills across five workspaces: Infection Control, Environment of Care, Emergency Action Plan, Medication Management, and Quality & Performance Improvement.

ReadinessOS records that a survey/audit/drill actually happened -- who did it, when, and what they found -- with an immutable audit trail. It does not include operational forms used during actual emergencies, medication storage, medication administration, or medication destruction.

Deployed at [co.myclg.net](https://co.myclg.net).

## Stack

- FastAPI + asyncpg + PostgreSQL
- Password and magic-link authentication
- Mailgun (email) and Telnyx (SMS) for reminders and overdue escalation
- Standalone database and OS user, isolated from the rest of the CLG platform

## Development

```bash
pip install -r requirements.txt
set -a && source .env && set +a
uvicorn main:app --reload
```

## Tests

Run against a dedicated `readinessos_test` database (never production -- the audit trail is append-only, so test data written to the real database could never be cleaned up again):

```bash
set -a && source .env && set +a
pytest tests/ -v
```

CI runs the same suite against a fresh Postgres 16 container on every push and pull request to `main`.
