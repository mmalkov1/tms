# ТМС Культтовари — застосунок водія (Android)

WebView поверх `driver.html` + foreground-сервіс GPS (фаза 2).

## Як отримати APK
1. `git push` (workflow `Android APK` збирає автоматично при змінах в `android/**`)
   або GitHub → Actions → Android APK → Run workflow.
2. Actions → останній запуск → Artifacts → `tms-driver-apk` → app-debug.apk.
3. Скинути водію (Telegram/пошта), встановити з дозволом «невідомі джерела».

## Перший запуск
- Водій вставляє токен (або повне посилання від логіста) → «Увійти».
- Дозволи: геолокація («Під час використання» достатньо — сервіс foreground) + сповіщення.
- У шторці висить «Передача GPS у рейсі» — трек пишеться навіть зі згорнутим застосунком.

## Архітектура
- `MainActivity` — екран токена / WebView (UA-суфікс `TMSKultApp` вимикає web-GPS на сторінці).
- `LocationService` — FusedLocation 10–15 с, черга до 1000 точок у SharedPreferences,
  батч ≤500 кожні 20 с на `POST /api/driver/{token}/position` (той самий контракт, що web).
- Сервіс шле accuracy у Activity → `nativeGps(acc)` оновлює індикатор сигналу в шапці.

## Далі (не в цій версії)
- release-підпис (keystore) і власне оновлення APK
- ACCESS_BACKGROUND_LOCATION + автостарт після перезавантаження телефона
- кнопка «Стоп трек» у нотифікації
