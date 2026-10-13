-- F07.01 — Event-day configuration, ordered shifts, operational snapshots, and history.
-- Event Mode is deliberately isolated from core Asset/Custody quantities.

CREATE TYPE event_day_status AS ENUM ('DRAFT', 'IN_PROGRESS', 'COMPLETED');

CREATE TYPE shift_status AS ENUM (
    'SCHEDULED',
    'OPENING',
    'ACTIVE',
    'ACTIVE_WITH_DEVIATION',
    'CLOSED'
);

CREATE TABLE event_days (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(200) NOT NULL,
    event_date DATE NOT NULL,
    timezone VARCHAR(64) NOT NULL DEFAULT 'Asia/Ho_Chi_Minh',
    status event_day_status NOT NULL DEFAULT 'DRAFT',
    created_by_user_id UUID NOT NULL REFERENCES users (id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ck_event_days_name_not_blank CHECK (btrim(name) <> ''),
    CONSTRAINT ck_event_days_timezone CHECK (timezone = 'Asia/Ho_Chi_Minh'),
    CONSTRAINT ck_event_days_updated_after_created CHECK (updated_at >= created_at)
);

CREATE TABLE shifts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_day_id UUID NOT NULL REFERENCES event_days (id),
    sequence INTEGER NOT NULL,
    name VARCHAR(200) NOT NULL,
    starts_at TIMESTAMPTZ NOT NULL,
    ends_at TIMESTAMPTZ NOT NULL,
    shift_lead_user_id UUID NOT NULL REFERENCES users (id),
    status shift_status NOT NULL DEFAULT 'SCHEDULED',
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_shifts_event_day_sequence UNIQUE (event_day_id, sequence),
    CONSTRAINT uq_shifts_event_day_id UNIQUE (event_day_id, id),
    CONSTRAINT ck_shifts_sequence_positive CHECK (sequence > 0),
    CONSTRAINT ck_shifts_name_not_blank CHECK (btrim(name) <> ''),
    CONSTRAINT ck_shifts_time_order CHECK (ends_at > starts_at),
    CONSTRAINT ck_shifts_updated_after_created CHECK (updated_at >= created_at)
);

CREATE TABLE shift_assets (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    shift_id UUID NOT NULL REFERENCES shifts (id),
    asset_id UUID NOT NULL REFERENCES assets (id),
    quota_quantity INTEGER NOT NULL,
    carry_over_quantity INTEGER NOT NULL DEFAULT 0,
    expected_open_quantity INTEGER NOT NULL,
    opening_actual_quantity INTEGER NULL,
    closing_actual_quantity INTEGER NULL,
    consumed_quantity INTEGER NULL,
    discrepancy_reason VARCHAR(1000) NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_shift_assets_shift_asset UNIQUE (shift_id, asset_id),
    CONSTRAINT ck_shift_assets_quota_non_negative CHECK (quota_quantity >= 0),
    CONSTRAINT ck_shift_assets_carry_over_non_negative CHECK (carry_over_quantity >= 0),
    CONSTRAINT ck_shift_assets_expected_open CHECK (
        expected_open_quantity = quota_quantity + carry_over_quantity
    ),
    CONSTRAINT ck_shift_assets_actual_non_negative CHECK (
        (opening_actual_quantity IS NULL OR opening_actual_quantity >= 0)
        AND (closing_actual_quantity IS NULL OR closing_actual_quantity >= 0)
    ),
    CONSTRAINT ck_shift_assets_updated_after_created CHECK (updated_at >= created_at)
);

CREATE TABLE shift_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    shift_id UUID NOT NULL REFERENCES shifts (id),
    actor_user_id UUID NOT NULL REFERENCES users (id),
    event_type VARCHAR(64) NOT NULL,
    from_status shift_status NULL,
    to_status shift_status NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::JSONB,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ck_shift_events_type_not_blank CHECK (btrim(event_type) <> ''),
    CONSTRAINT ck_shift_events_metadata_object CHECK (jsonb_typeof(metadata) = 'object')
);

CREATE INDEX ix_event_days_date ON event_days (event_date, id);
CREATE INDEX ix_event_days_creator ON event_days (created_by_user_id, created_at DESC, id);
CREATE INDEX ix_shifts_assignee_schedule
    ON shifts (shift_lead_user_id, starts_at, id)
    WHERE status <> 'CLOSED';
CREATE INDEX ix_shifts_event_schedule ON shifts (event_day_id, sequence, starts_at, id);
CREATE INDEX ix_shift_assets_asset ON shift_assets (asset_id, shift_id, id);
CREATE INDEX ix_shift_events_history ON shift_events (shift_id, occurred_at, id);
