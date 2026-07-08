"""Импорт заявок из выгрузки 1С «Задание транспорта (пакетная печать)»."""
import io
import re
import zipfile
from datetime import time as time_cls

import pandas as pd

DEPOT_MARKER = "Молодогвардійська"


def fix_1c_xlsx(raw: bytes) -> bytes:
    """1С пишет xl/SharedStrings.xml с большой буквы — openpyxl падает."""
    zin = zipfile.ZipFile(io.BytesIO(raw))
    if "xl/SharedStrings.xml" not in zin.namelist():
        return raw
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.namelist():
            data = zin.read(item)
            name = "xl/sharedStrings.xml" if item == "xl/SharedStrings.xml" else item
            if item.endswith(".rels") or "sheet" in item or item.endswith("workbook.xml"):
                data = data.replace(b"SharedStrings.xml", b"sharedStrings.xml")
            zout.writestr(name, data)
    return out.getvalue()


def _coord(v):
    if pd.isna(v):
        return None
    try:
        return float(str(v).replace(",", "."))
    except ValueError:
        return None


def _time(v):
    """-> datetime.time (asyncpg требует объект, не строку)."""
    if pd.isna(v):
        return None
    m = re.match(r"^(\d{1,2}):(\d{2})", str(v).strip())
    return time_cls(int(m.group(1)), int(m.group(2))) if m else None


def _service_min(v) -> int:
    t = _time(v)
    if not t:
        return 15
    return (t.hour * 60 + t.minute) or 15


def parse_orders(raw: bytes, plan_date: str) -> list[dict]:
    df = pd.read_excel(io.BytesIO(fix_1c_xlsx(raw)), sheet_name=0)
    df.columns = [str(c).strip() for c in df.columns]
    rows = []
    for _, r in df.iterrows():
        kind_raw = str(r.get("Вид операции", "")).strip().lower()
        if "забор" in kind_raw:
            kind = "pickup"
            address = r.get("Точка отправления")
        elif "доставка" in kind_raw:
            kind = "delivery"
            address = r.get("Точка прибытия")
        else:
            continue

        ref = str(r.get("Ссылка", ""))
        m = re.search(r"([А-ЯA-Z]{2}\d{9})", ref)

        rows.append({
            "plan_date": plan_date,
            "doc_number": m.group(1) if m else None,
            "doc_ref": ref or None,
            "kind": kind,
            "client": str(r.get("Контрагент/Склад", "")).strip(),
            "address": None if pd.isna(address) else str(address).strip(),
            "address_extra": None if pd.isna(r.get("Дополнение к адресу")) else str(r.get("Дополнение к адресу")),
            "lat": _coord(r.get("Широта")),
            "lon": _coord(r.get("Долгота")),
            "tw_from": _time(r.get("с")),
            "tw_to": _time(r.get("по")),
            "service_min": _service_min(r.get("разгрузка")),
            "weight_kg": 0 if pd.isna(r.get("Вес")) else float(r.get("Вес")),
            "volume_m3": 0 if pd.isna(r.get("Объем")) else float(r.get("Объем")),
            "status_1c": None if pd.isna(r.get("Статус документа")) else str(r.get("Статус документа")),
        })
    return rows
