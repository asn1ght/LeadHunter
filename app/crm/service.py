"""CRM-операции. Любая смена статуса — только через `change_status()` (раздел 5.3)."""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.extract import find_phones
from app.config import get_settings
from app.crm.statuses import CALL1_OUTCOMES, CALL2_OUTCOMES, Status
from app.db import repo
from app.db.models import Company, Event, Reminder, utcnow


async def change_status(
    session: AsyncSession,
    company: Company,
    to_status: Status | str,
    payload: dict[str, Any] | None = None,
) -> Event:
    """Сменить статус лида и записать событие `status_change` в той же транзакции.

    Коммит делает вызывающий код (одна транзакция на действие пользователя).
    """
    to = Status(to_status)
    ev = await repo.add_event(
        session,
        company_id=company.id,
        from_status=company.status,
        to_status=to.value,
        type="status_change",
        payload=payload or {},
    )
    company.status = to.value
    company.updated_at = utcnow()
    if to in (Status.AGREED, Status.REFUSED, Status.CANCELLED):
        await repo.close_open_reminders(session, company.id)
        company.next_action_at = None
    await session.flush()
    return ev


# Статусы, из которых «отправил сообщение» — шаг вперёд. Дальше по воронке (ответил, тест, звонок №2,
# сделка, отказ) отметка об отправке статус не трогает, иначе лид откатится назад.
SENDABLE_FROM = frozenset({Status.NEW, Status.CANCELLED, Status.IGNORED, Status.CALL1_NEEDED, Status.CALLBACK})


async def mark_sent(session: AsyncSession, company: Company, payload: dict[str, Any] | None = None) -> bool:
    """Отметить, что сообщение отправлено. Возвращает True, если статус сменился на «Отправлено»."""
    if Status(company.status) not in SENDABLE_FROM:
        return False
    await change_status(session, company, Status.SENT, payload=payload)
    return True


async def add_note(session: AsyncSession, company: Company, text: str) -> Event:
    """Добавить заметку: событие `note` + дописать в `notes` с датой."""
    text = text.strip()
    stamp = utcnow().astimezone(get_settings().tz).strftime("%d.%m %H:%M")
    line = f"[{stamp}] {text}"
    company.notes = f"{company.notes}\n{line}" if company.notes else line
    company.updated_at = utcnow()
    return await repo.add_event(session, company_id=company.id, type="note", payload={"text": text})


async def log_call(
    session: AsyncSession,
    company: Company,
    stage: int,
    outcome: str,
    note: str | None = None,
) -> Event:
    """Записать проведённый звонок (`call_logged`) и сменить статус по исходу.

    Исход `RESCHEDULED` (перенос звонка №2) статус не меняет.
    """
    payload: dict[str, Any] = {"stage": stage, "outcome": outcome}
    if note:
        payload["note"] = note
    ev = await repo.add_event(session, company_id=company.id, type="call_logged", payload=payload)
    if note:
        await add_note(session, company, f"Звонок №{stage}: {note}")
    outcomes = CALL1_OUTCOMES if stage == 1 else CALL2_OUTCOMES
    if outcome in outcomes:
        await change_status(session, company, outcomes[outcome][1], payload={"call_stage": stage, "outcome": outcome})
    if stage == 2:
        # звонок №2 состоялся (или переносится) — старое напоминание о нём закрываем
        await repo.close_open_reminders(session, company.id)
        company.next_action_at = None
    return ev


async def schedule(
    session: AsyncSession,
    company: Company,
    due_at: datetime,
    kind: str,
    contact: str | None = None,
    comment: str | None = None,
    set_status: Status | None = None,
) -> Reminder:
    """Создать напоминание (заменяет открытые) и при необходимости сменить статус.

    kind: `callback` (созвониться потом), `call2` (звонок №2), `remind` (просто напомнить).
    """
    await repo.close_open_reminders(session, company.id)
    parts = [p for p in (contact, comment) if p]
    if contact:
        name, phone = split_contact(contact)
        if name:
            company.contact_person = name
        if phone:
            company.contact_phone = phone
    reminder = await repo.add_reminder(
        session, company_id=company.id, due_at=due_at, kind=kind, text=" · ".join(parts)
    )
    company.next_action_at = due_at
    payload = {"due_at": due_at.isoformat(), "kind": kind}
    if contact:
        payload["contact"] = contact
    if comment:
        payload["comment"] = comment
    if set_status and set_status.value != company.status:
        await change_status(session, company, set_status, payload=payload)
    await session.flush()
    return reminder


def split_contact(text: str) -> tuple[str | None, str | None]:
    """Разделить «Иван +7 911 123-45-67» на имя и телефон."""
    phones = find_phones(text)
    phone = phones[0] if phones else None
    name = text
    if phone:
        name = re.sub(r"[+\d][\d\s\-()]{6,}\d", "", text).strip(" ,;-")
    return (name or None), phone
