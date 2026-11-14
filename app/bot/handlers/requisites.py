"""🏛 Реквизиты: сведения о компании из Чекко по ИНН — просмотр, обновление, ручной ввод ИНН."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.audit.extract import inn_is_valid
from app.bot.cards import render_requisites
from app.bot.handlers.start import USER_TEXT
from app.bot.keyboards import CardCb, requisites_keyboard, skip_cancel
from app.bot.states import InnFlow
from app.db import repo
from app.db.session import SessionLocal
from app.integrations.checko import CheckoError, enrich_company

router = Router(name="requisites")


async def _show(cb: CallbackQuery, cid: int) -> None:
    async with SessionLocal() as session:
        company = await repo.get_company(session, cid)
    if not company or not cb.message:
        await cb.answer("Лид не найден")
        return
    try:
        await cb.message.edit_text(render_requisites(company), reply_markup=requisites_keyboard(cid, bool(company.inn)),
                                   disable_web_page_preview=True)
    except TelegramBadRequest as exc:
        if "not modified" not in str(exc):
            raise


async def _load(cid: int, force: bool) -> str | None:
    """Подтянуть данные Чекко; вернуть текст ошибки или None."""
    try:
        await enrich_company(SessionLocal, cid, force=force)
    except CheckoError as exc:
        return f"⛔ {exc}"
    return None


@router.callback_query(CardCb.filter(F.action == "req"), StateFilter("*"))
async def requisites(cb: CallbackQuery, callback_data: CardCb, state: FSMContext) -> None:
    async with SessionLocal() as session:
        company = await repo.get_company(session, callback_data.cid)
    if company and not company.inn:
        await _ask_inn(cb, callback_data.cid, state, "🏛 На сайте ИНН не найден.\n")
        return
    if company and (not company.checko or company.checko.get("inn") != company.inn):
        await cb.answer("🔎 Запрашиваю Чекко…")
        error = await _load(callback_data.cid, force=False)
        if error and cb.message:
            await cb.message.answer(error)
    else:
        await cb.answer()
    await _show(cb, callback_data.cid)


@router.callback_query(CardCb.filter(F.action == "reqrf"), StateFilter("*"))
async def refresh(cb: CallbackQuery, callback_data: CardCb) -> None:
    await cb.answer("🔄 Обновляю…")
    error = await _load(callback_data.cid, force=True)
    if error and cb.message:
        await cb.message.answer(error)
    await _show(cb, callback_data.cid)


async def _ask_inn(cb: CallbackQuery, cid: int, state: FSMContext, prefix: str = "") -> None:
    assert cb.message is not None
    await state.set_state(InnFlow.waiting_inn)
    await state.update_data(cid=cid)
    await cb.message.answer(f"{prefix}✏️ Пришлите ИНН компании (10 цифр — ООО, 12 — ИП), проверю через Чекко.",
                            reply_markup=skip_cancel(skip=False))
    await cb.answer()


@router.callback_query(CardCb.filter(F.action == "reqinn"), StateFilter("*"))
async def enter_inn(cb: CallbackQuery, callback_data: CardCb, state: FSMContext) -> None:
    await _ask_inn(cb, callback_data.cid, state)


@router.message(InnFlow.waiting_inn, USER_TEXT)
async def inn_text(message: Message, state: FSMContext) -> None:
    inn = "".join(ch for ch in (message.text or "") if ch.isdigit())
    if not inn_is_valid(inn):
        await message.answer("🤔 Не похоже на ИНН: нужно 10 или 12 цифр с верной контрольной суммой. Ещё раз:",
                             reply_markup=skip_cancel(skip=False))
        return
    cid = (await state.get_data())["cid"]
    await state.clear()
    async with SessionLocal() as session:
        company = await repo.get_company(session, cid)
        if not company:
            await message.answer("Лид не найден.")
            return
        company.inn = inn
        await session.commit()
    error = await _load(cid, force=True)
    async with SessionLocal() as session:
        company = await repo.get_company(session, cid)
    assert company is not None
    if error:
        await message.answer(error)
    await message.answer(render_requisites(company), reply_markup=requisites_keyboard(cid, True),
                         disable_web_page_preview=True)

