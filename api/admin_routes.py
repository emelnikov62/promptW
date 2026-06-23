import os
import hmac
import json
import logging
from datetime import datetime
from decimal import Decimal
import uuid

from aiohttp import web

from db.database import get_pool
from db.queries import (
    admin_list_templates, admin_get_template, admin_create_template,
    admin_update_template, admin_delete_template, get_template_costs,
    get_face_verify_stats, get_face_verify_by_template,
)
from pricing import refresh_template_costs
from bot.auth import make_admin_token
from bot.config import BOT_TOKEN
import storage

logger = logging.getLogger(__name__)

admin_routes = web.RouteTableDef()

ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}
ADMIN_LOGIN = os.getenv("ADMIN_LOGIN", "")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")


# ── Login brute-force throttle (in-memory, fixed window, single-process) ──
import time as _time
_LOGIN_RL = {}  # ip -> [window_start, count]


def _login_rate_ok(ip: str, limit: int = 10, window: int = 300) -> bool:
    now = _time.time()
    rec = _LOGIN_RL.get(ip)
    if rec is None or now - rec[0] >= window:
        if len(_LOGIN_RL) > 2000:
            for k in [k for k, v in _LOGIN_RL.items() if now - v[0] >= window]:
                _LOGIN_RL.pop(k, None)
        _LOGIN_RL[ip] = [now, 1]
        return True
    if rec[1] >= limit:
        return False
    rec[1] += 1
    return True


def _qint(request, name, default, lo=None, hi=None):
    """Parse a query int defensively (a bad value yields the default, not a 500)."""
    try:
        v = int(request.query.get(name, default))
    except (ValueError, TypeError):
        v = default
    if lo is not None:
        v = max(lo, v)
    if hi is not None:
        v = min(hi, v)
    return v


async def _json_body(request):
    """await request.json() that never 500s on malformed input."""
    try:
        d = await request.json()
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _serialize(obj):
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, uuid.UUID):
        return str(obj)
    return obj


def _row(r):
    return {k: _serialize(v) for k, v in r.items()}


def _client_ip(request):
    return request.headers.get("X-Real-IP", request.remote or "")


def _require_admin(request):
    # Require BOTH an admin-scoped session token (set by auth_middleware) and
    # membership in ADMIN_IDS. A plain user/desktop token can authenticate to the
    # rest of the API but never carries admin_scope, so it can't reach the panel.
    tg_id = request.get("tg_id")
    if not request.get("admin_scope") or not tg_id or tg_id not in ADMIN_IDS:
        raise web.HTTPForbidden(text="forbidden")
    return tg_id


async def _audit(admin_tg_id, action, target_type=None, target_id=None,
                 before=None, after=None, reason=None, ip=""):
    pool = await get_pool()
    await pool.execute("""
        INSERT INTO admin_audit_log (admin_tg_id, action, target_type, target_id, before, after, reason, ip)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
    """, admin_tg_id, action, target_type, str(target_id) if target_id else None,
        json.dumps(before) if before else None,
        json.dumps(after) if after else None,
        reason, ip)


# ── Login (browser auth without Telegram) ──

@admin_routes.post("/api/admin/login")
async def admin_login(request):
    if not ADMIN_LOGIN or not ADMIN_PASSWORD:
        return web.json_response({"error": "ADMIN_LOGIN/ADMIN_PASSWORD not configured"}, status=503)
    ip = _client_ip(request)
    if not _login_rate_ok(ip):
        return web.json_response({"error": "too_many_attempts"}, status=429)
    data = await _json_body(request)
    login = (data.get("login") or "").strip()
    password = (data.get("password") or "").strip()
    if not hmac.compare_digest(login, ADMIN_LOGIN) or not hmac.compare_digest(password, ADMIN_PASSWORD):
        await _audit(0, "login_failed", "admin", None,
                     None, {"login": login}, None, ip)
        return web.json_response({"error": "invalid credentials"}, status=403)
    admin_tg_id = next(iter(ADMIN_IDS)) if ADMIN_IDS else 0
    token = make_admin_token(admin_tg_id, BOT_TOKEN, ttl_sec=12 * 3600)
    await _audit(admin_tg_id, "login_browser", "admin", admin_tg_id,
                 None, None, None, _client_ip(request))
    return web.json_response({"ok": True, "token": token})


# ── Dashboard ──

@admin_routes.get("/api/admin/stats")
async def admin_stats(request):
    admin_id = _require_admin(request)
    pool = await get_pool()
    async with pool.acquire() as conn:
        users_total = await conn.fetchval("SELECT COUNT(*) FROM users")
        users_today = await conn.fetchval(
            "SELECT COUNT(*) FROM users WHERE created_at >= CURRENT_DATE")
        users_7d = await conn.fetchval(
            "SELECT COUNT(*) FROM users WHERE created_at >= CURRENT_DATE - INTERVAL '7 days'")

        gens_total = await conn.fetchval("SELECT COUNT(*) FROM generations")
        gens_done = await conn.fetchval("SELECT COUNT(*) FROM generations WHERE status='done'")
        gens_error = await conn.fetchval("SELECT COUNT(*) FROM generations WHERE status='error'")

        revenue_total = await conn.fetchval(
            "SELECT COALESCE(SUM(amount_rub),0) FROM payments WHERE status='paid'") or 0
        revenue_7d = await conn.fetchval(
            "SELECT COALESCE(SUM(amount_rub),0) FROM payments WHERE status='paid' AND paid_at >= CURRENT_DATE - INTERVAL '7 days'") or 0
        payments_count = await conn.fetchval(
            "SELECT COUNT(*) FROM payments WHERE status='paid'")

        tokens_spent = await conn.fetchval(
            "SELECT COALESCE(SUM(ABS(amount)),0) FROM transactions WHERE amount < 0") or 0
        tokens_on_balances = await conn.fetchval(
            "SELECT COALESCE(SUM(balance),0) FROM users") or 0

        pending_withdrawals = await conn.fetchval(
            "SELECT COUNT(*) FROM withdrawals WHERE status='pending'")
        pending_wd_amount = await conn.fetchval(
            "SELECT COALESCE(SUM(amount_rub),0) FROM withdrawals WHERE status='pending'") or 0

    return web.json_response({
        "users": {"total": users_total, "today": users_today, "week": users_7d},
        "generations": {"total": gens_total, "done": gens_done, "error": gens_error},
        "revenue": {"total": float(revenue_total), "week": float(revenue_7d), "payments": payments_count},
        "tokens": {"spent": tokens_spent, "on_balances": tokens_on_balances},
        "withdrawals": {"pending": pending_withdrawals, "pending_amount": float(pending_wd_amount)},
    })


# ── Face-similarity verify dashboard ──

# Our real cost (₽) of one NanoBanana run — used to total the money lost on retries.
FACE_VERIFY_RETRY_UNIT_COST = float(os.getenv("FACE_VERIFY_RETRY_UNIT_COST", "0"))


@admin_routes.get("/api/admin/face-stats")
async def admin_face_stats(request):
    _require_admin(request)
    period = request.query.get("period", "all")
    if period not in ("day", "week", "month", "all"):
        period = "all"
    stats = await get_face_verify_stats(period)
    by_tpl = await get_face_verify_by_template(period, limit=_qint(request, "limit", 50, 1, 200))

    def _f(v):   # numbers may come back as Decimal/None
        return float(v) if isinstance(v, Decimal) else v

    stats = {k: _f(v) for k, v in (stats or {}).items()}
    extra = stats.get("extra_attempts") or 0
    stats["unit_cost"] = FACE_VERIFY_RETRY_UNIT_COST
    stats["money_lost"] = round(extra * FACE_VERIFY_RETRY_UNIT_COST, 2)
    rows = []
    for r in by_tpl:
        r = {k: _f(v) for k, v in r.items()}
        r["money_lost"] = round((r.get("extra_attempts") or 0) * FACE_VERIFY_RETRY_UNIT_COST, 2)
        rows.append(r)
    return web.json_response({"period": period, "stats": stats, "by_template": rows})


# ── Users ──

@admin_routes.get("/api/admin/users")
async def admin_users(request):
    _require_admin(request)
    pool = await get_pool()
    q = request.query.get("q", "").strip()
    limit = _qint(request, "limit", 50, 1, 200)
    offset = _qint(request, "offset", 0, 0)
    sort = request.query.get("sort", "created_at")
    order = "DESC" if request.query.get("order", "desc").lower() == "desc" else "ASC"

    allowed_sorts = {"created_at", "balance", "tg_id", "username"}
    if sort not in allowed_sorts:
        sort = "created_at"

    if q:
        rows = await pool.fetch(f"""
            SELECT tg_id, username, first_name, last_name, lang, balance, ref_balance,
                   banned, admin_note, referrer_id, created_at
            FROM users
            WHERE CAST(tg_id AS TEXT) LIKE $1 OR LOWER(username) LIKE LOWER($1)
            ORDER BY {sort} {order} LIMIT $2 OFFSET $3
        """, f"%{q}%", limit, offset)
    else:
        rows = await pool.fetch(f"""
            SELECT tg_id, username, first_name, last_name, lang, balance, ref_balance,
                   banned, admin_note, referrer_id, created_at
            FROM users ORDER BY {sort} {order} LIMIT $1 OFFSET $2
        """, limit, offset)

    total = await pool.fetchval("SELECT COUNT(*) FROM users")
    return web.json_response({"items": [_row(r) for r in rows], "total": total})


@admin_routes.get("/api/admin/users/{tg_id}")
async def admin_user_detail(request):
    _require_admin(request)
    pool = await get_pool()
    tg_id = int(request.match_info["tg_id"])

    user = await pool.fetchrow("""
        SELECT tg_id, username, first_name, last_name, lang, balance, ref_balance,
               banned, admin_note, referrer_id, created_at, updated_at
        FROM users WHERE tg_id = $1
    """, tg_id)
    if not user:
        return web.json_response({"error": "not_found"}, status=404)

    gens = await pool.fetch("""
        SELECT id, gen_type, model, prompt, status, cost, result_url, created_at
        FROM generations WHERE user_tg_id = $1 ORDER BY created_at DESC LIMIT 20
    """, tg_id)

    txns = await pool.fetch("""
        SELECT id, amount, tx_type, description, created_at
        FROM transactions WHERE user_tg_id = $1 ORDER BY created_at DESC LIMIT 20
    """, tg_id)

    payments = await pool.fetch("""
        SELECT id, order_id, provider, amount_rub, tokens, status, created_at, paid_at
        FROM payments WHERE user_tg_id = $1 ORDER BY created_at DESC LIMIT 20
    """, tg_id)

    return web.json_response({
        "user": _row(user),
        "generations": [_row(r) for r in gens],
        "transactions": [_row(r) for r in txns],
        "payments": [_row(r) for r in payments],
    })


@admin_routes.post("/api/admin/users/{tg_id}/adjust")
async def admin_adjust_balance(request):
    admin_id = _require_admin(request)
    pool = await get_pool()
    tg_id = int(request.match_info["tg_id"])
    data = await _json_body(request)
    amount = int(data.get("amount", 0))
    reason = (data.get("reason") or "").strip()
    if amount == 0 or not reason:
        return web.json_response({"error": "amount and reason required"}, status=400)

    async with pool.acquire() as conn:
        async with conn.transaction():
            old = await conn.fetchval("SELECT balance FROM users WHERE tg_id = $1", tg_id)
            if old is None:
                return web.json_response({"error": "not_found"}, status=404)
            new_bal = old + amount
            await conn.execute("UPDATE users SET balance = $1, updated_at = NOW() WHERE tg_id = $2",
                               new_bal, tg_id)
            await conn.execute("""
                INSERT INTO transactions (user_tg_id, amount, tx_type, description)
                VALUES ($1, $2, 'admin', $3)
            """, tg_id, amount, f"[admin] {reason}")

    await _audit(admin_id, "adjust_balance", "user", tg_id,
                 {"balance": old}, {"balance": new_bal, "amount": amount},
                 reason, _client_ip(request))
    return web.json_response({"ok": True, "balance": new_bal})


@admin_routes.post("/api/admin/users/{tg_id}/ban")
async def admin_ban_user(request):
    admin_id = _require_admin(request)
    pool = await get_pool()
    tg_id = int(request.match_info["tg_id"])
    data = await _json_body(request)
    banned = bool(data.get("banned", True))
    reason = (data.get("reason") or "").strip()

    await pool.execute("UPDATE users SET banned = $1, updated_at = NOW() WHERE tg_id = $2",
                       banned, tg_id)
    await _audit(admin_id, "ban" if banned else "unban", "user", tg_id,
                 None, {"banned": banned}, reason, _client_ip(request))
    return web.json_response({"ok": True, "banned": banned})


@admin_routes.post("/api/admin/users/{tg_id}/note")
async def admin_set_note(request):
    admin_id = _require_admin(request)
    pool = await get_pool()
    tg_id = int(request.match_info["tg_id"])
    data = await _json_body(request)
    note = (data.get("note") or "").strip()[:500]

    await pool.execute("UPDATE users SET admin_note = $1, updated_at = NOW() WHERE tg_id = $2",
                       note or None, tg_id)
    await _audit(admin_id, "set_note", "user", tg_id, None, {"note": note}, None, _client_ip(request))
    return web.json_response({"ok": True})


# ── Generations ──

@admin_routes.get("/api/admin/generations")
async def admin_generations(request):
    _require_admin(request)
    pool = await get_pool()
    limit = _qint(request, "limit", 50, 1, 200)
    offset = _qint(request, "offset", 0, 0)
    status = request.query.get("status")
    gen_type = request.query.get("type")

    conditions = []
    params = []
    idx = 1
    if status:
        conditions.append(f"status = ${idx}")
        params.append(status)
        idx += 1
    if gen_type:
        conditions.append(f"gen_type = ${idx}")
        params.append(gen_type)
        idx += 1

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    params.extend([limit, offset])

    rows = await pool.fetch(f"""
        SELECT g.id, g.user_tg_id, g.gen_type, g.model, g.prompt, g.status, g.cost,
               g.result_url, g.created_at, u.username
        FROM generations g LEFT JOIN users u ON g.user_tg_id = u.tg_id
        {where} ORDER BY g.created_at DESC LIMIT ${idx} OFFSET ${idx+1}
    """, *params)

    total = await pool.fetchval(f"SELECT COUNT(*) FROM generations {where}",
                                *params[:-2]) if params[:-2] else await pool.fetchval(
                                    "SELECT COUNT(*) FROM generations")

    return web.json_response({"items": [_row(r) for r in rows], "total": total})


# ── Payments ──

@admin_routes.get("/api/admin/payments")
async def admin_payments(request):
    _require_admin(request)
    pool = await get_pool()
    limit = _qint(request, "limit", 50, 1, 200)
    offset = _qint(request, "offset", 0, 0)
    status = request.query.get("status")

    if status:
        rows = await pool.fetch("""
            SELECT p.*, u.username FROM payments p
            LEFT JOIN users u ON p.user_tg_id = u.tg_id
            WHERE p.status = $1 ORDER BY p.created_at DESC LIMIT $2 OFFSET $3
        """, status, limit, offset)
        total = await pool.fetchval("SELECT COUNT(*) FROM payments WHERE status = $1", status)
    else:
        rows = await pool.fetch("""
            SELECT p.*, u.username FROM payments p
            LEFT JOIN users u ON p.user_tg_id = u.tg_id
            ORDER BY p.created_at DESC LIMIT $1 OFFSET $2
        """, limit, offset)
        total = await pool.fetchval("SELECT COUNT(*) FROM payments")

    return web.json_response({"items": [_row(r) for r in rows], "total": total})


# ── Withdrawals ──

@admin_routes.get("/api/admin/withdrawals")
async def admin_withdrawals(request):
    _require_admin(request)
    pool = await get_pool()
    limit = _qint(request, "limit", 50, 1, 200)
    offset = _qint(request, "offset", 0, 0)

    rows = await pool.fetch("""
        SELECT w.*, u.username FROM withdrawals w
        LEFT JOIN users u ON w.user_tg_id = u.tg_id
        ORDER BY w.created_at DESC LIMIT $1 OFFSET $2
    """, limit, offset)
    total = await pool.fetchval("SELECT COUNT(*) FROM withdrawals")
    return web.json_response({"items": [_row(r) for r in rows], "total": total})


@admin_routes.post("/api/admin/withdrawals/{wd_id}/action")
async def admin_withdrawal_action(request):
    admin_id = _require_admin(request)
    pool = await get_pool()
    wd_id = int(request.match_info["wd_id"])
    data = await _json_body(request)
    action = data.get("action")
    reason = (data.get("reason") or "").strip()

    if action not in ("approve", "reject", "paid"):
        return web.json_response({"error": "invalid action"}, status=400)

    wd = await pool.fetchrow("SELECT * FROM withdrawals WHERE id = $1", wd_id)
    if not wd:
        return web.json_response({"error": "not_found"}, status=404)

    old_status = wd["status"]
    if action == "approve" and old_status != "pending":
        return web.json_response({"error": "can only approve pending"}, status=400)
    if action == "paid" and old_status != "approved":
        return web.json_response({"error": "can only mark approved as paid"}, status=400)

    new_status = action if action != "paid" else "paid"
    if action == "approve":
        new_status = "approved"

    await pool.execute("""
        UPDATE withdrawals SET status = $1, processed_at = NOW() WHERE id = $2
    """, new_status, wd_id)

    await _audit(admin_id, f"withdrawal_{action}", "withdrawal", wd_id,
                 {"status": old_status}, {"status": new_status},
                 reason, _client_ip(request))
    return web.json_response({"ok": True, "status": new_status})


# ── Templates ──

_TPL_TYPES = {"photo", "video", "audio"}


def _tpl_payload(data: dict) -> dict:
    """Normalize an incoming template body into the columns the queries expect."""
    return {
        "id": (data.get("id") or "").strip(),
        "type": (data.get("type") or "").strip(),
        "enabled": bool(data.get("enabled", True)),
        "featured": bool(data.get("featured", False)),
        "sort_order": int(data.get("sort_order", 0) or 0),
        "category": (data.get("category") or None),
        "cost": int(data.get("cost", 0) or 0),
        "title": data.get("title") or {},
        "preview": data.get("preview") or {},
        "definition": data.get("definition") or {},
    }


async def _reload_costs():
    refresh_template_costs(await get_template_costs())


@admin_routes.get("/api/admin/templates")
async def admin_templates_list(request):
    _require_admin(request)
    limit = _qint(request, "limit", 200, 1, 500)
    offset = _qint(request, "offset", 0, 0)
    return web.json_response(await admin_list_templates(limit, offset))


@admin_routes.get("/api/admin/templates/{tpl_id}")
async def admin_template_get(request):
    _require_admin(request)
    tpl = await admin_get_template(request.match_info["tpl_id"])
    if not tpl:
        return web.json_response({"error": "not_found"}, status=404)
    return web.json_response(_row(tpl))


@admin_routes.post("/api/admin/templates")
async def admin_template_create(request):
    admin_id = _require_admin(request)
    data = _tpl_payload(await _json_body(request))
    if not data["id"] or data["type"] not in _TPL_TYPES:
        return web.json_response({"error": "id and valid type required"}, status=400)
    if data["enabled"] and data["cost"] <= 0:
        return web.json_response(
            {"error": "an enabled template needs a positive cost"}, status=400)
    ok = await admin_create_template(data)
    if not ok:
        return web.json_response({"error": "id already exists"}, status=409)
    await _reload_costs()
    await _audit(admin_id, "template_create", "template", data["id"],
                 None, {"cost": data["cost"], "type": data["type"]}, None, _client_ip(request))
    return web.json_response({"ok": True})


@admin_routes.put("/api/admin/templates/{tpl_id}")
async def admin_template_update(request):
    admin_id = _require_admin(request)
    tpl_id = request.match_info["tpl_id"]
    before = await admin_get_template(tpl_id)
    if not before:
        return web.json_response({"error": "not_found"}, status=404)
    body = await _json_body(request)
    body["id"] = tpl_id  # id is immutable; ignore any id in the body
    data = _tpl_payload(body)
    if data["type"] not in _TPL_TYPES:
        return web.json_response({"error": "valid type required"}, status=400)
    if data["enabled"] and data["cost"] <= 0:
        return web.json_response(
            {"error": "an enabled template needs a positive cost"}, status=400)
    await admin_update_template(tpl_id, data)
    await _reload_costs()
    await _audit(admin_id, "template_update", "template", tpl_id,
                 {"cost": before.get("cost"), "enabled": before.get("enabled")},
                 {"cost": data["cost"], "enabled": data["enabled"]},
                 None, _client_ip(request))
    return web.json_response({"ok": True})


@admin_routes.delete("/api/admin/templates/{tpl_id}")
async def admin_template_delete(request):
    admin_id = _require_admin(request)
    tpl_id = request.match_info["tpl_id"]
    ok = await admin_delete_template(tpl_id)
    if not ok:
        return web.json_response({"error": "not_found"}, status=404)
    await _reload_costs()
    await _audit(admin_id, "template_delete", "template", tpl_id, None, None, None, _client_ip(request))
    return web.json_response({"ok": True})


_PREVIEW_EXT_OK = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".mp4", ".webm", ".mov"}
_PREVIEW_MAX = 30 * 1024 * 1024  # 30 MB


@admin_routes.post("/api/admin/templates/upload")
async def admin_template_upload(request):
    """Upload a template preview (image/video). Stored via `storage` (S3 or /media,
    never the git checkout) so adding a template needs no deploy. Returns its URL."""
    admin_id = _require_admin(request)
    reader = await request.multipart()
    field = await reader.next()
    while field is not None and field.name != "file":
        field = await reader.next()
    if field is None:
        return web.json_response({"error": "no file"}, status=400)
    ext = os.path.splitext(field.filename or "")[1].lower()
    if ext not in _PREVIEW_EXT_OK:
        return web.json_response({"error": "unsupported file type"}, status=400)
    data = bytearray()
    while True:
        chunk = await field.read_chunk(64 * 1024)
        if not chunk:
            break
        data.extend(chunk)
        if len(data) > _PREVIEW_MAX:
            return web.json_response({"error": "file too large (max 30MB)"}, status=413)
    fname = "tpl-" + uuid.uuid4().hex + ext
    url = await storage.aput_bytes(bytes(data), fname)
    await _audit(admin_id, "template_upload", "template", fname,
                 None, {"url": url}, None, _client_ip(request))
    return web.json_response({"ok": True, "url": url})


# ── Promo codes ──

@admin_routes.get("/api/admin/promos")
async def admin_promos_list(request):
    _require_admin(request)
    pool = await get_pool()
    limit = _qint(request, "limit", 50, 1, 200)
    offset = _qint(request, "offset", 0, 0)
    rows = await pool.fetch("""
        SELECT * FROM promo_codes ORDER BY created_at DESC LIMIT $1 OFFSET $2
    """, limit, offset)
    total = await pool.fetchval("SELECT COUNT(*) FROM promo_codes")
    return web.json_response({"items": [_row(r) for r in rows], "total": total})


@admin_routes.post("/api/admin/promos")
async def admin_promo_create(request):
    admin_id = _require_admin(request)
    data = await _json_body(request)
    code = (data.get("code") or "").strip().upper()
    ptype = (data.get("type") or "").strip()
    value = int(data.get("value", 0))
    max_uses = int(data.get("max_uses", 0))
    enabled = bool(data.get("enabled", True))
    expires_at = data.get("expires_at") or None
    if expires_at:
        try:
            expires_at = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        except Exception:
            expires_at = None

    if not code or ptype not in ("topup", "bonus_pct") or value <= 0:
        return web.json_response({"error": "code, valid type (topup/bonus_pct), and value > 0 required"}, status=400)

    pool = await get_pool()
    try:
        await pool.execute("""
            INSERT INTO promo_codes (code, type, value, max_uses, enabled, expires_at)
            VALUES ($1, $2, $3, $4, $5, $6)
        """, code, ptype, value, max_uses, enabled, expires_at)
    except Exception:
        return web.json_response({"error": "code already exists"}, status=409)

    await _audit(admin_id, "promo_create", "promo", code,
                 None, {"type": ptype, "value": value, "max_uses": max_uses},
                 None, _client_ip(request))
    return web.json_response({"ok": True})


@admin_routes.put("/api/admin/promos/{promo_id}")
async def admin_promo_update(request):
    admin_id = _require_admin(request)
    promo_id = int(request.match_info["promo_id"])
    data = await _json_body(request)
    pool = await get_pool()

    old = await pool.fetchrow("SELECT * FROM promo_codes WHERE id = $1", promo_id)
    if not old:
        return web.json_response({"error": "not_found"}, status=404)

    enabled = data.get("enabled", old["enabled"])
    max_uses = int(data.get("max_uses", old["max_uses"]))
    value = int(data.get("value", old["value"]))
    ptype = (data.get("type") or old["type"]).strip()
    expires_at = data.get("expires_at")
    if expires_at == "":
        expires_at = None
    elif expires_at is not None:
        try:
            expires_at = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        except Exception:
            expires_at = old["expires_at"]
    else:
        expires_at = old["expires_at"]

    if ptype not in ("topup", "bonus_pct"):
        return web.json_response({"error": "valid type required"}, status=400)

    await pool.execute("""
        UPDATE promo_codes SET type=$1, value=$2, max_uses=$3, enabled=$4, expires_at=$5
        WHERE id=$6
    """, ptype, value, max_uses, bool(enabled), expires_at, promo_id)

    await _audit(admin_id, "promo_update", "promo", old["code"],
                 {"enabled": old["enabled"], "value": old["value"]},
                 {"enabled": enabled, "value": value},
                 None, _client_ip(request))
    return web.json_response({"ok": True})


@admin_routes.delete("/api/admin/promos/{promo_id}")
async def admin_promo_delete(request):
    admin_id = _require_admin(request)
    promo_id = int(request.match_info["promo_id"])
    pool = await get_pool()
    old = await pool.fetchrow("SELECT code FROM promo_codes WHERE id = $1", promo_id)
    if not old:
        return web.json_response({"error": "not_found"}, status=404)
    await pool.execute("DELETE FROM promo_activations WHERE promo_id = $1", promo_id)
    await pool.execute("DELETE FROM promo_codes WHERE id = $1", promo_id)
    await _audit(admin_id, "promo_delete", "promo", old["code"],
                 None, None, None, _client_ip(request))
    return web.json_response({"ok": True})


# ── Audit log ──

@admin_routes.get("/api/admin/audit")
async def admin_audit_log(request):
    _require_admin(request)
    pool = await get_pool()
    limit = _qint(request, "limit", 50, 1, 200)
    offset = _qint(request, "offset", 0, 0)

    rows = await pool.fetch("""
        SELECT * FROM admin_audit_log ORDER BY created_at DESC LIMIT $1 OFFSET $2
    """, limit, offset)
    total = await pool.fetchval("SELECT COUNT(*) FROM admin_audit_log")
    return web.json_response({"items": [_row(r) for r in rows], "total": total})


# ── Support tickets ──

@admin_routes.get("/api/admin/support")
async def admin_support_list(request):
    _require_admin(request)
    pool = await get_pool()
    status = request.query.get("status", "open")
    limit = _qint(request, "limit", 50, 1, 200)
    offset = _qint(request, "offset", 0, 0)
    if status == "all":
        rows = await pool.fetch("""
            SELECT t.*, u.username, u.first_name,
                   (SELECT COUNT(*) FROM support_messages WHERE ticket_id = t.id) AS msg_count
            FROM support_tickets t
            LEFT JOIN users u ON u.tg_id = t.user_tg_id
            ORDER BY t.updated_at DESC LIMIT $1 OFFSET $2
        """, limit, offset)
        total = await pool.fetchval("SELECT COUNT(*) FROM support_tickets")
    else:
        rows = await pool.fetch("""
            SELECT t.*, u.username, u.first_name,
                   (SELECT COUNT(*) FROM support_messages WHERE ticket_id = t.id) AS msg_count
            FROM support_tickets t
            LEFT JOIN users u ON u.tg_id = t.user_tg_id
            WHERE t.status = $1
            ORDER BY t.updated_at DESC LIMIT $2 OFFSET $3
        """, status, limit, offset)
        total = await pool.fetchval(
            "SELECT COUNT(*) FROM support_tickets WHERE status = $1", status)
    return web.json_response({"items": [_row(dict(r)) for r in rows], "total": total})


# ── Support agents management (registered before {ticket_id} routes) ──

@admin_routes.get("/api/admin/support/agents")
async def admin_support_agents_list(request):
    _require_admin(request)
    from db.queries import list_support_agents
    agents = await list_support_agents()
    return web.json_response({"items": [_row(a) for a in agents]})


@admin_routes.post("/api/admin/support/agents")
async def admin_support_agent_add(request):
    admin_id = _require_admin(request)
    data = await _json_body(request)
    tg_id = data.get("tg_id")
    name = (data.get("name") or "").strip() or None
    if not tg_id:
        return web.json_response({"error": "tg_id required"}, status=400)
    try:
        tg_id = int(tg_id)
    except (ValueError, TypeError):
        return web.json_response({"error": "invalid tg_id"}, status=400)
    from db.queries import add_support_agent
    await add_support_agent(tg_id, name)
    await _audit(admin_id, "support_agent_add", "support_agent", tg_id,
                 None, {"tg_id": tg_id, "name": name}, None, _client_ip(request))
    return web.json_response({"ok": True})


@admin_routes.put("/api/admin/support/agents/{agent_tg_id}")
async def admin_support_agent_update(request):
    admin_id = _require_admin(request)
    agent_tg_id = int(request.match_info["agent_tg_id"])
    data = await _json_body(request)
    name = (data.get("name") or "").strip()
    from db.queries import update_support_agent
    ok = await update_support_agent(agent_tg_id, name or None)
    if not ok:
        return web.json_response({"error": "not_found"}, status=404)
    await _audit(admin_id, "support_agent_update", "support_agent", agent_tg_id,
                 None, {"name": name}, None, _client_ip(request))
    return web.json_response({"ok": True})


@admin_routes.delete("/api/admin/support/agents/{agent_tg_id}")
async def admin_support_agent_delete(request):
    admin_id = _require_admin(request)
    agent_tg_id = int(request.match_info["agent_tg_id"])
    from db.queries import remove_support_agent
    ok = await remove_support_agent(agent_tg_id)
    if not ok:
        return web.json_response({"error": "not_found"}, status=404)
    await _audit(admin_id, "support_agent_remove", "support_agent", agent_tg_id,
                 None, None, None, _client_ip(request))
    return web.json_response({"ok": True})


# ── Support ticket detail (after /agents to avoid {ticket_id} capturing "agents") ──

@admin_routes.get("/api/admin/support/{ticket_id}")
async def admin_support_detail(request):
    _require_admin(request)
    pool = await get_pool()
    tid = int(request.match_info["ticket_id"])
    ticket = await pool.fetchrow("""
        SELECT t.*, u.username, u.first_name
        FROM support_tickets t
        LEFT JOIN users u ON u.tg_id = t.user_tg_id
        WHERE t.id = $1
    """, tid)
    if not ticket:
        return web.json_response({"error": "not_found"}, status=404)
    messages = await pool.fetch("""
        SELECT id, sender, content, image_url, created_at
        FROM support_messages WHERE ticket_id = $1 ORDER BY id
    """, tid)
    d = _row(dict(ticket))
    d["messages"] = [_row(dict(m)) for m in messages]
    return web.json_response(d)


@admin_routes.post("/api/admin/support/{ticket_id}/assign")
async def admin_support_assign(request):
    admin_id = _require_admin(request)
    tid = int(request.match_info["ticket_id"])
    from db.queries import assign_support_ticket
    result = await assign_support_ticket(tid, admin_id, "admin")
    if not result:
        return web.json_response({"error": "already_assigned"}, status=409)
    return web.json_response({"ok": True})


@admin_routes.post("/api/admin/support/{ticket_id}/close")
async def admin_support_close(request):
    admin_id = _require_admin(request)
    tid = int(request.match_info["ticket_id"])
    from db.queries import close_support_ticket
    ok = await close_support_ticket(tid)
    if not ok:
        return web.json_response({"error": "already_closed"}, status=409)
    return web.json_response({"ok": True})


@admin_routes.post("/api/admin/support/{ticket_id}/reply")
async def admin_support_reply(request):
    admin_id = _require_admin(request)
    tid = int(request.match_info["ticket_id"])
    data = await _json_body(request)
    text = (data.get("text") or "").strip()
    if not text or len(text) > 2000:
        return web.json_response({"error": "invalid_text"}, status=400)
    from db.queries import add_support_message, get_support_ticket_by_id
    msg = await add_support_message(tid, "agent", text)
    ticket = await get_support_ticket_by_id(tid)
    if ticket:
        from bot.support_bot import _notify_user
        import asyncio
        asyncio.ensure_future(_notify_user(ticket["user_tg_id"]))
    return web.json_response(_row(msg))


# ── Notifications management ──

@admin_routes.get("/api/admin/notif/overview")
async def admin_notif_overview(request):
    _require_admin(request)
    from api.routes import engagement_enabled
    pool = await get_pool()
    total = await pool.fetchval("SELECT COUNT(*) FROM users")
    opted_out = await pool.fetchval("SELECT COUNT(*) FROM users WHERE notif_marketing = FALSE")
    rows = await pool.fetch("""
        SELECT kind,
               COUNT(*) FILTER (WHERE sent_at >= NOW() - INTERVAL '7 days')  AS d7,
               COUNT(*) FILTER (WHERE sent_at >= NOW() - INTERVAL '30 days') AS d30
        FROM notification_sends GROUP BY kind ORDER BY kind
    """)
    weekly_eligible = await pool.fetchval("""
        SELECT COUNT(*) FROM users u
        WHERE u.notif_marketing = TRUE
          AND u.last_active_at IS NOT NULL
          AND u.last_active_at > NOW() - INTERVAL '30 days'
          AND NOT EXISTS (SELECT 1 FROM notification_sends n
                          WHERE n.user_tg_id = u.tg_id AND n.kind = 'weekly'
                            AND n.sent_at >= NOW() - INTERVAL '7 days')
    """)
    return web.json_response({
        "enabled": await engagement_enabled(),
        "total_users": total,
        "opted_out": opted_out,
        "weekly_eligible": weekly_eligible,
        "sends": [{"kind": r["kind"], "d7": r["d7"], "d30": r["d30"]} for r in rows],
    })


@admin_routes.post("/api/admin/notif/toggle")
async def admin_notif_toggle(request):
    admin_id = _require_admin(request)
    from api.routes import engagement_enabled, ENGAGEMENT_FLAG
    from db.queries import set_setting
    data = await _json_body(request)
    on = bool(data.get("on"))
    before = await engagement_enabled()
    await set_setting(ENGAGEMENT_FLAG, "1" if on else "0")
    await _audit(admin_id, "engagement_toggle", "setting", ENGAGEMENT_FLAG,
                 {"enabled": before}, {"enabled": on}, "", _client_ip(request))
    return web.json_response({"ok": True, "enabled": on})


@admin_routes.post("/api/admin/notif/weekly")
async def admin_notif_weekly(request):
    admin_id = _require_admin(request)
    from api.routes import run_weekly_broadcast, _in_quiet_hours
    if _in_quiet_hours():
        return web.json_response({"ok": False, "reason": "quiet_hours", "sent": 0})
    data = await _json_body(request)
    try:
        limit = max(1, min(2000, int(data.get("limit"))))
    except (TypeError, ValueError):
        limit = 1000
    sent = await run_weekly_broadcast(limit)
    await _audit(admin_id, "weekly_broadcast", "notification", "weekly",
                 None, {"sent": sent}, "", _client_ip(request))
    return web.json_response({"ok": True, "sent": sent})


@admin_routes.get("/api/admin/notif/log")
async def admin_notif_log(request):
    _require_admin(request)
    pool = await get_pool()
    limit = _qint(request, "limit", 50, 1, 200)
    offset = _qint(request, "offset", 0, 0)
    rows = await pool.fetch("""
        SELECT ns.id, ns.user_tg_id, ns.kind, ns.sent_at, u.username
        FROM notification_sends ns LEFT JOIN users u ON ns.user_tg_id = u.tg_id
        ORDER BY ns.sent_at DESC LIMIT $1 OFFSET $2
    """, limit, offset)
    total = await pool.fetchval("SELECT COUNT(*) FROM notification_sends")
    return web.json_response({"items": [_row(r) for r in rows], "total": total})
