import os
from typing import Optional

from aiogram import Router, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message, FSInputFile, WebAppInfo,
    ReplyKeyboardMarkup, KeyboardButton, MenuButtonWebApp,
)

from generators.base import BaseGenerator
from db.queries import upsert_user, create_referral, get_user
from bot.config import BOT_TOKEN
from bot.auth import make_auth_token
from bot import notify as notif

router = Router()
generator: Optional[BaseGenerator] = None

WEBAPP_URL = os.getenv("WEBAPP_URL", "")
WELCOME_BONUS = int(os.getenv("WELCOME_BONUS", "60"))
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}


def setup(gen: BaseGenerator):
    global generator
    generator = gen


def _wa_url(page: str = "", token: str = "") -> str:
    """WebApp URL with optional in-app page (?p=) and fallback auth token (?tgauth=,
    used by clients that don't expose initData, e.g. some Telegram Desktop builds)."""
    params = []
    if page:
        params.append("p=" + page)
    if token:
        params.append("tgauth=" + token)
    if not params:
        return WEBAPP_URL
    sep = "&" if "?" in WEBAPP_URL else "?"
    return WEBAPP_URL + sep + "&".join(params)


def main_menu_kb(token: str = "") -> ReplyKeyboardMarkup:
    """Persistent reply keyboard of WebApp shortcuts (like competitor menus)."""
    def wa(text: str, page: str) -> KeyboardButton:
        return KeyboardButton(text=text, web_app=WebAppInfo(url=_wa_url(page, token)))
    return ReplyKeyboardMarkup(
        keyboard=[
            [wa("🚀 Открыть PromptW", "")],
            [wa("🖼 Фото", "image"), wa("🎬 Видео", "video")],
            [wa("🎵 Аудио", "audio"), wa("💬 Текст", "text")],
            [wa("💎 Пополнить баланс", "topup"), wa("📜 История", "history")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


@router.message(CommandStart())
async def cmd_start(message: Message):
    user = message.from_user
    referrer_id = None
    args = message.text.split(maxsplit=1)
    if len(args) > 1:
        # WebApp links use "?start=r<id>"; also accept a bare numeric id.
        raw = args[1].strip()
        if raw[:1].lower() == "r":
            raw = raw[1:]
        if raw.isdigit():
            referrer_id = int(raw)
            if referrer_id == user.id:
                referrer_id = None

    # Only honour a referrer that actually exists (a real prior bot user) —
    # blocks crediting commissions to made-up ids.
    if referrer_id and not await get_user(referrer_id):
        referrer_id = None

    await upsert_user(
        tg_id=user.id,
        username=user.username,
        first_name=user.first_name,
        last_name=user.last_name,
        referrer_id=referrer_id,
        welcome_bonus=WELCOME_BONUS,
    )

    if referrer_id:
        await create_referral(referrer_id, user.id)
        # Tell the referrer someone joined (fail-safe, fire-and-forget).
        notif.notify_bg(referrer_id, "refNewTg", btn_key="partnerBtn", page="partner")

    if WEBAPP_URL:
        # Per-user fallback auth token, embedded in every WebApp entry point so the
        # app works even where Telegram doesn't expose initData (Desktop).
        token = make_auth_token(user.id, BOT_TOKEN)
        lang = notif.norm_lang(user.language_code)
        welcome = notif.text("welcomeTg", lang, bonus=WELCOME_BONUS) or (
            "Привет! 👋\n\nЭто PromptW — генерация фото, видео, музыки и текста через AI.\n"
            "Выбери, что создать, в меню ниже 👇")
        await message.answer(
            welcome,
            reply_markup=main_menu_kb(token),
        )
        # Personalised blue menu button (carries the token; overrides the global one).
        try:
            await message.bot.set_chat_menu_button(
                chat_id=user.id,
                menu_button=MenuButtonWebApp(text="🚀 PromptW",
                                             web_app=WebAppInfo(url=_wa_url("", token))),
            )
        except Exception:
            pass
    else:
        await message.answer(
            "Привет! Отправь мне текстовый промпт, и я сгенерирую изображение.\n"
            "Команды:\n"
            "/image <prompt> — сгенерировать картинку\n"
            "/video <prompt> — сгенерировать видео"
        )


@router.message(Command("image"))
async def cmd_image(message: Message):
    prompt = message.text.removeprefix("/image").strip()
    if not prompt:
        await message.answer("Укажи промпт после /image")
        return
    await message.answer("⏳ Генерирую...")
    try:
        result = await generator.generate_image(prompt)
        await message.answer_photo(FSInputFile(result.file_path), caption=result.prompt)
    except Exception as e:
        await message.answer(f"Ошибка генерации: {e}")


@router.message(Command("video"))
async def cmd_video(message: Message):
    prompt = message.text.removeprefix("/video").strip()
    if not prompt:
        await message.answer("Укажи промпт после /video")
        return
    await message.answer("⏳ Генерирую видео...")
    try:
        result = await generator.generate_video(prompt)
        await message.answer_video(FSInputFile(result.file_path), caption=result.prompt)
    except NotImplementedError:
        await message.answer("Генерация видео пока не подключена.")
    except Exception as e:
        await message.answer(f"Ошибка генерации: {e}")


@router.message(Command("audio"))
async def cmd_audio(message: Message):
    prompt = message.text.removeprefix("/audio").strip()
    if not prompt:
        await message.answer("Укажи промпт после /audio")
        return
    await message.answer("🎵 Генерирую аудио...")
    try:
        result = await generator.generate_audio(prompt)
        await message.answer_audio(FSInputFile(result.file_path), caption=result.prompt)
    except NotImplementedError:
        await message.answer("Генерация аудио пока не подключена.")
    except Exception as e:
        await message.answer(f"Ошибка генерации: {e}")


@router.message(Command("admin"))
async def cmd_admin(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    if not WEBAPP_URL:
        await message.answer("WEBAPP_URL не настроен")
        return
    token = make_auth_token(message.from_user.id, BOT_TOKEN)
    admin_url = WEBAPP_URL.rstrip("/") + "/admin?tgauth=" + token
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Открыть админку", web_app=WebAppInfo(url=admin_url))]
    ])
    await message.answer("Админ-панель PromptW:", reply_markup=kb)
