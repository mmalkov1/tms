"""v54: пілот транспортних листів для одного водія.

Пілот вмикається тільки для drivers.code_1c=FUEL_PILOT_DRIVER_CODE_1C.
Пробіг для списання пального — різниця одометра; GPS лише довідковий.
"""
import io
import os
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from pydantic import BaseModel

router = APIRouter(tags=["fuel"])
pool = None
KYIV_TZ = ZoneInfo("Europe/Kyiv")
PILOT_DRIVER_CODE = os.getenv("FUEL_PILOT_DRIVER_CODE_1C", "000000653")


async def init(db_pool):
    global pool
    pool = db_pool
    await pool.execute("""
        CREATE TABLE IF NOT EXISTS vehicle_fuel_settings (
            vehicle_id INT PRIMARY KEY REFERENCES vehicles(id) ON DELETE CASCADE,
            rate_l_per_100 NUMERIC(8,3), initial_balance_l NUMERIC(10,3),
            initial_balance_date DATE, updated_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
    await pool.execute("""
        CREATE TABLE IF NOT EXISTS transport_sheets (
            id BIGSERIAL PRIMARY KEY, work_date DATE NOT NULL,
            vehicle_id INT NOT NULL REFERENCES vehicles(id),
            driver_id INT NOT NULL REFERENCES drivers(id),
            odometer_start NUMERIC(12,1), odometer_end NUMERIC(12,1),
            opening_balance_l NUMERIC(10,3), fuel_used_l NUMERIC(10,3),
            closing_balance_l NUMERIC(10,3),
            status TEXT NOT NULL DEFAULT 'draft'
                CHECK (status IN ('draft','submitted','approved','revision')),
            revision_reason TEXT, submitted_at TIMESTAMPTZ, approved_at TIMESTAMPTZ,
            approved_by TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (work_date, vehicle_id))""")
    await pool.execute("CREATE INDEX IF NOT EXISTS idx_transport_sheets_date ON transport_sheets(work_date DESC)")
    await pool.execute("CREATE INDEX IF NOT EXISTS idx_transport_sheets_driver ON transport_sheets(driver_id, work_date DESC)")
    await pool.execute("""
        CREATE TABLE IF NOT EXISTS transport_sheet_refuels (
            id BIGSERIAL PRIMARY KEY, sheet_id BIGINT NOT NULL REFERENCES transport_sheets(id) ON DELETE CASCADE,
            liters NUMERIC(10,3) NOT NULL CHECK (liters > 0),
            refuel_at TIMESTAMPTZ NOT NULL DEFAULT now(), created_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
    await pool.execute("""
        CREATE TABLE IF NOT EXISTS transport_sheet_changes (
            id BIGSERIAL PRIMARY KEY, sheet_id BIGINT NOT NULL REFERENCES transport_sheets(id) ON DELETE CASCADE,
            actor_role TEXT NOT NULL CHECK (actor_role IN ('driver','logist','system')),
            actor_name TEXT, field_name TEXT NOT NULL, old_value TEXT, new_value TEXT,
            reason TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now())""")


def _num(value, field, allow_none=True):
    if value is None and allow_none:
        return None
    try:
        result = Decimal(str(value).replace(",", "."))
    except (InvalidOperation, ValueError):
        raise HTTPException(400, f"{field}: невірне число")
    if result < 0:
        raise HTTPException(400, f"{field}: значення не може бути від'ємним")
    return result


async def _driver(token):
    row = await pool.fetchrow("""
        SELECT d.id, d.name, d.code_1c FROM driver_tokens t
        JOIN drivers d ON d.id=t.driver_id
        WHERE t.token=$1 AND t.is_active AND d.is_active""", token)
    if not row:
        raise HTTPException(401, "Недійсний токен")
    return row


async def _vehicle_for(driver_id, day):
    row = await pool.fetchrow("""
        SELECT v.id, v.name, v.plate FROM routes r JOIN vehicles v ON v.id=r.vehicle_id
        WHERE r.plan_date=$2 AND (r.driver_id=$1 OR v.driver_id=$1)
        ORDER BY r.depart_time NULLS LAST, r.id LIMIT 1""", driver_id, day)
    if not row:
        row = await pool.fetchrow(
            "SELECT id,name,plate FROM vehicles WHERE driver_id=$1 AND is_active ORDER BY id LIMIT 1",
            driver_id)
    return row


async def _ensure_sheet(driver, day):
    vehicle = await _vehicle_for(driver["id"], day)
    if not vehicle:
        return None, None
    sheet = await pool.fetchrow(
        "SELECT * FROM transport_sheets WHERE work_date=$1 AND vehicle_id=$2", day, vehicle["id"])
    if sheet:
        return sheet, vehicle
    previous = await pool.fetchrow("""
        SELECT odometer_end, closing_balance_l FROM transport_sheets
        WHERE vehicle_id=$1 AND work_date<$2 AND odometer_end IS NOT NULL AND status='approved'
        ORDER BY work_date DESC LIMIT 1""", vehicle["id"], day)
    settings = await pool.fetchrow(
        "SELECT * FROM vehicle_fuel_settings WHERE vehicle_id=$1", vehicle["id"])
    opening = previous["closing_balance_l"] if previous and previous["closing_balance_l"] is not None else (
        settings["initial_balance_l"] if settings and settings["initial_balance_date"] and
        settings["initial_balance_date"] <= day else None)
    start = previous["odometer_end"] if previous else None
    sheet = await pool.fetchrow("""
        INSERT INTO transport_sheets (work_date,vehicle_id,driver_id,odometer_start,opening_balance_l)
        VALUES ($1,$2,$3,$4,$5)
        ON CONFLICT (work_date,vehicle_id) DO UPDATE SET driver_id=EXCLUDED.driver_id
        RETURNING *""", day, vehicle["id"], driver["id"], start, opening)
    return sheet, vehicle


async def _recalculate(sheet_id):
    row = await pool.fetchrow("""
        SELECT s.*, f.rate_l_per_100,
               COALESCE((SELECT sum(liters) FROM transport_sheet_refuels x WHERE x.sheet_id=s.id),0) refueled_l
        FROM transport_sheets s LEFT JOIN vehicle_fuel_settings f ON f.vehicle_id=s.vehicle_id
        WHERE s.id=$1""", sheet_id)
    km = None
    if row["odometer_start"] is not None and row["odometer_end"] is not None:
        km = row["odometer_end"] - row["odometer_start"]
        if km < 0:
            raise HTTPException(400, "Кінцевий одометр не може бути меншим за початковий")
    used = km * row["rate_l_per_100"] / 100 if km is not None and row["rate_l_per_100"] is not None else None
    closing = (row["opening_balance_l"] + row["refueled_l"] - used
               if row["opening_balance_l"] is not None and used is not None else None)
    return await pool.fetchrow("""
        UPDATE transport_sheets SET fuel_used_l=$1,closing_balance_l=$2,updated_at=now()
        WHERE id=$3 RETURNING *""", used, closing, sheet_id)


async def _payload(sheet, vehicle):
    sheet = await _recalculate(sheet["id"])
    cfg = await pool.fetchrow("SELECT * FROM vehicle_fuel_settings WHERE vehicle_id=$1", sheet["vehicle_id"])
    refs = await pool.fetch("SELECT id,liters,refuel_at FROM transport_sheet_refuels WHERE sheet_id=$1 ORDER BY refuel_at,id", sheet["id"])
    changes = await pool.fetch("""
        SELECT actor_role,actor_name,field_name,old_value,new_value,reason,created_at
        FROM transport_sheet_changes WHERE sheet_id=$1 ORDER BY created_at DESC LIMIT 30""", sheet["id"])
    km = (sheet["odometer_end"] - sheet["odometer_start"]
          if sheet["odometer_start"] is not None and sheet["odometer_end"] is not None else None)
    return {"id": sheet["id"], "date": sheet["work_date"].isoformat(),
            "vehicle_id": sheet["vehicle_id"], "vehicle": vehicle["name"], "plate": vehicle["plate"],
            "driver_id": sheet["driver_id"], "odometer_start": sheet["odometer_start"],
            "odometer_end": sheet["odometer_end"], "km": km,
            "opening_balance_l": sheet["opening_balance_l"], "refueled_l": sum(r["liters"] for r in refs),
            "fuel_used_l": sheet["fuel_used_l"], "closing_balance_l": sheet["closing_balance_l"],
            "rate_l_per_100": cfg["rate_l_per_100"] if cfg else None,
            "status": sheet["status"], "revision_reason": sheet["revision_reason"],
            "refuels": [dict(r) for r in refs], "changes": [dict(c) for c in changes]}


@router.get("/api/driver/{token}/transport-sheet")
async def driver_sheet(token: str, d: date | None = Query(None)):
    driver = await _driver(token)
    if (driver["code_1c"] or "").strip() != PILOT_DRIVER_CODE:
        return {"enabled": False}
    day = d or datetime.now(KYIV_TZ).date()
    sheet, vehicle = await _ensure_sheet(driver, day)
    if not sheet:
        return {"enabled": True, "sheet": None, "error": "За водієм не закріплено автомобіль"}
    return {"enabled": True, "sheet": await _payload(sheet, vehicle)}


class OdometerIn(BaseModel):
    odometer_start: str | float | None = None
    odometer_end: str | float | None = None
    reason: str | None = None


async def _pilot_sheet(token, day=None):
    driver = await _driver(token)
    if (driver["code_1c"] or "").strip() != PILOT_DRIVER_CODE:
        raise HTTPException(404, "Функція не активована")
    sheet, vehicle = await _ensure_sheet(driver, day or datetime.now(KYIV_TZ).date())
    if not sheet:
        raise HTTPException(400, "За водієм не закріплено автомобіль")
    return driver, sheet, vehicle


@router.post("/api/driver/{token}/transport-sheet/odometer")
async def save_odometer(token: str, body: OdometerIn):
    driver, sheet, vehicle = await _pilot_sheet(token)
    start, end = _num(body.odometer_start, "Початковий одометр"), _num(body.odometer_end, "Кінцевий одометр")
    if start is None and end is None:
        raise HTTPException(400, "Вкажіть показник")
    if sheet["status"] == "approved" and not (body.reason or "").strip():
        raise HTTPException(400, "Після підтвердження обов'язково вкажіть причину")
    new_start = start if start is not None else sheet["odometer_start"]
    new_end = end if end is not None else sheet["odometer_end"]
    if new_start is not None and new_end is not None and new_end < new_start:
        raise HTTPException(400, "Кінцевий одометр не може бути меншим за початковий")
    reason = (body.reason or "").strip() or None
    async with pool.acquire() as c:
        async with c.transaction():
            for field, old, new in (("odometer_start", sheet["odometer_start"], new_start),
                                    ("odometer_end", sheet["odometer_end"], new_end)):
                if new != old:
                    await c.execute("""INSERT INTO transport_sheet_changes
                        (sheet_id,actor_role,actor_name,field_name,old_value,new_value,reason)
                        VALUES ($1,'driver',$2,$3,$4,$5,$6)""",
                        sheet["id"], driver["name"], field,
                        str(old) if old is not None else None, str(new), reason)
            status = "revision" if sheet["status"] == "approved" else "draft"
            sheet = await c.fetchrow("""UPDATE transport_sheets SET odometer_start=$1,odometer_end=$2,
                status=$3,revision_reason=$4,approved_at=NULL,approved_by=NULL,updated_at=now()
                WHERE id=$5 RETURNING *""", new_start, new_end, status, reason, sheet["id"])
    return {"ok": True, "sheet": await _payload(sheet, vehicle)}


class RefuelIn(BaseModel):
    liters: str | float
    reason: str | None = None


@router.post("/api/driver/{token}/transport-sheet/refuels")
async def add_refuel(token: str, body: RefuelIn):
    driver, sheet, vehicle = await _pilot_sheet(token)
    liters = _num(body.liters, "Літри", False)
    if liters <= 0:
        raise HTTPException(400, "Кількість літрів має бути більшою за нуль")
    if sheet["status"] == "approved" and not (body.reason or "").strip():
        raise HTTPException(400, "Після підтвердження обов'язково вкажіть причину")
    reason = (body.reason or "").strip() or None
    async with pool.acquire() as c:
        async with c.transaction():
            ref_id = await c.fetchval(
                "INSERT INTO transport_sheet_refuels(sheet_id,liters) VALUES($1,$2) RETURNING id",
                sheet["id"], liters)
            await c.execute("""INSERT INTO transport_sheet_changes
                (sheet_id,actor_role,actor_name,field_name,new_value,reason)
                VALUES ($1,'driver',$2,'refuel',$3,$4)""", sheet["id"], driver["name"], str(liters), reason)
            if sheet["status"] == "approved":
                await c.execute("""UPDATE transport_sheets SET status='revision',revision_reason=$1,
                    approved_at=NULL,approved_by=NULL,updated_at=now() WHERE id=$2""", reason, sheet["id"])
    return {"ok": True, "refuel_id": ref_id,
            "sheet": await _payload(await pool.fetchrow("SELECT * FROM transport_sheets WHERE id=$1", sheet["id"]), vehicle)}


@router.post("/api/driver/{token}/transport-sheet/submit")
async def submit_sheet(token: str):
    driver, sheet, vehicle = await _pilot_sheet(token)
    if sheet["odometer_start"] is None or sheet["odometer_end"] is None:
        raise HTTPException(400, "Внесіть початковий і кінцевий одометр")
    cfg = await pool.fetchrow("SELECT * FROM vehicle_fuel_settings WHERE vehicle_id=$1", sheet["vehicle_id"])
    if not cfg or cfg["rate_l_per_100"] is None or sheet["opening_balance_l"] is None:
        raise HTTPException(400, "Логіст ще не налаштував норму та початковий залишок")
    await _recalculate(sheet["id"])
    sheet = await pool.fetchrow("""UPDATE transport_sheets SET status='submitted',submitted_at=now(),
        revision_reason=NULL,updated_at=now() WHERE id=$1 RETURNING *""", sheet["id"])
    return {"ok": True, "sheet": await _payload(sheet, vehicle)}


class SettingsIn(BaseModel):
    rate_l_per_100: str | float
    initial_balance_l: str | float
    initial_balance_date: date


@router.put("/api/transport-sheets/settings/{vehicle_id}")
async def save_settings(vehicle_id: int, body: SettingsIn):
    rate = _num(body.rate_l_per_100, "Норма", False)
    balance = _num(body.initial_balance_l, "Початковий залишок", False)
    if rate <= 0:
        raise HTTPException(400, "Норма має бути більшою за нуль")
    await pool.execute("""INSERT INTO vehicle_fuel_settings
        (vehicle_id,rate_l_per_100,initial_balance_l,initial_balance_date)
        VALUES ($1,$2,$3,$4) ON CONFLICT(vehicle_id) DO UPDATE SET
        rate_l_per_100=EXCLUDED.rate_l_per_100,initial_balance_l=EXCLUDED.initial_balance_l,
        initial_balance_date=EXCLUDED.initial_balance_date,updated_at=now()""",
        vehicle_id, rate, balance, body.initial_balance_date)
    await pool.execute("""UPDATE transport_sheets SET opening_balance_l=$1,updated_at=now()
        WHERE vehicle_id=$2 AND work_date=$3 AND status='draft'""", balance, vehicle_id, body.initial_balance_date)
    return {"ok": True}


def _sheet_dict(r):
    return {k: r[k] for k in r.keys()}


@router.get("/api/transport-sheets")
async def list_sheets(date_from: date, date_to: date, driver_ids: str | None = None):
    ids = [int(x) for x in (driver_ids or "").split(",") if x.strip().isdigit()]
    rows = await pool.fetch("""
        SELECT s.*,d.name driver_name,d.code_1c,v.name vehicle_name,v.plate,
               f.rate_l_per_100,COALESCE(sum(rf.liters),0) refueled_l
        FROM transport_sheets s JOIN drivers d ON d.id=s.driver_id
        JOIN vehicles v ON v.id=s.vehicle_id
        LEFT JOIN vehicle_fuel_settings f ON f.vehicle_id=s.vehicle_id
        LEFT JOIN transport_sheet_refuels rf ON rf.sheet_id=s.id
        WHERE s.work_date BETWEEN $1 AND $2 AND ($3::int[] IS NULL OR s.driver_id=ANY($3))
        GROUP BY s.id,d.name,d.code_1c,v.name,v.plate,f.rate_l_per_100
        ORDER BY s.work_date DESC,d.name""", date_from, date_to, ids or None)
    return [_sheet_dict(r) for r in rows]


@router.get("/api/transport-sheets/meta")
async def sheet_meta():
    rows = await pool.fetch("""
        SELECT d.id driver_id,d.name,d.code_1c,v.id vehicle_id,v.name vehicle_name,v.plate,
               f.rate_l_per_100,f.initial_balance_l,f.initial_balance_date
        FROM drivers d LEFT JOIN vehicles v ON v.driver_id=d.id AND v.is_active
        LEFT JOIN vehicle_fuel_settings f ON f.vehicle_id=v.id
        WHERE d.is_active AND d.code_1c=$1 ORDER BY v.id LIMIT 1""", PILOT_DRIVER_CODE)
    return {"pilot_code": PILOT_DRIVER_CODE, "drivers": [dict(r) for r in rows]}


class LogistSheetIn(BaseModel):
    odometer_start: str | float | None = None
    odometer_end: str | float | None = None
    opening_balance_l: str | float | None = None
    reason: str


@router.patch("/api/transport-sheets/{sheet_id}")
async def edit_sheet(sheet_id: int, body: LogistSheetIn):
    sheet = await pool.fetchrow("SELECT * FROM transport_sheets WHERE id=$1", sheet_id)
    if not sheet:
        raise HTTPException(404, "Лист не знайдено")
    reason = (body.reason or "").strip()
    if not reason:
        raise HTTPException(400, "Вкажіть причину зміни")
    vals = {"odometer_start": _num(body.odometer_start, "Початок"),
            "odometer_end": _num(body.odometer_end, "Кінець"),
            "opening_balance_l": _num(body.opening_balance_l, "Залишок")}
    if vals["odometer_start"] is not None and vals["odometer_end"] is not None and vals["odometer_end"] < vals["odometer_start"]:
        raise HTTPException(400, "Кінцевий одометр не може бути меншим")
    async with pool.acquire() as c:
        async with c.transaction():
            for field,new in vals.items():
                if new != sheet[field]:
                    await c.execute("""INSERT INTO transport_sheet_changes
                        (sheet_id,actor_role,actor_name,field_name,old_value,new_value,reason)
                        VALUES ($1,'logist','Логіст',$2,$3,$4,$5)""", sheet_id, field,
                        str(sheet[field]) if sheet[field] is not None else None,
                        str(new) if new is not None else None, reason)
            await c.execute("""UPDATE transport_sheets SET odometer_start=$1,odometer_end=$2,
                opening_balance_l=$3,status='submitted',updated_at=now() WHERE id=$4""",
                vals["odometer_start"], vals["odometer_end"], vals["opening_balance_l"], sheet_id)
    await _recalculate(sheet_id)
    return {"ok": True}


@router.post("/api/transport-sheets/{sheet_id}/approve")
async def approve_sheet(sheet_id: int):
    sheet = await pool.fetchrow("SELECT * FROM transport_sheets WHERE id=$1", sheet_id)
    if not sheet:
        raise HTTPException(404, "Лист не знайдено")
    if sheet["odometer_start"] is None or sheet["odometer_end"] is None:
        raise HTTPException(400, "Немає повних показників одометра")
    sheet = await _recalculate(sheet_id)
    if sheet["closing_balance_l"] is None:
        raise HTTPException(400, "Немає норми або початкового залишку")
    await pool.execute("""UPDATE transport_sheets SET status='approved',approved_at=now(),
        approved_by='Логіст',revision_reason=NULL,updated_at=now() WHERE id=$1""", sheet_id)
    return {"ok": True}


@router.get("/api/transport-sheets/notifications/count")
async def notification_count():
    count = await pool.fetchval("SELECT count(*) FROM transport_sheets WHERE status='revision'")
    return {"count": count}


@router.get("/api/transport-sheets/export.xlsx")
async def export_sheets(date_from: date, date_to: date, driver_ids: str | None = None,
                        approved_only: bool = True):
    ids = [int(x) for x in (driver_ids or "").split(",") if x.strip().isdigit()]
    rows = await pool.fetch("""
        SELECT s.*,d.name driver_name,v.name vehicle_name,v.plate,f.rate_l_per_100,
               COALESCE(sum(rf.liters),0) refueled_l
        FROM transport_sheets s JOIN drivers d ON d.id=s.driver_id
        JOIN vehicles v ON v.id=s.vehicle_id LEFT JOIN vehicle_fuel_settings f ON f.vehicle_id=s.vehicle_id
        LEFT JOIN transport_sheet_refuels rf ON rf.sheet_id=s.id
        WHERE s.work_date BETWEEN $1 AND $2
          AND ($3::int[] IS NULL OR s.driver_id=ANY($3))
          AND (NOT $4 OR s.status='approved')
        GROUP BY s.id,d.name,v.name,v.plate,f.rate_l_per_100
        ORDER BY v.name,s.work_date""", date_from, date_to, ids or None, approved_only)
    wb = Workbook(); ws = wb.active; ws.title = "Транспортні листи"
    headers = ["Дата","Автомобіль","Водій","Одометр початок","Одометр кінець","Пробіг, км",
               "Надходження, л","Норма, л/100 км","Витрата, л","Залишок початок, л",
               "Залишок кінець, л","Статус"]
    ws.append(headers)
    for cell in ws[1]:
        cell.font=Font(bold=True,color="FFFFFF");cell.fill=PatternFill("solid",fgColor="00356B");cell.alignment=Alignment(horizontal="center")
    for r in rows:
        km = r["odometer_end"]-r["odometer_start"] if r["odometer_start"] is not None and r["odometer_end"] is not None else None
        ws.append([r["work_date"],r["vehicle_name"],r["driver_name"],r["odometer_start"],r["odometer_end"],km,
                   r["refueled_l"],r["rate_l_per_100"],r["fuel_used_l"],r["opening_balance_l"],r["closing_balance_l"],r["status"]])
    for width,col in zip([13,24,22,18,18,14,17,18,14,21,20,16],ws.columns):
        ws.column_dimensions[col[0].column_letter].width=width
    ws.freeze_panes="A2";ws.auto_filter.ref=ws.dimensions
    buf=io.BytesIO();wb.save(buf);buf.seek(0)
    name=f"transport_sheets_{date_from}_{date_to}.xlsx"
    return StreamingResponse(buf,media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             headers={"Content-Disposition":f'attachment; filename="{name}"'})


@router.get("/api/transport-sheets/{sheet_id}")
async def sheet_detail(sheet_id: int):
    row = await pool.fetchrow("""
        SELECT s.*,d.name driver_name,v.name vehicle_name,v.plate,
               f.rate_l_per_100,COALESCE(sum(rf.liters),0) refueled_l
        FROM transport_sheets s JOIN drivers d ON d.id=s.driver_id
        JOIN vehicles v ON v.id=s.vehicle_id
        LEFT JOIN vehicle_fuel_settings f ON f.vehicle_id=s.vehicle_id
        LEFT JOIN transport_sheet_refuels rf ON rf.sheet_id=s.id
        WHERE s.id=$1 GROUP BY s.id,d.name,v.name,v.plate,f.rate_l_per_100""", sheet_id)
    if not row:
        raise HTTPException(404, "Лист не знайдено")
    changes = await pool.fetch("""SELECT actor_role,actor_name,field_name,old_value,new_value,reason,created_at
        FROM transport_sheet_changes WHERE sheet_id=$1 ORDER BY created_at DESC""", sheet_id)
    result = _sheet_dict(row)
    result["changes"] = [dict(c) for c in changes]
    return result
