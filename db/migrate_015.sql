-- v25: активація проекту («У роботу») — водії та План/Факт бачать лише активний проект дати
-- Застосовується автоматично при старті API; бекфіл — один раз при додаванні колонки:
-- останній проект з маршрутами на кожну дату позначається активним.

ALTER TABLE projects ADD COLUMN IF NOT EXISTS is_released BOOLEAN NOT NULL DEFAULT FALSE;

-- бекфіл (виконується в startup лише якщо колонки не було):
-- UPDATE projects SET is_released = TRUE WHERE id IN (
--     SELECT DISTINCT ON (r.plan_date) r.project_id
--     FROM routes r WHERE r.project_id IS NOT NULL
--     ORDER BY r.plan_date, r.project_id DESC);
