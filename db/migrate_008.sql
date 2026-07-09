-- v13: интеграция с 1С — коды справочников и ключи синхронизации
-- (применяется автоматически при старте API, файл — для истории)
ALTER TABLE vehicles ADD COLUMN IF NOT EXISTS code_1c TEXT;   -- код Kult_Транспорт
ALTER TABLE drivers  ADD COLUMN IF NOT EXISTS code_1c TEXT;   -- код ФизическиеЛица

CREATE TABLE IF NOT EXISTS sync_keys (
    key        TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,               -- 'session' | 'project'
    project_id INT REFERENCES projects(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
