from typing import Optional, List
from decimal import Decimal, ROUND_HALF_UP
import json

from db.database import get_pool


async def upsert_user(tg_id: int, username: Optional[str] = None,
                      first_name: Optional[str] = None,
                      last_name: Optional[str] = None,
                      referrer_id: Optional[int] = None,
                      welcome_bonus: int = 0) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # xmax = 0 marks a freshly INSERTed row (vs an ON CONFLICT update)
            row = await conn.fetchrow("""
                INSERT INTO users (tg_id, username, first_name, last_name, referrer_id, balance)
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (tg_id) DO UPDATE SET
                    username = COALESCE(EXCLUDED.username, users.username),
                    first_name = COALESCE(EXCLUDED.first_name, users.first_name),
                    last_name = COALESCE(EXCLUDED.last_name, users.last_name),
                    updated_at = NOW()
                RETURNING *, (xmax = 0) AS is_new
            """, tg_id, username, first_name, last_name, referrer_id,
               welcome_bonus if welcome_bonus > 0 else 0)
            if row["is_new"] and welcome_bonus > 0:
                await conn.execute("""
                    INSERT INTO transactions (user_tg_id, amount, tx_type, description)
                    VALUES ($1, $2, 'bonus', 'Welcome bonus')
                """, tg_id, welcome_bonus)
            user = dict(row)
            user.pop("is_new", None)
            return user


async def try_charge(tg_id: int, amount: int, description: str = "") -> Optional[int]:
    """Atomically deduct `amount` tokens if the balance is sufficient.
    Returns the new balance, or None when funds are insufficient."""
    if amount <= 0:
        return None
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow("""
                UPDATE users SET balance = balance - $1, updated_at = NOW()
                WHERE tg_id = $2 AND balance >= $1
                RETURNING balance
            """, amount, tg_id)
            if row is None:
                return None
            await conn.execute("""
                INSERT INTO transactions (user_tg_id, amount, tx_type, description)
                VALUES ($1, $2, 'spend', $3)
            """, tg_id, -amount, description)
            return row["balance"]


async def get_user(tg_id: int) -> Optional[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM users WHERE tg_id = $1", tg_id)
        return dict(row) if row else None


async def update_user_lang(tg_id: int, lang: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET lang = $1, updated_at = NOW() WHERE tg_id = $2",
            lang, tg_id
        )


async def update_balance(tg_id: int, amount: int, tx_type: str,
                         description: str = "") -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "UPDATE users SET balance = balance + $1, updated_at = NOW() WHERE tg_id = $2",
                amount, tg_id
            )
            await conn.execute("""
                INSERT INTO transactions (user_tg_id, amount, tx_type, description)
                VALUES ($1, $2, $3, $4)
            """, tg_id, amount, tx_type, description)
            row = await conn.fetchrow(
                "SELECT balance FROM users WHERE tg_id = $1", tg_id
            )
            return row["balance"]


async def create_generation(user_tg_id: int, gen_type: str, prompt: str,
                            model: Optional[str] = None,
                            settings: Optional[dict] = None,
                            cost: int = 0) -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            INSERT INTO generations (user_tg_id, gen_type, model, prompt, settings, cost)
            VALUES ($1, $2, $3, $4, $5::jsonb, $6)
            RETURNING id
        """, user_tg_id, gen_type, model, prompt,
           json.dumps(settings or {}), cost)
        return row["id"]


async def update_generation(gen_id: int, status: str,
                            result_url: Optional[str] = None):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE generations SET status = $1, result_url = $2
            WHERE id = $3
        """, status, result_url, gen_id)


async def set_generation_task(gen_id: int, task_id: str):
    """Persist the provider (KIE) task id so a restart-killed generation can be
    recovered later by re-polling that task."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE generations SET provider_task_id = $2 WHERE id = $1",
            gen_id, task_id)


async def get_pending_generations(limit: int = 50) -> List[dict]:
    """Generations still in flight (status 'pending'), oldest first — fed to the
    startup/periodic reconciliation sweep."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, user_tg_id, gen_type, model, cost, provider_task_id, created_at
            FROM generations WHERE status = 'pending'
            ORDER BY created_at ASC LIMIT $1
        """, limit)
        return [dict(r) for r in rows]


async def finish_generation_if_pending(gen_id: int, result_url: str) -> bool:
    """Atomically flip pending->done (only if still pending). Returns True if THIS
    call won the transition — guards against the live task and the sweep racing."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            UPDATE generations SET status = 'done', result_url = $2
            WHERE id = $1 AND status = 'pending'
            RETURNING id
        """, gen_id, result_url)
        return row is not None


async def fail_generation_if_pending(gen_id: int) -> Optional[dict]:
    """Atomically flip pending->error. Returns {user_tg_id, cost} if THIS call won
    the transition (caller then refunds exactly once), else None."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            UPDATE generations SET status = 'error'
            WHERE id = $1 AND status = 'pending'
            RETURNING user_tg_id, cost
        """, gen_id)
        return dict(row) if row else None


async def delete_generation(gen_id: int, tg_id: int) -> Optional[str]:
    """Delete a generation owned by tg_id. Returns its result_url (to clean up the
    media file), or None if it didn't exist / wasn't owned by this user."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            DELETE FROM generations WHERE id = $1 AND user_tg_id = $2
            RETURNING result_url
        """, gen_id, tg_id)
        return row["result_url"] if row else None


async def get_user_generations(tg_id: int, limit: int = 20,
                               offset: int = 0) -> List[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, gen_type, model, prompt, settings, result_url, status, cost, created_at
            FROM generations WHERE user_tg_id = $1
            ORDER BY created_at DESC LIMIT $2 OFFSET $3
        """, tg_id, limit, offset)
        return [dict(r) for r in rows]


async def get_user_transactions(tg_id: int, limit: int = 20) -> List[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, amount, tx_type, description, created_at
            FROM transactions WHERE user_tg_id = $1
            ORDER BY created_at DESC LIMIT $2
        """, tg_id, limit)
        return [dict(r) for r in rows]


async def create_referral(referrer_tg_id: int, referred_tg_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO referrals (referrer_tg_id, referred_tg_id)
            VALUES ($1, $2) ON CONFLICT DO NOTHING
        """, referrer_tg_id, referred_tg_id)


async def get_referral_stats(tg_id: int) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM referrals WHERE referrer_tg_id = $1", tg_id
        )
        return {"total_referrals": count}


# ── Payments & referral payouts (rubles) ──────────────────────────────

# Commission of a referee's top-up credited to the upline, in rubles.
REF_L1_RATE = 0.30   # direct referrals
REF_L2_RATE = 0.05   # second line


async def create_payment(order_id: str, tg_id: int, provider: str,
                         amount_rub, tokens: int,
                         external_id: Optional[str] = None) -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval("""
            INSERT INTO payments (order_id, user_tg_id, provider, amount_rub, tokens, external_id)
            VALUES ($1, $2, $3, $4, $5, $6) RETURNING id
        """, order_id, tg_id, provider, amount_rub, tokens, external_id)


async def get_payment(order_id: str) -> Optional[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT order_id, user_tg_id, provider, amount_rub, tokens, status, external_id
            FROM payments WHERE order_id = $1
        """, order_id)
        return dict(row) if row else None


async def get_latest_pending_payment(tg_id: int, max_age_min: int = 180) -> Optional[dict]:
    """Most recent still-pending payment for a user (within a time window, with an
    external id) — used to reconcile a top-up when the client lost the order id."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(f"""
            SELECT order_id, user_tg_id, provider, amount_rub, tokens, status, external_id
            FROM payments
            WHERE user_tg_id = $1 AND status = 'pending' AND external_id IS NOT NULL
              AND created_at >= NOW() - INTERVAL '{int(max_age_min)} minutes'
            ORDER BY created_at DESC LIMIT 1
        """, tg_id)
        return dict(row) if row else None


async def set_payment_external(order_id: str, external_id: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE payments SET external_id = $1 WHERE order_id = $2",
            external_id, order_id,
        )


async def _is_downline_of(conn, who: int, buyer: int, max_hops: int = 10) -> bool:
    """True if `buyer` appears in `who`'s upline chain — i.e. crediting `who` for
    `buyer`'s purchase would be a circular (mutual) referral. Bounded + cycle-safe."""
    cur = who
    seen = set()
    for _ in range(max_hops):
        ref = await conn.fetchval("SELECT referrer_id FROM users WHERE tg_id = $1", cur)
        if ref is None or ref in seen:
            return False
        if ref == buyer:
            return True
        seen.add(ref)
        cur = ref
    return False


async def settle_payment(order_id: str, external_id: Optional[str] = None,
                         provider: Optional[str] = None,
                         expected_amount=None) -> Optional[dict]:
    """Idempotently confirm a top-up: flip pending->paid (once), credit the
    buyer's tokens, then credit 30%/5% ruble commissions up the referral chain.
    Returns the payment dict, or None if it was already settled / not found.
    Optional `provider`/`expected_amount` cross-check the stored row so a callback
    can't settle a mismatched order."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            pay = await conn.fetchrow("""
                UPDATE payments SET status = 'paid', paid_at = NOW(),
                    external_id = COALESCE($2, external_id)
                WHERE order_id = $1 AND status = 'pending'
                  AND ($3::text IS NULL OR provider = $3)
                  AND ($4::numeric IS NULL OR amount_rub = $4)
                RETURNING id, user_tg_id, amount_rub, tokens
            """, order_id, external_id, provider, expected_amount)
            if pay is None:
                return None
            buyer = pay["user_tg_id"]
            # 1) credit purchased tokens
            await conn.execute(
                "UPDATE users SET balance = balance + $1, updated_at = NOW() WHERE tg_id = $2",
                pay["tokens"], buyer,
            )
            await conn.execute("""
                INSERT INTO transactions (user_tg_id, amount, tx_type, description)
                VALUES ($1, $2, 'topup', $3)
            """, buyer, pay["tokens"], f"topup:{pay['amount_rub']} RUB")
            # 2) referral commissions up the chain (rubles)
            amount = pay["amount_rub"]
            l1 = await conn.fetchval("SELECT referrer_id FROM users WHERE tg_id = $1", buyer)
            for line, rate, who in (
                (1, REF_L1_RATE, l1),
                (2, REF_L2_RATE, (await conn.fetchval(
                    "SELECT referrer_id FROM users WHERE tg_id = $1", l1) if l1 else None)),
            ):
                # skip self-credit and circular referrals (buyer is who's upline)
                if not who or who == buyer or await _is_downline_of(conn, who, buyer):
                    continue
                # exact money math (amount is a Decimal from NUMERIC)
                bonus = (amount * Decimal(str(rate))).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP)
                if bonus <= 0:
                    continue
                await conn.execute(
                    "UPDATE users SET ref_balance = ref_balance + $1 WHERE tg_id = $2",
                    bonus, who,
                )
                await conn.execute("""
                    INSERT INTO ref_earnings (referrer_tg_id, referred_tg_id, line, amount_rub, payment_id)
                    VALUES ($1, $2, $3, $4, $5)
                """, who, buyer, line, bonus, pay["id"])
            return dict(pay)


async def get_partner_overview(tg_id: int) -> dict:
    """Ruble balance, total earned, per-period earnings and line counts."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        bal = await conn.fetchval("SELECT ref_balance FROM users WHERE tg_id = $1", tg_id) or 0
        total = await conn.fetchval(
            "SELECT COALESCE(SUM(amount_rub),0) FROM ref_earnings WHERE referrer_tg_id = $1", tg_id) or 0
        l1_cnt = await conn.fetchval(
            "SELECT COUNT(*) FROM users WHERE referrer_id = $1", tg_id)
        l2_cnt = await conn.fetchval("""
            SELECT COUNT(*) FROM users WHERE referrer_id IN (
                SELECT tg_id FROM users WHERE referrer_id = $1)
        """, tg_id)

        async def period(interval):
            where = "referrer_tg_id = $1"
            if interval:
                where += f" AND created_at >= NOW() - INTERVAL '{interval}'"
            row = await conn.fetchrow(f"""
                SELECT COALESCE(SUM(amount_rub),0) AS earned,
                       COUNT(DISTINCT referred_tg_id) AS people
                FROM ref_earnings WHERE {where}
            """, tg_id)
            return {"earned": float(row["earned"]), "people": row["people"]}

        return {
            "tg_id": tg_id,
            "balance": float(bal),
            "total_earned": float(total),
            "total_referrals": l1_cnt,
            "line1": l1_cnt,
            "line2": l2_cnt,
            "day": await period("1 day"),
            "week": await period("7 days"),
            "month": await period("30 days"),
            "all": await period(None),
        }


# ── Withdrawals ───────────────────────────────────────────────────────

async def create_withdrawal(tg_id: int, method: str, details: str, amount) -> Optional[dict]:
    """Atomically reserve `amount` rubles from ref_balance and queue a payout.
    Returns the withdrawal row, or None when the balance is insufficient."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow("""
                UPDATE users SET ref_balance = ref_balance - $1
                WHERE tg_id = $2 AND ref_balance >= $1
                RETURNING ref_balance
            """, amount, tg_id)
            if row is None:
                return None
            wd = await conn.fetchrow("""
                INSERT INTO withdrawals (user_tg_id, method, details, amount_rub)
                VALUES ($1, $2, $3, $4)
                RETURNING id, method, amount_rub, status, created_at
            """, tg_id, method, details, amount)
            return dict(wd)


async def has_pending_withdrawal(tg_id: int) -> bool:
    pool = await get_pool()
    async with pool.acquire() as conn:
        n = await conn.fetchval(
            "SELECT COUNT(*) FROM withdrawals WHERE user_tg_id = $1 AND status = 'pending'", tg_id)
        return bool(n)


async def list_withdrawals(status: Optional[str] = None, limit: int = 100) -> List[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        if status:
            rows = await conn.fetch("""
                SELECT id, user_tg_id, method, details, amount_rub, status, created_at, processed_at
                FROM withdrawals WHERE status = $1 ORDER BY created_at DESC LIMIT $2
            """, status, limit)
        else:
            rows = await conn.fetch("""
                SELECT id, user_tg_id, method, details, amount_rub, status, created_at, processed_at
                FROM withdrawals ORDER BY created_at DESC LIMIT $1
            """, limit)
        return [dict(r) for r in rows]


async def set_withdrawal_status(wd_id: int, status: str) -> Optional[dict]:
    """Mark a withdrawal paid/rejected. Rejecting refunds the reserved rubles."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            wd = await conn.fetchrow("""
                UPDATE withdrawals SET status = $2, processed_at = NOW()
                WHERE id = $1 AND status = 'pending'
                RETURNING id, user_tg_id, amount_rub, status
            """, wd_id, status)
            if wd is None:
                return None
            if status == "rejected":
                await conn.execute(
                    "UPDATE users SET ref_balance = ref_balance + $1 WHERE tg_id = $2",
                    wd["amount_rub"], wd["user_tg_id"],
                )
            return dict(wd)


# ── Saved reference photos ("Мой референс") ──

MAX_REFERENCES = 6


async def list_references(tg_id: int) -> List[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, file_url, title, created_at FROM user_references
            WHERE user_tg_id = $1 ORDER BY created_at DESC
        """, tg_id)
        return [dict(r) for r in rows]


async def add_reference(tg_id: int, file_url: str, title: Optional[str] = None) -> Optional[dict]:
    """Insert a reference if the user is below MAX_REFERENCES. Returns the row,
    or None when the limit is reached (caller should drop the uploaded file)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            cnt = await conn.fetchval(
                "SELECT COUNT(*) FROM user_references WHERE user_tg_id = $1", tg_id)
            if cnt >= MAX_REFERENCES:
                return None
            row = await conn.fetchrow("""
                INSERT INTO user_references (user_tg_id, file_url, title)
                VALUES ($1, $2, $3) RETURNING id, file_url, title, created_at
            """, tg_id, file_url, title)
            return dict(row)


async def delete_reference(tg_id: int, ref_id: int) -> Optional[str]:
    """Delete a reference the user owns. Returns its file_url for cleanup, or None."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval("""
            DELETE FROM user_references WHERE id = $1 AND user_tg_id = $2
            RETURNING file_url
        """, ref_id, tg_id)


# ── Chat dialogs (server-side, synced across a user's devices) ──

async def list_chat_dialogs(tg_id: int, limit: int = 60) -> List[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, title, model, updated_at
            FROM chat_dialogs WHERE user_tg_id = $1
            ORDER BY updated_at DESC LIMIT $2
        """, tg_id, limit)
        return [dict(r) for r in rows]


async def get_chat_dialog(tg_id: int, dialog_id: int) -> Optional[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        d = await conn.fetchrow(
            "SELECT id, title, model FROM chat_dialogs WHERE id = $1 AND user_tg_id = $2",
            dialog_id, tg_id,
        )
        if not d:
            return None
        rows = await conn.fetch("""
            SELECT role, content, created_at FROM chat_messages
            WHERE dialog_id = $1 ORDER BY id
        """, dialog_id)
        dialog = dict(d)
        dialog["messages"] = [dict(r) for r in rows]
        return dialog


async def append_chat_turn(tg_id: int, dialog_id: Optional[int], model: str,
                           user_text: str, assistant_text: str,
                           title: str) -> Optional[dict]:
    """Persist a user message + assistant reply. Creates the dialog when
    dialog_id is None. Verifies ownership. Returns {id, user_at, assistant_at}."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            if dialog_id is None:
                dialog_id = await conn.fetchval("""
                    INSERT INTO chat_dialogs (user_tg_id, title, model)
                    VALUES ($1, $2, $3) RETURNING id
                """, tg_id, title[:120], model)
            else:
                owns = await conn.fetchval(
                    "SELECT 1 FROM chat_dialogs WHERE id = $1 AND user_tg_id = $2",
                    dialog_id, tg_id,
                )
                if not owns:
                    return None
                await conn.execute(
                    "UPDATE chat_dialogs SET model = $1, updated_at = NOW() WHERE id = $2",
                    model, dialog_id,
                )
            u_at = await conn.fetchval("""
                INSERT INTO chat_messages (dialog_id, role, content)
                VALUES ($1, 'user', $2) RETURNING created_at
            """, dialog_id, user_text)
            a_at = await conn.fetchval("""
                INSERT INTO chat_messages (dialog_id, role, content)
                VALUES ($1, 'assistant', $2) RETURNING created_at
            """, dialog_id, assistant_text)
            return {"id": dialog_id, "user_at": u_at, "assistant_at": a_at}


async def delete_chat_dialog(tg_id: int, dialog_id: int) -> bool:
    pool = await get_pool()
    async with pool.acquire() as conn:
        res = await conn.execute(
            "DELETE FROM chat_dialogs WHERE id = $1 AND user_tg_id = $2",
            dialog_id, tg_id,
        )
        return res.endswith("1")


# ── Templates ("тренды") ──
# JSONB columns (title/preview/definition) come back from asyncpg as strings;
# the API layer json.loads them. _tpl_flat() assembles the client-facing shape.

def _tpl_flat(row: dict, full: bool) -> dict:
    """Assemble a template row into the flat shape the client expects (same keys as
    the legacy TRENDS entry). `full=False` omits the heavy `definition` (list view)."""
    title = row.get("title")
    preview = row.get("preview")
    if isinstance(title, str):
        title = json.loads(title or "{}")
    if isinstance(preview, str):
        preview = json.loads(preview or "{}")
    out = {
        "id": row["id"],
        "type": row["type"],
        "cost": row["cost"],
        "sort_order": row.get("sort_order", 0),
        "category": row.get("category"),
        "featured": bool(row.get("featured")),
        "title": title,
        "preview": (preview or {}).get("img"),
        "full": (preview or {}).get("full"),
    }
    if "need_photo" in row:
        out["need_photo"] = bool(row.get("need_photo"))
    if full:
        definition = row.get("definition")
        if isinstance(definition, str):
            definition = json.loads(definition or "{}")
        # Flatten definition onto the top level so buildTplPrompt/showTplDetail read
        # the same property names they did with the inline TRENDS object.
        for k, v in (definition or {}).items():
            out.setdefault(k, v)
    return out


async def list_templates_public(type_filter: Optional[str] = None) -> List[dict]:
    """Light list of enabled templates for the gallery (no heavy `definition`)."""
    pool = await get_pool()
    if type_filter:
        rows = await pool.fetch("""
            SELECT id, type, cost, sort_order, category, title, preview, featured,
                   (definition->>'needPhoto')::boolean AS need_photo
            FROM templates WHERE enabled = TRUE AND type = $1
            ORDER BY sort_order, id
        """, type_filter)
    else:
        rows = await pool.fetch("""
            SELECT id, type, cost, sort_order, category, title, preview, featured,
                   (definition->>'needPhoto')::boolean AS need_photo
            FROM templates WHERE enabled = TRUE
            ORDER BY sort_order, id
        """)
    return [_tpl_flat(dict(r), full=False) for r in rows]


async def get_template_public(tpl_id: str) -> Optional[dict]:
    """Full enabled template (flat shape incl. definition) for the detail screen."""
    pool = await get_pool()
    row = await pool.fetchrow("""
        SELECT id, type, cost, sort_order, category, title, preview, definition
        FROM templates WHERE id = $1 AND enabled = TRUE
    """, tpl_id)
    return _tpl_flat(dict(row), full=True) if row else None


async def get_template_costs() -> dict:
    """{id: cost} for the server-authoritative pricing cache."""
    pool = await get_pool()
    rows = await pool.fetch("SELECT id, cost FROM templates")
    return {r["id"]: r["cost"] for r in rows}


# ── Templates: admin CRUD ──

async def admin_list_templates(limit: int = 200, offset: int = 0) -> dict:
    pool = await get_pool()
    rows = await pool.fetch("""
        SELECT id, type, enabled, sort_order, category, cost, title, preview, updated_at
        FROM templates ORDER BY sort_order, id LIMIT $1 OFFSET $2
    """, limit, offset)
    total = await pool.fetchval("SELECT COUNT(*) FROM templates")
    items = []
    for r in rows:
        d = dict(r)
        for k in ("title", "preview"):
            if isinstance(d.get(k), str):
                d[k] = json.loads(d[k] or "{}")
        if d.get("updated_at") is not None:
            d["updated_at"] = d["updated_at"].isoformat()
        items.append(d)
    return {"items": items, "total": total}


async def admin_get_template(tpl_id: str) -> Optional[dict]:
    pool = await get_pool()
    row = await pool.fetchrow("SELECT * FROM templates WHERE id = $1", tpl_id)
    if not row:
        return None
    d = dict(row)
    for k in ("title", "preview", "definition"):
        if isinstance(d.get(k), str):
            d[k] = json.loads(d[k] or "{}")
    return d


async def admin_create_template(data: dict) -> bool:
    """Insert a new template. Returns False if the id already exists."""
    pool = await get_pool()
    res = await pool.execute("""
        INSERT INTO templates (id, type, enabled, sort_order, category, cost, title, preview, definition, featured)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
        ON CONFLICT (id) DO NOTHING
    """, data["id"], data["type"], data.get("enabled", True), data.get("sort_order", 0),
        data.get("category"), data.get("cost", 0),
        json.dumps(data.get("title") or {}, ensure_ascii=False),
        json.dumps(data.get("preview") or {}, ensure_ascii=False),
        json.dumps(data.get("definition") or {}, ensure_ascii=False),
        data.get("featured", False))
    return res.endswith("1")


async def admin_update_template(tpl_id: str, data: dict) -> bool:
    """Update mutable fields of an existing template. Returns False if not found."""
    pool = await get_pool()
    res = await pool.execute("""
        UPDATE templates SET
            type = $2, enabled = $3, sort_order = $4, category = $5, cost = $6,
            title = $7, preview = $8, definition = $9, featured = $10, updated_at = NOW()
        WHERE id = $1
    """, tpl_id, data["type"], data.get("enabled", True), data.get("sort_order", 0),
        data.get("category"), data.get("cost", 0),
        json.dumps(data.get("title") or {}, ensure_ascii=False),
        json.dumps(data.get("preview") or {}, ensure_ascii=False),
        json.dumps(data.get("definition") or {}, ensure_ascii=False),
        data.get("featured", False))
    return res.endswith("1")


async def admin_delete_template(tpl_id: str) -> bool:
    pool = await get_pool()
    res = await pool.execute("DELETE FROM templates WHERE id = $1", tpl_id)
    return res.endswith("1")
