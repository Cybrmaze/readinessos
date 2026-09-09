-- ReadinessOS Facility Compliance -- schema.sql
-- Standalone database (readinessos), own OS user, own Postgres role.
-- Zero foreign keys to, or shared tables with, the CLG facility platform's
-- own `clg` database -- separate trust domain, separate product, same
-- isolation principle already applied to clg-bf (see clg-bf's own
-- services/auth.py docstring).
--
-- Five workspaces only: IC, EOC, EAP, MM, PI. This schema intentionally has
-- no table or field for operational emergency forms, medication storage,
-- medication administration, or medication destruction -- readiness_activities
-- and readiness_submissions record that a survey/audit/drill/review happened,
-- never the underlying clinical/operational act itself.

CREATE EXTENSION IF NOT EXISTS "pgcrypto";

CREATE TABLE facilities (
    facility_id     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name            varchar(255) NOT NULL,
    timezone        varchar(64) NOT NULL DEFAULT 'America/Los_Angeles',
    admin_name      varchar(255),
    admin_email     varchar(255),
    admin_phone     varchar(32),
    active          boolean NOT NULL DEFAULT true,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

-- One facility, many users. role='admin' sees everything + admin reporting;
-- role='officer' is assigned one of the five workspaces; role='staff' can be
-- assigned individual activities without owning a whole workspace.
CREATE TABLE users (
    user_id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    facility_id     uuid NOT NULL REFERENCES facilities(facility_id) ON DELETE CASCADE,
    name            varchar(255) NOT NULL,
    email           varchar(255) NOT NULL,
    phone           varchar(32),
    password_hash   text NOT NULL,
    role            varchar(20) NOT NULL CHECK (role IN ('admin','officer','staff')),
    workspace       varchar(10) CHECK (workspace IN ('IC','EOC','EAP','MM','PI')),
    email_opt_in    boolean NOT NULL DEFAULT true,
    sms_opt_in      boolean NOT NULL DEFAULT false,
    active          boolean NOT NULL DEFAULT true,
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (facility_id, email)
);

-- The recurring schedule/template -- "what needs to happen, how often, by whom."
CREATE TABLE activities (
    activity_id     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    facility_id     uuid NOT NULL REFERENCES facilities(facility_id) ON DELETE CASCADE,
    workspace       varchar(10) NOT NULL CHECK (workspace IN ('IC','EOC','EAP','MM','PI')),
    title           varchar(255) NOT NULL,
    cadence         varchar(50) NOT NULL,
    assignee_id     uuid REFERENCES users(user_id) ON DELETE SET NULL,
    checklist       jsonb NOT NULL DEFAULT '[]'::jsonb,  -- ordered list of question strings
    active          boolean NOT NULL DEFAULT true,
    created_by      uuid REFERENCES users(user_id) ON DELETE SET NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

-- One row per due occurrence of an activity -- "this specific instance, due
-- this specific date, in this specific state." Separate from `activities`
-- (the template) so cadence/checklist edits never rewrite history.
CREATE TABLE occurrences (
    occurrence_id   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    activity_id     uuid NOT NULL REFERENCES activities(activity_id) ON DELETE CASCADE,
    facility_id     uuid NOT NULL REFERENCES facilities(facility_id) ON DELETE CASCADE,
    due_at          timestamptz NOT NULL,
    status          varchar(20) NOT NULL DEFAULT 'upcoming'
                        CHECK (status IN ('upcoming','due','overdue','complete')),
    draft           jsonb,          -- in-progress answers/notes, mutable until locked
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (activity_id, due_at)
);

-- The locked, immutable completion record. Once inserted, never updated or
-- deleted -- enforced by trigger below, same pattern already proven on
-- orion_alerts in the main CLG platform (block_orion_alert_deletion).
CREATE TABLE submissions (
    submission_id   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    occurrence_id   uuid NOT NULL REFERENCES occurrences(occurrence_id) ON DELETE RESTRICT,
    facility_id     uuid NOT NULL REFERENCES facilities(facility_id) ON DELETE RESTRICT,
    submitted_by    uuid NOT NULL REFERENCES users(user_id) ON DELETE RESTRICT,
    answers         jsonb NOT NULL,          -- {questionIndex: "Yes"|"No"|"N/A"|"Needs action"}
    notes           text,
    signature_name  varchar(255) NOT NULL,
    submitted_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (occurrence_id)
);

CREATE OR REPLACE FUNCTION block_submission_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'submissions is append-only -- % is not permitted', TG_OP;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER submissions_no_update
    BEFORE UPDATE ON submissions
    FOR EACH ROW EXECUTE FUNCTION block_submission_mutation();

CREATE TRIGGER submissions_no_delete
    BEFORE DELETE ON submissions
    FOR EACH ROW EXECUTE FUNCTION block_submission_mutation();

-- Evidence files attached to a submission (photos, supporting documents).
-- Real file storage on disk under /opt/readinessos/uploads/{facility_id}/,
-- this table stores the pointer + integrity hash, never the bytes.
CREATE TABLE evidence_files (
    file_id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    submission_id   uuid NOT NULL REFERENCES submissions(submission_id) ON DELETE RESTRICT,
    facility_id     uuid NOT NULL REFERENCES facilities(facility_id) ON DELETE RESTRICT,
    original_filename varchar(255) NOT NULL,
    stored_path     text NOT NULL,
    content_type    varchar(128) NOT NULL,
    size_bytes      integer NOT NULL,
    sha256          varchar(64) NOT NULL,
    uploaded_by     uuid NOT NULL REFERENCES users(user_id) ON DELETE RESTRICT,
    uploaded_at     timestamptz NOT NULL DEFAULT now()
);

-- Append-only audit trail for every state-changing action -- logins,
-- activity created/edited, occurrence started/submitted, evidence attached,
-- notification sent, escalation fired, user added/deactivated. This is
-- separate from `submissions` (the compliance record itself) -- this is the
-- platform's own action log, same shape as document_audit_log in the main
-- CLG platform.
CREATE TABLE audit_log (
    audit_id        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    facility_id     uuid NOT NULL REFERENCES facilities(facility_id) ON DELETE RESTRICT,
    actor_id        uuid REFERENCES users(user_id) ON DELETE SET NULL,
    actor_email     varchar(255) NOT NULL,   -- denormalized: survives actor deactivation/deletion
    action          varchar(64) NOT NULL,
    entity_type     varchar(32) NOT NULL,
    entity_id       uuid,
    detail          jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE OR REPLACE FUNCTION block_audit_log_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'audit_log is append-only -- % is not permitted', TG_OP;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER audit_log_no_update
    BEFORE UPDATE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION block_audit_log_mutation();

CREATE TRIGGER audit_log_no_delete
    BEFORE DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION block_audit_log_mutation();

-- One row per real notification attempt (not per reminder "type") -- every
-- send is recorded whether it succeeds or fails, matching the spec's own
-- "every attempt is recorded" language literally.
CREATE TABLE notifications (
    notification_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    occurrence_id   uuid NOT NULL REFERENCES occurrences(occurrence_id) ON DELETE CASCADE,
    facility_id     uuid NOT NULL REFERENCES facilities(facility_id) ON DELETE CASCADE,
    channel         varchar(10) NOT NULL CHECK (channel IN ('email','sms')),
    stage           varchar(20) NOT NULL CHECK (stage IN ('reminder_7d','due','overdue_officer','overdue_admin')),
    recipient       varchar(255) NOT NULL,
    status          varchar(20) NOT NULL CHECK (status IN ('sent','failed')),
    provider_id     varchar(128),
    error           text,
    sent_at         timestamptz NOT NULL DEFAULT now(),
    -- one real attempt per (occurrence, channel, stage) -- the escalation
    -- job must not re-send the same stage twice for the same occurrence.
    UNIQUE (occurrence_id, channel, stage)
);

-- Magic-link auth for reminder/escalation SMS+email -- see
-- services/magic_links.py's module docstring for the security model
-- (opaque token, only its hash stored, multi-use until expiry).
CREATE TABLE magic_links (
    link_id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    facility_id     uuid NOT NULL REFERENCES facilities(facility_id) ON DELETE CASCADE,
    user_id         uuid NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    occurrence_id   uuid REFERENCES occurrences(occurrence_id) ON DELETE CASCADE,
    token_hash      varchar(64) NOT NULL UNIQUE,
    purpose         varchar(20) NOT NULL DEFAULT 'reminder' CHECK (purpose IN ('reminder','password_reset')),
    expires_at      timestamptz NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    last_used_at    timestamptz
);

CREATE INDEX idx_magic_links_token_hash ON magic_links(token_hash);
CREATE INDEX idx_users_facility ON users(facility_id);
CREATE INDEX idx_activities_facility ON activities(facility_id);
CREATE INDEX idx_occurrences_facility_status ON occurrences(facility_id, status);
CREATE INDEX idx_occurrences_due ON occurrences(due_at) WHERE status != 'complete';
CREATE INDEX idx_submissions_facility ON submissions(facility_id);
CREATE INDEX idx_evidence_submission ON evidence_files(submission_id);
CREATE INDEX idx_audit_log_facility ON audit_log(facility_id, created_at DESC);
CREATE INDEX idx_notifications_occurrence ON notifications(occurrence_id);
