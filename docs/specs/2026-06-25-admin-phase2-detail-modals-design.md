# Админка — Фаза 2: detail-модалки (спека)

**Дата:** 2026-06-25 · **Статус:** design approved → implementation

## Цель

Добавить две detail-модалки в админку (модалка Аудита с before/after уже сделана в Фазе 1,
`admin.js:1340`):

1. **Генерации** — просмотр полной генерации (промпт, настройки, превью медиа, face-метрики)
   + действие **возврат токенов** юзеру.
2. **Промокоды** — аналитика использования (сводка, график активаций по дням, список активаций).

Вне scope (YAGNI): «Повтор» генерации (перезапуск KIE — отложено), «Дублировать» промокод,
рефактор существующих модалок на общий примитив.

## Подход

Следуем существующей практике `openModal(html)` + ручная привязка кнопок после открытия
(как в `openPayment`/`loadUserDetail`/Аудит). Единственный новый фронт-хелпер — `mediaPreview()`
для рендера результата (img / батч-грид / video / audio). График активаций переиспользует
готовый `lineChart(points)` из дашборда. Никаких новых примитивов в admin-kit.

Роли: генерации видит и owner, и agent (read = `_require_admin`); **возврат — owner-only**
(`_require_role "owner"`). Промокоды — целиком owner-only (как и список промокодов).

## Backend

### Миграция (`db/database.py`, блок ALTER TABLE generations)

```sql
ALTER TABLE generations ADD COLUMN IF NOT EXISTS refunded_at TIMESTAMPTZ;
ALTER TABLE generations ADD COLUMN IF NOT EXISTS refunded_by BIGINT;
```

Зеркало `payments.refunded_at`. `refunded_at IS NOT NULL` = генерация возвращена (идемпотентность).

### `GET /api/admin/generations/{gen_id}` — `_require_admin`

Возвращает `{generation, user}`. JSONB-поля (`settings`, `result_urls`, `face_scores`)
парсятся через `json.loads` (asyncpg отдаёт их строками — паттерн проекта).

```python
gen = await pool.fetchrow("""
    SELECT id, user_tg_id, gen_type, model, prompt, settings, result_url, result_urls,
           status, cost, created_at, face_score, face_attempts, face_accepted,
           face_ref_found, face_scores, face_threshold, refunded_at, refunded_by
    FROM generations WHERE id=$1""", gen_id)
# 404 если не найдено
user = await pool.fetchrow(
    "SELECT tg_id, username, first_name, balance FROM users WHERE tg_id=$1", gen["user_tg_id"])
```

### `POST /api/admin/generations/{gen_id}/refund` — `_require_role "owner"`

Тело `{reason}`. Логика (атомарно, зеркало payment refund):

1. `SELECT id, user_tg_id, cost, status, refunded_at FROM generations WHERE id=$1` → 404 если нет.
2. Если `refunded_at` → **409** `already refunded` (идемпотентность).
3. Если `cost <= 0` → **400** `nothing to refund`.
4. В транзакции:
   - `UPDATE users SET balance = balance + $cost, updated_at=NOW() WHERE tg_id=$buyer`
   - `INSERT transactions(user_tg_id, amount=+cost, tx_type='refund', description='refund: generation {id}')`
     — описание с префиксом `refund` даёт юзеру лейбл `txRefund` (`app.js:txLabel`).
   - `UPDATE generations SET refunded_at=NOW(), refunded_by=$admin WHERE id=$id`
5. `_audit(admin, "generation_refund", "generation", id, {before}, {after, tokens_refunded}, reason, ip)`.
6. `{ok: True, refunded: cost}`.

Реферальные комиссии НЕ трогаем (генерация — это трата, не покупка; реф-комиссии завязаны на
платежи, не на генерации).

### `GET /api/admin/promos/{promo_id}` — `_require_role "owner"`

Возвращает `{promo, stats, activations, daily}`:

```python
promo = SELECT * FROM promo_codes WHERE id=$1            # 404 если нет
stats = SELECT COUNT(*) total, COALESCE(SUM(tokens_given),0) tokens,
               MIN(created_at) first_at, MAX(created_at) last_at
        FROM promo_activations WHERE promo_id=$1
activations = SELECT a.user_tg_id, u.username, a.tokens_given, a.created_at
              FROM promo_activations a LEFT JOIN users u ON a.user_tg_id=u.tg_id
              WHERE a.promo_id=$1 ORDER BY a.created_at DESC LIMIT 50
daily = SELECT created_at::date d, COUNT(*) v FROM promo_activations
        WHERE promo_id=$1 GROUP BY d ORDER BY d         # для lineChart
```

Статус (active/expired/exhausted) вычисляется на фронте из promo-полей.

## Frontend (`admin.js`)

### Генерации

- В `loadGenerations()` добавить `rowAction: function(r){ openGeneration(r.id); }`.
- `mediaPreview(gen)` — новый хелпер:
  - `photo`: `result_urls` (массив) → грид `<img>`; иначе одиночный `result_url` → `<img>`.
  - `video`: `<video controls preload="metadata">`.
  - `audio`: `<audio controls>`.
  - пусто → «Нет результата».
- `openGeneration(id)`: `api(...)` → собрать HTML:
  - шапка `#id` + бейдж типа + `badge(status)` + (если `refunded_at`) `badge("refunded")`;
  - юзер (кликабельно → `loadUserDetail(tg_id)`), дата;
  - превью (`mediaPreview`);
  - промпт (`<pre>`);
  - настройки — `settings` как kv-список / `<pre>`;
  - face-блок — только если `face_score != null`: score, threshold, accepted, attempts, ref_found;
  - стоимость `cost W`; если возвращено — строка «Возвращено …»;
  - кнопка «Вернуть N W» — только `window.adminRole==="owner" && !refunded_at && cost>0`;
    клик → `confirmDialog` → `POST .../refund {reason:"admin refund"}` →
    `toast` + `closeModal()` + `window._genTable.reload()`.
- Сохранить ссылку на таблицу: `window._genTable = DataTable(...)`.

### Промокоды

- В `loadPromos()` добавить `rowAction: function(r){ openPromo(r.id); }`
  (кнопки Изм./Удал. уже с `event.stopPropagation()` — клик по строке не конфликтует).
- `openPromo(id)`: `api(...)` → собрать HTML:
  - сводка: код, тип/номинал, `used_count/max_uses` + % исчерпания, всего токенов,
    первая/последняя активация, вычисляемый статус-бейдж;
  - график: `lineChart(daily.map(p=>({d:p.d, v:p.v})))` (или «нет данных»);
  - список активаций: `<table class="tbl">` кто/когда/сколько; юзер → `loadUserDetail`.

## CSS (`admin.css`)

Минимальный блок для превью (грид миниатюр, ограничение высоты медиа):

```css
.gen-preview{margin:12px 0}
.gen-preview img,.gen-preview video{max-width:100%;max-height:320px;border-radius:var(--r);border:1px solid var(--brd)}
.gen-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(120px,1fr));gap:8px}
.gen-grid img{max-height:160px;width:100%;object-fit:cover}
.gen-empty{color:var(--tx3);font-size:13px;padding:16px;text-align:center}
```

## Cache-bust (`admin.html`)

Бампнуть `admin.js?v=29→30` и `admin.css?v=13→14`.

## Тестирование (ручное, прод-БД — CI нет)

- Обе модалки открываются; пустые данные (нет результата / нет активаций) не падают.
- Возврат тест-генерации: баланс юзера +cost, транзакция `refund`, генерация помечена;
  повторный POST → **409**.
- Роли: agent открывает модалку Генераций, но кнопки возврата нет, а прямой POST → **403**;
  промокоды agent вообще не видит (owner-only).
- SQL-проверка на реальной БД до мёржа (ловушки `$N::date`, `GROUP BY` с PK из памяти проекта).

## Файлы

| Файл | Изменение |
|------|-----------|
| `db/database.py` | +2 ALTER TABLE generations (refunded_at, refunded_by) |
| `api/admin_routes.py` | +3 эндпоинта (gen detail, gen refund, promo detail) |
| `webapp/static/js/admin.js` | rowAction ×2, `openGeneration`, `openPromo`, `mediaPreview` |
| `webapp/static/css/admin.css` | +блок `.gen-preview/.gen-grid` |
| `webapp/templates/admin.html` | cache-bust `?v=` |
