import asyncio
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
    return resp


def create_app() -> web.Application:
    app = web.Application(middlewares=[security_headers, auth_middleware])

    gen = _create_generator()
    setup_bot(gen)
    setup_api(gen)

    app.router.add_get("/", index)
    app.router.add_get("/terms", terms_page)
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
    await init_db(DATABASE_URL)
    logging.info("Database connected")

    gen_name = "KieGenerator" if KIE_API_KEY else "StubGenerator"
    logging.info("Generator: %s", gen_name)

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
        await bot.set_webhook(f"{WEBHOOK_URL}{webhook_path}")
        SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path=webhook_path)
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
