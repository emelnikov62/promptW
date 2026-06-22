"""Payment gateways: ЮMoney (Quickpay, RF) and Platega (CIS).

Top-up flow:
  1. /api/topup/create builds a provider payment for a fixed token package and
     returns a URL to open.
  2. The provider calls back (ЮMoney HTTP-notification / Platega callback); the
     route verifies the signature and calls settle_payment() to credit tokens
     and referral commissions.

Keys come from .env; if a provider's keys are missing it is reported as
unavailable rather than crashing.
"""
import os
import base64
import hmac
import logging

import aiohttp

logger = logging.getLogger(__name__)

# ── Token packages (single source of truth for top-up) ──
# id -> (tokens, price in rubles). Mirrors the cards on the top-up page.
PACKAGES = {
    "100":  (100, 106),
    "300":  (300, 307),
    "500":  (500, 498),
    "1000": (1000, 954),
    "2000": (2000, 1802),
    "5000": (5000, 4240),
}

# ── ЮKassa (REST API, RF) ──
# shopId + secret key (test_… or live_…) from the ЮKassa dashboard.
YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID", "")
YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY", "")
YOOKASSA_API = "https://api.yookassa.ru/v3/payments"


def yookassa_available() -> bool:
    return bool(YOOKASSA_SHOP_ID and YOOKASSA_SECRET_KEY)


def _yk_auth() -> str:
    raw = f"{YOOKASSA_SHOP_ID}:{YOOKASSA_SECRET_KEY}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


# ЮKassa payment_method_data.type per UI choice. None = let ЮKassa show all.
YK_METHODS = {"sbp": "sbp", "bank_card": "bank_card"}


async def yookassa_create(amount_rub, order_id: str, return_url: str,
                          description: str, method: str = None) -> tuple:
    """Create a ЮKassa payment (redirect flow). Returns (confirmation_url,
    payment_id) or (None, None) on failure. order_id doubles as the
    Idempotence-Key and is echoed back via metadata. `method` pins СБП or card."""
    headers = {
        "Authorization": _yk_auth(),
        "Idempotence-Key": order_id,
        "Content-Type": "application/json",
    }
    amount_str = f"{int(amount_rub)}.00"
    body = {
        "amount": {"value": amount_str, "currency": "RUB"},
        "capture": True,
        "confirmation": {"type": "redirect", "return_url": return_url},
        "description": description,
        "metadata": {"order_id": order_id},
        "receipt": {
            "customer": {"email": "payments@promptw.ru"},
            "items": [{
                "description": description[:128],
                "quantity": "1.00",
                "amount": {"value": amount_str, "currency": "RUB"},
                "vat_code": 1,
                "payment_subject": "service",
                "payment_mode": "full_payment",
            }],
        },
    }
    if method in YK_METHODS:
        body["payment_method_data"] = {"type": YK_METHODS[method]}
    try:
        async with aiohttp.ClientSession() as s:
            url, pid, retry = await _yk_post(s, headers, body)
            if retry and "payment_method_data" in body:
                # The pinned method isn't enabled on the shop — fall back to the
                # universal checkout (ЮKassa shows whatever methods are available).
                body.pop("payment_method_data", None)
                # a fresh idempotence key so ЮKassa treats it as a new request
                headers = dict(headers, **{"Idempotence-Key": order_id + "-u"})
                url, pid, _ = await _yk_post(s, headers, body)
            return url, pid
    except Exception:
        logger.exception("YooKassa create error")
        return None, None


async def _yk_post(session, headers, body):
    """POST a payment. Returns (url, id, retry_without_method)."""
    async with session.post(YOOKASSA_API, json=body, headers=headers, timeout=20) as r:
        data = await r.json()
        if r.status >= 300:
            logger.error("YooKassa create failed %s: %s", r.status, str(data)[:200])
            retry = str(data.get("description", "")).lower().find("not available") >= 0
            return None, None, retry
        conf = data.get("confirmation") or {}
        return conf.get("confirmation_url"), data.get("id"), False


async def yookassa_verify(payment_id: str) -> tuple:
    """Re-fetch a payment from ЮKassa (authoritative — webhooks are unsigned).
    Returns (succeeded: bool, order_id or None, amount_rub or None). The amount is
    cross-checked against the stored order before crediting, so a tampered/replayed
    notification can't settle an order for a different sum."""
    if not payment_id or not yookassa_available():
        return False, None, None
    headers = {"Authorization": _yk_auth()}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{YOOKASSA_API}/{payment_id}", headers=headers, timeout=20) as r:
                data = await r.json()
                if r.status >= 300:
                    return False, None, None
                ok = data.get("status") == "succeeded" and data.get("paid") is True
                order_id = (data.get("metadata") or {}).get("order_id")
                # amount.value is a decimal string like "498.00"; we store integer rubles.
                amount = None
                try:
                    raw = (data.get("amount") or {}).get("value")
                    amount = int(round(float(raw))) if raw is not None else None
                except (ValueError, TypeError):
                    amount = None
                return ok, order_id, amount
    except Exception:
        logger.exception("YooKassa verify error")
        return False, None, None


# ── Platega ──
PLATEGA_BASE = os.getenv("PLATEGA_BASE", "https://app.platega.io")
PLATEGA_MERCHANT_ID = os.getenv("PLATEGA_MERCHANT_ID", "")
PLATEGA_SECRET = os.getenv("PLATEGA_SECRET", "")
# Default payment method: 11 = card acquiring (see docs for 2=SBP, 13=crypto).
PLATEGA_METHOD = int(os.getenv("PLATEGA_METHOD", "11"))


def platega_available() -> bool:
    return bool(PLATEGA_MERCHANT_ID and PLATEGA_SECRET)


async def platega_create(amount_rub, payload: str, return_url: str,
                         fail_url: str, description: str,
                         method: int = None) -> tuple:
    """Create a Platega transaction. Returns (redirect_url, transaction_id) or
    (None, None) on failure."""
    headers = {
        "X-MerchantId": PLATEGA_MERCHANT_ID,
        "X-Secret": PLATEGA_SECRET,
        "Content-Type": "application/json",
    }
    body = {
        "paymentMethod": method or PLATEGA_METHOD,
        "paymentDetails": {"amount": int(amount_rub), "currency": "RUB"},
        "description": description,
        "return": return_url,
        "failedUrl": fail_url,
        "payload": payload,           # our order_id, echoed back in the callback
    }
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(PLATEGA_BASE + "/transaction/process",
                              json=body, headers=headers, timeout=20) as r:
                data = await r.json()
                if r.status >= 300:
                    logger.error("Platega create failed %s: %s", r.status, str(data)[:200])
                    return None, None
                return data.get("redirect"), data.get("transactionId")
    except Exception:
        logger.exception("Platega create error")
        return None, None


def verify_platega(headers) -> bool:
    """Platega signs callbacks by echoing our own X-MerchantId / X-Secret.
    This is a COARSE filter (the secret is static and reused), not authentication —
    always confirm the transaction with platega_verify() before crediting."""
    if not platega_available():
        return False
    return (hmac.compare_digest(headers.get("X-MerchantId", ""), PLATEGA_MERCHANT_ID)
            and hmac.compare_digest(headers.get("X-Secret", ""), PLATEGA_SECRET))


# Path template for the transaction-status lookup. Override via env once confirmed
# against Platega's live API docs. {id} is replaced with the transaction id.
PLATEGA_STATUS_PATH = os.getenv("PLATEGA_STATUS_PATH", "/transaction/{id}")


async def platega_verify(transaction_id: str) -> tuple:
    """Authoritatively re-fetch a Platega transaction. Returns (confirmed, amount_rub).
    Fail-closed: any error / unexpected shape returns (False, None) so a forged
    callback can never settle a payment on the header check alone."""
    if not transaction_id or not platega_available():
        return False, None
    headers = {"X-MerchantId": PLATEGA_MERCHANT_ID, "X-Secret": PLATEGA_SECRET}
    url = PLATEGA_BASE + PLATEGA_STATUS_PATH.replace("{id}", str(transaction_id))
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, headers=headers, timeout=20) as r:
                if r.status >= 300:
                    logger.warning("Platega status %s for %s", r.status, transaction_id)
                    return False, None
                data = await r.json()
                confirmed = str(data.get("status", "")).upper() == "CONFIRMED"
                details = data.get("paymentDetails") or {}
                amount = data.get("amount") or details.get("amount")
                try:
                    amount = int(amount) if amount is not None else None
                except (ValueError, TypeError):
                    amount = None
                return confirmed, amount
    except Exception:
        logger.exception("Platega verify error")
        return False, None
