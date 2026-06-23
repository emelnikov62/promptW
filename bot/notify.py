"""Transactional Telegram notifications (Phase 1).

A small, FAIL-SAFE helper: sending a notification must NEVER break the flow that
triggered it (payment / generation / withdrawal). Every send is wrapped — on any
error (bot blocked, network, bad placeholder) we log and move on.

Texts live in ``bot/notif_text.json`` (ru/en/es); the WebApp toasts live in
i18n.js. See docs/specs/2026-06-23-notification-delivery-phase1-design.md.
"""
import os
import json
import asyncio
import logging
from typing import Optional

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

from bot.config import BOT_TOKEN
from bot.auth import make_auth_token
from db.queries import get_user

logger = logging.getLogger(__name__)

WEBAPP_URL = os.getenv("WEBAPP_URL", "")
_DEFAULT_LANG = "ru"

_TEXT = {}
try:
    with open(os.path.join(os.path.dirname(__file__), "notif_text.json"), encoding="utf-8") as _f:
        _TEXT = json.load(_f)
except Exception:
    logger.exception("notify: could not load notif_text.json — notifications disabled")

_bot: Optional[Bot] = None
_TASKS = set()   # strong refs so fire-and-forget sends aren't garbage-collected


def _get_bot() -> Bot:
    global _bot
    if _bot is None:
        _bot = Bot(token=BOT_TOKEN)
    return _bot


def _wa_url(page: str = "", token: str = "") -> str:
    """WebApp URL with optional in-app page (?p=) and fallback auth token (?tgauth=)."""
    params = []
    if page:
        params.append("p=" + page)
    if token:
        params.append("tgauth=" + token)
    if not params:
        return WEBAPP_URL
    sep = "&" if "?" in WEBAPP_URL else "?"
    return WEBAPP_URL + sep + "&".join(params)


def _string(lang: str, key: str) -> Optional[str]:
    return (_TEXT.get(lang) or _TEXT.get(_DEFAULT_LANG) or {}).get(key)


def norm_lang(code: Optional[str]) -> str:
    """Map a Telegram language_code (e.g. 'en-US') to a supported lang, default ru."""
    base = (code or "")[:2].lower()
    return base if base in _TEXT else _DEFAULT_LANG


def text(key: str, lang: Optional[str] = None, **params) -> Optional[str]:
    """Resolve a notification string (for callers that send the message themselves,
    e.g. the /start welcome). Returns None if missing."""
    lang = lang if (lang in _TEXT) else _DEFAULT_LANG
    s = _string(lang, key)
    if s and params:
        try:
            s = s.format(**params)
        except (KeyError, IndexError, ValueError):
            pass
    return s


async def notify(tg_id: int, key: str, *, btn_key: Optional[str] = None,
                 page: str = "", lang: Optional[str] = None, **params) -> bool:
    """Send one TG notification. Returns True on success, False on any failure
    (never raises). `params` fill {placeholders} in the text."""
    if not tg_id or not _TEXT or not BOT_TOKEN:
        return False
    try:
        if lang is None:
            try:
                user = await get_user(tg_id)
                lang = (user or {}).get("lang") or _DEFAULT_LANG
            except Exception:
                lang = _DEFAULT_LANG
        if lang not in _TEXT:
            lang = _DEFAULT_LANG
        text = _string(lang, key)
        if not text:
            logger.warning("notify: missing text for key=%s lang=%s", key, lang)
            return False
        if params:
            try:
                text = text.format(**params)
            except (KeyError, IndexError, ValueError):
                logger.warning("notify: bad placeholders for key=%s params=%s", key, list(params))
        markup = None
        if btn_key:
            label = _string(lang, btn_key)
            if label and WEBAPP_URL:
                token = make_auth_token(tg_id, BOT_TOKEN)
                markup = InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text=label,
                                         web_app=WebAppInfo(url=_wa_url(page, token)))
                ]])
        await _get_bot().send_message(tg_id, text, reply_markup=markup)
        return True
    except Exception:
        # bot blocked by user, network hiccup, etc. — never break the caller.
        logger.info("notify: send failed (key=%s tg_id=%s)", key, tg_id, exc_info=False)
        return False


def notify_bg(tg_id: int, key: str, **kwargs) -> None:
    """Fire-and-forget notify(): schedule it without blocking the caller."""
    try:
        task = asyncio.ensure_future(notify(tg_id, key, **kwargs))
        _TASKS.add(task)
        task.add_done_callback(_TASKS.discard)
    except RuntimeError:
        # no running loop (shouldn't happen in the aiohttp app) — skip silently
        pass
