import asyncpg
import json
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

_pool: Optional[asyncpg.Pool] = None


async def init_db(dsn: str):
    global _pool
    _pool = await asyncpg.create_pool(dsn, min_size=2, max_size=10)
    await _create_tables()
    await _seed_templates()


async def get_pool() -> asyncpg.Pool:
    return _pool


async def close_db():
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


async def _create_tables():
    async with _pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id BIGSERIAL PRIMARY KEY,
                tg_id BIGINT UNIQUE NOT NULL,
                username VARCHAR(255),
                first_name VARCHAR(255),
                last_name VARCHAR(255),
                lang VARCHAR(5) DEFAULT 'ru',
                balance INTEGER DEFAULT 0,
                referrer_id BIGINT REFERENCES users(tg_id),
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS generations (
                id BIGSERIAL PRIMARY KEY,
                user_tg_id BIGINT NOT NULL REFERENCES users(tg_id),
                gen_type VARCHAR(20) NOT NULL,
                model VARCHAR(100),
                prompt TEXT,
                settings JSONB DEFAULT '{}',
                result_url TEXT,
                status VARCHAR(20) DEFAULT 'pending',
                cost INTEGER DEFAULT 0,
                created_at TIMESTAMPTZ DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS transactions (
                id BIGSERIAL PRIMARY KEY,
                user_tg_id BIGINT NOT NULL REFERENCES users(tg_id),
                amount INTEGER NOT NULL,
                tx_type VARCHAR(20) NOT NULL,
                description TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS referrals (
                id BIGSERIAL PRIMARY KEY,
                referrer_tg_id BIGINT NOT NULL REFERENCES users(tg_id),
                referred_tg_id BIGINT UNIQUE NOT NULL REFERENCES users(tg_id),
                bonus_paid BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMPTZ DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS chat_dialogs (
                id BIGSERIAL PRIMARY KEY,
                user_tg_id BIGINT NOT NULL REFERENCES users(tg_id),
                title VARCHAR(120),
                model VARCHAR(40),
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS chat_messages (
                id BIGSERIAL PRIMARY KEY,
                dialog_id BIGINT NOT NULL REFERENCES chat_dialogs(id) ON DELETE CASCADE,
                role VARCHAR(12) NOT NULL,
                content TEXT NOT NULL,
                created_at TIMESTAMPTZ DEFAULT NOW()
            );

            -- KIE task id, stored as soon as the task is created so a restart-killed
            -- in-flight generation can be recovered (re-polled) on the next startup.
            ALTER TABLE generations ADD COLUMN IF NOT EXISTS provider_task_id TEXT;
            -- A photo gen with count=N runs N KIE tasks and yields N images. Store the
            -- FULL set so the whole batch survives a restart (recovery) and reloads
            -- (history), not just the first. Legacy single columns above keep holding
            -- the first element for back-compat.
            ALTER TABLE generations ADD COLUMN IF NOT EXISTS provider_task_ids JSONB DEFAULT '[]';
            ALTER TABLE generations ADD COLUMN IF NOT EXISTS result_urls JSONB DEFAULT '[]';

            CREATE INDEX IF NOT EXISTS idx_generations_user ON generations(user_tg_id);
            -- Hot path: history is "this user's rows, newest first" — a composite
            -- index serves the ORDER BY without a separate sort.
            CREATE INDEX IF NOT EXISTS idx_generations_user_created ON generations(user_tg_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_generations_pending ON generations(status) WHERE status = 'pending';
            CREATE INDEX IF NOT EXISTS idx_transactions_user ON transactions(user_tg_id);
            CREATE INDEX IF NOT EXISTS idx_transactions_user_created ON transactions(user_tg_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_payments_user_created ON payments(user_tg_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_referrals_referrer ON referrals(referrer_tg_id);
            CREATE INDEX IF NOT EXISTS idx_chat_dialogs_user ON chat_dialogs(user_tg_id, updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_chat_messages_dialog ON chat_messages(dialog_id, id);

            -- Partner earnings are paid in RUBLES, separate from the token balance.
            ALTER TABLE users ADD COLUMN IF NOT EXISTS ref_balance NUMERIC(12,2) DEFAULT 0;

            -- Top-up orders (ЮMoney for RF, Platega for CIS).
            CREATE TABLE IF NOT EXISTS payments (
                id BIGSERIAL PRIMARY KEY,
                order_id UUID UNIQUE NOT NULL,
                user_tg_id BIGINT NOT NULL REFERENCES users(tg_id),
                provider VARCHAR(20) NOT NULL,
                amount_rub NUMERIC(12,2) NOT NULL,
                tokens INTEGER NOT NULL,
                status VARCHAR(20) DEFAULT 'pending',
                external_id VARCHAR(80),
                created_at TIMESTAMPTZ DEFAULT NOW(),
                paid_at TIMESTAMPTZ
            );

            -- Referral commissions credited to a referrer's ruble balance.
            CREATE TABLE IF NOT EXISTS ref_earnings (
                id BIGSERIAL PRIMARY KEY,
                referrer_tg_id BIGINT NOT NULL REFERENCES users(tg_id),
                referred_tg_id BIGINT NOT NULL REFERENCES users(tg_id),
                line SMALLINT NOT NULL,
                amount_rub NUMERIC(12,2) NOT NULL,
                payment_id BIGINT REFERENCES payments(id),
                created_at TIMESTAMPTZ DEFAULT NOW()
            );

            -- Withdrawal requests (card ₽ / crypto USDT), processed manually.
            CREATE TABLE IF NOT EXISTS withdrawals (
                id BIGSERIAL PRIMARY KEY,
                user_tg_id BIGINT NOT NULL REFERENCES users(tg_id),
                method VARCHAR(20) NOT NULL,
                details VARCHAR(200) NOT NULL,
                amount_rub NUMERIC(12,2) NOT NULL,
                status VARCHAR(20) DEFAULT 'pending',
                created_at TIMESTAMPTZ DEFAULT NOW(),
                processed_at TIMESTAMPTZ
            );

            CREATE INDEX IF NOT EXISTS idx_payments_status ON payments(status);
            CREATE INDEX IF NOT EXISTS idx_ref_earnings_referrer ON ref_earnings(referrer_tg_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_withdrawals_status ON withdrawals(status, created_at DESC);

            -- Saved reference photos ("Мой референс") reused in photo generation.
            CREATE TABLE IF NOT EXISTS user_references (
                id BIGSERIAL PRIMARY KEY,
                user_tg_id BIGINT NOT NULL REFERENCES users(tg_id),
                file_url TEXT NOT NULL,
                title VARCHAR(60),
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_user_references_user ON user_references(user_tg_id, created_at DESC);

            -- Admin audit log
            CREATE TABLE IF NOT EXISTS admin_audit_log (
                id BIGSERIAL PRIMARY KEY,
                admin_tg_id BIGINT NOT NULL,
                action VARCHAR(60) NOT NULL,
                target_type VARCHAR(40),
                target_id VARCHAR(80),
                before JSONB,
                after JSONB,
                reason TEXT,
                ip VARCHAR(45),
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_audit_log_created ON admin_audit_log(created_at DESC);

            -- User ban/note fields
            ALTER TABLE users ADD COLUMN IF NOT EXISTS banned BOOLEAN DEFAULT FALSE;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS admin_note TEXT;

            -- Generation templates ("тренды"). Light columns for the gallery list +
            -- a `definition` JSONB holding the heavy prompt/params/settings payload.
            CREATE TABLE IF NOT EXISTS templates (
                id TEXT PRIMARY KEY,
                type VARCHAR(20) NOT NULL,
                enabled BOOLEAN DEFAULT TRUE,
                sort_order INT DEFAULT 0,
                category TEXT,
                cost INTEGER NOT NULL DEFAULT 0,
                title JSONB NOT NULL DEFAULT '{}',
                preview JSONB NOT NULL DEFAULT '{}',
                definition JSONB NOT NULL DEFAULT '{}',
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            );
            ALTER TABLE templates ADD COLUMN IF NOT EXISTS featured BOOLEAN DEFAULT FALSE;
            CREATE INDEX IF NOT EXISTS idx_templates_enabled ON templates(enabled, sort_order);
        """)


async def _seed_templates():
    """Bootstrap templates from the repo seed. Admin owns rows once they exist:
    INSERT ... ON CONFLICT DO NOTHING means a deploy only adds NEW templates and
    never overwrites edits made through the admin panel."""
    seed_path = os.path.join(os.path.dirname(__file__), "templates_seed.json")
    if not os.path.exists(seed_path):
        logger.warning("templates_seed.json not found, skipping template seed")
        return
    try:
        with open(seed_path, encoding="utf-8") as f:
            rows = json.load(f)
        inserted = 0
        async with _pool.acquire() as conn:
            for r in rows:
                res = await conn.execute("""
                    INSERT INTO templates (id, type, enabled, sort_order, category, cost, title, preview, definition, featured)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                    ON CONFLICT (id) DO NOTHING
                """, r["id"], r["type"], r.get("enabled", True), r.get("sort_order", 0),
                    r.get("category"), r.get("cost", 0),
                    json.dumps(r.get("title") or {}, ensure_ascii=False),
                    json.dumps(r.get("preview") or {}, ensure_ascii=False),
                    json.dumps(r.get("definition") or {}, ensure_ascii=False),
                    r.get("featured", False))
                if res.endswith("1"):
                    inserted += 1
                # One-time backfill of featured/category for rows inserted BEFORE those
                # fields existed (still in the pristine default state). Runs once per row;
                # never clobbers admin choices (skips as soon as either field is set).
                await conn.execute("""
                    UPDATE templates SET featured = $2, category = $3
                    WHERE id = $1 AND featured = FALSE AND category IS NULL
                """, r["id"], r.get("featured", False), r.get("category"))
        if inserted:
            logger.info("Seeded %d new template(s)", inserted)
    except Exception:
        # Seeding must never block startup — the app can run with an empty/partial
        # templates table (re-runs are idempotent via ON CONFLICT DO NOTHING).
        logger.exception("template seed failed")
