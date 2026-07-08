"""Геозоны: парсинг WKT POLYGON и point-in-polygon (ray casting)."""
import re


def parse_wkt_polygon(wkt: str) -> list[list[float]]:
    """'POLYGON((lon lat, lon lat, ...))' -> [[lat, lon], ...]"""
    m = re.search(r"POLYGON\s*\(\((.+?)\)\)", wkt, re.S)
    if not m:
        raise ValueError("Не WKT POLYGON")
    pts = []
    for pair in m.group(1).split(","):
        lon, lat = pair.split()[:2]
        pts.append([float(lat), float(lon)])
    return pts


def point_in_polygon(lat: float, lon: float, poly: list[list[float]]) -> bool:
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        yi, xi = poly[i]
        yj, xj = poly[j]
        if ((xi > lon) != (xj > lon)) and (lat < (yj - yi) * (lon - xi) / (xj - xi + 1e-12) + yi):
            inside = not inside
        j = i
    return inside


def zone_of(lat: float, lon: float, zones: list[dict]) -> int | None:
    """id первой зоны, содержащей точку, или None."""
    for z in zones:
        if point_in_polygon(lat, lon, z["points"]):
            return z["id"]
    return None
