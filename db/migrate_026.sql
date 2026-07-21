-- v59: трекінг використання кнопок водієм (Подзвонити, Google Maps, Waze)
CREATE TABLE IF NOT EXISTS ui_events (
    id        BIGSERIAL PRIMARY KEY,
    driver_id INT NOT NULL REFERENCES drivers(id) ON DELETE CASCADE,
    route_id  INT REFERENCES routes(id) ON DELETE SET NULL,
    order_id  BIGINT,
    event     TEXT NOT NULL CHECK (event IN ('call','nav_google','nav_waze')),
    ts        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_ui_events_event_ts  ON ui_events(event, ts);
CREATE INDEX IF NOT EXISTS idx_ui_events_driver_ts ON ui_events(driver_id, ts);
