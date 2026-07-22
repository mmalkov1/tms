-- v62: точний аудит геоперевірки натискань водія
-- lat/lon події = фактично використана позиція водія;
-- target_lat/target_lon = координати цілі на момент натискання.
ALTER TABLE stop_events
    ADD COLUMN IF NOT EXISTS gps_point_id BIGINT REFERENCES gps_points(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS gps_accuracy_m REAL,
    ADD COLUMN IF NOT EXISTS gps_age_sec INT,
    ADD COLUMN IF NOT EXISTS target_lat DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS target_lon DOUBLE PRECISION;

ALTER TABLE route_events
    ADD COLUMN IF NOT EXISTS gps_point_id BIGINT REFERENCES gps_points(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS gps_accuracy_m REAL,
    ADD COLUMN IF NOT EXISTS gps_age_sec INT,
    ADD COLUMN IF NOT EXISTS target_lat DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS target_lon DOUBLE PRECISION;
