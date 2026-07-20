-- v55: спрощений ТЛ водія (варіант C) + датовані коригування залишку пального
ALTER TABLE transport_sheets
    ADD COLUMN IF NOT EXISTS odometer_start_confirmed_at TIMESTAMPTZ;

-- маркер автоматичних подій геозони ('driver' | 'auto')
ALTER TABLE route_events
    ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'driver';

-- датовані коригування залишку: «на ранок adjust_date залишок = balance_l»
CREATE TABLE IF NOT EXISTS fuel_balance_adjustments (
    id          BIGSERIAL PRIMARY KEY,
    vehicle_id  INT NOT NULL REFERENCES vehicles(id) ON DELETE CASCADE,
    adjust_date DATE NOT NULL,
    balance_l   NUMERIC(10,3) NOT NULL CHECK (balance_l >= 0),
    reason      TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_fuel_adjustments_vehicle
    ON fuel_balance_adjustments(vehicle_id, adjust_date DESC, id DESC);
