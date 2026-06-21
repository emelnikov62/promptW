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
)
from pricing import refresh_template_costs
from bot.auth import make_auth_token
from bot.config import BOT_TOKEN

logger = logging.getLogger(__name__)

admin_routes = web.RouteTableDef()

ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}
ADMIN_LOGIN = os.getenv("ADMIN_LOGIN", "")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")


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
    tg_id = request.get("tg_id")
    if not tg_id or tg_id not in ADMIN_IDS:
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
    data = await request.json()
    login = (data.get("login") or "").strip()
    password = (data.get("password") or "").strip()
    if not hmac.compare_digest(login, ADMIN_LOGIN) or not hmac.compare_digest(password, ADMIN_PASSWORD):
        await _audit(0, "login_failed", "admin", None,
                     None, {"login": login}, None, _client_ip(request))
        return web.json_response({"error": "invalid credentials"}, status=403)
    admin_tg_id = next(iter(ADMIN_IDS)) if ADMIN_IDS else 0
    token = make_auth_token(admin_tg_id, BOT_TOKEN, ttl_sec=12 * 3600)
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


# ── Users ──

@admin_routes.get("/api/admin/users")
async def admin_users(request):
    _require_admin(request)
    pool = await get_pool()
    q = request.query.get("q", "").strip()
    limit = min(int(request.query.get("limit", "50")), 200)
    offset = int(request.query.get("offset", "0"))
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
    data = await request.json()
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
    data = await request.json()
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
    data = await request.json()
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
    limit = min(int(request.query.get("limit", "50")), 200)
    offset = int(request.query.get("offset", "0"))
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
    limit = min(int(request.query.get("limit", "50")), 200)
    offset = int(request.query.get("offset", "0"))
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
    limit = min(int(request.query.get("limit", "50")), 200)
    offset = int(request.query.get("offset", "0"))

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
    data = await request.json()
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
    limit = min(int(request.query.get("limit", "200")), 500)
    offset = int(request.query.get("offset", "0"))
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
    data = _tpl_payload(await request.json())
    if not data["id"] or data["type"] not in _TPL_TYPES:
        return web.json_response({"error": "id and valid type required"}, status=400)
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
    body = await request.json()
    body["id"] = tpl_id  # id is immutable; ignore any id in the body
    data = _tpl_payload(body)
    if data["type"] not in _TPL_TYPES:
        return web.json_response({"error": "valid type required"}, status=400)
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


# ── Audit log ──

@admin_routes.get("/api/admin/audit")
async def admin_audit_log(request):
    _require_admin(request)
    pool = await get_pool()
    limit = min(int(request.query.get("limit", "50")), 200)
    offset = int(request.query.get("offset", "0"))

    rows = await pool.fetch("""
        SELECT * FROM admin_audit_log ORDER BY created_at DESC LIMIT $1 OFFSET $2
    """, limit, offset)
    total = await pool.fetchval("SELECT COUNT(*) FROM admin_audit_log")
    return web.json_response({"items": [_row(r) for r in rows], "total": total})
