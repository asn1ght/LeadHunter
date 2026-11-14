"""Fallback: любое сообщение вне сценария → главное меню. Подключать последним."""
from __future__ import annotations

from aiogram import Router
from aiogram.filters import StateFilter
from aiogram.types import CallbackQuery, Message

from app.bot.keyboards import main_menu, skip_cancel

router = Router(name="fallback")


@router.message(StateFilter(None))
async def any_message(message: Message) -> None:
    await message.answer("Выберите действие в меню 👇", reply_markup=main_menu())


@router.message()
async def in_flow_message(message: Message) -> None:
    await message.answer("Жду текстовый ответ — или нажмите «Отмена».", reply_markup=skip_cancel(skip=False))


@router.callback_query()
async def stale_callback(cb: CallbackQuery) -> None:
    await cb.answer("Кнопка устарела — откройте карточку заново.", show_alert=False)
