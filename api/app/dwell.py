"""Нормативы простоя из GPS-отчета стоянок. Ключ: клиент + адрес (у каждого
адреса своя рампа/очередь). Поддержка xlsx (основной) и legacy CSV."""
import csv
import io
import re
import statistics as st
from collections import defaultdict

NOISE_MIN, NOISE_MAX = 2, 180
MATCH_RADIUS_KM = 0.5    # привязка заявки к нормативу адреса
NIGHT_SHARE_BASE = 0.2   # >20% стоянок ночью (21-06) = база/парковка, не клиент


def normalize_client(name: str) -> str:
    n = name.split(" / ")[0]
    n = re.sub(r'["«»\']', "", n)
    n = re.sub(r"\s+", " ", n).strip().upper()
    return n


def normalize_addr(a: str) -> str:
    a = re.sub(r"\b(україна|украина)\b", "", a.lower())
    a = re.sub(r"[.,\s]+", " ", a).strip()
    return a


def _dwell_minutes(v) -> float | None:
    m = re.match(r"^(\d{1,3}):(\d{2}):(\d{2})", str(v).strip())
    if not m:
        return None
    return int(m.group(1)) * 60 + int(m.group(2)) + int(m.group(3)) / 60


def parse_report(raw: bytes) -> list[dict]:
    if raw[:4] == b"PK\x03\x04":            # xlsx
        import pandas as pd

        from .importer import fix_1c_xlsx
        df = pd.read_excel(io.BytesIO(fix_1c_xlsx(raw)))
        df.columns = [str(c).strip() for c in df.columns]
        out = []
        for _, r in df.iterrows():
            dw = _dwell_minutes(r.get("Час стоянки"))
            if dw is None:
                continue
            coords = str(r.get("Координати") or "")
            m = re.match(r"^\s*([\d.]+)\s*,\s*([\d.]+)", coords)
            full = str(r.get("Назва клієнта") or "")
            name, _, addr_in_name = full.partition(" / ")
            # приоритет: колонка "Адреса" (заполнена), иначе адрес из имени после "/"
            addr_col = r.get("Адреса")
            address = (str(addr_col).strip()
                       if addr_col is not None and str(addr_col) not in ("nan", "") and str(addr_col).strip()
                       else addr_in_name.strip())
            start = str(r.get("Початок стоянки") or "")
            hm = re.search(r"(\d{2}):\d{2}:\d{2}$", start.strip())
            out.append({
                "code": str(r.get("Код клієнта") or ""),
                "name": name.strip(),
                "address": address,
                "dwell": dw,
                "hour": int(hm.group(1)) if hm else 12,
                "lat": float(m.group(1)) if m else None,
                "lon": float(m.group(2)) if m else None,
            })
        return out
    # legacy CSV с рваными колонками — якоря по регэкспам
    text = raw.decode("utf-8-sig", errors="replace")
    out = []
    rdr = csv.reader(io.StringIO(text))
    next(rdr, None)
    for row in rdr:
        row = [x.strip() for x in row]
        durs = [x for x in row if re.fullmatch(r"\d{2}:\d{2}:\d{2}", x)]
        if not durs or len(row) < 8:
            continue
        dw = _dwell_minutes(durs[0])
        out.append({"code": row[2], "name": row[3].split(" / ")[0].strip(),
                    "address": "", "dwell": dw, "lat": None, "lon": None})
    return out


def aggregate(visits: list[dict], min_addr: int = 4, min_client: int = 5) -> list[dict]:
    """Строки нормативов: по адресам (addr_key=норм.адрес) + сводная по клиенту (addr_key='*')."""
    # фильтр баз: место с высокой долей ночных стоянок — парковка машины,
    # GPS-геозона ближайшего клиента дает ложные "визиты" (кейс школы, 479 ночевок)
    pre = defaultdict(list)
    for v in visits:
        pre[(normalize_client(v["name"]), normalize_addr(v["address"]) or "?")].append(v)
    bases = {k for k, vs in pre.items()
             if len(vs) >= 10 and sum(1 for v in vs if v.get("hour", 12) >= 21 or v.get("hour", 12) < 6) / len(vs) > NIGHT_SHARE_BASE}

    by_addr = defaultdict(list)
    by_client = defaultdict(list)
    meta = {}
    for v in visits:
        if "склад" in v["code"].lower() or "склад" in v["name"].lower():
            continue
        if v["dwell"] is None or not (NOISE_MIN <= v["dwell"] <= NOISE_MAX):
            continue
        ck = normalize_client(v["name"])
        ak = normalize_addr(v["address"]) or "?"
        if (ck, ak) in bases:
            continue
        by_addr[(ck, ak)].append(v)
        by_client[ck].append(v["dwell"])
        meta[ck] = v["name"]

    def stat_row(ck, ak, dws, name, addr, lat, lon):
        dws = sorted(dws)
        return {"client_key": ck, "addr_key": ak, "client_name": name, "address": addr,
                "lat": lat, "lon": lon, "visits": len(dws),
                "median_min": round(st.median(dws), 1),
                "p80_min": round(dws[int(len(dws) * 0.8)], 1)}

    rows = []
    for (ck, ak), vs in by_addr.items():
        if len(vs) < min_addr or ak == "?":
            continue
        lats = [v["lat"] for v in vs if v["lat"]]
        lons = [v["lon"] for v in vs if v["lon"]]
        rows.append(stat_row(ck, ak, [v["dwell"] for v in vs], meta[ck], vs[0]["address"],
                             sum(lats) / len(lats) if lats else None,
                             sum(lons) / len(lons) if lons else None))
    for ck, dws in by_client.items():
        if len(dws) >= min_client:
            rows.append(stat_row(ck, "*", dws, meta[ck], "", None, None))
    return sorted(rows, key=lambda x: -x["visits"])


def pick_service_min(stats_by_client: dict, client: str, lat, lon,
                     mode: str, fallback: int) -> int:
    """Норматив для заявки: ближайший адрес клиента в радиусе, иначе сводный, иначе fallback."""
    import math
    col = "median_min" if mode == "hist_med" else "p80_min"
    cand = stats_by_client.get(normalize_client(client or ""))
    if not cand:
        return fallback
    best, best_d = None, MATCH_RADIUS_KM
    star = None
    for r in cand:
        if r["addr_key"] == "*":
            star = r
            continue
        if lat is None or r["lat"] is None:
            continue
        d = math.hypot((float(r["lat"]) - lat) * 111, (float(r["lon"]) - lon) * 70)
        if d < best_d:
            best, best_d = r, d
    src = best or star
    return max(3, round(float(src[col]))) if src else fallback
