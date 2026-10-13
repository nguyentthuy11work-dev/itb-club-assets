-- F09.01 — Immutable Custody reconciliation snapshots and close gate metadata.

CREATE TYPE reconciliation_status AS ENUM ('OPEN', 'CLOSED');
CREATE TYPE reconciliation_item_status AS ENUM (
    'PENDING_CONFIRMATION',
    'CONFIRMED',
    'DISCREPANCY',
    'RESOLVED'
);

CREATE TABLE reconciliation_sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    reconciliation_date DATE NOT NULL,
    status reconciliation_status NOT NULL DEFAULT 'OPEN',
    opened_by_user_id UUID NOT NULL REFERENCES users (id),
    temporary_loan_ticket_count INTEGER NOT NULL DEFAULT 0,
    opened_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    confirmation_requested_at TIMESTAMPTZ NULL,
    closed_by_user_id UUID NULL REFERENCES users (id),
    closed_at TIMESTAMPTZ NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ck_reconciliation_sessions_loan_count CHECK (
        temporary_loan_ticket_count >= 0
    ),
    CONSTRAINT ck_reconciliation_sessions_closed_state CHECK (
        (status = 'OPEN' AND closed_by_user_id IS NULL AND closed_at IS NULL)
        OR (status = 'CLOSED' AND closed_by_user_id IS NOT NULL AND closed_at IS NOT NULL)
    ),
    CONSTRAINT ck_reconciliation_sessions_timestamps CHECK (
        updated_at >= opened_at
        AND (confirmation_requested_at IS NULL OR confirmation_requested_at >= opened_at)
        AND (closed_at IS NULL OR closed_at >= opened_at)
    )
);

CREATE UNIQUE INDEX uq_reconciliation_sessions_single_open
    ON reconciliation_sessions ((status))
    WHERE status = 'OPEN';
CREATE INDEX ix_reconciliation_sessions_date
    ON reconciliation_sessions (reconciliation_date DESC, id);

CREATE TABLE reconciliation_items (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    reconciliation_id UUID NOT NULL REFERENCES reconciliation_sessions (id),
    custody_id UUID NOT NULL REFERENCES custodies (id),
    asset_id UUID NOT NULL REFERENCES assets (id),
    custodian_user_id UUID NOT NULL REFERENCES users (id),
    asset_name_snapshot VARCHAR(200) NOT NULL,
    expected_quantity INTEGER NOT NULL,
    expected_condition VARCHAR(16) NOT NULL,
    snapshot_at TIMESTAMPTZ NOT NULL,
    status reconciliation_item_status NOT NULL DEFAULT 'PENDING_CONFIRMATION',
    actual_quantity INTEGER NULL,
    actual_condition VARCHAR(16) NULL,
    condition_note VARCHAR(1000) NULL,
    discrepancy_reason VARCHAR(1000) NULL,
    confirmed_by_user_id UUID NULL REFERENCES users (id),
    confirmed_at TIMESTAMPTZ NULL,
    resolution_note VARCHAR(1000) NULL,
    resolved_by_user_id UUID NULL REFERENCES users (id),
    resolved_at TIMESTAMPTZ NULL,
    last_reminded_at TIMESTAMPTZ NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_reconciliation_items_session_custody UNIQUE (
        reconciliation_id,
        custody_id
    ),
    CONSTRAINT ck_reconciliation_items_asset_name CHECK (
        btrim(asset_name_snapshot) <> ''
    ),
    CONSTRAINT ck_reconciliation_items_expected_quantity CHECK (expected_quantity >= 0),
    CONSTRAINT ck_reconciliation_items_actual_quantity CHECK (
        actual_quantity IS NULL OR actual_quantity >= 0
    ),
    CONSTRAINT ck_reconciliation_items_condition CHECK (
        expected_condition IN ('GOOD', 'DAMAGED', 'LOST')
        AND (actual_condition IS NULL OR actual_condition IN ('GOOD', 'DAMAGED', 'LOST'))
    ),
    CONSTRAINT ck_reconciliation_items_confirmation_state CHECK (
        (status = 'PENDING_CONFIRMATION'
            AND actual_quantity IS NULL
            AND actual_condition IS NULL
            AND discrepancy_reason IS NULL
            AND confirmed_by_user_id IS NULL
            AND confirmed_at IS NULL)
        OR (status = 'CONFIRMED'
            AND actual_quantity = expected_quantity
            AND actual_condition = expected_condition
            AND discrepancy_reason IS NULL
            AND confirmed_by_user_id IS NOT NULL
            AND confirmed_at IS NOT NULL
            AND resolution_note IS NULL
            AND resolved_by_user_id IS NULL
            AND resolved_at IS NULL)
        OR (status = 'DISCREPANCY'
            AND actual_quantity IS NOT NULL
            AND actual_condition IS NOT NULL
            AND (actual_quantity <> expected_quantity OR actual_condition <> expected_condition)
            AND discrepancy_reason IS NOT NULL
            AND btrim(discrepancy_reason) <> ''
            AND confirmed_by_user_id IS NOT NULL
            AND confirmed_at IS NOT NULL
            AND resolution_note IS NULL
            AND resolved_by_user_id IS NULL
            AND resolved_at IS NULL)
        OR (status = 'RESOLVED'
            AND actual_quantity IS NOT NULL
            AND actual_condition IS NOT NULL
            AND discrepancy_reason IS NOT NULL
            AND btrim(discrepancy_reason) <> ''
            AND confirmed_by_user_id IS NOT NULL
            AND confirmed_at IS NOT NULL
            AND resolution_note IS NOT NULL
            AND btrim(resolution_note) <> ''
            AND resolved_by_user_id IS NOT NULL
            AND resolved_at IS NOT NULL)
    ),
    CONSTRAINT ck_reconciliation_items_timestamps CHECK (
        updated_at >= created_at
        AND snapshot_at <= created_at
        AND (confirmed_at IS NULL OR confirmed_at >= snapshot_at)
        AND (resolved_at IS NULL OR resolved_at >= confirmed_at)
        AND (last_reminded_at IS NULL OR last_reminded_at >= snapshot_at)
    )
);

CREATE INDEX ix_reconciliation_items_session_status
    ON reconciliation_items (reconciliation_id, status, id);
CREATE INDEX ix_reconciliation_items_custodian_status
    ON reconciliation_items (custodian_user_id, status, snapshot_at DESC, id);
CREATE INDEX ix_reconciliation_items_asset_history
    ON reconciliation_items (asset_id, snapshot_at DESC, id);

CREATE TABLE reconciliation_item_evidence (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    reconciliation_item_id UUID NOT NULL REFERENCES reconciliation_items (id),
    uploaded_by_user_id UUID NOT NULL REFERENCES users (id),
    object_key VARCHAR(512) NOT NULL,
    content_type VARCHAR(32) NOT NULL,
    content_sha256 CHAR(64) NOT NULL,
    size_bytes BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_reconciliation_item_evidence_object_key UNIQUE (object_key),
    CONSTRAINT ck_reconciliation_item_evidence_object_key CHECK (btrim(object_key) <> ''),
    CONSTRAINT ck_reconciliation_item_evidence_content_type CHECK (
        content_type IN ('image/jpeg', 'image/png', 'image/webp')
    ),
    CONSTRAINT ck_reconciliation_item_evidence_sha256 CHECK (
        content_sha256 ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT ck_reconciliation_item_evidence_size CHECK (
        size_bytes > 0 AND size_bytes <= 10485760
    )
);

CREATE INDEX ix_reconciliation_item_evidence_item
    ON reconciliation_item_evidence (reconciliation_item_id, created_at, id);
