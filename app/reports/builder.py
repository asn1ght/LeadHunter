"""Подсчёт отчёта по журналу событий (раздел 9).

Все цифры, кроме «Найдено новых лидов», считаются только по таблице `events`.
Смены статуса считаются по уникальным компаниям (повторная установка того же
статуса не удваивает цифру), звонки — по событиям (два звонка = два звонка).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, fields
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Company, Event

MONTHS = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август",
          "сентябрь", "октябрь", "ноябрь", "декабрь"]
IGNORE_OUTCOMES = {"IGNORED", "no_answer"}


@dataclass(frozen=True)
class Period:
    """Период отчёта `[start, end)` в датах часового пояса владельца."""

    kind: str  # day | week | month
    start: date
    end: date

    @classmethod
    def day(cls, d: date) -> Period:
        return cls("day", d, d + timedelta(days=1))

    @classmethod
    def week(cls, d: date) -> Period:
        """Неделя пн–вс, содержащая дату `d`."""
        start = d - timedelta(days=d.weekday())
        return cls("week", start, start + timedelta(days=7))

    @classmethod
    def month(cls, d: date) -> Period:
        start = d.replace(day=1)
        end = (start + timedelta(days=32)).replace(day=1)
        return cls("month", start, end)

    @classmethod
    def of(cls, kind: str, d: date) -> Period:
        return {"day": cls.day, "week": cls.week, "month": cls.month}[kind](d)

    def prev(self) -> Period:
        return Period.of(self.kind, self.start - timedelta(days=1))

    def next(self) -> Period:
        return Period.of(self.kind, self.end)

    def utc_bounds(self, tz: ZoneInfo) -> tuple[datetime, datetime]:
        """Границы периода в UTC."""
        return (
            datetime.combine(self.start, time(0), tz).astimezone(timezone.utc),
            datetime.combine(self.end, time(0), tz).astimezone(timezone.utc),
        )

    def title(self) -> str:
        if self.kind == "day":
            return f"за {self.start:%d.%m.%Y}"
        if self.kind == "week":
            last = self.end - timedelta(days=1)
            return f"за неделю {self.start:%d.%m}–{last:%d.%m.%Y}"
        return f"за {MONTHS[self.start.month - 1]} {self.start.year}"


@dataclass
class ReportData:
    """Цифры отчёта."""

    found_new: int = 0
    sent: int = 0
    replied: int = 0
    callback: int = 0
    call1_done: int = 0
    test_agreed: int = 0
    call1_refused: int = 0
    call1_ignored: int = 0
    call2_scheduled: int = 0
    call2_done: int = 0
    call2_refused: int = 0
    call2_ignored: int = 0
    call2_rescheduled: int = 0
    agreed: int = 0
    thinking: int = 0
    refused: int = 0
    ignored: int = 0

    def as_dict(self) -> dict[str, int]:
        return {f.name: getattr(self, f.name) for f in fields(self)}


async def build_report(session: AsyncSession, period: Period, tz: ZoneInfo, threshold: int) -> ReportData:
    """Посчитать отчёт за период."""
    start, end = period.utc_bounds(tz)
    data = ReportData()

    data.found_new = (
        await session.execute(
            select(func.count(Company.id)).where(
                Company.created_at >= start,
                Company.created_at < end,
                Company.score >= threshold,
            )
        )
    ).scalar_one()

    events = (
        await session.execute(select(Event).where(Event.created_at >= start, Event.created_at < end))
    ).scalars().all()

    to_status: dict[str, set[int]] = {}
    for ev in events:
        if ev.type == "status_change" and ev.to_status:
            to_status.setdefault(ev.to_status, set()).add(ev.company_id)
        elif ev.type == "call_logged":
            stage = (ev.payload or {}).get("stage")
            outcome = (ev.payload or {}).get("outcome")
            if stage == 1:
                data.call1_done += 1
                data.call1_refused += outcome == "REFUSED"
                data.call1_ignored += outcome in IGNORE_OUTCOMES
            elif stage == 2:
                if outcome == "RESCHEDULED":
                    data.call2_rescheduled += 1
                else:
                    data.call2_done += 1
                    data.call2_refused += outcome == "REFUSED"
                    data.call2_ignored += outcome in IGNORE_OUTCOMES

    n = lambda st: len(to_status.get(st, ()))  # noqa: E731
    data.sent = n("SENT")
    data.replied = n("REPLIED")
    data.callback = n("CALLBACK")
    data.test_agreed = n("TEST_AGREED")
    data.call2_scheduled = n("CALL2_SCHEDULED")
    data.agreed = n("AGREED")
    data.thinking = n("THINKING")
    data.refused = n("REFUSED")
    data.ignored = n("IGNORED")
    return data


def parse_report_date(text: str, today: date) -> date | None:
    """Дата для отчёта (в прошлом): «13.11», «13.11.2026», «2026-11-13», «вчера», «сегодня».

    Дата без года в будущем относится к прошлому году.
    """
    t = text.strip().lower()
    rel = {"сегодня": 0, "вчера": 1, "позавчера": 2}
    if t in rel:
        return today - timedelta(days=rel[t])
    try:
        if m := re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", t):
            return date(int(m[1]), int(m[2]), int(m[3]))
        if m := re.fullmatch(r"(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?", t):
            year = today.year
            if m[3]:
                year = int(m[3]) + (2000 if len(m[3]) == 2 else 0)
            d = date(year, int(m[2]), int(m[1]))
            if not m[3] and d > today:
                d = d.replace(year=year - 1)
            return d
    except ValueError:
        return None
    return None
