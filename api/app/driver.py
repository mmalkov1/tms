"""v17: мобильный кабинет водителя (фаза 1).

Токены доступа, выдача рейса на день, ручные факты «прибув/поїхав»,
приём GPS-точек, план/факт для логиста (страницы driver.html, tokens.html,
facts.html). Схема — migrate_011, применяется из init() при старте API.
"""
import secrets
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

router = APIRouter(tags=["driver"])
pool = None


async def init(db_pool):
    """Создание таблиц (идемпотентно). Вызывается из startup."""
    global pool
    pool = db_pool
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


async def _driver_by_token(token: str):
    row = await pool.fetchrow("""
        SELECT d.id, d.name FROM driver_tokens t
        JOIN drivers d ON d.id = t.driver_id
        WHERE t.token = $1 AND t.is_active AND d.is_active""", token)
    if not row:
        raise HTTPException(401, "Недійсний токен")
    return row


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
async def driver_trip(token: str, d: date | None = Query(None)):
    drv = await _driver_by_token(token)
    day = d or kyiv_today()
    route = await pool.fetchrow("""
        SELECT r.id, r.plan_date, r.color, r.total_km, r.depart_time, r.return_time,
               v.name AS vehicle_name, v.plate
        FROM routes r JOIN vehicles v ON v.id = r.vehicle_id
        WHERE r.plan_date = $2
          AND r.project_id IN (SELECT id FROM projects WHERE is_released)
          AND (r.driver_id = $1
               OR r.vehicle_id IN (SELECT id FROM vehicles WHERE driver_id = $1 AND is_active))
        ORDER BY r.id DESC LIMIT 1""", drv["id"], day)
    if not route:
        # рейси є, але проект не активовано?
        pending = await pool.fetchval("""
            SELECT 1 FROM routes r
            WHERE r.plan_date = $2
              AND (r.driver_id = $1
                   OR r.vehicle_id IN (SELECT id FROM vehicles WHERE driver_id = $1 AND is_active))
            LIMIT 1""", drv["id"], day)
        return {"driver": drv["name"], "date": day.isoformat(),
                "route": None, "stops": [], "not_released": bool(pending)}

    stops = await pool.fetch("""
        SELECT s.seq, s.eta, s.etd, o.id AS order_id, o.client, o.kind, o.address,
               o.address_extra, o.lat, o.lon, o.tw_from, o.tw_to,
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
        "route": {"id": route["id"], "vehicle": route["vehicle_name"], "plate": route["plate"],
                  "color": route["color"], "total_km": float(route["total_km"] or 0),
                  "depart": _hm(route["depart_time"]), "return": _hm(route["return_time"])},
        "stops": [{
            "seq": s["seq"], "order_id": s["order_id"], "doc_number": s["doc_number"],
            "client": s["client"], "kind": s["kind"],
            "address": s["address"], "address_extra": s["address_extra"],
            "lat": s["lat"], "lon": s["lon"], "phone": s["phone"], "seats": s["seats"],
            "contact_person": s["contact_person"],
            "tw_from": _hm(s["tw_from"]), "tw_to": _hm(s["tw_to"]),
            "eta": _hm(s["eta"]), "etd": _hm(s["etd"]),
            "weight_kg": float(s["weight_kg"] or 0), "volume_m3": float(s["volume_m3"] or 0),
            "arrive_ts": _iso(s["arrive_ts"]), "depart_ts": _iso(s["depart_ts"]),
            "fail_ts": _iso(s["fail_ts"]), "fail_reason": s["fail_reason"],
        } for s in stops],
    }


# ---------- факты «прибув / поїхав» ----------

class StopEvent(BaseModel):
    event: str                 # arrive | depart
    lat: float | None = None
    lon: float | None = None


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
    # первый зафиксированный факт — истина; повторная отправка идемпотентна
    row = await pool.fetchrow("""
        INSERT INTO stop_events (route_id, order_id, event, lat, lon)
        VALUES ($1,$2,$3,$4,$5)
        ON CONFLICT (route_id, order_id, event) DO NOTHING
        RETURNING ts""", rs["route_id"], order_id, body.event, body.lat, body.lon)
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
    reason_name = None
    if body.reason_id:
        reason_name = await pool.fetchval(
            "SELECT name FROM fail_reasons WHERE id=$1 AND is_active", body.reason_id)
        if not reason_name:
            raise HTTPException(404, "Причину не знайдено")
    txt = (body.reason_text or "").strip() or None
    if not body.reason_id and not txt:
        raise HTTPException(400, "Вкажи причину")
    row = await pool.fetchrow("""
        INSERT INTO stop_events (route_id, order_id, event, lat, lon, reason_id, reason_text)
        VALUES ($1,$2,'fail',$3,$4,$5,$6)
        ON CONFLICT (route_id, order_id, event) DO NOTHING
        RETURNING ts""", rs["route_id"], order_id, body.lat, body.lon, body.reason_id, txt)
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
async def position(token: str, body: GpsBatch):
    drv = await _driver_by_token(token)
    if not body.points:
        return {"saved": 0}
    route_id = await pool.fetchval("""
        SELECT id FROM routes WHERE plan_date=$2
          AND project_id IN (SELECT id FROM projects WHERE is_released)
          AND (driver_id=$1
               OR vehicle_id IN (SELECT id FROM vehicles WHERE driver_id=$1 AND is_active))
        ORDER BY id DESC LIMIT 1""", drv["id"], kyiv_today())
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
               v.name AS vehicle_name, v.driver_id AS veh_driver_id,
               d.id AS driver_id, d.name AS driver_name
        FROM routes r JOIN vehicles v ON v.id=r.vehicle_id
        LEFT JOIN drivers d ON d.id=r.driver_id
        WHERE r.plan_date = $1
          AND r.project_id IN (SELECT id FROM projects WHERE is_released)
        ORDER BY r.id""", plan_date)
    out = []
    for r in routes:
        stops = await pool.fetch("""
            SELECT s.seq, s.eta, s.etd, o.id AS order_id, o.client, o.address, o.lat, o.lon,
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
        out.append({
            "route_id": r["id"], "color": r["color"],
            "vehicle": r["vehicle_name"], "driver": r["driver_name"],
            "last_gps": ({"ts": _iso(last["ts"]), "lat": last["lat"], "lon": last["lon"],
                          "speed_kmh": last["speed_kmh"]} if last else None),
            "stops": [{
                "seq": s["seq"], "order_id": s["order_id"], "client": s["client"],
                "address": s["address"], "lat": s["lat"], "lon": s["lon"],
                "eta": _hm(s["eta"]), "etd": _hm(s["etd"]),
                "arrive_ts": _iso(s["arrive_ts"]), "depart_ts": _iso(s["depart_ts"]),
                "fail_ts": _iso(s["fail_ts"]), "fail_reason": s["fail_reason"],
            } for s in stops],
        })
    return out
