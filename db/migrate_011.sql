-- v17: мобильный кабинет водителя (фаза 1) — токены, факты, GPS
-- (применяется автоматически при старте API, файл — для истории)

CREATE TABLE IF NOT EXISTS driver_tokens (
    token      TEXT PRIMARY KEY,
    driver_id  INT NOT NULL REFERENCES drivers(id) ON DELETE CASCADE,
    is_active  BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- один активный токен на водителя
CREATE UNIQUE INDEX IF NOT EXISTS uq_driver_tokens_active
    ON driver_tokens(driver_id) WHERE is_active;

-- факты прибытия/убытия по точкам маршрута (ручное подтверждение, фаза 1)
CREATE TABLE IF NOT EXISTS stop_events (
    id       SERIAL PRIMARY KEY,
    route_id INT NOT NULL REFERENCES routes(id) ON DELETE CASCADE,
    order_id INT NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    event    TEXT NOT NULL CHECK (event IN ('arrive','depart')),
    ts       TIMESTAMPTZ NOT NULL DEFAULT now(),
    lat      DOUBLE PRECISION,
    lon      DOUBLE PRECISION,
    source   TEXT NOT NULL DEFAULT 'manual',   -- manual | geofence (фаза 3)
    UNIQUE (route_id, order_id, event)
);
CREATE INDEX IF NOT EXISTS idx_stop_events_route ON stop_events(route_id);

-- сырой GPS-трек (фаза 1: пока открыта страница; фаза 2: из APK)
CREATE TABLE IF NOT EXISTS gps_points (
    id          BIGSERIAL PRIMARY KEY,
    driver_id   INT NOT NULL REFERENCES drivers(id) ON DELETE CASCADE,
    route_id    INT REFERENCES routes(id) ON DELETE SET NULL,
    ts          TIMESTAMPTZ NOT NULL,
    lat         DOUBLE PRECISION NOT NULL,
    lon         DOUBLE PRECISION NOT NULL,
    speed_kmh   REAL,
    accuracy_m  REAL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_gps_driver_ts ON gps_points(driver_id, ts DESC);
