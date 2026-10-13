-- F02.01 — Asset catalog, physical custody allocation, and private evidence metadata.
-- Responsible Team is a nullable classification only; it never owns physical quantity.

CREATE TABLE asset_categories (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    code VARCHAR(64) NOT NULL,
    name VARCHAR(160) NOT NULL,
    description VARCHAR(500) NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_asset_categories_code UNIQUE (code),
    CONSTRAINT uq_asset_categories_name UNIQUE (name),
    CONSTRAINT ck_asset_categories_code_not_blank CHECK (btrim(code) <> ''),
    CONSTRAINT ck_asset_categories_name_not_blank CHECK (btrim(name) <> '')
);

CREATE TABLE assets (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    category_id UUID NOT NULL REFERENCES asset_categories (id),
    responsible_team_id UUID NULL REFERENCES teams (id),
    name VARCHAR(200) NOT NULL,
    description VARCHAR(1000) NULL,
    source VARCHAR(255) NOT NULL,
    total_quantity INTEGER NOT NULL,
    available_quantity INTEGER NOT NULL,
    created_by_user_id UUID NOT NULL REFERENCES users (id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ck_assets_name_not_blank CHECK (btrim(name) <> ''),
    CONSTRAINT ck_assets_source_not_blank CHECK (btrim(source) <> ''),
    CONSTRAINT ck_assets_total_quantity_non_negative CHECK (total_quantity >= 0),
    CONSTRAINT ck_assets_available_quantity_bounds CHECK (
        available_quantity >= 0
        AND available_quantity <= total_quantity
    )
);

CREATE INDEX ix_assets_category_name ON assets (category_id, name, id);
CREATE INDEX ix_assets_responsible_team ON assets (responsible_team_id, name, id)
    WHERE responsible_team_id IS NOT NULL;

CREATE TABLE custodies (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    asset_id UUID NOT NULL REFERENCES assets (id),
    custodian_user_id UUID NOT NULL REFERENCES users (id),
    assigned_by_user_id UUID NOT NULL REFERENCES users (id),
    status VARCHAR(16) NOT NULL,
    on_hand_quantity INTEGER NOT NULL,
    reserved_quantity INTEGER NOT NULL DEFAULT 0,
    temporarily_loaned_quantity INTEGER NOT NULL DEFAULT 0,
    condition VARCHAR(16) NOT NULL DEFAULT 'GOOD',
    condition_note VARCHAR(1000) NULL,
    assigned_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    confirmed_at TIMESTAMPTZ NULL,
    confirmed_by_user_id UUID NULL REFERENCES users (id),
    closed_at TIMESTAMPTZ NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ck_custodies_status CHECK (status IN ('PENDING', 'ACTIVE', 'CLOSED')),
    CONSTRAINT ck_custodies_condition CHECK (condition IN ('GOOD', 'DAMAGED', 'LOST')),
    CONSTRAINT ck_custodies_on_hand_non_negative CHECK (on_hand_quantity >= 0),
    CONSTRAINT ck_custodies_reserved_non_negative CHECK (reserved_quantity >= 0),
    CONSTRAINT ck_custodies_loaned_non_negative CHECK (temporarily_loaned_quantity >= 0),
    CONSTRAINT ck_custodies_quantity_partition CHECK (
        reserved_quantity + temporarily_loaned_quantity <= on_hand_quantity
    ),
    CONSTRAINT ck_custodies_confirmation_pair CHECK (
        (confirmed_at IS NULL AND confirmed_by_user_id IS NULL)
        OR (confirmed_at IS NOT NULL AND confirmed_by_user_id IS NOT NULL)
    ),
    CONSTRAINT ck_custodies_state_timestamps CHECK (
        (status = 'PENDING' AND confirmed_at IS NULL AND closed_at IS NULL)
        OR (status = 'ACTIVE' AND confirmed_at IS NOT NULL AND closed_at IS NULL)
        OR (status = 'CLOSED' AND confirmed_at IS NOT NULL AND closed_at IS NOT NULL)
    ),
    CONSTRAINT ck_custodies_confirmed_after_assigned CHECK (
        confirmed_at IS NULL OR confirmed_at >= assigned_at
    ),
    CONSTRAINT ck_custodies_closed_after_confirmed CHECK (
        closed_at IS NULL OR closed_at >= confirmed_at
    )
);

-- One actor has at most one current physical holding row for the same Asset.
CREATE UNIQUE INDEX uq_custodies_current_asset_custodian
    ON custodies (asset_id, custodian_user_id)
    WHERE status IN ('PENDING', 'ACTIVE');

CREATE INDEX ix_custodies_active_custodian_asset
    ON custodies (custodian_user_id, asset_id, id)
    WHERE status = 'ACTIVE';
CREATE INDEX ix_custodies_active_asset
    ON custodies (asset_id, custodian_user_id, id)
    WHERE status = 'ACTIVE';
CREATE INDEX ix_custodies_pending_reminder
    ON custodies (assigned_at, id)
    WHERE status = 'PENDING';

CREATE TABLE custody_evidence (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    custody_id UUID NOT NULL REFERENCES custodies (id),
    uploaded_by_user_id UUID NOT NULL REFERENCES users (id),
    evidence_type VARCHAR(32) NOT NULL,
    object_key VARCHAR(512) NOT NULL,
    content_type VARCHAR(32) NOT NULL,
    content_sha256 CHAR(64) NOT NULL,
    size_bytes BIGINT NOT NULL,
    note VARCHAR(1000) NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_custody_evidence_object_key UNIQUE (object_key),
    CONSTRAINT ck_custody_evidence_type CHECK (
        evidence_type IN ('ASSIGNMENT_CONFIRMATION')
    ),
    CONSTRAINT ck_custody_evidence_object_key_not_blank CHECK (btrim(object_key) <> ''),
    CONSTRAINT ck_custody_evidence_content_type CHECK (
        content_type IN ('image/jpeg', 'image/png', 'image/webp')
    ),
    CONSTRAINT ck_custody_evidence_sha256 CHECK (
        content_sha256 ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT ck_custody_evidence_size CHECK (
        size_bytes > 0 AND size_bytes <= 10485760
    )
);

CREATE INDEX ix_custody_evidence_custody_created
    ON custody_evidence (custody_id, created_at, id);
