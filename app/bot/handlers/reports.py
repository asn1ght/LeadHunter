"""/report, /week, /month и навигация по отчётам."""
from __future__ import annotations

from datetime import datetime

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot.handlers.start import USER_TEXT
from app.bot.keyboards import BTN_REPORT, RepCb, report_day_choices, report_keyboard
from app.bot.states import ReportFlow
from app.reports.builder import Period, parse_report_date
from app.reports.service import report_text, today_local

router = Router(name="reports")

DATE_HELP = "Пример: <code>13.11</code>, <code>2026-11-13</code>, <code>вчера</code>"


async def _send(message: Message, period: Period) -> None:
    await message.answer(await report_text(period), reply_markup=report_keyboard(period, today_local()))


@router.message(Command("report", "week", "month"), StateFilter("*"))
async def cmd_report(message: Message, command: CommandObject, state: FSMContext) -> None:
    await state.clear()
    kind = {"report": "day", "week": "week", "month": "month"}[command.command.lower()]
    today = today_local()
    d = today
    if command.args:
        parsed = parse_report_date(command.args, today)
        if not parsed:
            await message.answer(f"Не понял дату. {DATE_HELP}")
            return
        d = parsed
    await _send(message, Period.of(kind, d))


@router.message(F.text == BTN_REPORT, StateFilter("*"))
async def btn_report(message: Message, state: FSMContext) -> None:
    await state.clear()
    await _send(message, Period.day(today_local()))


@router.callback_query(RepCb.filter(F.kind == "pick"), StateFilter("*"))
async def pick_day(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ReportFlow.waiting_date)
    if cb.message:
        await cb.message.answer(f"📅 За какой день отчёт? Напишите дату или выберите. {DATE_HELP}",
                                reply_markup=report_day_choices(today_local()))
    await cb.answer()


@router.message(ReportFlow.waiting_date, USER_TEXT)
async def pick_day_text(message: Message, state: FSMContext) -> None:
    d = parse_report_date(message.text or "", today_local())
    if not d:
        await message.answer(f"Не понял дату. {DATE_HELP}", reply_markup=report_day_choices(today_local()))
        return
    await state.clear()
    await _send(message, Period.day(d))


@router.callback_query(RepCb.filter(F.kind.in_({"day", "week", "month"})), StateFilter("*"))
async def report_nav(cb: CallbackQuery, callback_data: RepCb, state: FSMContext) -> None:
    await state.clear()
    period = Period.of(callback_data.kind, datetime.strptime(callback_data.d, "%Y%m%d").date())
    text = await report_text(period)
    kb = report_keyboard(period, today_local())
    if cb.message:
        try:
            await cb.message.edit_text(text, reply_markup=kb)
        except TelegramBadRequest:
            await cb.message.answer(text, reply_markup=kb)
    await cb.answer()
