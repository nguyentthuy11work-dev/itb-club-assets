-- F03.01 — Durable notification outbox and idempotent command records.
-- Business changes and their outbox/idempotency rows share one application transaction.

CREATE TABLE outbox_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_type VARCHAR(100) NOT NULL,
    aggregate_type VARCHAR(100) NOT NULL,
    aggregate_id UUID NOT NULL,
    payload JSONB NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'PENDING',
    scheduled_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 8,
    lease_owner VARCHAR(160) NULL,
    lease_expires_at TIMESTAMPTZ NULL,
    last_error VARCHAR(2000) NULL,
    completed_at TIMESTAMPTZ NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ck_outbox_event_type_not_blank CHECK (btrim(event_type) <> ''),
    CONSTRAINT ck_outbox_aggregate_type_not_blank CHECK (btrim(aggregate_type) <> ''),
    CONSTRAINT ck_outbox_payload_object CHECK (jsonb_typeof(payload) = 'object'),
    CONSTRAINT ck_outbox_status CHECK (
        status IN ('PENDING', 'PROCESSING', 'DELIVERED', 'DEAD_LETTER')
    ),
    CONSTRAINT ck_outbox_attempts CHECK (
        attempt_count >= 0 AND max_attempts > 0 AND attempt_count <= max_attempts
    ),
    CONSTRAINT ck_outbox_lease_pair CHECK (
        (lease_owner IS NULL AND lease_expires_at IS NULL)
        OR (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)
    ),
    CONSTRAINT ck_outbox_state_metadata CHECK (
        (
            status = 'PENDING'
            AND lease_owner IS NULL
            AND completed_at IS NULL
        )
        OR (
            status = 'PROCESSING'
            AND lease_owner IS NOT NULL
            AND completed_at IS NULL
        )
        OR (
            status IN ('DELIVERED', 'DEAD_LETTER')
            AND lease_owner IS NULL
            AND completed_at IS NOT NULL
        )
    ),
    CONSTRAINT ck_outbox_lease_expiry CHECK (
        lease_expires_at IS NULL OR lease_expires_at > updated_at
    ),
    CONSTRAINT ck_outbox_completion_time CHECK (
        completed_at IS NULL OR completed_at >= created_at
    )
);

CREATE INDEX ix_outbox_pending_schedule
    ON outbox_events (next_attempt_at, scheduled_at, id)
    WHERE status = 'PENDING';
CREATE INDEX ix_outbox_processing_lease
    ON outbox_events (lease_expires_at, id)
    WHERE status = 'PROCESSING';
CREATE INDEX ix_outbox_aggregate_history
    ON outbox_events (aggregate_type, aggregate_id, created_at, id);

CREATE TABLE notifications (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    outbox_event_id UUID NOT NULL REFERENCES outbox_events (id),
    recipient_id UUID NOT NULL REFERENCES users (id),
    channel VARCHAR(16) NOT NULL,
    recipient_address VARCHAR(320) NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'PENDING',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    provider_message_id VARCHAR(255) NULL,
    last_error VARCHAR(2000) NULL,
    last_attempted_at TIMESTAMPTZ NULL,
    delivered_at TIMESTAMPTZ NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_notifications_event_recipient_channel UNIQUE (
        outbox_event_id,
        recipient_id,
        channel
    ),
    CONSTRAINT ck_notifications_channel CHECK (channel = 'EMAIL'),
    CONSTRAINT ck_notifications_recipient_not_blank CHECK (
        btrim(recipient_address) <> ''
    ),
    CONSTRAINT ck_notifications_status CHECK (
        status IN ('PENDING', 'DELIVERED', 'FAILED')
    ),
    CONSTRAINT ck_notifications_attempt_count CHECK (attempt_count >= 0),
    CONSTRAINT ck_notifications_delivery_state CHECK (
        (
            status = 'PENDING'
            AND delivered_at IS NULL
        )
        OR (
            status = 'DELIVERED'
            AND delivered_at IS NOT NULL
            AND attempt_count > 0
        )
        OR (
            status = 'FAILED'
            AND delivered_at IS NULL
            AND attempt_count > 0
            AND last_error IS NOT NULL
        )
    ),
    CONSTRAINT ck_notifications_attempt_time CHECK (
        last_attempted_at IS NULL OR last_attempted_at >= created_at
    ),
    CONSTRAINT ck_notifications_delivered_time CHECK (
        delivered_at IS NULL OR delivered_at >= created_at
    )
);

CREATE INDEX ix_notifications_recipient_delivery
    ON notifications (recipient_id, status, created_at, id);
CREATE INDEX ix_notifications_pending_delivery
    ON notifications (created_at, id)
    WHERE status = 'PENDING';

CREATE TABLE idempotency_records (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    actor_id UUID NOT NULL REFERENCES users (id),
    command_name VARCHAR(160) NOT NULL,
    idempotency_key VARCHAR(255) NOT NULL,
    request_fingerprint CHAR(64) NOT NULL,
    status VARCHAR(16) NOT NULL,
    response_status_code INTEGER NULL,
    response_body JSONB NULL,
    completed_at TIMESTAMPTZ NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_idempotency_actor_command_key UNIQUE (
        actor_id,
        command_name,
        idempotency_key
    ),
    CONSTRAINT ck_idempotency_command_not_blank CHECK (btrim(command_name) <> ''),
    CONSTRAINT ck_idempotency_key_not_blank CHECK (btrim(idempotency_key) <> ''),
    CONSTRAINT ck_idempotency_fingerprint CHECK (
        request_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT ck_idempotency_status CHECK (status IN ('IN_PROGRESS', 'SUCCEEDED')),
    CONSTRAINT ck_idempotency_state_response CHECK (
        (
            status = 'IN_PROGRESS'
            AND response_status_code IS NULL
            AND response_body IS NULL
            AND completed_at IS NULL
        )
        OR (
            status = 'SUCCEEDED'
            AND response_status_code BETWEEN 200 AND 299
            AND response_body IS NOT NULL
            AND completed_at IS NOT NULL
        )
    ),
    CONSTRAINT ck_idempotency_completion_time CHECK (
        completed_at IS NULL OR completed_at >= created_at
    ),
    CONSTRAINT ck_idempotency_expiry CHECK (expires_at > created_at)
);

CREATE INDEX ix_idempotency_in_progress
    ON idempotency_records (created_at, id)
    WHERE status = 'IN_PROGRESS';
CREATE INDEX ix_idempotency_expiry ON idempotency_records (expires_at, id);
