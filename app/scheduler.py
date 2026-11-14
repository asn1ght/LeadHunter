"""Планировщик (APScheduler): напоминания, отчёты, ночной поиск, утренняя рассылка."""
from __future__ import annotations

import re

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from loguru import logger

from app.config import get_settings
from app.crm.reminders import check_reminders
from app.db import repo
from app.db.session import SessionLocal
from app.discovery.jobs import morning_job, poll_job, scheduled_discovery
from app.reports.service import send_scheduled_report


def _hm(value: str, default: tuple[int, int] = (21, 0)) -> tuple[int, int]:
    try:
        h, m = value.strip().split(":")
        return int(h), int(m)
    except ValueError:
        logger.warning("bad time {!r}, using default", value)
        return default


_CRON_DOW = {"0": "sun", "1": "mon", "2": "tue", "3": "wed", "4": "thu", "5": "fri", "6": "sat", "7": "sun"}


def crontab_trigger(expr: str, tz) -> CronTrigger:  # type: ignore[no-untyped-def]
    """CronTrigger из обычной crontab-строки «мин час день месяц день_недели».

    В crontab понедельник = 1, воскресенье = 0/7, а в APScheduler числа дней недели начинаются
    с понедельника = 0 — поэтому числа переводим в названия (mon…sun), иначе всё сдвигается на день.
    """
    minute, hour, day, month, dow = expr.split()
    dow = re.sub(r"[0-7]", lambda m: _CRON_DOW[m.group(0)], dow) if not dow.startswith("*") else dow
    return CronTrigger(minute=minute, hour=hour, day=day, month=month, day_of_week=dow, timezone=tz)


def report_triggers(daily_time: str, weekly_dow: str, monthly_day: str, tz) -> dict[str, CronTrigger]:  # type: ignore[no-untyped-def]
    """Триггеры отчётов. Недельный и месячный — через 1 и 2 минуты после дневного, чтобы шли по порядку."""
    h, m = _hm(daily_time)

    def at(offset: int) -> tuple[int, int]:
        total = h * 60 + m + offset
        return (total // 60) % 24, total % 60

    wh, wm = at(1)
    mh, mm = at(2)
    return {
        "day": CronTrigger(hour=h, minute=m, timezone=tz),
        "week": CronTrigger(day_of_week=weekly_dow, hour=wh, minute=wm, timezone=tz),
        "month": CronTrigger(day=monthly_day, hour=mh, minute=mm, timezone=tz),
    }


async def setup_scheduler(bot: Bot) -> AsyncIOScheduler:
    tz = get_settings().tz
    async with SessionLocal() as session:
        daily = await repo.get_runtime_setting(session, "DAILY_REPORT_TIME")
        weekly = await repo.get_runtime_setting(session, "WEEKLY_REPORT_DOW")
        monthly = await repo.get_runtime_setting(session, "MONTHLY_REPORT_DAY")
        discovery_cron = await repo.get_runtime_setting(session, "DISCOVERY_CRON")
        push_time = await repo.get_runtime_setting(session, "NEW_LEADS_PUSH_TIME")

    scheduler = AsyncIOScheduler(timezone=tz)
    scheduler.add_job(
        check_reminders, "interval", minutes=1, args=[bot], id="reminders",
        max_instances=1, coalesce=True, misfire_grace_time=30,
    )
    for kind, trigger in report_triggers(daily, weekly, monthly, tz).items():
        scheduler.add_job(
            send_scheduled_report, trigger, args=[bot, kind], id=f"report_{kind}",
            coalesce=True, misfire_grace_time=3600,
        )
    try:
        discovery_trigger = crontab_trigger(discovery_cron, tz)
    except ValueError:
        logger.warning("bad DISCOVERY_CRON {!r}, using '0 2 * * 1'", discovery_cron)
        discovery_trigger = crontab_trigger("0 2 * * 1", tz)
    scheduler.add_job(scheduled_discovery, discovery_trigger, args=[bot], id="discovery",
                      max_instances=1, coalesce=True, misfire_grace_time=3600)
    scheduler.add_job(poll_job, "interval", minutes=10, args=[bot], id="search_poll",
                      max_instances=1, coalesce=True)
    ph, pm = _hm(push_time, (9, 30))
    scheduler.add_job(morning_job, CronTrigger(hour=ph, minute=pm, timezone=tz), args=[bot], id="morning",
                      coalesce=True, misfire_grace_time=3600)
    logger.info("reports: daily {}, weekly {}, monthly day={}; discovery '{}'; push {}",
                daily, weekly, monthly, discovery_cron, push_time)
    return scheduler
