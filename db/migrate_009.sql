-- v14: код склада 1С на проекте (заполняется автоматически при импорте из 1С
-- из WAREHOUSE_CODE заявок; экспорт рейсов добавляет склад как точку выезда
-- и точку возвращения — как в Tocan)
ALTER TABLE projects ADD COLUMN IF NOT EXISTS warehouse_code_1c TEXT;
