"""/add — ручное добавление сайтов: аудит и карточка независимо от порога."""
from __future__ import annotations

import asyncio

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from loguru import logger

from app.audit.runner import audit_company
from app.bot.common import send_card
from app.bot.handlers.start import USER_TEXT
from app.bot.keyboards import BTN_ADD, main_menu, skip_cancel
from app.bot.states import AddFlow
from app.db import repo
from app.db.models import utcnow
from app.db.session import SessionLocal
from app.discovery.filters import extract_urls, is_blacklisted, normalize_domain

router = Router(name="add")

MAX_URLS = 50
PROMPT = "➕ Пришлите ссылки на сайты (по одной в строке) или файл .txt / .csv."


@router.message(Command("add"), StateFilter("*"))
async def cmd_add(message: Message, command: CommandObject, state: FSMContext, bot: Bot) -> None:
    await state.clear()
    if command.args:
        await process_text(message, bot, command.args)
        return
    await state.set_state(AddFlow.waiting_urls)
    await message.answer(PROMPT, reply_markup=skip_cancel(skip=False))


@router.message(F.text == BTN_ADD, StateFilter("*"))
async def btn_add(message: Message, state: FSMContext) -> None:
    await state.set_state(AddFlow.waiting_urls)
    await message.answer(PROMPT, reply_markup=skip_cancel(skip=False))


@router.message(AddFlow.waiting_urls, USER_TEXT)
async def add_text(message: Message, state: FSMContext, bot: Bot) -> None:
    await state.clear()
    await process_text(message, bot, message.text or "")


@router.message(F.document, StateFilter(None, AddFlow.waiting_urls))
async def add_file(message: Message, state: FSMContext, bot: Bot) -> None:
    doc = message.document
    assert doc is not None
    name = (doc.file_name or "").lower()
    if not name.endswith((".txt", ".csv")):
        await message.answer("Нужен файл .txt или .csv со ссылками.", reply_markup=main_menu())
        return
    if (doc.file_size or 0) > 2 * 1024 * 1024:
        await message.answer("Файл слишком большой (макс. 2 МБ).")
        return
    await state.clear()
    buf = await bot.download(doc)
    raw = buf.read() if buf else b""
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1251", errors="replace")
    await process_text(message, bot, text)


async def process_text(message: Message, bot: Bot, text: str) -> None:
    """Распознать ссылки, создать компании, запустить аудит, прислать карточки."""
    urls = extract_urls(text)
    if not urls:
        await message.answer("Не нашёл ни одной ссылки. Пример: <code>/add gazsever.com</code>", reply_markup=main_menu())
        return
    if len(urls) > MAX_URLS:
        await message.answer(f"Беру первые {MAX_URLS} из {len(urls)}.")
        urls = urls[:MAX_URLS]

    to_audit: list[int] = []
    notes: list[str] = []
    async with SessionLocal() as session:
        niche = await repo.get_active_niche(session)
        db_black = await repo.blacklisted_domains(session)
        for raw_url in urls:
            domain = normalize_domain(raw_url)
            if not domain:
                continue
            if is_blacklisted(domain, db_black):
                notes.append(f"⛔ {domain} — в чёрном списке, пропускаю")
                continue
            existing = await repo.get_company_by_domain(session, domain)
            if existing:
                notes.append(f"ℹ️ {domain} — уже в базе")
                await send_card(bot, message.chat.id, existing.id)
                continue
            company = await repo.create_company(
                session,
                domain=domain,
                url=raw_url if raw_url.startswith(("http://", "https://")) else domain,
                niche_id=niche.id if niche else None,
                source="manual",
                status="NEW",
                notified_at=utcnow(),  # добавлен вручную — сразу в очередь обзвона
            )
            to_audit.append(company.id)
        await session.commit()

    if notes:
        await message.answer("\n".join(notes))
    if not to_audit:
        return
    await message.answer(f"⏳ Проверяю {len(to_audit)} сайт(ов)… Карточки придут по мере готовности.")

    async def one(cid: int) -> None:
        try:
            await audit_company(SessionLocal, cid)
            await send_card(bot, message.chat.id, cid)
        except Exception as exc:  # noqa: BLE001
            logger.exception("add: audit/send failed for {}: {}", cid, exc)
            await message.answer(f"⚠️ Не удалось проверить сайт (id {cid}): {type(exc).__name__}")

    await asyncio.gather(*(one(cid) for cid in to_audit))
