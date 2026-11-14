"""Действия на карточке лида и FSM-сценарии: статусы, звонки, заметки, напоминания."""
from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Any

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot.cards import render_why
from app.bot.common import card_payload, refresh_card
from app.bot.handlers.start import USER_TEXT
from app.bot.keyboards import (
    CardCb,
    DateCb,
    FlowCb,
    RemCb,
    back_to_card,
    call_outcomes,
    date_choices,
    skip_cancel,
    status_menu,
)
from app.bot.states import CallNoteFlow, NoteFlow, ReminderFlow
from app.config import get_settings
from app.crm import service
from app.crm.statuses import CALL1_OUTCOMES, CALL2_OUTCOMES, Status, label
from app.db import repo
from app.db.models import utcnow
from app.db.session import SessionLocal
from app.utils.dates import fmt_dt, parse_human_datetime

router = Router(name="card_actions")

# режимы сценария напоминания: (вопрос о дате, спрашивать контакт, спрашивать комментарий)
REMINDER_MODES: dict[str, tuple[str, bool, bool]] = {
    "callback": ("⏰ Когда созвониться?", True, True),
    "remind": ("⏰ Когда напомнить?", False, True),
    "call2": ("📅 Когда звонок №2?", False, False),
    "call2resched": ("⏸ На когда перенести звонок №2?", False, False),
}

DATE_HINT = "✍️ Введите дату и время, например: <code>пт 14:30</code>, <code>20.11 11:00</code>, <code>завтра в 12</code>"


def _card_ref(cb: CallbackQuery) -> dict[str, int]:
    assert cb.message is not None
    return {"chat_id": cb.message.chat.id, "message_id": cb.message.message_id}


async def _edit_markup(cb: CallbackQuery, markup: Any) -> None:
    try:
        assert cb.message is not None
        await cb.message.edit_reply_markup(reply_markup=markup)
    except TelegramBadRequest:
        pass


# ---------------------------------------------------------------- простые действия


@router.callback_query(CardCb.filter(F.action == "back"), StateFilter("*"))
async def back(cb: CallbackQuery, callback_data: CardCb, bot: Bot) -> None:
    ref = _card_ref(cb)
    await refresh_card(bot, ref["chat_id"], ref["message_id"], callback_data.cid)
    await cb.answer()


@router.callback_query(CardCb.filter(F.action == "why"), StateFilter("*"))
async def why(cb: CallbackQuery, callback_data: CardCb) -> None:
    async with SessionLocal() as session:
        company = await repo.get_company(session, callback_data.cid)
        audit = await repo.last_audit(session, callback_data.cid)
    if not company or not cb.message:
        await cb.answer("Лид не найден")
        return
    await cb.message.edit_text(render_why(company, audit), reply_markup=back_to_card(company.id))
    await cb.answer()


@router.callback_query(CardCb.filter(F.action == "stmenu"), StateFilter("*"))
async def stmenu(cb: CallbackQuery, callback_data: CardCb) -> None:
    async with SessionLocal() as session:
        company = await repo.get_company(session, callback_data.cid)
    if company:
        await _edit_markup(cb, status_menu(company))
    await cb.answer()


async def _set_status(cb: CallbackQuery, bot: Bot, cid: int, to: Status, payload: dict | None = None) -> None:
    async with SessionLocal() as session:
        company = await repo.get_company(session, cid)
        if not company:
            await cb.answer("Лид не найден")
            return
        if company.status == to.value:
            await cb.answer(f"Уже {label(to)}")
            ref = _card_ref(cb)
            await refresh_card(bot, ref["chat_id"], ref["message_id"], cid)
            return
        await service.change_status(session, company, to, payload=payload)
        await session.commit()
    ref = _card_ref(cb)
    await refresh_card(bot, ref["chat_id"], ref["message_id"], cid)
    await cb.answer(label(to))


@router.callback_query(CardCb.filter(F.action.startswith("st_")), StateFilter("*"))
async def direct_status(cb: CallbackQuery, callback_data: CardCb, bot: Bot) -> None:
    await _set_status(cb, bot, callback_data.cid, Status(callback_data.action[3:]))


@router.callback_query(CardCb.filter(F.action == "cancel"), StateFilter("*"))
async def cancel_lead(cb: CallbackQuery, callback_data: CardCb, bot: Bot) -> None:
    await _set_status(cb, bot, callback_data.cid, Status.CANCELLED, {"reason": "manual"})


@router.callback_query(CardCb.filter(F.action == "blacklist"), StateFilter("*"))
async def blacklist(cb: CallbackQuery, callback_data: CardCb, bot: Bot) -> None:
    async with SessionLocal() as session:
        company = await repo.get_company(session, callback_data.cid)
        if not company:
            await cb.answer("Лид не найден")
            return
        await repo.add_blacklist(session, company.domain, reason="manual")
        if company.status != Status.CANCELLED.value:
            await service.change_status(session, company, Status.CANCELLED, payload={"reason": "blacklist"})
        await session.commit()
    ref = _card_ref(cb)
    await refresh_card(bot, ref["chat_id"], ref["message_id"], callback_data.cid)
    await cb.answer("⛔ Домен в чёрном списке")


@router.callback_query(CardCb.filter(F.action == "set"), StateFilter("*"))
async def set_from_menu(cb: CallbackQuery, callback_data: CardCb, bot: Bot, state: FSMContext) -> None:
    to = Status(callback_data.arg)
    if to == Status.CALLBACK:
        await start_reminder(cb, state, callback_data.cid, "callback")
    elif to == Status.CALL2_SCHEDULED:
        await start_reminder(cb, state, callback_data.cid, "call2")
    else:
        await _set_status(cb, bot, callback_data.cid, to, {"via": "menu"})


# ---------------------------------------------------------------- звонки


@router.callback_query(CardCb.filter(F.action.in_({"call1", "call2done"})), StateFilter("*"))
async def call_done(cb: CallbackQuery, callback_data: CardCb) -> None:
    async with SessionLocal() as session:
        company = await repo.get_company(session, callback_data.cid)
    if company:
        await _edit_markup(cb, call_outcomes(company, 1 if callback_data.action == "call1" else 2))
    await cb.answer("Чем закончился звонок?")


@router.callback_query(CardCb.filter(F.action.in_({"out1", "out2"})), StateFilter("*"))
async def call_outcome(cb: CallbackQuery, callback_data: CardCb, state: FSMContext) -> None:
    stage = int(callback_data.action[-1])
    outcomes = CALL1_OUTCOMES if stage == 1 else CALL2_OUTCOMES
    text = outcomes[callback_data.arg][0]
    await state.set_state(CallNoteFlow.waiting_text)
    await state.update_data(cid=callback_data.cid, stage=stage, outcome=callback_data.arg, card=_card_ref(cb))
    assert cb.message is not None
    await cb.message.answer(
        f"Исход звонка №{stage}: {text}\n📝 Заметка по звонку? Напишите текст или нажмите «Пропустить».",
        reply_markup=skip_cancel(),
    )
    await cb.answer()


async def _finish_call(bot: Bot, state: FSMContext, note: str | None) -> str:
    data = await state.get_data()
    await state.clear()
    async with SessionLocal() as session:
        company = await repo.get_company(session, data["cid"])
        if not company:
            return "Лид не найден."
        await service.log_call(session, company, data["stage"], data["outcome"], note)
        await session.commit()
        new_status = company.status
    card = data["card"]
    await refresh_card(bot, card["chat_id"], card["message_id"], data["cid"])
    return f"✅ Звонок №{data['stage']} записан. Статус: {label(new_status)}"


@router.message(CallNoteFlow.waiting_text, USER_TEXT)
async def call_note_text(message: Message, state: FSMContext, bot: Bot) -> None:
    await message.answer(await _finish_call(bot, state, (message.text or "").strip()))


@router.callback_query(CallNoteFlow.waiting_text, FlowCb.filter(F.action == "skip"))
async def call_note_skip(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    result = await _finish_call(bot, state, None)
    if cb.message:
        await cb.message.edit_text(result)
    await cb.answer()


# ---------------------------------------------------------------- заметка


@router.callback_query(CardCb.filter(F.action == "note"), StateFilter("*"))
async def note_start(cb: CallbackQuery, callback_data: CardCb, state: FSMContext) -> None:
    await state.set_state(NoteFlow.waiting_text)
    await state.update_data(cid=callback_data.cid, card=_card_ref(cb))
    assert cb.message is not None
    await cb.message.answer("📝 Напишите заметку:", reply_markup=skip_cancel(skip=False))
    await cb.answer()


@router.message(NoteFlow.waiting_text, USER_TEXT)
async def note_text(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    await state.clear()
    async with SessionLocal() as session:
        company = await repo.get_company(session, data["cid"])
        if not company:
            await message.answer("Лид не найден.")
            return
        await service.add_note(session, company, message.text or "")
        await session.commit()
    await refresh_card(bot, data["card"]["chat_id"], data["card"]["message_id"], data["cid"])
    await message.answer("✅ Заметка сохранена.")


# ---------------------------------------------------------------- напоминания (FSM)


@router.callback_query(CardCb.filter(F.action.in_({"later", "remind", "call2", "call2resched"})), StateFilter("*"))
async def reminder_entry(cb: CallbackQuery, callback_data: CardCb, state: FSMContext) -> None:
    mode = {"later": "callback"}.get(callback_data.action, callback_data.action)
    await start_reminder(cb, state, callback_data.cid, mode)


async def start_reminder(cb: CallbackQuery, state: FSMContext, cid: int, mode: str) -> None:
    question = REMINDER_MODES[mode][0]
    await state.set_state(ReminderFlow.choosing_date)
    await state.update_data(cid=cid, mode=mode, card=_card_ref(cb))
    now_local = utcnow().astimezone(get_settings().tz)
    assert cb.message is not None
    await cb.message.answer(question, reply_markup=date_choices(now_local))
    await cb.answer()


def quick_date(choice: str, now_local: datetime) -> datetime | None:
    """Быстрые варианты даты в часовом поясе владельца."""
    tz = now_local.tzinfo
    d = now_local.date()
    match choice:
        case "t15":
            return datetime.combine(d, time(15, 0), tz)
        case "2h":
            return (now_local + timedelta(hours=2)).replace(second=0, microsecond=0)
        case "tm10":
            return datetime.combine(d + timedelta(days=1), time(10, 0), tz)
        case "d3":
            return datetime.combine(d + timedelta(days=3), time(10, 0), tz)
        case "w1":
            return datetime.combine(d + timedelta(days=7), time(10, 0), tz)
    return None


@router.callback_query(ReminderFlow.choosing_date, DateCb.filter())
async def reminder_quick(cb: CallbackQuery, callback_data: DateCb, state: FSMContext, bot: Bot) -> None:
    assert cb.message is not None
    if callback_data.choice == "manual":
        await state.set_state(ReminderFlow.entering_date)
        await cb.message.edit_text(DATE_HINT, reply_markup=skip_cancel(skip=False))
        await cb.answer()
        return
    due = quick_date(callback_data.choice, utcnow().astimezone(get_settings().tz))
    if due is None:
        await cb.answer("?")
        return
    await state.update_data(due=due.isoformat())
    await cb.message.edit_text(f"🗓 {fmt_dt(due, get_settings().tz)}")
    await cb.answer()
    await next_reminder_step(cb.message, state, bot)


@router.message(ReminderFlow.entering_date, USER_TEXT)
async def reminder_date_text(message: Message, state: FSMContext, bot: Bot) -> None:
    tz = get_settings().tz
    now_local = utcnow().astimezone(tz)
    due = parse_human_datetime(message.text or "", now_local)
    if due is None:
        await message.answer("Не понял дату 🤔\n" + DATE_HINT, reply_markup=skip_cancel(skip=False))
        return
    if due <= now_local:
        await message.answer(f"{fmt_dt(due, tz)} — уже прошло. Укажите время в будущем.", reply_markup=skip_cancel(skip=False))
        return
    await state.update_data(due=due.isoformat())
    await message.answer(f"🗓 {fmt_dt(due, tz)}")
    await next_reminder_step(message, state, bot)


async def next_reminder_step(message: Message, state: FSMContext, bot: Bot) -> None:
    """Перейти к следующему вопросу сценария или завершить его."""
    data = await state.get_data()
    _, ask_contact, ask_comment = REMINDER_MODES[data["mode"]]
    if ask_contact and "contact" not in data:
        await state.set_state(ReminderFlow.entering_contact)
        await message.answer("👤 Контакт: имя и/или телефон?", reply_markup=skip_cancel())
        return
    if ask_comment and "comment" not in data:
        await state.set_state(ReminderFlow.entering_comment)
        await message.answer("💬 Комментарий?", reply_markup=skip_cancel())
        return
    await message.answer(await finish_reminder(state, bot))


@router.message(ReminderFlow.entering_contact, USER_TEXT)
async def reminder_contact(message: Message, state: FSMContext, bot: Bot) -> None:
    await state.update_data(contact=(message.text or "").strip()[:200])
    await next_reminder_step(message, state, bot)


@router.message(ReminderFlow.entering_comment, USER_TEXT)
async def reminder_comment(message: Message, state: FSMContext, bot: Bot) -> None:
    await state.update_data(comment=(message.text or "").strip()[:500])
    await next_reminder_step(message, state, bot)


@router.callback_query(
    StateFilter(ReminderFlow.entering_contact, ReminderFlow.entering_comment), FlowCb.filter(F.action == "skip")
)
async def reminder_skip(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    key = "contact" if await state.get_state() == ReminderFlow.entering_contact.state else "comment"
    await state.update_data(**{key: None})
    assert cb.message is not None
    await cb.message.edit_text("⏭ Пропущено")
    await cb.answer()
    await next_reminder_step(cb.message, state, bot)


async def finish_reminder(state: FSMContext, bot: Bot) -> str:
    data = await state.get_data()
    await state.clear()
    mode = data["mode"]
    due = datetime.fromisoformat(data["due"])
    set_status = {"callback": Status.CALLBACK, "call2": Status.CALL2_SCHEDULED}.get(mode)
    kind = {"call2resched": "call2"}.get(mode, mode)
    async with SessionLocal() as session:
        company = await repo.get_company(session, data["cid"])
        if not company:
            return "Лид не найден."
        if mode == "call2resched":
            await service.log_call(session, company, 2, "RESCHEDULED", data.get("comment"))
        await service.schedule(
            session,
            company,
            due,
            kind=kind,
            contact=data.get("contact"),
            comment=data.get("comment"),
            set_status=set_status,
        )
        await session.commit()
    card = data["card"]
    await refresh_card(bot, card["chat_id"], card["message_id"], data["cid"])
    return f"✅ Напомню {fmt_dt(due, get_settings().tz)}"


# ---------------------------------------------------------------- кнопки сработавшего напоминания


@router.callback_query(RemCb.filter(), StateFilter("*"))
async def reminder_buttons(cb: CallbackQuery, callback_data: RemCb, bot: Bot) -> None:
    tz = get_settings().tz
    async with SessionLocal() as session:
        r = await repo.get_reminder(session, callback_data.rid)
        if not r:
            await cb.answer("Напоминание не найдено")
            return
        company = await repo.get_company(session, r.company_id)
        if callback_data.action == "done":
            r.is_done = True
            if company and company.next_action_at == r.due_at:
                company.next_action_at = None
            msg = "✅ Сделано"
        else:
            now_local = utcnow().astimezone(tz)
            if callback_data.action == "h1":
                due = (now_local + timedelta(hours=1)).replace(second=0, microsecond=0)
            else:
                due = datetime.combine(now_local.date() + timedelta(days=1), time(10, 0), tz)
            r.due_at = due
            r.fired_at = None
            r.pre_notified_at = None
            r.is_done = False
            if company:
                company.next_action_at = due
            msg = f"⏰ Напомню {fmt_dt(due, tz)}"
        await session.commit()
        cid = r.company_id
    # перерисовать карточку без ряда напоминания, оставив пометку
    payload = await card_payload(cid, prefix=msg)
    if payload and cb.message:
        try:
            await cb.message.edit_text(payload[0], reply_markup=payload[1], disable_web_page_preview=True)
        except TelegramBadRequest:
            pass
    await cb.answer(msg)
