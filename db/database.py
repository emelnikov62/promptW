import asyncpg
from typing import Optional

_pool: Optional[asyncpg.Pool] = None


async def init_db(dsn: str):
    global _pool
    _pool = await asyncpg.create_pool(dsn, min_size=2, max_size=10)
    await _create_tables()


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

            CREATE INDEX IF NOT EXISTS idx_generations_user ON generations(user_tg_id);
            CREATE INDEX IF NOT EXISTS idx_generations_pending ON generations(status) WHERE status = 'pending';
            CREATE INDEX IF NOT EXISTS idx_transactions_user ON transactions(user_tg_id);
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
        """)
