"""Payme (Paycom) Merchant API — входящий JSON-RPC провайдер (оплата в сумах, UZS).

Payme сам вызывает наш endpoint (CheckPerform/Create/Perform/Cancel/Check/GetStatement).
Исходящего "создать платёж" нет — мы лишь строим base64-ссылку на checkout. Деньги в
тийинах (1 сум = 100 тийин). Спека: docs/specs/2026-06-30-payme-merchant-api-design.md.
"""
import os
import time
import uuid
import base64
import hmac
import logging

try:
    from db import queries as q
except Exception:        # asyncpg may be absent in a test/dev env without DB drivers
    q = None

logger = logging.getLogger(__name__)

# ── Config ──
PAYME_MERCHANT_ID = os.getenv("PAYME_MERCHANT_ID", "")
PAYME_KEY_TEST = os.getenv("PAYME_KEY_TEST", "")
PAYME_KEY_LIVE = os.getenv("PAYME_KEY_LIVE", "")
PAYME_MODE = os.getenv("PAYME_MODE", "test")            # test | live
PAYME_CHECKOUT_BASE = os.getenv("PAYME_CHECKOUT_BASE", "https://checkout.paycom.uz")
# Show the Payme button to users. Decoupled from key-presence so the cashbox can be
# wired for sandbox testing (keys set) WITHOUT exposing a half-baked button in prod.
# Flip to "1" at go-live, after sandbox acceptance.
PAYME_PUBLIC = os.getenv("PAYME_PUBLIC", "") == "1"

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
    """Keys configured — endpoint can authenticate Payme and create orders."""
    return bool(PAYME_MERCHANT_ID and _active_key())


def payme_public() -> bool:
    """Show the Payme option to users (gated separately from key-presence)."""
    return payme_available() and PAYME_PUBLIC


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


def _now_ms() -> int:
    return int(time.time() * 1000)


async def _check_order(account: dict, amount_tiyin: int) -> dict:
    """Общая проверка для CheckPerform/Create. Возвращает order dict или бросает PaymeError."""
    order_id = (account or {}).get("order_id")
    if not order_id:
        raise PaymeError(ERR_ACCOUNT, _MSG_ORDER, "order_id")
    # Our order_id is always a uuid4. A non-UUID account value can't match any
    # order — treat it as "order not found" instead of letting asyncpg raise on
    # the UUID column (which would surface as a generic -32400).
    try:
        uuid.UUID(str(order_id))
    except (ValueError, AttributeError, TypeError):
        raise PaymeError(ERR_ACCOUNT, _MSG_ORDER, "order_id")
    order = await q.payme_order_by_id(str(order_id))
    if not order:
        raise PaymeError(ERR_ACCOUNT, _MSG_ORDER, "order_id")
    if not isinstance(amount_tiyin, int):
        raise PaymeError(ERR_AMOUNT, "Неверная сумма")
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
    if res.get("error") == "amount_mismatch":
        raise PaymeError(ERR_CANT_PERFORM, "Сумма транзакции не совпадает с заказом")
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
