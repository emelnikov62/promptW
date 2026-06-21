# PromptW — Telegram AI Generator Bot

Telegram-бот `@promptW_bot` с WebApp UI для генерации фото, видео, музыки и текстового
чата через AI (KIE.AI + OpenRouter). Оплата в рублях (ЮKassa/Platega), реферальная
программа. **Это канонический проектный гайд — единственный источник правды** (живёт в
репозитории, общий с коллабораторами).

## Development skills (вызывать через Skill tool ДО реализации)

При разработке проактивно вызывай профильные скиллы по типу задачи — обязательная часть
рабочего процесса, не дожидайся явной просьбы:

- **brainstorming** — ВСЕГДА перед созданием/изменением фич, компонентов, логики или
  поведения. Сначала исследовать намерение и требования, потом код.
- **frontend-design** — для любой работы с UI/визуалом: новые экраны, рестайл,
  типографика, компоненты WebApp, дизайн-решения.
- **telegram-dev** — для бота и Mini App: Bot API, WebApp/initData, платежи, inline,
  webhook, авторизация, сенсоры.

Фронт — vanilla JS без фреймворка и бандлера; React/Next не используем, скиллы под
React не применяем. Тривиальные правки, деплой, справочные и разговорные ответы скиллов
не требуют.

## Stack

- **Python 3.12**, aiogram 3.15.0, aiohttp (web server + REST API)
- **PostgreSQL 16** + asyncpg (async connection pool)
- **KIE.AI API** — генерация image/video/audio
- **OpenRouter API** — текстовый чат (GPT, Gemini, Grok)
- **Telegram WebApp SDK** — встроенный UI внутри Telegram (webview)
- **Frontend** — vanilla JS, CSS, без фреймворков и бандлеров
- **nginx + Let's Encrypt SSL** на VPS → reverse-proxy на `localhost:8081`

## Project structure

```
main.py                  — точка входа: aiohttp app + aiogram bot + webhook
bot/
  config.py              — загрузка BOT_TOKEN из .env
  handlers.py            — Telegram команды (/start с реф-параметром, меню-клавиатура)
  auth.py                — валидация WebApp initData (HMAC) + fallback auth-токен для десктопа
api/
  routes.py              — REST API: user/generate/chat/payments/withdraw/history/references/auth
db/
  database.py            — asyncpg pool, авто-создание таблиц + идемпотентные миграции
  queries.py             — SQL: users, generations, transactions, payments, ref_earnings, withdrawals, references, chat
generators/
  base.py                — BaseGenerator ABC + GenerationResult dataclass
  kie.py                 — KieGenerator: createTask → poll → download. IMAGE/VIDEO/AUDIO_MODELS
  stub.py                — StubGenerator: заглушка для локалки (Pillow)
payments_gw.py           — ЮKassa (РФ) + Platega (СНГ): создание платежа, верификация, шэр inline
pricing.py               — расчёт стоимости в токенах (compute_cost, CHAT_COST)
webapp/
  templates/index.html   — SPA: все страницы (home, text/chat, create, history, profile, topup, partner, info, stats, rewards)
  static/css/style.css   — все стили (минифицированные, CSS-токены в :root)
  static/js/app.js        — логика UI: навигация, формы, VIDEO_MODELS, upload, i18n, API
  static/js/i18n.js      — переводы ru/en/es (data-i18n атрибуты)
  static/tpl/            — медиа шаблонов (фото/видео)
```

## Environment variables (.env) — НЕ в git, правится прямо на VPS

```
BOT_TOKEN          — Telegram bot token
WEBAPP_URL         — публичный URL WebApp (https://promptw.ru)
WEBHOOK_URL        — URL webhook (пусто = polling); бот ре-регистрирует webhook при старте
WEBAPP_HOST=0.0.0.0
WEBAPP_PORT=8081   — на проде 8081 (за nginx), не дефолтные 8080
DATABASE_URL       — postgresql://user:pass@host:5432/db
KIE_API_KEY        — ключ KIE.AI (без него StubGenerator)
OPENROUTER_API_KEY — ключ для чата (без него чат отключён)
ADMIN_IDS          — tg_id админов (вывод средств)
YOOKASSA_SHOP_ID / YOOKASSA_SECRET_KEY — платежи РФ
AUTH_ENFORCE=1     — проверка initData (в проде ВСЕГДА 1; =0 только DEV=1)
BILLING_ENFORCE    — 1 = списывать токены; 0 = считать без списания
MEDIA_DIR          — директория скачанных файлов
```

## Running locally

```bash
pip install -r requirements.txt
cp .env.example .env   # заполнить BOT_TOKEN, DATABASE_URL и остальное
python main.py
```
Без `KIE_API_KEY` — StubGenerator. Без рабочего PostgreSQL сервер не стартует.

## Deploy (VPS)

- **Домен:** https://promptw.ru · **VPS:** 45.147.177.237 (root) · код в `/opt/tg-image-ai-bot/`
- **Сервис:** `systemctl restart promptw` · **nginx** → `localhost:8081` · **SSL** Let's Encrypt (auto-renew)
- **GitHub:** `https://github.com/emelnikov62/promptW.git` — репозиторий = этот каталог (`bot-src/`), ветка `main`. `.env`/`media`/`venv` в `.gitignore`.
- **VPS = git-чекаут** `origin/main`. Деплой: `git push` → на машине владельца `bash srv2.sh gitdeploy` (pull + pip-если-изменился + restart).
- ⚠️ **Один метод деплоя:** не смешивать `gitdeploy` с прямым `rsync` — rsync создаёт незакоммиченные правки в git-каталоге VPS и `git pull --ff-only` начнёт падать. Договориться в команде на git-flow.
- **Совместная работа:** изменения — в ветках, влитие в `main` через PR. Большие правки фронта (style.css/index.html/app.js) согласовывать, чтобы не ловить конфликты.

## Key patterns

- **Cache-busting:** ассеты подключены с `?v=N` в `index.html` (`style.css?v=N`, `app.js?v=N`, `i18n.js?v=N`). Бампать версию КАЖДОГО изменённого файла, иначе Telegram отдаёт старый кэш. Статика/index.html служатся live — рестарт сервиса не нужен.
- **Dynamic video forms:** `VIDEO_MODELS` в `app.js` маппит каждую видео-модель на свои UI-настройки; `renderVideoSettings(model)` строит форму.
- **File upload:** event-delegation на upload-зонах; `uploadedFiles` по ID; `buildFormData()` собирает multipart.
- **API parsing:** `_parse_request()` в `routes.py` — JSON и multipart; файлы в MEDIA_DIR с uuid-именами; защита (ext-allowlist, лимиты размера/числа).
- **asyncpg + JSONB:** JSONB-поля возвращаются строками — делать `json.loads()` в API. `_serialize` маппит Decimal→float, UUID→str, date→ISO.
- **Auth:** Telegram initData (HMAC по BOT_TOKEN). Часть Telegram Desktop НЕ отдаёт initData → fallback HMAC-токен `?tgauth=` в URL (см. `bot/auth.py`), сервер принимает его заголовком `X-Auth-Token`.
- **Сохранить/Поделиться:** `<a download>` в Telegram webview не работает — «Сохранить» шлёт файл в чат (Bot API), «Поделиться» = `savePreparedInlineMessage` + `tg.shareMessage` (нативный шэринг в любой чат).
- **i18n:** `data-i18n` на элементах; `applyLang()` в `i18n.js`; ru/en/es; паритет ключей держать.
- **Generator flow:** `createTask` → poll `recordInfo` → download → MEDIA_DIR → `/media/<file>` URL.

## Code style

- Минифицированный CSS (одна строка на правило)
- JS без модулей — глобальный скоуп, `var`
- Без тестов/линтера/CI · коммиты на ru или en

## Design system (текущий — НА РЕДИЗАЙНЕ)

Сейчас (до редизайна «Studio»): тёмная база + неон.
- Палитра: `--bg:#08080F` · `--card:#14141F` · `--violet:#8B5CF6` · `--magenta:#EC4899` · `--cyan:#22D3EE`
- Градиент: `linear-gradient(135deg,#8B5CF6,#EC4899,#22D3EE)` — «редкая драгоценность», только на бренде/primary-CTA/геро/балансе
- **Шрифт: только Manrope** (Google Fonts, веса 300–800, tabular nums); иерархия весом, не размером
- Радиусы: токены `--rs:12 / --r:16` (карточки) / 999 (кнопки) / 20 (шторки)
- Кастомные line-иконы (Lucide-style), без эмодзи как иконок

**В работе:** редизайн на направление **«Studio»** — тёплый уголь + цветовое кодирование зон
(фото/видео/аудио/чат каждый со своим цветом), геро из реальных результатов, типографика с
display+mono. Не «AI-дефолт». Детали — в спеке `docs/specs/*-studio-design.md`.
