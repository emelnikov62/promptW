import os
import json
import uuid
import asyncio
import logging
from typing import Optional
from datetime import datetime, date
from decimal import Decimal

import aiohttp
from aiohttp import web
from aiogram import Bot
from aiogram.types import FSInputFile

from generators.base import BaseGenerator
from bot.config import BOT_TOKEN
from bot.auth import validate_init_data, verify_auth_token
from pricing import compute_cost, CHAT_COST
from db.queries import (
    get_user, upsert_user, update_user_lang, update_balance, try_charge,
    create_generation, update_generation, delete_generation, get_user_generations,
    get_user_transactions, get_referral_stats, get_partner_overview,
    create_payment, set_payment_external, settle_payment, get_payment,
    get_latest_pending_payment,
    create_withdrawal, list_withdrawals, set_withdrawal_status, has_pending_withdrawal,
    list_references, add_reference, delete_reference,
    list_chat_dialogs, get_chat_dialog, append_chat_turn, delete_chat_dialog,
)
import payments_gw as pg

logger = logging.getLogger(__name__)
routes = web.RouteTableDef()
generator: Optional[BaseGenerator] = None
_bot: Optional[Bot] = None

MEDIA_DIR = os.getenv("MEDIA_DIR", "/tmp")

# Upload hardening: stream to disk with a hard size cap, cap the file count, and
# only keep known media extensions — anything else is stored as ".bin" so it can
# never be served as HTML/SVG (stored-XSS) from the public /media path.
_UPLOAD_EXT_OK = {
    ".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".gif",
    ".mp4", ".mov", ".webm", ".m4v",
    ".mp3", ".wav", ".m4a", ".ogg", ".aac", ".flac",
}
_UPLOAD_MAX_BYTES = 60 * 1024 * 1024   # 60 MB per file
_UPLOAD_MAX_FILES = 16                  # per request

# Telegram WebApp auth: validate initData on /api/* and trust the user id from
# the signed payload instead of the request body. Set AUTH_ENFORCE=0 as an
# escape hatch to fall back to the (insecure) body-supplied tg_id.
AUTH_ENFORCE = os.getenv("AUTH_ENFORCE", "1") == "1"
# Public /api/ paths that must work without a Telegram user (health + provider callbacks)
_AUTH_SKIP = ("/api/health", "/api/callback", "/api/pay/yoomoney", "/api/pay/platega")

WEBAPP_URL = os.getenv("WEBAPP_URL", "")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")
# Telegram ids allowed to view/process withdrawals.
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}
# Minimum withdrawal per method, in rubles.
WD_MIN = {"card": 1000, "crypto": 500}
# Absolute upper bound per request (sanity cap; the real limit is the ruble balance).
WD_MAX = int(os.getenv("WD_MAX", "500000"))

# ── Simple in-memory rate limiting (fixed window, single-process) ──
import time as _time
_RL = {}  # key -> [window_start, count]


def _rate_ok(key: str, limit: int, window: int) -> bool:
    now = _time.time()
    rec = _RL.get(key)
    if rec is None or now - rec[0] >= window:
        if len(_RL) > 5000:   # bound memory: drop stale windows
            for k in [k for k, v in _RL.items() if now - v[0] >= window]:
                _RL.pop(k, None)
        _RL[key] = [now, 1]
        return True
    if rec[1] >= limit:
        return False
    rec[1] += 1
    return True


# A client can spoof X-Forwarded-For, so by default key rate limits on the real
# peer address (non-forgeable). Set TRUST_XFF=1 only behind a trusted proxy that
# appends the real client IP — then use the LAST (nearest) hop, not the first.
TRUST_XFF = os.getenv("TRUST_XFF", "0") == "1"


def _client_ip(request: web.Request) -> str:
    if TRUST_XFF:
        xff = request.headers.get("X-Forwarded-For", "")
        if xff:
            return xff.split(",")[-1].strip()
    return request.remote or "?"


def _too_many():
    return web.json_response({"error": "rate_limited"}, status=429)


@web.middleware
async def auth_middleware(request: web.Request, handler):
    path = request.path
    if not path.startswith("/api/") or path in _AUTH_SKIP:
        return await handler(request)
    info = validate_init_data(request.headers.get("X-Init-Data", ""), BOT_TOKEN)
    if info and info.get("user"):
        request["tg_id"] = info["user"].get("id")
    else:
        # Fallback for clients that don't expose initData (some Telegram Desktop
        # builds): a bot-issued HMAC token carried in the WebApp URL.
        tok_id = verify_auth_token(request.headers.get("X-Auth-Token", ""), BOT_TOKEN)
        if tok_id is not None:
            request["tg_id"] = tok_id
        elif AUTH_ENFORCE:
            return web.json_response({"error": "unauthorized"}, status=401)
    return await handler(request)


def _authed_id(request: web.Request, fallback=None):
    """The Telegram-verified user id (preferred), or the given fallback when
    auth is not enforced."""
    return request.get("tg_id") if request.get("tg_id") is not None else fallback


def _int_or_none(val):
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


# Billing: deduct tokens before generation, refund on failure. Set BILLING_ENFORCE=0
# to compute & record cost without deducting/blocking (safe rollout / no payments yet).
BILLING_ENFORCE = os.getenv("BILLING_ENFORCE", "1") == "1"


async def _charge(tg_id, cost: int, label: str):
    """Reserve `cost` tokens. Returns (ok, new_balance). ok=False -> insufficient funds."""
    if not BILLING_ENFORCE or not tg_id or cost <= 0:
        return True, None
    balance = await try_charge(tg_id, cost, label)
    if balance is None:
        return False, None
    return True, balance


async def _refund(tg_id, cost: int, label: str):
    if BILLING_ENFORCE and tg_id and cost > 0:
        try:
            await update_balance(tg_id, cost, "refund", label)
        except Exception:
            logger.exception("refund failed for %s (%s tokens)", tg_id, cost)


async def _insufficient(tg_id, cost: int):
    user = await get_user(tg_id) if tg_id else None
    balance = user.get("balance") if user else 0
    return web.json_response(
        {"error": "insufficient_balance", "needed": cost, "balance": balance},
        status=402,
    )


# Generations run for minutes (video especially). Awaiting the work directly in
# the request handler meant a client disconnect (Telegram webview / nginx read
# timeout) cancelled the handler coroutine mid-flight — the KIE task was abandoned,
# the file never downloaded, the DB row stuck "pending" and the tokens spent.
# Instead run `build` (generate + persist 'done') as a DETACHED task and only
# shield-await it for the connected response: if the client goes away the task
# keeps running to completion, writes the result (or refunds + marks 'error' on
# failure), and the client picks it up via the History poll.
_BG_GENS = set()


async def _run_generation(gen_id, tg_id, cost: int, label: str, build):
    """Run `build()` to completion regardless of whether the HTTP client stays
    connected. `build` returns the JSON-able response dict (and writes the 'done'
    row itself). On failure: mark the row 'error', refund, return an error marker."""
    async def _runner():
        try:
            return await build()
        except Exception:
            logger.exception("Generation failed (%s)", label)
            if gen_id:
                await update_generation(gen_id, "error")
            await _refund(tg_id, cost, "refund " + label)
            return {"__error__": True}

    task = asyncio.ensure_future(_runner())
    _BG_GENS.add(task)
    task.add_done_callback(_BG_GENS.discard)
    # shield: if the client disconnects, our await is cancelled but `task` keeps
    # running in the background (and _BG_GENS holds a strong ref so it isn't GC'd).
    return await asyncio.shield(task)


def _get_bot() -> Bot:
    global _bot
    if _bot is None:
        _bot = Bot(token=BOT_TOKEN)
    return _bot


def setup(gen: BaseGenerator):
    global generator
    generator = gen


def _serialize(obj):
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        # NUMERIC columns (balances, ruble amounts) come back as Decimal — JSON can't encode it.
        return float(obj)
    if isinstance(obj, uuid.UUID):
        return str(obj)
    return obj


def _row_to_json(row: dict) -> dict:
    return {k: _serialize(v) for k, v in row.items()}


async def _parse_request(request: web.Request):
    """Parse both JSON and multipart form requests. Returns (data_dict, files_dict)."""
    files = {}
    if request.content_type and request.content_type.startswith("multipart/"):
        data = {}
        file_count = 0
        reader = await request.multipart()
        while True:
            part = await reader.next()
            if part is None:
                break
            if part.filename:
                file_count += 1
                if file_count > _UPLOAD_MAX_FILES:
                    raise web.HTTPBadRequest(text="too_many_files")
                ext = os.path.splitext(part.filename)[1].lower()
                if ext not in _UPLOAD_EXT_OK:
                    ext = ".bin"   # never let html/svg/etc. land on the public /media path
                fname = f"upload_{uuid.uuid4().hex[:12]}{ext}"
                fpath = os.path.join(MEDIA_DIR, fname)
                size = 0
                too_big = False
                with open(fpath, "wb") as f:
                    while True:
                        chunk = await part.read_chunk(8192)
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > _UPLOAD_MAX_BYTES:
                            too_big = True
                            break
                        f.write(chunk)
                if too_big:
                    try: os.remove(fpath)
                    except OSError: pass
                    continue   # silently drop the oversize file
                key = part.name
                if key in files:
                    if not isinstance(files[key], list):
                        files[key] = [files[key]]
                    files[key].append(fpath)
                else:
                    files[key] = fpath
            else:
                val = await part.text()
                data[part.name] = val
        if "settings" in data:
            try:
                data["settings"] = json.loads(data["settings"])
            except (json.JSONDecodeError, TypeError):
                data["settings"] = {}
        if "tg_id" in data:
            try:
                data["tg_id"] = int(data["tg_id"]) if data["tg_id"] else None
            except (ValueError, TypeError):
                data["tg_id"] = None
    else:
        try:
            data = await request.json()
        except Exception:
            raise web.HTTPBadRequest(text="bad_json")
    return data, files


@routes.get("/api/user/{tg_id}")
async def api_get_user(request: web.Request):
    tg_id = _authed_id(request, _int_or_none(request.match_info["tg_id"]))
    user = await get_user(tg_id)
    if not user:
        return web.json_response({"error": "user not found"}, status=404)
    return web.json_response(_row_to_json(user))


@routes.post("/api/user/{tg_id}/lang")
async def api_set_lang(request: web.Request):
    tg_id = _authed_id(request, _int_or_none(request.match_info["tg_id"]))
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "bad_json"}, status=400)
    lang = data.get("lang", "ru")
    await update_user_lang(tg_id, lang)
    return web.json_response({"ok": True, "lang": lang})


@routes.get("/api/user/{tg_id}/history")
async def api_get_history(request: web.Request):
    tg_id = _authed_id(request, _int_or_none(request.match_info["tg_id"]))
    limit = int(request.query.get("limit", "20"))
    offset = int(request.query.get("offset", "0"))
    rows = await get_user_generations(tg_id, limit, offset)
    result = []
    for r in rows:
        item = _row_to_json(r)
        if isinstance(item.get("settings"), str):
            try:
                item["settings"] = json.loads(item["settings"])
            except (json.JSONDecodeError, TypeError):
                item["settings"] = {}
        result.append(item)
    return web.json_response(result)


@routes.post("/api/generation/delete")
async def api_delete_generation(request: web.Request):
    data, _ = await _parse_request(request)
    tg_id = _authed_id(request, data.get("tg_id"))
    gen_id = _int_or_none(data.get("id"))
    if not tg_id or gen_id is None:
        return web.json_response({"error": "tg_id and id required"}, status=400)
    result_url = await delete_generation(gen_id, tg_id)
    if result_url is None:
        return web.json_response({"error": "not_found"}, status=404)
    # Best-effort cleanup of the stored media file (kept inside MEDIA_DIR).
    try:
        fpath = os.path.join(MEDIA_DIR, os.path.basename(result_url))
        if os.path.realpath(fpath).startswith(os.path.realpath(MEDIA_DIR) + os.sep) and os.path.isfile(fpath):
            os.remove(fpath)
    except OSError:
        pass
    return web.json_response({"ok": True})


@routes.get("/api/user/{tg_id}/transactions")
async def api_get_transactions(request: web.Request):
    tg_id = _authed_id(request, _int_or_none(request.match_info["tg_id"]))
    rows = await get_user_transactions(tg_id)
    return web.json_response([_row_to_json(r) for r in rows])


@routes.get("/api/user/{tg_id}/referrals")
async def api_get_referrals(request: web.Request):
    tg_id = _authed_id(request, _int_or_none(request.match_info["tg_id"]))
    return web.json_response(await get_partner_overview(tg_id))


# ── Saved reference photos ("Мой референс") ──
_REF_EXT_OK = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}
_REF_MAX_BYTES = 12 * 1024 * 1024  # 12 MB


def _media_url(path: str) -> str:
    return "/media/" + os.path.basename(path)


@routes.get("/api/user/{tg_id}/references")
async def api_list_references(request: web.Request):
    tg_id = _authed_id(request, _int_or_none(request.match_info["tg_id"]))
    rows = await list_references(tg_id)
    out = []
    for r in rows:
        item = _row_to_json(r)
        # stored as a filesystem path or already a /media url — normalise to url
        if item.get("file_url") and not str(item["file_url"]).startswith("/media/"):
            item["file_url"] = _media_url(item["file_url"])
        out.append(item)
    return web.json_response(out)


@routes.post("/api/references")
async def api_add_reference(request: web.Request):
    tg_id = _authed_id(request)
    if not tg_id:
        return web.json_response({"error": "unauthorized"}, status=401)
    data, files = await _parse_request(request)
    fpath = files.get("file")
    if isinstance(fpath, list):
        fpath = fpath[0] if fpath else None
    if not fpath:
        return web.json_response({"error": "no_file"}, status=400)
    # validate type + size; drop the file on any rejection
    ext = os.path.splitext(fpath)[1].lower()
    try:
        size = os.path.getsize(fpath)
    except OSError:
        size = 0
    if ext not in _REF_EXT_OK or size <= 0 or size > _REF_MAX_BYTES:
        try: os.remove(fpath)
        except OSError: pass
        return web.json_response({"error": "bad_file"}, status=400)
    title = (data.get("title") or "").strip()[:60] or None
    row = await add_reference(tg_id, _media_url(fpath), title)
    if row is None:
        try: os.remove(fpath)
        except OSError: pass
        return web.json_response({"error": "limit_reached"}, status=409)
    return web.json_response(_row_to_json(row))


@routes.delete("/api/references/{ref_id}")
async def api_delete_reference(request: web.Request):
    tg_id = _authed_id(request)
    if not tg_id:
        return web.json_response({"error": "unauthorized"}, status=401)
    file_url = await delete_reference(tg_id, int(request.match_info["ref_id"]))
    if file_url is None:
        return web.json_response({"error": "not_found"}, status=404)
    fpath = os.path.join(MEDIA_DIR, os.path.basename(file_url))
    try: os.remove(fpath)
    except OSError: pass
    return web.json_response({"ok": True})


# ── Top-up payments (ЮMoney RF / Platega CIS) ──
@routes.post("/api/topup/create")
async def api_topup_create(request: web.Request):
    tg_id = _authed_id(request)
    if not tg_id:
        return web.json_response({"error": "unauthorized"}, status=401)
    if not _rate_ok("topup:" + str(tg_id), 15, 60):
        return _too_many()
    data, _ = await _parse_request(request)
    pkg_id = str(data.get("package", ""))
    provider = data.get("provider", "")
    pkg = pg.PACKAGES.get(pkg_id)
    if not pkg:
        return web.json_response({"error": "bad_package"}, status=400)
    tokens, amount_rub = pkg
    order_id = str(uuid.uuid4())
    desc = f"PromptW: {tokens} токенов"
    success_url = WEBAPP_URL or "https://t.me"

    if provider == "yoomoney":
        if not pg.yookassa_available():
            return web.json_response({"error": "provider_unavailable"}, status=503)
        method = data.get("method")
        await create_payment(order_id, tg_id, "yoomoney", amount_rub, tokens)
        url, pid = await pg.yookassa_create(amount_rub, order_id, success_url, desc, method)
        if not url:
            return web.json_response({"error": "provider_error"}, status=502)
        if pid:
            await set_payment_external(order_id, pid)
        return web.json_response({"url": url, "order_id": order_id})

    if provider == "platega":
        if not pg.platega_available():
            return web.json_response({"error": "provider_unavailable"}, status=503)
        await create_payment(order_id, tg_id, "platega", amount_rub, tokens)
        fail_url = success_url
        url, txid = await pg.platega_create(amount_rub, order_id, success_url, fail_url, desc)
        if not url:
            return web.json_response({"error": "provider_error"}, status=502)
        if txid:
            await set_payment_external(order_id, txid)
        return web.json_response({"url": url, "order_id": order_id})

    return web.json_response({"error": "bad_provider"}, status=400)


@routes.get("/api/topup/status")
async def api_topup_status(request: web.Request):
    """Confirm a top-up on the user's return from the payment page, without
    depending on the (unsigned, possibly-unconfigured) provider webhook.
    Re-fetches the payment from ЮKassa and settles it if it has succeeded."""
    tg_id = _authed_id(request)
    if not tg_id:
        return web.json_response({"error": "unauthorized"}, status=401)
    if not _rate_ok("paystat:" + str(tg_id), 60, 60):
        return _too_many()
    order_id = request.query.get("order_id", "")
    if order_id:
        pay = await get_payment(order_id)
        if not pay or pay["user_tg_id"] != tg_id:
            return web.json_response({"error": "not_found"}, status=404)
    else:
        # No order id (e.g. client lost it on redirect) — reconcile the user's
        # most recent pending top-up.
        pay = await get_latest_pending_payment(tg_id)
        if not pay:
            return web.json_response({"status": "none", "paid": False})
        order_id = str(pay["order_id"])
    if pay["status"] != "paid" and pay["external_id"]:
        if pay["provider"] == "yoomoney":
            ok, vorder = await pg.yookassa_verify(pay["external_id"])
            if ok and vorder == order_id:
                try:
                    await settle_payment(order_id, pay["external_id"], provider="yoomoney")
                    pay = await get_payment(order_id) or pay
                except Exception:
                    logger.exception("ЮKassa settle (status) failed for %s", order_id)
        elif pay["provider"] == "platega":
            confirmed, amount = await pg.platega_verify(pay["external_id"])
            if confirmed:
                try:
                    await settle_payment(order_id, pay["external_id"], provider="platega",
                                         expected_amount=amount)
                    pay = await get_payment(order_id) or pay
                except Exception:
                    logger.exception("Platega settle (status) failed for %s", order_id)
    paid = pay["status"] == "paid"
    user = await get_user(tg_id)
    balance = user["balance"] if user else None
    return web.json_response({"status": pay["status"], "paid": paid,
                              "tokens": pay["tokens"], "balance": balance})


@routes.post("/api/pay/yoomoney")
async def api_pay_yoomoney(request: web.Request):
    # ЮKassa webhooks are unsigned — re-fetch the payment from the API and trust
    # only that. We always answer 200 so ЮKassa stops retrying.
    if not _rate_ok("cb:" + _client_ip(request), 120, 60):
        return web.json_response({"ok": True})
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"ok": True})
    obj = data.get("object") or {}
    if data.get("event") == "payment.succeeded" and obj.get("id"):
        ok, order_id = await pg.yookassa_verify(obj["id"])
        if ok and order_id:
            try:
                await settle_payment(order_id, obj["id"])
            except Exception:
                logger.exception("ЮKassa settle failed for %s", order_id)
    return web.json_response({"ok": True})


@routes.post("/api/pay/platega")
async def api_pay_platega(request: web.Request):
    if not _rate_ok("cb:" + _client_ip(request), 120, 60):
        return _too_many()
    if not pg.verify_platega(request.headers):
        logger.warning("Platega callback with bad credentials")
        return web.json_response({"error": "forbidden"}, status=403)
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "bad_json"}, status=400)
    if data.get("status") == "CONFIRMED":
        order_id = data.get("payload") or ""
        txid = str(data.get("id", ""))
        if order_id and txid:
            # The header check is a coarse filter; authoritatively re-fetch from
            # Platega before crediting (mirrors the ЮKassa flow).
            confirmed, amount = await pg.platega_verify(txid)
            if confirmed:
                try:
                    await settle_payment(order_id, txid, provider="platega",
                                         expected_amount=amount)
                except Exception:
                    logger.exception("Platega settle failed for %s", order_id)
            else:
                logger.warning("Platega callback for %s not confirmed on re-fetch", order_id)
    return web.json_response({"ok": True})


# ── Withdrawals ──
@routes.post("/api/withdraw")
async def api_withdraw(request: web.Request):
    tg_id = _authed_id(request)
    if not tg_id:
        return web.json_response({"error": "unauthorized"}, status=401)
    if not _rate_ok("withdraw:" + str(tg_id), 5, 60):
        return _too_many()
    data, _ = await _parse_request(request)
    method = data.get("method", "")
    details = str(data.get("details", "")).strip()
    amount = _int_or_none(data.get("amount"))
    if method not in WD_MIN:
        return web.json_response({"error": "bad_method"}, status=400)
    if not details:
        return web.json_response({"error": "no_details"}, status=400)
    if not amount or amount < WD_MIN[method]:
        return web.json_response({"error": "below_min", "min": WD_MIN[method]}, status=400)
    if amount > WD_MAX:
        return web.json_response({"error": "above_max", "max": WD_MAX}, status=400)
    if await has_pending_withdrawal(tg_id):
        return web.json_response({"error": "pending_exists"}, status=409)
    wd = await create_withdrawal(tg_id, method, details[:200], amount)
    if wd is None:
        return web.json_response({"error": "insufficient_balance"}, status=402)
    return web.json_response({"ok": True, "id": wd["id"]})


@routes.get("/api/admin/withdrawals")
async def api_admin_withdrawals(request: web.Request):
    tg_id = _authed_id(request)
    if tg_id not in ADMIN_IDS:
        return web.json_response({"error": "forbidden"}, status=403)
    status = request.query.get("status", "pending")
    rows = await list_withdrawals(status or None)
    return web.json_response([_row_to_json(r) for r in rows])


@routes.post("/api/admin/withdrawals/{wd_id}")
async def api_admin_withdrawal_set(request: web.Request):
    tg_id = _authed_id(request)
    if tg_id not in ADMIN_IDS:
        return web.json_response({"error": "forbidden"}, status=403)
    data, _ = await _parse_request(request)
    status = data.get("status", "")
    if status not in ("paid", "rejected"):
        return web.json_response({"error": "bad_status"}, status=400)
    wd = await set_withdrawal_status(int(request.match_info["wd_id"]), status)
    if wd is None:
        return web.json_response({"error": "not_pending"}, status=409)
    return web.json_response(_row_to_json(wd))


@routes.get("/api/models")
async def api_get_models(request: web.Request):
    from generators.kie import IMAGE_MODELS, VIDEO_MODELS, AUDIO_MODELS
    return web.json_response({
        "image": list(IMAGE_MODELS.keys()),
        "video": list(VIDEO_MODELS.keys()),
        "audio": list(AUDIO_MODELS.keys()),
    })


# ── Text chat via OpenRouter ──
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
CHAT_MODELS = {
    "ChatGPT": "openai/gpt-5.3",
    "Gemini": "google/gemini-3.5-flash",
    "Grok": "x-ai/grok-4.3",
}
CHAT_SYSTEM = (
    "You are a helpful, friendly assistant inside the PromptW app. "
    "Be concise and clear. Always answer in the same language the user writes in."
)


@routes.post("/api/chat")
async def api_chat(request: web.Request):
    api_key = os.getenv("OPENROUTER_API_KEY", "")
    if not api_key:
        return web.json_response({"error": "OpenRouter API key not configured"}, status=500)
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "bad request"}, status=400)

    model_ui = data.get("model", "ChatGPT")
    model = CHAT_MODELS.get(model_ui, CHAT_MODELS["ChatGPT"])
    try:
        dialog_id = int(data["dialog_id"]) if data.get("dialog_id") else None
    except (ValueError, TypeError):
        dialog_id = None
    msgs = [
        {"role": m.get("role"), "content": str(m.get("content", ""))}
        for m in (data.get("messages") or [])
        if m.get("role") in ("user", "assistant") and m.get("content")
    ][-24:]
    payload = {"model": model, "messages": [{"role": "system", "content": CHAT_SYSTEM}] + msgs}
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": os.getenv("WEBAPP_URL", "https://t.me/promptW_bot"),
        "X-Title": "PromptW",
    }

    tg_id = _authed_id(request)
    if not tg_id:
        # Never run the (paid) OpenRouter call without a known, billable user.
        return web.json_response({"error": "unauthorized"}, status=401)
    if not _rate_ok("chat:" + str(tg_id), 30, 60):
        return _too_many()
    ok, balance = await _charge(tg_id, CHAT_COST, "chat")
    if not ok:
        return await _insufficient(tg_id, CHAT_COST)

    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(OPENROUTER_URL, json=payload, headers=headers,
                              timeout=aiohttp.ClientTimeout(total=120)) as resp:
                d = await resp.json()
                if resp.status != 200:
                    msg = (d.get("error") or {}).get("message") if isinstance(d, dict) else None
                    logger.warning("OpenRouter %s: %s", resp.status, d)
                    await _refund(tg_id, CHAT_COST, "refund chat")
                    return web.json_response({"error": msg or "Model error"}, status=502)
                choices = d.get("choices") if isinstance(d, dict) else None
                if not choices:
                    logger.warning("OpenRouter empty choices: %s", d)
                    await _refund(tg_id, CHAT_COST, "refund chat")
                    return web.json_response({"error": "empty_response"}, status=502)
                reply = (choices[0].get("message") or {}).get("content") or ""
                out = {"reply": reply, "balance": balance}
                if tg_id:
                    try:
                        user_text = msgs[-1]["content"] if msgs and msgs[-1]["role"] == "user" else ""
                        first_user = next((m["content"] for m in msgs if m["role"] == "user"), user_text)
                        saved = await append_chat_turn(tg_id, dialog_id, model_ui, user_text, reply, first_user)
                        if saved is None and dialog_id is not None:
                            saved = await append_chat_turn(tg_id, None, model_ui, user_text, reply, first_user)
                        if saved:
                            out["dialog_id"] = saved["id"]
                            out["user_at"] = saved["user_at"].isoformat()
                            out["assistant_at"] = saved["assistant_at"].isoformat()
                    except Exception:
                        logger.exception("chat persist failed")
                return web.json_response(out)
    except Exception as e:
        logger.exception("chat error")
        await _refund(tg_id, CHAT_COST, "refund chat")
        return web.json_response({"error": "chat_failed"}, status=500)


@routes.get("/api/chats")
async def api_chats_list(request: web.Request):
    tg_id = _authed_id(request, _int_or_none(request.query.get("tg_id")))
    if not tg_id:
        return web.json_response([])
    rows = await list_chat_dialogs(tg_id)
    return web.json_response([_row_to_json(r) for r in rows])


@routes.get("/api/chats/{id}")
async def api_chat_get(request: web.Request):
    tg_id = _authed_id(request, _int_or_none(request.query.get("tg_id")))
    dialog_id = _int_or_none(request.match_info["id"])
    if not tg_id or dialog_id is None:
        return web.json_response({"error": "not found"}, status=404)
    dialog = await get_chat_dialog(tg_id, dialog_id)
    if not dialog:
        return web.json_response({"error": "not found"}, status=404)
    return web.json_response(_row_to_json({**dialog, "messages": [_row_to_json(m) for m in dialog["messages"]]}))


@routes.delete("/api/chats/{id}")
async def api_chat_delete(request: web.Request):
    tg_id = _authed_id(request, _int_or_none(request.query.get("tg_id")))
    dialog_id = _int_or_none(request.match_info["id"])
    if not tg_id or dialog_id is None:
        return web.json_response({"error": "not found"}, status=404)
    ok = await delete_chat_dialog(tg_id, dialog_id)
    return web.json_response({"ok": ok})


@routes.post("/api/generate/image")
async def generate_image(request: web.Request):
    data, files = await _parse_request(request)
    prompt = data.get("prompt", "").strip()
    tg_id = _authed_id(request, data.get("tg_id"))
    model = data.get("model")
    settings = data.get("settings", {})

    if tg_id and not _rate_ok("gen:" + str(tg_id), 20, 60):
        return _too_many()
    if not prompt:
        return web.json_response({"error": "prompt is required"}, status=400)

    if files:
        file_refs = {}
        for key, val in files.items():
            if isinstance(val, list):
                file_refs[key] = [f"/media/{os.path.basename(p)}" for p in val]
            else:
                file_refs[key] = f"/media/{os.path.basename(val)}"
        settings["references"] = file_refs

    cost = compute_cost("photo", model, settings)
    ok, balance = await _charge(tg_id, cost, f"photo:{model}")
    if not ok:
        return await _insufficient(tg_id, cost)

    gen_id = None
    if tg_id:
        gen_id = await create_generation(tg_id, "photo", prompt, model, settings, cost)

    async def _build():
        result = await generator.generate_image(
            prompt,
            model=model,
            aspect_ratio=settings.get("ratio"),
            resolution=settings.get("quality"),
            count=settings.get("count", 1),
            files=files,
        )
        paths = result.file_paths or [result.file_path]
        file_urls = [f"/media/{os.path.basename(p)}" for p in paths]
        file_url = file_urls[0]
        if gen_id:
            await update_generation(gen_id, "done", file_url)
        return {
            "file_url": file_url,
            "file_urls": file_urls,
            "media_type": result.media_type,
            "prompt": result.prompt,
            "task_id": result.task_id,
            "urls": result.urls,
            "cost": cost,
            "balance": balance,
        }

    resp = await _run_generation(gen_id, tg_id, cost, f"photo:{model}", _build)
    if resp.get("__error__"):
        return web.json_response({"error": "generation_failed"}, status=500)
    return web.json_response(resp)


@routes.post("/api/generate/video")
async def generate_video(request: web.Request):
    data, files = await _parse_request(request)
    prompt = data.get("prompt", "").strip()
    tg_id = _authed_id(request, data.get("tg_id"))
    model = data.get("model")
    settings = data.get("settings", {})

    if tg_id and not _rate_ok("gen:" + str(tg_id), 20, 60):
        return _too_many()
    if not prompt:
        return web.json_response({"error": "prompt is required"}, status=400)

    if files:
        file_refs = {}
        for key, val in files.items():
            if isinstance(val, list):
                file_refs[key] = [f"/media/{os.path.basename(p)}" for p in val]
            else:
                file_refs[key] = f"/media/{os.path.basename(val)}"
        settings["references"] = file_refs

    cost = compute_cost("video", model, settings)
    ok, balance = await _charge(tg_id, cost, f"video:{model}")
    if not ok:
        return await _insufficient(tg_id, cost)

    gen_id = None
    if tg_id:
        gen_id = await create_generation(tg_id, "video", prompt, model, settings, cost)

    async def _build():
        result = await generator.generate_video(
            prompt,
            model=model,
            aspect_ratio=settings.get("ratio"),
            duration=settings.get("duration"),
            sound=settings.get("sound", False),
            mode=settings.get("mode"),
            resolution=settings.get("quality"),
            orientation=settings.get("orientation"),
            files=files,
        )
        file_url = f"/media/{os.path.basename(result.file_path)}"
        if gen_id:
            await update_generation(gen_id, "done", file_url)
        return {
            "file_url": file_url,
            "media_type": result.media_type,
            "prompt": result.prompt,
            "task_id": result.task_id,
            "urls": result.urls,
            "cost": cost,
            "balance": balance,
        }

    resp = await _run_generation(gen_id, tg_id, cost, f"video:{model}", _build)
    if resp.get("__error__"):
        return web.json_response({"error": "generation_failed"}, status=500)
    return web.json_response(resp)


@routes.post("/api/generate/audio")
async def generate_audio(request: web.Request):
    data, files = await _parse_request(request)
    prompt = data.get("prompt", "").strip()
    tg_id = _authed_id(request, data.get("tg_id"))
    model = data.get("model")
    settings = data.get("settings", {})

    if tg_id and not _rate_ok("gen:" + str(tg_id), 20, 60):
        return _too_many()
    if not prompt:
        return web.json_response({"error": "prompt is required"}, status=400)

    cost = compute_cost("audio", model, settings)
    ok, balance = await _charge(tg_id, cost, f"audio:{model}")
    if not ok:
        return await _insufficient(tg_id, cost)

    gen_id = None
    if tg_id:
        gen_id = await create_generation(tg_id, "audio", prompt, model, settings, cost)

    async def _build():
        result = await generator.generate_audio(
            prompt,
            model=model,
            custom_mode=settings.get("custom_mode", False),
            instrumental=settings.get("instrumental", False),
            vocal_gender=settings.get("vocal_gender"),
            style=settings.get("style"),
            title=settings.get("title"),
            lyrics=settings.get("lyrics"),
            negative_tags=settings.get("negative_tags"),
            style_weight=settings.get("style_weight"),
            weirdness=settings.get("weirdness"),
            audio_weight=settings.get("audio_weight"),
            files=files,
        )
        file_url = f"/media/{os.path.basename(result.file_path)}"
        if gen_id:
            await update_generation(gen_id, "done", file_url)
        return {
            "file_url": file_url,
            "media_type": result.media_type,
            "prompt": result.prompt,
            "task_id": result.task_id,
            "urls": result.urls,
            "cost": cost,
            "balance": balance,
        }

    resp = await _run_generation(gen_id, tg_id, cost, f"audio:{model}", _build)
    if resp.get("__error__"):
        return web.json_response({"error": "generation_failed"}, status=500)
    return web.json_response(resp)


@routes.post("/api/send-media")
async def api_send_media(request: web.Request):
    data, _ = await _parse_request(request)
    # Under AUTH_ENFORCE=1 (prod) this is the Telegram-verified requester; the
    # body-supplied id is only a DEV fallback when auth is disabled. Prod refuses
    # to boot with AUTH_ENFORCE=0 (see main.py), so the fallback never applies live.
    tg_id = _authed_id(request, data.get("tg_id"))
    file_url = data.get("file_url", "")
    media_type = data.get("media_type", "photo")

    if not tg_id or not file_url:
        return web.json_response({"error": "tg_id and file_url required"}, status=400)

    # Contain to MEDIA_DIR (basename strips traversal; realpath blocks edge cases).
    filepath = os.path.join(MEDIA_DIR, os.path.basename(file_url))
    if os.path.realpath(filepath).startswith(os.path.realpath(MEDIA_DIR) + os.sep) is False \
            or not os.path.isfile(filepath):
        return web.json_response({"error": "file not found"}, status=404)

    bot = _get_bot()
    try:
        if media_type == "video":
            await bot.send_video(tg_id, FSInputFile(filepath))
        elif media_type == "audio":
            await bot.send_audio(tg_id, FSInputFile(filepath))
        else:
            # Send the image as a document, NOT send_photo — Telegram re-compresses
            # photos (JPEG, downscaled) and would degrade the generated result. A
            # document preserves the original file (full resolution, no recompression).
            await bot.send_document(tg_id, FSInputFile(filepath))
        return web.json_response({"ok": True})
    except Exception:
        logger.exception("Send media error")
        return web.json_response({"error": "send_failed"}, status=500)


@routes.post("/api/share-media")
async def api_share_media(request: web.Request):
    """Prepare an inline message so the user can forward the result to ANY chat /
    friend via tg.shareMessage(). Returns {id} = prepared_message_id."""
    tg_id = _authed_id(request)
    if not tg_id:
        return web.json_response({"error": "unauthorized"}, status=401)
    if not _rate_ok("share:" + str(tg_id), 20, 60):
        return _too_many()
    data, _ = await _parse_request(request)
    file_url = data.get("file_url", "")
    media_type = data.get("media_type", "photo")
    if not file_url:
        return web.json_response({"error": "bad_request"}, status=400)
    # Only allow our own media files (no arbitrary-URL inline messages).
    fname = os.path.basename(file_url)
    filepath = os.path.join(MEDIA_DIR, fname)
    if not os.path.realpath(filepath).startswith(os.path.realpath(MEDIA_DIR) + os.sep) \
            or not os.path.isfile(filepath):
        return web.json_response({"error": "file_not_found"}, status=404)
    if not WEBAPP_URL:
        return web.json_response({"error": "unavailable"}, status=503)
    public_url = WEBAPP_URL.rstrip("/") + "/media/" + fname
    rid = uuid.uuid4().hex
    if media_type == "audio":
        result = {"type": "audio", "id": rid, "audio_url": public_url, "title": "PromptW"}
    elif media_type == "video":
        result = {"type": "video", "id": rid, "video_url": public_url,
                  "mime_type": "video/mp4", "thumbnail_url": public_url, "title": "PromptW"}
    else:
        result = {"type": "photo", "id": rid, "photo_url": public_url, "thumbnail_url": public_url}
    payload = {"user_id": tg_id, "result": result,
               "allow_user_chats": True, "allow_group_chats": True, "allow_channel_chats": True}
    api = f"https://api.telegram.org/bot{BOT_TOKEN}/savePreparedInlineMessage"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(api, json=payload, timeout=20) as r:
                d = await r.json()
        if not d.get("ok"):
            logger.warning("savePreparedInlineMessage failed: %s", str(d)[:200])
            return web.json_response({"error": "share_failed"}, status=502)
        return web.json_response({"id": d["result"]["id"]})
    except Exception:
        logger.exception("share-media error")
        return web.json_response({"error": "share_failed"}, status=500)


@routes.post("/api/callback")
async def api_callback(request: web.Request):
    # KIE provider callback — currently a no-op ack. Do NOT trust or log the raw
    # body; if state-mutating logic is added here it MUST verify a shared secret.
    if not _rate_ok("cb:" + _client_ip(request), 120, 60):
        return web.json_response({"ok": True})
    try:
        await request.json()
    except Exception:
        pass
    logger.debug("KIE callback received")
    return web.json_response({"ok": True})


@routes.get("/api/health")
async def health(request: web.Request):
    return web.json_response({"status": "ok"})
