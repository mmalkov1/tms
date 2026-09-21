-- v92: independent internal warehouse selections for route start and finish.
-- Coordinates/name are snapshotted in the existing endpoint fields.
-- NULL keeps the legacy route.depot_id fallback; no 1C mapping changes.
BEGIN;
ALTER TABLE routes ADD COLUMN IF NOT EXISTS start_depot_id INTEGER REFERENCES depots(id);
ALTER TABLE routes ADD COLUMN IF NOT EXISTS finish_depot_id INTEGER REFERENCES depots(id);
COMMIT;
