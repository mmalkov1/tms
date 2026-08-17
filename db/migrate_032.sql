-- v88: нічне автооновлення планувальних коефіцієнтів за останні 30 днів.
ALTER TABLE traffic_planning_settings
    ADD COLUMN IF NOT EXISTS updated_source TEXT NOT NULL DEFAULT 'manual',
    ADD COLUMN IF NOT EXISTS calculation_date_from DATE,
    ADD COLUMN IF NOT EXISTS calculation_date_to DATE,
    ADD COLUMN IF NOT EXISTS sample_counts JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS min_sample_count INT NOT NULL DEFAULT 20,
    ADD COLUMN IF NOT EXISTS auto_updated_at TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS traffic_planning_factor_history (
    id                BIGSERIAL PRIMARY KEY,
    analyzer_version  TEXT NOT NULL,
    date_from         DATE NOT NULL,
    date_to           DATE NOT NULL,
    time_bucket       TEXT NOT NULL,
    sample_count      INT NOT NULL,
    calculated_factor DOUBLE PRECISION,
    previous_factor   DOUBLE PRECISION NOT NULL,
    applied_factor    DOUBLE PRECISION NOT NULL,
    status            TEXT NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_traffic_factor_history_created
    ON traffic_planning_factor_history(created_at DESC);
