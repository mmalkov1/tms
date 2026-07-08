"""Геокодинг: Visicom Data API (основной), Nominatim (fallback без ключа)."""
import asyncio
import os
import re

import httpx

NOMINATIM_URL = os.getenv("NOMINATIM_URL", "https://nominatim.openstreetmap.org")
VISICOM_KEY = os.getenv("VISICOM_API_KEY")
UA = "kultukr-tms/1.0"

STREET_PAT = re.compile(r"^(вул|ВУЛ|просп|бульв|пров|пл|шосе|наб|узвіз|туп)", re.I)


def normalize(addr: str) -> str:
    parts = [p.strip() for p in addr.split(",") if p.strip()]
    city = street = house = region = None
    for p in parts:
        low = p.lower()
        if re.fullmatch(r"\d{5}", p) or low in ("україна", "украина"):
            continue
        if low.startswith(("м.", "смт.", "с.", "смт ", "с ")):
            city = re.sub(r"^(м\.|смт\.?|с\.)\s*", "", p, flags=re.I)
        elif low.startswith("обл"):
            region = re.sub(r"^обл\.?\s*", "", p, flags=re.I) + " область"
        elif STREET_PAT.match(p):
            street = re.sub(r"^(вул|просп|бульв|пров|пл|шосе|наб)\.?\s*",
                            lambda m: m.group(1).lower() + ". ", p, flags=re.I)
        elif re.match(r"^(буд(инок)?\.?)\s*", low):
            house = re.sub(r"^буд(инок)?\.?\s*", "", p, flags=re.I)
        elif re.match(r"^\d+[\s\-а-яА-Яa-zA-Z]*$", p) and street and not house:
            house = p
        if low in ("київ", "киев"):
            city = "Київ"
    q = ", ".join(x for x in
                  [f"{street}, {house}" if street and house else street, city, region, "Україна"] if x)
    return q or addr


def candidates(addr: str) -> list[str]:
    """Сырой (Visicom его любит), нормализованный, перестановка слов улицы, без литеры."""
    base = normalize(addr)
    out = [addr, base]
    m = re.match(r"^(вул|просп|бульв|пров|пл|шосе|наб)\.\s+([^,]+),(.*)$", base)
    if m and len(m.group(2).split()) == 2:
        w = m.group(2).split()
        out.append(f"{m.group(1)}. {w[1]} {w[0]},{m.group(3)}")
    stripped = re.sub(r"(,\s*\d+)[\s\-]?[А-ЯІЇЄа-яіїєA-Za-z](,|$)", r"\1\2", base)
    if stripped != base:
        out.append(stripped)
    seen, uniq = set(), []
    for q in out:
        if q not in seen:
            seen.add(q)
            uniq.append(q)
    return uniq


async def _visicom(client: httpx.AsyncClient, q: str) -> tuple[float, float] | None:
    r = await client.get("https://api.visicom.ua/data-api/5.0/uk/geocode.json",
                         params={"text": q, "key": VISICOM_KEY, "limit": 1,
                                 "categories": "adr_address"})
    r.raise_for_status()
    j = r.json()
    feats = j.get("features") or ([j] if j.get("geo_centroid") else [])
    if not feats:  # повтор без фильтра категорий (улицы, POI)
        r = await client.get("https://api.visicom.ua/data-api/5.0/uk/geocode.json",
                             params={"text": q, "key": VISICOM_KEY, "limit": 1})
        r.raise_for_status()
        j = r.json()
        feats = j.get("features") or ([j] if j.get("geo_centroid") else [])
    if not feats:
        return None
    lon, lat = feats[0]["geo_centroid"]["coordinates"]
    return (lat, lon)


async def _nominatim(client: httpx.AsyncClient, q: str) -> tuple[float, float] | None:
    r = await client.get(f"{NOMINATIM_URL}/search",
                         params={"q": q, "format": "json", "limit": 1, "countrycodes": "ua"})
    r.raise_for_status()
    j = r.json()
    return (float(j[0]["lat"]), float(j[0]["lon"])) if j else None


async def geocode(addr: str) -> tuple[float, float] | None:
    fn = _visicom if VISICOM_KEY else _nominatim
    async with httpx.AsyncClient(timeout=15, headers={"User-Agent": UA}) as client:
        for q in candidates(addr):
            try:
                res = await fn(client, q)
                if res:
                    return res
            except httpx.HTTPError:
                pass
            if not VISICOM_KEY:
                await asyncio.sleep(1.1)
    return None
