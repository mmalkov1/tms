-- v21: контактний телефон точки (для кнопки «Подзвонити» в кабінеті водія)
-- Заповнення номера — з майбутньої синхронізації 1С; поле nullable, кнопка активується сама.
-- Застосовується автоматично при старті API (init у driver.init / main startup).

ALTER TABLE orders ADD COLUMN IF NOT EXISTS phone TEXT;
