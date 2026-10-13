-- F05.01 — Grouped Borrow Ticket lifecycle, immutable evidence, and transition history.
-- One Ticket has one borrower, source custodian, method, and due-date policy.
-- Ticket items reserve quantity from explicit source Custodies; no FK cascades
-- are used because Ticket business history must not be deleted implicitly.

CREATE TYPE ticket_method AS ENUM ('TEMPORARY', 'CUSTODY_TRANSFER');

CREATE TYPE ticket_status AS ENUM (
    'PENDING',
    'APPROVED',
    'BORROWING',
    'RETURNING',
    'CANCEL_PENDING',
    'REJECTED',
    'CANCELLED',
    'CLOSED'
);

CREATE TABLE borrow_tickets (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    borrower_user_id UUID NOT NULL REFERENCES users (id),
    source_custodian_user_id UUID NOT NULL REFERENCES users (id),
    method ticket_method NOT NULL,
    status ticket_status NOT NULL DEFAULT 'PENDING',
    due_date DATE NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ck_borrow_tickets_due_date_method CHECK (
        (method = 'TEMPORARY' AND due_date IS NOT NULL)
        OR (method = 'CUSTODY_TRANSFER' AND due_date IS NULL)
    ),
    CONSTRAINT ck_borrow_tickets_updated_after_created CHECK (updated_at >= created_at)
);

CREATE INDEX ix_borrow_tickets_borrower_status
    ON borrow_tickets (borrower_user_id, status, created_at DESC, id);
CREATE INDEX ix_borrow_tickets_custodian_status
    ON borrow_tickets (source_custodian_user_id, status, created_at DESC, id);
CREATE INDEX ix_borrow_tickets_status_created
    ON borrow_tickets (status, created_at, id);
CREATE INDEX ix_borrow_tickets_temporary_due
    ON borrow_tickets (due_date, id)
    WHERE method = 'TEMPORARY'
      AND status IN ('APPROVED', 'BORROWING', 'RETURNING');

-- PostgreSQL requires an explicit unique key for the composite Ticket-item FK.
-- It guarantees that the denormalized asset_id used for history belongs to the
-- selected source Custody.
CREATE UNIQUE INDEX uq_custodies_id_asset ON custodies (id, asset_id);

CREATE TABLE ticket_items (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ticket_id UUID NOT NULL REFERENCES borrow_tickets (id),
    asset_id UUID NOT NULL REFERENCES assets (id),
    source_custody_id UUID NOT NULL,
    quantity INTEGER NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_ticket_items_source_custody_asset FOREIGN KEY (
        source_custody_id,
        asset_id
    ) REFERENCES custodies (id, asset_id),
    CONSTRAINT uq_ticket_items_ticket_source_custody UNIQUE (
        ticket_id,
        source_custody_id
    ),
    CONSTRAINT ck_ticket_items_quantity_positive CHECK (quantity > 0)
);

CREATE INDEX ix_ticket_items_asset_history
    ON ticket_items (asset_id, created_at, ticket_id, id);
CREATE INDEX ix_ticket_items_source_custody
    ON ticket_items (source_custody_id, ticket_id, id);

CREATE TABLE ticket_evidence (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ticket_id UUID NOT NULL REFERENCES borrow_tickets (id),
    uploaded_by_user_id UUID NOT NULL REFERENCES users (id),
    evidence_type VARCHAR(32) NOT NULL,
    object_key VARCHAR(512) NOT NULL,
    content_type VARCHAR(32) NOT NULL,
    content_sha256 CHAR(64) NOT NULL,
    size_bytes BIGINT NOT NULL,
    note VARCHAR(1000) NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_ticket_evidence_object_key UNIQUE (object_key),
    CONSTRAINT ck_ticket_evidence_type CHECK (
        evidence_type IN ('PICKUP', 'BORROWER_RETURN', 'CUSTODIAN_RETURN')
    ),
    CONSTRAINT ck_ticket_evidence_object_key_not_blank CHECK (btrim(object_key) <> ''),
    CONSTRAINT ck_ticket_evidence_content_type CHECK (
        content_type IN ('image/jpeg', 'image/png', 'image/webp')
    ),
    CONSTRAINT ck_ticket_evidence_sha256 CHECK (
        content_sha256 ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT ck_ticket_evidence_size CHECK (
        size_bytes > 0 AND size_bytes <= 10485760
    )
);

CREATE INDEX ix_ticket_evidence_ticket_created
    ON ticket_evidence (ticket_id, created_at, id);

CREATE TABLE ticket_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ticket_id UUID NOT NULL REFERENCES borrow_tickets (id),
    actor_user_id UUID NOT NULL REFERENCES users (id),
    event_type VARCHAR(64) NOT NULL,
    from_status ticket_status NULL,
    to_status ticket_status NOT NULL,
    note VARCHAR(1000) NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::JSONB,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ck_ticket_events_type_not_blank CHECK (btrim(event_type) <> ''),
    CONSTRAINT ck_ticket_events_metadata_object CHECK (jsonb_typeof(metadata) = 'object'),
    CONSTRAINT ck_ticket_events_creation_state CHECK (
        from_status IS NOT NULL
        OR (event_type = 'CREATED' AND to_status = 'PENDING')
    )
);

CREATE INDEX ix_ticket_events_ticket_history
    ON ticket_events (ticket_id, occurred_at, id);
CREATE INDEX ix_ticket_events_actor_history
    ON ticket_events (actor_user_id, occurred_at, id);
