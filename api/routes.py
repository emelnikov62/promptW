import os
import re
import json
import uuid
import asyncio
import logging
import mimetypes
from typing import Optional
from datetime import datetime, date, timezone
from decimal import Decimal

import aiohttp
import asyncpg
from aiohttp import web
from aiogram import Bot
from aiogram.types import FSInputFile, BufferedInputFile

from generators.base import BaseGenerator
from bot.config import BOT_TOKEN
from bot.auth import validate_init_data, verify_auth_token, verify_admin_token, make_auth_token
from pricing import compute_cost, CHAT_COST
from db.database import get_pool
from db.queries import (
    get_user, upsert_user, update_user_lang, update_balance, try_charge,
    charge_and_create_generation,
    update_generation, delete_generation, get_user_generations,
    add_generation_task, get_pending_generations,
    set_generation_task_ids, record_face_verify,
    finish_generation_if_pending, fail_generation_if_pending,
    get_user_transactions, get_referral_stats, get_partner_overview,
    create_payment, set_payment_external, settle_payment, get_payment,
    get_latest_pending_payment, create_payme_payment,
    create_withdrawal, list_withdrawals, set_withdrawal_status, has_pending_withdrawal,
    touch_active, set_notif_marketing, was_sent, log_sent, get_setting,
    eligible_bonus_unspent, eligible_reengage, eligible_reward_avail, eligible_weekly,
    list_references, add_reference, delete_reference,
    list_chat_dialogs, get_chat_dialog, append_chat_turn, delete_chat_dialog,
    list_templates_public, get_template_public,
    claim_reward, claimed_rewards,
    get_or_create_support_ticket, add_support_message,
    get_support_messages, get_support_ticket_for_user, close_support_ticket,
)
import payments_gw as pg
import payme_gw as pm
import storage
import face_verify
from bot import notify as notif

logger = logging.getLogger(__name__)
routes = web.RouteTableDef()
generator: Optional[BaseGenerator] = None
_bot: Optional[Bot] = None
_support_bot: Optional[Bot] = None

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
# How long a signed initData stays acceptable (replay window; signature still required).
# Generous by default so returning users aren't bounced to the "session expired" screen.
INITDATA_MAX_AGE = int(os.getenv("INITDATA_MAX_AGE_DAYS", "30")) * 86400
# Public /api/ paths that must work without a Telegram user (health + provider callbacks)
_AUTH_SKIP = ("/api/health", "/api/callback", "/api/pay/yoomoney", "/api/pay/platega",
              "/api/pay/payme", "/api/admin/login", "/api/media-proxy")

WEBAPP_URL = os.getenv("WEBAPP_URL", "")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")
# Telegram ids allowed to view/process withdrawals.
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}
# Minimum withdrawal per method, in rubles.
WD_MIN = {"card": 1000, "crypto": 500}
# Absolute upper bound per request (sanity cap; the real limit is the ruble balance).
WD_MAX = int(os.getenv("WD_MAX", "500000"))

# Rewards ("Награды"). Channel handles come from .env so they can be plugged in
# without a code deploy; an empty chat → reward is "not configured" → hidden in the
# UI. The bot MUST be an admin/member of a subscription channel for getChatMember.
REWARDS = {
    "share":   {"type": "story",        "amount": 30},
    "tg":      {"type": "subscription", "amount": 10, "chat": os.getenv("RWD_TG_CHANNEL", "").strip()},
    "prompts": {"type": "subscription", "amount": 10, "chat": os.getenv("RWD_PROMPTS_CHANNEL", "").strip()},
}
_SUBSCRIBED_STATUSES = {"member", "administrator", "creator"}


def _channel_link(chat: str) -> str:
    """Public t.me link for a @username channel (numeric -100… ids have no link)."""
    if chat.startswith("@"):
        return "https://t.me/" + chat[1:]
    return ""


async def _is_subscribed(chat: str, tg_id: int) -> bool:
    """True if tg_id is a member of `chat` per Bot API getChatMember. Raises on any
    API/transport error so the caller can return check_failed (never credit on doubt)."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getChatMember"
    timeout = aiohttp.ClientTimeout(total=8)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, params={"chat_id": chat, "user_id": tg_id}) as resp:
            data = await resp.json()
    if not data.get("ok"):
        raise RuntimeError("getChatMember failed: " + str(data.get("description")))
    member = data.get("result") or {}
    status = member.get("status")
    if status in _SUBSCRIBED_STATUSES:
        return True
    # "restricted" but still in the chat counts as subscribed.
    if status == "restricted" and member.get("is_member"):
        return True
    return False

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
    # refresh_id: normal-user id to roll a fresh fallback token for (not admins).
    refresh_id = None
    info = validate_init_data(request.headers.get("X-Init-Data", ""), BOT_TOKEN, max_age_sec=INITDATA_MAX_AGE)
    if info and info.get("user"):
        request["tg_id"] = info["user"].get("id")
        refresh_id = request["tg_id"]
    else:
        # Fallback for clients that don't expose initData (some Telegram Desktop
        # builds): a bot-issued HMAC token carried in the WebApp URL.
        token = request.headers.get("X-Auth-Token", "")
        tok_id = verify_auth_token(token, BOT_TOKEN)
        if tok_id is not None:
            request["tg_id"] = tok_id
            refresh_id = tok_id
        else:
            # Admin-session token (distinct namespace) — grants the admin panel
            # only; a normal user token can never set admin_scope.
            adm_id = verify_admin_token(token, BOT_TOKEN)
            if adm_id is not None:
                request["tg_id"] = adm_id
                request["admin_scope"] = True
            elif AUTH_ENFORCE:
                return web.json_response({"error": "unauthorized"}, status=401)
    resp = await handler(request)
    # Rolling refresh: hand the client a fresh fallback token on every authenticated
    # request so an active session never expires (kills the "session expired" screen
    # for anyone who keeps using the app). Best-effort; never break the response.
    if refresh_id is not None:
        try:
            resp.headers["X-Auth-Refresh"] = make_auth_token(refresh_id, BOT_TOKEN)
        except Exception:
            pass
    return resp


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


async def _charge_and_create(tg_id, gen_type: str, prompt: str, model,
                             settings, cost: int, label: str):
    """Reserve tokens AND create the pending generation row atomically (one tx), so a
    crash between the two can't strand spent tokens with no row to refund.
    Returns (ok, gen_id, balance). ok=False -> insufficient funds (nothing written)."""
    if not tg_id:
        # dev/anonymous (AUTH_ENFORCE=0): no row, no charge — matches legacy behaviour.
        return True, None, None
    charge = bool(BILLING_ENFORCE and cost > 0)
    gen_id, balance = await charge_and_create_generation(
        tg_id, gen_type, prompt, model, settings, cost, charge=charge, label=label)
    if gen_id is None:
        return False, None, None
    return True, gen_id, balance


# Generations run for minutes (video especially). Awaiting the work directly in
# the request handler meant a client disconnect (Telegram webview / nginx read
# timeout) cancelled the handler coroutine mid-flight — the KIE task was abandoned,
# the file never downloaded, the DB row stuck "pending" and the tokens spent.
# Instead run `build` (generate + persist 'done') as a DETACHED task and only
# shield-await it for the connected response: if the client goes away the task
# keeps running to completion, writes the result (or refunds + marks 'error' on
# failure), and the client picks it up via the History poll.
_BG_GENS = set()


def _task_saver(gen_id):
    """Callback handed to the generator: persists the provider task id the moment
    it's created, so a restart-killed generation can be recovered on next boot."""
    async def _on_task(task_id):
        if gen_id and task_id:
            try:
                await add_generation_task(gen_id, task_id)
            except Exception:
                logger.exception("failed to persist task id for gen %s", gen_id)
    return _on_task


async def _discard_media(paths):
    """Delete already-produced media (remote S3 object or local file) — used when the
    reconciler beat the live task to finalizing a row, so we don't keep an orphan."""
    for p in paths:
        if not p:
            continue
        try:
            if storage.is_remote(p):
                await storage.adelete_url(p)
            else:
                os.remove(p)
        except OSError:
            pass


async def _run_generation(gen_id, tg_id, cost: int, label: str, build):
    """Run `build()` to completion regardless of whether the HTTP client stays
    connected. `build` returns the JSON-able response dict (and writes the 'done'
    row itself). On failure: mark the row 'error', refund, return an error marker."""
    async def _runner():
        try:
            return await build()
        except TimeoutError:
            # The foreground poll gave up, but the provider task is still alive and its id
            # is persisted — let the reconciler recover it instead of killing + refunding a
            # job that may still finish (long Suno songs, or a backgrounded webview). The
            # row stays 'pending'; no refund. (asyncio.TimeoutError is builtin TimeoutError.)
            logger.warning("Generation foreground timeout (%s) — leaving row pending for reconciler", label)
            return {"__pending__": True}
        except Exception:
            logger.exception("Generation failed (%s)", label)
            # Guarded flip so the refund happens exactly once even if the
            # reconciliation sweep touches the same row concurrently.
            notify_owner = None
            if gen_id:
                info = await fail_generation_if_pending(gen_id)
                if info is not None:
                    await _refund(info["user_tg_id"], info["cost"] or 0, "refund " + label)
                    notify_owner = info["user_tg_id"]
            else:
                await _refund(tg_id, cost, "refund " + label)
                notify_owner = tg_id
            # Tell the author only if they've left the app (otherwise the in-app
            # toast already covers it). Fully isolated — never affects the refund.
            if notify_owner:
                try:
                    if not await _user_recently_active(notify_owner):
                        notif.notify_bg(notify_owner, "genFailTg", btn_key="retryBtn", page="create")
                except Exception:
                    pass
            return {"__error__": True}

    task = asyncio.ensure_future(_runner())
    _BG_GENS.add(task)
    task.add_done_callback(_BG_GENS.discard)
    # shield: if the client disconnects, our await is cancelled but `task` keeps
    # running in the background (and _BG_GENS holds a strong ref so it isn't GC'd).
    return await asyncio.shield(task)


# ── Reconciliation sweep ───────────────────────────────────────────────────
# The detached task above survives a client disconnect, but NOT a service restart
# (deploy) — that kills the process and every in-flight task with it, leaving the
# row stuck "pending" and the tokens spent. This sweep (run on boot + periodically)
# re-polls KIE by the persisted task id and either recovers the finished result or
# refunds + marks "error" for genuinely dead jobs.
_RECONCILE_MIN_AGE = 60          # let the live in-process task own very recent rows
_RECONCILE_GIVEUP = 1800         # 30 min still "processing" -> assume dead, refund
_RECONCILE_NOTASK_GIVEUP = 1200  # 20 min and no task id ever recorded -> refund


async def _reconcile_fail(gen_id, model, reason):
    info = await fail_generation_if_pending(gen_id)
    if info is not None:   # we won the pending->error flip -> refund exactly once
        await _refund(info["user_tg_id"], info["cost"] or 0, f"refund {model}: {reason}")
        logger.info("reconcile: gen %s failed (%s), refunded %s", gen_id, reason, info["cost"])


def _parse_task_ids(row) -> list:
    """The task ids to recover for a pending row: the full multi-image set
    (provider_task_ids JSONB) if present, else the legacy single id."""
    raw = row.get("provider_task_ids")
    ids = []
    if raw:
        try:
            ids = json.loads(raw) if isinstance(raw, str) else list(raw)
        except (json.JSONDecodeError, TypeError):
            ids = []
    ids = [i for i in ids if i]
    if not ids and row.get("provider_task_id"):
        ids = [row["provider_task_id"]]
    return ids


async def _reconcile_once():
    if generator is None:
        return
    try:
        rows = await get_pending_generations(limit=50)
    except Exception:
        logger.exception("reconcile: could not load pending generations")
        return
    now = datetime.now(timezone.utc)
    for row in rows:
        gen_id = row["id"]
        model = row["model"]
        created = row["created_at"]
        age = (now - created).total_seconds() if created else 1e9
        if age < _RECONCILE_MIN_AGE:
            continue
        task_ids = _parse_task_ids(row)
        if not task_ids:
            if age > _RECONCILE_NOTASK_GIVEUP:
                await _reconcile_fail(gen_id, model, "no task id")
            continue
        # Recover EVERY task — a multi-image gen is only "done" once all N are ready.
        paths, failed, incomplete = [], False, False
        for tid in task_ids:
            try:
                p = await generator.recover_task(tid, row["gen_type"], model)
            except Exception as e:
                logger.warning("reconcile: task %s (gen %s) failed: %s", tid, gen_id, e)
                failed = True
                break
            if p is None:        # this task still processing on KIE
                incomplete = True
                break
            paths.append(p)
        if failed:
            await _discard_media(paths)   # drop any siblings already downloaded
            await _reconcile_fail(gen_id, model, "provider task failed")
            continue
        if incomplete:
            # Not all ready yet — discard this pass's partial downloads (they'll be
            # re-fetched next sweep, so nothing is orphaned) and wait, unless too old.
            await _discard_media(paths)
            if age > _RECONCILE_GIVEUP:
                await _reconcile_fail(gen_id, model, "timeout")
            continue
        file_urls = [_media_url(p) for p in paths]
        if await finish_generation_if_pending(gen_id, file_urls[0], file_urls):
            logger.info("reconcile: recovered gen %s -> %d file(s)", gen_id, len(file_urls))
            await _notify_gen_ready(row["user_tg_id"], row["gen_type"], len(file_urls))
        else:
            await _discard_media(paths)   # another path finalized first — drop duplicates


async def _reconcile_loop(interval: int):
    while True:
        try:
            await _reconcile_once()
        except Exception:
            logger.exception("reconcile loop iteration failed")
        await asyncio.sleep(interval)


_RECONCILER_TASK = None


def start_reconciler(interval: int = 60):
    """Launch the background reconciliation loop (call once after the generator and
    DB pool are ready). Runs an immediate pass, then every `interval` seconds.
    A module-level strong ref is kept so the task can't be garbage-collected
    mid-flight (a bare ensure_future return value is weakly held by the loop)."""
    global _RECONCILER_TASK
    _RECONCILER_TASK = asyncio.ensure_future(_reconcile_loop(interval))
    return _RECONCILER_TASK


# ── Engagement sweep (Phase 2) ───────────────────────────────────────────────
# Periodically nudges eligible users (bonus-unspent / re-engage / reward-available)
# with brand TG messages, respecting opt-out, quiet hours and frequency caps.
# See docs/specs/2026-06-23-notification-delivery-phase2-design.md.
_ENGAGE_QUIET_START = 22   # no engagement sends 22:00–10:00 MSK
_ENGAGE_QUIET_END = 10
_ENGAGE_BATCH = 100        # max sends per kind per sweep (Telegram-friendly drip)


def _in_quiet_hours() -> bool:
    """True during 22:00–10:00 MSK (UTC+3) — engagement sends are paused then."""
    msk_hour = (datetime.now(timezone.utc).hour + 3) % 24
    return msk_hour >= _ENGAGE_QUIET_START or msk_hour < _ENGAGE_QUIET_END


def _rewards_configured() -> bool:
    return any(REWARDS[k].get("chat") for k in ("tg", "prompts"))


def _reward_avail_tokens() -> int:
    return sum(REWARDS[k]["amount"] for k in ("tg", "prompts") if REWARDS[k].get("chat"))


ENGAGEMENT_FLAG = "engagement_enabled"   # app_settings key; '0' = kill switch on


async def engagement_enabled() -> bool:
    return (await get_setting(ENGAGEMENT_FLAG, "1")) != "0"


async def run_weekly_broadcast(limit: int = 1000) -> int:
    """Send the 'new templates this week' nudge to eligible users (opted-in, active
    <30d, not sent in 7d). Throttled ~20/s. Returns the number sent. Shared by the
    public admin endpoint and the admin panel."""
    sent = 0
    for tid in await eligible_weekly(limit):
        notif.notify_bg(tid, "weeklyTg", btn_key="viewBtn", page="home")
        await log_sent(tid, "weekly")
        sent += 1
        await asyncio.sleep(0.05)
    return sent


async def _engagement_once():
    if _in_quiet_hours():
        return
    if not await engagement_enabled():   # admin kill switch
        return
    # bonus-unspent: new users sitting on tokens who never created
    try:
        for u in await eligible_bonus_unspent(_ENGAGE_BATCH):
            notif.notify_bg(u["tg_id"], "bonusUnspentTg", btn_key="seeTemplatesBtn",
                            page="home", n=u["balance"])
            await log_sent(u["tg_id"], "bonusUnspent")
    except Exception:
        logger.exception("engagement: bonusUnspent pass failed")
    # re-engage: previously active, gone quiet 7–60 days
    try:
        for tid in await eligible_reengage(_ENGAGE_BATCH):
            notif.notify_bg(tid, "reengageTg", btn_key="seeTemplatesBtn", page="home")
            await log_sent(tid, "reengage")
    except Exception:
        logger.exception("engagement: reengage pass failed")
    # reward-available: only when reward channels are actually configured
    if _rewards_configured():
        try:
            n = _reward_avail_tokens()
            for tid in await eligible_reward_avail(_ENGAGE_BATCH):
                notif.notify_bg(tid, "rewardAvailTg", btn_key="claimBtn",
                                page="rewards", n=n)
                await log_sent(tid, "rewardAvail")
        except Exception:
            logger.exception("engagement: rewardAvail pass failed")


async def _engagement_loop(interval: int):
    while True:
        await asyncio.sleep(interval)   # delay first run; nothing is urgent here
        try:
            await _engagement_once()
        except Exception:
            logger.exception("engagement loop iteration failed")


_ENGAGE_TASK = None


def start_engagement_sweep(interval: int = 3600):
    """Launch the hourly engagement sweep (call once after the DB pool is ready)."""
    global _ENGAGE_TASK
    _ENGAGE_TASK = asyncio.ensure_future(_engagement_loop(interval))
    return _ENGAGE_TASK


def _get_bot() -> Bot:
    global _bot
    if _bot is None:
        _bot = Bot(token=BOT_TOKEN)
    return _bot


def setup(gen: BaseGenerator, support_bot: Optional[Bot] = None):
    global generator, _support_bot
    generator = gen
    _support_bot = support_bot


# ── Notifications (transactional, Phase 1) ──────────────────────────────────
# See docs/specs/2026-06-23-notification-delivery-phase1-design.md. All sends are
# fire-and-forget and fail-safe (notif.notify_bg never raises).
_NOTIF_ACTIVE_SECS = 60   # if the user pinged the WebApp this recently, skip TG dupes


async def _user_recently_active(tg_id: int) -> bool:
    """True if the user is currently in the app (heartbeat < _NOTIF_ACTIVE_SECS ago),
    so a TG duplicate of an in-app toast should be suppressed. Fail-open to False."""
    try:
        user = await get_user(tg_id)
        ts = (user or {}).get("last_active_at")
        if not ts:
            return False
        return (datetime.now(timezone.utc) - ts).total_seconds() < _NOTIF_ACTIVE_SECS
    except Exception:
        return False


async def _settle_and_notify(order_id, external_id=None, provider=None, expected_amount=None):
    """Wrap settle_payment so a freshly-settled top-up notifies the buyer (payment
    receipt) and any referral earners. Returns settle_payment's result unchanged."""
    pay = await settle_payment(order_id, external_id, provider=provider,
                               expected_amount=expected_amount)
    if pay:   # non-None == settled on THIS call (idempotent: already-paid returns None)
        try:
            total = (pay.get("tokens") or 0) + (pay.get("bonus_tokens") or 0)
            notif.notify_bg(pay["user_tg_id"], "payTg", btn_key="payBtn", page="create",
                            n=total, k=max(1, total // 30))
            for c in pay.get("ref_credits") or []:
                notif.notify_bg(c["tg_id"], "refEarnTg", btn_key="partnerBtn", page="partner",
                                amount=_fmt_rub(c["amount"]))
        except Exception:
            logger.exception("payment notify failed for %s", order_id)
    return pay


def _fmt_rub(v) -> str:
    """Money for messages: drop the .0 on whole rubles (120.0 -> '120', 12.5 -> '12.5')."""
    try:
        f = float(v)
        return str(int(f)) if f == int(f) else ("%.2f" % f).rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return str(v)


async def _notify_gen_ready(tg_id: int, gen_type: str, count: int = 1):
    """Notify the author that a generation finished — unless they're still in the app
    (then the in-app toast already covers it)."""
    if not tg_id:
        return
    if await _user_recently_active(tg_id):
        return
    if gen_type == "photo":
        if count and count > 1:
            notif.notify_bg(tg_id, "photosTgN", btn_key="viewBtn", page="history", n=count)
        else:
            notif.notify_bg(tg_id, "photoTg", btn_key="viewBtn", page="history")
    elif gen_type == "video":
        notif.notify_bg(tg_id, "videoTg", btn_key="viewBtn", page="history")
    elif gen_type == "audio":
        notif.notify_bg(tg_id, "audioTg", btn_key="listenBtn", page="history")


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


# KIE/NanoBanana silently ignores an oversized reference image (e.g. a 12MP/6MB
# phone photo) and then generates from the prompt alone — a totally different
# face. Downscale uploaded images to a safe longest side before they ever reach
# the model. Verified: 3024x4032 ref was dropped; 960x1280 was applied 1:1.
# Refs already within the cap AND upright are left BYTE-FOR-BYTE untouched (no
# re-encode); only over-cap or EXIF-rotated images are rewritten, then saved
# near-lossless (quality=100).
_IMG_SHRINK_EXT = {".jpg", ".jpeg", ".png", ".webp"}
_IMG_MAX_SIDE = 2048
_EXIF_ORIENTATION = 0x0112


def _shrink_image(path: str):
    ext = os.path.splitext(path)[1].lower()
    if ext not in _IMG_SHRINK_EXT:
        return
    try:
        from PIL import Image, ImageOps
        # Decompression-bomb guard: a tiny upload can declare a huge canvas and
        # exhaust memory on .load(). Cap declared pixels before decoding; Pillow
        # raises DecompressionBombError past the limit (caught below → left as-is).
        Image.MAX_IMAGE_PIXELS = 64_000_000   # ~64 MP
        im = Image.open(path)
        im.load()
        # Phone cameras store the photo unrotated + an EXIF orientation tag. KIE
        # (and a PIL re-save) drop/ignore that tag, so the ref arrives rotated.
        # Bake the rotation into the pixels (exif_transpose drops the tag too).
        orient = im.getexif().get(_EXIF_ORIENTATION, 1)
        needs_rotate = orient not in (0, 1)
        over_cap = max(im.size) > _IMG_MAX_SIDE
        if not needs_rotate and not over_cap:
            return   # upright & within cap -> keep the original file untouched
        if needs_rotate:
            im = ImageOps.exif_transpose(im)
        if over_cap:
            im.thumbnail((_IMG_MAX_SIDE, _IMG_MAX_SIDE))
        if ext in (".jpg", ".jpeg"):
            im.convert("RGB").save(path, "JPEG", quality=100, subsampling=0)
        elif ext == ".webp":
            im.save(path, "WEBP", quality=100, lossless=True)
        else:
            im.save(path, "PNG", optimize=True)
    except Exception:
        logger.exception("ref downscale failed (%s) — leaving original", path)


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
                    # Fail loudly instead of silently dropping the file: a dropped
                    # reference used to produce a confusing wrong-face result with no
                    # error. The client surfaces this as an upload error toast.
                    raise web.HTTPBadRequest(text="file_too_large")
                # PIL decode/resize/re-encode is blocking CPU+disk work; run it off
                # the event loop so a large phone photo doesn't stall every other
                # request (same executor pattern as storage/face_verify).
                await asyncio.get_event_loop().run_in_executor(None, _shrink_image, fpath)
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
    # Cap generously: the History "show more" grows the window by 30 each click,
    # so the bound only exists to reject absurd/garbage values, not to paginate.
    limit = max(1, min(_int_or_none(request.query.get("limit")) or 20, 2000))
    offset = max(0, _int_or_none(request.query.get("offset")) or 0)
    rows = await get_user_generations(tg_id, limit, offset)
    result = []
    for r in rows:
        item = _row_to_json(r)
        if isinstance(item.get("settings"), str):
            try:
                item["settings"] = json.loads(item["settings"])
            except (json.JSONDecodeError, TypeError):
                item["settings"] = {}
        # result_urls is JSONB (full image set for multi-photo gens) — decode to a list.
        if isinstance(item.get("result_urls"), str):
            try:
                item["result_urls"] = json.loads(item["result_urls"])
            except (json.JSONDecodeError, TypeError):
                item["result_urls"] = []
        result.append(item)
    return web.json_response(result)


@routes.get("/api/templates")
async def api_templates(request: web.Request):
    """Light list of enabled templates for the gallery (no heavy definition)."""
    items = await list_templates_public(request.query.get("type"))
    return web.json_response(items)


@routes.get("/api/templates/{tpl_id}")
async def api_template_detail(request: web.Request):
    """Full template definition, fetched lazily when a user opens a template."""
    tpl = await get_template_public(request.match_info["tpl_id"])
    if not tpl:
        return web.json_response({"error": "not_found"}, status=404)
    return web.json_response(tpl)


@routes.post("/api/generation/delete")
async def api_delete_generation(request: web.Request):
    data, _ = await _parse_request(request)
    tg_id = _authed_id(request, data.get("tg_id"))
    gen_id = _int_or_none(data.get("id"))
    if not tg_id or gen_id is None:
        return web.json_response({"error": "tg_id and id required"}, status=400)
    urls = await delete_generation(gen_id, tg_id)
    if urls is None:
        return web.json_response({"error": "not_found"}, status=404)
    # Best-effort cleanup of every stored object (the full set for a multi-image gen).
    for result_url in urls:
        if not result_url:
            continue
        if storage.is_remote(result_url):
            await storage.adelete_url(result_url)
        else:
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
    # s3-backed results/uploads are already full public URLs — keep them verbatim;
    # local files are exposed via the /media/<basename> route.
    if storage.is_remote(path):
        return path
    return "/media/" + os.path.basename(path)


async def _publish_refs(files: dict) -> dict:
    """Turn parsed upload paths into reference URLs (for the KIE call + the
    settings record). In s3 mode each upload is pushed to object storage and the
    `files` dict is rewritten to the resulting public URL so the generator hands
    KIE a fetchable link; local mode is unchanged (disk paths, /media/<name>)."""
    refs = {}
    if not storage.is_s3():
        for key, val in files.items():
            vals = val if isinstance(val, list) else [val]
            urls = [_media_url(p) for p in vals]
            refs[key] = urls if isinstance(val, list) else urls[0]
        return refs
    uploaded = []
    try:
        for key, val in files.items():
            vals = val if isinstance(val, list) else [val]
            urls = []
            for p in vals:
                u = await storage.aput_file(p)
                uploaded.append(u)
                urls.append(u)
            files[key] = urls if isinstance(val, list) else urls[0]
            refs[key] = urls if isinstance(val, list) else urls[0]
        return refs
    except Exception:
        # partial multi-file upload failed — drop already-uploaded objects and any
        # remaining local temps so nothing is orphaned (charge happens after this).
        for u in uploaded:
            await storage.adelete_url(u)
        for val in files.values():
            for p in (val if isinstance(val, list) else [val]):
                if not storage.is_remote(p):
                    try:
                        os.remove(p)
                    except OSError:
                        pass
        raise


@routes.get("/api/user/{tg_id}/references")
async def api_list_references(request: web.Request):
    tg_id = _authed_id(request, _int_or_none(request.match_info["tg_id"]))
    rows = await list_references(tg_id)
    out = []
    for r in rows:
        item = _row_to_json(r)
        # normalise: keep /media urls and remote (s3) urls as-is; a bare disk path
        # (legacy) becomes /media/<basename>
        fu = item.get("file_url")
        if fu and not str(fu).startswith("/media/") and not storage.is_remote(fu):
            item["file_url"] = _media_url(fu)
        out.append(item)
    return web.json_response(out)


@routes.get("/api/media-proxy")
async def api_media_proxy(request: web.Request):
    """Same-origin proxy for our own S3 objects.

    When the client re-uses a saved reference / history image as an upload it
    fetches the image URL and reads the blob. With media on S3 that's a
    cross-origin fetch, which iOS Telegram's WKWebView fails intermittently
    (CORS / `Vary: Origin` cache quirks) even though the bucket CORS is correct —
    the upload silently drops and the UI shows "не получилось". Serving the bytes
    from our own origin removes the cross-origin dependency entirely. Only objects
    inside our bucket are served (key_from_url guards against SSRF); they're
    already public, so no auth is required (this path is in _AUTH_SKIP)."""
    # Unauthenticated by design, so rate-limit on the peer IP to stop it being
    # used as an open bandwidth relay for our bucket.
    if not _rate_ok("mproxy:" + _client_ip(request), 600, 60):
        return _too_many()
    url = request.query.get("url", "")
    if not storage.is_remote(url) or not storage.key_from_url(url):
        return web.json_response({"error": "bad_url"}, status=400)
    try:
        data = await storage.aget_bytes(url)
    except Exception:
        logger.exception("media-proxy: fetch failed for %s", url)
        return web.json_response({"error": "not_found"}, status=404)
    ct = mimetypes.guess_type(url)[0] or "application/octet-stream"
    # Objects are immutable (uuid-named) and already public-read on S3, so allow
    # shared/edge caching: Cloudflare can cache each file once and serve it globally
    # (offloads the origin now that all history media is routed through this proxy).
    return web.Response(body=data, content_type=ct,
                        headers={"Cache-Control": "public, max-age=604800, immutable"})


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
    # s3: upload (public-read) and store the public URL; local: keep the /media url
    try:
        file_url = await storage.aput_file(fpath) if storage.is_s3() else _media_url(fpath)
    except Exception:
        try: os.remove(fpath)
        except OSError: pass
        raise
    row = await add_reference(tg_id, file_url, title)
    if row is None:
        if storage.is_remote(file_url):
            await storage.adelete_url(file_url)
        else:
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
    if storage.is_remote(file_url):
        await storage.adelete_url(file_url)
    else:
        fpath = os.path.join(MEDIA_DIR, os.path.basename(file_url))
        try: os.remove(fpath)
        except OSError: pass
    return web.json_response({"ok": True})


# ── Promo codes ──

@routes.post("/api/promo/activate")
async def api_promo_activate(request: web.Request):
    tg_id = _authed_id(request)
    if not tg_id:
        return web.json_response({"error": "unauthorized"}, status=401)
    if not _rate_ok("promo:" + str(tg_id), 10, 60):
        return _too_many()
    data, _ = await _parse_request(request)
    code = (data.get("code") or "").strip().upper()
    if not code:
        return web.json_response({"error": "no_code"}, status=400)

    pool = await get_pool()
    async with pool.acquire() as conn:
        promo = await conn.fetchrow(
            "SELECT * FROM promo_codes WHERE code = $1", code)
        if not promo:
            return web.json_response({"error": "not_found"}, status=404)
        if not promo["enabled"]:
            return web.json_response({"error": "disabled"}, status=400)
        if promo["expires_at"] and promo["expires_at"] < datetime.now(promo["expires_at"].tzinfo or None):
            return web.json_response({"error": "expired"}, status=400)
        if promo["max_uses"] > 0 and promo["used_count"] >= promo["max_uses"]:
            return web.json_response({"error": "limit_reached"}, status=400)

        existing = await conn.fetchval(
            "SELECT id FROM promo_activations WHERE promo_id = $1 AND user_tg_id = $2",
            promo["id"], tg_id)
        if existing:
            return web.json_response({"error": "already_used"}, status=400)

        # The check above is not atomic with the INSERT below — a concurrent double-tap
        # could pass both checks, then the UNIQUE(promo_id, user_tg_id) constraint makes
        # the 2nd INSERT raise. Catch it and return a clean "already_used" (400) instead
        # of leaking a 500 / stack trace.
        try:
            async with conn.transaction():
                await conn.execute("""
                    INSERT INTO promo_activations (promo_id, user_tg_id, tokens_given)
                    VALUES ($1, $2, $3)
                """, promo["id"], tg_id, promo["value"] if promo["type"] == "topup" else 0)
                await conn.execute(
                    "UPDATE promo_codes SET used_count = used_count + 1 WHERE id = $1",
                    promo["id"])

                if promo["type"] == "topup":
                    await conn.execute(
                        "UPDATE users SET balance = balance + $1, updated_at = NOW() WHERE tg_id = $2",
                        promo["value"], tg_id)
                    await conn.execute("""
                        INSERT INTO transactions (user_tg_id, amount, tx_type, description)
                        VALUES ($1, $2, 'promo', $3)
                    """, tg_id, promo["value"], f"promo:{code}")
                    bal = await conn.fetchval("SELECT balance FROM users WHERE tg_id = $1", tg_id)
                    resp = {"ok": True, "type": "topup",
                            "tokens": promo["value"], "balance": bal}
                else:
                    resp = {"ok": True, "type": "bonus_pct",
                            "value": promo["value"], "promo_id": promo["id"]}
        except asyncpg.UniqueViolationError:
            return web.json_response({"error": "already_used"}, status=400)
        return web.json_response(resp)


# ── Rewards ("Награды") ──
@routes.get("/api/rewards")
async def api_rewards(request: web.Request):
    """Reward catalogue + this user's claimed state (server source of truth).
    Unconfigured subscription rewards (no channel set) are marked configured=false."""
    tg_id = _authed_id(request)
    if not tg_id:
        return web.json_response({"error": "unauthorized"}, status=401)
    done = await claimed_rewards(tg_id)
    out = []
    for rid, r in REWARDS.items():
        configured = r["type"] != "subscription" or bool(r.get("chat"))
        item = {"id": rid, "type": r["type"], "amount": r["amount"],
                "configured": configured, "claimed": rid in done}
        if r["type"] == "subscription" and r.get("chat"):
            item["channel_link"] = _channel_link(r["chat"])
        out.append(item)
    return web.json_response(out)


@routes.post("/api/rewards/claim")
async def api_rewards_claim(request: web.Request):
    """Claim a reward once. Subscription rewards are verified via getChatMember;
    the story reward is honor-based (credited on the client's shareToStory tap)."""
    tg_id = _authed_id(request)
    if not tg_id:
        return web.json_response({"error": "unauthorized"}, status=401)
    if not _rate_ok("reward:" + str(tg_id), 10, 60):
        return _too_many()
    data, _ = await _parse_request(request)
    rid = str(data.get("reward_id", ""))
    reward = REWARDS.get(rid)
    if not reward:
        return web.json_response({"error": "bad_reward"}, status=400)

    if reward["type"] == "subscription":
        chat = reward.get("chat")
        if not chat:
            return web.json_response({"error": "not_configured"}, status=400)
        try:
            ok = await _is_subscribed(chat, tg_id)
        except Exception:
            logger.exception("reward %s: getChatMember failed", rid)
            return web.json_response({"error": "check_failed"}, status=400)
        if not ok:
            return web.json_response({"error": "not_subscribed"}, status=400)

    res = await claim_reward(tg_id, rid, reward["amount"])
    if res.get("already"):
        return web.json_response({"ok": True, "already": True, "balance": res["balance"]})
    return web.json_response({"ok": True, "credited": res["credited"], "balance": res["balance"]})


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
    success_url = "https://t.me/promptW_bot/app"

    bonus_pct = 0
    bonus_tokens = 0
    promo_id = None
    promo_id_raw = data.get("promo_id")
    if promo_id_raw:
        pool = await get_pool()
        promo = await pool.fetchrow(
            "SELECT id, type, value, enabled FROM promo_codes WHERE id = $1",
            int(promo_id_raw))
        if promo and promo["enabled"] and promo["type"] == "bonus_pct":
            bonus_pct = promo["value"]
            bonus_tokens = int(tokens * bonus_pct / 100)
            promo_id = promo["id"]

    if provider == "yoomoney":
        if not pg.yookassa_available():
            return web.json_response({"error": "provider_unavailable"}, status=503)
        method = data.get("method")
        await create_payment(order_id, tg_id, "yoomoney", amount_rub, tokens,
                             bonus_pct=bonus_pct, bonus_tokens=bonus_tokens, promo_id=promo_id)
        url, pid = await pg.yookassa_create(amount_rub, order_id, success_url, desc, method)
        if not url:
            return web.json_response({"error": "provider_error"}, status=502)
        if pid:
            await set_payment_external(order_id, pid)
        return web.json_response({"url": url, "order_id": order_id})

    if provider == "platega":
        if not pg.platega_available():
            return web.json_response({"error": "provider_unavailable"}, status=503)
        await create_payment(order_id, tg_id, "platega", amount_rub, tokens,
                             bonus_pct=bonus_pct, bonus_tokens=bonus_tokens, promo_id=promo_id)
        fail_url = success_url
        url, txid = await pg.platega_create(amount_rub, order_id, success_url, fail_url, desc)
        if not url:
            return web.json_response({"error": "provider_error"}, status=502)
        if txid:
            await set_payment_external(order_id, txid)
        return web.json_response({"url": url, "order_id": order_id})

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
            ok, vorder, vamount = await pg.yookassa_verify(pay["external_id"])
            if ok and vorder == order_id:
                try:
                    await _settle_and_notify(order_id, pay["external_id"], provider="yoomoney",
                                             expected_amount=vamount)
                    pay = await get_payment(order_id) or pay
                except Exception:
                    logger.exception("ЮKassa settle (status) failed for %s", order_id)
        elif pay["provider"] == "platega":
            confirmed, amount = await pg.platega_verify(pay["external_id"])
            if confirmed:
                try:
                    await _settle_and_notify(order_id, pay["external_id"], provider="platega",
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
        ok, order_id, vamount = await pg.yookassa_verify(obj["id"])
        if ok and order_id:
            try:
                await _settle_and_notify(order_id, obj["id"], provider="yoomoney",
                                         expected_amount=vamount)
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
                    await _settle_and_notify(order_id, txid, provider="platega",
                                             expected_amount=amount)
                except Exception:
                    logger.exception("Platega settle failed for %s", order_id)
            else:
                logger.warning("Platega callback for %s not confirmed on re-fetch", order_id)
    return web.json_response({"ok": True})


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
    try:
        amt = _fmt_rub(wd["amount_rub"])
        if status == "paid":
            notif.notify_bg(wd["user_tg_id"], "wdPaidTg", amount=amt)
        elif status == "rejected":
            notif.notify_bg(wd["user_tg_id"], "wdRejectTg",
                            btn_key="partnerBtn", page="partner", amount=amt)
    except Exception:
        logger.exception("withdrawal notify failed for wd %s", wd.get("id"))
    return web.json_response(_row_to_json(wd))


@routes.post("/api/heartbeat")
async def api_heartbeat(request: web.Request):
    """WebApp liveness ping → users.last_active_at. Used to suppress TG notification
    duplicates while the user is actively in the app."""
    tg_id = _authed_id(request)
    if not tg_id:
        return web.json_response({"ok": False}, status=401)
    if not _rate_ok("hb:" + str(tg_id), 30, 60):
        return web.json_response({"ok": True})   # silently accept; never error the app
    try:
        await touch_active(tg_id)
    except Exception:
        logger.exception("heartbeat touch_active failed for %s", tg_id)
    return web.json_response({"ok": True})


@routes.post("/api/user/{tg_id}/notif")
async def api_set_notif_pref(request: web.Request):
    """Toggle marketing/engagement notifications (transactional ones are unaffected)."""
    tg_id = _authed_id(request, request.match_info.get("tg_id"))
    if not tg_id:
        return web.json_response({"error": "unauthorized"}, status=401)
    data, _ = await _parse_request(request)
    on = bool(data.get("on"))
    try:
        await set_notif_marketing(tg_id, on)
    except Exception:
        logger.exception("set_notif_marketing failed for %s", tg_id)
        return web.json_response({"error": "failed"}, status=500)
    return web.json_response({"ok": True, "notif_marketing": on})


@routes.post("/api/admin/notify/weekly")
async def api_admin_notify_weekly(request: web.Request):
    """Manually broadcast the 'new templates this week' nudge to opted-in, recently
    active users (capped, quiet-hours-aware, max 1×/7d per user). Owner triggers this
    when there are actually new templates."""
    tg_id = _authed_id(request)
    if tg_id not in ADMIN_IDS:
        return web.json_response({"error": "forbidden"}, status=403)
    if _in_quiet_hours():
        return web.json_response({"ok": False, "reason": "quiet_hours", "sent": 0})
    data, _ = await _parse_request(request)
    limit = max(1, min(2000, _int_or_none(data.get("limit")) or 1000))
    try:
        sent = await run_weekly_broadcast(limit)
    except Exception:
        logger.exception("weekly broadcast failed")
        sent = 0
    return web.json_response({"ok": True, "sent": sent})


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
    "ChatGPT": "openai/gpt-5.3-chat",
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


# Appended (server-side, hidden from users) to every TEMPLATE photo generation that
# carries a face reference. Two proven levers against the "wrong person" drift:
#  1) image-first identity lock (the face is THIS person, 1:1, not idealised), and
#  2) face-prominent framing — in full-body/wide shots the face occupies too few
#     pixels and NanoBanana fills the gaps from the prompt prior (→ a generic
#     handsome man). A/B on prod: supercar full-body 3/4 ok → waist-up framing 4/4.
# Placed at the END of the prompt for recency. Kept out of the stored/returned
# prompt so history/repeat stay clean (the suffix is re-applied each run).
# Short identity lead placed at the very START of the prompt (primacy). The base
# template prompts already front-load a Russian "copy the face 1:1" block, but a
# concise English lead anchors the instruction the model weighs first and states the
# single rule that matters most: describe the SCENE, never the person's features.
_TPL_IDENTITY_ANCHOR = (
    "Use the uploaded reference photo as the strict source of the person's face and "
    "identity — the SAME real, specific person, copied 1:1. The description below "
    "specifies only the scene, clothing, light and framing, never the person's facial "
    "features or looks.\n\n"
)

# Appended at the END (recency). Kept in ENGLISH on purpose: the underlying model
# (Gemini-based NanoBanana) follows English directives more precisely than Russian.
# It reinforces the deltas the base prompt under-covers (real specific person, not a
# model/celebrity; no added beard / age change; face-prominent framing) rather than
# re-listing every feature the base block already enumerates.
_TPL_IDENTITY_SUFFIX = (
    "\n\n[IDENTITY — TOP PRIORITY] The person in the output MUST be the exact same real, "
    "specific individual from the uploaded reference photo — a real photographed person, "
    "NOT a similar-looking model, lookalike or celebrity, and NOT an idealized or "
    "AI-beautified face. Copy the reference face 1:1: face shape and width, cheek fullness, "
    "jawline, nose shape, eye shape and eye COLOR, eyebrows, lips, cheekbones, skin texture, "
    "moles, facial hair and hairstyle. Do NOT beautify, slim or narrow the face, sharpen the "
    "jaw or cheekbones, change the age, add a beard/stubble/moustache that is not in the photo "
    "(or remove one that is), or make the face more symmetrical or 'model-like'. The result "
    "must be instantly recognizable to the person's own family. "
    "[FRAMING] Keep the person LARGE in frame with the FACE as the sharp, well-lit focal point "
    "(waist-up or medium shot); never a tiny full-body figure with a small face."
)

# Black-and-white / editorial / high-fashion stylization is where faces drift most
# (the model "fashionizes" them). Appended only when the scene asks for such a look.
_BW_CUES = (
    "чёрно-бел", "черно-бел", "ч/б", "монохром", "grayscale",
    "black and white", "black-and-white", "editorial",
)
_BW_CLAUSE = (
    " Even under black-and-white, monochrome or editorial/high-fashion stylization, keep the "
    "exact real facial proportions and identity from the reference — never stylize the face "
    "toward a fashion model."
)


# Base template prompts carry "person-priming" adjectives (успешный/уверенный/
# статусный мужчина; женские VOGUE/editorial/высокая мода/модель/гламур). Those cue
# the model toward a generic idealized person and trigger substitution (eval: men get
# bearded GQ models on jet/supercar, women drift younger on vogue). Neutralize them at
# generation time — same code-side approach as _FULLBODY_REPL, no prod-DB mutation.
_DEPRIME_REPL = (
    # positive "be impressive/aspirational" priming on the person
    (r"\bуспешн\w+", ""),
    (r"\bуверенн\w+", ""),
    (r"\bстатусн\w+", ""),
    (r"\bэффектн\w+", ""),
    (r"\bгламурн\w+", ""),
    (r"\bроскошн\w+\s+(?=человек|мужчин|женщин|девушк|парн)", ""),
    # "look like a (super)model" cues — the strongest substitution trigger. Note we do
    # NOT strip a bare "модель": the base prompts use it inside protective negations
    # ("НЕ заменяй на типовое «модельное»"), so blanket removal would gut the guard.
    (r"\bсупермодел\w+", ""),
    (r"\b(?:топ[-\s]?)?модел\w+\s+(?=внешност|лиц)", ""),
    # appearance adjectives that bias the face toward a generic "pretty" prior
    (r"\bкрасив\w+", ""),
    (r"\bпривлекательн\w+", ""),
    (r"\bсимпатичн\w+", ""),
    (r"\bобаятельн\w+", ""),
    (r"\bочаровательн\w+", ""),
    # fashion-magazine style cues
    (r"в\s+стиле\s+VOGUE", "в стиле естественного портрета"),
    (r"\bVOGUE\b", ""),
    (r"\beditorial\b", ""),
    (r"\bfashion[-\s]?(?:модел\w+|съёмк\w+|съемк\w+)", "портрет"),
    (r"высок\w+\s+мод\w+", ""),
)


def _deprime(prompt: str) -> str:
    for pat, repl in _DEPRIME_REPL:
        prompt = re.sub(pat, repl, prompt, flags=re.IGNORECASE)
    # tidy the double spaces / dangling commas left by removals
    prompt = re.sub(r"\s{2,}", " ", prompt)
    prompt = re.sub(r"\s+([,.;])", r"\1", prompt)
    prompt = re.sub(r",(?:\s*,)+", ",", prompt)
    prompt = re.sub(r"\s{2,}", " ", prompt)
    return prompt.strip(" ,")


# A few legacy template skeletons ask for "в полный рост" (full-body), which is the
# very framing that shrinks the face and triggers identity drift. Rewrite those cues
# to a waist-up/medium shot at generation time so they don't contradict the suffix
# below (done in code rather than mutating the shared prod templates table).
_FULLBODY_REPL = (
    ("кадр в полный рост по грудь", "поясной портрет, крупный план по грудь"),
    ("кадр в полный рост,", "поясной/средний план,"),
    ("в полный рост", "поясной/средний план"),
)


def _augment_template_prompt(prompt: str, settings: dict, files: dict) -> str:
    """For template gens that carry a face ref: drop full-body framing cues and
    append the identity/face-prominence directive (the proven anti-drift levers)."""
    if not (settings or {}).get("tplId"):
        return prompt
    if not (files or {}).get("photo-refs"):
        return prompt
    # detect stylization cues on the ORIGINAL text (before de-prime strips them)
    low = prompt.lower()
    bw = any(c in low for c in _BW_CUES)
    for a, b in _FULLBODY_REPL:
        prompt = prompt.replace(a, b)
    prompt = _deprime(prompt)
    out = _TPL_IDENTITY_ANCHOR + prompt + _TPL_IDENTITY_SUFFIX
    if bw:
        out += _BW_CLAUSE
    return out


# ── Face-similarity verify + silent best-of retry (Level C) ──────────────────
# Re-checks that a template photo gen kept the reference face and silently
# re-generates on drift, keeping the best of the attempts. Disabled by default;
# see docs/specs/2026-06-22-face-verify-retry-design.md.
FACE_VERIFY = os.getenv("FACE_VERIFY", "0") == "1"
FACE_VERIFY_SHADOW = os.getenv("FACE_VERIFY_SHADOW", "0") == "1"
FACE_VERIFY_THRESHOLD = float(os.getenv("FACE_VERIFY_THRESHOLD", "0.35"))
FACE_VERIFY_MAX_RETRIES = int(os.getenv("FACE_VERIFY_MAX_RETRIES", "2"))
FACE_VERIFY_BUDGET_SEC = float(os.getenv("FACE_VERIFY_BUDGET_SEC", "900"))


def _face_verify_mode(settings: dict, files: dict) -> str:
    """'enforce' (retry on miss) | 'shadow' (score only, no retry) | 'off'.
    Gated to template photo gens with a face ref, single output, real KIE backend,
    and a loaded face model — otherwise off (the normal single-shot path runs)."""
    if not (FACE_VERIFY or FACE_VERIFY_SHADOW):
        return "off"
    if not (settings or {}).get("tplId"):
        return "off"
    if not (files or {}).get("photo-refs"):
        return "off"
    if (settings or {}).get("count", 1) != 1:
        return "off"
    if generator is None or generator.__class__.__name__ != "KieGenerator":
        return "off"
    if not face_verify.available():
        return "off"
    return "enforce" if FACE_VERIFY else "shadow"


async def _media_bytes(path: str):
    """Raw bytes of a reference/result, whether it's an S3 URL or a local/legacy path."""
    try:
        if storage.is_remote(path):
            return await storage.aget_bytes(path)
        local = os.path.join(MEDIA_DIR, os.path.basename(path)) if path.startswith("/media/") else path
        with open(local, "rb") as f:
            return f.read()
    except Exception:
        logger.exception("face_verify: could not read media %s", path)
        return None


async def _ref_face_embedding(files: dict):
    """Embedding of the first uploaded reference that has a usable face (or None)."""
    refs = files.get("photo-refs")
    refs = refs if isinstance(refs, list) else [refs]
    for r in refs:
        data = await _media_bytes(r)
        emb = await face_verify.aembed(data) if data else None
        if emb is not None:
            return emb
    return None


async def _generate_one_image(effective_prompt, model, settings, files, gen_id):
    result = await generator.generate_image(
        effective_prompt,
        model=model,
        aspect_ratio=settings.get("ratio"),
        resolution=settings.get("quality"),
        count=settings.get("count", 1),
        files=files,
        on_task=_task_saver(gen_id),
    )
    paths = result.file_paths or [result.file_path]
    return result, paths


async def _finalize_image(gen_id, result, paths, prompt, cost, balance, face_tip=None):
    file_urls = [_media_url(p) for p in paths]
    file_url = file_urls[0]
    # Guarded finalize (pending->done): if the reconciler already failed+refunded this
    # row, drop our orphan output and report failure (don't hand both refund + result).
    if gen_id and not await finish_generation_if_pending(gen_id, file_url, file_urls):
        await _discard_media(paths)
        return {"__superseded__": True}
    resp = {
        "file_url": file_url,
        "file_urls": file_urls,
        "media_type": result.media_type,
        "prompt": prompt,   # clean prompt (the identity/framing suffix stays internal)
        "task_id": result.task_id,
        "urls": result.urls,
        "cost": cost,
        "balance": balance,
    }
    if face_tip:
        resp["face_tip"] = face_tip
    return resp


async def _run_image_generation(gen_id, tg_id, cost, balance, prompt,
                                effective_prompt, model, settings, files):
    """Single-shot generation, or (when face-verify applies) a best-of loop that keeps
    the result most similar to the reference face. Always finalizes the row once."""
    mode = _face_verify_mode(settings, files)
    if mode == "off":
        result, paths = await _generate_one_image(effective_prompt, model, settings, files, gen_id)
        return await _finalize_image(gen_id, result, paths, prompt, cost, balance)

    ref_emb = await _ref_face_embedding(files)
    ref_found = ref_emb is not None
    do_retry = (mode == "enforce") and ref_found
    total = 1 + (FACE_VERIFY_MAX_RETRIES if do_retry else 0)

    loop = asyncio.get_event_loop()
    start = loop.time()
    scores = []
    best = None   # {"score", "result", "paths"}
    for i in range(total):
        if i > 0 and (loop.time() - start) >= FACE_VERIFY_BUDGET_SEC:
            logger.info("face_verify: time budget hit for gen %s after %d attempt(s)", gen_id, i)
            break
        try:
            result, paths = await _generate_one_image(effective_prompt, model, settings, files, gen_id)
        except Exception:
            if best is None:
                raise   # first attempt failed -> let _run_generation refund + mark error
            logger.exception("face_verify: retry attempt failed for gen %s, keeping best so far", gen_id)
            break
        score = -1.0
        if ref_found:
            res_emb = await face_verify.aembed(await _media_bytes(paths[0]))
            score = face_verify.similarity(ref_emb, res_emb)
        scores.append(round(score, 4))
        if best is None or score > best["score"]:
            if best is not None:
                await _discard_media(best["paths"])   # keep only the running best
            best = {"score": score, "result": result, "paths": paths}
        else:
            await _discard_media(paths)               # this attempt is worse -> drop now
        if not do_retry or score >= FACE_VERIFY_THRESHOLD:
            break

    best_score = best["score"] if ref_found else None
    accepted = (best["score"] >= FACE_VERIFY_THRESHOLD) if ref_found else None
    face_tip = "faceTipLowSim" if (mode == "enforce" and ref_found and not accepted) else None

    # Collapse the recorded task ids to the chosen attempt so a restart-time
    # reconciler recovers the kept image, not a discarded one.
    if gen_id and best["result"].task_id:
        await set_generation_task_ids(gen_id, [best["result"].task_id])

    resp = await _finalize_image(gen_id, best["result"], best["paths"], prompt, cost, balance, face_tip)
    if gen_id and not resp.get("__superseded__"):
        await record_face_verify(gen_id, len(scores), scores, FACE_VERIFY_THRESHOLD,
                                 ref_found, best_score, accepted)
    return resp


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

    # Clamp photo count to the priced range [1,4] and write it BACK into settings so
    # pricing and the generator agree. Pricing clamps to 4, but the raw count was handed
    # to the model — so count:100 was charged as 4 yet 100 images were requested (free
    # over-generation), and count:0/-5 could under-/mis-generate.
    try:
        _cnt = int(settings.get("count", 1))
    except (TypeError, ValueError):
        _cnt = 1
    settings["count"] = max(1, min(4, _cnt))

    if files:
        settings["references"] = await _publish_refs(files)
    effective_prompt = _augment_template_prompt(prompt, settings, files)

    cost = compute_cost("photo", model, settings)
    if cost is None:
        return web.json_response({"error": "unknown_model"}, status=400)
    ok, gen_id, balance = await _charge_and_create(
        tg_id, "photo", prompt, model, settings, cost, f"photo:{model}")
    if not ok:
        return await _insufficient(tg_id, cost)

    async def _build():
        return await _run_image_generation(
            gen_id, tg_id, cost, balance, prompt,
            effective_prompt, model, settings, files,
        )

    resp = await _run_generation(gen_id, tg_id, cost, f"photo:{model}", _build)
    if resp.get("__error__"):
        return web.json_response({"error": "generation_failed"}, status=500)
    if resp.get("__pending__") or resp.get("__superseded__"):
        # Still running (foreground timed out) or already delivered by the reconciler —
        # not a failure. Client shows "still running" and History/TG surface the result.
        return web.json_response({"pending": True}, status=202)
    await _notify_gen_ready(tg_id, "photo", len(resp.get("file_urls") or []))
    return web.json_response(resp)


async def _apply_tpl_overrides(settings: dict) -> None:
    """For template generations the server is authoritative for the MODEL PARAMETERS:
    quality/mode/duration/sound come from the STORED template definition, not the client.
    So admin edits in the panel apply to generation immediately (the client may still hold
    a cached definition), and a stale/forged client can't run a heavier (e.g. higher-res)
    generation than the template's fixed-price product allows. Ratio stays client-chosen
    only when the template offers a user choice (`ratios[]`); otherwise it's fixed too."""
    tpl_id = (settings or {}).get("tplId")
    if not tpl_id:
        return
    tpl = await get_template_public(tpl_id)
    if not tpl:
        return
    for key in ("quality", "mode", "duration", "sound"):
        if tpl.get(key) is not None:
            settings[key] = tpl[key]
    allowed = tpl.get("ratios")
    if allowed:
        if settings.get("ratio") not in allowed:
            settings["ratio"] = tpl.get("ratio") or allowed[0]
    elif tpl.get("ratio"):
        settings["ratio"] = tpl["ratio"]


@routes.post("/api/generate/video")
async def generate_video(request: web.Request):
    data, files = await _parse_request(request)
    prompt = data.get("prompt", "").strip()
    tg_id = _authed_id(request, data.get("tg_id"))
    model = data.get("model")
    settings = data.get("settings", {})
    # Template gens: model parameters are server-authoritative (read from the stored
    # definition) so admin edits apply at once and can't be overridden by the client.
    await _apply_tpl_overrides(settings)

    if tg_id and not _rate_ok("gen:" + str(tg_id), 20, 60):
        return _too_many()
    if not prompt:
        return web.json_response({"error": "prompt is required"}, status=400)

    # Clamp duration to a sane range and write it BACK so pricing and the model agree
    # (a forged negative/huge duration otherwise made cost<=0 → free, or over-billed
    # while asking the model for an absurd length). None falls through to the default.
    if settings.get("duration") is not None:
        try:
            settings["duration"] = max(1, min(60, int(settings.get("duration"))))
        except (TypeError, ValueError):
            settings.pop("duration", None)

    if files:
        settings["references"] = await _publish_refs(files)

    cost = compute_cost("video", model, settings)
    if cost is None:
        return web.json_response({"error": "unknown_model"}, status=400)
    ok, gen_id, balance = await _charge_and_create(
        tg_id, "video", prompt, model, settings, cost, f"video:{model}")
    if not ok:
        return await _insufficient(tg_id, cost)

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
            on_task=_task_saver(gen_id),
        )
        file_url = _media_url(result.file_path)
        if gen_id and not await finish_generation_if_pending(gen_id, file_url):
            await _discard_media([result.file_path])
            return {"__superseded__": True}
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
    if resp.get("__pending__") or resp.get("__superseded__"):
        # Still running (foreground timed out) or already delivered by the reconciler —
        # not a failure. Client shows "still running" and History/TG surface the result.
        return web.json_response({"pending": True}, status=202)
    await _notify_gen_ready(tg_id, "video")
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
    if cost is None:
        return web.json_response({"error": "unknown_model"}, status=400)
    ok, gen_id, balance = await _charge_and_create(
        tg_id, "audio", prompt, model, settings, cost, f"audio:{model}")
    if not ok:
        return await _insufficient(tg_id, cost)

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
            on_task=_task_saver(gen_id),
        )
        file_url = _media_url(result.file_path)
        if gen_id and not await finish_generation_if_pending(gen_id, file_url):
            await _discard_media([result.file_path])
            return {"__superseded__": True}
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
    if resp.get("__pending__") or resp.get("__superseded__"):
        # Still running (foreground timed out) or already delivered by the reconciler —
        # not a failure. Client shows "still running" and History/TG surface the result.
        return web.json_response({"pending": True}, status=202)
    await _notify_gen_ready(tg_id, "audio")
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

    # Resolve the media source. s3: stream the object's bytes through the bot
    # (BufferedInputFile); legacy/local: serve the on-disk file, contained to
    # MEDIA_DIR (basename strips traversal; realpath blocks edge cases).
    if storage.is_remote(file_url) and storage.key_from_url(file_url):
        try:
            src = BufferedInputFile(await storage.aget_bytes(file_url),
                                    filename=os.path.basename(file_url))
        except Exception:
            logger.exception("send-media: s3 fetch failed")
            return web.json_response({"error": "file not found"}, status=404)
    else:
        # Local-disk file: either a bare path (dev) or one of OUR legacy
        # '{WEBAPP_URL}/media/<file>' URLs (pre-S3 generations, still served from disk).
        # A foreign remote URL yields '' here → refused below.
        if storage.is_remote(file_url):
            fname = storage.media_name_from_url(file_url)
            if not fname:
                return web.json_response({"error": "file not found"}, status=404)
        else:
            fname = os.path.basename(file_url)
        filepath = os.path.join(MEDIA_DIR, fname)
        if os.path.realpath(filepath).startswith(os.path.realpath(MEDIA_DIR) + os.sep) is False \
                or not os.path.isfile(filepath):
            return web.json_response({"error": "file not found"}, status=404)
        src = FSInputFile(filepath)

    bot = _get_bot()
    try:
        if media_type == "audio":
            await bot.send_audio(tg_id, src)
        else:
            # "Сохранить" должно отдавать ОРИГИНАЛ файлом. Inline-медиа Telegram
            # портит: send_photo пережимает JPEG/даунскейлит, а send_video
            # переэнкодит и плющит соотношение сторон (без width/height). send_document
            # сохраняет исходный файл 1:1 — и для фото, и для видео.
            await bot.send_document(tg_id, src)
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
    # Only allow our own media (no arbitrary-URL inline messages). s3: the public
    # object URL is Telegram-fetchable as-is; legacy/local: serve via WEBAPP_URL.
    if storage.is_remote(file_url) and storage.key_from_url(file_url):
        public_url = file_url                       # our S3 object — already public
    elif storage.is_remote(file_url) and storage.media_name_from_url(file_url):
        # OUR legacy '{WEBAPP_URL}/media/<file>' URL (pre-S3) — already a public,
        # Telegram-fetchable URL; just verify the object still exists on disk.
        fname = storage.media_name_from_url(file_url)
        filepath = os.path.join(MEDIA_DIR, fname)
        if not os.path.realpath(filepath).startswith(os.path.realpath(MEDIA_DIR) + os.sep) \
                or not os.path.isfile(filepath):
            return web.json_response({"error": "file_not_found"}, status=404)
        public_url = file_url
    elif storage.is_remote(file_url):
        return web.json_response({"error": "file_not_found"}, status=404)  # foreign URL — refuse
    else:
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


# ── Support chat ─────────────────────────────────────────────────────

@routes.post("/api/support/send")
async def api_support_send(request: web.Request):
    tg_id = _authed_id(request)
    if not tg_id:
        return web.json_response({"error": "unauthorized"}, status=401)
    if not _rate_ok("support:" + str(tg_id), 5, 60):
        return _too_many()
    text = ""
    image_url = None
    ct = request.content_type or ""
    if "multipart" in ct:
        reader = await request.multipart()
        while True:
            part = await reader.next()
            if part is None:
                break
            if part.name == "text":
                text = (await part.text()).strip()
            elif part.name == "image" and part.filename:
                ext = os.path.splitext(part.filename)[1].lower()
                if ext not in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
                    ext = ".jpg"
                fname = uuid.uuid4().hex + ext
                fpath = os.path.join(MEDIA_DIR, fname)
                size = 0
                with open(fpath, "wb") as f:
                    while True:
                        chunk = await part.read_chunk()
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > 10 * 1024 * 1024:
                            f.close()
                            os.remove(fpath)
                            return web.json_response({"error": "file_too_large"}, status=400)
                        f.write(chunk)
                if WEBAPP_URL:
                    image_url = WEBAPP_URL.rstrip("/") + "/media/" + fname
                else:
                    image_url = "/media/" + fname
    else:
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "bad_request"}, status=400)
        text = (data.get("text") or "").strip()
    if not text and not image_url:
        return web.json_response({"error": "empty_message"}, status=400)
    if len(text) > 2000:
        return web.json_response({"error": "invalid_text"}, status=400)
    ticket = await get_or_create_support_ticket(tg_id)
    msg = await add_support_message(ticket["id"], "user", text or "", image_url)
    if _support_bot:
        from bot.support_bot import notify_agents
        asyncio.ensure_future(notify_agents(
            ticket["id"], tg_id, text or "", _support_bot, image_url=image_url))
    return web.json_response(_row_to_json(msg))


@routes.get("/api/support/messages")
async def api_support_messages(request: web.Request):
    tg_id = _authed_id(request)
    if not tg_id:
        return web.json_response({"error": "unauthorized"}, status=401)
    ticket = await get_support_ticket_for_user(tg_id)
    if not ticket:
        return web.json_response({"messages": [], "ticket": None})
    after_id = _int_or_none(request.query.get("after_id")) or 0
    messages = await get_support_messages(ticket["id"], after_id)
    return web.json_response({
        "ticket": _row_to_json(ticket),
        "messages": [_row_to_json(m) for m in messages],
    })


@routes.post("/api/support/close")
async def api_support_close(request: web.Request):
    tg_id = _authed_id(request)
    if not tg_id:
        return web.json_response({"error": "unauthorized"}, status=401)
    ticket = await get_support_ticket_for_user(tg_id)
    if ticket:
        await close_support_ticket(ticket["id"])
    return web.json_response({"ok": True})


@routes.get("/api/health")
async def health(request: web.Request):
    return web.json_response({"status": "ok"})
