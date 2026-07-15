"""TMS Культтовари Глобал — API v2."""
import os
from datetime import date, datetime, time, timezone

import asyncpg
import asyncio
from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import appupd, driver, dwell, geo, geocoder, importer, integration_1c, osrm, solver

DB_DSN = os.getenv("DATABASE_URL", "postgresql://tms:tms@db:5432/tms")
ROUTE_COLORS = ["#E82A2C", "#00356B", "#2E8B57", "#B8860B", "#8B008B", "#FF6347",
                "#1E90FF", "#FF8C00"]

app = FastAPI(title="TMS Kultukr")


def _stable_geofence_entry(points, depot_lat, depot_lon, distance_km, now=None):
    """Повернути час входу у складську геозону, стійкий до GPS-викидів."""
    if len(points) < 4:
        return None
    now = now or datetime.now(timezone.utc)
    if (now - points[0]["ts"]).total_seconds() > 180:
        return None
    cutoff = points[0]["ts"].timestamp() - 12 * 60
    window = [p for p in points if p["ts"].timestamp() >= cutoff]
    flags = [distance_km(p["lat"], p["lon"], depot_lat, depot_lon) <= 0.3
             for p in window]
    recent = flags[:min(3, len(flags))]
    span_s = (window[0]["ts"] - window[-1]["ts"]).total_seconds()
    stable_recent = sum(recent) >= (len(recent) + 1) // 2
    stable_window = sum(flags) / len(flags) >= 0.7
    if span_s < 600 or not stable_recent or not stable_window:
        return None
    return next(p for p, inside in zip(reversed(window), reversed(flags)) if inside)


async def _route_autoclose_loop():
    """v32: страхувальне авто-закриття рейсів.

    Кожні 10 хв, два рівні:
    1) Геозона складу: всі точки опрацьовані + водій стоїть у радіусі 300 м
       від складу щонайменше 10 хв → finish (ts = момент входу в геозону).
    2) Страховка: 23:30 Києва або минулий день → finish останньою GPS-точкою дня.
    Ручна кнопка «Завершив» завжди пріоритетніша — автоматика лише доганяє.
    """
    from .driver import _haversine_km
    while True:
        try:
            # --- рівень 1: геозона складу ---
            cand = await pool.fetch("""
                SELECT re.route_id, re.driver_id, d.lat AS dlat, d.lon AS dlon
                FROM route_events re
                JOIN routes r ON r.id = re.route_id
                JOIN depots d ON d.id = r.depot_id
                WHERE re.event = 'start'
                  AND r.plan_date = (now() AT TIME ZONE 'Europe/Kyiv')::date
                  AND NOT EXISTS (SELECT 1 FROM route_events f
                                  WHERE f.route_id = re.route_id AND f.event='finish')
                  AND NOT EXISTS (SELECT 1 FROM route_stops s
                                  WHERE s.route_id = re.route_id
                                    AND NOT EXISTS (SELECT 1 FROM stop_events e
                                        WHERE e.route_id = s.route_id AND e.order_id = s.order_id
                                          AND e.event IN ('depart','fail')))""")
            for cd in cand:
                pts = list(await pool.fetch("""
                    SELECT ts, lat, lon FROM gps_points
                    WHERE driver_id = $1 AND ts > now() - interval '15 minutes'
                      AND (accuracy_m IS NULL OR accuracy_m <= 80)
                    ORDER BY ts DESC""", cd["driver_id"]))
                entry = _stable_geofence_entry(
                    pts, cd["dlat"], cd["dlon"], _haversine_km)
                if entry:
                    # Найраніша коректна точка у стабільному вікні — час повернення.
                    await pool.execute("""
                        INSERT INTO route_events (route_id, driver_id, event, ts, lat, lon)
                        VALUES ($1,$2,'finish',$3,$4,$5)
                        ON CONFLICT (route_id, event) DO NOTHING""",
                        cd["route_id"], cd["driver_id"], entry["ts"],
                        entry["lat"], entry["lon"])
            rows = await pool.fetch("""
                SELECT re.route_id, re.driver_id, r.plan_date
                FROM route_events re
                JOIN routes r ON r.id = re.route_id
                WHERE re.event = 'start'
                  AND NOT EXISTS (SELECT 1 FROM route_events f
                                  WHERE f.route_id = re.route_id AND f.event='finish')
                  AND (r.plan_date < (now() AT TIME ZONE 'Europe/Kyiv')::date
                       OR (r.plan_date = (now() AT TIME ZONE 'Europe/Kyiv')::date
                           AND (now() AT TIME ZONE 'Europe/Kyiv')::time >= '23:30'))""")
            for row in rows:
                last_ts = await pool.fetchval("""
                    SELECT max(ts) FROM gps_points
                    WHERE driver_id = $1
                      AND (ts AT TIME ZONE 'Europe/Kyiv')::date = $2""",
                    row["driver_id"], row["plan_date"])
                await pool.execute("""
                    INSERT INTO route_events (route_id, driver_id, event, ts)
                    VALUES ($1, $2, 'finish', COALESCE($3, now()))
                    ON CONFLICT (route_id, event) DO NOTHING""",
                    row["route_id"], row["driver_id"], last_ts)
        except Exception:
            pass                                   # не валимо цикл через разову помилку
        await asyncio.sleep(600)


@app.middleware("http")
async def no_html_cache(request, call_next):
    """v29: HTML завжди свіжий — деплой видно без Ctrl+Shift+R, незалежно від проксі."""
    resp = await call_next(request)
    if request.url.path == "/" or request.url.path.endswith(".html"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp
pool: asyncpg.Pool = None


@app.on_event("startup")
async def startup():
    global pool
    pool = await asyncpg.create_pool(DB_DSN)
    # v47: фактична координата складу; важливо для геозони та меж пробігу.
    await pool.execute("""
        UPDATE depots SET lat=50.423507841149004, lon=30.450054761494783
        WHERE name='Склад Киев'""")
    # v11 (migrate_007): идемпотентно — возможности авто
    await pool.execute("""
        ALTER TABLE vehicles
            ADD COLUMN IF NOT EXISTS can_pickup   BOOLEAN NOT NULL DEFAULT TRUE,
            ADD COLUMN IF NOT EXISTS can_delivery BOOLEAN NOT NULL DEFAULT TRUE""")
    # v13 (migrate_008): коды 1С для маппинга авто/водителей + ключи синхронизации
    await pool.execute("ALTER TABLE vehicles ADD COLUMN IF NOT EXISTS code_1c TEXT")
    await pool.execute("ALTER TABLE drivers  ADD COLUMN IF NOT EXISTS code_1c TEXT")
    # v14 (migrate_009): код склада 1С на проекте — точки выезда/возвращения в экспорте рейсов
    await pool.execute("ALTER TABLE projects ADD COLUMN IF NOT EXISTS warehouse_code_1c TEXT")
    # v15 (migrate_010): кеш геокодирования — исправленные вручную адреса/координаты переживают реимпорт
    await pool.execute("""
        CREATE TABLE IF NOT EXISTS geo_cache (
            address_norm TEXT PRIMARY KEY,
            lat          DOUBLE PRECISION NOT NULL,
            lon          DOUBLE PRECISION NOT NULL,
            source       TEXT,
            updated_at   TIMESTAMPTZ NOT NULL DEFAULT now())""")
    # v30 (migrate_016): події рейсу «виїхав/завершив»
    await pool.execute("""
        CREATE TABLE IF NOT EXISTS route_events (
            id        SERIAL PRIMARY KEY,
            route_id  INT NOT NULL REFERENCES routes(id) ON DELETE CASCADE,
            driver_id INT,
            event     TEXT NOT NULL CHECK (event IN ('start','finish')),
            ts        TIMESTAMPTZ NOT NULL DEFAULT now(),
            lat       DOUBLE PRECISION,
            lon       DOUBLE PRECISION,
            UNIQUE (route_id, event))""")
    # v43 (migrate_020): джерело нормативу простою (tocan-довідник / факти tms)
    await pool.execute(
        "ALTER TABLE client_service_stats ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'tocan'")
    # v41 (migrate_019): типовий графік водія 09:00–16:00 (для нових водіїв)
    await pool.execute("""
        ALTER TABLE drivers
            ALTER COLUMN shift_start SET DEFAULT '09:00',
            ALTER COLUMN shift_end   SET DEFAULT '16:00'""")
    appupd.init()                      # v27: каталог APK для оновлень застосунку
    asyncio.create_task(_route_autoclose_loop())  # v32: нічне авто-закриття рейсів
    await integration_1c.init(pool)
    # v17 (migrate_011): мобильный кабинет водителя — токены, факты, GPS
    await driver.init(pool)
    # v21 (migrate_012): контактный телефон точки — для кнопки «Подзвонити»
    await pool.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS phone TEXT")
    # v22 (migrate_013): количество мест (SEATS из 1С)
    await pool.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS seats INT")
    # v24 (migrate_014): контактное лицо точки (PERSON_NAME из 1С)
    await pool.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS contact_person TEXT")
    # v39 (migrate_018): перерва точки (SHOP_DINNER_TIME из 1С) + ручная блокировка
    await pool.execute("""
        ALTER TABLE orders
            ADD COLUMN IF NOT EXISTS break_from TIME,
            ADD COLUMN IF NOT EXISTS break_to   TIME,
            ADD COLUMN IF NOT EXISTS is_locked  BOOLEAN NOT NULL DEFAULT FALSE""")
    # v39 (migrate_018): робоче вікно машини на конкретну дату (override зміни водія)
    await pool.execute("""
        CREATE TABLE IF NOT EXISTS vehicle_day_windows (
            plan_date  DATE NOT NULL,
            vehicle_id INT NOT NULL REFERENCES vehicles(id) ON DELETE CASCADE,
            work_from  TIME,
            work_to    TIME,
            PRIMARY KEY (plan_date, vehicle_id))""")
    # v25 (migrate_015): активация проекта («У роботу»); бэкфилл — один раз при добавлении колонки
    had_rel = await pool.fetchval("""
        SELECT 1 FROM information_schema.columns
        WHERE table_name='projects' AND column_name='is_released'""")
    await pool.execute(
        "ALTER TABLE projects ADD COLUMN IF NOT EXISTS is_released BOOLEAN NOT NULL DEFAULT FALSE")
    if not had_rel:
        await pool.execute("""
            UPDATE projects SET is_released = TRUE WHERE id IN (
                SELECT DISTINCT ON (r.plan_date) r.project_id
                FROM routes r WHERE r.project_id IS NOT NULL
                ORDER BY r.plan_date, r.project_id DESC)""")


def norm_addr(a: str | None) -> str | None:
    """Нормализация адреса для ключа кеша: нижний регистр, схлопнутые пробелы."""
    return " ".join(a.lower().replace(",", " ").split()) if a else None


async def geo_cache_put(address: str | None, lat, lon, source: str):
    key = norm_addr(address)
    if not key or lat is None or lon is None:
        return
    await pool.execute("""
        INSERT INTO geo_cache (address_norm, lat, lon, source) VALUES ($1,$2,$3,$4)
        ON CONFLICT (address_norm) DO UPDATE
            SET lat=EXCLUDED.lat, lon=EXCLUDED.lon, source=EXCLUDED.source, updated_at=now()""",
        key, float(lat), float(lon), source)


async def geo_cache_fill(project_id: int) -> int:
    """Проставить координаты заявкам проекта из кеша (по нормализованному адресу)."""
    res = await pool.execute("""
        UPDATE orders o SET lat=g.lat, lon=g.lon
        FROM geo_cache g
        WHERE o.project_id=$1 AND o.lat IS NULL AND o.address IS NOT NULL
          AND btrim(lower(regexp_replace(replace(o.address, ',', ' '), '\\s+', ' ', 'g'))) = g.address_norm""",
        project_id)
    try:
        return int(res.split()[-1])
    except Exception:
        return 0


def t2m(t: time | None, default: int) -> int:
    return t.hour * 60 + t.minute if t else default


def m2t(m: int) -> time:
    return time(m // 60 % 24, m % 60)


def running_loads(stops_rows):
    """[(cum_w, cum_v)] после каждой точки + (start_w, start_v, peak_w, peak_v).
    Доставки выезжают со склада, заборы добавляются в пути."""
    start_w = sum(float(s["weight_kg"] or 0) for s in stops_rows if s["kind"] == "delivery")
    start_v = sum(float(s["volume_m3"] or 0) for s in stops_rows if s["kind"] == "delivery")
    w, v, peak_w, peak_v, out = start_w, start_v, start_w, start_v, []
    for s in stops_rows:
        sign = 1 if s["kind"] == "pickup" else -1
        w += sign * float(s["weight_kg"] or 0)
        v += sign * float(s["volume_m3"] or 0)
        peak_w, peak_v = max(peak_w, w), max(peak_v, v)
        out.append((round(w, 2), round(v, 3)))
    return out, start_w, start_v, peak_w, peak_v


def parse_hhmm(s: str, default: int) -> int:
    try:
        h, m = s.strip().split(":")
        return int(h) * 60 + int(m)
    except Exception:
        return default


# ---------- проекты и импорт ----------

@app.get("/api/projects")
async def get_projects(plan_date: date = Query(...)):
    rows = await pool.fetch("""
        SELECT p.*, (SELECT count(*) FROM orders o WHERE o.project_id=p.id) AS orders_count,
               (SELECT count(*) FROM routes r WHERE r.project_id=p.id) AS routes_count
        FROM projects p WHERE p.plan_date=$1 ORDER BY p.id""", plan_date)
    return [dict(r) for r in rows]


@app.post("/api/projects/{project_id}/release")
async def release_project(project_id: int):
    """«У роботу»: активний проект на дату може бути лише один."""
    p = await pool.fetchrow("SELECT id, plan_date FROM projects WHERE id=$1", project_id)
    if not p:
        raise HTTPException(404, "Проект не знайдено")
    async with pool.acquire() as c:
        await c.execute(
            "UPDATE projects SET is_released=FALSE WHERE plan_date=$1", p["plan_date"])
        await c.execute(
            "UPDATE projects SET is_released=TRUE WHERE id=$1", project_id)
    return {"ok": True}


@app.post("/api/projects/{project_id}/unrelease")
async def unrelease_project(project_id: int):
    await pool.execute("UPDATE projects SET is_released=FALSE WHERE id=$1", project_id)
    return {"ok": True}


async def _new_project(plan_date: date) -> int:
    n = await pool.fetchval("SELECT count(*)+1 FROM projects WHERE plan_date=$1", plan_date)
    return await pool.fetchval(
        "INSERT INTO projects (plan_date, name) VALUES ($1,$2) RETURNING id",
        plan_date, plan_date.strftime("%d-%m") + f"_{n}")


@app.post("/api/import")
async def import_excel(plan_date: date = Query(...), file: UploadFile = File(...),
                       project_id: int = Query(None)):
    """project_id пуст — создается новый проект; задан — обновляется существующий."""
    rows = importer.parse_orders(await file.read(), str(plan_date))
    if project_id is None:
        project_id = await _new_project(plan_date)
    ins = upd = 0
    async with pool.acquire() as c:
        for r in rows:
            res = await c.execute("""
                INSERT INTO orders (plan_date, doc_number, doc_ref, kind, client, address,
                    address_extra, lat, lon, tw_from, tw_to, service_min, weight_kg, volume_m3,
                    status_1c, project_id)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16)
                ON CONFLICT (project_id, doc_number) DO UPDATE SET
                    kind=EXCLUDED.kind, client=EXCLUDED.client, address=EXCLUDED.address,
                    lat=EXCLUDED.lat, lon=EXCLUDED.lon, tw_from=EXCLUDED.tw_from,
                    tw_to=EXCLUDED.tw_to, weight_kg=EXCLUDED.weight_kg,
                    volume_m3=EXCLUDED.volume_m3, status_1c=EXCLUDED.status_1c
            """, date.fromisoformat(r["plan_date"]), r["doc_number"], r["doc_ref"],
                r["kind"], r["client"], r["address"], r["address_extra"], r["lat"], r["lon"],
                r["tw_from"], r["tw_to"], r["service_min"], r["weight_kg"], r["volume_m3"],
                r["status_1c"], project_id)
            if res.startswith("INSERT"):
                ins += 1
            else:
                upd += 1
    # координаты из кеша (исправленные ранее адреса) — до подсчета "без координат"
    await geo_cache_fill(project_id)
    no_geo = await pool.fetchval(
        "SELECT count(*) FROM orders WHERE project_id=$1 AND lat IS NULL", project_id)
    return {"parsed": len(rows), "inserted": ins, "updated": upd,
            "without_coords": no_geo, "project_id": project_id}


# ---------- справочники и заявки ----------

@app.get("/api/orders")
async def orders(project_id: int = Query(...)):
    rows = await pool.fetch("SELECT * FROM orders WHERE project_id=$1 ORDER BY id", project_id)
    return [dict(r) for r in rows]


@app.get("/api/vehicles")
async def vehicles():
    rows = await pool.fetch("""
        SELECT v.*, d.name AS driver_name, d.shift_start, d.shift_end, d.code_1c AS driver_code_1c
        FROM vehicles v LEFT JOIN drivers d ON d.id=v.driver_id AND d.is_active
        WHERE v.is_active ORDER BY v.id""")
    return [dict(r) for r in rows]


class VehicleIn(BaseModel):
    name: str
    plate: str | None = None
    max_weight_kg: float
    max_volume_m3: float
    is_hired: bool = False
    can_pickup: bool = True
    can_delivery: bool = True
    driver_id: int | None = None        # існуючий водій (пріоритет)
    driver_name: str | None = None      # новий водій (створюється, якщо ПІБ не знайдено)
    shift_start: str = "09:00"
    shift_end: str = "16:00"


@app.get("/api/drivers")
async def get_drivers():
    rows = await pool.fetch("""
        SELECT d.id, d.name, d.code_1c, d.phone, d.shift_start, d.shift_end,
               t.created_at AS token_created
        FROM drivers d
        LEFT JOIN driver_tokens t ON t.driver_id = d.id AND t.is_active
        WHERE d.is_active ORDER BY d.name""")
    return [dict(r) for r in rows]


class DriverIn(BaseModel):
    name: str


@app.post("/api/drivers")
async def create_driver(d: DriverIn):
    name = d.name.strip()
    if not name:
        raise HTTPException(400, "Вкажи ПІБ")
    ex = await pool.fetchval(
        "SELECT id FROM drivers WHERE is_active AND upper(trim(name))=upper($1)", name)
    if ex:
        raise HTTPException(409, "Такий водій вже є")
    did = await pool.fetchval(
        "INSERT INTO drivers (name, shift_start, shift_end) VALUES ($1,$2,$3) RETURNING id",
        name, m2t(9 * 60), m2t(16 * 60))
    return {"driver_id": did}


class DriverPatch(BaseModel):
    name: str | None = None
    phone: str | None = None
    code_1c: str | None = None
    shift_start: str | None = None      # v41: постійний графік водія "HH:MM"
    shift_end: str | None = None
    is_active: bool | None = None


@app.patch("/api/drivers/{driver_id}")
async def patch_driver(driver_id: int, d: DriverPatch):
    cur = await pool.fetchrow("SELECT id FROM drivers WHERE id=$1", driver_id)
    if not cur:
        raise HTTPException(404, "Водія не знайдено")
    if d.name is not None and d.name.strip():
        await pool.execute("UPDATE drivers SET name=$1 WHERE id=$2", d.name.strip(), driver_id)
    if d.phone is not None:
        await pool.execute("UPDATE drivers SET phone=$1 WHERE id=$2",
                           d.phone.strip() or None, driver_id)
    if d.code_1c is not None:
        await pool.execute("UPDATE drivers SET code_1c=$1 WHERE id=$2",
                           d.code_1c.strip() or None, driver_id)
    if d.shift_start is not None and d.shift_end is not None:            # v41
        ss, se = parse_hhmm(d.shift_start, -1), parse_hhmm(d.shift_end, -1)
        if ss < 0 or se < 0 or se <= ss:
            raise HTTPException(400, "Графік: формат HH:MM, кінець пізніше початку")
        await pool.execute("UPDATE drivers SET shift_start=$1, shift_end=$2 WHERE id=$3",
                           m2t(ss), m2t(se), driver_id)
    if d.is_active is not None:
        await pool.execute("UPDATE drivers SET is_active=$1 WHERE id=$2",
                           d.is_active, driver_id)
        if not d.is_active:   # архів водія — гасимо його токени
            await pool.execute(
                "UPDATE driver_tokens SET is_active=FALSE WHERE driver_id=$1", driver_id)
    return {"ok": True}


@app.post("/api/vehicles")
async def create_vehicle(v: VehicleIn):
    driver_id = None
    if v.driver_id:
        driver_id = await pool.fetchval(
            "SELECT id FROM drivers WHERE id=$1 AND is_active", v.driver_id)
        if not driver_id:
            raise HTTPException(404, "Водія не знайдено")
    elif v.driver_name:
        # захист від дублів: спершу шукаємо активного водія з таким ПІБ
        driver_id = await pool.fetchval(
            "SELECT id FROM drivers WHERE is_active AND upper(trim(name))=upper(trim($1))",
            v.driver_name)
        if not driver_id:
            driver_id = await pool.fetchval(
                "INSERT INTO drivers (name, shift_start, shift_end) VALUES ($1,$2,$3) RETURNING id",
                v.driver_name.strip(), m2t(parse_hhmm(v.shift_start, 480)),
                m2t(parse_hhmm(v.shift_end, 1080)))
    vid = await pool.fetchval("""
        INSERT INTO vehicles (name, plate, max_weight_kg, max_volume_m3, is_hired, can_pickup, can_delivery, driver_id)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8) RETURNING id""",
        v.name, v.plate, v.max_weight_kg, v.max_volume_m3, v.is_hired, v.can_pickup, v.can_delivery, driver_id)
    return {"vehicle_id": vid}


class VehiclePatch(BaseModel):
    name: str | None = None
    max_weight_kg: float | None = None
    max_volume_m3: float | None = None
    is_hired: bool | None = None
    can_pickup: bool | None = None
    can_delivery: bool | None = None
    code_1c: str | None = None          # код авто в 1С (Kult_Транспорт)
    driver_code_1c: str | None = None   # код водителя в 1С (ФизическиеЛица)
    driver_id: int | None = None        # None = не змінювати, 0 = зняти, >0 = призначити


@app.patch("/api/vehicles/{vehicle_id}")
async def patch_vehicle(vehicle_id: int, v: VehiclePatch):
    cur = await pool.fetchrow("SELECT * FROM vehicles WHERE id=$1", vehicle_id)
    if not cur:
        raise HTTPException(404, "Не знайдено")
    await pool.execute("""
        UPDATE vehicles SET name=$1, max_weight_kg=$2, max_volume_m3=$3, is_hired=$4,
                            can_pickup=$5, can_delivery=$6, code_1c=$7 WHERE id=$8""",
        v.name or cur["name"],
        v.max_weight_kg if v.max_weight_kg is not None else cur["max_weight_kg"],
        v.max_volume_m3 if v.max_volume_m3 is not None else cur["max_volume_m3"],
        v.is_hired if v.is_hired is not None else cur["is_hired"],
        v.can_pickup if v.can_pickup is not None else cur["can_pickup"],
        v.can_delivery if v.can_delivery is not None else cur["can_delivery"],
        v.code_1c if v.code_1c is not None else cur["code_1c"], vehicle_id)
    if v.driver_id is not None:         # 0 = зняти водія
        new_drv = None
        if v.driver_id > 0:
            new_drv = await pool.fetchval(
                "SELECT id FROM drivers WHERE id=$1 AND is_active", v.driver_id)
            if not new_drv:
                raise HTTPException(404, "Водія не знайдено")
        await pool.execute("UPDATE vehicles SET driver_id=$1 WHERE id=$2", new_drv, vehicle_id)
        cur = await pool.fetchrow("SELECT * FROM vehicles WHERE id=$1", vehicle_id)
    if v.driver_code_1c is not None and cur["driver_id"]:
        await pool.execute("UPDATE drivers SET code_1c=$1 WHERE id=$2",
                           v.driver_code_1c, cur["driver_id"])
    return {"ok": True}


@app.delete("/api/vehicles/{vehicle_id}")
async def deactivate_vehicle(vehicle_id: int):
    await pool.execute("UPDATE vehicles SET is_active=FALSE WHERE id=$1", vehicle_id)
    return {"ok": True}


# ---------- нормативы простоя ----------

@app.get("/api/service-stats")
async def service_stats():
    rows = await pool.fetch("SELECT * FROM client_service_stats ORDER BY visits DESC")
    return [dict(r) for r in rows]


@app.post("/api/service-stats/import")
async def import_service_stats(file: UploadFile = File(...)):
    """Обновление нормативов свежим Cars_report.csv (полный пересчет по файлу)."""
    stats = dwell.aggregate(dwell.parse_report(await file.read()))
    for st_ in stats:
        await pool.execute("""
            INSERT INTO client_service_stats (client_key, addr_key, client_name, address,
                lat, lon, visits, median_min, p80_min, updated_at, source)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,now(),'tocan')
            ON CONFLICT (client_key, addr_key) DO UPDATE SET visits=EXCLUDED.visits,
                address=EXCLUDED.address, lat=EXCLUDED.lat, lon=EXCLUDED.lon,
                median_min=EXCLUDED.median_min, p80_min=EXCLUDED.p80_min, updated_at=now()
            WHERE client_service_stats.source <> 'tms'""",
            st_["client_key"], st_["addr_key"], st_["client_name"], st_["address"],
            st_["lat"], st_["lon"], st_["visits"], st_["median_min"], st_["p80_min"])
    return {"rows": len(stats)}


async def _refresh_service_stats_tms():
    """v43: нормативи простою з власних фактів TMS (stop_events прибув→поїхав).

    Одна фізична зупинка = рейс × клієнт+адреса (від першого arrive до
    останнього depart). Конвеєр той самий, що для Cars_report:
    dwell.aggregate — фільтри шуму 2–180 хв, нічних баз, нормалізація ключів.
    Рядок Tocan замінюється, щойно TMS накопичила ≥5 візитів по ключу;
    далі рядок source='tms' і оновлюється перед кожним розрахунком."""
    rows = await pool.fetch("""
        SELECT o.client AS name, COALESCE(o.address,'') AS address,
               max(o.lat) AS lat, max(o.lon) AS lon,
               EXTRACT(EPOCH FROM (max(d.ts) - min(a.ts)))/60.0 AS dwell,
               EXTRACT(HOUR FROM min(a.ts) AT TIME ZONE 'Europe/Kiev')::int AS hour
        FROM stop_events a
        JOIN stop_events d ON d.route_id=a.route_id AND d.order_id=a.order_id
                          AND d.event='depart'
        JOIN orders o ON o.id=a.order_id
        WHERE a.event='arrive' AND d.ts > a.ts
        GROUP BY a.route_id, o.client, o.address""")
    visits = [{"code": "", "name": r["name"] or "", "address": r["address"],
               "lat": r["lat"], "lon": r["lon"], "dwell": float(r["dwell"]),
               "hour": r["hour"]} for r in rows]
    stats = dwell.aggregate(visits)
    for s in stats:
        await pool.execute("""
            INSERT INTO client_service_stats (client_key, addr_key, client_name, address,
                lat, lon, visits, median_min, p80_min, updated_at, source)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,now(),'tms')
            ON CONFLICT (client_key, addr_key) DO UPDATE SET visits=EXCLUDED.visits,
                address=EXCLUDED.address, lat=EXCLUDED.lat, lon=EXCLUDED.lon,
                median_min=EXCLUDED.median_min, p80_min=EXCLUDED.p80_min,
                updated_at=now(), source='tms'
            WHERE client_service_stats.source='tms' OR EXCLUDED.visits >= 5""",
            s["client_key"], s["addr_key"], s["client_name"], s["address"],
            s["lat"], s["lon"], s["visits"], s["median_min"], s["p80_min"])
    return len(stats)


# ---------- геозоны ----------

@app.post("/api/geozones/import")
async def import_geozones(file: UploadFile = File(...)):
    import io as _io
    import json as _json

    import pandas as pd
    df = pd.read_excel(_io.BytesIO(importer.fix_1c_xlsx(await file.read())))
    df.columns = [str(c).strip() for c in df.columns]
    n = 0
    for _, r in df.iterrows():
        try:
            pts = geo.parse_wkt_polygon(str(r["Координати"]))
        except (ValueError, KeyError):
            continue
        await pool.execute("""
            INSERT INTO geozones (name, points) VALUES ($1, $2)
            ON CONFLICT (name) DO UPDATE SET points=EXCLUDED.points""",
            str(r["Назва зони"]).strip(), _json.dumps(pts))
        n += 1
    return {"imported": n}


@app.get("/api/geozones")
async def get_geozones():
    import json as _json
    rows = await pool.fetch("SELECT * FROM geozones ORDER BY id")
    return [{"id": r["id"], "name": r["name"], "points": _json.loads(r["points"])} for r in rows]


@app.get("/api/drivers/{driver_id}/zones")
async def get_driver_zones(driver_id: int):
    rows = await pool.fetch("SELECT zone_id FROM driver_zones WHERE driver_id=$1", driver_id)
    return [r["zone_id"] for r in rows]


class ZoneIds(BaseModel):
    zone_ids: list[int]


@app.put("/api/drivers/{driver_id}/zones")
async def set_driver_zones(driver_id: int, body: ZoneIds):
    async with pool.acquire() as c:
        await c.execute("DELETE FROM driver_zones WHERE driver_id=$1", driver_id)
        for z in body.zone_ids:
            await c.execute("INSERT INTO driver_zones VALUES ($1,$2)", driver_id, z)
    return {"ok": True}


# ---------- геокодинг ----------

@app.post("/api/geocode")
async def geocode_missing(project_id: int = Query(...)):
    # сначала кеш (исправленные вручную / ранее геокодированные адреса)
    from_cache = await geo_cache_fill(project_id)
    rows = await pool.fetch(
        "SELECT id, address FROM orders WHERE project_id=$1 AND lat IS NULL AND address IS NOT NULL",
        project_id)
    ok, fail = from_cache, []
    for r in rows:
        res = await geocoder.geocode(r["address"])
        if res:
            await pool.execute("UPDATE orders SET lat=$1, lon=$2 WHERE id=$3", res[0], res[1], r["id"])
            await geo_cache_put(r["address"], res[0], res[1], "geocoder")
            ok += 1
        else:
            fail.append({"id": r["id"], "address": r["address"]})
    return {"geocoded": ok, "failed": fail}


class OrderPatch(BaseModel):
    address: str | None = None
    lat: float | None = None
    lon: float | None = None


@app.patch("/api/orders/{order_id}")
async def patch_order(order_id: int, b: OrderPatch):
    cur = await pool.fetchrow("SELECT * FROM orders WHERE id=$1", order_id)
    if not cur:
        raise HTTPException(404, "Заявку не знайдено")
    new_addr = b.address if b.address is not None else cur["address"]
    new_lat = b.lat if b.lat is not None else cur["lat"]
    new_lon = b.lon if b.lon is not None else cur["lon"]
    await pool.execute("UPDATE orders SET address=$1, lat=$2, lon=$3 WHERE id=$4",
        new_addr, new_lat, new_lon, order_id)
    # исправленные вручную координаты запоминаем: следующий импорт возьмет их из кеша
    await geo_cache_put(new_addr, new_lat, new_lon, "manual")
    return {"ok": True}


@app.post("/api/orders/{order_id}/lock")   # v39: замок від авторозподілу
async def toggle_lock(order_id: int):
    new = await pool.fetchval(
        "UPDATE orders SET is_locked = NOT is_locked WHERE id=$1 RETURNING is_locked", order_id)
    if new is None:
        raise HTTPException(404, "Заявку не знайдено")
    return {"is_locked": new}


# v39: робочі вікна машин на дату
@app.get("/api/day-windows")
async def get_day_windows(plan_date: date = Query(...)):
    rows = await pool.fetch(
        "SELECT vehicle_id, work_from, work_to FROM vehicle_day_windows WHERE plan_date=$1",
        plan_date)
    return [dict(r) for r in rows]


class DayWindowIn(BaseModel):
    plan_date: date
    vehicle_id: int
    work_from: str | None = None    # "HH:MM"; обидва пусті = скинути до графіка
    work_to: str | None = None


@app.put("/api/day-windows")
async def put_day_window(b: DayWindowIn):
    def _t(s):
        if not s:
            return None
        v = parse_hhmm(s, -1)
        if v < 0:
            raise HTTPException(400, "Формат часу HH:MM")
        return m2t(v)
    wf, wt = _t(b.work_from), _t(b.work_to)
    if not wf and not wt:
        await pool.execute(
            "DELETE FROM vehicle_day_windows WHERE plan_date=$1 AND vehicle_id=$2",
            b.plan_date, b.vehicle_id)
        return {"ok": True, "cleared": True}
    await pool.execute("""
        INSERT INTO vehicle_day_windows (plan_date, vehicle_id, work_from, work_to)
        VALUES ($1,$2,$3,$4)
        ON CONFLICT (plan_date, vehicle_id) DO UPDATE
            SET work_from=EXCLUDED.work_from, work_to=EXCLUDED.work_to""",
        b.plan_date, b.vehicle_id, wf, wt)
    return {"ok": True}


@app.post("/api/orders/{order_id}/geocode")
async def geocode_one(order_id: int):
    cur = await pool.fetchrow("SELECT address FROM orders WHERE id=$1", order_id)
    if not cur or not cur["address"]:
        raise HTTPException(400, "Немає адреси")
    res = await geocoder.geocode(cur["address"])
    if not res:
        raise HTTPException(422, "Адресу не знайдено — спробуй виправити її або ввести координати")
    await pool.execute("UPDATE orders SET lat=$1, lon=$2 WHERE id=$3", res[0], res[1], order_id)
    await geo_cache_put(cur["address"], res[0], res[1], "geocoder")
    return {"lat": res[0], "lon": res[1]}


# ---------- планирование ----------

@app.post("/api/plan")
async def plan(
    project_id: int = Query(...),
    plan_date: date = Query(...),
    vehicle_ids: str = Query(None),          # "1,2,5" — выбор машин (п.1)
    service_min: int = Query(None),          # простой на точке (ручной / fallback)
    service_source: str = Query("manual"),   # manual | hist_med | hist_p80
    depot_depart: str = Query("09:00"),      # выезд со склада (п.7)
    depot_return: str = Query("16:00"),      # возврат (п.7)
    use_zones: bool = Query(False),          # учитывать геозоны водителей
    zone_penalty: int = Query(20),           # мягкость зон: штраф хв за чужую точку; >=240 = жестко
    balance: str = Query("soft"),            # off | soft | hard — выравнивание машин
    time_limit: int = 15,
):
    depart_m = parse_hhmm(depot_depart, 9 * 60)
    return_m = parse_hhmm(depot_return, 16 * 60)
    if return_m <= depart_m:
        raise HTTPException(400, "Час повернення має бути пізніше виїзду")

    depot = await pool.fetchrow("SELECT * FROM depots WHERE id=1")
    vrows = await pool.fetch("""
        SELECT v.*, COALESCE(d.shift_start,'09:00'::time) ss, COALESCE(d.shift_end,'16:00'::time) se
        FROM vehicles v LEFT JOIN drivers d ON d.id=v.driver_id WHERE v.is_active ORDER BY v.id""")
    if vehicle_ids:
        want = {int(x) for x in vehicle_ids.split(",")}
        vrows = [v for v in vrows if v["id"] in want]
    if not vrows:
        raise HTTPException(400, "Не обрано жодної машини")

    # простой: ручной для всех ИЛИ персональный из истории по клиент+адресу
    fallback = service_min or 15
    if service_source in ("hist_med", "hist_p80"):
        try:
            await _refresh_service_stats_tms()             # v43: свіжі факти TMS
        except Exception as e:
            print("service-stats refresh failed:", e)      # нормативи лишаться попередні
        srows = await pool.fetch("SELECT * FROM client_service_stats")
        by_client = {}
        for r in srows:
            by_client.setdefault(r["client_key"], []).append(dict(r))
        ords = await pool.fetch("SELECT id, client, lat, lon FROM orders WHERE project_id=$1", project_id)
        for o in ords:
            val = dwell.pick_service_min(by_client, o["client"], o["lat"], o["lon"],
                                         service_source, fallback)
            await pool.execute("UPDATE orders SET service_min=$1 WHERE id=$2", val, o["id"])
    elif service_min:
        await pool.execute("UPDATE orders SET service_min=$1 WHERE project_id=$2", service_min, project_id)

    # v39: рейси з замкненими точками переживають перерахунок
    keep_routes = {r["route_id"] for r in await pool.fetch("""
        SELECT DISTINCT s.route_id FROM route_stops s
        JOIN routes r ON r.id = s.route_id
        JOIN orders o ON o.id = s.order_id
        WHERE r.project_id=$1 AND r.status='draft' AND o.is_locked""", project_id)}
    placed_kept = {r["order_id"] for r in await pool.fetch(
        "SELECT order_id FROM route_stops WHERE route_id = ANY($1::int[])", list(keep_routes))} \
        if keep_routes else set()

    orows = await pool.fetch(
        "SELECT * FROM orders WHERE project_id=$1 AND lat IS NOT NULL AND lon IS NOT NULL"
        " AND NOT is_locked ORDER BY id",
        project_id)
    orows = [o for o in orows if o["id"] not in placed_kept]
    if not orows:
        raise HTTPException(400, "Немає заявок з координатами на дату (не замкнених)")

    points = [(depot["lat"], depot["lon"])] + [(o["lat"], o["lon"]) for o in orows]
    durations, distances = await osrm.table(points)

    stops = [solver.Stop(
        order_id=i + 1,
        tw_from=t2m(o["tw_from"], 8 * 60),
        tw_to=t2m(o["tw_to"], 20 * 60),
        service_min=o["service_min"],
        weight=float(o["weight_kg"] or 0),
        volume=float(o["volume_m3"] or 0),
        kind=o["kind"],
        break_from=t2m(o["break_from"], None) if o["break_from"] else None,   # v39
        break_to=t2m(o["break_to"], None) if o["break_to"] else None,
    ) for i, o in enumerate(orows)]

    # смена машины = пересечение смены водителя и окна склада (п.7)
    # v39: + робоче вікно машини саме на цю дату (пріоритетніше за графік водія)
    dw = {r["vehicle_id"]: r for r in await pool.fetch(
        "SELECT * FROM vehicle_day_windows WHERE plan_date=$1", plan_date)}
    def _shift(v):
        o = dw.get(v["id"])
        ss = t2m(o["work_from"], None) if o and o["work_from"] else t2m(v["ss"], 8 * 60)
        se = t2m(o["work_to"], None) if o and o["work_to"] else t2m(v["se"], 18 * 60)
        return max(ss, depart_m), min(se, return_m)
    trucks = [solver.Truck(
        vehicle_id=v["id"],
        max_weight=float(v["max_weight_kg"]),
        max_volume=float(v["max_volume_m3"]),
        shift_start=_shift(v)[0],
        shift_end=_shift(v)[1],
    ) for v in vrows]
    if any(tr.shift_end <= tr.shift_start for tr in trucks):
        bad = [v["name"] for v, tr in zip(vrows, trucks) if tr.shift_end <= tr.shift_start]
        raise HTTPException(400, "Порожнє робоче вікно: " + ", ".join(bad))

    allowed = None
    zone_stats = {}
    if use_zones:
        import json as _json
        zrows = await pool.fetch("SELECT * FROM geozones ORDER BY id")
        zones = [{"id": z["id"], "points": _json.loads(z["points"])} for z in zrows]
        dz = await pool.fetch("SELECT driver_id, zone_id FROM driver_zones")
        drv_zones = {}
        for r in dz:
            drv_zones.setdefault(r["driver_id"], set()).add(r["zone_id"])
        veh_zones = [drv_zones.get(v["driver_id"], set()) for v in vrows]
        allowed = []
        for o in orows:
            zid = geo.zone_of(o["lat"], o["lon"], zones)
            if zid is None:
                allowed.append(None)     # вне зон — любая машина
                zone_stats["поза зонами"] = zone_stats.get("поза зонами", 0) + 1
            else:
                ok_v = [i for i, vz in enumerate(veh_zones) if not vz or zid in vz]
                allowed.append(ok_v if ok_v else None)  # зону никто не обслуживает — не блокируем
                zname = next(z["name"] for z in zrows if z["id"] == zid)
                zone_stats[zname] = zone_stats.get(zname, 0) + 1

    span = {"off": 0, "soft": 10, "hard": 100}.get(balance, 10)
    zpen = None if (not use_zones or zone_penalty >= 240) else max(1, zone_penalty)

    # возможности авто: забор/доставка — жесткое ограничение (наемный "только доставка" и т.п.)
    cap_allowed = None
    if any(not v["can_pickup"] or not v["can_delivery"] for v in vrows):
        cap_allowed = []
        for o in orows:
            key = "can_pickup" if o["kind"] == "pickup" else "can_delivery"
            ok = [i for i, v in enumerate(vrows) if v[key]]
            cap_allowed.append(ok if len(ok) < len(vrows) else None)

    routes_idx = solver.solve(stops, trucks, durations, time_limit, allowed,
                              zone_penalty_min=zpen, span_cost=span, hard_allowed=cap_allowed)
    if routes_idx is None:
        raise HTTPException(422, "Рішення не знайдено — перевір вікна/ліміти")

    async with pool.acquire() as c:
        await c.execute(
            "DELETE FROM routes WHERE project_id=$1 AND status='draft'"
            " AND NOT (id = ANY($2::int[]))",             # v39: рейси з замками лишаються
            project_id, list(keep_routes))
        out, dropped = [], set(range(len(orows)))
        for v_i, seq in enumerate(routes_idx):
            if not seq:
                continue
            dropped -= set(seq)
            tr, veh = trucks[v_i], vrows[v_i]
            sched = solver.eta_schedule([stops[i] for i in seq], durations, tr.shift_start)
            node_seq = [0] + [i + 1 for i in seq] + [0]
            geom = await osrm.route_geometry([points[n] for n in node_seq])
            km = sum(distances[node_seq[j]][node_seq[j + 1]] for j in range(len(node_seq) - 1)) / 1000
            ret = sched[-1][1] + durations[seq[-1] + 1][0] // 60  # возврат: ETD последней + перегон (п.8)

            seq_rows = [{"kind": stops[i].kind, "weight_kg": stops[i].weight,
                         "volume_m3": stops[i].volume} for i in seq]
            _, _, _, peak_w, peak_v = running_loads(seq_rows)
            rid = await c.fetchval("""
                INSERT INTO routes (plan_date, vehicle_id, driver_id, color, total_km,
                    load_weight, load_volume, geometry, depart_time, return_time, project_id)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11) RETURNING id""",
                plan_date, veh["id"], veh["driver_id"], ROUTE_COLORS[v_i % len(ROUTE_COLORS)],
                round(km, 1), peak_w, peak_v, geom,
                m2t(tr.shift_start), m2t(ret), project_id)

            for pos, (si, (eta, etd)) in enumerate(zip(seq, sched), start=1):
                await c.execute(
                    "INSERT INTO route_stops (route_id, order_id, seq, eta, etd) VALUES ($1,$2,$3,$4,$5)",
                    rid, orows[si]["id"], pos, m2t(eta), m2t(etd))
            out.append({"route_id": rid, "vehicle": veh["name"], "stops": len(seq), "km": round(km, 1)})

    return {"routes": out, "dropped_orders": [orows[i]["id"] for i in dropped],
            "zone_stats": zone_stats}


# ---------- ручное управление маршрутами ----------

class NewRoute(BaseModel):
    plan_date: date
    project_id: int
    vehicle_id: int
    depot_depart: str = "09:00"
    depot_return: str | None = None     # v41: кінець роботи маршруту


@app.post("/api/routes")   # п.4: добавить машину в день вручную
async def create_route(body: NewRoute):
    veh = await pool.fetchrow("SELECT * FROM vehicles WHERE id=$1", body.vehicle_id)
    if not veh:
        raise HTTPException(404, "Машина не знайдена")
    used = await pool.fetch("SELECT color FROM routes WHERE project_id=$1", body.project_id)
    used_colors = {u["color"] for u in used}
    color = next((c for c in ROUTE_COLORS if c not in used_colors), ROUTE_COLORS[0])
    dep_m = parse_hhmm(body.depot_depart, 9 * 60)                        # v41
    ret_m = parse_hhmm(body.depot_return, 16 * 60) if body.depot_return else 16 * 60
    if ret_m <= dep_m:
        raise HTTPException(400, "Кінець роботи має бути пізніше початку")
    rid = await pool.fetchval("""
        INSERT INTO routes (plan_date, vehicle_id, driver_id, color, total_km,
            load_weight, load_volume, depart_time, return_time, project_id)
        VALUES ($1,$2,$3,$4,0,0,0,$5,$6,$7) RETURNING id""",
        body.plan_date, veh["id"], veh["driver_id"], color,
        m2t(dep_m), m2t(ret_m), body.project_id)
    return {"route_id": rid}


@app.delete("/api/routes/{route_id}")
async def delete_route(route_id: int):
    n = await pool.fetchval("SELECT count(*) FROM route_stops WHERE route_id=$1", route_id)
    if n:
        raise HTTPException(400, f"На маршруті {n} точок — спочатку прибери їх")
    await pool.execute("DELETE FROM routes WHERE id=$1", route_id)
    return {"ok": True}


class SetStops(BaseModel):
    order_ids: list[int]


async def _rebuild_route(route_id: int):
    r = await pool.fetchrow("""
        SELECT r.id, r.vehicle_id, r.depart_time, d.lat, d.lon,
               COALESCE(dr.shift_start,'08:00'::time) ss
        FROM routes r JOIN depots d ON d.id=r.depot_id
        LEFT JOIN drivers dr ON dr.id=r.driver_id WHERE r.id=$1""", route_id)
    ss = await pool.fetch("""
        SELECT s.order_id, o.lat, o.lon, o.tw_from, o.break_from, o.break_to,
               o.service_min, o.weight_kg, o.volume_m3, o.kind
        FROM route_stops s JOIN orders o ON o.id=s.order_id
        WHERE s.route_id=$1 ORDER BY s.seq""", route_id)
    if not ss:
        await pool.execute("""UPDATE routes SET geometry=NULL, total_km=0, total_min=0,
            load_weight=0, load_volume=0, return_time=NULL WHERE id=$1""", route_id)
        return
    _, _, _, peak_w, peak_v = running_loads(ss)
    points = [(r["lat"], r["lon"])] + [(s["lat"], s["lon"]) for s in ss] + [(r["lat"], r["lon"])]
    geom, legs, km = await osrm.route_with_legs(points)
    start = t2m(r["depart_time"], None) if r["depart_time"] else t2m(r["ss"], 9 * 60)
    t = start
    for i, s in enumerate(ss):
        t += legs[i] // 60
        t = max(t, t2m(s["tw_from"], 0))
        if s["break_from"] and s["break_to"]:              # v39: перерва
            bf, bt = t2m(s["break_from"], 0), t2m(s["break_to"], 0)
            if bf - s["service_min"] < t < bt:
                t = bt
        await pool.execute(
            "UPDATE route_stops SET eta=$1, etd=$2 WHERE route_id=$3 AND order_id=$4",
            m2t(t), m2t(t + s["service_min"]), route_id, s["order_id"])
        t += s["service_min"]
    ret = t + legs[-1] // 60
    await pool.execute("""
        UPDATE routes SET geometry=$1, total_km=$2, total_min=$3, load_weight=$4,
            load_volume=$5, return_time=$6 WHERE id=$7""",
        geom, round(km, 1), ret - start, peak_w, peak_v, m2t(ret), route_id)


@app.put("/api/routes/{route_id}/stops")
async def set_stops(route_id: int, body: SetStops):
    async with pool.acquire() as c:
        affected = {route_id}
        if body.order_ids:
            others = await c.fetch(
                "SELECT DISTINCT route_id FROM route_stops WHERE order_id = ANY($1) AND route_id<>$2",
                body.order_ids, route_id)
            affected |= {o["route_id"] for o in others}
            await c.execute("DELETE FROM route_stops WHERE order_id = ANY($1)", body.order_ids)
        await c.execute("DELETE FROM route_stops WHERE route_id=$1", route_id)
        for pos, oid in enumerate(body.order_ids, start=1):
            await c.execute(
                "INSERT INTO route_stops (route_id, order_id, seq) VALUES ($1,$2,$3)",
                route_id, oid, pos)
        for rid in affected - {route_id}:
            rows = await c.fetch("SELECT id FROM route_stops WHERE route_id=$1 ORDER BY seq", rid)
            for pos, row in enumerate(rows, start=1):
                await c.execute("UPDATE route_stops SET seq=$1 WHERE id=$2", pos, row["id"])

    warnings = []
    for rid in affected:
        await _rebuild_route(rid)
        chk = await pool.fetchrow("""
            SELECT r.load_weight, r.load_volume, v.max_weight_kg, v.max_volume_m3, v.name
            FROM routes r JOIN vehicles v ON v.id=r.vehicle_id WHERE r.id=$1""", rid)
        if chk and (float(chk["load_weight"] or 0) > float(chk["max_weight_kg"])
                    or float(chk["load_volume"] or 0) > float(chk["max_volume_m3"])):
            warnings.append(f"{chk['name']}: перевантаження")
    return {"ok": True, "recalculated": sorted(affected), "warnings": warnings}


# зеркальный разворот маршрута: последняя точка становится первой
@app.post("/api/routes/{route_id}/reverse")
async def reverse_route(route_id: int):
    async with pool.acquire() as c:
        async with c.transaction():
            rows = await c.fetch(
                "SELECT id FROM route_stops WHERE route_id=$1 ORDER BY seq DESC", route_id)
            if len(rows) < 2:
                return {"ok": True, "note": "менше 2 точок — нема що розвертати"}
            # сдвигаем seq в свободный диапазон, чтобы не нарушить UNIQUE(route_id, seq)
            await c.execute(
                "UPDATE route_stops SET seq = seq + 100000 WHERE route_id=$1", route_id)
            for pos, row in enumerate(rows, start=1):
                await c.execute("UPDATE route_stops SET seq=$1 WHERE id=$2", pos, row["id"])
    await _rebuild_route(route_id)
    return {"ok": True}


# п.3: снять точку с маршрута в буфер
@app.delete("/api/routes/{route_id}/stops/{order_id}")
async def remove_stop(route_id: int, order_id: int):
    await pool.execute(
        "DELETE FROM route_stops WHERE route_id=$1 AND order_id=$2", route_id, order_id)
    rows = await pool.fetch("SELECT id FROM route_stops WHERE route_id=$1 ORDER BY seq", route_id)
    for pos, row in enumerate(rows, start=1):
        await pool.execute("UPDATE route_stops SET seq=$1 WHERE id=$2", pos, row["id"])
    await _rebuild_route(route_id)
    return {"ok": True}


@app.get("/api/routes")
async def get_routes(project_id: int = Query(...)):
    rr = await pool.fetch("""
        SELECT r.*, v.name vehicle_name, v.max_weight_kg, v.max_volume_m3, d.name driver_name,
               dep.name depot_name
        FROM routes r JOIN vehicles v ON v.id=r.vehicle_id
        LEFT JOIN drivers d ON d.id=r.driver_id
        JOIN depots dep ON dep.id=r.depot_id
        WHERE r.project_id=$1 ORDER BY r.id""", project_id)
    result = []
    for r in rr:
        ss = await pool.fetch("""
            SELECT s.seq, s.eta, s.etd, o.id order_id, o.client, o.kind, o.address, o.address_extra, o.seats,
                   o.lat, o.lon, o.tw_from, o.tw_to, o.break_from, o.break_to,
                   o.weight_kg, o.volume_m3, o.service_min
            FROM route_stops s JOIN orders o ON o.id=s.order_id
            WHERE s.route_id=$1 ORDER BY s.seq""", r["id"])
        stops_list = [dict(x) for x in ss]
        cums, start_w, start_v, peak_w, peak_v = running_loads(stops_list)
        for st_, (cw, cv) in zip(stops_list, cums):
            st_["cum_weight"], st_["cum_volume"] = cw, cv
        result.append({**dict(r), "stops": stops_list,
                       "start_weight": round(start_w, 1), "start_volume": round(start_v, 3),
                       "peak_weight": round(peak_w, 1), "peak_volume": round(peak_v, 3)})
    return result


app.include_router(integration_1c.router)
app.include_router(driver.router)
app.include_router(appupd.router)

app.mount("/", StaticFiles(directory="static", html=True), name="static")
