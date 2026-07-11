-- v24: відмови водія (причини + подія fail) і контактна особа точки
-- Застосовується автоматично при старті API (driver.init / startup), файл — для історії.

-- довідник причин відмов
CREATE TABLE IF NOT EXISTS fail_reasons (
    id        SERIAL PRIMARY KEY,
    kind      TEXT NOT NULL CHECK (kind IN ('delivery','pickup')),
    name      TEXT NOT NULL,
    sort      INT  NOT NULL DEFAULT 100,
    is_system BOOLEAN NOT NULL DEFAULT FALSE,   -- «Інше» не архівується
    is_active BOOLEAN NOT NULL DEFAULT TRUE
);

-- сид — тільки якщо довідник порожній
INSERT INTO fail_reasons (kind, name, sort, is_system)
SELECT * FROM (VALUES
    ('delivery','Закрито',10,FALSE),
    ('delivery','Клієнт відмовився',20,FALSE),
    ('delivery','Нема товару',30,FALSE),
    ('delivery','Інше',999,TRUE),
    ('pickup','Закрито',10,FALSE),
    ('pickup','Не готовий товар',20,FALSE),
    ('pickup','Інше',999,TRUE)
) AS v(kind,name,sort,is_system)
WHERE NOT EXISTS (SELECT 1 FROM fail_reasons);

-- stop_events: дозволяємо подію 'fail' + причина
ALTER TABLE stop_events DROP CONSTRAINT IF EXISTS stop_events_event_check;
ALTER TABLE stop_events ADD CONSTRAINT stop_events_event_check
    CHECK (event IN ('arrive','depart','fail'));
ALTER TABLE stop_events ADD COLUMN IF NOT EXISTS reason_id INT REFERENCES fail_reasons(id);
ALTER TABLE stop_events ADD COLUMN IF NOT EXISTS reason_text TEXT;

-- контактна особа точки (PERSON_NAME з 1С)
ALTER TABLE orders ADD COLUMN IF NOT EXISTS contact_person TEXT;
