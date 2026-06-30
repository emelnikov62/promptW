# Payme (Paycom) Merchant API — Implementation Plan (Фаза 1)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Принимать оплату пакетов токенов в сумах (UZS) через Payme Merchant API — входящий JSON-RPC endpoint с 6 методами, начисление токенов на PerformTransaction.

**Architecture:** Payme сам вызывает наш `POST /api/pay/payme` (JSON-RPC 2.0, Basic auth). Логика Merchant API изолирована в новом модуле `payme_gw.py`; стейт-машина транзакций (1/2/−1/−2) живёт в новой таблице `payme_transactions`; `payments` остаётся «заказом» (+колонки `currency`, `amount_uzs`). Юзера отправляем на `checkout.paycom.uz` base64-ссылкой. Статус читается из нашей БД (без исходящих вызовов).

**Tech Stack:** Python 3.12, aiohttp, asyncpg, vanilla JS. Без тест-фреймворка (по `bot-src/CLAUDE.md`) — чистая логика покрывается dependency-free assert-скриптом, БД/роуты — песочницей Payme.

## Global Constraints

- Спека: `bot-src/docs/specs/2026-06-30-payme-merchant-api-design.md` (источник правды).
- **Фаза 1 — без реферальных комиссий по UZS** (рублёвую рефералку не трогаем).
- Цены (сум): `100→16900, 300→49000, 500→79000, 1000→149000, 2000→279000, 5000→649000`.
- account-поле — одно: `order_id` (строка). Сумма — в тийинах (сум × 100).
- **Endpoint всегда отвечает HTTP 200** с JSON-RPC телом (даже на ошибках).
- Состояния: `1` создана, `2` проведена, `−1` отмена до проведения, `−2` отмена после.
- Коды ошибок: `-32504` auth, `-31001` сумма, `-31050..-31099` account (с локализ. message + data), `-31003` txn не найдена, `-31008` нельзя выполнить, `-31007` нельзя отменить.
- Cache-busting: бампать `?v=N` для каждого изменённого ассета в `index.html`.
- Деплой: ветка → PR → `bash srv2.sh gitdeploy`. env (`PAYME_*`) — в прод `.env` (отдаётся владельцу). Кнопка Payme скрыта, пока `payme_available()` ложно — прод для текущих юзеров не меняется.
- Порядок: **Tasks 1–4 = деплоимый endpoint** (вариант «а» — выкатить до отдачи URL в Payme, чтобы прошла песочница). Task 5 (фронт) — следом.

---

### Task 1: Миграции БД (currency/amount_uzs + payme_transactions)

**Files:**
- Modify: `bot-src/db/database.py:188` (после блока индексов payments/ref/withdrawals)

**Interfaces:**
- Produces: колонки `payments.currency VARCHAR(3) DEFAULT 'RUB'`, `payments.amount_uzs BIGINT`; таблица `payme_transactions(payme_txn_id PK, payment_id, order_id, state, amount_tiyin, create_time, perform_time, cancel_time, reason, created_at)`.

- [ ] **Step 1: Добавить миграции в идемпотентный DDL-блок**

В `bot-src/db/database.py` сразу после строки 188 (`CREATE INDEX ... idx_withdrawals_status`) добавить в тот же выполняемый SQL-скрипт:

```sql
            -- Payme (Paycom) Merchant API: оплата в сумах (UZS).
            ALTER TABLE payments ADD COLUMN IF NOT EXISTS currency VARCHAR(3) NOT NULL DEFAULT 'RUB';
            ALTER TABLE payments ADD COLUMN IF NOT EXISTS amount_uzs BIGINT;

            CREATE TABLE IF NOT EXISTS payme_transactions (
                payme_txn_id VARCHAR(40) PRIMARY KEY,        -- id транзакции Payme
                payment_id   BIGINT NOT NULL REFERENCES payments(id),
                order_id     UUID NOT NULL REFERENCES payments(order_id),
                state        SMALLINT NOT NULL,              -- 1 | 2 | -1 | -2
                amount_tiyin BIGINT NOT NULL,               -- сумма в тийинах (как прислал Payme)
                create_time  BIGINT,                        -- ms epoch
                perform_time BIGINT,
                cancel_time  BIGINT,
                reason       SMALLINT,
                created_at   TIMESTAMPTZ DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_payme_txn_order  ON payme_transactions(order_id);
            CREATE INDEX IF NOT EXISTS idx_payme_txn_ctime  ON payme_transactions(create_time);
```

- [ ] **Step 2: Прогнать миграции локально**

Run: `python -c "import asyncio; from db.database import init_db; asyncio.run(init_db())"` (из `bot-src/`, при поднятом PostgreSQL и заполненном `DATABASE_URL`).
Expected: без ошибок. (Если точка входа миграций называется иначе — использовать ту, что зовётся из `main.py` при старте.)

- [ ] **Step 3: Проверить схему**

Run: `psql "$DATABASE_URL" -c "\d payme_transactions" -c "\d payments"`
Expected: таблица `payme_transactions` со всеми колонками; в `payments` есть `currency` и `amount_uzs`.

- [ ] **Step 4: Commit**

```bash
git add db/database.py
git commit -m "feat(payme): db migrations — payments.currency/amount_uzs + payme_transactions"
```

---

### Task 2: Запросы к БД (queries.py)

**Files:**
- Modify: `bot-src/db/queries.py` (рядом с `create_payment`/`settle_payment`, ~line 509–643)

**Interfaces:**
- Consumes: `get_pool()` (уже импортирован в модуле).
- Produces:
  - `create_payme_payment(order_id, tg_id, amount_uzs, tokens, bonus_pct=0, bonus_tokens=0, promo_id=None) -> int`
  - `payme_order_by_id(order_id) -> Optional[dict]` → `{id,user_tg_id,status,amount_uzs,tokens,bonus_tokens}`
  - `payme_txn_by_id(payme_txn_id) -> Optional[dict]` → полная строка `payme_transactions`
  - `payme_active_txn_for_order(order_id) -> Optional[dict]`
  - `payme_insert_txn(payme_txn_id, payment_id, order_id, amount_tiyin, create_time) -> None`
  - `payme_perform_txn(payme_txn_id, perform_time) -> Optional[dict]` → `{state,perform_time,payment_id,user_tg_id,total_tokens,credited}` или None
  - `payme_cancel_txn(payme_txn_id, cancel_time, reason) -> Optional[dict]` → `{state,cancel_time}` или None
  - `payme_list_txns(from_ms, to_ms) -> list[dict]`

- [ ] **Step 1: Добавить create_payme_payment и read-хелперы**

```python
async def create_payme_payment(order_id: str, tg_id: int, amount_uzs: int, tokens: int,
                               bonus_pct: int = 0, bonus_tokens: int = 0,
                               promo_id: Optional[int] = None) -> int:
    """Создать pending-заказ в сумах. amount_rub=0 (UZS-заказ), currency='UZS'."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval("""
            INSERT INTO payments (order_id, user_tg_id, provider, amount_rub, currency,
                                  amount_uzs, tokens, bonus_pct, bonus_tokens, promo_id)
            VALUES ($1, $2, 'payme', 0, 'UZS', $3, $4, $5, $6, $7) RETURNING id
        """, order_id, tg_id, amount_uzs, tokens, bonus_pct, bonus_tokens, promo_id)


async def payme_order_by_id(order_id: str) -> Optional[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT id, user_tg_id, status, amount_uzs, tokens, bonus_tokens
            FROM payments WHERE order_id = $1 AND provider = 'payme'
        """, order_id)
        return dict(row) if row else None


async def payme_txn_by_id(payme_txn_id: str) -> Optional[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM payme_transactions WHERE payme_txn_id = $1", payme_txn_id)
        return dict(row) if row else None


async def payme_active_txn_for_order(order_id: str) -> Optional[dict]:
    """Активная (state IN 1,2) транзакция этого заказа, если есть."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM payme_transactions WHERE order_id = $1 AND state IN (1,2) LIMIT 1",
            order_id)
        return dict(row) if row else None


async def payme_insert_txn(payme_txn_id: str, payment_id: int, order_id: str,
                           amount_tiyin: int, create_time: int) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO payme_transactions (payme_txn_id, payment_id, order_id, state,
                                            amount_tiyin, create_time)
            VALUES ($1, $2, $3, 1, $4, $5)
        """, payme_txn_id, payment_id, order_id, amount_tiyin, create_time)
```

- [ ] **Step 2: Добавить payme_perform_txn (атомарное начисление, идемпотентно)**

```python
async def payme_perform_txn(payme_txn_id: str, perform_time: int) -> Optional[dict]:
    """Провести транзакцию: state 1->2, начислить токены ОДИН раз, флипнуть payments->paid.
    Гард по state внутри транзакции защищает от двойного начисления при ретраях Payme.
    Возвращает {state, perform_time, payment_id, user_tg_id, total_tokens, credited} или None."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            txn = await conn.fetchrow(
                "SELECT * FROM payme_transactions WHERE payme_txn_id = $1 FOR UPDATE",
                payme_txn_id)
            if txn is None:
                return None
            if txn["state"] == 2:
                return {"state": 2, "perform_time": txn["perform_time"],
                        "payment_id": txn["payment_id"], "credited": False}
            if txn["state"] != 1:
                return None  # отменённую провести нельзя
            pay = await conn.fetchrow(
                "SELECT user_tg_id, tokens, bonus_tokens, amount_uzs FROM payments WHERE id = $1",
                txn["payment_id"])
            total = (pay["tokens"] or 0) + (pay["bonus_tokens"] or 0)
            await conn.execute(
                "UPDATE payme_transactions SET state = 2, perform_time = $2 WHERE payme_txn_id = $1",
                payme_txn_id, perform_time)
            await conn.execute(
                "UPDATE payments SET status = 'paid', paid_at = NOW() WHERE id = $1 AND status = 'pending'",
                txn["payment_id"])
            await conn.execute(
                "UPDATE users SET balance = balance + $1, updated_at = NOW() WHERE tg_id = $2",
                total, pay["user_tg_id"])
            await conn.execute(
                "INSERT INTO transactions (user_tg_id, amount, tx_type, description) VALUES ($1,$2,'topup',$3)",
                pay["user_tg_id"], total, f"topup:{pay['amount_uzs']} UZS (payme)")
            return {"state": 2, "perform_time": perform_time, "payment_id": txn["payment_id"],
                    "user_tg_id": pay["user_tg_id"], "total_tokens": total, "credited": True}
```

- [ ] **Step 3: Добавить payme_cancel_txn (откат при необходимости) и payme_list_txns**

```python
async def payme_cancel_txn(payme_txn_id: str, cancel_time: int, reason: int) -> Optional[dict]:
    """Отмена: state 1->-1; state 2->-2 + откат токенов (clamp >=0) и payments->refunded.
    Идемпотентно: повторный вызов на уже отменённой возвращает текущее состояние."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            txn = await conn.fetchrow(
                "SELECT * FROM payme_transactions WHERE payme_txn_id = $1 FOR UPDATE",
                payme_txn_id)
            if txn is None:
                return None
            if txn["state"] in (-1, -2):
                return {"state": txn["state"], "cancel_time": txn["cancel_time"]}
            new_state = -1 if txn["state"] == 1 else -2
            await conn.execute(
                "UPDATE payme_transactions SET state = $2, cancel_time = $3, reason = $4 WHERE payme_txn_id = $1",
                payme_txn_id, new_state, cancel_time, reason)
            if txn["state"] == 2:   # уже была проведена — откатываем начисление
                pay = await conn.fetchrow(
                    "SELECT user_tg_id, tokens, bonus_tokens FROM payments WHERE id = $1",
                    txn["payment_id"])
                total = (pay["tokens"] or 0) + (pay["bonus_tokens"] or 0)
                await conn.execute(
                    "UPDATE users SET balance = GREATEST(0, balance - $1), updated_at = NOW() WHERE tg_id = $2",
                    total, pay["user_tg_id"])
                await conn.execute(
                    "UPDATE payments SET status = 'refunded', refunded_at = NOW() WHERE id = $1",
                    txn["payment_id"])
                await conn.execute(
                    "INSERT INTO transactions (user_tg_id, amount, tx_type, description) VALUES ($1,$2,'refund',$3)",
                    pay["user_tg_id"], -total, f"payme cancel txn {payme_txn_id}")
            return {"state": new_state, "cancel_time": cancel_time}


async def payme_list_txns(from_ms: int, to_ms: int) -> list:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT * FROM payme_transactions
            WHERE create_time >= $1 AND create_time <= $2 ORDER BY create_time
        """, from_ms, to_ms)
        return [dict(r) for r in rows]
```

- [ ] **Step 4: Проверить импорт модуля**

Run: `python -c "import db.queries"` (из `bot-src/`)
Expected: без ошибок (синтаксис/типы валидны).

- [ ] **Step 5: Commit**

```bash
git add db/queries.py
git commit -m "feat(payme): queries — create_payme_payment + txn state machine helpers"
```

---

### Task 3: Модуль payme_gw.py (протокол + диспетчер)

**Files:**
- Create: `bot-src/payme_gw.py`
- Create: `bot-src/tests/test_payme_gw.py`

**Interfaces:**
- Consumes: `db.queries` (Task 2 helpers).
- Produces:
  - `PACKAGES_UZS: dict[str,int]`
  - `payme_available() -> bool`
  - `build_checkout_url(order_id, amount_uzs, return_url, lang='ru') -> str`
  - `verify_auth(auth_header: str) -> bool`
  - `async handle(req: dict) -> dict` (полный JSON-RPC envelope)
  - `class PaymeError(Exception)` с `.code/.message/.data`

- [ ] **Step 1: Написать падающий тест чистой логики**

Создать `bot-src/tests/test_payme_gw.py` (dependency-free, запуск `python tests/test_payme_gw.py`):

```python
"""Юнит-тесты чистой логики Payme (без БД). Запуск: python tests/test_payme_gw.py"""
import base64, os, sys
os.environ["PAYME_MERCHANT_ID"] = "MID123"
os.environ["PAYME_KEY_TEST"] = "testkey"
os.environ["PAYME_MODE"] = "test"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import payme_gw as p

def test_build_checkout_url():
    url = p.build_checkout_url("ord-1", 16900, "https://t.me/promptW_bot/app", "uz")
    assert url.startswith("https://checkout.paycom.uz/"), url
    raw = base64.b64decode(url.rsplit("/", 1)[1]).decode()
    assert "m=MID123" in raw and "ac.order_id=ord-1" in raw
    assert "a=1690000" in raw                      # 16900 сум -> тийины
    assert "l=uz" in raw

def test_verify_auth_ok_and_fail():
    good = "Basic " + base64.b64encode(b"Paycom:testkey").decode()
    bad  = "Basic " + base64.b64encode(b"Paycom:wrong").decode()
    assert p.verify_auth(good) is True
    assert p.verify_auth(bad) is False
    assert p.verify_auth("") is False

def test_available():
    assert p.payme_available() is True

if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); print("ok:", name)
    print("ALL PASS")
```

- [ ] **Step 2: Запустить тест — убедиться, что падает**

Run: `python tests/test_payme_gw.py` (из `bot-src/`)
Expected: FAIL — `ModuleNotFoundError: No module named 'payme_gw'`.

- [ ] **Step 3: Создать payme_gw.py — конфиг, чистые функции, ошибки**

```python
"""Payme (Paycom) Merchant API — входящий JSON-RPC провайдер (оплата в сумах, UZS).

Payme сам вызывает наш endpoint (CheckPerform/Create/Perform/Cancel/Check/GetStatement).
Исходящего "создать платёж" нет — мы лишь строим base64-ссылку на checkout. Деньги в
тийинах (1 сум = 100 тийин). Спека: docs/specs/2026-06-30-payme-merchant-api-design.md.
"""
import os
import time
import base64
import hmac
import logging

from db import queries as q

logger = logging.getLogger(__name__)

# ── Config ──
PAYME_MERCHANT_ID = os.getenv("PAYME_MERCHANT_ID", "")
PAYME_KEY_TEST = os.getenv("PAYME_KEY_TEST", "")
PAYME_KEY_LIVE = os.getenv("PAYME_KEY_LIVE", "")
PAYME_MODE = os.getenv("PAYME_MODE", "test")            # test | live
PAYME_CHECKOUT_BASE = os.getenv("PAYME_CHECKOUT_BASE", "https://checkout.paycom.uz")

# Цены пакетов в сумах (UZS). tokens берутся из payments_gw.PACKAGES.
PACKAGES_UZS = {"100": 16900, "300": 49000, "500": 79000,
                "1000": 149000, "2000": 279000, "5000": 649000}

# ── Состояния и коды ошибок ──
STATE_CREATED, STATE_DONE, STATE_CANCELED, STATE_CANCELED_AFTER = 1, 2, -1, -2
ERR_AUTH = -32504
ERR_AMOUNT = -31001
ERR_TXN_NOT_FOUND = -31003
ERR_CANT_PERFORM = -31008
ERR_CANT_CANCEL = -31007
ERR_ACCOUNT = -31050          # заказ не найден / неверный account
_MSG_ORDER = {"ru": "Заказ не найден", "uz": "Buyurtma topilmadi", "en": "Order not found"}


class PaymeError(Exception):
    def __init__(self, code, message, data=None):
        super().__init__(message)
        self.code, self.message, self.data = code, message, data


def _active_key() -> str:
    return PAYME_KEY_LIVE if PAYME_MODE == "live" else PAYME_KEY_TEST


def payme_available() -> bool:
    return bool(PAYME_MERCHANT_ID and _active_key())


def build_checkout_url(order_id: str, amount_uzs: int, return_url: str, lang: str = "ru") -> str:
    """GET-чек: base64(m=..;ac.order_id=..;a=<тийины>;c=<return>;l=<lang>)."""
    params = (f"m={PAYME_MERCHANT_ID};ac.order_id={order_id};"
              f"a={int(amount_uzs) * 100};c={return_url};l={lang}")
    token = base64.b64encode(params.encode("utf-8")).decode("ascii")
    return f"{PAYME_CHECKOUT_BASE}/{token}"


def verify_auth(auth_header: str) -> bool:
    """Authorization: Basic base64('Paycom:<KEY>'). Сверка ключа constant-time."""
    if not auth_header or not auth_header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(auth_header[6:]).decode("utf-8")
    except Exception:
        return False
    _, _, password = decoded.partition(":")
    key = _active_key()
    return bool(key) and hmac.compare_digest(password, key)
```

- [ ] **Step 4: Дописать в payme_gw.py методы протокола и диспетчер**

```python
def _now_ms() -> int:
    return int(time.time() * 1000)


async def _check_order(account: dict, amount_tiyin: int) -> dict:
    """Общая проверка для CheckPerform/Create. Возвращает order dict или бросает PaymeError."""
    order_id = (account or {}).get("order_id")
    if not order_id:
        raise PaymeError(ERR_ACCOUNT, _MSG_ORDER, {"order_id": _MSG_ORDER["ru"]})
    order = await q.payme_order_by_id(str(order_id))
    if not order:
        raise PaymeError(ERR_ACCOUNT, _MSG_ORDER, {"order_id": _MSG_ORDER["ru"]})
    if int(order["amount_uzs"]) * 100 != int(amount_tiyin):
        raise PaymeError(ERR_AMOUNT, "Неверная сумма")
    if order["status"] != "pending":
        raise PaymeError(ERR_CANT_PERFORM, "Заказ уже обработан")
    return order


async def _m_check_perform(params):
    await _check_order(params.get("account"), params.get("amount"))
    return {"allow": True}


async def _m_create(params):
    txn_id = params["id"]
    existing = await q.payme_txn_by_id(txn_id)
    if existing:
        return {"create_time": existing["create_time"],
                "transaction": str(existing["payment_id"]), "state": existing["state"]}
    order = await _check_order(params.get("account"), params.get("amount"))
    # один активный платёж на заказ
    if await q.payme_active_txn_for_order(str(params["account"]["order_id"])):
        raise PaymeError(ERR_CANT_PERFORM, "По заказу уже есть транзакция")
    create_time = int(params.get("time") or _now_ms())
    await q.payme_insert_txn(txn_id, order["id"], str(params["account"]["order_id"]),
                             int(params["amount"]), create_time)
    return {"create_time": create_time, "transaction": str(order["id"]), "state": STATE_CREATED}


async def _m_perform(params):
    res = await q.payme_perform_txn(params["id"], _now_ms())
    if res is None:
        raise PaymeError(ERR_TXN_NOT_FOUND, "Транзакция не найдена")
    return {"transaction": str(res["payment_id"]), "perform_time": res["perform_time"],
            "state": STATE_DONE, "_credited": res.get("credited"),
            "_user": res.get("user_tg_id"), "_tokens": res.get("total_tokens")}


async def _m_cancel(params):
    res = await q.payme_cancel_txn(params["id"], _now_ms(), int(params.get("reason") or 0))
    if res is None:
        raise PaymeError(ERR_TXN_NOT_FOUND, "Транзакция не найдена")
    txn = await q.payme_txn_by_id(params["id"])
    return {"transaction": str(txn["payment_id"]), "cancel_time": res["cancel_time"],
            "state": res["state"]}


async def _m_check(params):
    txn = await q.payme_txn_by_id(params["id"])
    if not txn:
        raise PaymeError(ERR_TXN_NOT_FOUND, "Транзакция не найдена")
    return {"create_time": txn["create_time"] or 0, "perform_time": txn["perform_time"] or 0,
            "cancel_time": txn["cancel_time"] or 0, "transaction": str(txn["payment_id"]),
            "state": txn["state"], "reason": txn["reason"]}


async def _m_statement(params):
    rows = await q.payme_list_txns(int(params["from"]), int(params["to"]))
    return {"transactions": [{
        "id": r["payme_txn_id"], "time": r["create_time"] or 0, "amount": r["amount_tiyin"],
        "account": {"order_id": str(r["order_id"])}, "create_time": r["create_time"] or 0,
        "perform_time": r["perform_time"] or 0, "cancel_time": r["cancel_time"] or 0,
        "transaction": str(r["payment_id"]), "state": r["state"], "reason": r["reason"],
    } for r in rows]}


_METHODS = {
    "CheckPerformTransaction": _m_check_perform, "CreateTransaction": _m_create,
    "PerformTransaction": _m_perform, "CancelTransaction": _m_cancel,
    "CheckTransaction": _m_check, "GetStatement": _m_statement,
}


async def handle(req: dict) -> dict:
    """Диспетчер JSON-RPC. Возвращает полный envelope (result или error). Не бросает."""
    rid = req.get("id")
    method = req.get("method")
    params = req.get("params") or {}
    fn = _METHODS.get(method)
    if fn is None:
        return {"jsonrpc": "2.0", "id": rid,
                "error": {"code": -32601, "message": "Method not found"}}
    try:
        result = await fn(params)
        return {"jsonrpc": "2.0", "id": rid, "result": result}
    except PaymeError as e:
        return {"jsonrpc": "2.0", "id": rid,
                "error": {"code": e.code, "message": e.message, "data": e.data}}
    except Exception:
        logger.exception("Payme handle error: %s", method)
        return {"jsonrpc": "2.0", "id": rid,
                "error": {"code": -32400, "message": "Internal error"}}
```

> Примечание: `_credited/_user/_tokens` в ответе Perform — служебные поля для уведомления в роуте; роут удаляет их перед отдачей Payme (Task 4).

- [ ] **Step 5: Запустить тест — убедиться, что проходит**

Run: `python tests/test_payme_gw.py` (из `bot-src/`)
Expected: `ok: test_available` / `ok: test_build_checkout_url` / `ok: test_verify_auth_ok_and_fail` / `ALL PASS`.

- [ ] **Step 6: Commit**

```bash
git add payme_gw.py tests/test_payme_gw.py
git commit -m "feat(payme): payme_gw module — checkout url, auth, 6-method JSON-RPC dispatcher"
```

---

### Task 4: Роут /api/pay/payme + ветка create + providers (routes.py)

**Files:**
- Modify: `bot-src/api/routes.py:31` (импорт queries), `:75` (`_AUTH_SKIP`), `:1136-1194` (topup/create), хвост платёжных роутов (~`:1293`)

**Interfaces:**
- Consumes: `payme_gw` (Task 3), `create_payme_payment` (Task 2), `pg.PACKAGES`, `_rate_ok`, `_client_ip`, `notif`.
- Produces: `POST /api/pay/payme`, `GET /api/pay/providers`, ветка `provider=="payme"` в `/api/topup/create`.

- [ ] **Step 1: Импорты и auth-skip**

В `bot-src/api/routes.py` добавить импорт модуля рядом с `import payments_gw as pg` (найти эту строку):
```python
import payme_gw as pm
```
Добавить `create_payme_payment` в импорт из `db.queries` (строка ~31, где уже импортируются `create_payment, set_payment_external, ...`).
Добавить путь в `_AUTH_SKIP` (строка 75) — Payme вызывает endpoint без initData:
```python
_AUTH_SKIP = ("/api/health", "/api/callback", "/api/pay/yoomoney", "/api/pay/platega",
              "/api/pay/payme", "/api/admin/login", "/api/media-proxy")
```

- [ ] **Step 2: Ветка provider=="payme" в /api/topup/create**

В `api_topup_create`, перед финальным `return web.json_response({"error": "bad_provider"}, ...)` (строка 1194), вставить:
```python
    if provider == "payme":
        if not pm.payme_available():
            return web.json_response({"error": "provider_unavailable"}, status=503)
        amount_uzs = pm.PACKAGES_UZS.get(pkg_id)
        if not amount_uzs:
            return web.json_response({"error": "bad_package"}, status=400)
        await create_payme_payment(order_id, tg_id, amount_uzs, tokens,
                                   bonus_pct=bonus_pct, bonus_tokens=bonus_tokens, promo_id=promo_id)
        user = await get_user(tg_id)
        lang = (user or {}).get("lang") or "ru"
        url = pm.build_checkout_url(order_id, amount_uzs, success_url, lang)
        return web.json_response({"url": url, "order_id": order_id})
```

- [ ] **Step 3: Роут /api/pay/payme и /api/pay/providers**

После `api_pay_platega` (после строки 1293) добавить:
```python
@routes.post("/api/pay/payme")
async def api_pay_payme(request: web.Request):
    # Payme сам вызывает endpoint (JSON-RPC). ВСЕГДА отвечаем HTTP 200 с JSON-RPC телом.
    if not _rate_ok("cb:" + _client_ip(request), 240, 60):
        return web.json_response({"error": {"code": -32400, "message": "Too many requests"}})
    auth = request.headers.get("Authorization", "")
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"jsonrpc": "2.0", "id": None,
                                  "error": {"code": -32700, "message": "Parse error"}})
    if not pm.verify_auth(auth):
        logger.warning("Payme call with bad auth")
        return web.json_response({"jsonrpc": "2.0", "id": body.get("id"),
                                  "error": {"code": pm.ERR_AUTH, "message": "Authorization failed"}})
    resp = await pm.handle(body)
    # уведомление при первом успешном Perform; служебные поля убрать из ответа Payme
    res = resp.get("result")
    if isinstance(res, dict) and res.pop("_credited", False):
        try:
            total = res.pop("_tokens", 0) or 0
            uid = res.pop("_user", None)
            if uid:
                notif.notify_bg(uid, "payTg", btn_key="payBtn", page="create",
                                n=total, k=max(1, total // 30))
        except Exception:
            logger.exception("Payme perform notify failed")
    elif isinstance(res, dict):
        res.pop("_credited", None); res.pop("_user", None); res.pop("_tokens", None)
    return web.json_response(resp)


@routes.get("/api/pay/providers")
async def api_pay_providers(request: web.Request):
    """Доступность провайдеров — фронт скрывает недоступные способы оплаты."""
    return web.json_response({"yoomoney": pg.yookassa_available(),
                              "platega": pg.platega_available(),
                              "payme": pm.payme_available()})
```

> `/api/topup/status` менять НЕ нужно: для payme нет outbound-ветки, эндпоинт вернёт текущий `status` из БД (PerformTransaction уже выставил `paid`).

- [ ] **Step 4: Проверить запуск сервера и роуты**

Run: `python -c "import api.routes"` (из `bot-src/`) — Expected: без ошибок импорта.
Run (при поднятом сервере, `PAYME_*` не заданы): `curl -s localhost:8081/api/pay/providers`
Expected: `{"yoomoney": ..., "platega": ..., "payme": false}`.
Run: `curl -s -X POST localhost:8081/api/pay/payme -H 'Content-Type: application/json' -d '{"id":1,"method":"CheckPerformTransaction","params":{}}'`
Expected: HTTP 200, тело с `error.code = -32504` (auth, т.к. без заголовка ключа).

- [ ] **Step 5: Commit**

```bash
git add api/routes.py
git commit -m "feat(payme): /api/pay/payme endpoint + topup create branch + providers flag"
```

**🚀 Деплой-чекпоинт (вариант «а»):** после Task 4 endpoint полнофункционален. Влить PR, `bash srv2.sh gitdeploy`, затем отдать Payme `https://promptw.ru/api/pay/payme` + account `order_id` (строка), получить тестовый ключ. До Task 5 кнопки Payme на фронте нет — прод для юзеров не меняется.

---

### Task 5: Фронт — выбор Payme + UZS-цены (app.js, index.html, i18n.js)

**Files:**
- Modify: `bot-src/webapp/static/js/app.js:3168-3227` (top-up sheet)
- Modify: `bot-src/webapp/templates/index.html` (разметка `#tu-overlay`, cache-bust `?v=`)
- Modify: `bot-src/webapp/static/js/i18n.js` (ключи ru/en/es)

**Interfaces:**
- Consumes: `GET /api/pay/providers`, `GET /api/topup/create` (provider:"payme"), `t()`, `authHeaders()`.
- Produces: строка оплаты Payme в шторке (видна только если `providers.payme`), переключение цен на сумы.

- [ ] **Step 1: Добавить UZS-прайс и загрузку доступности провайдеров**

В `app.js` рядом с `PKG_PRICE` (строка 3169) добавить:
```javascript
var PKG_PRICE_UZS = {"100":16900,"300":49000,"500":79000,"1000":149000,"2000":279000,"5000":649000};
var payProviders = {yoomoney:true, platega:false, payme:false};
function fmtUzs(n){ return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, " ") + " сум"; }
(function(){ fetch("/api/pay/providers",{headers:authHeaders({})})
    .then(function(r){return r.ok?r.json():null;})
    .then(function(d){ if(d){ payProviders=d;
        var row=document.getElementById("tu-pay-payme"); if(row) row.classList.toggle("hidden", !d.payme); } })
    .catch(function(){}); })();
```

- [ ] **Step 2: Показывать цену Payme в сумах при открытии шторки**

В `tuOpen(pkg)` (после строки 3181, где ставятся `tu-price-sbp/card`) добавить:
```javascript
    setText("tu-price-payme", fmtUzs(PKG_PRICE_UZS[pkg] || 0));
    var pr = document.getElementById("tu-pay-payme"); if (pr) pr.classList.toggle("hidden", !payProviders.payme);
```

- [ ] **Step 3: Выбор провайдера в startPay**

В `startPay(method)` заменить тело fetch-запроса (строка 3195-3199) так, чтобы `method:"payme"` слал нужный провайдер:
```javascript
        var provider = (method === "payme") ? "payme" : "yoomoney";
        var res = await fetch("/api/topup/create", {
            method: "POST",
            headers: authHeaders({"Content-Type": "application/json"}),
            body: JSON.stringify({ package: selectedPkg, provider: provider,
                                   method: (provider === "payme" ? undefined : method),
                                   promo_id: activePromoId || undefined })
        });
```

- [ ] **Step 4: Разметка строки Payme в index.html**

В `#tu-overlay`, рядом с кнопками `.pay-m.primary` (СБП/Карта), добавить (класс `hidden` по умолчанию — JS снимет, если доступно):
```html
<div class="pay-m primary hidden" id="tu-pay-payme" data-pm="payme">
  <span data-i18n="payViaPayme">Оплатить через Payme</span>
  <span id="tu-price-payme"></span>
</div>
```
Существующий обработчик `document.querySelectorAll("#tu-overlay .pay-m.primary").forEach(... startPay(b.dataset.pm))` (строка 3224) подхватит `data-pm="payme"` автоматически.
Бампнуть версии: `app.js?v=N+1`, `index.html` ассеты (и `i18n.js?v=` если правится).

- [ ] **Step 5: i18n-ключи**

В `i18n.js` добавить в каждый словарь (ru/en/es) ключ `payViaPayme`:
- ru: `"Оплатить через Payme"` · en: `"Pay with Payme"` · es: `"Pagar con Payme"`.

- [ ] **Step 6: Ручная проверка в браузере**

Локально с заданными `PAYME_*` (или временно `payme_available` → true): открыть шторку пополнения — строка Payme видна, цена в сумах; без ключей — строка скрыта, рублёвые способы работают как раньше.

- [ ] **Step 7: Commit**

```bash
git add webapp/static/js/app.js webapp/templates/index.html webapp/static/js/i18n.js
git commit -m "feat(payme): top-up sheet — Payme option with UZS pricing (gated by availability)"
```

---

### Task 6: Песочница Payme + запуск

**Files:** нет (операционная задача; env правит владелец).

- [ ] **Step 1:** В прод `.env` добавить `PAYME_MERCHANT_ID`, `PAYME_KEY_TEST`, `PAYME_MODE=test` (отдать владельцу). `bash srv2.sh gitdeploy` уже сделан на чекпоинте Task 4.
- [ ] **Step 2:** Прогнать тест-кейсы песочницы (developer.help.paycom.uz/pesochnitsa): CheckPerform (ок/неверная сумма/чужой order), Create (новый/повторный с тем же id → идемпотентность), Perform (двойной вызов не начисляет повторно), Cancel (до и после Perform), Check, GetStatement.
- [ ] **Step 3:** Проверить в БД: после Perform — `payme_transactions.state=2`, `payments.status='paid'`, `users.balance` +токены; повторный Perform баланс не меняет.
- [ ] **Step 4:** Инфра-хвост исполнителю: не блокировать `/api/pay/payme` в Cloudflare (Bot Fight Mode / WAF skip).
- [ ] **Step 5:** После приёмки: боевой ключ `PAYME_KEY_LIVE`, `PAYME_MODE=live`, рестарт. Контрольная боевая оплата минимального пакета.

---

## Self-Review

**Spec coverage:**
- Входящий endpoint + 6 методов + auth + HTTP 200 → Task 3 (handle/методы), Task 4 (роут). ✔
- Checkout base64-ссылка → Task 3 `build_checkout_url`, Task 4 create-ветка. ✔
- Модель данных (currency/amount_uzs + payme_transactions) → Task 1. ✔
- Идемпотентное начисление на Perform, гард 1→2 → Task 2 `payme_perform_txn`. ✔
- Cancel с откатом начисления → Task 2 `payme_cancel_txn`. ✔
- UZS-прайс (точные суммы) → Task 3 `PACKAGES_UZS`, Task 5 `PKG_PRICE_UZS`. ✔
- UI: Payme всем, цены в сумах, кнопка скрыта без ключей → Task 5 + `/api/pay/providers` (Task 4). ✔
- Коды ошибок (-32504/-31001/-31050/-31003/-31008/-31007) → Task 3 константы + методы. ✔
- Статус без outbound для payme → отмечено в Task 4 Step 3. ✔
- Фаза 1 без UZS-рефералки → нигде не вызывается реферальная логика в payme-путях. ✔
- Деплой скелета до отдачи URL (вариант «а») → чекпоинт после Task 4. ✔
- Cloudflare skip → Task 6 Step 4 (инфра-хвост). ✔

**Placeholder scan:** код во всех шагах конкретный; «TBD/TODO» нет.

**Type consistency:** имена функций/полей согласованы между Task 2 (queries) и Task 3 (вызовы `q.*`): `payme_order_by_id`, `payme_txn_by_id`, `payme_active_txn_for_order`, `payme_insert_txn`, `payme_perform_txn`, `payme_cancel_txn`, `payme_list_txns`. Возвращаемые ключи (`payment_id`, `user_tg_id`, `total_tokens`, `credited`, `state`, `perform_time`, `cancel_time`) совпадают с использованием в `payme_gw`. `PACKAGES_UZS` ключи = id пакетов из `pg.PACKAGES`. ✔

**Известное упрощение (осознанное):** ширина `payments.amount_rub` остаётся NOT NULL — для UZS-заказов пишем `0` (сумма в `amount_uzs`); это не ломает рублёвые отчёты, но учитывать при аналитике выручки (UZS считать по `amount_uzs WHERE currency='UZS'`).
