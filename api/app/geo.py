"""Геозоны: валидация полигонов, WKT и point-in-polygon (ray casting)."""
import math
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


def normalize_polygon(points: list[list[float]]) -> list[list[float]]:
    """Проверить и нормализовать полигон для хранения как [[lat, lon], ...]."""
    if not isinstance(points, list) or len(points) > 2000:
        raise ValueError("Полігон має містити від 3 до 2000 вершин")

    normalized: list[list[float]] = []
    for point in points:
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            raise ValueError("Кожна вершина має містити широту і довготу")
        try:
            lat, lon = float(point[0]), float(point[1])
        except (TypeError, ValueError):
            raise ValueError("Координати вершин мають бути числами") from None
        if not math.isfinite(lat) or not math.isfinite(lon):
            raise ValueError("Координати вершин мають бути скінченними числами")
        if not -90 <= lat <= 90 or not -180 <= lon <= 180:
            raise ValueError("Координати вершини поза допустимим діапазоном")
        point_norm = [round(lat, 7), round(lon, 7)]
        if not normalized or point_norm != normalized[-1]:
            normalized.append(point_norm)

    # Leaflet сам замикає полігон, тому дубль першої вершини наприкінці не зберігаємо.
    if len(normalized) > 1 and normalized[0] == normalized[-1]:
        normalized.pop()
    if len(normalized) < 3 or len({tuple(p) for p in normalized}) < 3:
        raise ValueError("Для геозони потрібно щонайменше 3 різні вершини")

    # Нульова площа означає, що всі вершини фактично лежать на одній лінії.
    twice_area = 0.0
    for i, (lat, lon) in enumerate(normalized):
        next_lat, next_lon = normalized[(i + 1) % len(normalized)]
        twice_area += lon * next_lat - next_lon * lat
    if abs(twice_area) < 1e-10:
        raise ValueError("Геозона повинна мати ненульову площу")
    return normalized


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
