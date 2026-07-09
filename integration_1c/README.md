# Интеграция 1С ↔ TMS (замена Tocan)

## Схема потока

```
1С «Передать все в TMS»  ──XML ORDERS──▶  POST /api/1c/import   (создает/обновляет проект)
                                              │ ответ: SECURITY_KEY проекта (1С хранит)
TMS: планирование, «Розрахувати», маршруты готовы
1С «Загрузить рейсы из TMS» ◀──XML TRIP──  GET /api/1c/export?key=<ключ проекта>
```

## Настройка на стороне TMS (сервер)

1. Логин/пароль API — переменные окружения в `docker-compose.yml` / `.env`:
   `TMS_1C_LOGIN` (по умолчанию `1c`), `TMS_1C_PASSWORD` (по умолчанию `kultukr-1c`).
   **Смените пароль по умолчанию.**
2. Порт API должен быть доступен с сервера 1С (проверить проброс/прокси).
3. В карточках авто (кнопка «Авто» → таблица) заполнить колонку **«Код 1С»**:
   - код авто = код элемента справочника `Kult_Транспорт`;
   - код водителя = код элемента справочника `ФизическиеЛица`.
   Без кодов рейсы выгрузятся, но 1С не найдет транспорт/водителя (`НайтиПоКоду`).

## Настройка на стороне 1С

1. Скопировать процедуры из `РабочееМестоЛогиста_TMS.bsl` в модуль формы
   «Рабочее место логиста» (старый код Tocan не трогаем — работают параллельно).
2. В конфигураторе добавить кнопки командных панелей:
   - Заказы → «Передать все в TMS» → `КоманднаяПанельЗаказыПередатьВсеВTMS`
   - Заказы → «Передать выделенное в TMS» → `КоманднаяПанельЗаказыПередатьВыделенноеВTMS`
   - Рейсы → «Загрузить рейсы из TMS» → `КоманднаяПанельРейсовЗагрузитьРейсыИзTMS`
3. Завести периодические реквизиты для объекта **`tms.kultukr`**
   (по аналогии с `s5.vvtrack.com`): `server`, `port`, `login`, `password`.

## Протокол (для отладки curl-ом)

```bash
# 1. сессионный ключ
curl "http://SERVER/api/1c/auth?login=1c&password=kultukr-1c"

# 2. импорт заказов (новый проект)
curl -X POST "http://SERVER/api/1c/import?key=SESSION_KEY&name_project=08-07_Киев_1&date_project=2026-07-08" \
     -H "Content-Type: application/xml" --data-binary @orders.xml
# ответ содержит MESSAGE/SECURITY_KEY — это ключ ПРОЕКТА

# 3. обновление того же проекта
curl -X POST "http://SERVER/api/1c/import?key=SESSION_KEY&project=PROJECT_KEY" --data-binary @orders.xml

# 4. выгрузка рейсов проекта
curl "http://SERVER/api/1c/export?key=PROJECT_KEY"
```

## Маппинг полей

| 1С (XML)          | TMS orders        | Примечание |
|--------------------|-------------------|------------|
| CODE               | doc_number        | ключ синка (upsert по project+doc_number) |
| NAME               | doc_ref           | + определение забора по слову «Забор/Забір» |
| ADDRESS            | address           | |
| GeoX / GeoY        | lat / lon         | GeoX=широта; пустые не затирают геокод в TMS |
| COMMENTS_SHOP      | address_extra     | + COMMENTS через « · » |
| CLIENT_NAME        | client            | |
| SHOP_WORK_TIME     | tw_from / tw_to   | «HH:MM-HH:MM» |
| UNLOAD_TIME        | service_min       | 0/пусто → 15 |
| PRODUCT WEIGHT/VOLUME | weight_kg / volume_m3 | сумма по модулю; минус или COUNT=-1 → pickup |
| SHOP_DINNER_TIME   | — (пока игнор)    | обед клиента — в бэклог |

| TMS route          | 1С TRIP           |
|--------------------|-------------------|
| route.id           | TRIP_CODE (в 1С Kult_ID = `TMS_<id>`) |
| vehicles.code_1c   | CODE_CAR          |
| drivers.code_1c    | CODE_DRIVER       |
| total_km           | TRIP_DIST_PLAN    |
| plan_date+depart/return | START/FINISH_TIME_PLAN (`yyyy-MM-ddTHH:mm:ss`) |
| stop seq/eta/etd   | IN_TRIP_NUMBER, DELIVERY_DATE_PLAN, DELIVERY_OUTDATE_PLAN |
| —                  | STATUS_POINT=1, факты пустые (план) |
