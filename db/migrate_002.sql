-- v3: геозоны
CREATE TABLE IF NOT EXISTS geozones (
    id      SERIAL PRIMARY KEY,
    name    TEXT NOT NULL UNIQUE,
    points  JSONB NOT NULL           -- [[lat,lon],...]
);
CREATE TABLE IF NOT EXISTS driver_zones (
    driver_id INT NOT NULL REFERENCES drivers(id) ON DELETE CASCADE,
    zone_id   INT NOT NULL REFERENCES geozones(id) ON DELETE CASCADE,
    PRIMARY KEY (driver_id, zone_id)
);
