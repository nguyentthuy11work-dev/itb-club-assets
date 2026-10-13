-- F11.01 — Configurable term calendar and immutable two-party Handover records.

CREATE TYPE handover_status AS ENUM ('PENDING_CONFIRMATIONS', 'LOCKED');

CREATE TABLE terms (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    code VARCHAR(64) NOT NULL,
    name VARCHAR(200) NOT NULL,
    starts_on DATE NOT NULL,
    ends_on DATE NOT NULL,
    old_hr_user_id UUID NOT NULL REFERENCES users (id),
    new_hr_user_id UUID NOT NULL REFERENCES users (id),
    created_by_user_id UUID NOT NULL REFERENCES users (id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_terms_code UNIQUE (code),
    CONSTRAINT ck_terms_code_not_blank CHECK (btrim(code) <> ''),
    CONSTRAINT ck_terms_name_not_blank CHECK (btrim(name) <> ''),
    CONSTRAINT ck_terms_dates CHECK (starts_on <= ends_on),
    CONSTRAINT ck_terms_distinct_hr CHECK (old_hr_user_id <> new_hr_user_id)
);

CREATE INDEX ix_terms_calendar ON terms (starts_on, ends_on, id);

CREATE TABLE handovers (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    term_id UUID NOT NULL REFERENCES terms (id),
    term_code_snapshot VARCHAR(64) NOT NULL,
    term_name_snapshot VARCHAR(200) NOT NULL,
    term_starts_on DATE NOT NULL,
    term_ends_on DATE NOT NULL,
    old_hr_user_id UUID NOT NULL REFERENCES users (id),
    new_hr_user_id UUID NOT NULL REFERENCES users (id),
    status handover_status NOT NULL DEFAULT 'PENDING_CONFIRMATIONS',
    created_by_user_id UUID NOT NULL REFERENCES users (id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    old_confirmed_by_user_id UUID NULL REFERENCES users (id),
    old_confirmed_at TIMESTAMPTZ NULL,
    new_confirmed_by_user_id UUID NULL REFERENCES users (id),
    new_confirmed_at TIMESTAMPTZ NULL,
    locked_at TIMESTAMPTZ NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_handovers_term UNIQUE (term_id),
    CONSTRAINT ck_handovers_term_code_not_blank CHECK (btrim(term_code_snapshot) <> ''),
    CONSTRAINT ck_handovers_term_name_not_blank CHECK (btrim(term_name_snapshot) <> ''),
    CONSTRAINT ck_handovers_term_dates CHECK (term_starts_on <= term_ends_on),
    CONSTRAINT ck_handovers_distinct_hr CHECK (old_hr_user_id <> new_hr_user_id),
    CONSTRAINT ck_handovers_old_confirmation_pair CHECK (
        (old_confirmed_by_user_id IS NULL AND old_confirmed_at IS NULL)
        OR (old_confirmed_by_user_id = old_hr_user_id AND old_confirmed_at IS NOT NULL)
    ),
    CONSTRAINT ck_handovers_new_confirmation_pair CHECK (
        (new_confirmed_by_user_id IS NULL AND new_confirmed_at IS NULL)
        OR (new_confirmed_by_user_id = new_hr_user_id AND new_confirmed_at IS NOT NULL)
    ),
    CONSTRAINT ck_handovers_state CHECK (
        (status = 'PENDING_CONFIRMATIONS' AND locked_at IS NULL)
        OR (
            status = 'LOCKED'
            AND old_confirmed_by_user_id = old_hr_user_id
            AND old_confirmed_at IS NOT NULL
            AND new_confirmed_by_user_id = new_hr_user_id
            AND new_confirmed_at IS NOT NULL
            AND locked_at IS NOT NULL
        )
    ),
    CONSTRAINT ck_handovers_timestamps CHECK (
        updated_at >= created_at
        AND (old_confirmed_at IS NULL OR old_confirmed_at >= created_at)
        AND (new_confirmed_at IS NULL OR new_confirmed_at >= created_at)
        AND (locked_at IS NULL OR locked_at >= created_at)
    )
);

CREATE INDEX ix_handovers_old_hr_status
    ON handovers (old_hr_user_id, status, created_at DESC, id);
CREATE INDEX ix_handovers_new_hr_status
    ON handovers (new_hr_user_id, status, created_at DESC, id);

CREATE TABLE handover_items (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    handover_id UUID NOT NULL REFERENCES handovers (id),
    asset_id UUID NOT NULL REFERENCES assets (id),
    asset_name_snapshot VARCHAR(200) NOT NULL,
    total_quantity_snapshot INTEGER NOT NULL,
    available_quantity_snapshot INTEGER NOT NULL,
    condition_snapshot VARCHAR(16) NOT NULL,
    custody_snapshot JSONB NOT NULL,
    is_stale BOOLEAN NOT NULL,
    last_qualifying_transaction_at TIMESTAMPTZ NULL,
    snapshot_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_handover_items_asset UNIQUE (handover_id, asset_id),
    CONSTRAINT ck_handover_items_name_not_blank CHECK (btrim(asset_name_snapshot) <> ''),
    CONSTRAINT ck_handover_items_total_quantity CHECK (total_quantity_snapshot >= 0),
    CONSTRAINT ck_handover_items_available_quantity CHECK (
        available_quantity_snapshot >= 0
        AND available_quantity_snapshot <= total_quantity_snapshot
    ),
    CONSTRAINT ck_handover_items_condition CHECK (
        condition_snapshot IN ('GOOD', 'DAMAGED', 'LOST')
    ),
    CONSTRAINT ck_handover_items_custody_array CHECK (
        jsonb_typeof(custody_snapshot) = 'array'
    ),
    CONSTRAINT ck_handover_items_transaction_before_snapshot CHECK (
        last_qualifying_transaction_at IS NULL
        OR last_qualifying_transaction_at <= snapshot_at
    )
);

CREATE INDEX ix_handover_items_handover_stale
    ON handover_items (handover_id, is_stale, asset_name_snapshot, id);
CREATE INDEX ix_handover_items_asset_history
    ON handover_items (asset_id, snapshot_at DESC, id);

CREATE FUNCTION protect_locked_handover()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.status = 'LOCKED' THEN
        RAISE EXCEPTION 'Locked Handover records are immutable.'
            USING ERRCODE = '23514';
    END IF;
    RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
END;
$$;

CREATE TRIGGER trg_handovers_locked_immutable
BEFORE UPDATE OR DELETE ON handovers
FOR EACH ROW
EXECUTE FUNCTION protect_locked_handover();

CREATE FUNCTION protect_locked_handover_item()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    parent_id UUID;
BEGIN
    parent_id := CASE WHEN TG_OP = 'DELETE' THEN OLD.handover_id ELSE NEW.handover_id END;
    IF EXISTS (
        SELECT 1 FROM handovers WHERE id = parent_id AND status = 'LOCKED'
    ) THEN
        RAISE EXCEPTION 'Locked Handover items are immutable.'
            USING ERRCODE = '23514';
    END IF;
    RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
END;
$$;

CREATE TRIGGER trg_handover_items_locked_immutable
BEFORE INSERT OR UPDATE OR DELETE ON handover_items
FOR EACH ROW
EXECUTE FUNCTION protect_locked_handover_item();
