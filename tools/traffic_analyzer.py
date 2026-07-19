"""TMS historical traffic analyzer (read-only for operational tables).

The script is designed to be piped into the already running API container:

    docker compose exec -T api python - daily --lookback 3 < tools/traffic_analyzer.py
    docker compose exec -T api python - backfill --from 2026-07-01 --to 2026-07-31 \
        < tools/traffic_analyzer.py
    docker compose exec -T api python - aggregate --days 30 < tools/traffic_analyzer.py

Operational tables are only read. The script creates and updates its own
traffic_analysis_runs, traffic_leg_facts and traffic_coefficients tables.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import os
import statistics
import sys
import traceback
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from math import asin, cos, radians, sin, sqrt
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import asyncpg
import httpx


ANALYZER_VERSION = "traffic-v1"
KYIV = ZoneInfo("Europe/Kyiv")
DB_DSN = os.getenv("DATABASE_URL", "postgresql://tms:tms@db:5432/tms")
OSRM_URL = os.getenv("OSRM_URL", "http://osrm:5000").rstrip("/")

STOP_RADIUS_M = int(os.getenv("TRAFFIC_STOP_RADIUS_M", "200"))
DEPOT_RADIUS_M = int(os.getenv("TRAFFIC_DEPOT_RADIUS_M", "300"))
MAX_ACCURACY_M = float(os.getenv("TRAFFIC_MAX_ACCURACY_M", "80"))
MIN_GEOFENCE_POINTS = int(os.getenv("TRAFFIC_MIN_GEOFENCE_POINTS", "2"))
MAX_VISIT_EVENT_DELTA_MIN = int(os.getenv("TRAFFIC_MAX_EVENT_DELTA_MIN", "45"))
MAX_GPS_SEGMENT_GAP_SEC = int(os.getenv("TRAFFIC_MAX_GPS_GAP_SEC", "180"))
MIN_GPS_COVERAGE = float(os.getenv("TRAFFIC_MIN_GPS_COVERAGE", "0.80"))
MIN_OSRM_SEC = int(os.getenv("TRAFFIC_MIN_OSRM_SEC", "120"))
MIN_ROUTE_STOPS = int(os.getenv("TRAFFIC_MIN_ROUTE_STOPS", "3"))


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS traffic_analysis_runs (
    id               BIGSERIAL PRIMARY KEY,
    analyzer_version TEXT NOT NULL,
    mode             TEXT NOT NULL,
    date_from        DATE NOT NULL,
    date_to          DATE NOT NULL,
    started_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at      TIMESTAMPTZ,
    status           TEXT NOT NULL DEFAULT 'running',
    routes_total     INT NOT NULL DEFAULT 0,
    routes_accepted  INT NOT NULL DEFAULT 0,
    legs_total       INT NOT NULL DEFAULT 0,
    legs_usable      INT NOT NULL DEFAULT 0,
    legs_suspect     INT NOT NULL DEFAULT 0,
    legs_rejected    INT NOT NULL DEFAULT 0,
    error_text       TEXT
);

CREATE TABLE IF NOT EXISTS traffic_leg_facts (
    id                BIGSERIAL PRIMARY KEY,
    analyzer_version  TEXT NOT NULL,
    plan_date         DATE NOT NULL,
    route_id          INT NOT NULL,
    driver_id         INT,
    driver_name       TEXT,
    vehicle_name      TEXT,
    from_key          TEXT NOT NULL,
    to_key            TEXT NOT NULL,
    from_seq          INT,
    to_seq            INT,
    from_name         TEXT,
    to_name           TEXT,
    departure_ts      TIMESTAMPTZ,
    arrival_ts        TIMESTAMPTZ,
    departure_lat     DOUBLE PRECISION,
    departure_lon     DOUBLE PRECISION,
    arrival_lat       DOUBLE PRECISION,
    arrival_lon       DOUBLE PRECISION,
    event_departure_ts TIMESTAMPTZ,
    event_arrival_ts  TIMESTAMPTZ,
    actual_sec        DOUBLE PRECISION,
    osrm_sec          DOUBLE PRECISION,
    osrm_distance_m   DOUBLE PRECISION,
    low_speed_sec     DOUBLE PRECISION,
    gps_gap_sec       DOUBLE PRECISION,
    gps_points        INT,
    gps_coverage      DOUBLE PRECISION,
    ratio             DOUBLE PRECISION,
    extra_sec         DOUBLE PRECISION,
    time_bucket       TEXT,
    duration_bucket   TEXT,
    weekday           SMALLINT,
    route_complete    BOOLEAN NOT NULL DEFAULT FALSE,
    quality           TEXT NOT NULL,
    reason            TEXT,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (analyzer_version, route_id, from_key, to_key)
);

ALTER TABLE traffic_leg_facts ADD COLUMN IF NOT EXISTS departure_lat DOUBLE PRECISION;
ALTER TABLE traffic_leg_facts ADD COLUMN IF NOT EXISTS departure_lon DOUBLE PRECISION;
ALTER TABLE traffic_leg_facts ADD COLUMN IF NOT EXISTS arrival_lat DOUBLE PRECISION;
ALTER TABLE traffic_leg_facts ADD COLUMN IF NOT EXISTS arrival_lon DOUBLE PRECISION;

CREATE INDEX IF NOT EXISTS idx_traffic_leg_date_quality
    ON traffic_leg_facts(plan_date, quality);
CREATE INDEX IF NOT EXISTS idx_traffic_leg_bucket
    ON traffic_leg_facts(analyzer_version, time_bucket, duration_bucket);

CREATE TABLE IF NOT EXISTS traffic_coefficients (
    analyzer_version TEXT NOT NULL,
    date_from        DATE NOT NULL,
    date_to          DATE NOT NULL,
    group_key        TEXT NOT NULL,
    time_bucket      TEXT,
    duration_bucket  TEXT,
    sample_count     INT NOT NULL,
    weighted_factor DOUBLE PRECISION,
    median_ratio     DOUBLE PRECISION,
    p75_ratio        DOUBLE PRECISION,
    median_extra_sec DOUBLE PRECISION,
    mean_bias_sec    DOUBLE PRECISION,
    mae_osrm_sec     DOUBLE PRECISION,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (analyzer_version, date_from, date_to, group_key)
);
"""


@dataclass
class Boundary:
    ts: datetime
    lat: float
    lon: float


@dataclass
class Visit:
    entry: Boundary | None
    exit: Boundary | None
    distance_to_event_sec: float | None
    points: int


def kyiv_today() -> date:
    return datetime.now(KYIV).date()


def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Некорректная дата {value!r}; нужен YYYY-MM-DD") from exc


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = radians(lat1), radians(lat2)
    dp, dl = radians(lat2 - lat1), radians(lon2 - lon1)
    a = sin(dp / 2) ** 2 + cos(p1) * cos(p2) * sin(dl / 2) ** 2
    return 12_742_000 * asin(sqrt(a))


def local(ts: datetime | None) -> datetime | None:
    return ts.astimezone(KYIV) if ts else None


def time_bucket(ts: datetime | None) -> str | None:
    if not ts:
        return None
    hour = local(ts).hour
    if 7 <= hour < 10:
        return "07-10"
    if 10 <= hour < 13:
        return "10-13"
    if 13 <= hour < 16:
        return "13-16"
    if 16 <= hour < 19:
        return "16-19"
    return "other"


def duration_bucket(osrm_sec: float | None) -> str | None:
    if osrm_sec is None:
        return None
    if osrm_sec < 5 * 60:
        return "00-05"
    if osrm_sec < 15 * 60:
        return "05-15"
    if osrm_sec < 30 * 60:
        return "15-30"
    return "30+"


def percentile(values: Iterable[float], q: float) -> float | None:
    ordered = sorted(values)
    if not ordered:
        return None
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * q
    lo, hi = int(pos), min(int(pos) + 1, len(ordered) - 1)
    weight = pos - lo
    return ordered[lo] * (1 - weight) + ordered[hi] * weight


def stable_runs(points: list[dict[str, Any]], lat: float, lon: float,
                radius_m: float) -> list[dict[str, Any]]:
    inside = [haversine_m(p["lat"], p["lon"], lat, lon) <= radius_m for p in points]
    runs: list[dict[str, Any]] = []
    i = 0
    while i < len(points):
        if not inside[i]:
            i += 1
            continue
        j = i
        while j + 1 < len(points) and inside[j + 1]:
            j += 1
        count = j - i + 1
        duration = (points[j]["ts"] - points[i]["ts"]).total_seconds()
        if count >= MIN_GEOFENCE_POINTS or duration >= 10:
            runs.append({
                "entry": Boundary(points[i]["ts"], points[i]["lat"], points[i]["lon"]),
                "last_inside_ts": points[j]["ts"],
                "exit": (Boundary(points[j + 1]["ts"], points[j + 1]["lat"],
                                  points[j + 1]["lon"])
                         if j + 1 < len(points) else None),
                "count": count,
            })
        i = j + 1
    return runs


def choose_visit(points: list[dict[str, Any]], lat: float | None, lon: float | None,
                 event_arrive: datetime | None, event_depart: datetime | None,
                 radius_m: float) -> Visit:
    if lat is None or lon is None or event_arrive is None:
        return Visit(None, None, None, 0)
    runs = stable_runs(points, lat, lon, radius_m)
    if not runs:
        return Visit(None, None, None, 0)
    limit = MAX_VISIT_EVENT_DELTA_MIN * 60

    def event_distance(run: dict[str, Any]) -> float:
        start, end = run["entry"].ts, run["last_inside_ts"]
        if start <= event_arrive <= end:
            return 0.0
        return min(abs((start - event_arrive).total_seconds()),
                   abs((end - event_arrive).total_seconds()))

    candidates = [(event_distance(run), run) for run in runs]
    candidates.sort(key=lambda item: item[0])
    delta, best = candidates[0]
    if delta > limit:
        return Visit(None, None, delta, 0)
    # If the driver pressed depart while still inside the geofence, the first
    # point outside remains the correct beginning of road movement.
    return Visit(best["entry"], best["exit"], delta, best["count"])


def choose_depot_exit(points: list[dict[str, Any]], depot_lat: float, depot_lon: float,
                      start_ts: datetime, first_arrival: datetime | None) -> Boundary | None:
    runs = stable_runs(points, depot_lat, depot_lon, DEPOT_RADIUS_M)
    upper = first_arrival or (start_ts + timedelta(hours=3))
    candidates = [run for run in runs
                  if run["entry"].ts <= upper and run["exit"]
                  and run["exit"].ts >= start_ts - timedelta(minutes=30)]
    if not candidates:
        return None
    return min(candidates, key=lambda run: abs((run["exit"].ts - start_ts).total_seconds()))[
        "exit"
    ]


def choose_depot_entry(points: list[dict[str, Any]], depot_lat: float, depot_lon: float,
                       last_depart: datetime | None, finish_ts: datetime) -> Boundary | None:
    if not last_depart:
        return None
    runs = stable_runs(points, depot_lat, depot_lon, DEPOT_RADIUS_M)
    candidates = [run for run in runs
                  if run["entry"].ts >= last_depart
                  and run["entry"].ts <= finish_ts + timedelta(minutes=30)]
    return min((run["entry"] for run in candidates), key=lambda boundary: boundary.ts,
               default=None)


def gps_stats(points: list[dict[str, Any]], ts_from: datetime,
              ts_to: datetime) -> dict[str, float | int]:
    selected = [p for p in points if ts_from <= p["ts"] <= ts_to]
    total = max(0.0, (ts_to - ts_from).total_seconds())
    observed = low_speed = gap = 0.0
    for a, b in zip(selected, selected[1:]):
        dt = (b["ts"] - a["ts"]).total_seconds()
        if dt <= 0:
            continue
        if dt > MAX_GPS_SEGMENT_GAP_SEC:
            gap += dt
            continue
        observed += dt
        dist_km = haversine_m(a["lat"], a["lon"], b["lat"], b["lon"]) / 1000
        derived_speed = dist_km * 3600 / dt
        reported = [float(p["speed_kmh"]) for p in (a, b)
                    if p.get("speed_kmh") is not None]
        reported_speed = max(reported) if reported else derived_speed
        if derived_speed < 3 and reported_speed < 3:
            low_speed += dt
    return {
        "gps_points": len(selected),
        "gps_coverage": observed / total if total else 0.0,
        "low_speed_sec": low_speed,
        "gps_gap_sec": gap,
    }


async def osrm_leg(client: httpx.AsyncClient, a: tuple[float, float],
                   b: tuple[float, float], cache: dict) -> tuple[float, float]:
    key = tuple(round(v, 6) for v in (*a, *b))
    if key in cache:
        return cache[key]
    coords = f"{a[1]},{a[0]};{b[1]},{b[0]}"
    response = await client.get(
        f"{OSRM_URL}/route/v1/driving/{coords}",
        params={"overview": "false", "steps": "false", "alternatives": "false"},
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("code") != "Ok" or not payload.get("routes"):
        raise RuntimeError(f"OSRM: {payload.get('code') or 'no route'}")
    route = payload["routes"][0]
    result = float(route["duration"]), float(route["distance"])
    cache[key] = result
    return result


async def ensure_schema(conn: asyncpg.Connection) -> None:
    await conn.execute(SCHEMA_SQL)


async def fetch_routes(conn: asyncpg.Connection, day: date) -> list[asyncpg.Record]:
    return list(await conn.fetch(
        """
        SELECT r.id, r.plan_date, r.driver_id,
               v.driver_id AS vehicle_driver_id, v.name AS vehicle_name,
               d.id AS resolved_driver_id, d.name AS driver_name,
               dep.lat AS depot_lat, dep.lon AS depot_lon,
               (SELECT ts FROM route_events
                WHERE route_id=r.id AND event='start' LIMIT 1) AS start_ts,
               (SELECT ts FROM route_events
                WHERE route_id=r.id AND event='finish' LIMIT 1) AS finish_ts,
               (SELECT count(*) FROM route_stops WHERE route_id=r.id) AS planned_stops
        FROM routes r
        JOIN vehicles v ON v.id=r.vehicle_id
        LEFT JOIN drivers d ON d.id=COALESCE(r.driver_id, v.driver_id)
        JOIN depots dep ON dep.id=r.depot_id
        WHERE r.plan_date=$1
          AND EXISTS (SELECT 1 FROM route_events
                      WHERE route_id=r.id AND event='start')
          AND EXISTS (SELECT 1 FROM route_events
                      WHERE route_id=r.id AND event='finish')
        ORDER BY r.id
        """, day))


async def fetch_stops(conn: asyncpg.Connection, route_id: int) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        """
        SELECT s.seq, o.id AS order_id, o.client, o.lat, o.lon,
               max(e.ts) FILTER (WHERE e.event='arrive') AS arrive_ts,
               max(e.ts) FILTER (WHERE e.event='depart') AS depart_ts,
               max(e.ts) FILTER (WHERE e.event='fail') AS fail_ts
        FROM route_stops s
        JOIN orders o ON o.id=s.order_id
        LEFT JOIN stop_events e ON e.route_id=s.route_id AND e.order_id=s.order_id
        WHERE s.route_id=$1
        GROUP BY s.seq, o.id, o.client, o.lat, o.lon
        ORDER BY s.seq
        """, route_id)
    out = []
    for row in rows:
        item = dict(row)
        item["event_arrive"] = row["arrive_ts"] or row["fail_ts"]
        item["event_depart"] = row["depart_ts"] or row["fail_ts"]
        out.append(item)
    return out


async def fetch_gps(conn: asyncpg.Connection, driver_ids: list[int],
                    start_ts: datetime, finish_ts: datetime) -> list[dict[str, Any]]:
    if not driver_ids:
        return []
    rows = await conn.fetch(
        """
        SELECT ts, lat, lon, speed_kmh, accuracy_m
        FROM gps_points
        WHERE driver_id = ANY($1::int[])
          AND ts BETWEEN $2 AND $3
          AND (accuracy_m IS NULL OR accuracy_m <= $4)
        ORDER BY ts
        """, driver_ids, start_ts - timedelta(minutes=30),
        finish_ts + timedelta(minutes=30), MAX_ACCURACY_M)
    return [dict(row) for row in rows]


def quality_for_leg(*, route_complete: bool, departure_ts: datetime | None,
                     arrival_ts: datetime | None, osrm_sec: float | None,
                     stats: dict[str, Any] | None, event_overlap: bool) -> tuple[str, str | None]:
    rejected: list[str] = []
    suspect: list[str] = []
    if not route_complete:
        rejected.append("incomplete_route")
    if departure_ts is None:
        rejected.append("missing_geofence_exit")
    if arrival_ts is None:
        rejected.append("missing_geofence_entry")
    if departure_ts and arrival_ts and arrival_ts <= departure_ts:
        rejected.append("nonpositive_travel_time")
    if event_overlap:
        rejected.append("event_overlap")
    if osrm_sec is None:
        rejected.append("osrm_error")
    elif osrm_sec < MIN_OSRM_SEC:
        suspect.append("short_osrm_leg")
    if stats is not None:
        if stats["gps_points"] < 2:
            rejected.append("insufficient_gps")
        if stats["gps_coverage"] < MIN_GPS_COVERAGE:
            rejected.append("low_gps_coverage")
        if stats["gps_gap_sec"] > MAX_GPS_SEGMENT_GAP_SEC:
            rejected.append("gps_gap")
    if departure_ts and arrival_ts and osrm_sec and arrival_ts > departure_ts:
        ratio = (arrival_ts - departure_ts).total_seconds() / osrm_sec
        if ratio < 0.5:
            suspect.append("ratio_below_0.5")
        elif ratio > 3.0:
            suspect.append("ratio_above_3")
    reasons = sorted(set(rejected + suspect))
    if rejected:
        return "rejected", ";".join(reasons)
    if suspect:
        return "suspect", ";".join(reasons)
    return "usable", None


async def analyze_route(conn: asyncpg.Connection, client: httpx.AsyncClient,
                        route: asyncpg.Record, cache: dict) -> list[dict[str, Any]]:
    stops = await fetch_stops(conn, route["id"])
    actual = [s for s in stops if s["event_arrive"]]
    actual.sort(key=lambda s: (s["event_arrive"], s["seq"]))
    route_complete = bool(
        route["start_ts"] and route["finish_ts"]
        and int(route["planned_stops"] or 0) >= MIN_ROUTE_STOPS
        and len(actual) == int(route["planned_stops"] or 0)
        and all(s["event_depart"] for s in actual)
    )
    driver_ids = sorted({x for x in (route["driver_id"], route["vehicle_driver_id"],
                                     route["resolved_driver_id"]) if x})
    points = await fetch_gps(conn, driver_ids, route["start_ts"], route["finish_ts"])
    for stop in actual:
        stop["visit"] = choose_visit(
            points, stop["lat"], stop["lon"], stop["event_arrive"],
            stop["event_depart"], STOP_RADIUS_M)

    first_arrive = actual[0]["event_arrive"] if actual else None
    depot_exit = choose_depot_exit(
        points, route["depot_lat"], route["depot_lon"], route["start_ts"], first_arrive)
    last_depart = actual[-1]["event_depart"] if actual else None
    depot_entry = choose_depot_entry(
        points, route["depot_lat"], route["depot_lon"], last_depart, route["finish_ts"])

    nodes: list[dict[str, Any]] = [{
        "key": "depot", "seq": None, "name": "СКЛАД",
        "lat": route["depot_lat"], "lon": route["depot_lon"],
        "entry": None, "exit": depot_exit,
        "event_arrive": None, "event_depart": route["start_ts"],
    }]
    for stop in actual:
        visit: Visit = stop["visit"]
        nodes.append({
            "key": f"order:{stop['order_id']}", "seq": stop["seq"],
            "name": stop["client"], "lat": stop["lat"], "lon": stop["lon"],
            "entry": visit.entry, "exit": visit.exit,
            "event_arrive": stop["event_arrive"], "event_depart": stop["event_depart"],
        })
    nodes.append({
        "key": "depot", "seq": None, "name": "СКЛАД",
        "lat": route["depot_lat"], "lon": route["depot_lon"],
        "entry": depot_entry, "exit": None,
        "event_arrive": route["finish_ts"], "event_depart": None,
    })

    out: list[dict[str, Any]] = []
    for prev, nxt in zip(nodes, nodes[1:]):
        departure_boundary: Boundary | None = prev["exit"]
        arrival_boundary: Boundary | None = nxt["entry"]
        departure_ts = departure_boundary.ts if departure_boundary else None
        arrival_ts = arrival_boundary.ts if arrival_boundary else None
        osrm_sec = osrm_distance = None
        osrm_from = ((departure_boundary.lat, departure_boundary.lon)
                     if departure_boundary else (prev["lat"], prev["lon"]))
        osrm_to = ((arrival_boundary.lat, arrival_boundary.lon)
                   if arrival_boundary else (nxt["lat"], nxt["lon"]))
        if None not in (*osrm_from, *osrm_to):
            try:
                osrm_sec, osrm_distance = await osrm_leg(
                    client, osrm_from, osrm_to, cache)
            except Exception:
                pass
        stats = None
        actual_sec = ratio = extra_sec = None
        if departure_ts and arrival_ts and arrival_ts > departure_ts:
            actual_sec = (arrival_ts - departure_ts).total_seconds()
            stats = gps_stats(points, departure_ts, arrival_ts)
            if osrm_sec and osrm_sec > 0:
                ratio = actual_sec / osrm_sec
                extra_sec = actual_sec - osrm_sec
        event_overlap = bool(
            prev["event_depart"] and nxt["event_arrive"]
            and prev["event_depart"] > nxt["event_arrive"])
        quality, reason = quality_for_leg(
            route_complete=route_complete, departure_ts=departure_ts,
            arrival_ts=arrival_ts, osrm_sec=osrm_sec, stats=stats,
            event_overlap=event_overlap)
        out.append({
            "analyzer_version": ANALYZER_VERSION,
            "plan_date": route["plan_date"], "route_id": route["id"],
            "driver_id": route["resolved_driver_id"],
            "driver_name": route["driver_name"], "vehicle_name": route["vehicle_name"],
            "from_key": prev["key"], "to_key": nxt["key"],
            "from_seq": prev["seq"], "to_seq": nxt["seq"],
            "from_name": prev["name"], "to_name": nxt["name"],
            "departure_ts": departure_ts, "arrival_ts": arrival_ts,
            "departure_lat": departure_boundary.lat if departure_boundary else None,
            "departure_lon": departure_boundary.lon if departure_boundary else None,
            "arrival_lat": arrival_boundary.lat if arrival_boundary else None,
            "arrival_lon": arrival_boundary.lon if arrival_boundary else None,
            "event_departure_ts": prev["event_depart"],
            "event_arrival_ts": nxt["event_arrive"],
            "actual_sec": actual_sec, "osrm_sec": osrm_sec,
            "osrm_distance_m": osrm_distance,
            "low_speed_sec": stats["low_speed_sec"] if stats else None,
            "gps_gap_sec": stats["gps_gap_sec"] if stats else None,
            "gps_points": stats["gps_points"] if stats else 0,
            "gps_coverage": stats["gps_coverage"] if stats else 0,
            "ratio": ratio, "extra_sec": extra_sec,
            "time_bucket": time_bucket(departure_ts),
            "duration_bucket": duration_bucket(osrm_sec),
            "weekday": route["plan_date"].weekday(),
            "route_complete": route_complete,
            "quality": quality, "reason": reason,
        })
    return out


INSERT_LEG_SQL = """
INSERT INTO traffic_leg_facts (
    analyzer_version, plan_date, route_id, driver_id, driver_name, vehicle_name,
    from_key, to_key, from_seq, to_seq, from_name, to_name,
    departure_ts, arrival_ts, departure_lat, departure_lon, arrival_lat, arrival_lon,
    event_departure_ts, event_arrival_ts,
    actual_sec, osrm_sec, osrm_distance_m, low_speed_sec, gps_gap_sec,
    gps_points, gps_coverage, ratio, extra_sec, time_bucket, duration_bucket,
    weekday, route_complete, quality, reason, updated_at)
VALUES (
    $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,
    $21,$22,$23,$24,$25,$26,$27,$28,$29,$30,$31,$32,$33,$34,$35,now())
ON CONFLICT (analyzer_version, route_id, from_key, to_key) DO UPDATE SET
    plan_date=EXCLUDED.plan_date, driver_id=EXCLUDED.driver_id,
    driver_name=EXCLUDED.driver_name, vehicle_name=EXCLUDED.vehicle_name,
    from_seq=EXCLUDED.from_seq, to_seq=EXCLUDED.to_seq,
    from_name=EXCLUDED.from_name, to_name=EXCLUDED.to_name,
    departure_ts=EXCLUDED.departure_ts, arrival_ts=EXCLUDED.arrival_ts,
    departure_lat=EXCLUDED.departure_lat, departure_lon=EXCLUDED.departure_lon,
    arrival_lat=EXCLUDED.arrival_lat, arrival_lon=EXCLUDED.arrival_lon,
    event_departure_ts=EXCLUDED.event_departure_ts,
    event_arrival_ts=EXCLUDED.event_arrival_ts,
    actual_sec=EXCLUDED.actual_sec, osrm_sec=EXCLUDED.osrm_sec,
    osrm_distance_m=EXCLUDED.osrm_distance_m,
    low_speed_sec=EXCLUDED.low_speed_sec, gps_gap_sec=EXCLUDED.gps_gap_sec,
    gps_points=EXCLUDED.gps_points, gps_coverage=EXCLUDED.gps_coverage,
    ratio=EXCLUDED.ratio, extra_sec=EXCLUDED.extra_sec,
    time_bucket=EXCLUDED.time_bucket, duration_bucket=EXCLUDED.duration_bucket,
    weekday=EXCLUDED.weekday, route_complete=EXCLUDED.route_complete,
    quality=EXCLUDED.quality, reason=EXCLUDED.reason, updated_at=now()
"""


def leg_values(row: dict[str, Any]) -> tuple:
    keys = (
        "analyzer_version", "plan_date", "route_id", "driver_id", "driver_name",
        "vehicle_name", "from_key", "to_key", "from_seq", "to_seq", "from_name",
        "to_name", "departure_ts", "arrival_ts", "departure_lat", "departure_lon",
        "arrival_lat", "arrival_lon", "event_departure_ts", "event_arrival_ts",
        "actual_sec", "osrm_sec", "osrm_distance_m",
        "low_speed_sec", "gps_gap_sec", "gps_points", "gps_coverage", "ratio",
        "extra_sec", "time_bucket", "duration_bucket", "weekday", "route_complete",
        "quality", "reason",
    )
    return tuple(row.get(key) for key in keys)


async def analyze_range(conn: asyncpg.Connection, date_from: date, date_to: date,
                        mode: str, quiet: bool = False) -> dict[str, int]:
    run_id = await conn.fetchval(
        """INSERT INTO traffic_analysis_runs
           (analyzer_version, mode, date_from, date_to)
           VALUES ($1,$2,$3,$4) RETURNING id""",
        ANALYZER_VERSION, mode, date_from, date_to)
    counters = defaultdict(int)
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            cache: dict = {}
            day = date_from
            while day <= date_to:
                routes = await fetch_routes(conn, day)
                counters["routes_total"] += len(routes)
                day_rows: list[dict[str, Any]] = []
                for route in routes:
                    rows = await analyze_route(conn, client, route, cache)
                    day_rows.extend(rows)
                    if any(row["quality"] == "usable" for row in rows):
                        counters["routes_accepted"] += 1
                async with conn.transaction():
                    await conn.execute(
                        "DELETE FROM traffic_leg_facts WHERE analyzer_version=$1 AND plan_date=$2",
                        ANALYZER_VERSION, day)
                    if day_rows:
                        await conn.executemany(INSERT_LEG_SQL,
                                               [leg_values(row) for row in day_rows])
                counters["legs_total"] += len(day_rows)
                for row in day_rows:
                    counters[f"legs_{row['quality']}"] += 1
                if not quiet:
                    print(
                        f"{day}: маршрутов {len(routes)}, перегонов {len(day_rows)}, "
                        f"usable={sum(r['quality']=='usable' for r in day_rows)}, "
                        f"suspect={sum(r['quality']=='suspect' for r in day_rows)}, "
                        f"rejected={sum(r['quality']=='rejected' for r in day_rows)}",
                        flush=True)
                day += timedelta(days=1)
        await conn.execute(
            """UPDATE traffic_analysis_runs SET finished_at=now(), status='success',
               routes_total=$2, routes_accepted=$3, legs_total=$4,
               legs_usable=$5, legs_suspect=$6, legs_rejected=$7 WHERE id=$1""",
            run_id, counters["routes_total"], counters["routes_accepted"],
            counters["legs_total"], counters["legs_usable"],
            counters["legs_suspect"], counters["legs_rejected"])
        return dict(counters)
    except Exception:
        error = traceback.format_exc()[-8000:]
        await conn.execute(
            """UPDATE traffic_analysis_runs SET finished_at=now(), status='failed',
               error_text=$2 WHERE id=$1""", run_id, error)
        raise


def summarize_group(rows: list[asyncpg.Record]) -> dict[str, Any]:
    ratios = [float(row["actual_sec"]) / float(row["osrm_sec"]) for row in rows]
    extras = [float(row["actual_sec"]) - float(row["osrm_sec"]) for row in rows]
    actual_sum = sum(float(row["actual_sec"]) for row in rows)
    osrm_sum = sum(float(row["osrm_sec"]) for row in rows)
    return {
        "sample_count": len(rows),
        "weighted_factor": actual_sum / osrm_sum if osrm_sum else None,
        "median_ratio": statistics.median(ratios),
        "p75_ratio": percentile(ratios, 0.75),
        "median_extra_sec": statistics.median(extras),
        "mean_bias_sec": statistics.mean(extras),
        "mae_osrm_sec": statistics.mean(abs(x) for x in extras),
    }


async def aggregate(conn: asyncpg.Connection, date_from: date, date_to: date,
                    output_format: str) -> None:
    rows = list(await conn.fetch(
        """
        SELECT actual_sec, osrm_sec, time_bucket, duration_bucket, weekday
        FROM traffic_leg_facts
        WHERE analyzer_version=$1 AND plan_date BETWEEN $2 AND $3
          AND quality='usable' AND actual_sec IS NOT NULL AND osrm_sec > 0
        ORDER BY plan_date, route_id, departure_ts
        """, ANALYZER_VERSION, date_from, date_to))
    groups: dict[str, tuple[str | None, str | None, list[asyncpg.Record]]] = {
        "overall": (None, None, rows)
    }
    by_time: dict[str, list] = defaultdict(list)
    by_day_type: dict[str, list] = defaultdict(list)
    by_day_time: dict[tuple[str, str], list] = defaultdict(list)
    by_time_duration: dict[tuple[str, str], list] = defaultdict(list)
    for row in rows:
        tb = row["time_bucket"] or "unknown"
        day_type = "weekday" if int(row["weekday"]) < 5 else "weekend"
        by_time[tb].append(row)
        by_day_type[day_type].append(row)
        by_day_time[(day_type, tb)].append(row)
        by_time_duration[(tb,
                          row["duration_bucket"] or "unknown")].append(row)
    for day_type, bucket_rows in sorted(by_day_type.items()):
        groups[f"day:{day_type}"] = (None, None, bucket_rows)
    for (day_type, bucket), bucket_rows in sorted(by_day_time.items()):
        groups[f"day:{day_type}|time:{bucket}"] = (bucket, None, bucket_rows)
    for bucket, bucket_rows in sorted(by_time.items()):
        groups[f"time:{bucket}"] = (bucket, None, bucket_rows)
    for (tb, db), bucket_rows in sorted(by_time_duration.items()):
        groups[f"time:{tb}|duration:{db}"] = (tb, db, bucket_rows)

    results = []
    for key, (tb, db, group_rows) in groups.items():
        if not group_rows:
            continue
        results.append({"group_key": key, "time_bucket": tb,
                        "duration_bucket": db, **summarize_group(group_rows)})
    async with conn.transaction():
        await conn.execute(
            """DELETE FROM traffic_coefficients
               WHERE analyzer_version=$1 AND date_from=$2 AND date_to=$3""",
            ANALYZER_VERSION, date_from, date_to)
        if results:
            await conn.executemany(
                """
                INSERT INTO traffic_coefficients (
                    analyzer_version,date_from,date_to,group_key,time_bucket,
                    duration_bucket,sample_count,weighted_factor,median_ratio,
                    p75_ratio,median_extra_sec,mean_bias_sec,mae_osrm_sec)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
                """, [(
                    ANALYZER_VERSION, date_from, date_to, row["group_key"],
                    row["time_bucket"], row["duration_bucket"], row["sample_count"],
                    row["weighted_factor"], row["median_ratio"], row["p75_ratio"],
                    row["median_extra_sec"], row["mean_bias_sec"], row["mae_osrm_sec"]
                ) for row in results])

    columns = ["group_key", "time_bucket", "duration_bucket", "sample_count",
               "weighted_factor", "median_ratio", "p75_ratio",
               "median_extra_min", "mean_bias_min", "mae_osrm_min"]
    if output_format == "csv":
        writer = csv.writer(sys.stdout)
        writer.writerow(columns)
        for row in results:
            writer.writerow([
                row["group_key"], row["time_bucket"] or "",
                row["duration_bucket"] or "", row["sample_count"],
                f"{row['weighted_factor']:.4f}", f"{row['median_ratio']:.4f}",
                f"{row['p75_ratio']:.4f}", f"{row['median_extra_sec']/60:.2f}",
                f"{row['mean_bias_sec']/60:.2f}", f"{row['mae_osrm_sec']/60:.2f}",
            ])
        return
    print(f"Период: {date_from} — {date_to}; качественных перегонов: {len(rows)}")
    print("группа                              n   weighted median  p75   +median  bias   MAE")
    print("-" * 90)
    for row in results:
        print(
            f"{row['group_key']:<35} {row['sample_count']:>4} "
            f"{row['weighted_factor']:>8.3f} {row['median_ratio']:>6.3f} "
            f"{row['p75_ratio']:>5.3f} {row['median_extra_sec']/60:>8.1f} "
            f"{row['mean_bias_sec']/60:>6.1f} {row['mae_osrm_sec']/60:>6.1f}"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Накопительный анализ фактического времени движения TMS")
    sub = parser.add_subparsers(dest="command", required=True)
    daily = sub.add_parser("daily", help="обработать последние завершённые дни")
    daily.add_argument("--lookback", type=int, default=3,
                       help="число завершённых дней для повторной обработки (по умолчанию 3)")
    backfill = sub.add_parser("backfill", help="обработать указанный диапазон")
    backfill.add_argument("--from", dest="date_from", type=parse_date, required=True)
    backfill.add_argument("--to", dest="date_to", type=parse_date, required=True)
    agg = sub.add_parser("aggregate", help="рассчитать коэффициенты по накопленным данным")
    agg.add_argument("--days", type=int, default=30)
    agg.add_argument("--end", type=parse_date, default=None,
                     help="последний день периода; по умолчанию вчера")
    agg.add_argument("--format", choices=("table", "csv"), default="table")
    return parser


async def async_main(args: argparse.Namespace) -> None:
    conn = await asyncpg.connect(DB_DSN)
    try:
        await ensure_schema(conn)
        if args.command == "daily":
            if args.lookback < 1:
                raise SystemExit("--lookback должен быть не меньше 1")
            date_to = kyiv_today() - timedelta(days=1)
            date_from = date_to - timedelta(days=args.lookback - 1)
            counters = await analyze_range(conn, date_from, date_to, "daily")
            print("Итого:", ", ".join(f"{k}={v}" for k, v in sorted(counters.items())))
        elif args.command == "backfill":
            if args.date_from > args.date_to:
                raise SystemExit("--from не может быть позже --to")
            counters = await analyze_range(conn, args.date_from, args.date_to, "backfill")
            print("Итого:", ", ".join(f"{k}={v}" for k, v in sorted(counters.items())))
        else:
            date_to = args.end or (kyiv_today() - timedelta(days=1))
            date_from = date_to - timedelta(days=args.days - 1)
            await aggregate(conn, date_from, date_to, args.format)
    finally:
        await conn.close()


def main() -> None:
    args = build_parser().parse_args()
    asyncio.run(async_main(args))


if __name__ == "__main__":
    main()
