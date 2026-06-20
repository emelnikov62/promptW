# PromptW — Telegram AI Generator Bot

Telegram-бот `@promptW_bot` с WebApp UI для генерации фото, видео, музыки и текста через AI (KIE.AI + OpenRouter). Оплата в рублях (ЮKassa/Platega), реферальная программа.

## Стек
Python 3 · aiogram 3.15 · aiohttp · PostgreSQL 16 (asyncpg) · vanilla JS/CSS WebApp.

## Локальный запуск
```bash
git clone <repo-url>
cd promptW            # каталог с этим README
python -m venv venv && source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env  # заполнить реальными значениями (см. ниже)
python main.py
```

## Секреты (.env) — НИКОГДА не коммитить
`.env` в `.gitignore`. Реальные значения передаются между разработчиками **вне git**
(личным сообщением / менеджером паролей), не через репозиторий. Шаблон — `.env.example`.
Минимум для старта: `BOT_TOKEN`, `DATABASE_URL`. Без `KIE_API_KEY` работает заглушка
(StubGenerator). Без `OPENROUTER_API_KEY` чат отключён.

## Совместная работа
- `main` — рабочая ветка. Изменения делаем в отдельных ветках и через Pull Request.
- Перед push: `python -m py_compile` по изменённым .py; для фронта поднимать `?v=N`
  в `webapp/templates/index.html` (иначе Telegram отдаёт старый кэш).
- Деплой на VPS — отдельным скриптом у владельца (`srv2.sh`, вне репозитория).

## Структура
`main.py` — точка входа · `bot/` — команды/конфиг/auth · `api/routes.py` — REST API ·
`db/` — asyncpg + SQL · `generators/` — KIE/Stub · `payments_gw.py` — ЮKassa/Platega ·
`webapp/` — SPA (index.html, app.js, i18n.js, style.css).
