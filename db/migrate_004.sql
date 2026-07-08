-- v5: проекты планирования
CREATE TABLE IF NOT EXISTS projects (
    id         SERIAL PRIMARY KEY,
    plan_date  DATE NOT NULL,
    name       TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_projects_date ON projects(plan_date);

ALTER TABLE orders ADD COLUMN IF NOT EXISTS project_id INT REFERENCES projects(id) ON DELETE CASCADE;
ALTER TABLE routes ADD COLUMN IF NOT EXISTS project_id INT REFERENCES projects(id) ON DELETE CASCADE;

-- бэкфилл: по проекту на каждую дату с существующими заявками
INSERT INTO projects (plan_date, name)
SELECT DISTINCT o.plan_date, to_char(o.plan_date, 'DD-MM') || '_1'
FROM orders o
WHERE o.project_id IS NULL
  AND NOT EXISTS (SELECT 1 FROM projects p WHERE p.plan_date = o.plan_date);

UPDATE orders o SET project_id = p.id
FROM projects p WHERE o.project_id IS NULL AND p.plan_date = o.plan_date;

UPDATE routes r SET project_id = p.id
FROM projects p WHERE r.project_id IS NULL AND p.plan_date = r.plan_date;

-- уникальность заявки теперь в рамках проекта
ALTER TABLE orders DROP CONSTRAINT IF EXISTS orders_plan_date_doc_number_key;
CREATE UNIQUE INDEX IF NOT EXISTS uq_orders_project_doc ON orders(project_id, doc_number);
