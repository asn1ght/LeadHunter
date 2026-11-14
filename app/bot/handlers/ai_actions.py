"""🧠 Глубокий анализ и ✍️ Черновик письма через DeepSeek (раздел 10.2)."""
from __future__ import annotations

from html import escape

from aiogram import Bot, F, Router
from aiogram.filters import StateFilter
from aiogram.types import CallbackQuery

from app.ai.client import DeepSeekClient, LLMUnavailable
from app.ai.prompts import LETTER_SYSTEM, letter_user
from app.audit.quality_llm import analyze_company
from app.audit.runner import audit_company
from app.bot.cards import main_points
from app.bot.common import refresh_card
from app.bot.keyboards import CardCb, DraftCb, draft_keyboard, letter_channels
from app.config import get_settings
from app.crm import service
from app.crm.statuses import Status, label
from app.db import repo
from app.db.models import Event
from app.db.session import SessionLocal

router = Router(name="ai_actions")
LETTER_MAX_CHARS = 600


def ai_client() -> DeepSeekClient:
    return DeepSeekClient(SessionLocal)


@router.callback_query(CardCb.filter(F.action == "deep"), StateFilter("*"))
async def deep_analysis(cb: CallbackQuery, callback_data: CardCb, bot: Bot) -> None:
    assert cb.message is not None
    cid = callback_data.cid
    await cb.answer("🧠 Анализирую…")
    status = await cb.message.answer("🧠 Анализирую сайт через DeepSeek, это 10–40 секунд…")
    async with SessionLocal() as session:
        audit = await repo.last_audit(session, cid)
    if not audit or not audit.raw_text_excerpt:
        await audit_company(SessionLocal, cid)
    try:
        result = await analyze_company(SessionLocal, ai_client(), cid)
    except LLMUnavailable as exc:
        await status.edit_text(f"⚠️ {escape(str(exc))}")
        return
    if not result:
        await status.edit_text("⚠️ Нет текста страницы для анализа (сайт не открылся?).")
        return
    await refresh_card(bot, cb.message.chat.id, cb.message.message_id, cid)
    async with SessionLocal() as session:
        company = await repo.get_company(session, cid)
    lines = [f"🧠 <b>Готово: качество {result.quality_score}/100</b>, итоговая оценка {company.score if company else '—'}/100"]
    if result.trust_gaps:
        lines.append("Доверие: " + escape(", ".join(result.trust_gaps[:5])))
    if result.is_good_site:
        lines.append("ИИ считает сайт хорошим." + (f" Статус: {label(company.status)}" if company else ""))
    await status.edit_text("\n".join(lines))


# ---------------------------------------------------------------- письмо


@router.callback_query(CardCb.filter(F.action == "letter"), StateFilter("*"))
async def letter_start(cb: CallbackQuery, callback_data: CardCb) -> None:
    assert cb.message is not None
    await cb.message.edit_reply_markup(reply_markup=letter_channels(callback_data.cid))
    await cb.answer("Куда пишем?")


async def generate_letter(cid: int, channel: str, shorter: bool = False, previous: str | None = None) -> tuple[int, str]:
    """Сгенерировать черновик, сохранить событием ai_draft. Возвращает (id события, текст)."""
    s = get_settings()
    async with SessionLocal() as session:
        company = await repo.get_company(session, cid)
        audit = await repo.last_audit(session, cid)
        if not company:
            raise LLMUnavailable("Лид не найден")
        problems = main_points(audit)
        if not problems:
            raise LLMUnavailable("В аудите нет проблем — не о чем писать. Запустите 🧠 Глубокий анализ.")
        pitch = ((audit.llm_summary or {}) if audit else {}).get("pitch_angle")
        name, domain = company.name or company.domain, company.domain
    owner = {"name": s.OWNER_NAME, "phone": s.OWNER_PHONE, "telegram": s.OWNER_TELEGRAM, "email": s.OWNER_EMAIL}
    ai = ai_client()
    text = await ai.text(
        "letter", LETTER_SYSTEM, letter_user(channel, name, domain, problems, pitch, owner, shorter, previous)
    )
    if len(text) > LETTER_MAX_CHARS:  # модель иногда превышает лимит — одна попытка сократить
        text = await ai.text(
            "letter", LETTER_SYSTEM, letter_user(channel, name, domain, problems, pitch, owner, True, text)
        )
    async with SessionLocal() as session:
        ev = await repo.add_event(session, company_id=cid, type="ai_draft",
                                  payload={"channel": channel, "text": text, "shorter": shorter})
        await session.commit()
        return ev.id, text


def draft_message(text: str, channel: str) -> str:
    title = "📧 Черновик письма" if channel == "email" else "💬 Черновик сообщения"
    return f"<b>{title}</b> ({len(text)} зн.)\n\n{escape(text)}"


@router.callback_query(CardCb.filter(F.action == "lt"), StateFilter("*"))
async def letter_generate(cb: CallbackQuery, callback_data: CardCb, bot: Bot) -> None:
    assert cb.message is not None
    await cb.answer("✍️ Пишу…")
    await refresh_card(bot, cb.message.chat.id, cb.message.message_id, callback_data.cid)
    status = await cb.message.answer("✍️ Пишу черновик…")
    try:
        eid, text = await generate_letter(callback_data.cid, callback_data.arg)
    except LLMUnavailable as exc:
        await status.edit_text(f"⚠️ {escape(str(exc))}")
        return
    await status.edit_text(draft_message(text, callback_data.arg), reply_markup=draft_keyboard(eid))


async def _draft_event(eid: int) -> Event | None:
    async with SessionLocal() as session:
        return await session.get(Event, eid)


@router.callback_query(DraftCb.filter(F.action.in_({"again", "short"})), StateFilter("*"))
async def draft_regen(cb: CallbackQuery, callback_data: DraftCb) -> None:
    ev = await _draft_event(callback_data.eid)
    if not ev or not cb.message:
        await cb.answer("Черновик не найден")
        return
    await cb.answer("✍️ Переписываю…")
    channel = ev.payload.get("channel", "email")
    try:
        eid, text = await generate_letter(ev.company_id, channel, shorter=callback_data.action == "short",
                                          previous=ev.payload.get("text"))
    except LLMUnavailable as exc:
        await cb.message.answer(f"⚠️ {escape(str(exc))}")
        return
    await cb.message.answer(draft_message(text, channel), reply_markup=draft_keyboard(eid))


@router.callback_query(DraftCb.filter(F.action == "copy"), StateFilter("*"))
async def draft_copy(cb: CallbackQuery, callback_data: DraftCb) -> None:
    ev = await _draft_event(callback_data.eid)
    if not ev or not cb.message:
        await cb.answer("Черновик не найден")
        return
    await cb.message.answer(ev.payload.get("text", ""), parse_mode=None)
    await cb.answer()


@router.callback_query(DraftCb.filter(F.action == "sent"), StateFilter("*"))
async def draft_sent(cb: CallbackQuery, callback_data: DraftCb) -> None:
    ev = await _draft_event(callback_data.eid)
    if not ev:
        await cb.answer("Черновик не найден")
        return
    async with SessionLocal() as session:
        company = await repo.get_company(session, ev.company_id)
        if not company:
            await cb.answer("Лид не найден")
            return
        if company.status != Status.SENT.value:
            await service.change_status(session, company, Status.SENT,
                                        payload={"via": "ai_draft", "draft_event": ev.id, "channel": ev.payload.get("channel")})
            await session.commit()
    await cb.answer("✉️ Отмечено: отправлено")
    if cb.message:
        await cb.message.edit_reply_markup(reply_markup=None)
        await cb.message.answer(f"✉️ {escape(company.name or company.domain)}: статус {label(Status.SENT)}")
