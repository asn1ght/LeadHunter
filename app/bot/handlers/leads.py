"""Списки лидов: /new, /leads, /today, /find."""
from __future__ import annotations

import math
from datetime import datetime, time, timedelta
from html import escape

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.bot.common import send_card
from app.bot.handlers.start import USER_TEXT
from app.bot.keyboards import (
    BTN_LEADS,
    BTN_NEW,
    BTN_TODAY,
    CardCb,
    ListCb,
    NavCb,
    lead_button,
    leads_list,
    main_menu,
    open_buttons,
    skip_cancel,
    status_filter,
)
from app.bot.states import FindFlow
from app.config import get_settings
from app.crm import workplan
from app.crm.statuses import Status, label, parse_status
from app.db import repo
from app.db.models import utcnow
from app.db.session import SessionLocal
from app.utils.dates import fmt_dt

router = Router(name="leads")
PER_PAGE = 10


async def build_list(status: str, page: int) -> tuple[str, InlineKeyboardMarkup]:
    async with SessionLocal() as session:
        items, total = await repo.list_companies(session, status=status or None, offset=page * PER_PAGE, limit=PER_PAGE)
    title = label(status) if status else "Все"
    if not total:
        text = f"📋 Лиды · {title}\n\nПусто. Добавьте сайты через ➕ Добавить."
    else:
        text = f"📋 Лиды · {title} · {total}\nОценка · название · статус. Нажмите, чтобы открыть карточку."
    return text, leads_list(items, status, page, total, PER_PAGE)


@router.message(Command("new"), StateFilter("*"))
@router.message(F.text == BTN_NEW, StateFilter("*"))
async def cmd_new(message: Message, state: FSMContext) -> None:
    await state.clear()
    text, kb = await build_list(Status.NEW.value, 0)
    await message.answer(text.replace("📋 Лиды", "🔥 Новые"), reply_markup=kb)


@router.message(Command("leads"), StateFilter("*"))
@router.message(F.text == BTN_LEADS, StateFilter("*"))
async def cmd_leads(message: Message, state: FSMContext, command: CommandObject | None = None) -> None:
    await state.clear()
    status = ""
    if command and command.args:
        st = parse_status(command.args)
        if not st:
            await message.answer("Не понял статус. Пример: <code>/leads новый</code> или <code>/leads SENT</code>")
            return
        status = st.value
    text, kb = await build_list(status, 0)
    await message.answer(text, reply_markup=kb)


@router.callback_query(ListCb.filter(), StateFilter("*"))
async def list_nav(cb: CallbackQuery, callback_data: ListCb) -> None:
    if not cb.message:
        return
    try:
        if callback_data.filters:
            async with SessionLocal() as session:
                counts = await repo.count_by_status(session)
            await cb.message.edit_text("🔎 Фильтр по статусу:", reply_markup=status_filter(counts))
        else:
            text, kb = await build_list(callback_data.status, callback_data.page)
            await cb.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        pass
    await cb.answer()


@router.callback_query(CardCb.filter(F.action == "open"), StateFilter("*"))
async def open_card(cb: CallbackQuery, callback_data: CardCb, bot: Bot) -> None:
    if cb.message:
        await send_card(bot, cb.message.chat.id, callback_data.cid)
    await cb.answer()


# ---------------------------------------------------------------- /today

TODAY_PER_PAGE = 15
WD = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


class TodayCb(CallbackData, prefix="td"):
    page: int = 0


async def build_today(page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    """Напоминания (просроченные и на сегодня) + очередь обзвона с темпом до пятницы."""
    tz = get_settings().tz
    now = utcnow()
    now_local = now.astimezone(tz)
    day_end = datetime.combine(now_local.date() + timedelta(days=1), time(0), tz)
    async with SessionLocal() as session:
        reminders = await repo.open_reminders_before(session, day_end)
        queue = await workplan.call_queue(session)
        in_pool = len(await workplan.pool(session))

    overdue = [r for r in reminders if r.due_at < now]
    today = [r for r in reminders if r.due_at >= now]
    lines = [f"📞 <b>Сегодня, {WD[now_local.weekday()]} {now_local:%d.%m}</b>"]

    def rline(r) -> str:  # type: ignore[no-untyped-def]
        c = r.company
        kind = {"call2": "звонок №2", "remind": "напомнить"}.get(r.kind, "созвониться")
        extra = f" · {escape(r.text)}" if r.text else ""
        return f"• {fmt_dt(r.due_at, tz)} — {escape(c.name or c.domain)} · {kind}{extra}"

    if overdue:
        lines += ["", f"⚠️ Просрочено ({len(overdue)}):"] + [rline(r) for r in overdue]
    lines += ["", f"⏰ Напоминания на сегодня ({len(today)}):"] + ([rline(r) for r in today] or ["—"])

    pace = workplan.daily_pace(len(queue), now_local.date())
    head = f"📋 К обзвону: {len(queue)}"
    if pace:
        head += f" · ≈{pace} сегодня, чтобы успеть до пятницы"
    lines += ["", head]
    pages = max(1, math.ceil(len(queue) / TODAY_PER_PAGE))
    page = min(page, pages - 1)
    chunk = queue[page * TODAY_PER_PAGE:(page + 1) * TODAY_PER_PAGE]
    week0 = workplan.week_start_utc(now)
    for c in chunk:
        since = c.notified_at or c.created_at
        carried = " ↪️" if since < week0 else ""
        st = "📞" if c.status == Status.CALL1_NEEDED.value else "🆕"
        score = c.score if c.score is not None else "—"
        lines.append(f"• {score} · {escape(c.name or c.domain)} · {st} с {since.astimezone(tz):%d.%m}{carried}")
    if not queue:
        lines.append("— очередь пуста" + (f", в запасе {in_pool} → /new" if in_pool else ""))
    elif any((c.notified_at or c.created_at) < week0 for c in queue):
        lines.append("<i>↪️ — перенесено с прошлой недели</i>")

    kb = InlineKeyboardBuilder()
    seen: set[int] = set()
    for c in [r.company for r in overdue + today] + chunk:
        if c.id not in seen:
            seen.add(c.id)
            kb.row(lead_button(c))
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="◀️", callback_data=TodayCb(page=page - 1).pack()))
        nav.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data=TodayCb(page=page).pack()))
        if page + 1 < pages:
            nav.append(InlineKeyboardButton(text="▶️", callback_data=TodayCb(page=page + 1).pack()))
        kb.row(*nav)
    kb.row(InlineKeyboardButton(text="🏠 Меню", callback_data=NavCb(action="menu").pack()))
    return "\n".join(lines)[:4000], kb.as_markup()


@router.message(Command("today"), StateFilter("*"))
@router.message(F.text == BTN_TODAY, StateFilter("*"))
async def cmd_today(message: Message, state: FSMContext) -> None:
    await state.clear()
    text, kb = await build_today()
    await message.answer(text, reply_markup=kb)


@router.callback_query(NavCb.filter(F.action == "today"), StateFilter("*"))
async def nav_today(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    if cb.message:
        text, kb = await build_today()
        await cb.message.answer(text, reply_markup=kb)
    await cb.answer()


@router.callback_query(TodayCb.filter(), StateFilter("*"))
async def today_page(cb: CallbackQuery, callback_data: TodayCb) -> None:
    if cb.message:
        text, kb = await build_today(callback_data.page)
        try:
            await cb.message.edit_text(text, reply_markup=kb)
        except TelegramBadRequest:
            pass
    await cb.answer()


# ---------------------------------------------------------------- /find


async def do_find(message: Message, query: str) -> None:
    async with SessionLocal() as session:
        found = await repo.find_companies(session, query)
    if not found:
        await message.answer(
            f"🔍 По запросу «{escape(query)}» ничего не найдено.\n"
            "Ищу по названию, домену, телефону, ИНН, контакту, email, адресу и заметкам.",
            reply_markup=main_menu(),
        )
        return
    if len(found) == 1:
        await send_card(message.bot, message.chat.id, found[0].id)  # type: ignore[arg-type]
        return
    await message.answer(f"🔍 Найдено: {len(found)}, самые подходящие сверху", reply_markup=open_buttons(found))


@router.message(Command("find"), StateFilter("*"))
async def cmd_find(message: Message, command: CommandObject, state: FSMContext) -> None:
    await state.clear()
    if command.args:
        await do_find(message, command.args)
        return
    await state.set_state(FindFlow.waiting_query)
    await message.answer("🔍 Что ищем? Название, домен, телефон или ИНН.", reply_markup=skip_cancel(skip=False))


@router.message(FindFlow.waiting_query, USER_TEXT)
async def find_text(message: Message, state: FSMContext) -> None:
    await state.clear()
    await do_find(message, message.text or "")
