# TMS Культтовари Глобал — MVP

Планування маршрутів: імпорт заявок із 1С (Excel), CVRPTW-оптимізація (OR-Tools),
OSRM-маршрутизація по дорогах, Leaflet-карта.

## Розгортання (Hetzner / Portainer)

### 1. Підготовка карти OSRM (одноразово, ~15 хв)
```bash
cd tms/osrm
wget https://download.geofabrik.de/europe/ukraine-latest.osm.pbf
docker run -t -v $(pwd):/data ghcr.io/project-osrm/osrm-backend:latest \
  osrm-extract -p /opt/car.lua /data/ukraine-latest.osm.pbf
docker run -t -v $(pwd):/data ghcr.io/project-osrm/osrm-backend:latest \
  osrm-partition /data/ukraine-latest.osrm
docker run -t -v $(pwd):/data ghcr.io/project-osrm/osrm-backend:latest \
  osrm-customize /data/ukraine-latest.osrm
```
RAM у роботі ~1.5 ГБ. Оновлення карти — раз на квартал тим самим скриптом.

### 2. Запуск
```bash
cd tms
DB_PASSWORD=<пароль> docker compose up -d --build
```
API+фронт на порту 8000 контейнера `api`. У Nginx Proxy Manager — новий host
`tms.rpa.com.ua` → `tms-api-1:8000` (мережа n8n_default вже підключена).

### 3. Довідники
Машини/водії — таблиці `vehicles`, `drivers` (seed у db/init.sql, ліміти правити INSERT-ами
або через psql). Наймані машини: `INSERT INTO vehicles (name, max_weight_kg, max_volume_m3, is_hired) VALUES (...)`.

## Використання
1. Обрати дату (за замовчуванням завтра)
2. Імпорт Excel — файл «Задание транспорта (пакетная печать)» з 1С
   (фікс SharedStrings вбудовано, координати з колонок Широта/Долгота)
3. «Розрахувати маршрути» — солвер враховує: вікна клієнтів, зміни водіїв,
   вес+об'єм машини, старт/фініш на складі, 15 хв сервіс на точці
4. Заявки без координат показані ✗ — потрібен геокодинг (фаза 2)

## Roadmap
- [x] Геокодинг (Nominatim за замовчуванням, Visicom через VISICOM_API_KEY)
- [x] Drag-and-drop заявок між маршрутами (перерахунок ETA/км/навантаження, контроль перевантаження)
- [ ] Друк маршрутних листів
- [ ] Синхронізація 1С (HTTP-сервіс / n8n): імпорт заявок + експорт рейсів
- [ ] Claude API: нормалізація адрес, NL-правки плану
