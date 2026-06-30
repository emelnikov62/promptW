# Дизайн: интеграция Payme (Paycom) Merchant API

**Дата:** 2026-06-30
**Статус:** утверждён к реализации (Фаза 1)
**Автор:** сессия Claude + владелец

## Цель

Добавить **Payme (Paycom) Merchant API** как платёжного провайдера для оплаты пакетов
токенов в сумах (UZS). Провайдер доступен всем пользователям наряду с ЮKassa и Platega;
при выборе Payme цены показываются в сумах.

## Контекст и ключевое отличие от существующих провайдеров

Текущие провайдеры (`payments_gw.py`) — **исходящие**, рублёвые, однофазные: сервер зовёт
API провайдера (`yookassa_create`/`platega_create`), затем сверяет статус (`*_verify`).

Payme Merchant API устроен **наоборот**:
- **Входящий JSON-RPC 2.0**: серверы Payme сами вызывают наш endpoint. Исходящего
  «создать платёж» нет — мы лишь формируем base64-ссылку на checkout и реализуем endpoint
  с 6 методами.
- **Валюта — сумы/тийины** (1 UZS = 100 тийин), тогда как вся текущая биллинг-модель
  рублёвая (`amount_rub`, реферальные комиссии в ₽).
- **Двухфазная транзакция** с состоянием `1/2/-1/-2`, которое мы обязаны персистить, чтобы
  отвечать на CheckTransaction/GetStatement и быть идемпотентными.

Подробный справочник протокола — в рабочих заметках сессии (методы, коды ошибок, формат
ссылки); ниже — выжимка, достаточная для реализации.

## Принятые продуктовые решения

| Вопрос | Решение |
|---|---|
| Валютная модель | Отдельный UZS-прайс (не конвертация из ₽) |
| Цены пакетов (сум) | 100→16 900, 300→49 000, 500→79 000, 1000→149 000, 2000→279 000, 5000→649 000 |
| Реферальные комиссии | Отдельный сумовый леджер — **Фаза 2**, в Фазе 1 по UZS не начисляются |
| Видимость в UI | Payme виден всем; при выборе — цены в сумах |
| Статус кассы | Кассы ещё нет → код под env-флагом, кнопка скрыта пока нет ключей |
| account-поле | `order_id` (строка) — единственное поле аккаунта |
| Модель данных | Отдельная таблица `payme_transactions` + 2 колонки в `payments` |

## Архитектура потока

```
Юзер → «Оплатить через Payme»
  → POST /api/topup/create {provider:"payme", package}
  → сервер: payments(pending, currency='UZS', amount_uzs) + строит base64 checkout-ссылку
  → редирект на https://checkout.paycom.uz/<base64>
       ┌──── серверы Payme → POST /api/pay/payme (JSON-RPC, Basic auth) ────┐
       │  CheckPerformTransaction → проверка заказа/суммы → {allow:true}     │
       │  CreateTransaction       → payme_transactions state=1              │
       │  PerformTransaction      → state=2, НАЧИСЛЕНИЕ ТОКЕНОВ (идемпот.)   │
       │  CancelTransaction       → state=-1/-2, откат начисления           │
       │  CheckTransaction / GetStatement → чтение                          │
       └────────────────────────────────────────────────────────────────────┘
  → фронт поллит /api/topup/status → читает наш payme_state (state=2 → paid)
```

Важно: `/api/topup/status` для Payme **не ходит наружу** — читает наше состояние, которое
Payme обновил вебхуком.

## Модель данных

Новая таблица изолирует стейт-машину Merchant API; `payments` остаётся «заказом».

```sql
-- ALTER существующей payments (идемпотентно при старте)
ALTER TABLE payments ADD COLUMN IF NOT EXISTS currency VARCHAR(3) NOT NULL DEFAULT 'RUB';
ALTER TABLE payments ADD COLUMN IF NOT EXISTS amount_uzs BIGINT;   -- сумма в сумах (не тийинах)

CREATE TABLE IF NOT EXISTS payme_transactions (
    payme_txn_id  VARCHAR(40) PRIMARY KEY,        -- id транзакции от Payme
    order_id      UUID NOT NULL REFERENCES payments(order_id),
    state         SMALLINT NOT NULL,              -- 1 | 2 | -1 | -2
    amount_tiyin  BIGINT NOT NULL,                -- сумма в тийинах (как прислал Payme)
    create_time   BIGINT,                         -- мс, как в протоколе
    perform_time  BIGINT,
    cancel_time   BIGINT,
    reason        SMALLINT,                        -- причина отмены
    created_at    TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_payme_txn_order ON payme_transactions(order_id);
CREATE INDEX IF NOT EXISTS idx_payme_txn_ctime ON payme_transactions(create_time);
```

Инвариант: один `order_id` (один checkout) ↔ не более одной строки `payme_transactions`
в активном/проведённом состоянии.

## Backend

### Новый модуль `bot-src/payme_gw.py`
Вся логика Merchant API изолирована и юнит-тестируема (чистые функции над БД-пулом):
- `payme_available() -> bool` — есть ли merchant_id и активный ключ.
- `build_checkout_url(order_id, amount_uzs, lang) -> str` — base64-ссылка:
  `m=<MERCHANT_ID>;ac.order_id=<order_id>;a=<amount_uzs*100>;c=<return_url>;l=<lang>`.
- `verify_auth(header) -> bool` — Basic `Paycom:<KEY>`, constant-time (`hmac.compare_digest`).
- Диспетчер: `handle(method, params) -> result | JsonRpcError`.
- Функции методов: `check_perform`, `create`, `perform`, `cancel`, `check`, `get_statement`.

Константы: `PACKAGES_UZS = {100:16900, 300:49000, 500:79000, 1000:149000, 2000:279000, 5000:649000}`.

### Роут `POST /api/pay/payme` (routes.py)
Тонкая обёртка: rate-limit по IP (`_rate_ok`) → `verify_auth` → `payme_gw.handle` →
JSON-RPC ответ. **HTTP всегда 200** (даже на ошибках — иначе Payme считает `-32400`).

### Логика методов

| Метод | Действие | Идемпотентность |
|---|---|---|
| CheckPerformTransaction | заказ есть и `pending`? сумма (тийины) == `amount_uzs*100`? → `{allow:true}` | чтение |
| CreateTransaction | нет txn → перепроверить как Check, создать state=1; есть → вернуть существующую | по `payme_txn_id` |
| PerformTransaction | state=1 → начислить токены (атомарно, гард 1→2), state=2; state=2 → как есть | по переходу state |
| CancelTransaction | state=1→−1; state=2→−2 + откат токенов | по конечному state |
| CheckTransaction | вернуть тайминги + state + reason | чтение |
| GetStatement | строки `payme_transactions` за `[from,to]` | чтение |

**Начисление токенов** (PerformTransaction): атомарная транзакция БД — гард перехода
state 1→2 в `payme_transactions`, апдейт `users.balance`, запись `transactions('topup')`,
флип `payments.status` → `paid`. Двойной вызов Perform не начисляет повторно.
Реферальные комиссии в сумах — **не начисляются в Фазе 1**.

**Коды ошибок** (JSON-RPC `error`): `-32504` auth; `-31001` неверная сумма;
`-31050..-31099` заказ не найден / неверный account (локализованный `message` {ru,uz,en},
`data:"order_id"`); `-31003` транзакция не найдена; `-31008` нельзя выполнить;
`-31007` нельзя отменить.

### Безопасность
- Сумма всегда сверяется с заказом на сервере (защита от подмены).
- Все мутации — в одной БД-транзакции с гардами (защита от двойного начисления при ретраях).
- account = только `order_id`; чужой/неизвестный → `-31050`.
- Логи без ключей/PII.
- Cloudflare: путь `/api/pay/payme` — в WAF Skip / не блокировать Bot Fight Mode
  (инфра-хвост, исполнителю). Серверы Payme вызывают endpoint снаружи.

## Frontend (`webapp/static/js/app.js`)

- В оверлее оплаты добавить **выбор провайдера**: Карта/СБП (₽) + **Payme (сум)**.
- При выборе Payme прайс пакетов переключается на `PKG_PRICE_UZS`, подпись «сум»;
  при рублёвых — прежние ₽-цены.
- `startPay()` шлёт `{provider:"payme", package}`; сервер сам берёт сумму из `PACKAGES_UZS`
  (фронт-цена не доверяется).
- Кнопка Payme показывается только если `payme_available()` (флаг в bootstrap/`/api/config`).
- `/api/topup/create` для Payme → `{url: checkout-ссылка, order_id}`; фронт открывает url,
  кладёт `order_id` в `localStorage.pwPendingOrder`.
- `/api/topup/status` для Payme читает наш `payme_state` (2 → paid). Поллинг и тост — как есть.

## Конфигурация (env, прод `.env` — отдаётся владельцу)

```
PAYME_MERCHANT_ID=
PAYME_KEY_TEST=
PAYME_KEY_LIVE=
PAYME_MODE=test            # test | live
PAYME_CHECKOUT_BASE=https://checkout.paycom.uz
```

## Админка / возвраты

Refund по Payme через Merchant API наружу не пушится (нет исходящего вызова): возврат
инициируется в кабинете Payme, либо Payme зовёт наш CancelTransaction. Админ-refund для
Payme — ручная отметка (как Platega) + откат токенов. (Сумовый реферальный откат — Фаза 2.)

## Тестирование

- Юнит-тесты диспетчера: Check/Create/Perform/Cancel идемпотентность, неверная сумма,
  чужой order, auth-фейл, двойной Perform.
- Ручной прогон песочницы Payme (developer.help.paycom.uz/pesochnitsa) по их тест-кейсам.

## Порядок работ (Фаза 1)

1. Код: `payme_gw.py` + endpoint-скелет + миграции + фронт за флагом → PR → деплой.
   Кнопка скрыта (ключей нет) — для текущих юзеров ничего не меняется.
2. Отдать Payme: webhook `https://promptw.ru/api/pay/payme` + account-поле `order_id`
   (строка) → получить merchant_id + тестовый ключ.
3. `.env` тест-ключ → прогон песочницы (все 6 методов + двойное начисление/отмена).
4. Боевой ключ, `PAYME_MODE=live`, CF-skip для `/api/pay/payme` → запуск.

## Out of scope (Фаза 2, отдельная спека)

- Сумовый реферальный леджер: `users.ref_balance_uzs`, `ref_earnings.currency`
  (или `ref_earnings_uzs`), начисление 30%/5% в сумах на Perform, откат на Cancel,
  UI выплат в сумах.
- Настройка UZS-цен из админки (сейчас — в коде).

## Справочник протокола (выжимка)

- **Checkout (GET):** `https://checkout.paycom.uz/<base64(params)>`,
  `params = m=<id>;ac.order_id=<v>;a=<тийины>;c=<return>;l=<ru|uz|en>`.
- **Auth:** `Authorization: Basic base64("Paycom:<KEY>")`. Логин всегда `Paycom`,
  пароль — ключ кассы. Провал → `-32504`.
- **Состояния:** 1 создана, 2 проведена (оплачено), −1 отмена до проведения,
  −2 отмена после проведения (возврат).
- **Ответ всегда HTTP 200**, тело JSON-RPC `{result}` или `{error:{code,message,data}}`.
