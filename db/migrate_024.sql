-- v54: пілот транспортних листів та обліку пального
CREATE TABLE IF NOT EXISTS vehicle_fuel_settings (
    vehicle_id          INT PRIMARY KEY REFERENCES vehicles(id) ON DELETE CASCADE,
    rate_l_per_100      NUMERIC(8,3),
    initial_balance_l   NUMERIC(10,3),
    initial_balance_date DATE,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS transport_sheets (
    id                BIGSERIAL PRIMARY KEY,
    work_date         DATE NOT NULL,
    vehicle_id        INT NOT NULL REFERENCES vehicles(id),
    driver_id         INT NOT NULL REFERENCES drivers(id),
    odometer_start    NUMERIC(12,1),
    odometer_end      NUMERIC(12,1),
    opening_balance_l NUMERIC(10,3),
    fuel_used_l       NUMERIC(10,3),
    closing_balance_l NUMERIC(10,3),
    status            TEXT NOT NULL DEFAULT 'draft'
                      CHECK (status IN ('draft','submitted','approved','revision')),
    revision_reason   TEXT,
    submitted_at      TIMESTAMPTZ,
    approved_at       TIMESTAMPTZ,
    approved_by       TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (work_date, vehicle_id)
);
CREATE INDEX IF NOT EXISTS idx_transport_sheets_date ON transport_sheets(work_date DESC);
CREATE INDEX IF NOT EXISTS idx_transport_sheets_driver ON transport_sheets(driver_id, work_date DESC);

CREATE TABLE IF NOT EXISTS transport_sheet_refuels (
    id         BIGSERIAL PRIMARY KEY,
    sheet_id   BIGINT NOT NULL REFERENCES transport_sheets(id) ON DELETE CASCADE,
    liters     NUMERIC(10,3) NOT NULL CHECK (liters > 0),
    refuel_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS transport_sheet_changes (
    id         BIGSERIAL PRIMARY KEY,
    sheet_id   BIGINT NOT NULL REFERENCES transport_sheets(id) ON DELETE CASCADE,
    actor_role TEXT NOT NULL CHECK (actor_role IN ('driver','logist','system')),
    actor_name TEXT,
    field_name TEXT NOT NULL,
    old_value  TEXT,
    new_value  TEXT,
    reason     TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_transport_sheet_changes_sheet
    ON transport_sheet_changes(sheet_id, created_at DESC);
