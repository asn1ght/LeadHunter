"""Задачи планировщика этапа 3: ночной поиск, опрос отложенных запросов, утренняя рассылка (8.7)."""
from __future__ import annotations

from aiogram import Bot
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.client import DeepSeekClient
from app.bot.common import send_card
from app.config import get_settings
from app.crm.statuses import Status
from app.db import repo
from app.db.models import Company, utcnow
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


async def discovery_job(bot: Bot, force: bool = False, niche_slug: str | None = None) -> RunStats:
    """Прогон поиска (по расписанию или из /run)."""
    try:
        client = make_search_client()
    except YandexSearchError as exc:
        stats = RunStats(stopped_reason=str(exc))
        return stats
    try:
        return await run_discovery(
            SessionLocal, client, DeepSeekClient(SessionLocal), niche_slug=niche_slug, force=force,
            notify=lambda t: notify_admins(bot, t),
        )
    finally:
        await client.aclose()


async def scheduled_discovery(bot: Bot) -> None:
    stats = await discovery_job(bot)
    if stats.errors or stats.stopped_reason:
        await notify_admins(bot, "🌙 Ночной поиск\n" + stats.summary())


async def poll_job(bot: Bot) -> None:
    try:
        client = make_search_client()
    except YandexSearchError:
        return
    try:
        stats = await poll_operations(SessionLocal, client, DeepSeekClient(SessionLocal),
                                      notify=lambda t: notify_admins(bot, t))
        if stats.new_companies:
            logger.info("poll: {}", stats.summary().replace("\n", " | "))
    except Exception as exc:  # noqa: BLE001
        logger.exception("poll_job failed: {}", exc)
    finally:
        await client.aclose()


async def pending_new_leads(session: AsyncSession) -> list[Company]:
    """Найденные поиском и ещё не присланные лиды выше порога, по убыванию оценки."""
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


async def morning_push(bot: Bot, session_factory: async_sessionmaker[AsyncSession] = SessionLocal) -> int:
    """Сводка «Найдено за ночь: N, отправлю топ-K» и карточки. Возвращает число отправленных."""
    async with session_factory() as session:
        leads = await pending_new_leads(session)
        limit = int(await repo.get_runtime_setting(session, "LEADS_DAILY_LIMIT"))
    if not leads:
        return 0
    top = leads[:limit]
    rest = len(leads) - len(top)
    head = f"☀️ Найдено новых лидов: {len(leads)}, отправляю топ-{len(top)}."
    if rest:
        head += f" Остальные {rest} — в /new."
    await notify_admins(bot, head)
    sent_ids: list[int] = []
    for c in top:
        for admin in get_settings().admin_ids:
            try:
                await send_card(bot, admin, c.id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("push card {} failed: {}", c.id, exc)
        sent_ids.append(c.id)
    async with session_factory() as session:
        for cid in sent_ids:
            c = await repo.get_company(session, cid)
            if c:
                c.notified_at = utcnow()
        await session.commit()
    return len(sent_ids)
