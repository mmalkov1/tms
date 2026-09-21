-- v91: internal TMS multi-depot planning. No changes to 1C mapping/protocol.
-- Unknown coordinates must be confirmed in Settings -> Warehouses, never guessed.
BEGIN;
ALTER TABLE depots ALTER COLUMN lat DROP NOT NULL;
ALTER TABLE depots ALTER COLUMN lon DROP NOT NULL;
INSERT INTO depots (name, address, lat, lon)
SELECT 'Склад Тернопіль', 'Україна, Тернопільська обл., с. Біла, вул. Мазепи, 24Д', NULL, NULL
WHERE NOT EXISTS (SELECT 1 FROM depots WHERE name='Склад Тернопіль');
COMMIT;
