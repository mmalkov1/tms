"""Интеграция с 1С (замена Tocan API).

Протокол повторяет схему Tocan, чтобы 1С-код менялся минимально:
  GET  /api/1c/auth?login=&password=          -> XML {ERROR, MESSAGE{SECURITY_KEY}}  (сессионный ключ, 24ч)
  POST /api/1c/import?key=&project=&name_project=&date_project=   body: <ORDERS><ORDER>...</ORDER></ORDERS>
       project пуст  -> создается новый проект, в ответе MESSAGE.SECURITY_KEY = ключ проекта
       project задан -> обновление существующего проекта (ключ проекта)
  GET  /api/1c/export?key=&start_date=&end_date=  -> XML {ERROR, TRIP*} по выпущенным маршрутам

Формат дат в экспорте: yyyy-MM-ddTHH:mm:ss (парсится в 1С функцией TMS_ПрочитатьДату).
Знак веса/объема в PRODUCT: отрицательный или COUNT=-1 => забор груза (pickup).

v34: экспорт отдает ФАКТИЧЕСКИЕ статусы точек и время из stop_events /
route_events — для фоновой синхронизации статусов документов в 1С
(замена Tocan exportAPILogist). Семантика STATUS_POINT (как у Tocan):
  склад-выезд (IN_TRIP_NUMBER=0): 4 после «Виїхав на маршрут», иначе 1
  точка: fail -> 5, depart -> 4, arrive без depart -> 2, иначе 1
  склад-возврат: 4 после «Завершив маршрут», иначе 1
Все фактические времена — Europe/Kyiv.
"""
import asyncio
import os
import secrets
import xml.etree.ElementTree as ET
from datetime import date, datetime, time, timedelta, timezone

from fastapi import APIRouter, Query, Request, Response

try:
    from zoneinfo import ZoneInfo
    KYIV = ZoneInfo("Europe/Kyiv")
except Exception:                       # в контейнере нет tzdata — летнее UTC+3
    KYIV = timezone(timedelta(hours=3))

router = APIRouter(prefix="/api/1c", tags=["1c"])
pool = None  # инициализируется из main.py


LOGIN = os.getenv("TMS_1C_LOGIN", "1c")
PASSWORD = os.getenv("TMS_1C_PASSWORD", "kultukr-1c")
SESSION_TTL_H = 24


def _xml(text: str) -> Response:
    return Response(content='<?xml version="1.0" encoding="UTF-8"?>\n' + text,
                    media_type="application/xml; charset=utf-8")


def _err(msg: str) -> Response:
    return _xml(f"<RESPONSE><ERROR>1</ERROR><ERROR_MESSAGE>{_esc(msg)}</ERROR_MESSAGE></RESPONSE>")


def _esc(s) -> str:
    return ("" if s is None else str(s)).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


async def init(db_pool):
    """Создание таблицы ключей (идемпотентно). Вызывается из startup."""
    global pool
    pool = db_pool
    await pool.execute("""
        CREATE TABLE IF NOT EXISTS sync_keys (
            key        TEXT PRIMARY KEY,
            kind       TEXT NOT NULL,               -- 'session' | 'project'
            project_id INT REFERENCES projects(id) ON DELETE CASCADE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )""")


async def _check_key(key: str):
    """-> ('session', None) | ('project', project_id) | None"""
    if not key:
        return None
    r = await pool.fetchrow("SELECT * FROM sync_keys WHERE key=$1", key)
    if not r:
        return None
    if r["kind"] == "session" and r["created_at"] < datetime.now(r["created_at"].tzinfo) - timedelta(hours=SESSION_TTL_H):
        await pool.execute("DELETE FROM sync_keys WHERE key=$1", key)
        return None
    return r["kind"], r["project_id"]


# ---------- auth ----------

@router.get("/auth")
async def auth(login: str = Query(""), password: str = Query("")):
    if login != LOGIN or password != PASSWORD:
        return _err("Невірний логін або пароль")
    key = secrets.token_hex(16)
    await pool.execute("INSERT INTO sync_keys (key, kind) VALUES ($1,'session')", key)
    # чистка протухших сессионных ключей
    await pool.execute("DELETE FROM sync_keys WHERE kind='session' AND created_at < now() - interval '48 hours'")
    return _xml(f"<RESPONSE><ERROR>0</ERROR><MESSAGE><SECURITY_KEY>{key}</SECURITY_KEY></MESSAGE></RESPONSE>")


# ---------- импорт заказов из 1С ----------

def _parse_hhmm(s: str) -> time | None:
    try:
        h, m = s.strip().split(":")
        return time(int(h) % 24, int(m) % 60)
    except Exception:
        return None


def _int_or_none(s: str) -> int | None:
    """1С може прислати '', '3', '3,0' або '3.0' — приводимо до int або None."""
    if not s:
        return None
    try:
        return int(float(str(s).strip().replace(",", ".")))
    except (TypeError, ValueError):
        return None


def _parse_order(el: ET.Element) -> dict | None:
    def g(tag, default=""):
        x = el.find(tag)
        return (x.text or "").strip() if x is not None and x.text else default

    def g_any(tag, default=""):
        """Как g(), но ищет тег на любом уровне вложенности внутри ORDER.
        1С может писать реквизит как прямым потомком, так и внутри строк заказа."""
        x = el.find(tag)                       # сперва прямой потомок
        if x is None or not (x.text or "").strip():
            x = next((e for e in el.iter(tag) if (e.text or "").strip()), None)
        return (x.text or "").strip() if x is not None and x.text else default

    code = g("CODE")
    if not code:
        return None

    # вес/объем из PRODUCTS; отрицательные значения или COUNT=-1 => забор
    w = v = 0.0
    pickup = False
    for p in el.findall("./PRODUCTS/PRODUCT"):
        try:
            pw = float((p.get("WEIGHT") or "0").replace(",", "."))
            pv = float((p.get("VOLUME") or "0").replace(",", "."))
        except ValueError:
            pw = pv = 0.0
        if pw < 0 or pv < 0 or (p.get("COUNT") or "") == "-1":
            pickup = True
        w += abs(pw)
        v += abs(pv)
    name = g("NAME")
    if "Забор" in name or "Забір" in name:
        pickup = True

    tw_from = tw_to = None
    wt = g("SHOP_WORK_TIME")           # "09:00-18:00"
    if "-" in wt:
        a, b = wt.split("-", 1)
        tw_from, tw_to = _parse_hhmm(a), _parse_hhmm(b)

    break_from = break_to = None       # v39: перерва точки (обід)
    dt_ = g("SHOP_DINNER_TIME")        # "13:00-14:00"
    if "-" in dt_:
        a, b = dt_.split("-", 1)
        break_from, break_to = _parse_hhmm(a), _parse_hhmm(b)
    if break_from and break_to and break_to <= break_from:
        break_from = break_to = None   # сміття з 1С не пускаємо в модель

    lat = lon = None
    try:
        lat = float(g("GeoX").replace(",", ".")) or None   # GeoX = Широта
        lon = float(g("GeoY").replace(",", ".")) or None   # GeoY = Долгота
    except ValueError:
        pass

    try:
        service = int(g("UNLOAD_TIME") or "0") or None
    except ValueError:
        service = None

    return {
        "doc_number": code,
        "doc_ref": name or code,
        "warehouse_code": g("WAREHOUSE_CODE") or None,
        "kind": "pickup" if pickup else "delivery",
        "client": g("CLIENT_NAME") or g("SHOP_NAME") or "—",
        "address": g("ADDRESS"),
        "address_extra": g("COMMENTS_SHOP") or None,
        "comment": g("COMMENTS") or None,
        # v21/v22/v23: телефон і кількість місць — шукаємо на будь-якому рівні
        "phone": g_any("PHONE") or None,
        "seats": _int_or_none(g_any("SEATS")),
        "contact_person": g_any("PERSON_NAME") or None,
        "lat": lat, "lon": lon,
        "tw_from": tw_from, "tw_to": tw_to,
        "break_from": break_from, "break_to": break_to,
        "service_min": service,          # v84: None = 1С не передала, дефолт не нав'язуємо
        "weight_kg": round(w, 2), "volume_m3": round(v, 3),
    }


@router.post("/import")
async def import_orders(request: Request,
                        key: str = Query(""),
                        project: str = Query(None),
                        name_project: str = Query(None),
                        date_project: str = Query(None)):
    auth_kind = await _check_key(key)
    if auth_kind is None:
        return _err("Невірний або протухлий SECURITY_KEY — авторизуйтесь заново")

    body = await request.body()
    try:
        root = ET.fromstring(body.decode("utf-8-sig"))
    except Exception as e:
        return _err(f"XML не розібрано: {e}")

    order_els = root.findall(".//ORDER") if root.tag != "ORDER" else [root]

    # --- ВРЕМЕННАЯ ДИАГНОСТИКА (v23): что реально прислала 1С ---
    if order_els:
        first = order_els[0]
        direct = [ch.tag for ch in first]                       # прямые потомки ORDER
        all_tags = sorted({e.tag for e in first.iter()})        # все теги внутри ORDER (любой уровень)
        print("=" * 60, flush=True)
        print("[1C-DEBUG] ORDER, прямые потомки:", direct, flush=True)
        print("[1C-DEBUG] ORDER, все теги внутри:", all_tags, flush=True)
        for tag in ("SEATS", "PHONE"):
            found = first.findall(f".//{tag}")                  # ищем на любом уровне
            if found:
                for el in found:
                    parent = next((p.tag for p in first.iter()
                                   for c in p if c is el), "ORDER")
                    print(f"[1C-DEBUG] {tag}: НАЙДЕН, родитель=<{parent}>, "
                          f"значение={el.text!r}", flush=True)
            else:
                print(f"[1C-DEBUG] {tag}: НЕТ В XML ВООБЩЕ", flush=True)
        print("[1C-DEBUG] сырой XML первого ORDER:", flush=True)
        print(ET.tostring(first, encoding="unicode")[:1500], flush=True)
        print("=" * 60, flush=True)
    # --- КОНЕЦ ДИАГНОСТИКИ ---

    rows = [r for r in (_parse_order(el) for el in order_els) if r]
    if not rows:
        return _err("У файлі немає жодного ORDER з CODE")

    # проект: по ключу проекта — обновляем; иначе создаем новый
    project_key = None
    project_id = None
    if project:
        pk = await _check_key(project)
        if pk and pk[0] == "project":
            project_id, project_key = pk[1], project
        else:
            return _err("Невірний ключ проекту (параметр project)")
    if project_id is None:
        try:
            pd = date.fromisoformat(date_project) if date_project else date.today()
        except ValueError:
            return _err("date_project: очікується yyyy-MM-dd")
        pname = name_project or pd.strftime("%d-%m") + "_1C"
        project_id = await pool.fetchval(
            "INSERT INTO projects (plan_date, name) VALUES ($1,$2) RETURNING id", pd, pname)
        project_key = secrets.token_hex(16)
        await pool.execute(
            "INSERT INTO sync_keys (key, kind, project_id) VALUES ($1,'project',$2)",
            project_key, project_id)

    plan_date = await pool.fetchval("SELECT plan_date FROM projects WHERE id=$1", project_id)

    # код склада 1С (для точек выезда/возвращения в экспорте рейсов)
    wcode = next((r["warehouse_code"] for r in rows if r.get("warehouse_code")), None)
    if wcode:
        await pool.execute("UPDATE projects SET warehouse_code_1c=$1 WHERE id=$2", wcode, project_id)

    ins = upd = 0
    async with pool.acquire() as c:
        for r in rows:
            extra = r["address_extra"]
            if r["comment"]:
                extra = f'{extra} · {r["comment"]}' if extra else r["comment"]
            res = await c.execute("""
                INSERT INTO orders (plan_date, doc_number, doc_ref, kind, client, address,
                    address_extra, lat, lon, tw_from, tw_to, service_min, weight_kg, volume_m3,
                    status_1c, project_id, phone, seats, contact_person, break_from, break_to,
                    address_1c)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21,$6)
                ON CONFLICT (project_id, doc_number) DO UPDATE SET
                    kind=EXCLUDED.kind, client=EXCLUDED.client, address=EXCLUDED.address,
                    address_1c=EXCLUDED.address_1c,
                    address_extra=EXCLUDED.address_extra,
                    lat=COALESCE(EXCLUDED.lat, orders.lat),
                    lon=COALESCE(EXCLUDED.lon, orders.lon),
                    tw_from=EXCLUDED.tw_from, tw_to=EXCLUDED.tw_to,
                    -- v84: розрахований норматив простою не затираємо;
                    -- перезапис лише коли 1С реально прислала своє значення
                    service_min=COALESCE(EXCLUDED.service_min, orders.service_min),
                    weight_kg=EXCLUDED.weight_kg, volume_m3=EXCLUDED.volume_m3,
                    phone=COALESCE(EXCLUDED.phone, orders.phone),
                    seats=COALESCE(EXCLUDED.seats, orders.seats),
                    contact_person=COALESCE(EXCLUDED.contact_person, orders.contact_person),
                    break_from=EXCLUDED.break_from, break_to=EXCLUDED.break_to
            """, plan_date, r["doc_number"], r["doc_ref"], r["kind"], r["client"], r["address"],
                extra, r["lat"], r["lon"], r["tw_from"], r["tw_to"], r["service_min"],
                r["weight_kg"], r["volume_m3"], "1C", project_id, r["phone"], r["seats"],
                r["contact_person"], r["break_from"], r["break_to"])
            if res.startswith("INSERT"):
                ins += 1
            else:
                upd += 1

    # v84: заявки без service_min від 1С — норматив з історії, інакше дефолт 15.
    # Робиться після кожного імпорту, щоб нові заявки не з'являлися в плані з «15 хв».
    try:
        from . import main as _main
        await _main.apply_service_norms(project_id)
    except Exception as e:
        print("service norms after 1C import failed:", e)

    # координаты из кеша геокодирования (адреса, исправленные логистом ранее)
    try:
        from . import main as _main
        await _main.geo_cache_fill(project_id)
        await _main.geo_cache_apply_manual(project_id)   # v63: ручні правки понад GeoX/GeoY з 1С
        # v60: решту адрес геокодуємо у фоні — відповідь 1С не чекає
        asyncio.create_task(_main.geocode_missing_bg(project_id))
    except Exception:
        pass

    # v33: повторний імпорт оновлює заявки, що вже в рейсах (вага/місця/вікна
    # можуть змінитися вранці) — перераховуємо зачеплені рейси, водій побачить
    # свіже при наступному оновленні застосунку
    try:
        from . import main as _main
        rids = await pool.fetch("""
            SELECT DISTINCT s.route_id FROM route_stops s
            JOIN orders o ON o.id = s.order_id WHERE o.project_id = $1""", project_id)
        for row in rids:
            await _main._rebuild_route(row["route_id"])
    except Exception:
        pass

    return _xml(
        "<RESPONSE><ERROR>0</ERROR>"
        f"<MESSAGE><SECURITY_KEY>{project_key}</SECURITY_KEY>"
        f"<PROJECT_ID>{project_id}</PROJECT_ID>"
        f"<PARSED>{len(rows)}</PARSED><INSERTED>{ins}</INSERTED><UPDATED>{upd}</UPDATED>"
        "</MESSAGE></RESPONSE>")


# ---------- экспорт рейсов в 1С ----------

def _dt(plan_date: date, t: time | None) -> str:
    if t is None:
        return ""
    return datetime.combine(plan_date, t).strftime("%Y-%m-%dT%H:%M:%S")


def _ft(ts) -> str:
    """v34: фактическое время (TIMESTAMPTZ) -> строка в Europe/Kyiv для 1С."""
    if ts is None:
        return ""
    return ts.astimezone(KYIV).strftime("%Y-%m-%dT%H:%M:%S")


@router.get("/export")
async def export_trips(key: str = Query(""),
                       start_date: str = Query(None), end_date: str = Query(None)):
    auth_kind = await _check_key(key)
    if auth_kind is None:
        return _err("Невірний або протухлий SECURITY_KEY")

    kind, project_id = auth_kind
    if kind == "project":
        rr = await pool.fetch("""
            SELECT r.*, v.name vehicle_name, v.code_1c car_code, d.code_1c driver_code,
                   d.name driver_name, dep.name depot_name, p.warehouse_code_1c
            FROM routes r JOIN vehicles v ON v.id=r.vehicle_id
            LEFT JOIN drivers d ON d.id=r.driver_id
            JOIN depots dep ON dep.id=r.depot_id
            LEFT JOIN projects p ON p.id=r.project_id
            WHERE r.project_id=$1 ORDER BY r.id""", project_id)
    else:
        try:
            d1 = date.fromisoformat(start_date) if start_date else date.today()
            d2 = date.fromisoformat(end_date) if end_date else d1
        except ValueError:
            return _err("start_date/end_date: очікується yyyy-MM-dd")
        rr = await pool.fetch("""
            SELECT r.*, v.name vehicle_name, v.code_1c car_code, d.code_1c driver_code,
                   d.name driver_name, dep.name depot_name, p.warehouse_code_1c
            FROM routes r JOIN vehicles v ON v.id=r.vehicle_id
            LEFT JOIN drivers d ON d.id=r.driver_id
            JOIN depots dep ON dep.id=r.depot_id
            LEFT JOIN projects p ON p.id=r.project_id
            WHERE r.plan_date BETWEEN $1 AND $2 ORDER BY r.id""", d1, d2)

    parts = ["<RESPONSE><ERROR>0</ERROR>"]
    for r in rr:
        ret_t = r["return_time_manual"] or r["return_time"]     # v51: ручний фініш
        ss = await pool.fetch("""
            SELECT s.seq, s.eta, s.etd, s.order_id, o.doc_number, o.weight_kg
            FROM route_stops s JOIN orders o ON o.id=s.order_id
            WHERE s.route_id=$1 ORDER BY s.seq""", r["id"])
        if not ss:
            continue   # пустые машины в 1С не отдаем

        # v34: факты рейса и точек — для STATUS_POINT и *_FACT
        r_ev = {e["event"]: e["ts"] for e in await pool.fetch(
            "SELECT event, ts FROM route_events WHERE route_id=$1", r["id"])}
        s_ev = {}
        for e in await pool.fetch(
                "SELECT order_id, event, ts FROM stop_events WHERE route_id=$1", r["id"]):
            s_ev.setdefault(e["order_id"], {})[e["event"]] = e["ts"]
        started, finished = r_ev.get("start"), r_ev.get("finish")

        total_w = sum(float(s["weight_kg"] or 0) for s in ss)
        parts.append("<TRIP>")
        parts.append(f"<TRIP_CODE>{r['id']}</TRIP_CODE>")
        parts.append(f"<CODE_CAR>{_esc(r['car_code'] or '')}</CODE_CAR>")
        parts.append(f"<CODE_DRIVER>{_esc(r['driver_code'] or '')}</CODE_DRIVER>")
        parts.append(f"<COMMENTS>{_esc(r['vehicle_name'])} · {_esc(r['driver_name'] or '')}</COMMENTS>")
        parts.append(f"<TRIP_DIST_PLAN>{r['total_km'] or 0}</TRIP_DIST_PLAN>")
        parts.append("<TRIP_DIST_FACT></TRIP_DIST_FACT>")
        parts.append(f"<START_TIME_PLAN>{_dt(r['plan_date'], r['depart_time'])}</START_TIME_PLAN>")
        parts.append(f"<FINISH_TIME_PLAN>{_dt(r['plan_date'], ret_t)}</FINISH_TIME_PLAN>")
        parts.append(f"<START_TIME_FACT>{_ft(started)}</START_TIME_FACT>"
                     f"<FINISH_TIME_FACT>{_ft(finished)}</FINISH_TIME_FACT>")
        parts.append(f"<TRIP_ORDERS_WEIGHT_PLAN>{round(total_w, 2)}</TRIP_ORDERS_WEIGHT_PLAN>")
        parts.append("<ORDERS>")
        # склад — точка выезда (как у Tocan: 1С найдет по коду в Справочники.Склады)
        # v34: STATUS_POINT=4 после «Виїхав на маршрут» — открывает синхронизацию
        # статусов точек на стороне 1С (логика СтатусСоСклада)
        wcode = r["warehouse_code_1c"]
        if wcode:
            dep_arr = r_ev.get("depot_arrive")     # v38: «прибув на склад»
            parts.append("<ORDER>")
            parts.append(f"<CODE>{_esc(wcode)}</CODE>")
            parts.append("<IN_TRIP_NUMBER>0</IN_TRIP_NUMBER>")
            parts.append("<PLAN_DIST>0</PLAN_DIST><FACT_DIST>0</FACT_DIST>")
            parts.append(f"<DELIVERY_DATE_PLAN>{_dt(r['plan_date'], r['depart_time'])}</DELIVERY_DATE_PLAN>")
            parts.append(f"<DELIVERY_DATE_FACT>{_ft(dep_arr or started)}</DELIVERY_DATE_FACT>")
            parts.append(f"<DELIVERY_OUTDATE_PLAN>{_dt(r['plan_date'], r['depart_time'])}</DELIVERY_OUTDATE_PLAN>")
            parts.append(f"<DELIVERY_OUTDATE_FACT>{_ft(started)}</DELIVERY_OUTDATE_FACT>")
            parts.append(f"<STATUS_POINT>{4 if started else 1}</STATUS_POINT>")
            parts.append("</ORDER>")
        for s in ss:
            ev = s_ev.get(s["order_id"], {})
            if "fail" in ev:                       # відмова водія
                st, f_in, f_out = 5, ev.get("arrive"), ev.get("fail")
            elif "depart" in ev:                   # поїхав від клієнта
                st, f_in, f_out = 4, ev.get("arrive"), ev.get("depart")
            elif "arrive" in ev:                   # на точці
                st, f_in, f_out = 2, ev.get("arrive"), None
            else:                                  # план; 1С сама повысит до «2»
                st, f_in, f_out = 1, None, None    # после выезда со склада
            parts.append("<ORDER>")
            parts.append(f"<CODE>{_esc(s['doc_number'])}</CODE>")
            parts.append(f"<IN_TRIP_NUMBER>{s['seq']}</IN_TRIP_NUMBER>")
            parts.append("<PLAN_DIST>0</PLAN_DIST><FACT_DIST>0</FACT_DIST>")
            parts.append(f"<DELIVERY_DATE_PLAN>{_dt(r['plan_date'], s['eta'])}</DELIVERY_DATE_PLAN>")
            parts.append(f"<DELIVERY_DATE_FACT>{_ft(f_in)}</DELIVERY_DATE_FACT>")
            parts.append(f"<DELIVERY_OUTDATE_PLAN>{_dt(r['plan_date'], s['etd'])}</DELIVERY_OUTDATE_PLAN>")
            parts.append(f"<DELIVERY_OUTDATE_FACT>{_ft(f_out)}</DELIVERY_OUTDATE_FACT>")
            parts.append(f"<STATUS_POINT>{st}</STATUS_POINT>")
            parts.append("</ORDER>")
        # склад — точка возвращения (последняя в рейсе)
        if wcode:
            last_seq = max(s["seq"] for s in ss) + 1
            parts.append("<ORDER>")
            parts.append(f"<CODE>{_esc(wcode)}</CODE>")
            parts.append(f"<IN_TRIP_NUMBER>{last_seq}</IN_TRIP_NUMBER>")
            parts.append("<PLAN_DIST>0</PLAN_DIST><FACT_DIST>0</FACT_DIST>")
            parts.append(f"<DELIVERY_DATE_PLAN>{_dt(r['plan_date'], ret_t)}</DELIVERY_DATE_PLAN>")
            parts.append(f"<DELIVERY_DATE_FACT>{_ft(finished)}</DELIVERY_DATE_FACT>")
            parts.append(f"<DELIVERY_OUTDATE_PLAN>{_dt(r['plan_date'], ret_t)}</DELIVERY_OUTDATE_PLAN>")
            parts.append(f"<DELIVERY_OUTDATE_FACT>{_ft(finished)}</DELIVERY_OUTDATE_FACT>")
            parts.append(f"<STATUS_POINT>{4 if finished else 1}</STATUS_POINT>")
            parts.append("</ORDER>")
        parts.append("</ORDERS></TRIP>")
    parts.append("</RESPONSE>")
    return _xml("".join(parts))
