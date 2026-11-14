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
GIVE_UP_AFTER = timedelta(hours=24)  # столько пытаемся доставить напоминание, потом закрываем
KIND_TEXT = {"callback": "созвониться", "call2": "звонок №2", "remind": "напоминание"}


async def _deliver(bot: Bot, admins: set[int], cid: int, what: str, **kw: object) -> bool:
    """Отправить карточку всем админам. True — хотя бы одному дошло."""
    delivered = False
    for admin in admins:
        try:
            await send_card(bot, admin, cid, **kw)  # type: ignore[arg-type]
            delivered = True
        except Exception as exc:  # noqa: BLE001
            logger.warning("send {} to {} failed: {}", what, admin, exc)
    return delivered


async def check_reminders(bot: Bot) -> None:
    """Отправить наступившие напоминания и предупреждения о звонках №2.

    Напоминание помечается отправленным только после доставки: если Telegram недоступен,
    попытка повторится через минуту. Через GIVE_UP_AFTER безуспешных попыток напоминание
    закрывается с отметкой в журнале, чтобы не повторять его бесконечно.
    """
    s = get_settings()
    tz = s.tz
    now = utcnow()
    try:
        async with SessionLocal() as session:
            due = await repo.due_reminders(session, now)
            upcoming = await repo.upcoming_call2(session, now, now + PRE_NOTIFY)
            items = [(r.id, r.company_id, r.kind, r.text, r.due_at) for r in due]
            pre = [(r.id, r.company_id, r.due_at) for r in upcoming]
    except Exception as exc:  # noqa: BLE001
        logger.exception("check_reminders failed: {}", exc)
        return

    fired: list[tuple[int, bool]] = []  # (id напоминания, доставлено ли)
    for rid, cid, kind, text, due_at in items:
        prefix = f"⏰ <b>Напоминание: {KIND_TEXT.get(kind, kind)}</b> · {fmt_dt(due_at, tz)}"
        if text:
            prefix += f"\n{escape(text)}"
        ok = await _deliver(bot, s.admin_ids, cid, f"reminder {rid}", prefix=prefix, extra_rows=[reminder_row(rid)])
        if ok or now - due_at > GIVE_UP_AFTER:
            fired.append((rid, ok))

    pre_sent: list[int] = []
    for rid, cid, due_at in pre:
        mins = max(1, int((due_at - now).total_seconds() // 60))
        prefix = f"🔔 <b>Через {mins} мин звонок №2</b> · {fmt_dt(due_at, tz)}"
        if await _deliver(bot, s.admin_ids, cid, f"pre-notify {rid}", prefix=prefix):
            pre_sent.append(rid)

    if not fired and not pre_sent:
        return
    try:
        async with SessionLocal() as session:
            for rid, ok in fired:
                r = await repo.get_reminder(session, rid)
                if not r:
                    continue
                r.fired_at = now
                payload: dict[str, object] = {"reminder_id": rid, "kind": r.kind}
                if not ok:
                    payload["undelivered"] = True
                    logger.error("reminder {} not delivered for {}, giving up", rid, GIVE_UP_AFTER)
                await repo.add_event(session, company_id=r.company_id, type="reminder_fired", payload=payload)
            for rid in pre_sent:
                r = await repo.get_reminder(session, rid)
                if r:
                    r.pre_notified_at = now
            await session.commit()
    except Exception as exc:  # noqa: BLE001 — отправлено, но не отмечено: в худшем случае придёт повторно
        logger.exception("check_reminders: mark sent failed: {}", exc)
