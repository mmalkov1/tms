"""v27: раздача APK застосунку водія та перевірка оновлень.

APK лежить у volume /app/apk (не в образі — щоб нова збірка застосунку
не вимагала перебілду API). Файли:
    /app/apk/tms-driver.apk   — сам застосунок
    /app/apk/version.json     — {"versionCode": N, "versionName": "...", "notes": "..."}

Ендпоінти:
    GET  /api/app/version   — маніфест (публічний, дергає застосунок)
    GET  /api/app/apk       — сам файл  (публічний, качає застосунок)
    POST /api/app/upload    — залив нової збірки (під SECURITY_KEY, дергає CI)
"""
import json
import os
import shutil

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

router = APIRouter(tags=["app-update"])

APK_DIR = os.getenv("APK_DIR", "/app/apk")
APK_PATH = os.path.join(APK_DIR, "tms-driver.apk")
VER_PATH = os.path.join(APK_DIR, "version.json")
UPLOAD_KEY = os.getenv("APK_UPLOAD_KEY", "")


def init():
    """Каталог APK (ідемпотентно). Викликається зі startup."""
    os.makedirs(APK_DIR, exist_ok=True)


@router.get("/api/app/version")
async def app_version():
    """Маніфест поточної збірки. Застосунок порівнює versionCode зі своїм."""
    if not os.path.exists(VER_PATH):
        return {"versionCode": 0, "versionName": "", "notes": "", "available": False}
    with open(VER_PATH, encoding="utf-8") as f:
        data = json.load(f)
    data["available"] = os.path.exists(APK_PATH)
    return data


@router.get("/api/app/apk")
async def app_apk():
    """Віддає APK. Публічно — водій качає без пароля (токен тут не потрібен)."""
    if not os.path.exists(APK_PATH):
        raise HTTPException(404, "APK ще не завантажено")
    return FileResponse(APK_PATH, media_type="application/vnd.android.package-archive",
                        filename="tms-driver.apk")


@router.post("/api/app/upload")
async def app_upload(
    key: str = Form(...),
    version_code: int = Form(...),
    version_name: str = Form(...),
    notes: str = Form(""),
    apk: UploadFile = File(...),
):
    """Залив нової збірки з CI. Захищено ключем APK_UPLOAD_KEY."""
    if not UPLOAD_KEY or key != UPLOAD_KEY:
        raise HTTPException(403, "Невірний ключ")
    init()
    tmp = APK_PATH + ".tmp"
    with open(tmp, "wb") as f:
        shutil.copyfileobj(apk.file, f)
    os.replace(tmp, APK_PATH)                      # атомарна заміна
    with open(VER_PATH, "w", encoding="utf-8") as f:
        json.dump({"versionCode": version_code, "versionName": version_name,
                   "notes": notes}, f, ensure_ascii=False)
    return {"ok": True, "versionCode": version_code,
            "size": os.path.getsize(APK_PATH)}
