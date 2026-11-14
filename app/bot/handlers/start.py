"""/start, /help, /cancel, заглушки разделов следующих этапов, общая отмена."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot.keyboards import BTN_SETTINGS, MENU_BUTTONS, FlowCb, NavCb, main_menu
from app.db import repo
from app.db.session import SessionLocal

router = Router(name="start")

# Текст пользователя внутри FSM: не кнопка меню и не команда
USER_TEXT = F.text & ~F.text.in_(MENU_BUTTONS) & ~F.text.startswith("/")

HELP = """<b>LeadHunter</b> — поиск клиентов и мини-CRM.

/new — новые лиды по оценке
/today — что сделать сегодня
/leads [статус] — все лиды с фильтром
/find &lt;текст&gt; — поиск по названию, домену, телефону, ИНН
/add &lt;url …&gt; — добавить сайты вручную (или .txt/.csv файлом)
/report [дата] — отчёт за день (например, /report 13.11)
/week — отчёт за неделю, /month — за месяц
/run — запустить поиск компаний сейчас
/queries — поисковые запросы активной ниши
/niche — переключить или создать нишу
/usage — расход Search API, DeepSeek и Чекко
/cancel — отменить текущий ввод
/help — эта справка

Скоро: /settings (изменение), /export."""


@router.message(CommandStart(), StateFilter("*"))
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "👋 Привет! Я ищу компании, которым нужен новый сайт, и веду по ним CRM.\n"
        "Начните с ➕ Добавить — пришлите сайт, я его проверю.",
        reply_markup=main_menu(),
    )


@router.message(Command("help"), StateFilter("*"))
async def cmd_help(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(HELP, reply_markup=main_menu())


@router.message(Command("cancel"), StateFilter("*"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("✖️ Отменено.", reply_markup=main_menu())


@router.callback_query(FlowCb.filter(F.action == "cancel"), StateFilter("*"))
async def flow_cancel(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    if cb.message:
        await cb.message.edit_text("✖️ Отменено.")
    await cb.answer()


@router.callback_query(NavCb.filter(F.action == "menu"), StateFilter("*"))
async def nav_menu(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    if cb.message:
        await cb.message.answer("🏠 Главное меню", reply_markup=main_menu())
    await cb.answer()


@router.message(F.text == BTN_SETTINGS, StateFilter("*"))
@router.message(Command("settings"), StateFilter("*"))
async def settings_view(message: Message, state: FSMContext) -> None:
    await state.clear()
    keys = ["SCORE_THRESHOLD", "LEADS_WEEKLY_LIMIT", "AUDIT_CONCURRENCY", "ACTIVE_NICHE", "TEST_STAGE_LABEL"]
    async with SessionLocal() as session:
        values = {k: await repo.get_runtime_setting(session, k) for k in keys}
    lines = ["⚙️ <b>Настройки</b> (изменение из бота — на следующих этапах)", ""]
    lines += [f"• {k} = <code>{v}</code>" for k, v in values.items()]
    await message.answer("\n".join(lines), reply_markup=main_menu())
