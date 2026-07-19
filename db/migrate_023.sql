-- v51: старт/фініш маршруту не зі складу + адреса дому водія
ALTER TABLE drivers ADD COLUMN IF NOT EXISTS home_address TEXT;
ALTER TABLE drivers ADD COLUMN IF NOT EXISTS home_lat DOUBLE PRECISION;
ALTER TABLE drivers ADD COLUMN IF NOT EXISTS home_lon DOUBLE PRECISION;

-- kind: depot|home|custom; lat/lon — знімок точки (для home копіюється з водія
-- в момент збереження), NULL = склад. Всі споживачі: COALESCE(r.start_lat, d.lat).
ALTER TABLE routes ADD COLUMN IF NOT EXISTS start_kind  TEXT NOT NULL DEFAULT 'depot';
ALTER TABLE routes ADD COLUMN IF NOT EXISTS start_address TEXT;
ALTER TABLE routes ADD COLUMN IF NOT EXISTS start_lat   DOUBLE PRECISION;
ALTER TABLE routes ADD COLUMN IF NOT EXISTS start_lon   DOUBLE PRECISION;
ALTER TABLE routes ADD COLUMN IF NOT EXISTS finish_kind TEXT NOT NULL DEFAULT 'depot';
ALTER TABLE routes ADD COLUMN IF NOT EXISTS finish_address TEXT;
ALTER TABLE routes ADD COLUMN IF NOT EXISTS finish_lat  DOUBLE PRECISION;
ALTER TABLE routes ADD COLUMN IF NOT EXISTS finish_lon  DOUBLE PRECISION;
-- ручна фіксація часу фінішу (порожньо = розрахунковий return_time)
ALTER TABLE routes ADD COLUMN IF NOT EXISTS return_time_manual TIME;
