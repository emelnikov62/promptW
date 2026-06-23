# PromptW — Доставка уведомлений, Этап 2 (вовлекающие)

> Продолжение Этапа 1 (PR #92). Добавляет вовлекающие TG-уведомления + инфраструктуру:
> опт-аут, тихие часы, лимиты частоты, периодический sweep. Реф-доход остаётся мгновенным
> (без дайджеста). Каталог: 2026-06-23-notifications-catalog-and-copy-design.md.

Дата: 2026-06-23.

## 1. Триггеры (все 4)
| kind | Когда | Лимит | Канал/кнопка |
|---|---|---|---|
| `bonusUnspent` | есть баланс, аккаунту 1–30 дней, НЕТ ни одной генерации | 1 раз навсегда | 💬 createBtn→create |
| `reengage` | `last_active_at` 7–60 дней назад И есть ≥1 генерация | 1×/7 дней | 💬 seeTemplatesBtn→home |
| `rewardAvail` | награды настроены (RWD_*), не клеймил подписочную | 1 раз навсегда | 💬 claimBtn→rewards |
| `weekly` | ручной триггер (есть новинки) | 1×/7 дней | 💬 viewBtn→home |

## 2. Инфраструктура

### 2.1 Схема (миграции в `db/database.py`, идемпотентно)
- `users.notif_marketing BOOLEAN DEFAULT TRUE` — опт-аут вовлекающих.
- `notification_sends(id, user_tg_id, kind, sent_at)` + индекс `(user_tg_id, kind, sent_at DESC)` —
  лог отправок вовлекающих для лимитов/дедупа.

### 2.2 Опт-аут
- Транзакционные (Этап 1) — ВСЕГДА, опт-аут не трогает.
- Вовлекающие — только если `notif_marketing = TRUE`.
- Тумблер «Уведомления о новинках» в профиле WebApp → `POST /api/user/{tg_id}/notif`
  `{on: bool}` → `set_notif_marketing`. Лейбл i18n ru/en/es. Бамп `app.js`/`i18n.js` `?v`.

### 2.3 Тихие часы
Вовлекающий sweep шлёт только в окно **10:00–22:00 МСК** (UTC+3). Sweep ежечасный → просто
проверяет текущий час МСК. Ручной `weekly` тоже уважает окно.

### 2.4 Лимиты / дедуп
Перед отправкой — `was_sent(tg_id, kind, within)`:
- `bonusUnspent`, `rewardAvail` — `within=None` (1 раз навсегда).
- `reengage`, `weekly` — `within='7 days'`.
После успешной отправки — `log_sent(tg_id, kind)`.

### 2.5 Sweep
`start_engagement_sweep(interval=3600)` в `routes.py` (как `start_reconciler`), запуск из
`main.py`. Каждый прогон: если вне окна МСК — выход; иначе для `bonusUnspent`/`reengage`/
`rewardAvail` берём по batch (≤100) подходящих юзеров (SQL с фильтрами §1 + `notif_marketing`
+ NOT was_sent), шлём `notify_bg`, пишем `log_sent`. `rewardAvail` пропускается целиком,
если RWD-каналы не настроены (env пуст).

### 2.6 Weekly (ручной)
`POST /api/admin/notify/weekly` (только ADMIN_IDS): рассылает `weeklyTg` юзерам с
`notif_marketing=TRUE` И активностью за 30 дней И NOT was_sent(weekly, 7d). Уважает тихие
часы. Возвращает число отправленных. Так владелец жмёт «разослать», когда реально добавил
шаблоны.

## 3. Тексты
Добавить в `bot/notif_text.json` (ru/en/es): `reengageTg`, `bonusUnspentTg`, `weeklyTg`,
`rewardAvailTg` + кнопки `seeTemplatesBtn`, `createBtn`, `claimBtn`, `viewBtn` (часть уже есть).
Тексты — из каталога (i18n.js `notif`).

## 4. Обработка ошибок / безопасность
- Sweep полностью изолирован (как reconcile loop): исключение в итерации логируется, цикл
  живёт. Отправки — `notify_bg` (fail-safe).
- Лимиты гарантируют отсутствие спама даже при рестартах (состояние в `notification_sends`,
  не в памяти).
- Деплой: ветка → PR → merge → `gitdeploy`. Миграции идемпотентны. Бэкенд — рестарт; фронт —
  `?v`-бамп.

## 5. Вне Этапа 2
Дайджест реф-дохода (решено: оставляем мгновенно), A/B текстов, аналитика доставки/CTR,
push вне Telegram. Поддержка (ответ/закрытие) уже шлётся ботом.

Связано: [[promptw-notifications]], [[promptw-brand]], [[promptw-bot-deploy]].
