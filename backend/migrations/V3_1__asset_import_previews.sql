-- F02.08 — Durable baseline-Asset import previews reviewed before confirmation.

CREATE TABLE asset_imports (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_by_user_id UUID NOT NULL REFERENCES users (id),
    status VARCHAR(16) NOT NULL,
    source_filename VARCHAR(255) NOT NULL,
    source_content_type VARCHAR(100) NOT NULL,
    source_sha256 CHAR(64) NOT NULL,
    valid_row_count INTEGER NOT NULL,
    invalid_row_count INTEGER NOT NULL,
    rows JSONB NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    confirmed_at TIMESTAMPTZ NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ck_asset_imports_status CHECK (status IN ('PREVIEWED', 'COMPLETED')),
    CONSTRAINT ck_asset_imports_filename_not_blank CHECK (btrim(source_filename) <> ''),
    CONSTRAINT ck_asset_imports_content_type_not_blank CHECK (btrim(source_content_type) <> ''),
    CONSTRAINT ck_asset_imports_sha256 CHECK (source_sha256 ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_asset_imports_counts CHECK (
        valid_row_count >= 0
        AND invalid_row_count >= 0
        AND valid_row_count + invalid_row_count > 0
    ),
    CONSTRAINT ck_asset_imports_rows_array CHECK (jsonb_typeof(rows) = 'array'),
    CONSTRAINT ck_asset_imports_expiry CHECK (expires_at > created_at),
    CONSTRAINT ck_asset_imports_state_time CHECK (
        (status = 'PREVIEWED' AND confirmed_at IS NULL)
        OR (status = 'COMPLETED' AND confirmed_at IS NOT NULL)
    ),
    CONSTRAINT ck_asset_imports_confirmation_time CHECK (
        confirmed_at IS NULL OR confirmed_at >= created_at
    )
);

CREATE INDEX ix_asset_imports_owner_created
    ON asset_imports (created_by_user_id, created_at DESC, id);
CREATE INDEX ix_asset_imports_preview_expiry
    ON asset_imports (expires_at, id)
    WHERE status = 'PREVIEWED';
