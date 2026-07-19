"""Read-only travel-time diagnostic for one TMS driver/day.

Run inside the API container, where DATABASE_URL and OSRM_URL are available:
  docker compose exec -T -e DIAG_DATE=2026-07-16 \
    -e DIAG_DRIVER=Мартинов api python - < martinov_diagnostic.py
"""

import asyncio
import os
from datetime import date, datetime
from math import asin, cos, radians, sin, sqrt
from zoneinfo import ZoneInfo

import asyncpg
import httpx


KYIV = ZoneInfo("Europe/Kyiv")
DATE = date.fromisoformat(
    os.getenv("DIAG_DATE", datetime.now(KYIV).date().isoformat())
)
DRIVER = os.getenv("DIAG_DRIVER", "Мартинов")
DB_DSN = os.getenv("DATABASE_URL", "postgresql://tms:tms@db:5432/tms")
OSRM_URL = os.getenv("OSRM_URL", "http://osrm:5000").rstrip("/")


def local(ts):
    return ts.astimezone(KYIV) if ts else None


def hhmm(ts):
    return local(ts).strftime("%H:%M:%S") if ts else "—"


def hm_time(value):
    return str(value)[:5] if value else "—"


def minutes(seconds):
    return seconds / 60.0


def fmt_min(value, signed=False):
    if value is None:
        return "—"
    return f"{value:+.1f}" if signed else f"{value:.1f}"


def planned_dt(day, value):
    return datetime.combine(day, value, tzinfo=KYIV) if value else None


def diff_min(actual, planned):
    if not actual or not planned:
        return None
    return (local(actual) - planned).total_seconds() / 60.0


def haversine_km(lat1, lon1, lat2, lon2):
    p1, p2 = radians(lat1), radians(lat2)
    dp, dl = radians(lat2 - lat1), radians(lon2 - lon1)
    a = sin(dp / 2) ** 2 + cos(p1) * cos(p2) * sin(dl / 2) ** 2
    return 12742 * asin(sqrt(a))


def short(value, width=26):
    value = " ".join((value or "—").split())
    return value if len(value) <= width else value[: width - 1] + "…"


async def osrm_leg(client, a, b, cache):
    key = tuple(round(v, 6) for v in (*a, *b))
    if key in cache:
        return cache[key]
    coords = f"{a[1]},{a[0]};{b[1]},{b[0]}"
    response = await client.get(
        f"{OSRM_URL}/route/v1/driving/{coords}",
        params={"overview": "false", "steps": "false", "alternatives": "false"},
    )
    response.raise_for_status()
    route = response.json()["routes"][0]
    result = (float(route["duration"]), float(route["distance"]) / 1000.0)
    cache[key] = result
    return result


async def gps_stats(conn, driver_ids, ts_from, ts_to):
    total = max(0.0, (ts_to - ts_from).total_seconds())
    points = await conn.fetch(
        """
        SELECT ts, lat, lon, speed_kmh, accuracy_m
        FROM gps_points
        WHERE driver_id = ANY($1::int[])
          AND ts BETWEEN $2 AND $3
          AND (accuracy_m IS NULL OR accuracy_m <= 100)
        ORDER BY ts
        """,
        driver_ids,
        ts_from,
        ts_to,
    )
    observed = 0.0
    low_speed = 0.0
    gap = 0.0
    distance = 0.0
    for a, b in zip(points, points[1:]):
        dt = (b["ts"] - a["ts"]).total_seconds()
        if dt <= 0:
            continue
        if dt > 180:
            gap += dt
            continue
        observed += dt
        dist = haversine_km(a["lat"], a["lon"], b["lat"], b["lon"])
        distance += dist
        derived_speed = dist * 3600 / dt
        reported = [float(p["speed_kmh"]) for p in (a, b) if p["speed_kmh"] is not None]
        reported_speed = max(reported) if reported else derived_speed
        if derived_speed < 3 and reported_speed < 3:
            low_speed += dt
    coverage = observed / total if total else 0.0
    return {
        "points": len(points),
        "coverage": coverage,
        "low_min": minutes(low_speed),
        "gap_min": minutes(gap),
        "gps_km": distance,
    }


async def sequence_osrm(client, depot, stops, include_return, cache):
    points = [depot] + [(s["lat"], s["lon"]) for s in stops]
    if include_return:
        points.append(depot)
    seconds = 0.0
    km = 0.0
    for a, b in zip(points, points[1:]):
        leg_s, leg_km = await osrm_leg(client, a, b, cache)
        seconds += leg_s
        km += leg_km
    return minutes(seconds), km


async def diagnose_route(conn, client, route):
    route_id = route["id"]
    day = route["plan_date"]
    depot = (route["depot_lat"], route["depot_lon"])
    driver_ids = [x for x in {route["driver_id"], route["vehicle_driver_id"]} if x]
    events = {
        row["event"]: row["ts"]
        for row in await conn.fetch(
            "SELECT event, ts FROM route_events WHERE route_id=$1", route_id
        )
    }
    stops = list(
        await conn.fetch(
            """
            SELECT s.seq, s.eta, s.etd, o.id AS order_id, o.client, o.address,
                   o.lat, o.lon, o.service_min,
                   max(e.ts) FILTER (WHERE e.event='arrive') AS arrive_ts,
                   max(e.ts) FILTER (WHERE e.event='depart') AS depart_ts,
                   max(e.ts) FILTER (WHERE e.event='fail') AS fail_ts
            FROM route_stops s
            JOIN orders o ON o.id=s.order_id
            LEFT JOIN stop_events e ON e.route_id=s.route_id AND e.order_id=s.order_id
            WHERE s.route_id=$1
            GROUP BY s.seq, s.eta, s.etd, o.id, o.client, o.address,
                     o.lat, o.lon, o.service_min
            ORDER BY s.seq
            """,
            route_id,
        )
    )
    valid = [s for s in stops if s["lat"] is not None and s["lon"] is not None]
    actual = [s for s in valid if s["arrive_ts"] or s["fail_ts"]]
    actual.sort(key=lambda s: s["arrive_ts"] or s["fail_ts"])

    print()
    print("=" * 96)
    print(f"РЕЙС {route_id} · {route['vehicle_name']} · {route['driver_name']} · {day}")
    print(
        f"План: {hm_time(route['depart_time'])}–{hm_time(route['return_time'])} · "
        f"Факт: {hhmm(events.get('start'))}–{hhmm(events.get('finish'))} · "
        f"точек {len(actual)}/{len(stops)}"
    )
    start_delay = diff_min(events.get("start"), planned_dt(day, route["depart_time"]))
    finish_delay = diff_min(events.get("finish"), planned_dt(day, route["return_time"]))
    print(
        f"Отклонение старта: {fmt_min(start_delay, True)} мин · "
        f"финиша: {fmt_min(finish_delay, True)} мин"
    )

    cache = {}
    legs = []
    previous_name = "СКЛАД"
    previous_point = depot
    previous_leave = events.get("start")
    for fact_no, stop in enumerate(actual, 1):
        arrive = stop["arrive_ts"] or stop["fail_ts"]
        leave = stop["depart_ts"] or stop["fail_ts"]
        if previous_leave and arrive and arrive > previous_leave:
            osrm_sec, osrm_km = await osrm_leg(
                client, previous_point, (stop["lat"], stop["lon"]), cache
            )
            actual_min = minutes((arrive - previous_leave).total_seconds())
            gps = await gps_stats(conn, driver_ids, previous_leave, arrive)
            eta_delay = diff_min(arrive, planned_dt(day, stop["eta"]))
            legs.append(
                {
                    "fact_no": fact_no,
                    "plan_no": stop["seq"],
                    "from": previous_name,
                    "to": stop["client"],
                    "depart": hhmm(previous_leave),
                    "arrive": hhmm(arrive),
                    "actual": actual_min,
                    "osrm": minutes(osrm_sec),
                    "extra": actual_min - minutes(osrm_sec),
                    "km": osrm_km,
                    "factor": actual_min / minutes(osrm_sec) if osrm_sec else None,
                    "eta_delay": eta_delay,
                    **gps,
                }
            )
        previous_name = stop["client"]
        previous_point = (stop["lat"], stop["lon"])
        previous_leave = leave

    if events.get("finish") and previous_leave and events["finish"] > previous_leave:
        osrm_sec, osrm_km = await osrm_leg(client, previous_point, depot, cache)
        actual_min = minutes((events["finish"] - previous_leave).total_seconds())
        gps = await gps_stats(conn, driver_ids, previous_leave, events["finish"])
        legs.append(
            {
                "fact_no": len(actual) + 1,
                "plan_no": "D",
                "from": previous_name,
                "to": "СКЛАД",
                "depart": hhmm(previous_leave),
                "arrive": hhmm(events["finish"]),
                "actual": actual_min,
                "osrm": minutes(osrm_sec),
                "extra": actual_min - minutes(osrm_sec),
                "km": osrm_km,
                "factor": actual_min / minutes(osrm_sec) if osrm_sec else None,
                "eta_delay": finish_delay,
                **gps,
            }
        )

    print("\nПЕРЕЕЗДЫ ПО ФАКТИЧЕСКОЙ ПОСЛЕДОВАТЕЛЬНОСТИ")
    print(
        " факт/план | время             | откуда → куда"
        "                                  | факт | OSRM | +дорога | ≤3 | gap | коэф | Δ ETA"
    )
    print("-" * 150)
    for leg in legs:
        pair = f"{short(leg['from'], 20)} → {short(leg['to'], 25)}"
        factor_text = f"{leg['factor']:.2f}" if leg["factor"] is not None else "—"
        print(
            f" {str(leg['fact_no']):>4}/{str(leg['plan_no']):<4} | "
            f"{leg['depart']}–{leg['arrive']} | {pair:<48} | "
            f"{leg['actual']:>4.1f} | {leg['osrm']:>4.1f} | {leg['extra']:>+7.1f} | "
            f"{leg['low_min']:>3.1f} | {leg['gap_min']:>3.1f} | "
            f"{factor_text:>4} | {fmt_min(leg['eta_delay'], True):>6}"
        )

    service_rows = []
    for fact_no, stop in enumerate(actual, 1):
        arrive = stop["arrive_ts"]
        leave = stop["depart_ts"]
        if not arrive or not leave or leave < arrive:
            continue
        actual_service = minutes((leave - arrive).total_seconds())
        if stop["eta"] and stop["etd"]:
            planned_service = (
                datetime.combine(day, stop["etd"]) - datetime.combine(day, stop["eta"])
            ).total_seconds() / 60
        else:
            planned_service = float(stop["service_min"] or 0)
        service_rows.append((fact_no, stop, actual_service, planned_service))

    print("\nОБСЛУЖИВАНИЕ НА ТОЧКАХ")
    print(" факт/план | клиент                         | прибыл–уехал       | факт | план | разница")
    print("-" * 100)
    for fact_no, stop, actual_service, planned_service in service_rows:
        print(
            f" {fact_no:>4}/{stop['seq']:<4} | {short(stop['client'], 30):<30} | "
            f"{hhmm(stop['arrive_ts'])}–{hhmm(stop['depart_ts'])} | "
            f"{actual_service:>4.1f} | {planned_service:>4.1f} | "
            f"{actual_service - planned_service:>+7.1f}"
        )

    completed_ids = {s["order_id"] for s in actual}
    planned_subset = [s for s in valid if s["order_id"] in completed_ids]
    include_return = bool(events.get("finish") and len(completed_ids) == len(valid))
    actual_osrm_min, actual_osrm_km = await sequence_osrm(
        client, depot, actual, include_return, cache
    ) if actual else (0.0, 0.0)
    planned_osrm_min, planned_osrm_km = await sequence_osrm(
        client, depot, planned_subset, include_return, cache
    ) if planned_subset else (0.0, 0.0)

    total_actual = sum(x["actual"] for x in legs)
    total_osrm = sum(x["osrm"] for x in legs)
    total_low = sum(x["low_min"] for x in legs)
    total_gap = sum(x["gap_min"] for x in legs)
    actual_service = sum(x[2] for x in service_rows)
    planned_service = sum(x[3] for x in service_rows)
    last_delay = legs[-1]["eta_delay"] if legs else None

    print("\nИТОГ")
    print(f"  Переезды, факт:                         {total_actual:7.1f} мин")
    print(f"  Те же переезды по OSRM:                 {total_osrm:7.1f} мин")
    print(f"  Дорожная надбавка к OSRM:               {total_actual-total_osrm:+7.1f} мин")
    print(f"  Из неё подтверждено GPS со скоростью <3:{total_low:7.1f} мин")
    print(f"  Разрывы GPS >3 мин (не классифицированы):{total_gap:7.1f} мин")
    print(f"  Обслуживание факт / план:               {actual_service:5.1f} / {planned_service:.1f} мин")
    print(f"  Отклонение обслуживания:                {actual_service-planned_service:+7.1f} мин")
    print(f"  Последнее отклонение от исходной ETA:   {fmt_min(last_delay, True):>7} мин")
    print(
        f"  OSRM выполненного набора, факт. порядок:{actual_osrm_min:7.1f} мин / {actual_osrm_km:.1f} км"
    )
    print(
        f"  OSRM того же набора, плановый порядок:  {planned_osrm_min:7.1f} мин / {planned_osrm_km:.1f} км"
    )
    print(
        f"  Влияние перестановки по OSRM:           {actual_osrm_min-planned_osrm_min:+7.1f} мин / "
        f"{actual_osrm_km-planned_osrm_km:+.1f} км"
    )
    print("\nФактический порядок:")
    for fact_no, stop in enumerate(actual, 1):
        marker = "=" if fact_no == stop["seq"] else ("↑" if stop["seq"] > fact_no else "↓")
        print(
            f"  {fact_no:>2} <- план {stop['seq']:>2} {marker} · "
            f"{hhmm(stop['arrive_ts'] or stop['fail_ts'])} · {stop['client']}"
        )
    print("\nПримечание: «+дорога» = факт между кнопками − статический OSRM. Это смесь")
    print("пробок, светофоров, парковки/поиска въезда и остановок в пути, а не доказанная")
    print("пробка. Столбцы ≤3 и gap помогают отделить наблюдаемую стоянку и разрывы GPS.")


async def main():
    conn = await asyncpg.connect(DB_DSN)
    try:
        routes = await conn.fetch(
            """
            SELECT r.id, r.plan_date, r.depart_time, r.return_time, r.driver_id,
                   v.driver_id AS vehicle_driver_id, v.name AS vehicle_name,
                   d.name AS driver_name, dep.lat AS depot_lat, dep.lon AS depot_lon
            FROM routes r
            JOIN vehicles v ON v.id=r.vehicle_id
            LEFT JOIN drivers d ON d.id=COALESCE(r.driver_id, v.driver_id)
            JOIN depots dep ON dep.id=r.depot_id
            WHERE r.plan_date=$1::date AND d.name ILIKE '%' || $2 || '%'
            ORDER BY r.id
            """,
            DATE,
            DRIVER,
        )
        if not routes:
            raise SystemExit(f"Рейс не найден: дата={DATE}, водитель содержит {DRIVER!r}")
        async with httpx.AsyncClient(timeout=30) as client:
            for route in routes:
                await diagnose_route(conn, client, route)
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
