"""v17: мобильный кабинет водителя (фаза 1).

Токены доступа, выдача рейса на день, ручные факты «прибув/поїхав»,
приём GPS-точек, план/факт для логиста (страницы driver.html, tokens.html,
facts.html). Схема — migrate_011, применяется из init() при старте API.
"""
import os
import asyncio
import secrets
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from . import osrm

router = APIRouter(tags=["driver"])
pool = None
_track_cache = {}                       # route_id -> (GPS signature, готовий трек)
KYIV_TZ = ZoneInfo("Europe/Kyiv")


async def init(db_pool):
    """Создание таблиц (идемпотентно). Вызывается из startup."""
    global pool
    pool = db_pool
    # v59: трекінг використання кнопок (Подзвонити / Google Maps / Waze)
    await pool.execute("""
        CREATE TABLE IF NOT EXISTS ui_events (
            id        BIGSERIAL PRIMARY KEY,
            driver_id INT NOT NULL REFERENCES drivers(id) ON DELETE CASCADE,
            route_id  INT REFERENCES routes(id) ON DELETE SET NULL,
            order_id  BIGINT,
            event     TEXT NOT NULL CHECK (event IN ('call','nav_google','nav_waze')),
            ts        TIMESTAMPTZ NOT NULL DEFAULT now())""")
    await pool.execute(
        "CREATE INDEX IF NOT EXISTS idx_ui_events_event_ts ON ui_events(event, ts)")
    await pool.execute(
        "CREATE INDEX IF NOT EXISTS idx_ui_events_driver_ts ON ui_events(driver_id, ts)")
    await pool.execute("""
        CREATE TABLE IF NOT EXISTS driver_tokens (
            token      TEXT PRIMARY KEY,
            driver_id  INT NOT NULL REFERENCES drivers(id) ON DELETE CASCADE,
            is_active  BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
    await pool.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_driver_tokens_active
            ON driver_tokens(driver_id) WHERE is_active""")
    await pool.execute("""
        CREATE TABLE IF NOT EXISTS logist_tokens (
            id           SERIAL PRIMARY KEY,
            name         TEXT NOT NULL,
            token        TEXT NOT NULL UNIQUE,
            is_active    BOOLEAN NOT NULL DEFAULT TRUE,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            last_used_at TIMESTAMPTZ)""")
    await pool.execute("""
        CREATE TABLE IF NOT EXISTS stop_events (
            id       SERIAL PRIMARY KEY,
            route_id INT NOT NULL REFERENCES routes(id) ON DELETE CASCADE,
            order_id INT NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
            event    TEXT NOT NULL CHECK (event IN ('arrive','depart')),
            ts       TIMESTAMPTZ NOT NULL DEFAULT now(),
            lat      DOUBLE PRECISION,
            lon      DOUBLE PRECISION,
            source   TEXT NOT NULL DEFAULT 'manual',
            UNIQUE (route_id, order_id, event))""")
    await pool.execute(
        "CREATE INDEX IF NOT EXISTS idx_stop_events_route ON stop_events(route_id)")
    await pool.execute("""
        CREATE TABLE IF NOT EXISTS gps_points (
            id          BIGSERIAL PRIMARY KEY,
            driver_id   INT NOT NULL REFERENCES drivers(id) ON DELETE CASCADE,
            route_id    INT REFERENCES routes(id) ON DELETE SET NULL,
            ts          TIMESTAMPTZ NOT NULL,
            lat         DOUBLE PRECISION NOT NULL,
            lon         DOUBLE PRECISION NOT NULL,
            speed_kmh   REAL,
            accuracy_m  REAL,
            received_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
    await pool.execute(
        "CREATE INDEX IF NOT EXISTS idx_gps_driver_ts ON gps_points(driver_id, ts DESC)")
    # v24 (migrate_014): відмови — довідник причин + подія 'fail'
    await pool.execute("""
        CREATE TABLE IF NOT EXISTS fail_reasons (
            id        SERIAL PRIMARY KEY,
            kind      TEXT NOT NULL CHECK (kind IN ('delivery','pickup')),
            name      TEXT NOT NULL,
            sort      INT  NOT NULL DEFAULT 100,
            is_system BOOLEAN NOT NULL DEFAULT FALSE,
            is_active BOOLEAN NOT NULL DEFAULT TRUE)""")
    await pool.execute("""
        INSERT INTO fail_reasons (kind, name, sort, is_system)
        SELECT * FROM (VALUES
            ('delivery','Закрито',10,FALSE),
            ('delivery','Клієнт відмовився',20,FALSE),
            ('delivery','Нема товару',30,FALSE),
            ('delivery','Інше',999,TRUE),
            ('pickup','Закрито',10,FALSE),
            ('pickup','Не готовий товар',20,FALSE),
            ('pickup','Інше',999,TRUE)
        ) AS v(kind,name,sort,is_system)
        WHERE NOT EXISTS (SELECT 1 FROM fail_reasons)""")
    await pool.execute(
        "ALTER TABLE stop_events DROP CONSTRAINT IF EXISTS stop_events_event_check")
    await pool.execute("""
        ALTER TABLE stop_events ADD CONSTRAINT stop_events_event_check
            CHECK (event IN ('arrive','depart','fail'))""")
    await pool.execute(
        "ALTER TABLE stop_events ADD COLUMN IF NOT EXISTS reason_id INT REFERENCES fail_reasons(id)")
    await pool.execute(
        "ALTER TABLE stop_events ADD COLUMN IF NOT EXISTS reason_text TEXT")
    # v44 (migrate_021): відстань водія до цілі в момент натискання (аудит)
    await pool.execute(
        "ALTER TABLE stop_events ADD COLUMN IF NOT EXISTS dist_m INT")
    await pool.execute(
        "ALTER TABLE route_events ADD COLUMN IF NOT EXISTS dist_m INT")
    # v38 (migrate_017): «прибув на склад» перед виїздом
    await pool.execute(
        "ALTER TABLE route_events DROP CONSTRAINT IF EXISTS route_events_event_check")
    await pool.execute("""
        ALTER TABLE route_events ADD CONSTRAINT route_events_event_check
            CHECK (event IN ('depot_arrive','start','finish'))""")
    # v38 (migrate_017): повідомлення водіїв про помилки в даних точки.
    # Дані точки денормалізовано — повідомлення переживає реімпорт/видалення заявки.
    await pool.execute("""
        CREATE TABLE IF NOT EXISTS stop_issues (
            id          SERIAL PRIMARY KEY,
            route_id    INT REFERENCES routes(id) ON DELETE SET NULL,
            order_id    INT REFERENCES orders(id) ON DELETE SET NULL,
            driver_id   INT,
            driver_name TEXT,
            client      TEXT,
            address     TEXT,
            doc_number  TEXT,
            issue_type  TEXT NOT NULL CHECK (issue_type IN ('phone','contact','address')),
            note        TEXT,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            acked_at    TIMESTAMPTZ)""")
    await pool.execute("""
        CREATE INDEX IF NOT EXISTS idx_stop_issues_unacked
            ON stop_issues(created_at DESC) WHERE acked_at IS NULL""")


def kyiv_today() -> date:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Europe/Kyiv")).date()
    except Exception:                       # в slim-образе может не быть tzdata
        return (datetime.now(timezone.utc) + timedelta(hours=3)).date()


def _hm(t) -> str | None:
    return str(t)[:5] if t else None


def _iso(ts) -> str | None:
    return ts.isoformat() if ts else None


def _route_timing(plan_date, plan_depart, plan_return, stops, start_dt, finish_dt,
                  now=None):
    """Поточне відхилення з урахуванням уже простроченої наступної віхи."""
    candidates = []
    if start_dt and plan_depart:
        candidates.append((start_dt, plan_depart))
    for stop in stops:
        if stop["arrive_ts"] and stop["eta"]:
            candidates.append((stop["arrive_ts"], stop["eta"]))
        if stop["depart_ts"] and (stop["etd"] or stop["eta"]):
            candidates.append((stop["depart_ts"], stop["etd"] or stop["eta"]))
        if stop["fail_ts"] and stop["eta"]:
            candidates.append((stop["fail_ts"], stop["eta"]))
    if finish_dt and plan_return:
        candidates.append((finish_dt, plan_return))
    milestone_delay = None
    if candidates:
        actual, planned_time = max(candidates, key=lambda item: item[0])
        planned = datetime.combine(plan_date, planned_time, tzinfo=KYIV_TZ)
        milestone_delay = round(
            (actual.astimezone(KYIV_TZ) - planned).total_seconds() / 60)

    # Остання виконана точка могла бути ранньою, але після неї водій міг
    # зупинитися, а наступна ETA/ETD уже минула. Для поточного дня це і є
    # операційне запізнення, яке повинно рости разом із часом.
    delay_min = milestone_delay
    now = now or datetime.now(KYIV_TZ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=KYIV_TZ)
    else:
        now = now.astimezone(KYIV_TZ)
    if not finish_dt and plan_date == now.date():
        overdue = []
        for stop in stops:
            if stop["fail_ts"] or stop["depart_ts"]:
                continue
            planned_time = stop["etd"] if stop["arrive_ts"] else stop["eta"]
            if not planned_time:
                continue
            planned = datetime.combine(plan_date, planned_time, tzinfo=KYIV_TZ)
            overdue.append(round((now - planned).total_seconds() / 60))
        if not overdue and plan_return:
            planned = datetime.combine(plan_date, plan_return, tzinfo=KYIV_TZ)
            overdue.append(round((now - planned).total_seconds() / 60))
        positive_overdue = [minutes for minutes in overdue if minutes > 0]
        if positive_overdue:
            delay_min = max(milestone_delay or 0, max(positive_overdue))
    forecast = None
    if plan_return and delay_min is not None:
        forecast_dt = datetime.combine(plan_date, plan_return) + timedelta(minutes=delay_min)
        forecast = forecast_dt.strftime("%H:%M")
    return delay_min, _hm(plan_return), forecast


async def _driver_by_token(token: str):
    row = await pool.fetchrow("""
        SELECT d.id, d.name, d.code_1c FROM driver_tokens t
        JOIN drivers d ON d.id = t.driver_id
        WHERE t.token = $1 AND t.is_active AND d.is_active""", token)
    if not row:
        raise HTTPException(401, "Недійсний токен")
    return row


async def _logist_by_token(token: str):
    row = await pool.fetchrow("""
        UPDATE logist_tokens SET last_used_at=now()
        WHERE token=$1 AND is_active
        RETURNING id, name""", token)
    if not row:
        raise HTTPException(401, "Недійсний токен логіста")
    return row


class LogistAccessIn(BaseModel):
    name: str


@router.get("/api/logist-accesses")
async def get_logist_accesses():
    rows = await pool.fetch("""
        SELECT id, name, token, created_at, last_used_at
        FROM logist_tokens WHERE is_active ORDER BY created_at, id""")
    return [{**dict(row), "created_at": _iso(row["created_at"]),
             "last_used_at": _iso(row["last_used_at"])} for row in rows]


@router.post("/api/logist-accesses")
async def create_logist_access(body: LogistAccessIn):
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "Вкажіть назву доступу")
    token = secrets.token_urlsafe(24)
    row = await pool.fetchrow("""
        INSERT INTO logist_tokens (name, token) VALUES ($1,$2)
        RETURNING id, name, token, created_at, last_used_at""", name, token)
    return {**dict(row), "created_at": _iso(row["created_at"]), "last_used_at": None,
            "url": f"/logist.html?token={token}"}


@router.patch("/api/logist-accesses/{access_id}")
async def rename_logist_access(access_id: int, body: LogistAccessIn):
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "Вкажіть назву доступу")
    row = await pool.fetchrow("""
        UPDATE logist_tokens SET name=$2 WHERE id=$1 AND is_active
        RETURNING id, name""", access_id, name)
    if not row:
        raise HTTPException(404, "Доступ не знайдено")
    return dict(row)


@router.post("/api/logist-accesses/{access_id}/rotate")
async def rotate_logist_access(access_id: int):
    token = secrets.token_urlsafe(24)
    row = await pool.fetchrow("""
        UPDATE logist_tokens SET token=$2, created_at=now(), last_used_at=NULL
        WHERE id=$1 AND is_active RETURNING id, name, token, created_at""",
        access_id, token)
    if not row:
        raise HTTPException(404, "Доступ не знайдено")
    return {**dict(row), "created_at": _iso(row["created_at"]),
            "url": f"/logist.html?token={token}"}


@router.delete("/api/logist-accesses/{access_id}")
async def delete_logist_access(access_id: int):
    result = await pool.execute(
        "UPDATE logist_tokens SET is_active=FALSE WHERE id=$1 AND is_active", access_id)
    if result.endswith(" 0"):
        raise HTTPException(404, "Доступ не знайдено")
    return {"ok": True}


# ---------- токены (для логиста) ----------

@router.get("/api/drivers/{driver_id}/token")
async def get_token(driver_id: int):
    row = await pool.fetchrow(
        "SELECT token, created_at FROM driver_tokens WHERE driver_id=$1 AND is_active",
        driver_id)
    return {"token": row["token"] if row else None,
            "created_at": _iso(row["created_at"]) if row else None}


@router.post("/api/drivers/{driver_id}/token")
async def new_token(driver_id: int):
    drv = await pool.fetchrow("SELECT id FROM drivers WHERE id=$1 AND is_active", driver_id)
    if not drv:
        raise HTTPException(404, "Водія не знайдено")
    token = secrets.token_urlsafe(9)
    async with pool.acquire() as c:
        await c.execute("UPDATE driver_tokens SET is_active=FALSE WHERE driver_id=$1", driver_id)
        await c.execute("INSERT INTO driver_tokens (token, driver_id) VALUES ($1,$2)",
                        token, driver_id)
    return {"token": token, "url": f"/driver.html?token={token}"}


# ---------- рейс водителя ----------

@router.get("/api/driver/{token}/trip")
async def driver_trip(token: str, d: date | None = Query(None),
                      route_id: int | None = Query(None)):
    drv = await _driver_by_token(token)
    day = d or kyiv_today()
    # v39: у водія може бути кілька рейсів на день
    rr = await pool.fetch("""
        SELECT r.id, r.plan_date, r.color, r.total_km, r.depart_time,
               COALESCE(r.return_time_manual, r.return_time) AS return_time,   -- v51
               COALESCE(r.start_kind, 'depot')  AS start_kind,
               r.start_address,
               COALESCE(r.finish_kind, 'depot') AS finish_kind,
               r.finish_address,
               v.name AS vehicle_name, v.plate,
               (SELECT count(*) FROM route_stops s WHERE s.route_id = r.id) AS n_stops,
               (SELECT ts FROM route_events e WHERE e.route_id=r.id AND e.event='start')  AS start_ts,
               (SELECT ts FROM route_events e WHERE e.route_id=r.id AND e.event='finish') AS finish_ts
        FROM routes r JOIN vehicles v ON v.id = r.vehicle_id
        WHERE r.plan_date = $2
          AND r.project_id IN (SELECT id FROM projects WHERE is_released)
          AND (r.driver_id = $1
               OR r.vehicle_id IN (SELECT id FROM vehicles WHERE driver_id = $1 AND is_active))
        ORDER BY r.depart_time NULLS LAST, r.id""", drv["id"], day)
    if not rr:
        # рейси є, але проект не активовано?
        pending = await pool.fetchval("""
            SELECT 1 FROM routes r
            WHERE r.plan_date = $2
              AND (r.driver_id = $1
                   OR r.vehicle_id IN (SELECT id FROM vehicles WHERE driver_id = $1 AND is_active))
            LIMIT 1""", drv["id"], day)
        return {"driver": drv["name"], "date": day.isoformat(),
                "route": None, "routes": [], "stops": [], "not_released": bool(pending)}

    # обраний рейс: явний route_id -> перший незавершений -> останній
    route = None
    if route_id:
        route = next((r for r in rr if r["id"] == route_id), None)
    if route is None:
        route = next((r for r in rr if not r["finish_ts"]), rr[-1])

    stops = await pool.fetch("""
        SELECT s.seq, s.eta, s.etd, o.id AS order_id, o.client, o.kind, o.address,
               o.address_extra, o.lat, o.lon, o.tw_from, o.tw_to, o.break_from, o.break_to,
               o.weight_kg, o.volume_m3, o.doc_number, o.phone, o.seats, o.contact_person,
               ea.ts AS arrive_ts, ed.ts AS depart_ts,
               ef.ts AS fail_ts, COALESCE(ef.reason_text, fr.name) AS fail_reason
        FROM route_stops s
        JOIN orders o ON o.id = s.order_id
        LEFT JOIN stop_events ea ON ea.route_id=s.route_id AND ea.order_id=o.id AND ea.event='arrive'
        LEFT JOIN stop_events ed ON ed.route_id=s.route_id AND ed.order_id=o.id AND ed.event='depart'
        LEFT JOIN stop_events ef ON ef.route_id=s.route_id AND ef.order_id=o.id AND ef.event='fail'
        LEFT JOIN fail_reasons fr ON fr.id = ef.reason_id
        WHERE s.route_id = $1 ORDER BY s.seq""", route["id"])

    reasons = await pool.fetch(
        "SELECT id, kind, name FROM fail_reasons WHERE is_active ORDER BY kind, sort, id")
    fail_reasons = {"delivery": [], "pickup": []}
    for r in reasons:
        fail_reasons[r["kind"]].append({"id": r["id"], "name": r["name"]})

    return {
        "driver": drv["name"], "date": day.isoformat(),
        "fail_reasons": fail_reasons,
        # v39: список усіх рейсів дня для перемикача
        "routes": [{"id": r["id"], "depart": _hm(r["depart_time"]),
                    "return": _hm(r["return_time"]), "stops": r["n_stops"],
                    "started": bool(r["start_ts"]), "finished": bool(r["finish_ts"]),
                    # v55/v57: GPS-пробіг для вечірнього передзаповнення
                    "gps_km": (await _route_worklog(r["id"], [drv["id"]]))[3]}
                   for r in rr],
        "route": {"id": route["id"], "vehicle": route["vehicle_name"], "plate": route["plate"],
                  "color": route["color"], "total_km": float(route["total_km"] or 0),
                  "depart": _hm(route["depart_time"]), "return": _hm(route["return_time"]),
                  "start_kind": route["start_kind"],                  # v51
                  "start_address": route["start_address"],
                  "finish_kind": route["finish_kind"],
                  "finish_address": route["finish_address"],
                  **(lambda da: {"depot_arrive_ts": _iso(da["ts"]) if da else None,   # v38/v55
                                 "depot_arrive_auto": bool(da and da["source"] != "driver")})(
                      await pool.fetchrow(
                          "SELECT ts, COALESCE(source,'driver') AS source FROM route_events "
                          "WHERE route_id=$1 AND event='depot_arrive'", route["id"])),
                  **dict(zip(("start_ts", "finish_ts", "work_min", "gps_km"),
                             await _route_worklog(route["id"], [drv["id"]])))},
        "stops": [{
            "seq": s["seq"], "order_id": s["order_id"], "doc_number": s["doc_number"],
            "client": s["client"], "kind": s["kind"],
            "address": s["address"], "address_extra": s["address_extra"],
            "lat": s["lat"], "lon": s["lon"], "phone": s["phone"], "seats": s["seats"],
            "contact_person": s["contact_person"],
            "tw_from": _hm(s["tw_from"]), "tw_to": _hm(s["tw_to"]),
            "break_from": _hm(s["break_from"]), "break_to": _hm(s["break_to"]),   # v39
            "eta": _hm(s["eta"]), "etd": _hm(s["etd"]),
            "weight_kg": float(s["weight_kg"] or 0), "volume_m3": float(s["volume_m3"] or 0),
            "arrive_ts": _iso(s["arrive_ts"]), "depart_ts": _iso(s["depart_ts"]),
            "fail_ts": _iso(s["fail_ts"]), "fail_reason": s["fail_reason"],
        } for s in stops],
    }


GEO_CONFIRM_M = 500      # v44: поріг м'якого підтвердження натискань здалеку


async def _press_distance_m(driver_id: int, tgt_lat, tgt_lon,
                            body_lat=None, body_lon=None):
    """Відстань водія до цілі в момент натискання, м.

    Джерело позиції: найсвіжіша GPS-точка водія (≤3 хв, точність ≤100 м) —
    в APK web-геолокація вимкнена і координати в тілі запиту порожні;
    fallback — координати з запиту (web-режим). Немає ні того, ні того — None.
    """
    if tgt_lat is None or tgt_lon is None:
        return None
    fix = await pool.fetchrow("""
        SELECT lat, lon FROM gps_points
        WHERE driver_id = $1 AND ts > now() - interval '3 minutes'
          AND (accuracy_m IS NULL OR accuracy_m <= 100)
        ORDER BY ts DESC LIMIT 1""", driver_id)
    lat = fix["lat"] if fix else body_lat
    lon = fix["lon"] if fix else body_lon
    if lat is None or lon is None:
        return None
    return int(round(_haversine_km(lat, lon, tgt_lat, tgt_lon) * 1000))


# ---------- факты «прибув / поїхав» ----------

class StopEvent(BaseModel):
    event: str                 # arrive | depart
    lat: float | None = None
    lon: float | None = None
    force: bool = False        # v44: водій підтвердив натискання здалеку


async def _require_route_started(route_id: int):
    """Не дозволяти факти точки до фактичного виїзду зі складу."""
    started = await pool.fetchval(
        "SELECT 1 FROM route_events WHERE route_id=$1 AND event='start'", route_id)
    if not started:
        raise HTTPException(409, "Спочатку натисни «Виїхав на маршрут»")


@router.post("/api/driver/{token}/stops/{order_id}/event")
async def stop_event(token: str, order_id: int, body: StopEvent):
    if body.event not in ("arrive", "depart"):
        raise HTTPException(400, "event: arrive | depart")
    drv = await _driver_by_token(token)
    rs = await pool.fetchrow("""
        SELECT s.route_id FROM route_stops s
        JOIN routes r ON r.id = s.route_id
        WHERE s.order_id = $1
          AND (r.driver_id = $2
               OR r.vehicle_id IN (SELECT id FROM vehicles WHERE driver_id = $2 AND is_active))
        ORDER BY r.plan_date DESC, r.id DESC LIMIT 1""", order_id, drv["id"])
    if not rs:
        raise HTTPException(404, "Точка не на твоєму маршруті")
    await _require_route_started(rs["route_id"])
    tgt = await pool.fetchrow("SELECT lat, lon FROM orders WHERE id=$1", order_id)  # v44
    dist = await _press_distance_m(drv["id"], tgt["lat"] if tgt else None,
                                   tgt["lon"] if tgt else None, body.lat, body.lon)
    if dist is not None and dist > GEO_CONFIRM_M and not body.force:
        return {"ok": False, "confirm_required": True, "dist_m": dist}
    # первый зафиксированный факт — истина; повторная отправка идемпотентна
    row = await pool.fetchrow("""
        INSERT INTO stop_events (route_id, order_id, event, lat, lon, dist_m)
        VALUES ($1,$2,$3,$4,$5,$6)
        ON CONFLICT (route_id, order_id, event) DO NOTHING
        RETURNING ts""", rs["route_id"], order_id, body.event, body.lat, body.lon, dist)
    if row is None:
        row = await pool.fetchrow(
            "SELECT ts FROM stop_events WHERE route_id=$1 AND order_id=$2 AND event=$3",
            rs["route_id"], order_id, body.event)
    return {"ok": True, "ts": _iso(row["ts"])}


# ---------- відмова «не виконано» ----------

class FailIn(BaseModel):
    reason_id: int | None = None
    reason_text: str | None = None      # обовʼязково для «Інше»
    lat: float | None = None
    lon: float | None = None


async def _stop_route(order_id: int, driver_id: int):
    return await pool.fetchrow("""
        SELECT s.route_id, r.plan_date FROM route_stops s
        JOIN routes r ON r.id = s.route_id
        WHERE s.order_id = $1
          AND (r.driver_id = $2
               OR r.vehicle_id IN (SELECT id FROM vehicles WHERE driver_id = $2 AND is_active))
        ORDER BY r.plan_date DESC, r.id DESC LIMIT 1""", order_id, driver_id)


@router.post("/api/driver/{token}/stops/{order_id}/fail")
async def stop_fail(token: str, order_id: int, body: FailIn):
    drv = await _driver_by_token(token)
    rs = await _stop_route(order_id, drv["id"])
    if not rs:
        raise HTTPException(404, "Точка не на твоєму маршруті")
    await _require_route_started(rs["route_id"])
    reason_name = None
    if body.reason_id:
        reason_name = await pool.fetchval(
            "SELECT name FROM fail_reasons WHERE id=$1 AND is_active", body.reason_id)
        if not reason_name:
            raise HTTPException(404, "Причину не знайдено")
    txt = (body.reason_text or "").strip() or None
    if not body.reason_id and not txt:
        raise HTTPException(400, "Вкажи причину")
    tgt = await pool.fetchrow("SELECT lat, lon FROM orders WHERE id=$1", order_id)  # v44
    dist = await _press_distance_m(drv["id"], tgt["lat"] if tgt else None,
                                   tgt["lon"] if tgt else None, body.lat, body.lon)
    row = await pool.fetchrow("""
        INSERT INTO stop_events (route_id, order_id, event, lat, lon, reason_id, reason_text, dist_m)
        VALUES ($1,$2,'fail',$3,$4,$5,$6,$7)
        ON CONFLICT (route_id, order_id, event) DO NOTHING
        RETURNING ts""", rs["route_id"], order_id, body.lat, body.lon, body.reason_id, txt, dist)
    if row is None:
        row = await pool.fetchrow(
            "SELECT ts FROM stop_events WHERE route_id=$1 AND order_id=$2 AND event='fail'",
            rs["route_id"], order_id)
    return {"ok": True, "ts": _iso(row["ts"])}


@router.post("/api/driver/{token}/stops/{order_id}/unfail")
async def stop_unfail(token: str, order_id: int):
    drv = await _driver_by_token(token)
    rs = await _stop_route(order_id, drv["id"])
    if not rs:
        raise HTTPException(404, "Точка не на твоєму маршруті")
    if rs["plan_date"] != kyiv_today():
        raise HTTPException(403, "Відмінити можна лише в день рейсу")
    await pool.execute(
        "DELETE FROM stop_events WHERE route_id=$1 AND order_id=$2 AND event='fail'",
        rs["route_id"], order_id)
    return {"ok": True}


# ---------- v38: повідомлення про помилки в даних точки ----------

ISSUE_TYPES = {"phone": "Невірний телефон",
               "contact": "Невірна контактна особа",
               "address": "Невірна адреса"}


class IssueIn(BaseModel):
    issue_type: str            # phone | contact | address
    note: str | None = None    # правильне значення, якщо водій знає


@router.post("/api/driver/{token}/stops/{order_id}/issue")
async def stop_issue(token: str, order_id: int, body: IssueIn):
    if body.issue_type not in ISSUE_TYPES:
        raise HTTPException(400, "issue_type: phone|contact|address")
    drv = await _driver_by_token(token)
    rs = await _stop_route(order_id, drv["id"])
    if not rs:
        raise HTTPException(404, "Точка не на твоєму маршруті")
    o = await pool.fetchrow(
        "SELECT client, address, doc_number FROM orders WHERE id=$1", order_id)
    iid = await pool.fetchval("""
        INSERT INTO stop_issues (route_id, order_id, driver_id, driver_name,
                                 client, address, doc_number, issue_type, note)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9) RETURNING id""",
        rs["route_id"], order_id, drv["id"], drv["name"],
        o["client"] if o else None, o["address"] if o else None,
        o["doc_number"] if o else None,
        body.issue_type, (body.note or "").strip() or None)
    return {"ok": True, "id": iid}


@router.get("/api/issues")
async def issues_list(all: bool = Query(False)):
    """Повідомлення водіїв для логіста; за замовчуванням — лише неприйняті."""
    rows = await pool.fetch(f"""
        SELECT id, driver_name, client, address, doc_number, issue_type, note,
               created_at, acked_at
        FROM stop_issues
        {'' if all else 'WHERE acked_at IS NULL'}
        ORDER BY created_at DESC LIMIT 100""")
    return [{**dict(r), "type_name": ISSUE_TYPES.get(r["issue_type"], r["issue_type"]),
             "created_at": _iso(r["created_at"]), "acked_at": _iso(r["acked_at"])}
            for r in rows]


@router.post("/api/issues/{issue_id}/ack")
async def issue_ack(issue_id: int):
    n = await pool.execute(
        "UPDATE stop_issues SET acked_at=now() WHERE id=$1 AND acked_at IS NULL", issue_id)
    return {"ok": n.endswith("1")}


# ---------- довідник причин відмов (налаштування) ----------

@router.get("/api/fail-reasons")
async def fail_reasons_list():
    rows = await pool.fetch("""
        SELECT id, kind, name, sort, is_system, is_active
        FROM fail_reasons ORDER BY kind, sort, id""")
    return [dict(r) for r in rows]


class ReasonIn(BaseModel):
    kind: str
    name: str


@router.post("/api/fail-reasons")
async def fail_reason_add(r: ReasonIn):
    if r.kind not in ("delivery", "pickup"):
        raise HTTPException(400, "kind: delivery | pickup")
    name = r.name.strip()
    if not name:
        raise HTTPException(400, "Вкажи назву")
    ex = await pool.fetchval("""
        SELECT id FROM fail_reasons
        WHERE kind=$1 AND is_active AND upper(trim(name))=upper($2)""", r.kind, name)
    if ex:
        raise HTTPException(409, "Така причина вже є")
    # нові причини — перед системним «Інше» (sort 999)
    nxt = await pool.fetchval(
        "SELECT COALESCE(MAX(sort),0)+10 FROM fail_reasons WHERE kind=$1 AND NOT is_system", r.kind)
    rid = await pool.fetchval(
        "INSERT INTO fail_reasons (kind, name, sort) VALUES ($1,$2,$3) RETURNING id",
        r.kind, name, nxt)
    return {"id": rid}


class ReasonPatch(BaseModel):
    name: str | None = None
    is_active: bool | None = None


@router.patch("/api/fail-reasons/{reason_id}")
async def fail_reason_patch(reason_id: int, r: ReasonPatch):
    cur = await pool.fetchrow("SELECT is_system FROM fail_reasons WHERE id=$1", reason_id)
    if not cur:
        raise HTTPException(404, "Причину не знайдено")
    if r.is_active is not None:
        if cur["is_system"] and not r.is_active:
            raise HTTPException(403, "Системну причину не можна архівувати")
        await pool.execute("UPDATE fail_reasons SET is_active=$1 WHERE id=$2",
                           r.is_active, reason_id)
    if r.name is not None and r.name.strip():
        await pool.execute("UPDATE fail_reasons SET name=$1 WHERE id=$2",
                           r.name.strip(), reason_id)
    return {"ok": True}


# ---------- GPS ----------

class GpsPoint(BaseModel):
    ts: float                  # epoch ms
    lat: float
    lon: float
    speed_kmh: float | None = None
    accuracy_m: float | None = None


class GpsBatch(BaseModel):
    points: list[GpsPoint]


@router.post("/api/driver/{token}/position")
async def position(token: str, body: GpsBatch, request: Request):
    drv = await _driver_by_token(token)
    # v52: звичайний браузер з посиланням водія — лише режим перегляду.
    # Старі відкриті вкладки продовжують викликати /position до перезавантаження,
    # тому захист дублюємо на backend. Нативний LocationService Android має
    # User-Agent Dalvik/Android, а WebView/desktop/mobile browser — Mozilla.
    if "mozilla/" in request.headers.get("user-agent", "").lower():
        return {"saved": 0, "ignored": "browser_read_only"}
    if not body.points:
        return {"saved": 0}
    route_id = await pool.fetchval("""
        SELECT r.id
        FROM routes r
        LEFT JOIN route_events rs ON rs.route_id=r.id AND rs.event='start'
        LEFT JOIN route_events rf ON rf.route_id=r.id AND rf.event='finish'
        WHERE r.plan_date=$2
          AND r.project_id IN (SELECT id FROM projects WHERE is_released)
          AND (r.driver_id=$1
               OR r.vehicle_id IN (SELECT id FROM vehicles WHERE driver_id=$1 AND is_active))
        -- v52: при кількох рейсах спочатку активний (старт є, фінішу немає),
        -- потім останній розпочатий; id більше не визначає поточний рейс.
        ORDER BY (rs.ts IS NOT NULL AND rf.ts IS NULL) DESC,
                 rs.ts DESC NULLS LAST, r.depart_time NULLS LAST, r.id
        LIMIT 1""", drv["id"], kyiv_today())
    rows = [(drv["id"], route_id,
             datetime.fromtimestamp(p.ts / 1000, tz=timezone.utc),
             p.lat, p.lon, p.speed_kmh, p.accuracy_m) for p in body.points[:500]]
    await pool.executemany("""
        INSERT INTO gps_points (driver_id, route_id, ts, lat, lon, speed_kmh, accuracy_m)
        VALUES ($1,$2,$3,$4,$5,$6,$7)""", rows)
    return {"saved": len(rows)}


# ---------- план/факт для логиста ----------

@router.get("/api/facts")
async def facts(plan_date: date = Query(...)):
    routes = await pool.fetch("""
        SELECT r.id, r.color, r.plan_date, r.project_id,
               r.total_km, r.depart_time,
               COALESCE(r.return_time_manual, r.return_time) AS return_time,   -- v51
               COALESCE(r.start_kind,'depot')  AS start_kind,  r.start_address,   -- v59
               COALESCE(r.finish_kind,'depot') AS finish_kind, r.finish_address,
               v.name AS vehicle_name, v.driver_id AS veh_driver_id,
               d.id AS driver_id, d.name AS driver_name
        FROM routes r JOIN vehicles v ON v.id=r.vehicle_id
        LEFT JOIN drivers d ON d.id=COALESCE(r.driver_id, v.driver_id)
        WHERE r.plan_date = $1
          AND r.project_id IN (SELECT id FROM projects WHERE is_released)
        ORDER BY r.id""", plan_date)
    route_ids = {
        r["id"]: [i for i in {r["driver_id"], r["veh_driver_id"]} if i]
        for r in routes
    }
    matched_tracks = await asyncio.gather(*(
        _route_actual_track(r["id"], route_ids[r["id"]]) for r in routes))
    tracks_by_route = {r["id"]: track for r, track in zip(routes, matched_tracks)}
    out = []
    for r in routes:
        stops = await pool.fetch("""
            SELECT s.seq, s.eta, s.etd, o.id AS order_id, o.client, o.address, o.lat, o.lon,
                   o.kind, o.doc_number, o.address_extra, o.contact_person, o.phone,   -- v51
                   o.tw_from, o.tw_to, o.break_from, o.break_to,
                   o.weight_kg, o.volume_m3, o.seats, o.service_min,
                   ea.ts AS arrive_ts, ed.ts AS depart_ts,
                   ef.ts AS fail_ts, COALESCE(ef.reason_text, fr.name) AS fail_reason
            FROM route_stops s JOIN orders o ON o.id=s.order_id
            LEFT JOIN stop_events ea ON ea.route_id=s.route_id AND ea.order_id=o.id AND ea.event='arrive'
            LEFT JOIN stop_events ed ON ed.route_id=s.route_id AND ed.order_id=o.id AND ed.event='depart'
            LEFT JOIN stop_events ef ON ef.route_id=s.route_id AND ef.order_id=o.id AND ef.event='fail'
            LEFT JOIN fail_reasons fr ON fr.id = ef.reason_id
            WHERE s.route_id=$1 ORDER BY s.seq""", r["id"])
        last = await pool.fetchrow("""
            SELECT ts, lat, lon, speed_kmh FROM gps_points
            WHERE driver_id = ANY($1::int[]) ORDER BY ts DESC LIMIT 1""",
            [i for i in {r["driver_id"], r["veh_driver_id"]} if i]) \
            if (r["driver_id"] or r["veh_driver_id"]) else None
        driver_ids = route_ids[r["id"]]
        w_start, w_fin, w_min, raw_km = await _route_worklog(r["id"], driver_ids)
        track = tracks_by_route[r["id"]]
        w_km = track["km"] if track else raw_km
        stop_times = [ts for s in stops for ts in
                      (s["arrive_ts"], s["depart_ts"], s["fail_ts"]) if ts]
        start_dt = datetime.fromisoformat(w_start) if w_start else None
        finish_dt = datetime.fromisoformat(w_fin) if w_fin else None
        delay_min, plan_finish, forecast_finish = _route_timing(
            r["plan_date"], r["depart_time"], r["return_time"],
            stops, start_dt, finish_dt)
        bad_event_order = bool(
            (start_dt and finish_dt and start_dt >= finish_dt)
            or (start_dt and stop_times and min(stop_times) < start_dt)
            or (finish_dt and stop_times and max(stop_times) > finish_dt))
        if not w_fin:
            gps_issue = "route_open"
        elif bad_event_order:
            gps_issue = "event_order"
        elif not track:
            gps_issue = "insufficient_gps"
        elif not track.get("boundary_ok"):
            gps_issue = "depot_boundary"
        elif track["source"] != "osrm":
            gps_issue = "partial_match"
        else:
            gps_issue = None
        out.append({
            "route_id": r["id"], "color": r["color"],
            "vehicle": r["vehicle_name"], "driver": r["driver_name"],
            "start_ts": w_start, "finish_ts": w_fin, "work_min": w_min, "gps_km": w_km,
            "gps_source": track["source"] if track else "gps_raw",
            "gps_coverage": track.get("coverage", 0) if track else 0,
            "gps_boundary_ok": track.get("boundary_ok", False) if track else False,
            "gps_complete": gps_issue is None, "gps_issue": gps_issue,
            "plan_km": float(r["total_km"] or 0),
            "start_kind": r["start_kind"], "start_address": r["start_address"],     # v59
            "finish_kind": r["finish_kind"], "finish_address": r["finish_address"],
            "plan_depart": _hm(r["depart_time"]), "plan_return": _hm(r["return_time"]),
            "delay_min": delay_min, "plan_finish": plan_finish,
            "forecast_finish": forecast_finish,
            "last_gps": ({"ts": _iso(last["ts"]), "lat": last["lat"], "lon": last["lon"],
                          "speed_kmh": last["speed_kmh"]} if last else None),
            "stops": [{
                "seq": s["seq"], "order_id": s["order_id"], "client": s["client"],
                "address": s["address"], "lat": s["lat"], "lon": s["lon"],
                "kind": s["kind"], "doc_number": s["doc_number"],               # v51
                "address_extra": s["address_extra"],
                "contact_person": s["contact_person"], "phone": s["phone"],
                "tw_from": _hm(s["tw_from"]), "tw_to": _hm(s["tw_to"]),
                "break_from": _hm(s["break_from"]), "break_to": _hm(s["break_to"]),
                "weight_kg": float(s["weight_kg"] or 0),
                "volume_m3": float(s["volume_m3"] or 0),
                "seats": s["seats"], "service_min": s["service_min"],
                "eta": _hm(s["eta"]), "etd": _hm(s["etd"]),
                "arrive_ts": _iso(s["arrive_ts"]), "depart_ts": _iso(s["depart_ts"]),
                "fail_ts": _iso(s["fail_ts"]), "fail_reason": s["fail_reason"],
            } for s in stops],
        })
    return out


# ---------- v28: трек на План/Факт ----------

@router.get("/api/facts/tracks")
async def facts_tracks(plan_date: date = Query(...)):
    """Очищені GPS-треки конкретних рейсів + фактичний пробіг."""
    routes = await pool.fetch("""
        SELECT r.id, r.color, r.driver_id, v.driver_id AS veh_driver_id
        FROM routes r JOIN vehicles v ON v.id=r.vehicle_id
        WHERE r.plan_date = $1
          AND r.project_id IN (SELECT id FROM projects WHERE is_released)""", plan_date)
    matched_tracks = await asyncio.gather(*(
        _route_actual_track(r["id"], [i for i in {r["driver_id"], r["veh_driver_id"]} if i])
        for r in routes))
    out = []
    for r, track in zip(routes, matched_tracks):
        out.append({"route_id": r["id"], "color": r["color"],
                    "matched": bool(track and track["source"] == "osrm"),
                    "gps_km": track["km"] if track else None,
                    "source": track["source"] if track else None,
                    "coverage": track.get("coverage", 0) if track else 0,
                    "boundary_ok": track.get("boundary_ok", False) if track else False,
                    "segments": track.get("segments", []) if track else [],
                    "points": track["points"] if track else []})
    return out


@router.get("/api/logist/{token}/dashboard")
async def logist_dashboard(token: str, plan_date: date | None = Query(None)):
    """Мобільний кабінет логіста: факти, GPS-маркери та очищені треки."""
    access = await _logist_by_token(token)
    day = plan_date or kyiv_today()
    # facts() наповнює кеш matched-треків; другий виклик повторно використовує його,
    # а не запускає паралельний дубль OSRM map-matching для кожного рейсу.
    routes = await facts(day)
    track_rows = await facts_tracks(day)
    tracks = {row["route_id"]: row for row in track_rows}
    for route in routes:
        track = tracks.get(route["route_id"])
        route["track"] = track or {
            "points": [], "segments": [], "source": None, "gps_km": None}
    return {"access_name": access["name"], "date": day.isoformat(), "routes": routes}


# ---------- v28: рейс на наступний день ----------

@router.get("/api/driver/{token}/next-trip")
async def driver_next_trip(token: str):
    """Рейси найближчого майбутнього дня в активованому проекті (перегляд + пуш).

    v40: днів з кількома рейсами теж стосується — віддаємо ВСІ рейси дати,
    "trip" лишається першим для сумісності."""
    drv = await _driver_by_token(token)
    nd = await pool.fetchval("""
        SELECT min(r.plan_date) FROM routes r
        WHERE r.plan_date > $2
          AND r.project_id IN (SELECT id FROM projects WHERE is_released)
          AND (r.driver_id = $1
               OR r.vehicle_id IN (SELECT id FROM vehicles WHERE driver_id = $1 AND is_active))""",
        drv["id"], kyiv_today())
    if not nd:
        return {"trip": None, "trips": []}
    rr = await pool.fetch("""
        SELECT r.id, r.plan_date, r.depart_time, v.name AS vehicle_name, v.plate,
               COALESCE(r.start_lat, d.lat)   AS depot_lat,          -- v51: точка старту
               COALESCE(r.start_lon, d.lon)   AS depot_lon,
               COALESCE(r.finish_lat, d.lat)  AS fin_lat,
               COALESCE(r.finish_lon, d.lon)  AS fin_lon,
               COALESCE(r.start_kind, 'depot')  AS start_kind,
               COALESCE(r.finish_kind, 'depot') AS finish_kind,
               r.start_address, r.finish_address, d.name AS depot_name
        FROM routes r JOIN vehicles v ON v.id = r.vehicle_id
        JOIN depots d ON d.id = r.depot_id
        WHERE r.plan_date = $2
          AND r.project_id IN (SELECT id FROM projects WHERE is_released)
          AND (r.driver_id = $1
               OR r.vehicle_id IN (SELECT id FROM vehicles WHERE driver_id = $1 AND is_active))
        ORDER BY r.depart_time NULLS LAST, r.id""", drv["id"], nd)
    trips = []
    for route in rr:
        stops = await pool.fetch("""
            SELECT s.seq, s.eta, o.client, o.kind, o.address, o.address_extra, o.weight_kg,
                   o.lat, o.lon
            FROM route_stops s JOIN orders o ON o.id = s.order_id
            WHERE s.route_id = $1 ORDER BY s.seq""", route["id"])
        start_pt = (route["depot_lat"], route["depot_lon"])   # v51: старт (може бути не склад)
        fin_pt = (route["fin_lat"], route["fin_lon"])
        geometry = await osrm.route_latlon(                  # v33: плечі старт→1 і остання→фініш
            [start_pt] + [(s["lat"], s["lon"]) for s in stops if s["lat"] is not None] + [fin_pt])

        def _pt_name(kind, addr):
            if kind == "depot":
                return route["depot_name"]
            if kind == "home":
                return f"Дім · {addr}" if addr else "Дім водія"
            return addr or "Інша адреса"
        trips.append({
            "date": route["plan_date"].isoformat(), "route_id": route["id"],
            "vehicle": route["vehicle_name"], "plate": route["plate"],
            "depart": _hm(route["depart_time"]),
            "depot": {"lat": route["depot_lat"], "lon": route["depot_lon"],
                      "kind": route["start_kind"],
                      "name": _pt_name(route["start_kind"], route["start_address"])},
            "finish": {"lat": route["fin_lat"], "lon": route["fin_lon"],   # v51
                       "kind": route["finish_kind"],
                       "name": _pt_name(route["finish_kind"], route["finish_address"])},
            "geometry": geometry,
            "stops": [{"seq": s["seq"], "client": s["client"], "kind": s["kind"],
                       "address": s["address"], "address_extra": s["address_extra"],
                       "eta": _hm(s["eta"]), "weight_kg": float(s["weight_kg"] or 0),
                       "lat": s["lat"], "lon": s["lon"]}
                      for s in stops]})
    return {"trip": trips[0], "trips": trips}


# ---------- v30: «виїхав / завершив маршрут» ----------


# ---------- v59: трекінг кнопок Подзвонити / Google Maps / Waze ----------

class UiEventIn(BaseModel):
    event: str
    route_id: int | None = None
    order_id: int | None = None


@router.post("/api/driver/{token}/ui-event")
async def driver_ui_event(token: str, body: UiEventIn):
    drv = await _driver_by_token(token)
    if body.event not in ("call", "nav_google", "nav_waze"):
        raise HTTPException(400, "Невідома подія")
    await pool.execute(
        "INSERT INTO ui_events (driver_id, route_id, order_id, event) VALUES ($1,$2,$3,$4)",
        drv["id"], body.route_id, body.order_id, body.event)
    return {"ok": True}


@router.get("/api/ui-events/stats")
async def ui_events_stats(date_from: date = Query(...), date_to: date = Query(...)):
    """Зведення для логіста: скільки разів тиснули дзвінок/навігацію."""
    totals = await pool.fetch("""
        SELECT event, count(*) AS n FROM ui_events
        WHERE ts >= $1::date AND ts < $2::date + 1 GROUP BY event ORDER BY event""",
        date_from, date_to)
    by_driver = await pool.fetch("""
        SELECT d.name AS driver, e.event, count(*) AS n
        FROM ui_events e JOIN drivers d ON d.id = e.driver_id
        WHERE e.ts >= $1::date AND e.ts < $2::date + 1
        GROUP BY d.name, e.event ORDER BY d.name, e.event""", date_from, date_to)
    by_day = await pool.fetch("""
        SELECT (ts AT TIME ZONE 'Europe/Kyiv')::date AS day, event, count(*) AS n
        FROM ui_events WHERE ts >= $1::date AND ts < $2::date + 1
        GROUP BY day, event ORDER BY day""", date_from, date_to)
    return {"totals": {r["event"]: r["n"] for r in totals},
            "by_driver": [dict(r) for r in by_driver],
            "by_day": [{"day": r["day"].isoformat(), "event": r["event"], "n": r["n"]}
                       for r in by_day]}

class RouteEventIn(BaseModel):
    event: str                     # depot_arrive | start | finish
    lat: float | None = None
    lon: float | None = None
    force: bool = False            # v44: водій підтвердив натискання здалеку


@router.post("/api/driver/{token}/route/{route_id}/event")
async def route_event(token: str, route_id: int, body: RouteEventIn):
    if body.event not in ("depot_arrive", "start", "finish"):
        raise HTTPException(400, "event: depot_arrive|start|finish")
    drv = await _driver_by_token(token)
    ok = await pool.fetchval("""
        SELECT 1 FROM routes r
        WHERE r.id = $2
          AND (r.driver_id = $1
               OR r.vehicle_id IN (SELECT id FROM vehicles WHERE driver_id = $1 AND is_active))""",
        drv["id"], route_id)
    if not ok:
        raise HTTPException(404, "Не твій рейс")
    dep = await pool.fetchrow("""
        SELECT COALESCE(r.start_lat, d.lat)  AS s_lat,               -- v51
               COALESCE(r.start_lon, d.lon)  AS s_lon,
               COALESCE(r.finish_lat, d.lat) AS f_lat,
               COALESCE(r.finish_lon, d.lon) AS f_lon,
               COALESCE(r.start_kind, 'depot') AS start_kind
        FROM routes r JOIN depots d ON d.id=r.depot_id
        WHERE r.id=$1""", route_id)                                                # v47
    if body.event == "start" and dep and dep["start_kind"] == "depot":
        arrived = await pool.fetchval(               # v38/v51: склад-крок лише
            "SELECT 1 FROM route_events WHERE route_id=$1 AND event='depot_arrive'", route_id)
        if not arrived:
            # v55/v57: кнопки складу немає — подію дописуємо на виїзді
            await pool.execute("""
                INSERT INTO route_events (route_id, driver_id, event, lat, lon, source)
                VALUES ($1,$2,'depot_arrive',$3,$4,'auto')
                ON CONFLICT (route_id, event) DO NOTHING""",
                route_id, drv["id"], body.lat, body.lon)
    if body.event == "finish":
        started = await pool.fetchval(
            "SELECT 1 FROM route_events WHERE route_id=$1 AND event='start'", route_id)
        if not started:
            raise HTTPException(400, "Спочатку познач виїзд")
    tgt_lat = (dep["f_lat"] if body.event == "finish" else dep["s_lat"]) if dep else None
    tgt_lon = (dep["f_lon"] if body.event == "finish" else dep["s_lon"]) if dep else None
    dist = await _press_distance_m(drv["id"], tgt_lat, tgt_lon, body.lat, body.lon)
    if dist is not None and dist > GEO_CONFIRM_M and not body.force:
        return {"confirm_required": True, "dist_m": dist}
    # перше натискання перемагає, повторне — ігнорується
    await pool.execute("""
        INSERT INTO route_events (route_id, driver_id, event, lat, lon, dist_m)
        VALUES ($1,$2,$3,$4,$5,$6) ON CONFLICT (route_id, event) DO NOTHING""",
        route_id, drv["id"], body.event, body.lat, body.lon, dist)
    evs = await pool.fetch(
        "SELECT event, ts FROM route_events WHERE route_id=$1", route_id)
    return {e["event"]: _iso(e["ts"]) for e in evs}


def _haversine_km(lat1, lon1, lat2, lon2):
    from math import asin, cos, radians, sin, sqrt
    p1, p2 = radians(lat1), radians(lat2)
    dp, dl = radians(lat2 - lat1), radians(lon2 - lon1)
    a = sin(dp / 2) ** 2 + cos(p1) * cos(p2) * sin(dl / 2) ** 2
    return 12742 * asin(sqrt(a))


def _gps_leg_kmh(a, b):
    """Швидкість між двома GPS-точками; inf для однакових/зворотних ts."""
    seconds = (b["ts"] - a["ts"]).total_seconds()
    if seconds <= 0:
        return float("inf")
    return _haversine_km(a["lat"], a["lon"], b["lat"], b["lon"]) * 3600 / seconds


def _impossible_gps_leg(a, b):
    """Стрибок, який автомобіль фізично не міг проїхати."""
    distance = _haversine_km(a["lat"], a["lon"], b["lat"], b["lon"])
    return distance >= 2 and _gps_leg_kmh(a, b) > 180


def _drop_returning_teleports(points):
    """Прибрати короткий стрибок на другий пристрій з поверненням у реальний трек.

    Типовий випадок: логіст відкрив URL водія на ПК, браузер відправив свою
    координату, а через кілька секунд прийшла наступна точка з Android водія.
    Шукаємо повернення до фізично досяжної точки не далі ніж за 120 секунд.
    Односторонній GPS-розрив не видаляємо — його лише розірве відмальовка.
    """
    if len(points) < 3:
        return list(points)
    out = [points[0]]
    i = 1
    while i < len(points):
        current = points[i]
        previous = out[-1]
        if _impossible_gps_leg(previous, current):
            returned_at = None
            for j in range(i + 1, min(len(points), i + 9)):
                if (points[j]["ts"] - previous["ts"]).total_seconds() > 120:
                    break
                if not _impossible_gps_leg(previous, points[j]):
                    returned_at = j
                    break
            if returned_at is not None:
                i = returned_at
                continue
        out.append(current)
        i += 1
    return out


def _gps_segments(points):
    """Розбити fallback-трек на частини, не малюючи телепорти і GPS-паузи."""
    if not points:
        return []
    segments, current = [], [points[0]]
    for previous, point in zip(points, points[1:]):
        gap_sec = (point["ts"] - previous["ts"]).total_seconds()
        if gap_sec > 180 or _impossible_gps_leg(previous, point):
            if len(current) >= 2:
                segments.append([[p["lat"], p["lon"]] for p in current])
            current = [point]
        else:
            current.append(point)
    if len(current) >= 2:
        segments.append([[p["lat"], p["lon"]] for p in current])
    return segments


def _collapse_stationary(points):
    """Згорнути GPS-шум на стоянці в одну найточнішу координату.

    Android передає швидкість від FusedLocation. Послідовність <3 км/год
    вважаємо стоянкою: реальний пробіг там мізерний, зате дрейф GPS може
    намалювати кілометри та петлі по сусідніх дорогах.
    """
    out, stationary = [], []

    def flush():
        if not stationary:
            return
        best = min(stationary, key=lambda p: float(p["accuracy_m"] or 9999))
        out.append(best)
        stationary.clear()

    def moving(point):
        speed = point["speed_kmh"]
        return speed is None or float(speed) >= 3

    i = 0
    while i < len(points):
        if not moving(points[i]):
            stationary.append(points[i])
            i += 1
            continue
        # Окремі 1–2 стрибки швидкості всередині стоянки теж є GPS-шумом.
        # Рух відновлюємо лише після трьох послідовних точок >=3 км/год.
        if stationary:
            run = 1
            while i + run < len(points) and run < 3 and moving(points[i + run]):
                run += 1
            if run < 3:
                stationary.extend(points[i:i + run])
                i += run
                continue
        flush()
        out.append(points[i])
        i += 1
    flush()
    return out


async def _route_gps_points(route_id: int, driver_ids: list[int]):
    """GPS лише в межах конкретного рейсу, обрізаний поверненням на склад."""
    evs = {e["event"]: e["ts"] for e in await pool.fetch(
        "SELECT event, ts FROM route_events WHERE route_id=$1", route_id)}
    start, fin = evs.get("start"), evs.get("finish")
    depot = await pool.fetchrow("""
        SELECT COALESCE(r.start_lat, d.lat)  AS s_lat,               -- v51
               COALESCE(r.start_lon, d.lon)  AS s_lon,
               COALESCE(r.finish_lat, d.lat) AS f_lat,
               COALESCE(r.finish_lon, d.lon) AS f_lon
        FROM routes r JOIN depots d ON d.id=r.depot_id
        WHERE r.id=$1""", route_id)
    if not start or not driver_ids:
        return start, fin, [], depot
    pts = list(await pool.fetch("""
        SELECT id, ts, lat, lon, speed_kmh, accuracy_m FROM gps_points
        WHERE driver_id = ANY($1::int[]) AND ts >= $2 AND ts <= $3
          AND (accuracy_m IS NULL OR accuracy_m <= 60)
        ORDER BY ts""", driver_ids, start, fin or datetime.now(timezone.utc)))
    stop_range = await pool.fetchrow(
        "SELECT min(ts) AS first_ev, max(ts) AS last_ev FROM stop_events WHERE route_id=$1",
        route_id)
    if depot and pts:
        inside_s = [_haversine_km(p["lat"], p["lon"], depot["s_lat"], depot["s_lon"]) <= 0.3
                    for p in pts]
        inside_f = [_haversine_km(p["lat"], p["lon"], depot["f_lat"], depot["f_lon"]) <= 0.3
                    for p in pts]
        i0, i1 = 0, len(pts) - 1
        if stop_range and stop_range["first_ev"]:
            for i, point in enumerate(pts):
                if point["ts"] >= stop_range["first_ev"]:
                    break
                if inside_s[i]:
                    i0 = i
        if stop_range and stop_range["last_ev"]:
            for i in range(len(pts) - 1, -1, -1):
                if pts[i]["ts"] <= stop_range["last_ev"]:
                    break
                if inside_f[i]:
                    i1 = i
        if i1 >= i0:
            pts = pts[i0:i1 + 1]
    return start, fin, pts, depot


async def _route_actual_track(route_id: int, driver_ids: list[int]):
    """Фактичний дорожній трек і км; очищений GPS — резерв при збої OSRM."""
    _, fin, raw, depot = await _route_gps_points(route_id, driver_ids)
    if len(raw) < 2:
        return None
    signature = (raw[0]["id"], raw[-1]["id"], len(raw), fin)
    cached = _track_cache.get(route_id)
    if cached and cached[0] == signature:
        return cached[1]

    filtered = _drop_returning_teleports(raw)
    clean = _collapse_stationary(filtered)
    # Спершу прибираємо стоянки, потім рівномірно обмежуємо запит OSRM.
    step = max(1, (len(clean) + 399) // 400)
    thin = clean[::step]
    if thin[-1]["id"] != clean[-1]["id"]:
        thin.append(clean[-1])
    boundary_ok = bool(
        depot
        and _haversine_km(raw[0]["lat"], raw[0]["lon"],
                          depot["s_lat"], depot["s_lon"]) <= 0.5
        and (not fin or _haversine_km(
            raw[-1]["lat"], raw[-1]["lon"], depot["f_lat"], depot["f_lon"]) <= 0.5))
    matched = await osrm.match_with_distance([
        (p["lat"], p["lon"], int(p["ts"].timestamp()),
         min(50, max(10, int(p["accuracy_m"] or 25))))
        for p in thin])
    if matched and matched[2] >= 0.9:
        geometry, km, coverage, segments = matched
        result = {"points": geometry, "km": round(km, 1), "source": "osrm",
                  "segments": segments, "coverage": round(coverage, 3),
                  "boundary_ok": boundary_ok}
    else:
        km = sum(seg for a, b in zip(clean, clean[1:])
                 if (seg := _haversine_km(a["lat"], a["lon"], b["lat"], b["lon"])) < 2)
        result = {"points": [[p["lat"], p["lon"]] for p in thin],
                  "segments": _gps_segments(thin),
                  "km": round(km, 1),
                  "source": "gps_fallback_partial" if matched else "gps_fallback",
                  "coverage": round(matched[2], 3) if matched else 0,
                  "boundary_ok": boundary_ok}
    _track_cache[route_id] = (signature, result)
    return result


async def _route_worklog(route_id: int, driver_ids: list[int]):
    """(start_ts, finish_ts, хвилини, км по GPS) між подіями рейсу.

    v41: пробіг обрізається геозоною складу (300 м) — плечі дім→склад і
    склад→дім не входять у факт. Межі кліпінгу прив'язані до подій по точках,
    щоб проїзд повз склад посеред рейсу нічого не відрізав.
    """
    evs = {e["event"]: e["ts"] for e in await pool.fetch(
        "SELECT event, ts FROM route_events WHERE route_id=$1", route_id)}
    start, fin = evs.get("start"), evs.get("finish")
    minutes = int((fin - start).total_seconds() // 60) if start and fin else None
    km = None
    if start and driver_ids:
        pts = await pool.fetch("""
            SELECT ts, lat, lon FROM gps_points
            WHERE driver_id = ANY($1::int[]) AND ts >= $2 AND ts <= $3
              AND (accuracy_m IS NULL OR accuracy_m <= 60)
            ORDER BY ts""", driver_ids, start, fin or datetime.now(timezone.utc))
        depot = await pool.fetchrow("""
            SELECT COALESCE(r.start_lat, d.lat)  AS s_lat,           -- v51
                   COALESCE(r.start_lon, d.lon)  AS s_lon,
                   COALESCE(r.finish_lat, d.lat) AS f_lat,
                   COALESCE(r.finish_lon, d.lon) AS f_lon
            FROM routes r JOIN depots d ON d.id=r.depot_id
            WHERE r.id=$1""", route_id)
        ev = await pool.fetchrow(
            "SELECT min(ts) AS first_ev, max(ts) AS last_ev FROM stop_events WHERE route_id=$1",
            route_id)
        if depot and pts:
            ing_s = [_haversine_km(p["lat"], p["lon"], depot["s_lat"], depot["s_lon"]) <= 0.3
                     for p in pts]
            ing_f = [_haversine_km(p["lat"], p["lon"], depot["f_lat"], depot["f_lon"]) <= 0.3
                     for p in pts]
            i0, i1 = 0, len(pts) - 1
            if ev and ev["first_ev"]:      # старт: остання точка на старті до 1-ї події
                for i, p in enumerate(pts):
                    if p["ts"] >= ev["first_ev"]:
                        break
                    if ing_s[i]:
                        i0 = i
            if ev and ev["last_ev"]:       # фініш: перша точка на фініші після останньої події
                for i in range(len(pts) - 1, -1, -1):
                    if pts[i]["ts"] <= ev["last_ev"]:
                        break
                    if ing_f[i]:
                        i1 = i
            if i1 >= i0:
                pts = pts[i0:i1 + 1]
        km = 0.0
        for a, b in zip(pts, pts[1:]):
            seg = _haversine_km(a["lat"], a["lon"], b["lat"], b["lon"])
            if seg < 2:            # телепорти від збоїв GPS не рахуємо
                km += seg
    return _iso(start), _iso(fin), minutes, (round(km, 1) if km is not None else None)
