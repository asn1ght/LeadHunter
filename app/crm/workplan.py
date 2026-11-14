"""План обзвона на неделю и очередь «📞 Сегодня».

- В понедельник лучшие по оценке найденные лиды (LEADS_WEEKLY_LIMIT) попадают в план недели:
  им ставится `notified_at`. Остальные ждут в запасе (/new) до следующего плана.
- Очередь обзвона — лиды плана (и добавленные вручную через /add) в статусах «Новый» и
  «Нужно созвониться». Пока статус не сменили, лид остаётся в очереди и переходит на следующий
  день и неделю. Сменили статус (отправил, игнор, отказ, созвониться потом…) — ушёл из очереди.
"""
from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.crm.statuses import Status
from app.db import repo
from app.db.models import Company, utcnow

QUEUE_STATUSES = (Status.NEW.value, Status.CALL1_NEEDED.value)
WORKDAYS = 5  # пн–пт


def week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def week_start_utc(now: datetime | None = None) -> datetime:
    tz = get_settings().tz
    local = (now or utcnow()).astimezone(tz)
    return datetime.combine(week_start(local.date()), time(0), tz).astimezone(timezone.utc)


def workdays_left(d: date) -> int:
    """Рабочих дней до конца недели, включая сегодня (в выходные — 0)."""
    return max(0, WORKDAYS - d.weekday())


def daily_pace(queue_len: int, d: date) -> int | None:
    """Сколько звонков в день нужно, чтобы закрыть очередь к пятнице. None — выходной."""
    left = workdays_left(d)
    if left == 0:
        return None
    return math.ceil(queue_len / left) if queue_len else 0


def _in_plan():
    return or_(Company.notified_at.is_not(None), Company.source == "manual")


async def call_queue(session: AsyncSession) -> list[Company]:
    """Очередь обзвона: сначала самые давние в плане, внутри — по оценке."""
    return list(
        (
            await session.execute(
                select(Company)
                .where(Company.status.in_(QUEUE_STATUSES), _in_plan())
                .order_by(func.coalesce(Company.notified_at, Company.created_at), Company.score.desc().nulls_last())
            )
        ).scalars()
    )


async def pool(session: AsyncSession) -> list[Company]:
    """Запас: найденные поиском лиды выше порога, ещё не взятые в план."""
    threshold = await repo.get_runtime_setting(session, "SCORE_THRESHOLD")
    return list(
        (
            await session.execute(
                select(Company)
                .where(
                    Company.status == Status.NEW.value,
                    Company.source == "yandex",
                    Company.notified_at.is_(None),
                    Company.score >= threshold,
                )
                .order_by(Company.score.desc(), Company.id)
            )
        ).scalars()
    )


async def assigned_this_week(session: AsyncSession) -> int:
    return int(
        (
            await session.execute(
                select(func.count(Company.id)).where(
                    Company.source == "yandex", Company.notified_at >= week_start_utc()
                )
            )
        ).scalar_one()
    )


async def assign(session: AsyncSession, limit: int) -> list[Company]:
    """Взять в план до `limit` лучших лидов из запаса."""
    if limit <= 0:
        return []
    chosen = (await pool(session))[:limit]
    now = utcnow()
    for c in chosen:
        c.notified_at = now
    await session.flush()
    return chosen
