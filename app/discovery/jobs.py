"""Задачи планировщика: ночной поиск, опрос отложенных запросов, план обзвона на неделю."""
from __future__ import annotations

import asyncio
from datetime import datetime, time, timedelta, timezone
from html import escape

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.client import DeepSeekClient
from app.config import get_settings
from app.crm import workplan
from app.db import repo
from app.db.models import utcnow
from app.db.session import SessionLocal
from app.discovery.runner import RunStats, poll_operations, run_discovery
from app.discovery.yandex_search import YandexSearchClient, YandexSearchError


def make_search_client() -> YandexSearchClient:
    s = get_settings()
    return YandexSearchClient(s.YANDEX_SEARCH_API_KEY, s.YANDEX_FOLDER_ID, s.YANDEX_SEARCH_REGION,
                              per_page=s.YANDEX_GROUPS_ON_PAGE)


async def notify_admins(bot: Bot, text: str) -> None:
    for admin in get_settings().admin_ids:
        try:
            await bot.send_message(admin, text)
        except Exception as exc:  # noqa: BLE001
            logger.warning("notify {} failed: {}", admin, exc)


# Один поиск за раз: ночной прогон, ручной /run и опрос отложенных запросов не пересекаются
# (иначе — двойной расход запросов и гонка при записи одних и тех же доменов).
discovery_lock = asyncio.Lock()
BUSY = "поиск уже идёт — дождитесь итога"


async def discovery_job(bot: Bot, force: bool = False, niche_slug: str | None = None) -> RunStats:
    """Прогон поиска (по расписанию или из /run). Если другой прогон ещё идёт — не запускается."""
    if discovery_lock.locked():
        return RunStats(stopped_reason=BUSY)
    async with discovery_lock:
        try:
            client = make_search_client()
        except YandexSearchError as exc:
            return RunStats(stopped_reason=str(exc))
        try:
            return await run_discovery(
                SessionLocal, client, DeepSeekClient(SessionLocal), niche_slug=niche_slug, force=force,
                notify=lambda t: notify_admins(bot, t),
            )
        finally:
            await client.aclose()


async def scheduled_discovery(bot: Bot) -> None:
    try:
        stats = await discovery_job(bot)
    except Exception as exc:  # noqa: BLE001 — непредвиденный сбой не должен пропадать молча
        logger.exception("scheduled discovery failed: {}", exc)
        await notify_admins(bot, f"🌙 Ночной поиск упал: {escape(type(exc).__name__)}: {escape(str(exc)[:300])}\n"
                                 "Подробности в логе. Можно повторить вручную: /run")
        return
    if stats.errors or stats.stopped_reason or stats.paused:
        tail = "\nВключить фразы обратно — в /queries." if stats.paused else ""
        await notify_admins(bot, "🌙 Ночной поиск\n" + stats.summary() + tail)


async def poll_job(bot: Bot) -> None:
    if discovery_lock.locked():
        return  # идёт прогон — заберём на следующем опросе
    try:
        client = make_search_client()
    except YandexSearchError:
        return
    try:
        async with discovery_lock:
            stats = await poll_operations(SessionLocal, client, DeepSeekClient(SessionLocal),
                                          notify=lambda t: notify_admins(bot, t))
        if stats.new_companies:
            logger.info("poll: {}", stats.summary().replace("\n", " | "))
    except Exception as exc:  # noqa: BLE001
        logger.exception("poll_job failed: {}", exc)
    finally:
        await client.aclose()


DOW = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def today_button() -> InlineKeyboardMarkup:
    from app.bot.keyboards import NavCb

    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📞 Открыть «Сегодня»", callback_data=NavCb(action="today").pack())
    ]])


async def _send(bot: Bot, text: str) -> None:
    for admin in get_settings().admin_ids:
        try:
            await bot.send_message(admin, text, reply_markup=today_button())
        except Exception as exc:  # noqa: BLE001
            logger.warning("notify {} failed: {}", admin, exc)


async def weekly_plan(bot: Bot, session_factory: async_sessionmaker[AsyncSession] = SessionLocal) -> int:
    """Понедельник: взять в план недели лучшие лиды из запаса и прислать сводку. Возвращает число взятых."""
    async with session_factory() as session:
        carried = len(await workplan.call_queue(session))
        limit = int(await repo.get_runtime_setting(session, "LEADS_WEEKLY_LIMIT"))
        taken = await workplan.assign(session, limit)
        left_in_pool = len(await workplan.pool(session))
        await session.commit()
    total = carried + len(taken)
    if not total:
        await _send(bot, "☀️ План на неделю: новых лидов нет, очередь пуста.\nМожно запустить поиск: /run")
        return 0
    today = utcnow().astimezone(get_settings().tz).date()
    pace = workplan.daily_pace(total, today)
    lines = ["☀️ <b>План на неделю</b>", f"🆕 Новых в плане: {len(taken)}"]
    if left_in_pool:
        lines.append(f"📦 В запасе ещё {left_in_pool} — в /new, придут в следующий план")
    if carried:
        lines.append(f"↪️ Не обработано с прошлых недель: {carried}")
    lines.append(f"📞 Всего к обзвону: {total}" + (f" · ≈{pace} в день до пятницы" if pace else ""))
    await _send(bot, "\n".join(lines))
    return len(taken)


async def daily_reminder(bot: Bot, session_factory: async_sessionmaker[AsyncSession] = SessionLocal) -> None:
    """Вт–пт: короткое напоминание, если есть кому звонить."""
    tz = get_settings().tz
    today = utcnow().astimezone(tz).date()
    if workplan.workdays_left(today) == 0:
        return
    end = datetime.combine(today + timedelta(days=1), time(0), tz).astimezone(timezone.utc)
    async with session_factory() as session:
        if not await repo.get_runtime_setting(session, "DAILY_QUEUE_REMINDER"):
            return
        queue = len(await workplan.call_queue(session))
        reminders = len(await repo.open_reminders_before(session, end))
    if not queue and not reminders:
        return
    pace = workplan.daily_pace(queue, today)
    text = f"📞 К обзвону: {queue}" + (f" (≈{pace} сегодня)" if pace else "") + f" · напоминаний: {reminders}"
    await _send(bot, text)


async def morning_job(bot: Bot) -> None:
    """Утро: в день плана (NEW_LEADS_PUSH_DOW) — план недели, в остальные — напоминание."""
    async with SessionLocal() as session:
        dow = str(await repo.get_runtime_setting(session, "NEW_LEADS_PUSH_DOW")).lower()[:3]
    today = utcnow().astimezone(get_settings().tz).date()
    if today.weekday() == DOW.get(dow, 0):
        await weekly_plan(bot)
    else:
        await daily_reminder(bot)


async def add_found_to_plan(bot: Bot, chat_id: int,
                            session_factory: async_sessionmaker[AsyncSession] = SessionLocal) -> int:
    """После ручного /run: добавить найденное в план текущей недели в пределах лимита."""
    async with session_factory() as session:
        limit = int(await repo.get_runtime_setting(session, "LEADS_WEEKLY_LIMIT"))
        quota = max(0, limit - await workplan.assigned_this_week(session))
        taken = await workplan.assign(session, quota)
        left_in_pool = len(await workplan.pool(session))
        queue = len(await workplan.call_queue(session))
        await session.commit()
    if not taken and not left_in_pool:
        return 0
    lines = [f"🆕 В план недели добавлено: {len(taken)} · к обзвону всего: {queue}"]
    if left_in_pool:
        reason = "лимит недели исчерпан" if quota <= len(taken) else "ниже порога не берём"
        lines.append(f"📦 В запасе: {left_in_pool} ({reason}) — в /new, придут в следующий план")
    await bot.send_message(chat_id, "\n".join(lines), reply_markup=today_button())
    return len(taken)
