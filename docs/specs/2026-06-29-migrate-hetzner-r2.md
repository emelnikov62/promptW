# Миграция PromptW: Beget(РФ) → Hetzner(EU) + Cloudflare R2 — план

> **Для исполнителя:** выполнять по задачам по порядку. Шаги — конкретные команды с
> ожидаемым результатом. На каждом рубеже переключения есть **Откат**. Цель — near-zero
> downtime. Канонический гайд проекта — `bot-src/CLAUDE.md`.

**Goal:** перенести origin-сервер с Beget (РФ, `45.147.177.237`) на Hetzner (EU) и медиа с
Beget S3 на Cloudflare R2, сохранив Cloudflare перед доменом. Россияне продолжают заходить
без VPN (через CF-edge → EU-origin).

**Architecture:** lift-and-shift того же стека (Python 3.12 + aiohttp + aiogram, Postgres 16,
nginx, InsightFace). Cloudflare остаётся фронтом; переключение = смена A-записи origin в CF.
Медиа уезжает в R2, отдаётся через CF-проксированный сабдомен `cdn.promptw.ru`.

**Tech Stack:** Hetzner CX VPS (Ubuntu 24.04), PostgreSQL 16, Cloudflare R2 (S3-совместимый),
rclone, boto3, nginx, certbot/Cloudflare Origin Cert.

## Global Constraints
- Деплой остаётся git-flow: ветка → PR → `srv2.sh gitdeploy`. После миграции `srv2.sh` смотрит
  на Hetzner-IP.
- Секреты (S3/R2 ключи, DB-пароль, BOT_TOKEN) — только в `.env` на сервере, НИКОГДА в git/чат.
- Cloudflare остаётся включён (Full strict, ECH off, Skip-правила) — см. `docs/specs/cloudflare-access.md`.
- Платежи ЮKassa/Platega — это API; локация сервера на них не влияет, колбэки идут через CF.
- Объекты медиа immutable (uuid-имена), public-read — менять модель приватности не нужно.

---

## Task 1: Cloudflare R2 — бакет + публичный доступ через cdn.promptw.ru

**Files:** только Cloudflare dashboard (кода нет).

- [ ] **Шаг 1.** Cloudflare → R2 → Create bucket: имя `promptw-media`, локация `EU` (или Automatic).
- [ ] **Шаг 2.** Bucket → Settings → **Public access → Connect Custom Domain** → `cdn.promptw.ru`.
      CF сам создаст проксированную DNS-запись `cdn` и выдаст сертификат. (Это даёт публичные
      URL вида `https://cdn.promptw.ru/<key>` с edge-кэшем CF и нулевым egress.)
- [ ] **Шаг 3.** R2 → Manage API Tokens → Create token (Object Read & Write, scope: bucket
      `promptw-media`). Сохрани **Access Key ID**, **Secret**, и **S3 endpoint**
      (`https://<account_id>.r2.cloudflarestorage.com`). Эти значения пойдут в `.env`.
- [ ] **Шаг 4 (проверка).** Залей тестовый файл через dashboard, открой
      `https://cdn.promptw.ru/<key>` в браузере — должен отдаться (HTTP 200).

**Откат:** ничего не затронуто (старый Beget S3 продолжает работать).

---

## Task 2: storage.py — поддержка R2 + публичная база cdn.promptw.ru

R2 S3-совместим, `storage.py` уже умеет S3 через boto3 (path-style, `when_required` checksum —
это и для R2 правильно). Нужно: указать R2-эндпоинт/ключи через те же `S3_*` и отдавать
публичный URL через `cdn.promptw.ru`, а не через прямой S3-endpoint.

**Files:**
- Modify: `bot-src/storage.py` (функция формирования публичного URL + чтение env)

**Interfaces:**
- Produces: публичный URL объекта = `{S3_PUBLIC_BASE}/{key}` если задан `S3_PUBLIC_BASE`,
  иначе прежний `{endpoint}/{bucket}/{key}`.

- [ ] **Шаг 1.** В `storage.py` найди место сборки публичного URL (там, где сейчас
      `f"{endpoint}/{bucket}/{key}"`). Добавь чтение `S3_PUBLIC_BASE = os.getenv("S3_PUBLIC_BASE","").rstrip("/")`.
- [ ] **Шаг 2.** Изменить сборку URL:
  ```python
  def _public_url(key: str) -> str:
      base = os.getenv("S3_PUBLIC_BASE", "").rstrip("/")
      if base:
          return f"{base}/{key}"
      return f"{_endpoint().rstrip('/')}/{_bucket()}/{key}"
  ```
  и использовать `_public_url(key)` во всех местах, где формируется ссылка на залитый объект.
- [ ] **Шаг 3.** Убедиться, что `key_from_url()` и `is_remote()` распознают и новый
      `cdn.promptw.ru`, и старый Beget-URL (для media-proxy и SSRF-гарда на переходный период):
  ```python
  def is_remote(url: str) -> bool:
      return url.startswith("http://") or url.startswith("https://")
  # key_from_url: вытащить часть после '/media/' (или последний сегмент пути) для обоих хостов
  ```
  (Точную реализацию свери с текущей; цель — оба хоста дают валидный key.)
- [ ] **Шаг 4 (проверка локально/на стейдже).** `python -c "import ast; ast.parse(open('storage.py').read())"`
      → без ошибок. Ревью диффа.
- [ ] **Шаг 5. Commit.**
  ```bash
  git add bot-src/storage.py
  git commit -m "storage: R2 backend + S3_PUBLIC_BASE (cdn.promptw.ru)"
  ```

**Примечание:** медиа-proxy остаётся (iOS webview cross-origin фикс). `cdn.promptw.ru` —
другой сабдомен → cross-origin к `promptw.ru`, поэтому `mediaBlobUrl()` так и гонит его через
`/api/media-proxy` (который теперь тянет из R2 — без egress-платы, с CF-кэшем). Прямую отдачу с
cdn без прокси можно включить позже отдельной задачей.

---

## Task 3: Скопировать все объекты Beget S3 → R2 (rclone)

**Files:** только операционные команды (на любой машине с доступом в оба S3).

- [ ] **Шаг 1.** Установить rclone, настроить два remote (`rclone config`):
      `beget` (Beget S3: endpoint `s3.ru1.storage.beget.cloud`, ключи из текущего `.env`,
      provider Other, path-style) и `r2` (R2: endpoint из Task 1, ключи из Task 1, provider
      Cloudflare).
- [ ] **Шаг 2.** Первичная копия:
  ```bash
  rclone copy beget:7ffff2eb6e4b-elevated-yan r2:promptw-media --transfers=16 --progress
  ```
- [ ] **Шаг 3 (проверка).** Сверить число объектов:
  ```bash
  rclone size beget:7ffff2eb6e4b-elevated-yan
  rclone size r2:promptw-media
  ```
  Должны совпасть (или R2 ≥, если параллельно лились новые). Запомни — финальный `sync` будет
  в окне переключения (Task 8), чтобы догнать новые файлы.

**Откат:** не требуется (копирование не трогает источник).

---

## Task 4: Одноразовая миграция URL в БД (Beget-S3 → cdn.promptw.ru)

После переключения старые записи в БД указывают на Beget-S3. Переписать их на cdn-домен.
Делается миграцией с гардом `app_settings` (как существующие `_apply_*`), запускается на новом
сервере **после** старта с R2-конфигом.

**Files:**
- Modify: `bot-src/db/database.py` (новая `_apply_media_to_r2_v1`, регистрация в `init_db`)

- [ ] **Шаг 1.** Добавить функцию (по образцу `_apply_cateyes_blueeyes_v1`):
  ```python
  async def _apply_media_to_r2_v1():
      """One-time: rewrite stored media URLs from Beget S3 to the R2 cdn domain.
      Covers result_url, user_references.file_url, and generations.settings.references
      (JSONB). Guarded by app_settings."""
      old = "https://s3.ru1.storage.beget.cloud/7ffff2eb6e4b-elevated-yan"
      new = "https://cdn.promptw.ru"
      try:
          # SAFETY GATE: never rewrite until R2/cdn is the ACTIVE media backend.
          # Without this, deploying this code to the old Beget server would rewrite
          # every URL to cdn.promptw.ru (which wouldn't resolve yet) and break media.
          if not os.getenv("S3_PUBLIC_BASE", "").rstrip("/").endswith("cdn.promptw.ru"):
              return
          async with _pool.acquire() as conn:
              if await conn.fetchval("SELECT value FROM app_settings WHERE key='media_to_r2_v1'"):
                  return
              await conn.execute("UPDATE generations SET result_url=replace(result_url,$1,$2) WHERE result_url LIKE $3", old, new, old+'%')
              await conn.execute("UPDATE generations SET settings=replace(settings::text,$1,$2)::jsonb WHERE settings::text LIKE $3", old, new, '%'+old+'%')
              await conn.execute("UPDATE user_references SET file_url=replace(file_url,$1,$2) WHERE file_url LIKE $3", old, new, old+'%')
              await conn.execute("INSERT INTO app_settings (key,value,updated_at) VALUES ('media_to_r2_v1','1',NOW()) ON CONFLICT (key) DO UPDATE SET value='1', updated_at=NOW()")
          logger.info("Applied media_to_r2_v1")
      except Exception:
          logger.exception("media_to_r2_v1 failed")
  ```
  ⚠️ Свери имена таблиц/колонок с актуальной схемой (`generations.result_url`,
  `user_references.file_url`, `generations.settings`) перед запуском.
- [ ] **Шаг 2.** Зарегистрировать в `init_db()` после прочих `_apply_*`.
- [ ] **Шаг 3 (проверка).** `python -c "import ast; ast.parse(open('db/database.py').read())"`.
- [ ] **Шаг 4. Commit.**
  ```bash
  git add bot-src/db/database.py
  git commit -m "migration: rewrite media URLs Beget S3 -> R2 cdn (guarded)"
  ```
  (Применится автоматически при старте нового сервера; `cdn.promptw.ru` указывает на R2 с
  полной копией из Task 3, поэтому ссылки сразу валидны.)

---

## Task 5: Поднять Hetzner-сервер (зеркало стека)

**Files:** сервер (без кода).

- [ ] **Шаг 1.** Hetzner Cloud → Create server: Ubuntu 24.04, тип CX22+ (2vCPU/4GB+ — InsightFace
      требует RAM), локация EU (Nuremberg/Helsinki). Добавить SSH-ключ.
- [ ] **Шаг 2.** Базовая настройка: `apt update && apt install -y python3.12 python3.12-venv
      postgresql-16 nginx git rclone`. Завести пользователя/каталог `/opt/tg-image-ai-bot`.
- [ ] **Шаг 3.** Склонировать репо: `git clone https://github.com/emelnikov62/promptW.git /opt/tg-image-ai-bot`,
      создать venv, `pip install -r requirements.txt`.
- [ ] **Шаг 4.** Скопировать InsightFace модель в `FACE_MODEL_ROOT` (вне git; перенести с
      Beget через `scp`/`rclone`), и каталог `MEDIA_DIR` (transient — можно пустой).
- [ ] **Шаг 5.** Создать `.env` (скопировать с Beget; заменить S3-блок на R2: `S3_ENDPOINT`,
      `S3_ACCESS_KEY`, `S3_SECRET_KEY`, `S3_BUCKET=promptw-media`, добавить
      `S3_PUBLIC_BASE=https://cdn.promptw.ru`; `DATABASE_URL` пока на локальный Postgres Hetzner).
- [ ] **Шаг 6.** systemd-юнит `promptw.service` (скопировать с Beget, поправить пути).
- [ ] **Шаг 7 (проверка).** НЕ стартовать сервис с боевым вебхуком пока (чтобы не перехватить
      Telegram). Проверить импорт: `venv/bin/python -c "import main"` без ошибок.

**Откат:** удалить сервер Hetzner.

---

## Task 6: Перенести PostgreSQL (Beget → Hetzner)

**Files:** операционные команды.

- [ ] **Шаг 1.** Настроить локальный Postgres 16 на Hetzner: создать БД и пользователя как в
      текущем `DATABASE_URL`.
- [ ] **Шаг 2.** Пробный дамп (без окна простоя — для проверки совместимости):
  ```bash
  pg_dump "<BEGET_DATABASE_URL>" -Fc -f /tmp/promptw.dump
  pg_restore -d "<HETZNER_DATABASE_URL>" --no-owner --clean --if-exists /tmp/promptw.dump
  ```
- [ ] **Шаг 3 (проверка).** Сверить количество строк ключевых таблиц на обоих:
  ```sql
  SELECT (SELECT count(*) FROM users), (SELECT count(*) FROM generations), (SELECT count(*) FROM payments);
  ```
- [ ] **Шаг 4.** Стартануть сервис Hetzner с `AUTH_ENFORCE=1`, но **с временным
      `WEBHOOK_URL` пустым** (polling выключить тоже) ИЛИ вообще не подключать webhook — только
      убедиться, что приложение поднимается, читает БД, отвечает `/api/health` локально
      (`curl localhost:8081/api/health`). **Финальный** дамп будет в окне переключения (Task 8).

**Откат:** дропнуть БД на Hetzner.

---

## Task 7: SSL + nginx на Hetzner

**Files:** сервер.

- [ ] **Шаг 1.** Вариант проще под Cloudflare: **Cloudflare Origin Certificate** (SSL/TLS →
      Origin Server → Create) — положить cert/key на сервер, прописать в nginx; CF↔origin будет
      Full strict. (Альтернатива — certbot, но он требует, чтобы домен уже указывал на Hetzner.)
- [ ] **Шаг 2.** Скопировать nginx-конфиг с Beget (`/etc/nginx/sites-available/promptw`),
      путь к cert поправить, **сразу заложить real-IP за Cloudflare**:
  ```nginx
  proxy_set_header X-Real-IP        $http_cf_connecting_ip;
  proxy_set_header X-Forwarded-For  $http_cf_connecting_ip;
  ```
  и `TRUST_XFF=1` в `.env`.
- [ ] **Шаг 3 (проверка).** `nginx -t` → ok; локально
      `curl -k --resolve promptw.ru:443:127.0.0.1 https://promptw.ru/api/health` → `{"status":"ok"}`.

---

## Task 8: Переключение (cutover) — near-zero downtime

Короткое окно (минуты), в низкий трафик.

- [ ] **Шаг 1.** Объявить мини-окно. На **Beget** остановить сервис: `systemctl stop promptw`
      (чтобы не было новых записей в старую БД/старый S3).
- [ ] **Шаг 2.** Финальный догон медиа: `rclone sync beget:7ffff2eb6e4b-elevated-yan r2:promptw-media --transfers=16`.
- [ ] **Шаг 3.** Финальный дамп БД Beget → restore на Hetzner (как Task 6, шаг 2).
- [ ] **Шаг 4.** На **Hetzner** запустить сервис с боевым `.env` (R2 + webhook на
      `https://promptw.ru/webhook`): `systemctl start promptw`. Приложение при старте
      переустановит webhook (URL тот же) и прогонит `_apply_media_to_r2_v1`.
- [ ] **Шаг 5.** В **Cloudflare DNS** изменить A-запись `promptw.ru` с `45.147.177.237` на
      **IP Hetzner** (Proxied, оранжевое облако). TTL у CF低 — переключится почти сразу.
- [ ] **Шаг 6 (проверка).**
  ```bash
  curl -s https://promptw.ru/api/health         # {"status":"ok"} через CF→Hetzner
  curl -sI https://cdn.promptw.ru/<любой-key>    # 200 из R2
  ```
  Отправить боту `/start`, открыть Mini App, сделать тест-генерацию, открыть историю
  (медиа с cdn), проверить, что прошлые элементы истории грузятся (URL переписаны миграцией).
- [ ] **Шаг 7.** `getWebhookInfo` → без ошибок, `pending_update_count` падает.

**Откат (если что-то не так):** вернуть A-запись `promptw.ru` на `45.147.177.237`, на Beget
`systemctl start promptw`. Данные, записанные на Hetzner за время окна, при откате потеряются —
поэтому окно короткое и в низкий трафик. Медиа на R2 не мешает (Beget S3 на месте).

---

## Task 9: Обновить деплой и почистить

**Files:**
- Modify: `srv2.sh` (HOST/креды → Hetzner)

- [ ] **Шаг 1.** В `srv2.sh` заменить `HOST`/`PW`/hostkey на Hetzner (SSH-ключ вместо пароля —
      желательно). Проверить `bash srv2.sh ssh "systemctl is-active promptw"` → `active`.
- [ ] **Шаг 2.** Перенести почту домена: MX/TXT (`v=spf1 redirect=beget.com`) сейчас на Beget —
      если почта `@promptw.ru` нужна, решить отдельно (Beget-почта продолжит работать, пока
      MX указывают на Beget; сервер тут ни при чём). Зафиксировать решение.
- [ ] **Шаг 3.** Наблюдать 3–7 дней (логи, оплаты, генерации). После стабильности —
      декоммишн Beget (остановить VPS, удалить Beget S3 после бэкапа).

---

## Self-review (соответствие spec)
- Hetzner origin ✓ (Task 5–7), R2 медиа ✓ (Task 1–4), Cloudflare остаётся фронтом ✓ (Task 8 cutover).
- РФ без VPN ✓ (CF-edge → EU-origin, не трогаем CF).
- Платежи ✓ (API, не зависят от локации; колбэки через CF на новый origin).
- Near-zero downtime ✓ (Task 8 окно минут), откат на каждом рубеже ✓.
- Реальный IP за CF ✓ заложен сразу (Task 7).
- Секреты не в git ✓ (всё через `.env`).

## Открытые вопросы для владельца
1. Почта `@promptw.ru` (MX на Beget) — оставляем на Beget или тоже переносим (напр. на
   стороннего почтовика)? Влияет на Task 9 шаг 2.
2. Размер медиа в S3 (для оценки времени rclone и лимитов R2) — узнать `rclone size` (Task 3).
3. Окно переключения (Task 8) — выбрать время низкого трафика.
