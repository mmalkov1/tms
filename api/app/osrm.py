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
