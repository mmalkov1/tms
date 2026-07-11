-- v22: кількість місць у замовленні (тег SEATS з вивантаження 1С)
-- Застосовується автоматично при старті API.

ALTER TABLE orders ADD COLUMN IF NOT EXISTS seats INT;
