import os
import csv
import io
import hmac
import hashlib
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
    reverse_referral_commissions,
)
from pricing import refresh_template_costs
from bot.auth import make_admin_token
from payments_gw import yookassa_refund
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
    # Require an admin-scoped session token (set by auth_middleware). The token is
    # minted only by a successful /api/admin/login (credentials validated against
    # admin_accounts/env), so admin_scope is sufficient proof of admin identity.
    # Authorization (owner vs agent) is enforced per-endpoint via _require_role.
    tg_id = request.get("tg_id")
    if not request.get("admin_scope") or not tg_id:
        raise web.HTTPForbidden(text="forbidden")
    return tg_id


async def _account_role(tg_id):
    pool = await get_pool()
    row = await pool.fetchrow("SELECT role, disabled FROM admin_accounts WHERE tg_id=$1", tg_id)
    if row and not row["disabled"]:
        return row["role"]
    # env owner fallback (table empty / not yet seeded)
    if tg_id in ADMIN_IDS:
        return "owner"
    return None


def _pw_hash(pw): return hashlib.sha256((pw or "").encode()).hexdigest()


async def _require_role(request, role):
    tg_id = _require_admin(request)
    r = await _account_role(tg_id)
    if r != role and r != "owner":   # owner passes every gate
        raise web.HTTPForbidden(text="role_forbidden")
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


def _csv_cell(v):
    s = "" if v is None else str(v)
    # Formula-injection guard for spreadsheet apps.
    if s and s[0] in ("=", "+", "-", "@"):
        s = "'" + s
    return s


async def list_query(request, *, base_sql, count_sql, params=None,
                     search_cols=(), sortable=None, default_sort="created_at",
                     default_order="desc", filters=None, date_col=None,
                     serialize=_row, csv_name="export", require="admin"):
    """Generic paged/filtered/sortable list. base_sql/count_sql end right before
    WHERE; this appends WHERE/ORDER/LIMIT. params are positional placeholders
    already present in base_sql ($1..$k).

    NOTE: Both base_sql and count_sql must NOT already contain a WHERE clause —
    the helper appends its own WHERE (or none if no filters are active).

    Auth: this function performs the admin auth check itself (once). Callers
    must NOT call _require_admin() again before delegating here."""
    pool = await get_pool()
    if require == "owner":
        admin_id = await _require_role(request, "owner")
    else:
        admin_id = _require_admin(request)
    params = list(params or [])
    where = []

    # search (ILIKE over allowlisted columns)
    q = (request.query.get("q") or "").strip()
    if q and search_cols:
        params.append(f"%{q}%")
        idx = len(params)
        where.append("(" + " OR ".join(f"{c} ILIKE ${idx}" for c in search_cols) + ")")

    # equality filters (allowlisted)
    for key, col in (filters or {}).items():
        val = request.query.get(key)
        if val not in (None, ""):
            params.append(val); where.append(f"{col} = ${len(params)}")

    # date range (allowlisted single column)
    if date_col:
        frm = request.query.get("from"); to = request.query.get("to")
        if frm: params.append(frm); where.append(f"{date_col} >= ${len(params)}")
        if to:  params.append(to);  where.append(f"{date_col} < (${len(params)}::date + 1)")

    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    # sort (allowlist)
    sortable = sortable or {}
    sort_key = request.query.get("sort", default_sort)
    sort_col = sortable.get(sort_key, sortable.get(default_sort, default_sort))
    order = "ASC" if (request.query.get("order", default_order).lower() == "asc") else "DESC"

    total = await pool.fetchval(count_sql + where_sql, *params)

    if request.query.get("format") == "csv":
        rows = await pool.fetch(f"{base_sql}{where_sql} ORDER BY {sort_col} {order}", *params)
        await _audit(admin_id, "export_csv", csv_name, None, None, {"count": len(rows)}, None, _client_ip(request))
        buf = io.StringIO(); buf.write("﻿")  # BOM for Excel
        w = csv.writer(buf, delimiter=";")
        if rows:
            cols = list(rows[0].keys()); w.writerow(cols)
            for r in rows:
                w.writerow([_csv_cell(_serialize(r[c])) for c in cols])
        resp = web.Response(body=buf.getvalue().encode("utf-8"),
                            content_type="text/csv",
                            headers={"Content-Disposition": f'attachment; filename="promptw-{csv_name}.csv"'})
        return resp

    limit = _qint(request, "limit", 50, 1, 200)
    offset = _qint(request, "offset", 0, 0)
    params_page = params + [limit, offset]
    rows = await pool.fetch(
        f"{base_sql}{where_sql} ORDER BY {sort_col} {order} LIMIT ${len(params)+1} OFFSET ${len(params)+2}",
        *params_page)
    return web.json_response({"items": [serialize(r) for r in rows], "total": total})


# ── Login (browser auth without Telegram) ──

@admin_routes.post("/api/admin/login")
async def admin_login(request):
    ip = _client_ip(request)
    if not _login_rate_ok(ip):
        return web.json_response({"error": "too_many_attempts"}, status=429)
    data = await _json_body(request)
    login = (data.get("login") or "").strip()
    password = (data.get("password") or "").strip()
    pool = await get_pool()
    acct = await pool.fetchrow("SELECT tg_id, password_hash, disabled FROM admin_accounts WHERE login=$1", login)
    ok = False; admin_tg_id = None
    if acct and not acct["disabled"] and hmac.compare_digest(acct["password_hash"], _pw_hash(password)):
        ok = True; admin_tg_id = acct["tg_id"]
    elif ADMIN_LOGIN and ADMIN_PASSWORD and hmac.compare_digest(login, ADMIN_LOGIN) and hmac.compare_digest(password, ADMIN_PASSWORD):
        ok = True; admin_tg_id = next(iter(ADMIN_IDS)) if ADMIN_IDS else 0
    if not ok:
        await _audit(0, "login_failed", "admin", None, None, {"login": login}, None, ip)
        return web.json_response({"error": "invalid credentials"}, status=403)
    token = make_admin_token(admin_tg_id, BOT_TOKEN, ttl_sec=12 * 3600)
    await _audit(admin_tg_id, "login_browser", "admin", admin_tg_id, None, None, None, ip)
    return web.json_response({"ok": True, "token": token})


# ── Me + Accounts management ──

@admin_routes.get("/api/admin/me")
async def admin_me(request):
    tg_id = _require_admin(request)
    return web.json_response({"tg_id": tg_id, "role": await _account_role(tg_id)})


@admin_routes.get("/api/admin/accounts")
async def admin_accounts_list(request):
    await _require_role(request, "owner")
    pool = await get_pool()
    rows = await pool.fetch("SELECT tg_id, login, role, disabled, created_at FROM admin_accounts ORDER BY created_at")
    return web.json_response({"items": [_row(r) for r in rows], "total": len(rows)})


@admin_routes.post("/api/admin/accounts")
async def admin_accounts_create(request):
    admin_id = await _require_role(request, "owner")
    d = await _json_body(request)
    try:
        tg_id = int(d.get("tg_id"))
    except (TypeError, ValueError):
        return web.json_response({"error": "tg_id must be a number"}, status=400)
    login = (d.get("login") or "").strip(); pw = (d.get("password") or "").strip()
    role = d.get("role") if d.get("role") in ("owner", "agent") else "agent"
    if not login or not pw:
        return web.json_response({"error": "login and password required"}, status=400)
    pool = await get_pool()
    try:
        await pool.execute("INSERT INTO admin_accounts (tg_id, login, password_hash, role) VALUES ($1,$2,$3,$4)",
                           tg_id, login, _pw_hash(pw), role)
    except Exception:
        return web.json_response({"error": "tg_id or login already exists"}, status=409)
    await _audit(admin_id, "account_create", "admin_account", tg_id, None, {"login": login, "role": role}, None, _client_ip(request))
    return web.json_response({"ok": True})


@admin_routes.put("/api/admin/accounts/{tg_id}")
async def admin_accounts_update(request):
    admin_id = await _require_role(request, "owner")
    tg_id = int(request.match_info["tg_id"])
    d = await _json_body(request)
    sets, params = [], []
    if d.get("role") in ("owner", "agent"): params.append(d["role"]); sets.append(f"role=${len(params)}")
    if "disabled" in d: params.append(bool(d["disabled"])); sets.append(f"disabled=${len(params)}")
    if d.get("password"): params.append(_pw_hash(d["password"])); sets.append(f"password_hash=${len(params)}")
    if not sets: return web.json_response({"error": "nothing to update"}, status=400)
    params.append(tg_id)
    pool = await get_pool()
    await pool.execute(f"UPDATE admin_accounts SET {', '.join(sets)} WHERE tg_id=${len(params)}", *params)
    await _audit(admin_id, "account_update", "admin_account", tg_id, None, {k:v for k,v in d.items() if k!='password'}, None, _client_ip(request))
    return web.json_response({"ok": True})


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


@admin_routes.get("/api/admin/stats/timeseries")
async def admin_stats_timeseries(request):
    _require_admin(request)
    pool = await get_pool()
    metric = request.query.get("metric", "revenue")
    frm = request.query.get("from"); to = request.query.get("to")
    # default last 30 days
    where_date = "created_at >= COALESCE($1::date, NOW()::date - INTERVAL '30 days') AND created_at < (COALESCE($2::date, NOW()::date) + 1)"
    qmap = {
        "revenue":     f"SELECT created_at::date d, COALESCE(SUM(amount_rub),0) v FROM payments WHERE status='paid' AND {where_date} GROUP BY d ORDER BY d",
        "payments":    f"SELECT created_at::date d, COUNT(*) v FROM payments WHERE status='paid' AND {where_date} GROUP BY d ORDER BY d",
        "users":       f"SELECT created_at::date d, COUNT(*) v FROM users WHERE {where_date} GROUP BY d ORDER BY d",
        "generations": f"SELECT created_at::date d, COUNT(*) v FROM generations WHERE {where_date} GROUP BY d ORDER BY d",
    }
    sql = qmap.get(metric, qmap["revenue"])
    rows = await pool.fetch(sql, frm, to)
    return web.json_response({"points": [{"d": r["d"].isoformat(), "v": float(r["v"])} for r in rows]})


# ── Face-similarity verify dashboard ──

# Our real cost (₽) of one NanoBanana run — used to total the money lost on retries.
FACE_VERIFY_RETRY_UNIT_COST = float(os.getenv("FACE_VERIFY_RETRY_UNIT_COST", "0"))


@admin_routes.get("/api/admin/face-stats")
async def admin_face_stats(request):
    await _require_role(request, "owner")
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
    admin_id = await _require_role(request, "owner")
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
    admin_id = await _require_role(request, "owner")
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
    admin_id = await _require_role(request, "owner")
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
    return await list_query(
        request,
        base_sql="""SELECT g.id, g.user_tg_id, g.gen_type, g.model, g.status, g.cost, g.prompt, g.created_at
                    FROM generations g""",
        count_sql="SELECT COUNT(*) FROM generations g",
        search_cols=("g.prompt",),
        sortable={"id":"g.id","cost":"g.cost","created_at":"g.created_at"},
        filters={"gen_type":"g.gen_type","status":"g.status"},
        date_col="g.created_at",
        csv_name="generations",
    )


# ── Payments ──

@admin_routes.get("/api/admin/payments")
async def admin_payments(request):
    return await list_query(
        request,
        base_sql="""SELECT p.*, u.username FROM payments p
                    LEFT JOIN users u ON p.user_tg_id = u.tg_id""",
        count_sql="SELECT COUNT(*) FROM payments p LEFT JOIN users u ON p.user_tg_id = u.tg_id",
        search_cols=("u.username", "p.order_id::text", "p.external_id"),
        sortable={"created_at": "p.created_at", "amount_rub": "p.amount_rub", "tokens": "p.tokens"},
        filters={"status": "p.status", "provider": "p.provider"},
        date_col="p.created_at",
        csv_name="payments",
        require="owner",
    )


@admin_routes.get("/api/admin/payments/{pid}")
async def admin_payment_detail(request):
    await _require_role(request, "owner")
    pool = await get_pool()
    pid = int(request.match_info["pid"])
    p = await pool.fetchrow("""SELECT p.*, u.username, u.first_name FROM payments p
                               LEFT JOIN users u ON p.user_tg_id=u.tg_id WHERE p.id=$1""", pid)
    if not p:
        return web.json_response({"error": "not_found"}, status=404)
    return web.json_response({"payment": _row(p)})


@admin_routes.post("/api/admin/payments/{pid}/refund")
async def admin_payment_refund(request):
    admin_id = await _require_role(request, "owner")
    pool = await get_pool()
    pid = int(request.match_info["pid"])
    reason = ((await _json_body(request)).get("reason") or "").strip()
    p = await pool.fetchrow("SELECT * FROM payments WHERE id=$1", pid)
    if not p:
        return web.json_response({"error": "not_found"}, status=404)
    if p["status"] != "paid":
        return web.json_response({"error": "only paid payments can be refunded"}, status=400)
    if p["refunded_at"]:
        return web.json_response({"error": "already refunded"}, status=409)
    # How many tokens were originally granted at settle time (mirrors settle_payment logic):
    #   total_tokens = tokens + (bonus_tokens or 0)
    granted = (p["tokens"] or 0) + (p["bonus_tokens"] or 0)
    buyer = p["user_tg_id"]

    if p["provider"] != "yookassa":
        # Platega has no confirmed refund API — record a manual refund mark only.
        # Referral commissions for this payment are reversed atomically below (ref_balance debited clamped + negative ref_earnings rows).
        rev = []
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "UPDATE payments SET status='refunded', refunded_at=NOW(), refund_id='manual' WHERE id=$1", pid)
                await conn.execute(
                    "UPDATE users SET balance = GREATEST(0, balance - $1), updated_at = NOW() WHERE tg_id = $2",
                    granted, buyer)
                await conn.execute(
                    "INSERT INTO transactions (user_tg_id, amount, tx_type, description) VALUES ($1, $2, 'topup', $3)",
                    buyer, -granted, f"refund reversal: payment {pid} (manual)")
                rev = await reverse_referral_commissions(conn, pid)
        await _audit(admin_id, "payment_refund_manual", "payment", pid,
                     {"status": p["status"]}, {"status": "refunded", "tokens_reversed": granted, "referrals_reversed": rev}, reason, _client_ip(request))
        return web.json_response({"ok": True, "manual": True})

    # amount recomputed server-side from the stored payment — never trust client
    # Call the gateway FIRST; only write to DB on success (gateway failure => 502, no DB mutation).
    ok, refund_id = await yookassa_refund(p["external_id"], int(round(float(p["amount_rub"]))))
    if not ok:
        return web.json_response({"error": "gateway refund failed"}, status=502)

    # Referral commissions for this payment are reversed atomically below (ref_balance debited clamped + negative ref_earnings rows).
    rev = []
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "UPDATE payments SET status='refunded', refunded_at=NOW(), refund_id=$2 WHERE id=$1", pid, refund_id)
            await conn.execute(
                "UPDATE users SET balance = GREATEST(0, balance - $1), updated_at = NOW() WHERE tg_id = $2",
                granted, buyer)
            await conn.execute(
                "INSERT INTO transactions (user_tg_id, amount, tx_type, description) VALUES ($1, $2, 'topup', $3)",
                buyer, -granted, f"refund reversal: payment {pid} ({refund_id})")
            rev = await reverse_referral_commissions(conn, pid)
    await _audit(admin_id, "payment_refund", "payment", pid,
                 {"status": p["status"]}, {"status": "refunded", "refund_id": refund_id, "tokens_reversed": granted, "referrals_reversed": rev}, reason, _client_ip(request))
    return web.json_response({"ok": True, "refund_id": refund_id})


# ── Withdrawals ──

@admin_routes.get("/api/admin/withdrawals")
async def admin_withdrawals(request):
    return await list_query(
        request,
        base_sql="""SELECT w.*, u.username FROM withdrawals w LEFT JOIN users u ON w.user_tg_id=u.tg_id""",
        count_sql="SELECT COUNT(*) FROM withdrawals w LEFT JOIN users u ON w.user_tg_id=u.tg_id",
        search_cols=("u.username", "w.details"),
        sortable={"created_at": "w.created_at", "amount_rub": "w.amount_rub"},
        filters={"status": "w.status", "method": "w.method"},
        date_col="w.created_at",
        csv_name="withdrawals",
        require="owner",
    )


@admin_routes.post("/api/admin/withdrawals/{wd_id}/action")
async def admin_withdrawal_action(request):
    admin_id = await _require_role(request, "owner")
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
    await _require_role(request, "owner")
    limit = _qint(request, "limit", 200, 1, 500)
    offset = _qint(request, "offset", 0, 0)
    return web.json_response(await admin_list_templates(limit, offset))


@admin_routes.get("/api/admin/templates/{tpl_id}")
async def admin_template_get(request):
    await _require_role(request, "owner")
    tpl = await admin_get_template(request.match_info["tpl_id"])
    if not tpl:
        return web.json_response({"error": "not_found"}, status=404)
    return web.json_response(_row(tpl))


@admin_routes.post("/api/admin/templates")
async def admin_template_create(request):
    admin_id = await _require_role(request, "owner")
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
    admin_id = await _require_role(request, "owner")
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
    admin_id = await _require_role(request, "owner")
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
    admin_id = await _require_role(request, "owner")
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
    await _require_role(request, "owner")
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
    admin_id = await _require_role(request, "owner")
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
    admin_id = await _require_role(request, "owner")
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
    admin_id = await _require_role(request, "owner")
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


# ── Referrals ──

@admin_routes.get("/api/admin/referrals")
async def admin_referrals(request):
    await _require_role(request, "owner")
    pool = await get_pool()
    limit = _qint(request, "limit", 50, 1, 200)
    offset = _qint(request, "offset", 0, 0)
    q = (request.query.get("q") or "").strip()

    where = ""
    params: list = []
    if q:
        if q.isdigit():
            where = "WHERE u.tg_id = $1"
            params = [int(q)]
        else:
            where = "WHERE u.username ILIKE $1"
            params = [f"%{q}%"]

    idx = len(params)
    rows = await pool.fetch(f"""
        SELECT u.tg_id, u.username, u.first_name, u.ref_balance,
               u.created_at,
               COUNT(r.referred_tg_id) AS invites,
               COALESCE(SUM(re.earned), 0) AS total_earned
        FROM users u
        LEFT JOIN referrals r ON r.referrer_tg_id = u.tg_id
        LEFT JOIN (
            SELECT referrer_tg_id, SUM(amount_rub) AS earned
            FROM ref_earnings GROUP BY referrer_tg_id
        ) re ON re.referrer_tg_id = u.tg_id
        {where}
        GROUP BY u.tg_id
        HAVING COUNT(r.referred_tg_id) > 0
        ORDER BY invites DESC
        LIMIT ${idx+1} OFFSET ${idx+2}
    """, *params, limit, offset)

    total = await pool.fetchval(f"""
        SELECT COUNT(*) FROM (
            SELECT u.tg_id FROM users u
            LEFT JOIN referrals r ON r.referrer_tg_id = u.tg_id
            {where}
            GROUP BY u.tg_id HAVING COUNT(r.referred_tg_id) > 0
        ) sub
    """, *params)

    return web.json_response({
        "items": [_row(r) for r in rows],
        "total": total,
    })


@admin_routes.get("/api/admin/referrals/{tg_id}")
async def admin_referral_detail(request):
    await _require_role(request, "owner")
    pool = await get_pool()
    tg_id = int(request.match_info["tg_id"])

    referrer = await pool.fetchrow("""
        SELECT tg_id, username, first_name, ref_balance, created_at
        FROM users WHERE tg_id = $1
    """, tg_id)
    if not referrer:
        return web.json_response({"error": "not found"}, status=404)

    invitees = await pool.fetch("""
        SELECT u.tg_id, u.username, u.first_name, u.created_at,
               COALESCE(p.total_paid, 0) AS total_paid
        FROM referrals r
        JOIN users u ON u.tg_id = r.referred_tg_id
        LEFT JOIN (
            SELECT user_tg_id, SUM(amount_rub) AS total_paid
            FROM payments WHERE status = 'paid'
            GROUP BY user_tg_id
        ) p ON p.user_tg_id = u.tg_id
        WHERE r.referrer_tg_id = $1
        ORDER BY r.created_at DESC
    """, tg_id)

    earnings = await pool.fetch("""
        SELECT re.referred_tg_id, u.username, re.line, re.amount_rub, re.created_at
        FROM ref_earnings re
        JOIN users u ON u.tg_id = re.referred_tg_id
        WHERE re.referrer_tg_id = $1
        ORDER BY re.created_at DESC LIMIT 50
    """, tg_id)

    total_earned = await pool.fetchval("""
        SELECT COALESCE(SUM(amount_rub), 0) FROM ref_earnings
        WHERE referrer_tg_id = $1
    """, tg_id)

    return web.json_response({
        "referrer": _row(referrer),
        "invitees": [_row(r) for r in invitees],
        "earnings": [_row(r) for r in earnings],
        "total_earned": float(total_earned),
    })


# ── Audit log ──

@admin_routes.get("/api/admin/audit")
async def admin_audit_log(request):
    await _require_role(request, "owner")
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
    admin_id = await _require_role(request, "owner")
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
    admin_id = await _require_role(request, "owner")
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
    admin_id = await _require_role(request, "owner")
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
    await _require_role(request, "owner")
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
    admin_id = await _require_role(request, "owner")
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
    admin_id = await _require_role(request, "owner")
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
    await _require_role(request, "owner")
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
