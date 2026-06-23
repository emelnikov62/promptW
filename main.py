import asyncio
import hashlib
import logging
import os

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

from bot.config import BOT_TOKEN
from bot.handlers import router, setup as setup_bot
from api.routes import routes, setup as setup_api, auth_middleware, start_reconciler
from api.admin_routes import admin_routes
from db.database import init_db, close_db
import storage

logging.basicConfig(level=logging.INFO)

WEBAPP_HOST = os.getenv("WEBAPP_HOST", "0.0.0.0")
WEBAPP_PORT = int(os.getenv("WEBAPP_PORT", "8080"))
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")
MEDIA_DIR = os.getenv("MEDIA_DIR", "/tmp")
DATABASE_URL = os.getenv("DATABASE_URL", "")
KIE_API_KEY = os.getenv("KIE_API_KEY", "")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _create_generator():
    if KIE_API_KEY:
        from generators.kie import KieGenerator
        callback_url = (WEBHOOK_URL + "/api/callback") if WEBHOOK_URL else None
        return KieGenerator(api_key=KIE_API_KEY, callback_url=callback_url)
    from generators.stub import StubGenerator
    return StubGenerator()


async def index(request: web.Request):
    return web.FileResponse(os.path.join(BASE_DIR, "webapp", "templates", "index.html"))


async def terms_page(request: web.Request):
    return web.FileResponse(os.path.join(BASE_DIR, "webapp", "templates", "terms.html"))


async def privacy_page(request: web.Request):
    return web.FileResponse(os.path.join(BASE_DIR, "webapp", "templates", "privacy.html"))


async def offer_page(request: web.Request):
    return web.FileResponse(os.path.join(BASE_DIR, "webapp", "templates", "offer.html"))


async def admin_index(request: web.Request):
    resp = web.FileResponse(os.path.join(BASE_DIR, "webapp", "templates", "admin.html"))
    resp.headers["X-Robots-Tag"] = "noindex"
    return resp


@web.middleware
async def security_headers(request: web.Request, handler):
    resp = await handler(request)
    # Stop browsers from MIME-sniffing user-uploaded media into executable types.
    if request.path.startswith("/media/"):
        resp.headers["X-Content-Type-Options"] = "nosniff"
    # Cache static assets so the Telegram webview stops re-downloading them on every
    # reopen. Versioned JS/CSS (?v=N busts the URL) can cache forever; preview images
    # change in place without a version bump, so they get a short cache instead.
    elif request.path.startswith("/static/") and resp.status in (200, 206):
        if request.path.startswith(("/static/js/", "/static/css/")):
            resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            resp.headers.setdefault("Cache-Control", "public, max-age=3600")
    return resp


def create_app() -> web.Application:
    app = web.Application(middlewares=[security_headers, auth_middleware])

    gen = _create_generator()
    setup_bot(gen)
    setup_api(gen)

    # Surface the active file-storage backend so a misconfigured prod (silent
    # fallback to local /tmp) is obvious in the logs at boot.
    logging.getLogger(__name__).info(
        "storage backend: %s (bucket=%s)", storage.backend(),
        storage.S3_BUCKET if storage.is_s3() else "-")

    app.router.add_get("/", index)
    app.router.add_get("/terms", terms_page)
    app.router.add_get("/privacy", privacy_page)
    app.router.add_get("/offer", offer_page)
    app.router.add_get("/admin", admin_index)
    app.router.add_get("/admin/", admin_index)
    app.router.add_static("/static", os.path.join(BASE_DIR, "webapp", "static"))
    app.router.add_static("/media", MEDIA_DIR)
    app.router.add_routes(routes)
    app.router.add_routes(admin_routes)

    return app


async def main():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set — configure it in .env")
    # AUTH_ENFORCE=0 disables Telegram initData checks and trusts a body/query
    # tg_id — a full cross-user IDOR. Allow it ONLY for local dev (DEV=1).
    if os.getenv("AUTH_ENFORCE", "1") != "1" and os.getenv("DEV", "0") != "1":
        raise RuntimeError(
            "AUTH_ENFORCE=0 is unsafe in production (cross-user IDOR). "
            "Set AUTH_ENFORCE=1, or set DEV=1 to allow it for local development.")
    # BILLING_ENFORCE=0 generates for free and never deducts/refunds, while still
    # stamping `cost` on rows — corrupting accounting. Allow it ONLY for local dev (DEV=1),
    # mirroring the AUTH_ENFORCE guard, so it can't silently ship to prod.
    if os.getenv("BILLING_ENFORCE", "1") != "1" and os.getenv("DEV", "0") != "1":
        raise RuntimeError(
            "BILLING_ENFORCE=0 means free generation + no charge/refund (corrupted "
            "accounting) — unsafe in production. Set BILLING_ENFORCE=1, or DEV=1 for "
            "local development.")
    await init_db(DATABASE_URL)
    logging.info("Database connected")

    # Load server-authoritative template prices from the DB into the pricing cache.
    # Falls back to the hardcoded defaults in pricing.py if this fails.
    try:
        from db.queries import get_template_costs
        from pricing import refresh_template_costs
        refresh_template_costs(await get_template_costs())
    except Exception:
        logging.exception("failed to load template costs from DB; using fallback")

    gen_name = "KieGenerator" if KIE_API_KEY else "StubGenerator"
    logging.info("Generator: %s", gen_name)

    # Warm up the face-similarity model off the request path (the first request would
    # otherwise block the loop loading it). Only when the feature is actually enabled.
    if os.getenv("FACE_VERIFY", "0") == "1" or os.getenv("FACE_VERIFY_SHADOW", "0") == "1":
        try:
            import face_verify
            ok = await face_verify.aavailable()
            logging.info("face_verify warmup: %s", "ready" if ok else "unavailable (fail-open)")
        except Exception:
            logging.exception("face_verify warmup failed (feature stays off)")

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)

    # Blue "menu" button next to the chat input → opens the WebApp.
    webapp_url = os.getenv("WEBAPP_URL", "")
    if webapp_url:
        from aiogram.types import MenuButtonWebApp, WebAppInfo
        try:
            await bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(text="🚀 PromptW", web_app=WebAppInfo(url=webapp_url))
            )
        except Exception as e:
            logging.warning("set_chat_menu_button failed: %s", e)

    app = create_app()

    # Recover generations whose in-flight task was killed by a previous restart
    # (re-poll KIE by the persisted task id), then keep sweeping periodically.
    start_reconciler(interval=60)

    if WEBHOOK_URL:
        webhook_path = "/webhook"
        # Authenticate the webhook: Telegram echoes this secret in the
        # X-Telegram-Bot-Api-Secret-Token header and aiogram rejects mismatches, so
        # forged updates posted straight to /webhook (which bypasses /api auth) are
        # dropped. Derived from BOT_TOKEN if WEBHOOK_SECRET isn't set, so it's stable
        # across restarts without extra config.
        webhook_secret = os.getenv("WEBHOOK_SECRET") or hashlib.sha256(
            ("whsec:" + BOT_TOKEN).encode()).hexdigest()
        await bot.set_webhook(f"{WEBHOOK_URL}{webhook_path}", secret_token=webhook_secret)
        SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=webhook_secret).register(app, path=webhook_path)
        setup_application(app, dp, bot=bot)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, WEBAPP_HOST, WEBAPP_PORT)
        await site.start()
        logging.info(f"WebApp running at http://{WEBAPP_HOST}:{WEBAPP_PORT}")
        await asyncio.Event().wait()
    else:
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, WEBAPP_HOST, WEBAPP_PORT)
        await site.start()
        logging.info(f"WebApp running at http://{WEBAPP_HOST}:{WEBAPP_PORT}")
        await dp.start_polling(bot)

    await close_db()


if __name__ == "__main__":
    asyncio.run(main())
