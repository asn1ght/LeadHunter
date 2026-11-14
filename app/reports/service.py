"""Сборка готового текста отчёта и автоотправка по расписанию."""
from __future__ import annotations

from datetime import date

from aiogram import Bot
from loguru import logger

from app.config import get_settings
from app.db import repo
from app.db.models import utcnow
from app.db.session import SessionLocal
from app.reports.builder import Period, build_report
from app.reports.formatter import format_report


def today_local() -> date:
    return utcnow().astimezone(get_settings().tz).date()


async def report_text(period: Period) -> str:
    """Текст отчёта; для недели и месяца — со сравнением с прошлым периодом."""
    tz = get_settings().tz
    async with SessionLocal() as session:
        threshold = await repo.get_runtime_setting(session, "SCORE_THRESHOLD")
        data = await build_report(session, period, tz, threshold)
        prev = None
        if period.kind in ("week", "month"):
            prev = await build_report(session, period.prev(), tz, threshold)
    return format_report(period, data, prev)


async def send_scheduled_report(bot: Bot, kind: str) -> None:
    """Отправить отчёт за текущий день / неделю / месяц всем админам."""
    from app.bot.keyboards import report_keyboard

    period = Period.of(kind, today_local())
    try:
        text = await report_text(period)
    except Exception as exc:  # noqa: BLE001
        logger.exception("report {} failed: {}", kind, exc)
        return
    for admin in get_settings().admin_ids:
        try:
            await bot.send_message(admin, text, reply_markup=report_keyboard(period, today_local()))
        except Exception as exc:  # noqa: BLE001
            logger.warning("send report to {} failed: {}", admin, exc)
