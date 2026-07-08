"""TMS Культтовари Глобал — API v2."""
import os
from datetime import date, time

import asyncpg
from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import dwell, geo, geocoder, importer, osrm, solver

DB_DSN = os.getenv("DATABASE_URL", "postgresql://tms:tms@db:5432/tms")
ROUTE_COLORS = ["#E82A2C", "#00356B", "#2E8B57", "#B8860B", "#8B008B", "#FF6347",
                "#1E90FF", "#FF8C00"]

app = FastAPI(title="TMS Kultukr")
pool: asyncpg.Pool = None


@app.on_event("startup")
async def startup():
    global pool
    pool = await asyncpg.create_pool(DB_DSN)


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
    no_geo = sum(1 for r in rows if r["lat"] is None)
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
        SELECT v.*, d.name AS driver_name, d.shift_start, d.shift_end
        FROM vehicles v LEFT JOIN drivers d ON d.id=v.driver_id
        WHERE v.is_active ORDER BY v.id""")
    return [dict(r) for r in rows]


class VehicleIn(BaseModel):
    name: str
    plate: str | None = None
    max_weight_kg: float
    max_volume_m3: float
    is_hired: bool = False
    driver_name: str | None = None
    shift_start: str = "08:00"
    shift_end: str = "18:00"


@app.post("/api/vehicles")
async def create_vehicle(v: VehicleIn):
    driver_id = None
    if v.driver_name:
        driver_id = await pool.fetchval(
            "INSERT INTO drivers (name, shift_start, shift_end) VALUES ($1,$2,$3) RETURNING id",
            v.driver_name, m2t(parse_hhmm(v.shift_start, 480)), m2t(parse_hhmm(v.shift_end, 1080)))
    vid = await pool.fetchval("""
        INSERT INTO vehicles (name, plate, max_weight_kg, max_volume_m3, is_hired, driver_id)
        VALUES ($1,$2,$3,$4,$5,$6) RETURNING id""",
        v.name, v.plate, v.max_weight_kg, v.max_volume_m3, v.is_hired, driver_id)
    return {"vehicle_id": vid}


class VehiclePatch(BaseModel):
    name: str | None = None
    max_weight_kg: float | None = None
    max_volume_m3: float | None = None
    is_hired: bool | None = None


@app.patch("/api/vehicles/{vehicle_id}")
async def patch_vehicle(vehicle_id: int, v: VehiclePatch):
    cur = await pool.fetchrow("SELECT * FROM vehicles WHERE id=$1", vehicle_id)
    if not cur:
        raise HTTPException(404, "Не знайдено")
    await pool.execute("""
        UPDATE vehicles SET name=$1, max_weight_kg=$2, max_volume_m3=$3, is_hired=$4 WHERE id=$5""",
        v.name or cur["name"],
        v.max_weight_kg if v.max_weight_kg is not None else cur["max_weight_kg"],
        v.max_volume_m3 if v.max_volume_m3 is not None else cur["max_volume_m3"],
        v.is_hired if v.is_hired is not None else cur["is_hired"], vehicle_id)
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
            INSERT INTO client_service_stats VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,now())
            ON CONFLICT (client_key, addr_key) DO UPDATE SET visits=EXCLUDED.visits,
                address=EXCLUDED.address, lat=EXCLUDED.lat, lon=EXCLUDED.lon,
                median_min=EXCLUDED.median_min, p80_min=EXCLUDED.p80_min, updated_at=now()""",
            st_["client_key"], st_["addr_key"], st_["client_name"], st_["address"],
            st_["lat"], st_["lon"], st_["visits"], st_["median_min"], st_["p80_min"])
    return {"rows": len(stats)}


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
    rows = await pool.fetch(
        "SELECT id, address FROM orders WHERE project_id=$1 AND lat IS NULL AND address IS NOT NULL",
        project_id)
    ok, fail = 0, []
    for r in rows:
        res = await geocoder.geocode(r["address"])
        if res:
            await pool.execute("UPDATE orders SET lat=$1, lon=$2 WHERE id=$3", res[0], res[1], r["id"])
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
    await pool.execute("UPDATE orders SET address=$1, lat=$2, lon=$3 WHERE id=$4",
        b.address if b.address is not None else cur["address"],
        b.lat if b.lat is not None else cur["lat"],
        b.lon if b.lon is not None else cur["lon"], order_id)
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
    time_limit: int = 15,
):
    depart_m = parse_hhmm(depot_depart, 9 * 60)
    return_m = parse_hhmm(depot_return, 16 * 60)
    if return_m <= depart_m:
        raise HTTPException(400, "Час повернення має бути пізніше виїзду")

    depot = await pool.fetchrow("SELECT * FROM depots WHERE id=1")
    vrows = await pool.fetch("""
        SELECT v.*, COALESCE(d.shift_start,'08:00'::time) ss, COALESCE(d.shift_end,'18:00'::time) se
        FROM vehicles v LEFT JOIN drivers d ON d.id=v.driver_id WHERE v.is_active ORDER BY v.id""")
    if vehicle_ids:
        want = {int(x) for x in vehicle_ids.split(",")}
        vrows = [v for v in vrows if v["id"] in want]
    if not vrows:
        raise HTTPException(400, "Не обрано жодної машини")

    # простой: ручной для всех ИЛИ персональный из истории по клиент+адресу
    fallback = service_min or 15
    if service_source in ("hist_med", "hist_p80"):
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

    orows = await pool.fetch(
        "SELECT * FROM orders WHERE project_id=$1 AND lat IS NOT NULL AND lon IS NOT NULL ORDER BY id",
        project_id)
    if not orows:
        raise HTTPException(400, "Немає заявок з координатами на дату")

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
    ) for i, o in enumerate(orows)]

    # смена машины = пересечение смены водителя и окна склада (п.7)
    trucks = [solver.Truck(
        vehicle_id=v["id"],
        max_weight=float(v["max_weight_kg"]),
        max_volume=float(v["max_volume_m3"]),
        shift_start=max(t2m(v["ss"], 8 * 60), depart_m),
        shift_end=min(t2m(v["se"], 18 * 60), return_m),
    ) for v in vrows]

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

    routes_idx = solver.solve(stops, trucks, durations, time_limit, allowed)
    if routes_idx is None:
        raise HTTPException(422, "Рішення не знайдено — перевір вікна/ліміти")

    async with pool.acquire() as c:
        await c.execute("DELETE FROM routes WHERE project_id=$1 AND status='draft'", project_id)
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


@app.post("/api/routes")   # п.4: добавить машину в день вручную
async def create_route(body: NewRoute):
    veh = await pool.fetchrow("SELECT * FROM vehicles WHERE id=$1", body.vehicle_id)
    if not veh:
        raise HTTPException(404, "Машина не знайдена")
    used = await pool.fetch("SELECT color FROM routes WHERE project_id=$1", body.project_id)
    used_colors = {u["color"] for u in used}
    color = next((c for c in ROUTE_COLORS if c not in used_colors), ROUTE_COLORS[0])
    rid = await pool.fetchval("""
        INSERT INTO routes (plan_date, vehicle_id, driver_id, color, total_km,
            load_weight, load_volume, depart_time, project_id)
        VALUES ($1,$2,$3,$4,0,0,0,$5,$6) RETURNING id""",
        body.plan_date, veh["id"], veh["driver_id"], color,
        m2t(parse_hhmm(body.depot_depart, 9 * 60)), body.project_id)
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
        SELECT s.order_id, o.lat, o.lon, o.tw_from, o.service_min, o.weight_kg, o.volume_m3, o.kind
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
            SELECT s.seq, s.eta, s.etd, o.id order_id, o.client, o.kind, o.address,
                   o.lat, o.lon, o.tw_from, o.tw_to, o.weight_kg, o.volume_m3, o.service_min
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


app.mount("/", StaticFiles(directory="static", html=True), name="static")
