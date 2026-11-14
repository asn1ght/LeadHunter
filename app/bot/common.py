"""Общие помощники для хендлеров: отправка и обновление карточек."""
from __future__ import annotations

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from loguru import logger

from app.bot.cards import render_card
from app.bot.keyboards import card_keyboard
from app.db import repo
from app.db.session import SessionLocal


async def card_payload(
    company_id: int,
    prefix: str = "",
    extra_rows: list[list[InlineKeyboardButton]] | None = None,
) -> tuple[str, InlineKeyboardMarkup] | None:
    """Текст и клавиатура карточки по id компании."""
    async with SessionLocal() as session:
        company = await repo.get_company(session, company_id)
        if not company:
            return None
        audit = await repo.last_audit(session, company_id)
        threshold = await repo.get_runtime_setting(session, "SCORE_THRESHOLD")
        return render_card(company, audit, threshold, prefix), card_keyboard(company, extra_rows)


async def send_card(
    bot: Bot,
    chat_id: int,
    company_id: int,
    prefix: str = "",
    extra_rows: list[list[InlineKeyboardButton]] | None = None,
) -> None:
    payload = await card_payload(company_id, prefix, extra_rows)
    if not payload:
        await bot.send_message(chat_id, "Лид не найден.")
        return
    text, kb = payload
    await bot.send_message(chat_id, text, reply_markup=kb, disable_web_page_preview=True)


async def refresh_card(bot: Bot, chat_id: int, message_id: int, company_id: int) -> None:
    """Перерисовать карточку в том же сообщении (после смены статуса и т.п.)."""
    payload = await card_payload(company_id)
    if not payload:
        return
    text, kb = payload
    try:
        await bot.edit_message_text(
            text, chat_id=chat_id, message_id=message_id, reply_markup=kb, disable_web_page_preview=True
        )
    except TelegramBadRequest as exc:
        if "not modified" not in str(exc):
            logger.warning("refresh_card failed: {}", exc)
