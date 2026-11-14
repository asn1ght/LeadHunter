"""Проверка напоминаний (каждую минуту) и предупреждение за 15 минут до звонка №2."""
from __future__ import annotations

from datetime import timedelta
from html import escape

from aiogram import Bot
from loguru import logger

from app.bot.common import send_card
from app.bot.keyboards import reminder_row
from app.config import get_settings
from app.db import repo
from app.db.models import utcnow
from app.db.session import SessionLocal
from app.utils.dates import fmt_dt

PRE_NOTIFY = timedelta(minutes=15)
KIND_TEXT = {"callback": "созвониться", "call2": "звонок №2", "remind": "напоминание"}


async def check_reminders(bot: Bot) -> None:
    """Отправить наступившие напоминания и предупреждения о звонках №2."""
    s = get_settings()
    tz = s.tz
    now = utcnow()
    try:
        async with SessionLocal() as session:
            due = await repo.due_reminders(session, now)
            upcoming = await repo.upcoming_call2(session, now, now + PRE_NOTIFY)
            items = [(r.id, r.company_id, r.kind, r.text, r.due_at) for r in due]
            pre = [(r.id, r.company_id, r.due_at) for r in upcoming]
            for r in due:
                r.fired_at = now
                await repo.add_event(
                    session, company_id=r.company_id, type="reminder_fired", payload={"reminder_id": r.id, "kind": r.kind}
                )
            for r in upcoming:
                r.pre_notified_at = now
            await session.commit()
    except Exception as exc:  # noqa: BLE001
        logger.exception("check_reminders failed: {}", exc)
        return

    for rid, cid, kind, text, due_at in items:
        prefix = f"⏰ <b>Напоминание: {KIND_TEXT.get(kind, kind)}</b> · {fmt_dt(due_at, tz)}"
        if text:
            prefix += f"\n{escape(text)}"
        for admin in s.admin_ids:
            try:
                await send_card(bot, admin, cid, prefix=prefix, extra_rows=[reminder_row(rid)])
            except Exception as exc:  # noqa: BLE001
                logger.warning("send reminder {} to {} failed: {}", rid, admin, exc)

    for rid, cid, due_at in pre:
        mins = max(1, int((due_at - now).total_seconds() // 60))
        prefix = f"🔔 <b>Через {mins} мин звонок №2</b> · {fmt_dt(due_at, tz)}"
        for admin in s.admin_ids:
            try:
                await send_card(bot, admin, cid, prefix=prefix)
            except Exception as exc:  # noqa: BLE001
                logger.warning("send pre-notify {} failed: {}", rid, exc)
