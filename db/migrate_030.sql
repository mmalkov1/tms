-- v86: service_min може бути NULL = «1С не передала, підставимо норматив з історії».
-- Без цього імпорт з 1С падає з 500 (NOT NULL violation) після зміни в v84.
ALTER TABLE orders ALTER COLUMN service_min DROP NOT NULL;
ALTER TABLE orders ALTER COLUMN service_min DROP DEFAULT;
