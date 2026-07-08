-- v2: время выезда/возврата на маршруте
ALTER TABLE routes ADD COLUMN IF NOT EXISTS depart_time time;
ALTER TABLE routes ADD COLUMN IF NOT EXISTS return_time time;
