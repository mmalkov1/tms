-- v87: коефіцієнти часу руху та знімок режиму розрахунку в маршруті.
CREATE TABLE IF NOT EXISTS traffic_planning_settings (
    id            SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    factor_07_10  NUMERIC(5,3) NOT NULL DEFAULT 1.590,
    factor_10_13  NUMERIC(5,3) NOT NULL DEFAULT 1.430,
    factor_13_16  NUMERIC(5,3) NOT NULL DEFAULT 1.680,
    factor_16_19  NUMERIC(5,3) NOT NULL DEFAULT 1.100,
    factor_other  NUMERIC(5,3) NOT NULL DEFAULT 1.520,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO traffic_planning_settings (id)
VALUES (1)
ON CONFLICT (id) DO NOTHING;

ALTER TABLE routes
    ADD COLUMN IF NOT EXISTS use_traffic_factors BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS traffic_factors JSONB;
