-- v60: оригінальна адреса з 1С — ключ для кешу геокодування, що переживає правки
ALTER TABLE orders ADD COLUMN IF NOT EXISTS address_1c TEXT;
-- заповнити для існуючих заявок (поточна адреса = найкраще наближення)
UPDATE orders SET address_1c = address WHERE address_1c IS NULL;
