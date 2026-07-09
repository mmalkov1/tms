-- v15: кеш геокодирования. Ключ — нормализованный адрес (нижний регистр,
-- запятые -> пробел, схлопнутые пробелы). Заполняется при ручном исправлении
-- координат заявки и при успешном геокодировании; при каждом импорте (xlsx и 1С)
-- заявки без координат получают их из кеша до запуска геокодера.
CREATE TABLE IF NOT EXISTS geo_cache (
    address_norm TEXT PRIMARY KEY,
    lat          DOUBLE PRECISION NOT NULL,
    lon          DOUBLE PRECISION NOT NULL,
    source       TEXT,               -- 'manual' | 'geocoder'
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
