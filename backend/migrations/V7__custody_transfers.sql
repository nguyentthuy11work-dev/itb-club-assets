-- F10.01 — Standalone two-party Custody Transfer and immutable evidence.

CREATE TYPE custody_transfer_status AS ENUM (
    'PENDING_BOTH_CONFIRMATIONS',
    'PENDING_CURRENT_CONFIRMATION',
    'PENDING_TARGET_CONFIRMATION',
    'COMPLETED'
);

CREATE TYPE custody_transfer_evidence_side AS ENUM ('CURRENT', 'TARGET');

CREATE TABLE custody_transfers (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_custody_id UUID NOT NULL REFERENCES custodies (id),
    asset_id UUID NOT NULL REFERENCES assets (id),
    current_custodian_user_id UUID NOT NULL REFERENCES users (id),
    target_custodian_user_id UUID NOT NULL REFERENCES users (id),
    quantity INTEGER NOT NULL,
    condition_snapshot VARCHAR(16) NOT NULL,
    reconciliation_item_id UUID NULL REFERENCES reconciliation_items (id),
    status custody_transfer_status NOT NULL DEFAULT 'PENDING_BOTH_CONFIRMATIONS',
    created_by_user_id UUID NOT NULL REFERENCES users (id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    current_confirmed_by_user_id UUID NULL REFERENCES users (id),
    current_confirmed_at TIMESTAMPTZ NULL,
    target_confirmed_by_user_id UUID NULL REFERENCES users (id),
    target_confirmed_at TIMESTAMPTZ NULL,
    target_custody_id UUID NULL REFERENCES custodies (id),
    completed_at TIMESTAMPTZ NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_custody_transfers_source_asset FOREIGN KEY (
        source_custody_id,
        asset_id
    ) REFERENCES custodies (id, asset_id),
    CONSTRAINT ck_custody_transfers_distinct_users CHECK (
        current_custodian_user_id <> target_custodian_user_id
    ),
    CONSTRAINT ck_custody_transfers_quantity CHECK (quantity > 0),
    CONSTRAINT ck_custody_transfers_condition CHECK (
        condition_snapshot IN ('GOOD', 'DAMAGED', 'LOST')
    ),
    CONSTRAINT ck_custody_transfers_confirmation_state CHECK (
        (status = 'PENDING_BOTH_CONFIRMATIONS'
            AND current_confirmed_by_user_id IS NULL
            AND current_confirmed_at IS NULL
            AND target_confirmed_by_user_id IS NULL
            AND target_confirmed_at IS NULL
            AND target_custody_id IS NULL
            AND completed_at IS NULL)
        OR (status = 'PENDING_CURRENT_CONFIRMATION'
            AND current_confirmed_by_user_id IS NULL
            AND current_confirmed_at IS NULL
            AND target_confirmed_by_user_id = target_custodian_user_id
            AND target_confirmed_at IS NOT NULL
            AND target_custody_id IS NULL
            AND completed_at IS NULL)
        OR (status = 'PENDING_TARGET_CONFIRMATION'
            AND current_confirmed_by_user_id = current_custodian_user_id
            AND current_confirmed_at IS NOT NULL
            AND target_confirmed_by_user_id IS NULL
            AND target_confirmed_at IS NULL
            AND target_custody_id IS NULL
            AND completed_at IS NULL)
        OR (status = 'COMPLETED'
            AND current_confirmed_by_user_id = current_custodian_user_id
            AND current_confirmed_at IS NOT NULL
            AND target_confirmed_by_user_id = target_custodian_user_id
            AND target_confirmed_at IS NOT NULL
            AND target_custody_id IS NOT NULL
            AND completed_at IS NOT NULL)
    ),
    CONSTRAINT ck_custody_transfers_timestamps CHECK (
        updated_at >= created_at
        AND (current_confirmed_at IS NULL OR current_confirmed_at >= created_at)
        AND (target_confirmed_at IS NULL OR target_confirmed_at >= created_at)
        AND (completed_at IS NULL OR completed_at >= created_at)
    )
);

CREATE INDEX ix_custody_transfers_current_status
    ON custody_transfers (current_custodian_user_id, status, created_at DESC, id);
CREATE INDEX ix_custody_transfers_target_status
    ON custody_transfers (target_custodian_user_id, status, created_at DESC, id);
CREATE INDEX ix_custody_transfers_source_history
    ON custody_transfers (source_custody_id, created_at DESC, id);
CREATE INDEX ix_custody_transfers_reconciliation_item
    ON custody_transfers (reconciliation_item_id, id)
    WHERE reconciliation_item_id IS NOT NULL;

CREATE TABLE custody_transfer_evidence (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    transfer_id UUID NOT NULL REFERENCES custody_transfers (id),
    side custody_transfer_evidence_side NOT NULL,
    uploaded_by_user_id UUID NOT NULL REFERENCES users (id),
    object_key VARCHAR(512) NOT NULL,
    content_type VARCHAR(32) NOT NULL,
    content_sha256 CHAR(64) NOT NULL,
    size_bytes BIGINT NOT NULL,
    note VARCHAR(1000) NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_custody_transfer_evidence_object_key UNIQUE (object_key),
    CONSTRAINT ck_custody_transfer_evidence_object_key CHECK (btrim(object_key) <> ''),
    CONSTRAINT ck_custody_transfer_evidence_content_type CHECK (
        content_type IN ('image/jpeg', 'image/png', 'image/webp')
    ),
    CONSTRAINT ck_custody_transfer_evidence_sha256 CHECK (
        content_sha256 ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT ck_custody_transfer_evidence_size CHECK (
        size_bytes > 0 AND size_bytes <= 10485760
    )
);

CREATE INDEX ix_custody_transfer_evidence_transfer_side
    ON custody_transfer_evidence (transfer_id, side, created_at, id);

ALTER TABLE custody_evidence
    DROP CONSTRAINT ck_custody_evidence_type;

ALTER TABLE custody_evidence
    ADD CONSTRAINT ck_custody_evidence_type CHECK (
        evidence_type IN ('ASSIGNMENT_CONFIRMATION', 'TRANSFER_CONFIRMATION')
    );

CREATE FUNCTION enforce_transfer_source_snapshot()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    source_status VARCHAR(16);
    source_asset_id UUID;
    source_custodian_user_id UUID;
BEGIN
    SELECT status, asset_id, custodian_user_id
    INTO source_status, source_asset_id, source_custodian_user_id
    FROM custodies
    WHERE id = NEW.source_custody_id;

    IF source_status IS DISTINCT FROM 'ACTIVE' THEN
        RAISE EXCEPTION 'Transfer source Custody must be active.'
            USING ERRCODE = '23514';
    END IF;
    IF source_asset_id IS DISTINCT FROM NEW.asset_id
       OR source_custodian_user_id IS DISTINCT FROM NEW.current_custodian_user_id THEN
        RAISE EXCEPTION 'Transfer source snapshot does not match its Custody.'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_custody_transfers_source_snapshot
BEFORE INSERT ON custody_transfers
FOR EACH ROW
EXECUTE FUNCTION enforce_transfer_source_snapshot();

CREATE FUNCTION enforce_pending_transfer_active_source()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.status <> 'ACTIVE'
       AND EXISTS (
           SELECT 1
           FROM custody_transfers
           WHERE source_custody_id = NEW.id
             AND status <> 'COMPLETED'
       ) THEN
        RAISE EXCEPTION 'Pending Transfer source Custody must remain active.'
            USING ERRCODE = '23514';
    END IF;
    RETURN NULL;
END;
$$;

-- Deferred so the completion transaction may close the source before updating
-- the Transfer row; the committed state must still satisfy the invariant.
CREATE CONSTRAINT TRIGGER trg_custodies_pending_transfer_source
AFTER UPDATE OF status ON custodies
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW
WHEN (OLD.status IS DISTINCT FROM NEW.status)
EXECUTE FUNCTION enforce_pending_transfer_active_source();
