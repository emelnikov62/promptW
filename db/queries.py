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
            return dict(row)   # includes is_new (True on first INSERT)


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


async def charge_and_create_generation(tg_id: int, gen_type: str, prompt: str,
                                       model: Optional[str], settings: Optional[dict],
                                       cost: int, *, charge: bool,
                                       label: str = "") -> tuple:
    """Atomically reserve `cost` tokens (when charge=True) AND insert the pending
    generation row in ONE transaction. This closes the window where a crash between
    the charge and the row INSERT would burn tokens with no row for the reconciler
    to refund. Returns (gen_id, new_balance). On insufficient funds returns
    (None, None) and nothing is written. When charge=False, no deduction happens
    and balance is None (the row is still created)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            balance = None
            if charge and cost > 0:
                row = await conn.fetchrow("""
                    UPDATE users SET balance = balance - $1, updated_at = NOW()
                    WHERE tg_id = $2 AND balance >= $1
                    RETURNING balance
                """, cost, tg_id)
                if row is None:
                    return None, None   # insufficient -> rollback, no generation row
                balance = row["balance"]
                await conn.execute("""
                    INSERT INTO transactions (user_tg_id, amount, tx_type, description)
                    VALUES ($1, $2, 'spend', $3)
                """, tg_id, -cost, label)
            grow = await conn.fetchrow("""
                INSERT INTO generations (user_tg_id, gen_type, model, prompt, settings, cost)
                VALUES ($1, $2, $3, $4, $5::jsonb, $6)
                RETURNING id
            """, tg_id, gen_type, model, prompt, json.dumps(settings or {}), cost)
            return grow["id"], balance


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


async def touch_active(tg_id: int):
    """Record a WebApp heartbeat — used to suppress duplicate TG notifications
    while the user is actively in the app."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET last_active_at = NOW() WHERE tg_id = $1", tg_id)


# ── App settings (generic key/value) ─────────────────────────────────────────

async def get_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        v = await conn.fetchval("SELECT value FROM app_settings WHERE key = $1", key)
        return v if v is not None else default


async def set_setting(key: str, value: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO app_settings (key, value, updated_at) VALUES ($1, $2, NOW())
            ON CONFLICT (key) DO UPDATE SET value = $2, updated_at = NOW()
        """, key, value)


# ── Engagement notifications (Phase 2): opt-out, send-log, eligibility ─────────

async def set_notif_marketing(tg_id: int, on: bool):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET notif_marketing = $1 WHERE tg_id = $2", on, tg_id)


async def was_sent(tg_id: int, kind: str, within: Optional[str] = None) -> bool:
    """True if `kind` was already sent to this user — ever (within=None) or within an
    interval like '7 days'. Backs the frequency caps / once-ever dedup."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if within:
            v = await conn.fetchval(
                "SELECT 1 FROM notification_sends WHERE user_tg_id=$1 AND kind=$2 "
                "AND sent_at >= NOW() - $3::interval LIMIT 1", tg_id, kind, within)
        else:
            v = await conn.fetchval(
                "SELECT 1 FROM notification_sends WHERE user_tg_id=$1 AND kind=$2 LIMIT 1",
                tg_id, kind)
        return v is not None


async def log_sent(tg_id: int, kind: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO notification_sends (user_tg_id, kind) VALUES ($1, $2)", tg_id, kind)


async def eligible_bonus_unspent(limit: int = 100) -> List[dict]:
    """Has tokens, account 1–30 days old, never generated → nudge to spend the bonus.
    Once ever (excludes anyone already sent 'bonusUnspent')."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT u.tg_id, u.balance FROM users u
            WHERE u.notif_marketing = TRUE AND u.balance > 0
              AND u.created_at < NOW() - INTERVAL '1 day'
              AND u.created_at > NOW() - INTERVAL '30 days'
              AND NOT EXISTS (SELECT 1 FROM generations g WHERE g.user_tg_id = u.tg_id)
              AND NOT EXISTS (SELECT 1 FROM notification_sends n
                              WHERE n.user_tg_id = u.tg_id AND n.kind = 'bonusUnspent')
            LIMIT $1
        """, limit)
        return [dict(r) for r in rows]


async def eligible_reengage(limit: int = 100) -> List[int]:
    """Was active before (>=1 generation) but quiet for 7–60 days. Max 1×/7 days."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT u.tg_id FROM users u
            WHERE u.notif_marketing = TRUE
              AND u.last_active_at IS NOT NULL
              AND u.last_active_at < NOW() - INTERVAL '7 days'
              AND u.last_active_at > NOW() - INTERVAL '60 days'
              AND EXISTS (SELECT 1 FROM generations g WHERE g.user_tg_id = u.tg_id)
              AND NOT EXISTS (SELECT 1 FROM notification_sends n
                              WHERE n.user_tg_id = u.tg_id AND n.kind = 'reengage'
                                AND n.sent_at >= NOW() - INTERVAL '7 days')
            LIMIT $1
        """, limit)
        return [r["tg_id"] for r in rows]


async def eligible_reward_avail(limit: int = 100) -> List[int]:
    """Opted-in users who never claimed any reward → one-time nudge to grab free tokens.
    (Sweep additionally gates this on RWD_* channels being configured.) Once ever."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT u.tg_id FROM users u
            WHERE u.notif_marketing = TRUE
              AND NOT EXISTS (SELECT 1 FROM reward_claims r WHERE r.user_tg_id = u.tg_id)
              AND NOT EXISTS (SELECT 1 FROM notification_sends n
                              WHERE n.user_tg_id = u.tg_id AND n.kind = 'rewardAvail')
            LIMIT $1
        """, limit)
        return [r["tg_id"] for r in rows]


async def eligible_weekly(limit: int = 1000) -> List[int]:
    """For the manual weekly broadcast: opted-in, active in the last 30 days,
    not already sent 'weekly' in the last 7 days."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT u.tg_id FROM users u
            WHERE u.notif_marketing = TRUE
              AND u.last_active_at IS NOT NULL
              AND u.last_active_at > NOW() - INTERVAL '30 days'
              AND NOT EXISTS (SELECT 1 FROM notification_sends n
                              WHERE n.user_tg_id = u.tg_id AND n.kind = 'weekly'
                                AND n.sent_at >= NOW() - INTERVAL '7 days')
            LIMIT $1
        """, limit)
        return [r["tg_id"] for r in rows]


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


async def add_generation_task(gen_id: int, task_id: str):
    """Append a provider (KIE) task id so a restart-killed generation can be recovered
    later by re-polling. A photo gen with count=N creates N tasks, all of which report
    here; the legacy single column keeps the first id for back-compat. Idempotent: a
    repeated id (callback retry) is not appended twice."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE generations SET
                provider_task_ids = CASE
                    WHEN provider_task_ids @> to_jsonb($2::text) THEN provider_task_ids
                    ELSE COALESCE(provider_task_ids, '[]'::jsonb) || to_jsonb($2::text)
                END,
                provider_task_id = COALESCE(provider_task_id, $2)
            WHERE id = $1
        """, gen_id, task_id)


async def set_generation_task_ids(gen_id: int, ids: list):
    """Overwrite the recorded provider task ids with the chosen best-of attempt's
    id(s). After a face-verify best-of run we discard the rejected attempts, so the
    reconciler must not later 'recover' one of them — collapse the set to what we kept."""
    ids = [i for i in (ids or []) if i]
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE generations SET provider_task_ids = $2::jsonb, provider_task_id = $3
            WHERE id = $1
        """, gen_id, json.dumps(ids), ids[0] if ids else None)


async def record_face_verify(gen_id: int, attempts: int, scores: list,
                             threshold: float, ref_found: bool,
                             best_score=None, accepted=None):
    """Persist face-verify telemetry for a generation (admin dashboard + calibration).
    `best_score`/`accepted` are NULL when the reference had no usable face (ref_found
    is False) — the loop fell back to a plain single-shot generation."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE generations SET
                face_attempts = $2, face_scores = $3::jsonb, face_threshold = $4,
                face_ref_found = $5, face_score = $6, face_accepted = $7
            WHERE id = $1
        """, gen_id, attempts, json.dumps(scores or []), threshold,
           ref_found, best_score, accepted)


async def get_pending_generations(limit: int = 50) -> List[dict]:
    """Generations still in flight (status 'pending'), oldest first — fed to the
    startup/periodic reconciliation sweep."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, user_tg_id, gen_type, model, cost,
                   provider_task_id, provider_task_ids, created_at
            FROM generations WHERE status = 'pending'
            ORDER BY created_at ASC LIMIT $1
        """, limit)
        return [dict(r) for r in rows]


async def finish_generation_if_pending(gen_id: int, result_url: str,
                                       result_urls: Optional[list] = None) -> bool:
    """Atomically flip pending->done (only if still pending). Returns True if THIS
    call won the transition — guards against the live task and the sweep racing.
    `result_urls` persists the full image set (multi-photo gens); the legacy
    `result_url` keeps the first url."""
    urls = result_urls if result_urls else [result_url]
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            UPDATE generations SET status = 'done', result_url = $2, result_urls = $3::jsonb
            WHERE id = $1 AND status = 'pending'
            RETURNING id
        """, gen_id, result_url, json.dumps(urls))
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


async def delete_generation(gen_id: int, tg_id: int) -> Optional[list]:
    """Delete a generation owned by tg_id. Returns the list of its media urls (the
    full set for a multi-image gen, to clean up every object), or None if it didn't
    exist / wasn't owned by this user."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            DELETE FROM generations WHERE id = $1 AND user_tg_id = $2
            RETURNING result_url, result_urls
        """, gen_id, tg_id)
    if not row:
        return None
    urls = []
    if row["result_urls"]:
        try:
            urls = json.loads(row["result_urls"]) if isinstance(row["result_urls"], str) else list(row["result_urls"])
        except (json.JSONDecodeError, TypeError):
            urls = []
    if not urls and row["result_url"]:
        urls = [row["result_url"]]
    return urls


async def get_user_generations(tg_id: int, limit: int = 20,
                               offset: int = 0) -> List[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, gen_type, model, prompt, settings, result_url, result_urls,
                   status, cost, created_at
            FROM generations WHERE user_tg_id = $1
            ORDER BY created_at DESC LIMIT $2 OFFSET $3
        """, tg_id, limit, offset)
        return [dict(r) for r in rows]


# ── Face-verify analytics (admin) ─────────────────────────────────────
_FACE_PERIOD_INTERVAL = {"day": "1 day", "week": "7 days", "month": "30 days"}


def _face_time_clause(period: str) -> str:
    iv = _FACE_PERIOD_INTERVAL.get(period)
    return f"AND created_at >= NOW() - INTERVAL '{iv}'" if iv else ""


async def get_face_verify_stats(period: str = "all") -> dict:
    """Aggregate face-verify telemetry over template photo gens for a period.
    'eligible' = rows where the verify path ran (face_attempts recorded); 'ref_found'
    = the subset where a comparable reference face existed (scores are meaningful)."""
    tc = _face_time_clause(period)
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(f"""
            SELECT
              COUNT(*)                                                       AS total,
              COUNT(*) FILTER (WHERE face_ref_found)                         AS ref_found,
              COUNT(*) FILTER (WHERE face_attempts = 1)                      AS att1,
              COUNT(*) FILTER (WHERE face_attempts = 2)                      AS att2,
              COUNT(*) FILTER (WHERE face_attempts >= 3)                     AS att3,
              COUNT(*) FILTER (WHERE face_attempts > 1)                      AS retried,
              COALESCE(SUM(GREATEST(face_attempts - 1, 0)), 0)              AS extra_attempts,
              COUNT(*) FILTER (WHERE face_accepted)                          AS accepted,
              COUNT(*) FILTER (WHERE face_accepted AND face_attempts = 1)    AS accepted_first,
              AVG(face_score) FILTER (WHERE face_ref_found)                  AS avg_score,
              PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY face_score)
                  FILTER (WHERE face_ref_found)                             AS median_score,
              COUNT(*) FILTER (WHERE face_ref_found AND face_score >= 0.5)                       AS band_strong,
              COUNT(*) FILTER (WHERE face_ref_found AND face_score >= 0.35 AND face_score < 0.5) AS band_ok,
              COUNT(*) FILTER (WHERE face_ref_found AND face_score < 0.35)                       AS band_weak
            FROM generations
            WHERE gen_type = 'photo' AND face_attempts IS NOT NULL {tc}
        """)
        return dict(row) if row else {}


async def get_face_verify_by_template(period: str = "all", limit: int = 50) -> List[dict]:
    """Per-template face-verify breakdown — surfaces which templates drift most
    (high retry rate / low avg score), i.e. which prompts need fixing."""
    tc = _face_time_clause(period)
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(f"""
            SELECT settings->>'tplId'                                AS tpl_id,
                   COUNT(*)                                          AS total,
                   COUNT(*) FILTER (WHERE face_attempts > 1)         AS retried,
                   COALESCE(SUM(GREATEST(face_attempts - 1, 0)), 0) AS extra_attempts,
                   COUNT(*) FILTER (WHERE face_accepted)             AS accepted,
                   AVG(face_score) FILTER (WHERE face_ref_found)     AS avg_score
            FROM generations
            WHERE gen_type = 'photo' AND face_attempts IS NOT NULL
              AND settings->>'tplId' IS NOT NULL {tc}
            GROUP BY settings->>'tplId'
            ORDER BY retried DESC, total DESC
            LIMIT {int(limit)}
        """)
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
                         external_id: Optional[str] = None,
                         bonus_pct: int = 0, bonus_tokens: int = 0,
                         promo_id: Optional[int] = None) -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval("""
            INSERT INTO payments (order_id, user_tg_id, provider, amount_rub, tokens,
                                  external_id, bonus_pct, bonus_tokens, promo_id)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9) RETURNING id
        """, order_id, tg_id, provider, amount_rub, tokens, external_id,
            bonus_pct, bonus_tokens, promo_id)


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
                  -- Compare ROUNDED rubles: the provider verifiers return int(round(amount)),
                  -- so an exact NUMERIC match would wrongly reject a fractional-priced order
                  -- ("paid but got nothing"). order_id already identifies the row uniquely;
                  -- this is a defense-in-depth cross-check, so rounded equality is enough.
                  AND ($4::numeric IS NULL OR ROUND(amount_rub) = ROUND($4::numeric))
                RETURNING id, user_tg_id, amount_rub, tokens, bonus_pct, bonus_tokens
            """, order_id, external_id, provider, expected_amount)
            if pay is None:
                return None
            buyer = pay["user_tg_id"]
            base_tokens = pay["tokens"]
            bonus = pay["bonus_tokens"] or 0
            total_tokens = base_tokens + bonus
            # 1) credit purchased tokens + bonus
            await conn.execute(
                "UPDATE users SET balance = balance + $1, updated_at = NOW() WHERE tg_id = $2",
                total_tokens, buyer,
            )
            desc = f"topup:{pay['amount_rub']} RUB"
            if bonus > 0:
                desc += f" (+{bonus} bonus {pay['bonus_pct']}%)"
            await conn.execute("""
                INSERT INTO transactions (user_tg_id, amount, tx_type, description)
                VALUES ($1, $2, 'topup', $3)
            """, buyer, total_tokens, desc)
            # 2) referral commissions up the chain (rubles)
            amount = pay["amount_rub"]
            ref_credits = []   # [{tg_id, line, amount}] — for post-settle notifications
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
                ref_credits.append({"tg_id": who, "line": line, "amount": float(bonus)})
            out = dict(pay)
            out["ref_credits"] = ref_credits
            return out


async def reverse_referral_commissions(conn, payment_id: int) -> list:
    """Reverse referral commissions credited at settle for `payment_id` (called on refund).
    For each ORIGINAL (positive) ref_earnings row of this payment: debit the referrer's
    ref_balance clamped at 0 (they may have already withdrawn it) and write a NEGATIVE
    reversing ref_earnings row (preserves audit trail; nets the per-payment earnings to 0).
    Only positive rows are selected, so a re-run cannot double-reverse. MUST run inside an
    existing transaction (takes `conn`). Returns [{tg_id, line, amount}] of what was reversed."""
    rows = await conn.fetch(
        "SELECT referrer_tg_id, referred_tg_id, line, amount_rub FROM ref_earnings "
        "WHERE payment_id = $1 AND amount_rub > 0", payment_id)
    reversed_out = []
    for r in rows:
        amt = r["amount_rub"]
        await conn.execute(
            "UPDATE users SET ref_balance = GREATEST(0, ref_balance - $1) WHERE tg_id = $2",
            amt, r["referrer_tg_id"])
        await conn.execute(
            "INSERT INTO ref_earnings (referrer_tg_id, referred_tg_id, line, amount_rub, payment_id) "
            "VALUES ($1, $2, $3, $4, $5)",
            r["referrer_tg_id"], r["referred_tg_id"], r["line"], -amt, payment_id)
        reversed_out.append({"tg_id": r["referrer_tg_id"], "line": r["line"], "amount": float(amt)})
    return reversed_out


# ── Payme (Paycom) Merchant API — UZS payments ───────────────────────

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


# ── Rewards ("Награды") ───────────────────────────────────────────────

async def claimed_rewards(tg_id: int) -> set:
    """Reward ids this user has already claimed (server source of truth)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT reward_id FROM reward_claims WHERE user_tg_id = $1", tg_id)
        return {r["reward_id"] for r in rows}


async def claim_reward(tg_id: int, reward_id: str, amount: int) -> dict:
    """Idempotently credit a reward once. ON CONFLICT makes a concurrent double-tap
    a no-op (returns already) instead of double-crediting. Returns
    {credited, balance} on first claim, {already: True, balance} otherwise."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            inserted = await conn.fetchval("""
                INSERT INTO reward_claims (user_tg_id, reward_id, amount)
                VALUES ($1, $2, $3)
                ON CONFLICT (user_tg_id, reward_id) DO NOTHING
                RETURNING id
            """, tg_id, reward_id, amount)
            if inserted is None:
                bal = await conn.fetchval("SELECT balance FROM users WHERE tg_id = $1", tg_id)
                return {"already": True, "balance": bal}
            await conn.execute(
                "UPDATE users SET balance = balance + $1, updated_at = NOW() WHERE tg_id = $2",
                amount, tg_id)
            await conn.execute("""
                INSERT INTO transactions (user_tg_id, amount, tx_type, description)
                VALUES ($1, $2, 'bonus', $3)
            """, tg_id, amount, f"reward:{reward_id}")
            bal = await conn.fetchval("SELECT balance FROM users WHERE tg_id = $1", tg_id)
            return {"credited": amount, "balance": bal}


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
    if "is_new" in row:
        out["is_new"] = bool(row.get("is_new"))
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
                   (definition->>'needPhoto')::boolean AS need_photo,
                   (definition->>'isNew')::boolean AS is_new
            FROM templates WHERE enabled = TRUE AND type = $1
            ORDER BY sort_order, id
        """, type_filter)
    else:
        rows = await pool.fetch("""
            SELECT id, type, cost, sort_order, category, title, preview, featured,
                   (definition->>'needPhoto')::boolean AS need_photo,
                   (definition->>'isNew')::boolean AS is_new
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


# ── Support tickets ──────────────────────────────────────────────────

async def get_or_create_support_ticket(tg_id: int) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT id, user_tg_id, status, agent_tg_id, agent_name, created_at, updated_at
            FROM support_tickets
            WHERE user_tg_id = $1 AND status IN ('open', 'assigned')
            ORDER BY created_at DESC LIMIT 1
        """, tg_id)
        if row:
            return dict(row)
        row = await conn.fetchrow("""
            INSERT INTO support_tickets (user_tg_id)
            VALUES ($1) RETURNING id, user_tg_id, status, agent_tg_id, agent_name, created_at, updated_at
        """, tg_id)
        return dict(row)


async def get_support_ticket_for_user(tg_id: int) -> Optional[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT id, user_tg_id, status, agent_tg_id, agent_name, created_at, updated_at
            FROM support_tickets
            WHERE user_tg_id = $1 AND status IN ('open', 'assigned')
            ORDER BY created_at DESC LIMIT 1
        """, tg_id)
        return dict(row) if row else None


async def get_support_ticket_by_id(ticket_id: int) -> Optional[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM support_tickets WHERE id = $1", ticket_id)
        return dict(row) if row else None


async def add_support_message(ticket_id: int, sender: str, content: str,
                              image_url: Optional[str] = None) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow("""
                INSERT INTO support_messages (ticket_id, sender, content, image_url)
                VALUES ($1, $2, $3, $4)
                RETURNING id, ticket_id, sender, content, image_url, created_at
            """, ticket_id, sender, content, image_url)
            await conn.execute(
                "UPDATE support_tickets SET updated_at = NOW() WHERE id = $1",
                ticket_id)
            return dict(row)


async def get_support_messages(ticket_id: int, after_id: int = 0) -> List[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, ticket_id, sender, content, image_url, created_at
            FROM support_messages
            WHERE ticket_id = $1 AND id > $2
            ORDER BY id
        """, ticket_id, after_id)
        return [dict(r) for r in rows]


async def assign_support_ticket(ticket_id: int, agent_tg_id: int,
                                agent_name: str) -> Optional[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            UPDATE support_tickets
            SET status = 'assigned', agent_tg_id = $2, agent_name = $3, updated_at = NOW()
            WHERE id = $1 AND status = 'open'
            RETURNING id, user_tg_id, status, agent_tg_id, agent_name
        """, ticket_id, agent_tg_id, agent_name)
        return dict(row) if row else None


async def get_agent_tickets(agent_tg_id: int) -> List[dict]:
    pool = await get_pool()
    rows = await pool.fetch("""
        SELECT t.id, t.user_tg_id, t.status, t.updated_at, u.username, u.first_name
        FROM support_tickets t
        LEFT JOIN users u ON u.tg_id = t.user_tg_id
        WHERE t.agent_tg_id = $1 AND t.status = 'assigned'
        ORDER BY t.updated_at DESC
    """, agent_tg_id)
    return [dict(r) for r in rows]


async def close_support_ticket(ticket_id: int) -> bool:
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            res = await conn.execute("""
                UPDATE support_tickets
                SET status = 'closed', closed_at = NOW(), updated_at = NOW()
                WHERE id = $1 AND status IN ('open', 'assigned')
            """, ticket_id)
            if res.endswith("1"):
                await conn.execute("""
                    INSERT INTO support_messages (ticket_id, sender, content)
                    VALUES ($1, 'system', 'Обращение закрыто. Рады были помочь! Если появятся вопросы — пишите.')
                """, ticket_id)
                return True
            return False


# ── Support agents (dynamic) ──

async def list_support_agents() -> List[dict]:
    pool = await get_pool()
    rows = await pool.fetch(
        "SELECT tg_id, name, added_at FROM support_agents ORDER BY added_at")
    return [dict(r) for r in rows]


async def add_support_agent(tg_id: int, name: Optional[str] = None) -> bool:
    pool = await get_pool()
    res = await pool.execute("""
        INSERT INTO support_agents (tg_id, name) VALUES ($1, $2)
        ON CONFLICT (tg_id) DO UPDATE SET name = COALESCE(EXCLUDED.name, support_agents.name)
    """, tg_id, name)
    return True


async def update_support_agent(tg_id: int, name: str) -> bool:
    pool = await get_pool()
    res = await pool.execute(
        "UPDATE support_agents SET name = $2 WHERE tg_id = $1", tg_id, name)
    return res.endswith("1")


async def remove_support_agent(tg_id: int) -> bool:
    pool = await get_pool()
    res = await pool.execute(
        "DELETE FROM support_agents WHERE tg_id = $1", tg_id)
    return res.endswith("1")


async def is_support_agent(tg_id: int) -> bool:
    pool = await get_pool()
    return bool(await pool.fetchval(
        "SELECT 1 FROM support_agents WHERE tg_id = $1", tg_id))


async def get_support_agent_ids() -> set:
    pool = await get_pool()
    rows = await pool.fetch("SELECT tg_id FROM support_agents")
    return {r["tg_id"] for r in rows}


async def seed_support_agents(agent_ids: set):
    if not agent_ids:
        return
    pool = await get_pool()
    async with pool.acquire() as conn:
        for aid in agent_ids:
            await conn.execute("""
                INSERT INTO support_agents (tg_id) VALUES ($1)
                ON CONFLICT (tg_id) DO NOTHING
            """, aid)
