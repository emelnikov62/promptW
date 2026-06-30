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
    await _safe_migrations()
    await _seed_owner_account()
    await _seed_templates()
    await _apply_trend_order_v1()
    await _apply_gasstation_nophoto_v1()
    await _apply_gasstation_notice_v1()
    await _apply_video_notice_v1()
    await _apply_gasstation_new_v1()
    await _apply_cateyes_blueeyes_v1()
    await _apply_cateyes_preview_v2()
    await _apply_cateyes_order_v1()


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

            -- Face-similarity verify (Level C): per-gen telemetry for the silent
            -- best-of retry loop. All nullable — only template photo gens with a face
            -- ref populate them; everything else stays NULL. Used for the admin
            -- "Сходство лиц" dashboard and offline threshold calibration.
            ALTER TABLE generations ADD COLUMN IF NOT EXISTS face_attempts SMALLINT;   -- KIE runs made (1..N)
            ALTER TABLE generations ADD COLUMN IF NOT EXISTS face_score REAL;          -- best cosine kept
            ALTER TABLE generations ADD COLUMN IF NOT EXISTS face_accepted BOOLEAN;    -- best >= threshold
            ALTER TABLE generations ADD COLUMN IF NOT EXISTS face_ref_found BOOLEAN;   -- ref had a usable face
            ALTER TABLE generations ADD COLUMN IF NOT EXISTS face_scores JSONB;        -- per-attempt scores
            ALTER TABLE generations ADD COLUMN IF NOT EXISTS face_threshold REAL;      -- threshold at gen time

            -- Admin token refund for a single generation (mirrors payments.refunded_at):
            -- refunded_at IS NOT NULL marks the gen as already refunded => idempotent.
            ALTER TABLE generations ADD COLUMN IF NOT EXISTS refunded_at TIMESTAMPTZ;
            ALTER TABLE generations ADD COLUMN IF NOT EXISTS refunded_by BIGINT;

            CREATE INDEX IF NOT EXISTS idx_generations_user ON generations(user_tg_id);
            -- Hot path: history is "this user's rows, newest first" — a composite
            -- index serves the ORDER BY without a separate sort.
            CREATE INDEX IF NOT EXISTS idx_generations_user_created ON generations(user_tg_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_generations_pending ON generations(status) WHERE status = 'pending';
            CREATE INDEX IF NOT EXISTS idx_transactions_user ON transactions(user_tg_id);
            CREATE INDEX IF NOT EXISTS idx_transactions_user_created ON transactions(user_tg_id, created_at DESC);
            -- NB: idx_payments_* live in the payments-index block BELOW, after the
            -- payments table is created — creating them here breaks a fresh-DB bootstrap.
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
            CREATE INDEX IF NOT EXISTS idx_payments_user_created ON payments(user_tg_id, created_at DESC);

            ALTER TABLE payments ADD COLUMN IF NOT EXISTS refunded_at TIMESTAMPTZ;
            ALTER TABLE payments ADD COLUMN IF NOT EXISTS refund_id TEXT;
            CREATE INDEX IF NOT EXISTS idx_ref_earnings_referrer ON ref_earnings(referrer_tg_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_withdrawals_status ON withdrawals(status, created_at DESC);

            -- Payme (Paycom) Merchant API: оплата в сумах (UZS).
            ALTER TABLE payments ADD COLUMN IF NOT EXISTS currency VARCHAR(3) NOT NULL DEFAULT 'RUB';
            ALTER TABLE payments ADD COLUMN IF NOT EXISTS amount_uzs BIGINT;

            CREATE TABLE IF NOT EXISTS payme_transactions (
                payme_txn_id VARCHAR(40) PRIMARY KEY,        -- id транзакции Payme
                payment_id   BIGINT NOT NULL REFERENCES payments(id),
                order_id     UUID NOT NULL REFERENCES payments(order_id),
                state        SMALLINT NOT NULL,              -- 1 | 2 | -1 | -2
                amount_tiyin BIGINT NOT NULL,               -- сумма в тийинах (как прислал Payme)
                create_time  BIGINT,                        -- ms epoch
                perform_time BIGINT,
                cancel_time  BIGINT,
                reason       SMALLINT,
                created_at   TIMESTAMPTZ DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_payme_txn_order  ON payme_transactions(order_id);
            CREATE INDEX IF NOT EXISTS idx_payme_txn_ctime  ON payme_transactions(create_time);

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

            -- Last WebApp activity (heartbeat) — used to suppress duplicate TG
            -- notifications while the user is actively in the app.
            ALTER TABLE users ADD COLUMN IF NOT EXISTS last_active_at TIMESTAMPTZ;

            -- Marketing/engagement notification opt-out (transactional ones ignore it).
            ALTER TABLE users ADD COLUMN IF NOT EXISTS notif_marketing BOOLEAN DEFAULT TRUE;

            -- Log of engagement notifications sent — drives frequency caps / once-ever
            -- dedup that survive restarts (state in DB, not memory).
            CREATE TABLE IF NOT EXISTS notification_sends (
                id BIGSERIAL PRIMARY KEY,
                user_tg_id BIGINT NOT NULL REFERENCES users(tg_id),
                kind VARCHAR(40) NOT NULL,
                sent_at TIMESTAMPTZ DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_notif_sends_user_kind
                ON notification_sends(user_tg_id, kind, sent_at DESC);

            -- Generic key/value app settings (admin-toggleable flags, e.g. the
            -- engagement-sweep kill switch).
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TIMESTAMPTZ DEFAULT NOW()
            );

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

            ALTER TABLE payments ADD COLUMN IF NOT EXISTS bonus_pct INTEGER DEFAULT 0;
            ALTER TABLE payments ADD COLUMN IF NOT EXISTS bonus_tokens INTEGER DEFAULT 0;
            ALTER TABLE payments ADD COLUMN IF NOT EXISTS promo_id BIGINT;

            CREATE TABLE IF NOT EXISTS promo_codes (
                id BIGSERIAL PRIMARY KEY,
                code VARCHAR(40) UNIQUE NOT NULL,
                type VARCHAR(20) NOT NULL,
                value INTEGER NOT NULL,
                max_uses INTEGER DEFAULT 0,
                used_count INTEGER DEFAULT 0,
                enabled BOOLEAN DEFAULT TRUE,
                expires_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
            CREATE TABLE IF NOT EXISTS promo_activations (
                id BIGSERIAL PRIMARY KEY,
                promo_id BIGINT NOT NULL REFERENCES promo_codes(id),
                user_tg_id BIGINT NOT NULL REFERENCES users(tg_id),
                tokens_given INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE(promo_id, user_tg_id)
            );
            -- Rewards ("Награды"): one credit per (user, reward) — UNIQUE makes the
            -- crediting idempotent at the storage layer (mirrors promo_activations).
            CREATE TABLE IF NOT EXISTS reward_claims (
                id BIGSERIAL PRIMARY KEY,
                user_tg_id BIGINT NOT NULL REFERENCES users(tg_id),
                reward_id  TEXT  NOT NULL,
                amount     INTEGER NOT NULL,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE(user_tg_id, reward_id)
            );

            CREATE TABLE IF NOT EXISTS support_tickets (
                id BIGSERIAL PRIMARY KEY,
                user_tg_id BIGINT NOT NULL REFERENCES users(tg_id),
                status VARCHAR(20) DEFAULT 'open',
                agent_tg_id BIGINT,
                agent_name VARCHAR(255),
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW(),
                closed_at TIMESTAMPTZ
            );
            CREATE INDEX IF NOT EXISTS idx_support_tickets_user ON support_tickets(user_tg_id, created_at DESC);

            CREATE TABLE IF NOT EXISTS support_messages (
                id BIGSERIAL PRIMARY KEY,
                ticket_id BIGINT NOT NULL REFERENCES support_tickets(id) ON DELETE CASCADE,
                sender VARCHAR(12) NOT NULL,
                content TEXT NOT NULL,
                image_url TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_support_messages_ticket ON support_messages(ticket_id, id);

            ALTER TABLE support_messages ADD COLUMN IF NOT EXISTS image_url TEXT;

            CREATE TABLE IF NOT EXISTS support_agents (
                tg_id BIGINT PRIMARY KEY,
                name VARCHAR(255),
                added_at TIMESTAMPTZ DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS admin_accounts (
                tg_id BIGINT PRIMARY KEY,
                login VARCHAR(64) UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                role VARCHAR(16) NOT NULL DEFAULT 'agent',
                disabled BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
        """)


async def _safe_migrations():
    """Best-effort migrations that MUST NOT block startup if they fail (e.g. a unique
    index that can't be built because of pre-existing duplicate data). Each runs in its
    own statement; a failure is logged and skipped rather than aborting boot."""
    # Defense-in-depth backstop against double-crediting a referral commission: settle
    # already prevents it via the pending->paid flip, but a unique index guarantees it
    # at the storage layer even if a future code path re-runs crediting. (payment_id is
    # nullable; Postgres treats NULLs as distinct, so non-commission rows are unaffected.)
    async with _pool.acquire() as conn:
        try:
            await conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_ref_earnings_payment_line "
                "ON ref_earnings(payment_id, line)")
        except Exception:
            logger.warning("ref_earnings unique index not created (pre-existing dupes?) "
                           "— skipping; settle's pending-flip still prevents double-credit")


async def _seed_owner_account():
    """Seed the env ADMIN_LOGIN/PASSWORD as the first 'owner' account (idempotent).
    Lets the existing operator keep logging in; agents are added via the panel."""
    import os, hashlib
    login = os.getenv("ADMIN_LOGIN", ""); pw = os.getenv("ADMIN_PASSWORD", "")
    ids = [int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x]
    if not (login and pw and ids):
        return
    tg_id = ids[0]
    ph = hashlib.sha256(pw.encode()).hexdigest()
    async with _pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO admin_accounts (tg_id, login, password_hash, role)
            VALUES ($1,$2,$3,'owner')
            ON CONFLICT (tg_id) DO UPDATE SET login=$2, password_hash=$3, role='owner'
        """, tg_id, login, ph)


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
                # One-time backfill: add any new language keys from the seed into the
                # existing title JSONB. Runs only when a key is absent — never overwrites
                # existing translations (admin-edited or already correct).
                seed_title = r.get("title") or {}
                for lang, val in seed_title.items():
                    if val:
                        await conn.execute("""
                            UPDATE templates
                            SET title = title || jsonb_build_object($2::text, $3::text)
                            WHERE id = $1 AND NOT (title ? $2)
                        """, r["id"], lang, val)
        if inserted:
            logger.info("Seeded %d new template(s)", inserted)
    except Exception:
        # Seeding must never block startup — the app can run with an empty/partial
        # templates table (re-runs are idempotent via ON CONFLICT DO NOTHING).
        logger.exception("template seed failed")


async def _apply_trend_order_v1():
    """One-time: pin the first six trends to the owner-curated order. Existing rows
    are owned by the admin (the seed's ON CONFLICT DO NOTHING never updates them), so
    a deploy can't reorder them — this does, exactly once. Negative sort_order keeps
    these six ahead of every other template (all >= 0) regardless of their values;
    ORDER BY (sort_order, id) then renders them in this precise sequence. Guarded by an
    app_settings flag so it runs ONCE and never clobbers later admin reordering."""
    order = [
        ("gasstation-broom-video", -6),  # 1. Улетела с заправки
        ("car-dubai-gwagon-photo", -5),  # 2. Гелик в Дубае
        ("birthday-photo", -4),          # 3. С днём рождения фото
        ("birthday-video", -3),          # 4. С днём рождения видео
        ("yacht-photo", -2),             # 5. На яхте фото
        ("yacht-video", -1),             # 6. На яхте видео
    ]
    try:
        async with _pool.acquire() as conn:
            if await conn.fetchval("SELECT value FROM app_settings WHERE key = 'trend_order_v1'"):
                return
            for tid, so in order:
                await conn.execute(
                    "UPDATE templates SET sort_order = $2, updated_at = NOW() WHERE id = $1", tid, so)
            await conn.execute(
                "INSERT INTO app_settings (key, value, updated_at) VALUES ('trend_order_v1', '1', NOW()) "
                "ON CONFLICT (key) DO UPDATE SET value = '1', updated_at = NOW()")
        logger.info("Applied trend_order_v1 (curated first-six trends)")
    except Exception:
        logger.exception("trend_order_v1 failed")


async def _apply_gasstation_nophoto_v1():
    """One-time: drop the `needPhoto` flag from the gas-station trend's definition so
    the gallery/detail no longer shows the "СНАЧАЛА СОЗДАЙ ФОТО" badge — that trend takes
    a raw selfie + car photo directly, no pre-generated photo step. The 2-photo
    requirement stays (minPhotos/maxPhotos). Existing rows are admin-owned (seed won't
    update them); guarded by app_settings so it runs once."""
    try:
        async with _pool.acquire() as conn:
            if await conn.fetchval("SELECT value FROM app_settings WHERE key = 'gasstation_nophoto_v1'"):
                return
            await conn.execute(
                "UPDATE templates SET definition = definition - 'needPhoto', updated_at = NOW() "
                "WHERE id = 'gasstation-broom-video'")
            await conn.execute(
                "INSERT INTO app_settings (key, value, updated_at) VALUES ('gasstation_nophoto_v1', '1', NOW()) "
                "ON CONFLICT (key) DO UPDATE SET value = '1', updated_at = NOW()")
        logger.info("Applied gasstation_nophoto_v1")
    except Exception:
        logger.exception("gasstation_nophoto_v1 failed")


async def _apply_gasstation_notice_v1():
    """One-time: give the gas-station trend a custom upload notice (selfie + car, NOT
    "2-3 face photos") and a shorter scene-setting description (drop the "prompt is
    built in" line). Merges into the existing admin-owned definition via `||`; guarded
    by app_settings so it runs once."""
    patch = json.dumps({
        "uploadNotice": {
            "ru": {"title": "Нужно ровно 2 фото", "items": ["Селфи — лицо крупным планом, анфас, без тёмных очков", "Фото вашей машины"]},
            "en": {"title": "Exactly 2 photos", "items": ["A selfie — face close-up, front view, no sunglasses", "A photo of your car"]},
            "es": {"title": "Exactamente 2 fotos", "items": ["Un selfie — rostro de cerca, de frente, sin gafas de sol", "Una foto de tu coche"]},
        },
        "desc": {
            "ru": "Очередь, табло «нет топлива» — и ты эффектно улетаешь с заправки на метле.",
            "en": "A queue, a 'no fuel' sign — and you dramatically fly off from the gas station on a broom.",
            "es": "Una cola, un cartel de 'sin combustible' — y te vas volando de la gasolinera en una escoba.",
        },
    }, ensure_ascii=False)
    try:
        async with _pool.acquire() as conn:
            if await conn.fetchval("SELECT value FROM app_settings WHERE key = 'gasstation_notice_v1'"):
                return
            await conn.execute(
                "UPDATE templates SET definition = definition || $1::jsonb, updated_at = NOW() "
                "WHERE id = 'gasstation-broom-video'", patch)
            await conn.execute(
                "INSERT INTO app_settings (key, value, updated_at) VALUES ('gasstation_notice_v1', '1', NOW()) "
                "ON CONFLICT (key) DO UPDATE SET value = '1', updated_at = NOW()")
        logger.info("Applied gasstation_notice_v1")
    except Exception:
        logger.exception("gasstation_notice_v1 failed")


async def _apply_video_notice_v1():
    """One-time: the birthday/yacht video trends consume the SINGLE photo made in their
    neighbouring photo template — not "2-3 face photos". Give each a custom uploadNotice
    saying so, and pin yacht-video to exactly 1 photo (maxPhotos). Merges into the
    admin-owned definition via `||`; guarded by app_settings so it runs once."""
    patches = {
        "birthday-video": {
            "uploadNotice": {
                "ru": {"title": "Нужно 1 фото", "text": "То самое фото, которое вы создали в соседнем шаблоне «С днём рождения фото» — именно для этого видео."},
                "en": {"title": "Just 1 photo", "text": "The photo you created in the neighbouring «Birthday photo» template — the one made for this video."},
                "es": {"title": "Solo 1 foto", "text": "La foto que creaste en la plantilla vecina «Foto de cumpleaños» — la hecha para este video."},
            },
        },
        "yacht-video": {
            "maxPhotos": 1,
            "uploadNotice": {
                "ru": {"title": "Нужно 1 фото", "text": "То самое фото, которое вы создали в соседнем шаблоне «На яхте фото» — именно для этого видео."},
                "en": {"title": "Just 1 photo", "text": "The photo you created in the neighbouring «Yacht photo» template — the one made for this video."},
                "es": {"title": "Solo 1 foto", "text": "La foto que creaste en la plantilla vecina «Foto en yate» — la hecha para este video."},
            },
        },
    }
    try:
        async with _pool.acquire() as conn:
            if await conn.fetchval("SELECT value FROM app_settings WHERE key = 'video_notice_v1'"):
                return
            for tid, patch in patches.items():
                await conn.execute(
                    "UPDATE templates SET definition = definition || $2::jsonb, updated_at = NOW() WHERE id = $1",
                    tid, json.dumps(patch, ensure_ascii=False))
            await conn.execute(
                "INSERT INTO app_settings (key, value, updated_at) VALUES ('video_notice_v1', '1', NOW()) "
                "ON CONFLICT (key) DO UPDATE SET value = '1', updated_at = NOW()")
        logger.info("Applied video_notice_v1")
    except Exception:
        logger.exception("video_notice_v1 failed")


async def _apply_gasstation_new_v1():
    """One-time: switch on the NEW badge for the gas-station trend (definition.isNew).
    Merges into the admin-owned definition via `||`; guarded by app_settings so it runs
    once and never overrides a later admin toggle of the badge."""
    try:
        async with _pool.acquire() as conn:
            if await conn.fetchval("SELECT value FROM app_settings WHERE key = 'gasstation_new_v1'"):
                return
            await conn.execute(
                "UPDATE templates SET definition = definition || '{\"isNew\": true}'::jsonb, updated_at = NOW() "
                "WHERE id = 'gasstation-broom-video'")
            await conn.execute(
                "INSERT INTO app_settings (key, value, updated_at) VALUES ('gasstation_new_v1', '1', NOW()) "
                "ON CONFLICT (key) DO UPDATE SET value = '1', updated_at = NOW()")
        logger.info("Applied gasstation_new_v1")
    except Exception:
        logger.exception("gasstation_new_v1 failed")


async def _apply_cateyes_blueeyes_v1():
    """One-time: the two cat-eyes girl templates were seeded with the cat's eyes
    matching the woman's (real colour from the reference). Per request the cat must
    have bright-blue eyes (as in the original prompt); the woman's eyes stay real.
    String-replaces the segment inside definition (covers both prompt and skeleton);
    guarded by app_settings so it runs once and never clobbers later admin edits."""
    old = "выраженные блики в глазах; глаза того же цвета, что и глаза девушки (как на референсе)."
    new = "ярко-голубые глаза с выраженными бликами."
    try:
        async with _pool.acquire() as conn:
            if await conn.fetchval("SELECT value FROM app_settings WHERE key = 'cateyes_blueeyes_v1'"):
                return
            await conn.execute(
                "UPDATE templates SET definition = replace(definition::text, $1, $2)::jsonb, "
                "updated_at = NOW() WHERE id IN ('girl-cateyes-photo', 'girl-cateyes-hijab-photo')",
                old, new)
            await conn.execute(
                "INSERT INTO app_settings (key, value, updated_at) VALUES ('cateyes_blueeyes_v1', '1', NOW()) "
                "ON CONFLICT (key) DO UPDATE SET value = '1', updated_at = NOW()")
        logger.info("Applied cateyes_blueeyes_v1")
    except Exception:
        logger.exception("cateyes_blueeyes_v1 failed")


async def _apply_cateyes_preview_v2():
    """One-time: the cat-eyes previews were regenerated in place (blue cat eyes) at the
    same /static/tpl path, so the 1h-cached old preview would linger. Bump the stored
    preview URL with ?v=2 to bust caches. Guarded by app_settings so it runs once."""
    bumps = {
        "girl-cateyes-photo": "/static/tpl/girl-cateyes-photo.jpg?v=2",
        "girl-cateyes-hijab-photo": "/static/tpl/girl-cateyes-hijab-photo.jpg?v=2",
    }
    try:
        async with _pool.acquire() as conn:
            if await conn.fetchval("SELECT value FROM app_settings WHERE key = 'cateyes_preview_v2'"):
                return
            for tid, url in bumps.items():
                await conn.execute(
                    "UPDATE templates SET preview = $2::jsonb, updated_at = NOW() WHERE id = $1",
                    tid, json.dumps({"img": url, "full": url}))
            await conn.execute(
                "INSERT INTO app_settings (key, value, updated_at) VALUES ('cateyes_preview_v2', '1', NOW()) "
                "ON CONFLICT (key) DO UPDATE SET value = '1', updated_at = NOW()")
        logger.info("Applied cateyes_preview_v2")
    except Exception:
        logger.exception("cateyes_preview_v2 failed")


async def _apply_cateyes_order_v1():
    """One-time: move the two cat-eyes templates from the front of the girls section
    to the middle (between sort_order 24 and 101). Guarded by app_settings so it runs
    once and never clobbers a later manual reorder."""
    order = {"girl-cateyes-photo": 50, "girl-cateyes-hijab-photo": 51}
    try:
        async with _pool.acquire() as conn:
            if await conn.fetchval("SELECT value FROM app_settings WHERE key = 'cateyes_order_v1'"):
                return
            for tid, so in order.items():
                await conn.execute(
                    "UPDATE templates SET sort_order = $2, updated_at = NOW() WHERE id = $1",
                    tid, so)
            await conn.execute(
                "INSERT INTO app_settings (key, value, updated_at) VALUES ('cateyes_order_v1', '1', NOW()) "
                "ON CONFLICT (key) DO UPDATE SET value = '1', updated_at = NOW()")
        logger.info("Applied cateyes_order_v1")
    except Exception:
        logger.exception("cateyes_order_v1 failed")
