# PromptW — Telegram AI Bot

Telegram-бот (@promptW_bot) с WebApp UI для AI-генерации фото/видео и текстового чата.

## Stack

- Python 3, aiogram 3.15.0, aiohttp web server
- PostgreSQL 16 + asyncpg (JSONB fields returned as strings — need json.loads on backend)
- KIE.AI API for image/video generation
- OpenRouter API for text chat (GPT, Gemini, Grok)
- Telegram WebApp SDK (runs in iframe/webview)
- Nginx + Let's Encrypt SSL on VPS

## Project structure

```
main.py              — entrypoint: starts bot + aiohttp server
api/routes.py        — all HTTP endpoints (generate, chat, payments, history, auth)
bot/handlers.py      — Telegram bot command handlers
bot/auth.py          — authentication middleware
bot/config.py        — bot configuration
db/database.py       — asyncpg connection pool
db/queries.py        — all SQL queries
generators/kie.py    — KIE.AI API client (image/video generation)
generators/base.py   — generator base class
payments_gw.py       — YooKassa payment gateway
pricing.py           — token pricing logic
webapp/templates/index.html  — single-page app HTML
webapp/static/js/app.js      — main frontend logic
webapp/static/js/i18n.js     — i18n (ru/en/es)
webapp/static/css/style.css  — all styles
webapp/static/tpl/           — template media (photos/videos)
```

## Deploy

- **Domain:** https://promptw.ru
- **VPS:** 45.147.177.237 (root)
- **Deploy path:** /opt/tg-image-ai-bot/
- **Service:** `systemctl restart promptw`
- **SSL:** Let's Encrypt via certbot (auto-renew)
- **Reverse proxy:** nginx → localhost:8081

Deploy (git-only, never rsync/scp):
```bash
git push origin main
ssh root@45.147.177.237 "/opt/tg-image-ai-bot/deploy.sh"
```

## Key conventions

- Static files use cache-busting: `?v=N` in index.html (bump on every change)
- `.env` is NOT synced — edit directly on VPS
- Telegram WebApp: `<a download>` doesn't work — use Bot API to send files to chat
- asyncpg returns JSONB as strings — always json.loads() in API routes
- Auth via Telegram WebApp initData validation
