"""📄 PDF-аудит и 🖼 скриншоты сайта (этап 4)."""
from __future__ import annotations

from datetime import datetime
from html import escape

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.types import BufferedInputFile, CallbackQuery, FSInputFile, InputMediaPhoto
from loguru import logger

from app.audit.browser import abs_path, capture
from app.audit.pdf import render_pdf
from app.bot.keyboards import CardCb
from app.config import get_settings
from app.db import repo
from app.db.models import Audit, Company
from app.db.session import SessionLocal

router = Router(name="pdf")


async def ensure_screenshots(cid: int) -> tuple[Company | None, Audit | None]:
    """Последний аудит со скриншотами; если файлов нет — снять заново и сохранить в аудит."""
    async with SessionLocal() as session:
        company = await repo.get_company(session, cid)
        audit = await repo.last_audit(session, cid)
        if not company or not audit:
            return company, audit
        if abs_path(audit.screenshot_desktop) and abs_path(audit.screenshot_mobile):
            return company, audit
        url = audit.final_url or company.url
    res = await capture(url, cid)
    async with SessionLocal() as session:
        audit = await repo.last_audit(session, cid)
        company = await repo.get_company(session, cid)
        if audit:
            audit.screenshot_desktop = res.screenshot_desktop or audit.screenshot_desktop
            audit.screenshot_mobile = res.screenshot_mobile or audit.screenshot_mobile
            if res.mobile_overflow is not None:
                audit.mobile_overflow = res.mobile_overflow
            await session.commit()
        return company, audit


@router.callback_query(CardCb.filter(F.action == "pdf"), StateFilter("*"))
async def pdf_audit(cb: CallbackQuery, callback_data: CardCb) -> None:
    assert cb.message is not None
    await cb.answer("📄 Готовлю PDF…")
    status = await cb.message.answer("📄 Готовлю PDF-аудит: скриншоты и вёрстка, до минуты…")
    try:
        company, audit = await ensure_screenshots(callback_data.cid)
        if not company or not audit:
            await status.edit_text("⚠️ Сначала нужен аудит сайта.")
            return
        pdf = await render_pdf(company, audit)
    except Exception as exc:  # noqa: BLE001
        logger.exception("pdf failed for {}: {}", callback_data.cid, exc)
        await status.edit_text(f"⚠️ Не удалось сделать PDF: {escape(type(exc).__name__)}")
        return
    date = datetime.now(get_settings().tz).strftime("%Y-%m-%d")
    name = f"audit-{company.domain}-{date}.pdf"
    await cb.message.answer_document(
        BufferedInputFile(pdf, filename=name),
        caption=f"📄 Мини-аудит {escape(company.name or company.domain)} — можно отправлять клиенту.",
    )
    await status.delete()


@router.callback_query(CardCb.filter(F.action == "shots"), StateFilter("*"))
async def screenshots(cb: CallbackQuery, callback_data: CardCb) -> None:
    assert cb.message is not None
    await cb.answer("🖼 Снимаю…")
    company, audit = await ensure_screenshots(callback_data.cid)
    if not company or not audit:
        await cb.message.answer("⚠️ Сначала нужен аудит сайта.")
        return
    media = []
    for rel, label in ((audit.screenshot_desktop, "💻 Компьютер"), (audit.screenshot_mobile, "📱 Телефон")):
        if (p := abs_path(rel)):
            media.append(InputMediaPhoto(media=FSInputFile(p), caption=f"{label} · {escape(company.domain)}"))
    if not media:
        await cb.message.answer("⚠️ Не удалось сделать скриншоты (сайт не открылся в браузере).")
        return
    await cb.message.answer_media_group(media)
