"""Клиент OSRM: матрица времени/расстояний + геометрия маршрута."""
import os

import httpx

OSRM_URL = os.getenv("OSRM_URL", "http://osrm:5000")


async def table(points: list[tuple[float, float]]) -> tuple[list[list[int]], list[list[float]]]:
    """points: [(lat, lon), ...] -> (durations сек, distances м)."""
    coords = ";".join(f"{lon},{lat}" for lat, lon in points)
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.get(
            f"{OSRM_URL}/table/v1/driving/{coords}",
            params={"annotations": "duration,distance"},
        )
        r.raise_for_status()
        j = r.json()
    dur = [[int(round(x or 0)) for x in row] for row in j["durations"]]
    dist = [[float(x or 0) for x in row] for row in j["distances"]]
    return dur, dist


async def route_with_legs(points: list[tuple[float, float]]) -> tuple[str, list[int], float]:
    """(polyline, длительности перегонов сек, всего км) через точки по порядку."""
    coords = ";".join(f"{lon},{lat}" for lat, lon in points)
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.get(
            f"{OSRM_URL}/route/v1/driving/{coords}",
            params={"overview": "full", "geometries": "polyline"},
        )
        r.raise_for_status()
        rt = r.json()["routes"][0]
    legs = [int(round(l["duration"])) for l in rt["legs"]]
    return rt["geometry"], legs, rt["distance"] / 1000


async def route_geometry(points: list[tuple[float, float]]) -> str:
    """Полилиния (encoded polyline) через точки в заданном порядке."""
    coords = ";".join(f"{lon},{lat}" for lat, lon in points)
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.get(
            f"{OSRM_URL}/route/v1/driving/{coords}",
            params={"overview": "full", "geometries": "polyline"},
        )
        r.raise_for_status()
        j = r.json()
    return j["routes"][0]["geometry"]


async def match_with_distance(
        points: list[tuple[float, float, int, int]],
) -> tuple[list[list[float]], float, float, list[list[list[float]]]] | None:
    """Map-matching GPS: геометрія, км, coverage і окремі сегменти.

    points: [(lat, lon, ts_unix, radius_m), ...] у хронологічному порядку.
    OSRM обмежує запит ~100 координатами — ріжемо на шматки по 95 з однією
    спільною точкою. Відстань беремо з matchings[].distance, а не вимірюємо
    ламану на екрані.
    """
    if len(points) < 2:
        return None
    out: list[list[float]] = []
    segments: list[list[list[float]]] = []
    distance_m = 0.0
    matched_points = 0
    input_points = 0
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            for i in range(0, len(points) - 1, 94):
                chunk = points[i:i + 95]
                if len(chunk) < 2:
                    break
                coords = ";".join(f"{lon},{lat}" for lat, lon, _, _ in chunk)
                r = await c.get(
                    f"{OSRM_URL}/match/v1/driving/{coords}",
                    params={
                        "overview": "full", "geometries": "geojson",
                        "tidy": "true", "gaps": "ignore",
                        "timestamps": ";".join(str(t) for _, _, t, _ in chunk),
                        "radiuses": ";".join(str(rd) for _, _, _, rd in chunk),
                    })
                if r.status_code != 200:
                    return None
                payload = r.json()
                tracepoints = payload.get("tracepoints") or []
                input_points += len(chunk)
                matched_points += sum(point is not None for point in tracepoints)
                for m in payload.get("matchings", []):
                    segment = [[lat, lon] for lon, lat in m["geometry"]["coordinates"]]
                    if len(segment) >= 2:
                        segments.append(segment)
                        out.extend(segment)
                    distance_m += float(m.get("distance") or 0)
    except Exception:
        return None
    coverage = matched_points / input_points if input_points else 0.0
    return (out, distance_m / 1000, coverage, segments) if len(out) >= 2 else None


async def match(points: list[tuple[float, float, int, int]]) -> list[list[float]] | None:
    """Сумісна обгортка для викликів, яким потрібна лише геометрія."""
    result = await match_with_distance(points)
    return result[0] if result else None


async def route_latlon(points: list[tuple[float, float]]) -> list[list[float]] | None:
    """v32: маршрут через точки у заданому порядку як [[lat, lon], ...] —
    без encoded polyline, щоб фронт малював без декодера."""
    if len(points) < 2:
        return None
    coords = ";".join(f"{lon},{lat}" for lat, lon in points)
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.get(
                f"{OSRM_URL}/route/v1/driving/{coords}",
                params={"overview": "full", "geometries": "geojson"})
            r.raise_for_status()
            g = r.json()["routes"][0]["geometry"]["coordinates"]
        return [[lat, lon] for lon, lat in g]
    except Exception:
        return None
