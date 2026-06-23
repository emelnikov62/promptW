# PromptW — Доставка уведомлений, Этап 1 (транзакционные TG)

> Реализация отправки уведомлений из каталога
> [2026-06-23-notifications-catalog-and-copy-design.md](2026-06-23-notifications-catalog-and-copy-design.md).
> Этап 1 = только транзакционные 💬 TG-сообщения (всплывашки уже живут в app.js).
> Вовлекающие, опт-аут, тихие часы, дайджест — Этап 2.

Дата: 2026-06-23.

## 1. Цель и принципы

- Слать брендовые TG-сообщения по транзакционным событиям, когда они важны (часто при
  закрытом приложении).
- **Fail-safe прежде всего:** отправка уведомления НИКОГДА не ломает основной флоу
  (оплата/генерация/вывод). Любая ошибка отправки — лог + проглотить.
- **Без спама:** дедуп через хартбит — если юзер активен в WebApp (<60с), TG-дубль для
  событий генерации не шлём. Финансовые «чеки» (оплата, вывод) шлём всегда.

## 2. Компоненты

### 2.1 `bot/notif_text.json` — тексты (источник для бэка)
Плоский JSON `{ "ru": {...}, "en": {...}, "es": {...} }` с TG-строками и подписями кнопок.
Копия текстов из каталога; для бота это **рантайм-источник** (i18n.js обслуживает фронт-
всплывашки). Набор Этапа 1: `payTg/payBtn`, `welcomeTg/welcomeBtn`, `photoTg/photosTgN/
viewBtn`, `videoTg`, `audioTg/listenBtn`, `genFailTg/retryBtn`, `refNewTg/partnerBtn`,
`refEarnTg`, `wdPaidTg`, `wdRejectTg`.

### 2.2 `bot/notify.py` — отправка
```
async def notify(tg_id, key, *, btn_key=None, page=None, **params) -> bool
```
- Берёт язык: `get_user(tg_id).lang` (fallback `ru`).
- Текст: `notif_text[lang][key]`, `.format(**params)` (нехватка плейсхолдера → лог, без падения).
- Кнопка (опц.): inline `InlineKeyboardButton(text=notif_text[lang][btn_key],
  web_app=WebAppInfo(url=_wa_url(page, token)))` — открывает мини-апп (deep-link с
  fallback-токеном, как в `handlers._wa_url`).
- Шлёт через `_get_bot()` (хэндл уже есть в routes; notify берёт его лениво).
- **Весь body в try/except** → возвращает False при любой ошибке (бот заблокирован и т.п.),
  никогда не пробрасывает.
- Хелпер `notify_bg(...)` = `asyncio.ensure_future` + хранить ссылку (как `_BG_GENS`),
  чтобы отправка не блокировала ответ HTTP и не терялась в GC.

### 2.3 Хартбит (дедуп)
- Миграция: `users.last_active_at timestamptz` (nullable), идемпотентно в `db.database`.
- Запрос `touch_active(tg_id)` → `UPDATE users SET last_active_at = NOW()`.
- Эндпоинт `POST /api/heartbeat` (авторизованный, rate-limited) → `touch_active`.
- Фронт: `app.js` пингует `/api/heartbeat` при старте и раз в 45с, пока вкладка видима
  (`visibilitychange`). Бамп `app.js?v=N` в `index.html`.
- Хелпер `is_active(user_row, secs=60)` → `last_active_at` свежее порога.

## 3. Точки вызова (хуки)

| Событие | Где | Кому | Дедуп |
|---|---|---|---|
| Оплата прошла + реф-доход | `settle_and_notify()` обёртка вокруг 4 вызовов `settle_payment` | buyer + рефереры | нет |
| Фото готово | хендлер `/api/generate/image` после успешного `resp` + reconciler | автор | да |
| Видео / аудио готово | хендлеры video/audio после успеха + reconciler | автор | да |
| Генерация не удалась | `_run_generation._runner` except (после refund) | автор | да |
| Новый реферал | `handlers.cmd_start` после `create_referral` | реферер | нет |
| Вывод выплачен/отклонён | `api_admin_withdrawal_set` после `set_withdrawal_status` | юзер | нет |
| Приветствие + бонус | `handlers.cmd_start` (замена текущего текста) | юзер | нет |

### 3.1 `settle_payment` — добавить возврат реф-кредитов
Сейчас возвращает dict платежа. Дополнить: собирать начисления в список и вернуть
`pay["ref_credits"] = [{"tg_id", "line", "amount"}]`. Аддитивно, существующие читатели не
ломаются. Обёртка `settle_and_notify(order_id, ...)` в routes вызывает `settle_payment`,
и если вернулось не-None (свежий расчёт) — шлёт `payTg` покупателю + `refEarnTg` каждому из
`ref_credits` (сумма `amount`).

### 3.2 Генерация готова — дедуп на хендлере
Юзер, который только что нажал «Создать», активен → TG-дубль гасится (видит всплывашку).
TG-сообщение реально уходит, когда генерация долгая (видео) и юзер ушёл, либо при
восстановлении после рестарта (reconciler, см. §3.3). `photosTgN` — если фото >1.

### 3.3 Reconciler
В `_reconcile_once` после успешного `finish_generation_if_pending` (строка ~388) — `notify`
автору о готовности (через `notify_bg`). Тут юзер почти всегда давно ушёл → дедуп пропустит.

## 4. Обработка ошибок
- `notify`/`notify_bg` — полностью изолированы (try/except внутри). Хуки вызывают их
  fire-and-forget; основной код не ждёт и не зависит от результата.
- Неизвестный язык/ключ/плейсхолдер → лог + пропуск (не падать).

## 5. Безопасность изменений / деплой
- Бэкенд-правки (`routes.py`, `queries.py`, `database.py`, `bot/*`) — рестарт сервиса.
- Фронт (`app.js` + `index.html` ?v-бамп) — статика live.
- Миграция `last_active_at` — идемпотентная `ADD COLUMN IF NOT EXISTS` в `db.database`.
- Деплой: ветка → PR → `gh pr merge` → `bash srv2.sh gitdeploy` (по команде владельца).
- **Прод-чек после деплоя:** тест-оплата (или промокод), быстрая генерация (всплывашка, без
  TG т.к. активен), генерация с закрытым приложением (приходит TG), реф-доход, вывод.

## 6. Вне Этапа 1
Вовлекающие (reengage/weekly/bonusUnspent), таблица опт-аута, тихие часы, дайджест
реф-дохода (сейчас — по событию), планировщик. Поддержка (6.1/6.2) уже работает — тексты
причешем отдельно.

Связано: [[promptw-bot-deploy]], каталог уведомлений (выше), [[promptw-brand]].
