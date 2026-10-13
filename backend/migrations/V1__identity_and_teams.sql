-- F01.01 — Identity, RBAC audit trail, teams and memberships.
-- Local credentials are approved. This migration intentionally seeds role
-- definitions only; it never creates a user, password, token, or real email.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    username VARCHAR(64) NOT NULL,
    email VARCHAR(320) NOT NULL,
    display_name VARCHAR(160) NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ck_users_username_not_blank CHECK (btrim(username) <> ''),
    CONSTRAINT ck_users_email_not_blank CHECK (btrim(email) <> ''),
    CONSTRAINT ck_users_display_name_not_blank CHECK (btrim(display_name) <> ''),
    CONSTRAINT ck_users_password_hash_not_blank CHECK (btrim(password_hash) <> '')
);

CREATE UNIQUE INDEX uq_users_username_normalized ON users (lower(username));
CREATE UNIQUE INDEX uq_users_email_normalized ON users (lower(email));
CREATE INDEX ix_users_active ON users (is_active) WHERE is_active;

-- Refresh tokens are never stored in plaintext. A session row contains only
-- the keyed SHA-256 digest produced by the token adapter. Rotation/revocation
-- closes a row instead of deleting authentication history.
CREATE TABLE refresh_sessions (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users (id),
    token_digest CHAR(64) NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    revoked_at TIMESTAMPTZ NULL,
    CONSTRAINT uq_refresh_sessions_token_digest UNIQUE (token_digest),
    CONSTRAINT ck_refresh_sessions_digest_format CHECK (
        token_digest ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT ck_refresh_sessions_expiry CHECK (expires_at > created_at),
    CONSTRAINT ck_refresh_sessions_revoked_after_created CHECK (
        revoked_at IS NULL OR revoked_at >= created_at
    )
);

CREATE INDEX ix_refresh_sessions_active_user
    ON refresh_sessions (user_id, expires_at)
    WHERE revoked_at IS NULL;
CREATE INDEX ix_refresh_sessions_active_expiry
    ON refresh_sessions (expires_at)
    WHERE revoked_at IS NULL;

CREATE TABLE roles (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    code VARCHAR(32) NOT NULL,
    description VARCHAR(255) NOT NULL,
    is_elevated BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_roles_code UNIQUE (code),
    CONSTRAINT ck_roles_code_not_blank CHECK (btrim(code) <> '')
);

-- An assignment is immutable audit history. Revocation closes the current
-- assignment; it never deletes a row. The partial unique index permits only
-- one active assignment of a role to a user at a time.
CREATE TABLE user_roles (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users (id),
    role_id UUID NOT NULL REFERENCES roles (id),
    granted_by_user_id UUID NOT NULL REFERENCES users (id),
    granted_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    revoked_by_user_id UUID NULL REFERENCES users (id),
    revoked_at TIMESTAMPTZ NULL,
    CONSTRAINT ck_user_roles_revocation_pair CHECK (
        (revoked_at IS NULL AND revoked_by_user_id IS NULL)
        OR (revoked_at IS NOT NULL AND revoked_by_user_id IS NOT NULL)
    ),
    CONSTRAINT ck_user_roles_revoked_after_granted CHECK (
        revoked_at IS NULL OR revoked_at >= granted_at
    )
);

CREATE UNIQUE INDEX uq_user_roles_active_assignment
    ON user_roles (user_id, role_id)
    WHERE revoked_at IS NULL;
CREATE INDEX ix_user_roles_active_user ON user_roles (user_id) WHERE revoked_at IS NULL;
CREATE INDEX ix_user_roles_role ON user_roles (role_id) WHERE revoked_at IS NULL;

CREATE TABLE teams (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(160) NOT NULL,
    code VARCHAR(32) NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_teams_name UNIQUE (name),
    CONSTRAINT uq_teams_code UNIQUE (code),
    CONSTRAINT ck_teams_name_not_blank CHECK (btrim(name) <> ''),
    CONSTRAINT ck_teams_code_not_blank CHECK (btrim(code) <> '')
);

CREATE TABLE team_members (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    team_id UUID NOT NULL REFERENCES teams (id),
    user_id UUID NOT NULL REFERENCES users (id),
    joined_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    left_at TIMESTAMPTZ NULL,
    CONSTRAINT ck_team_members_left_after_joined CHECK (left_at IS NULL OR left_at >= joined_at)
);

CREATE UNIQUE INDEX uq_team_members_active_membership
    ON team_members (team_id, user_id)
    WHERE left_at IS NULL;
CREATE INDEX ix_team_members_active_user ON team_members (user_id) WHERE left_at IS NULL;
CREATE INDEX ix_team_members_active_team ON team_members (team_id) WHERE left_at IS NULL;

INSERT INTO roles (code, description, is_elevated)
VALUES
    ('SYSTEM_ADMIN', 'System-level role and email administration', TRUE),
    ('HR', 'Asset and operational administration', TRUE),
    ('TEAM_LEAD_ASSET', 'Asset-team operational lead', FALSE),
    ('SHIFT_LEAD', 'Assigned shift operational lead', FALSE),
    ('TEAM_HEAD', 'Responsible-team asset read scope', FALSE)
ON CONFLICT (code) DO NOTHING;
