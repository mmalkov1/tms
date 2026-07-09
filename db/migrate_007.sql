-- v11: возможности авто — может забирать / может доставлять
-- (наемный транспорт бывает "только доставка")
ALTER TABLE vehicles
    ADD COLUMN IF NOT EXISTS can_pickup   BOOLEAN NOT NULL DEFAULT TRUE,
    ADD COLUMN IF NOT EXISTS can_delivery BOOLEAN NOT NULL DEFAULT TRUE;
