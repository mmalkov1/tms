-- v30: події рейсу «виїхав / завершив» — робочий час і кілометраж водія
CREATE TABLE IF NOT EXISTS route_events (
    id        SERIAL PRIMARY KEY,
    route_id  INT NOT NULL REFERENCES routes(id) ON DELETE CASCADE,
    driver_id INT,
    event     TEXT NOT NULL CHECK (event IN ('start','finish')),
    ts        TIMESTAMPTZ NOT NULL DEFAULT now(),
    lat       DOUBLE PRECISION,
    lon       DOUBLE PRECISION,
    UNIQUE (route_id, event)
);
