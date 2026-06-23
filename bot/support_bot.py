import logging
from typing import Optional, Dict

from aiogram import Router, F, Bot
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo,
)

from db.queries import (
    get_support_ticket_by_id, assign_support_ticket,
    add_support_message, close_support_ticket, get_user,
    is_support_agent, get_support_agent_ids,
)

logger = logging.getLogger(__name__)
router = Router()

_main_bot: Optional[Bot] = None
_webapp_url: str = ""

_agent_active: Dict[int, int] = {}


def setup(main_bot: Bot, webapp_url: str):
    global _main_bot, _webapp_url
    _main_bot = main_bot
    _webapp_url = webapp_url


@router.message(CommandStart())
async def cmd_start(message: Message):
    uid = message.from_user.id
    if not await is_support_agent(uid):
        await message.answer("Нет доступа.")
        return
    await message.answer(
        "Бот поддержки PromptW.\n"
        "Тикеты от пользователей придут сюда автоматически.\n"
        "Нажмите «Ответить» чтобы взять тикет.\n\n"
        "/close — закрыть текущий тикет"
    )


async def _close_and_notify(ticket_id: int):
    ticket = await get_support_ticket_by_id(ticket_id)
    ok = await close_support_ticket(ticket_id)
    if ok and ticket and _main_bot:
        try:
            sep = "&" if "?" in _webapp_url else "?"
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(
                    text="Открыть поддержку",
                    web_app=WebAppInfo(url=_webapp_url + sep + "p=support"),
                )
            ]])
            await _main_bot.send_message(
                ticket["user_tg_id"],
                "Ваше обращение закрыто.\n"
                "Рады были помочь! Если появятся вопросы — пишите, мы всегда на связи.",
                reply_markup=kb,
            )
        except Exception:
            logger.exception("failed to notify user %s about ticket close", ticket.get("user_tg_id"))
    return ok


@router.message(Command("close"))
async def cmd_close(message: Message):
    uid = message.from_user.id
    if not await is_support_agent(uid):
        return
    ticket_id = _agent_active.get(uid)
    if not ticket_id:
        await message.answer("Нет активного тикета.")
        return
    ok = await _close_and_notify(ticket_id)
    del _agent_active[uid]
    await message.answer(f"Тикет #{ticket_id} закрыт ✓" if ok else "Тикет уже был закрыт.")


@router.callback_query(F.data.startswith("sup:take:"))
async def cb_take(callback: CallbackQuery):
    uid = callback.from_user.id
    if not await is_support_agent(uid):
        await callback.answer("Нет доступа", show_alert=True)
        return
    ticket_id = int(callback.data.split(":")[2])
    name = callback.from_user.full_name or str(uid)
    result = await assign_support_ticket(ticket_id, uid, name)
    if result is None:
        ticket = await get_support_ticket_by_id(ticket_id)
        who = ticket.get("agent_name", "другой агент") if ticket else "другой агент"
        await callback.answer(f"Уже работает: {who}", show_alert=True)
        return
    _agent_active[uid] = ticket_id
    await callback.answer("Тикет назначен вам. Пишите ответ текстом.")
    try:
        await callback.message.edit_reply_markup(
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text=f"Взял: {name}", callback_data="noop"),
                InlineKeyboardButton(text="Закрыть", callback_data=f"sup:close:{ticket_id}"),
            ]])
        )
    except Exception:
        pass


@router.callback_query(F.data.startswith("sup:close:"))
async def cb_close(callback: CallbackQuery):
    uid = callback.from_user.id
    if not await is_support_agent(uid):
        await callback.answer("Нет доступа", show_alert=True)
        return
    ticket_id = int(callback.data.split(":")[2])
    ok = await _close_and_notify(ticket_id)
    if _agent_active.get(uid) == ticket_id:
        del _agent_active[uid]
    await callback.answer("Тикет закрыт" if ok else "Уже закрыт")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.edit_text(
            callback.message.text + "\n\n✅ Закрыт: " + (callback.from_user.full_name or str(uid))
        )
    except Exception:
        pass


@router.callback_query(F.data == "noop")
async def cb_noop(callback: CallbackQuery):
    await callback.answer()


@router.message(F.text)
async def on_agent_reply(message: Message):
    uid = message.from_user.id
    if not await is_support_agent(uid):
        return
    ticket_id = _agent_active.get(uid)
    if not ticket_id:
        await message.answer(
            "Нет активного тикета. Нажмите «Ответить» на тикете выше."
        )
        return
    ticket = await get_support_ticket_by_id(ticket_id)
    if not ticket or ticket["status"] == "closed":
        del _agent_active[uid]
        await message.answer("Тикет уже закрыт.")
        return
    text = message.text.strip()
    if not text:
        return
    await add_support_message(ticket_id, "agent", text)
    await message.answer("Ответ отправлен ✓")
    await _notify_user(ticket["user_tg_id"])


async def _notify_user(user_tg_id: int):
    if not _main_bot or not _webapp_url:
        return
    try:
        sep = "&" if "?" in _webapp_url else "?"
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text="Открыть чат поддержки",
                web_app=WebAppInfo(url=_webapp_url + sep + "p=support"),
            )
        ]])
        await _main_bot.send_message(
            user_tg_id,
            "Вам ответила поддержка. Откройте чат, чтобы прочитать.",
            reply_markup=kb,
        )
    except Exception:
        logger.exception("failed to notify user %s", user_tg_id)


async def notify_agents(ticket_id: int, user_tg_id: int, text: str,
                        support_bot: Bot, *, image_url: str = None):
    from aiogram.types import URLInputFile
    ticket = await get_support_ticket_by_id(ticket_id)
    if not ticket:
        return
    user = await get_user(user_tg_id)
    uname = ""
    if user:
        uname = user.get("username") or ""
        if uname:
            uname = f"@{uname}"
        fname = user.get("first_name") or ""
        if fname:
            uname = f"{fname} ({uname})" if uname else fname

    header = (
        f"Тикет #{ticket_id}\n"
        f"От: {uname or user_tg_id} (ID {user_tg_id})\n"
        f"───\n"
        f"{text[:1500]}"
    )

    async def _send_to(chat_id: int, kb):
        if image_url:
            try:
                await support_bot.send_photo(
                    chat_id, URLInputFile(image_url),
                    caption=header[:1024], reply_markup=kb)
                return
            except Exception:
                logger.warning("photo send failed, falling back to text")
        await support_bot.send_message(chat_id, header, reply_markup=kb)

    if ticket["status"] == "assigned" and ticket.get("agent_tg_id"):
        close_kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Закрыть тикет", callback_data=f"sup:close:{ticket_id}"),
        ]])
        try:
            await _send_to(ticket["agent_tg_id"], close_kb)
        except Exception:
            logger.exception("notify assigned agent %s", ticket["agent_tg_id"])
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Ответить", callback_data=f"sup:take:{ticket_id}"),
        InlineKeyboardButton(text="Закрыть", callback_data=f"sup:close:{ticket_id}"),
    ]])
    agent_ids = await get_support_agent_ids()
    for agent_id in agent_ids:
        try:
            await _send_to(agent_id, kb)
        except Exception:
            logger.exception("notify agent %s", agent_id)
