"""Прогон ниши: запросы → домены → фильтры → аудит → ИИ (разделы 6, 7)."""
from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.client import DeepSeekClient, LLMUnavailable, month_start
from app.audit.quality_llm import analyze_company
from app.audit.runner import audit_company
from app.config import get_settings
from app.crm import service
from app.crm.statuses import Status
from app.db import repo
from app.db.models import ApiUsage, Niche, SearchOperation, SearchQuery, utcnow
from app.discovery.filters import is_blacklisted, normalize_domain
from app.discovery.query_policy import (
    Policy,
    apply_autopause,
    is_paused,
    pages_for,
    record_run,
    stop_paging,
)
from app.discovery.yandex_search import SerpItem, YandexSearchClient, YandexSearchError

Notify = Callable[[str], Awaitable[None]]

PRICE_DAY_SYNC = 488  # ₽ за 1000 (aistudio.yandex.ru/docs/ru/search-api/pricing, ноябрь 2026)
PRICE_NIGHT_SYNC = 366
PRICE_DEFERRED = 30.5
OPERATION_TTL = timedelta(hours=12)


@dataclass
class RunStats:
    queries: int = 0
    requests: int = 0
    found_domains: int = 0
    new_companies: int = 0
    skipped: int = 0
    cancelled: dict[str, int] = field(default_factory=dict)
    audited: int = 0
    llm_analyzed: int = 0
    ops_created: int = 0
    pages_saved: int = 0
    skipped_paused: int = 0
    paused: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    stopped_reason: str | None = None

    def summary(self) -> str:
        lines = [
            f"🔎 Запросов к Яндексу: {self.requests} (фраз: {self.queries})",
            f"🌐 Доменов в выдаче: {self.found_domains}, новых компаний: {self.new_companies}, отсеяно: {self.skipped}",
        ]
        if self.ops_created:
            lines.append(f"⏳ Отложенных запросов создано: {self.ops_created} — результаты придут в течение нескольких часов")
        if self.audited:
            lines.append(f"🩺 Проверено сайтов: {self.audited}, ИИ-анализ: {self.llm_analyzed}")
        if self.cancelled:
            reasons = {"region": "не СПб/ЛО", "off_niche": "не по нише", "good_site": "хороший сайт"}
            lines.append("❌ Отменено: " + ", ".join(f"{reasons.get(k, k)} — {v}" for k, v in self.cancelled.items()))
        if self.pages_saved or self.skipped_paused:
            lines.append(f"💸 Сэкономлено запросов: {self.pages_saved} (страницы без новых), "
                         f"фраз на паузе пропущено: {self.skipped_paused}")
        if self.paused:
            lines.append(f"⏸ Поставлены на паузу ({len(self.paused)}): " + "; ".join(self.paused[:10]))
        if self.stopped_reason:
            lines.append(f"⛔ Остановлено: {self.stopped_reason}")
        if self.errors:
            lines.append(f"⚠️ Ошибок: {len(self.errors)} (первая: {self.errors[0][:200]})")
        return "\n".join(lines)


# ---------------------------------------------------------------- лимиты


def is_night(now_local: datetime) -> bool:
    """Ночное окно тарифа: 00:00–07:59 МСК."""
    return 0 <= now_local.hour < 8


def is_deferred_window(now_local: datetime) -> bool:
    """Отложенные запросы принимаются 00:00–06:00 МСК."""
    return 0 <= now_local.hour < 6


async def month_requests(session: AsyncSession) -> int:
    return int(
        (
            await session.execute(
                select(func.coalesce(func.sum(ApiUsage.requests), 0)).where(
                    ApiUsage.created_at >= month_start(), ApiUsage.api.like("yandex%")
                )
            )
        ).scalar_one()
    )


async def record_request(session: AsyncSession, api: str, paid: bool) -> None:
    session.add(ApiUsage(api=api, requests=1, paid=paid))
    await session.flush()


def estimate_cost(requests: int, mode: str, night: bool) -> float:
    price = PRICE_DEFERRED if mode == "deferred_v2" else PRICE_NIGHT_SYNC if night else PRICE_DAY_SYNC
    return requests * price / 1000


async def planned_requests(session: AsyncSession, niche_id: int) -> int:
    pages = await repo.get_runtime_setting(session, "YANDEX_PAGES_PER_QUERY")
    n = (
        await session.execute(
            select(func.count(SearchQuery.id)).where(SearchQuery.niche_id == niche_id, SearchQuery.is_active.is_(True))
        )
    ).scalar_one()
    return int(n) * int(pages)


async def load_policy(session: AsyncSession) -> Policy:
    g = lambda k: repo.get_runtime_setting(session, k)  # noqa: E731
    return Policy(
        full_depth_every=int(await g("QUERY_FULL_DEPTH_EVERY")),
        zero_runs_to_pause=int(await g("QUERY_ZERO_RUNS_TO_PAUSE")),
        pause_days=int(await g("QUERY_PAUSE_DAYS")),
        min_active=int(await g("QUERY_MIN_ACTIVE")),
    )


async def autopause_niche(session: AsyncSession, niche_id: int, policy: Policy, stats: RunStats) -> None:
    """Применить автопаузу к фразам ниши; названия поставленных на паузу — в stats.paused."""
    qs = list((await session.execute(select(SearchQuery).where(SearchQuery.niche_id == niche_id))).scalars())
    for q in apply_autopause(qs, utcnow(), policy):
        stats.paused.append(q.text)


# ---------------------------------------------------------------- приём выдачи


async def ingest(
    session: AsyncSession, niche: Niche, query_text: str, items: list[SerpItem], stats: RunStats,
    seen: set[str] | None = None,
) -> list[int]:
    """Отсеять домены (6.3, шаги 1–3) и создать новые компании. Возвращает их id."""
    db_black = await repo.blacklisted_domains(session)
    seen = seen if seen is not None else set()
    new_ids: list[int] = []
    for it in items:
        domain = normalize_domain(it.url)
        if not domain or domain in seen:
            continue
        seen.add(domain)
        stats.found_domains += 1
        if is_blacklisted(domain, db_black) or await repo.get_company_by_domain(session, domain):
            stats.skipped += 1
            continue
        c = await repo.create_company(
            session, domain=domain, url=it.url, niche_id=niche.id, source="yandex",
            source_query=query_text[:255], serp_position=it.position, status=Status.NEW.value,
        )
        new_ids.append(c.id)
        stats.new_companies += 1
    await session.flush()
    return new_ids


async def process_companies(
    session_factory: async_sessionmaker[AsyncSession],
    company_ids: list[int],
    stats: RunStats,
    ai: DeepSeekClient | None = None,
    notify: Notify | None = None,
) -> None:
    """Аудит новых компаний, отсев по региону/нише (6.3, шаги 4–5), ИИ-анализ (7.4)."""
    if not company_ids:
        return

    async def one(cid: int) -> None:
        try:
            await audit_company(session_factory, cid)
            stats.audited += 1
        except Exception as exc:  # noqa: BLE001
            stats.errors.append(f"audit {cid}: {exc!r}")

    await asyncio.gather(*(one(cid) for cid in company_ids))

    to_llm: list[int] = []
    async with session_factory() as session:
        min_tech = await repo.get_runtime_setting(session, "LLM_QUALITY_MIN_TECH")
        for cid in company_ids:
            c = await repo.get_company(session, cid)
            if not c or c.status != Status.NEW.value:
                continue
            tags = set(c.tags or [])
            reason = "region" if "off_region" in tags else "off_niche" if "off_niche" in tags else None
            if reason:
                await service.change_status(session, c, Status.CANCELLED, payload={"reason": reason})
                stats.cancelled[reason] = stats.cancelled.get(reason, 0) + 1
            elif "audit_failed" not in tags and (c.score_tech or 0) >= min_tech:
                to_llm.append(cid)
        await session.commit()

    if not ai:
        return
    for cid in to_llm:
        try:
            res = await analyze_company(session_factory, ai, cid)
            if res:
                stats.llm_analyzed += 1
                if res.is_good_site:
                    async with session_factory() as session:
                        c = await repo.get_company(session, cid)
                        if c and c.status == Status.CANCELLED.value:
                            stats.cancelled["good_site"] = stats.cancelled.get("good_site", 0) + 1
        except LLMUnavailable as exc:
            stats.errors.append(f"ИИ: {exc}")
            if notify and "Бюджет" in str(exc):
                await notify_budget_once(session_factory, notify, str(exc))
            break


async def notify_budget_once(session_factory: async_sessionmaker[AsyncSession], notify: Notify, text: str) -> None:
    """Уведомить об исчерпании бюджета ИИ не чаще раза в месяц."""
    key = utcnow().astimezone(get_settings().tz).strftime("%Y-%m")
    async with session_factory() as session:
        if await repo.get_setting_raw(session, "LLM_BUDGET_NOTIFIED") == key:
            return
        await repo.set_setting(session, "LLM_BUDGET_NOTIFIED", key)
        await session.commit()
    await notify(f"🧠 {text}. ИИ-функции отключены до конца месяца, бот работает с техническим аудитом.")


# ---------------------------------------------------------------- прогон


async def run_discovery(
    session_factory: async_sessionmaker[AsyncSession],
    search_client: YandexSearchClient,
    ai: DeepSeekClient | None = None,
    niche_slug: str | None = None,
    force: bool = False,
    notify: Notify | None = None,
) -> RunStats:
    """Прогнать все активные запросы ниши.

    force — запуск вручную (/run) с подтверждением: разрешает синхронный поиск вне ночного окна.
    Месячный лимит SEARCH_MONTHLY_LIMIT превышается только при ALLOW_PAID_SEARCH=true.
    """
    stats = RunStats()
    tz = get_settings().tz
    now_local = utcnow().astimezone(tz)
    async with session_factory() as session:
        niche = await repo.get_niche_by_slug(session, niche_slug) if niche_slug else await repo.get_active_niche(session)
        if not niche:
            stats.stopped_reason = "нет активной ниши"
            return stats
        mode = await repo.get_runtime_setting(session, "YANDEX_SEARCH_MODE")
        pages = int(await repo.get_runtime_setting(session, "YANDEX_PAGES_PER_QUERY"))
        limit = int(await repo.get_runtime_setting(session, "SEARCH_MONTHLY_LIMIT"))
        allow_paid = bool(await repo.get_runtime_setting(session, "ALLOW_PAID_SEARCH"))
        queries = list(
            (
                await session.execute(
                    select(SearchQuery).where(SearchQuery.niche_id == niche.id, SearchQuery.is_active.is_(True))
                )
            ).scalars()
        )
        used = await month_requests(session)
        policy = await load_policy(session)

    if mode == "deferred_v2" and not is_deferred_window(now_local):
        stats.stopped_reason = "отложенные запросы принимаются только 00:00–06:00 МСК"
        return stats
    if mode != "deferred_v2" and not is_night(now_local) and not force:
        stats.stopped_reason = "вне ночного окна 00:00–07:59 МСК (запустите /run с подтверждением)"
        return stats

    now = utcnow()
    run_tag = f"{now:%Y%m%d%H%M%S}-{uuid.uuid4().hex[:8]}"  # один прогон = один тег
    seen: set[str] = set()
    new_ids: list[int] = []
    for q in queries:
        if is_paused(q, now):
            stats.skipped_paused += 1
            continue
        stats.queries += 1
        q_found = q_new = 0
        q_pages = pages_for(q, pages, policy) if mode == "deferred_v2" else pages
        if q_pages < pages:
            stats.pages_saved += pages - q_pages
        completed = False
        for page in range(q_pages):
            if used >= limit and not allow_paid:
                stats.stopped_reason = f"достигнут месячный лимит {limit} запросов (ALLOW_PAID_SEARCH=false)"
                break
            try:
                if mode == "deferred_v2":
                    op_id = await search_client.start_async(q.text, page)
                    async with session_factory() as session:
                        session.add(SearchOperation(op_id=op_id, query_id=q.id, niche_id=niche.id,
                                                    query_text=q.text, page=page, run_tag=run_tag))
                        await record_request(session, "yandex_v2_async", paid=used >= limit)
                        await session.commit()
                    stats.ops_created += 1
                else:
                    items = await search_client.search(q.text, page)
                    async with session_factory() as session:
                        await record_request(session, "yandex_v2_sync", paid=used >= limit)
                        niche_obj = await session.get(Niche, niche.id)
                        assert niche_obj is not None
                        page_new = await ingest(session, niche_obj, q.text, items, stats, seen)
                        await session.commit()
                    new_ids += page_new
                    q_found += len(items)
                    q_new += len(page_new)
                used += 1
                stats.requests += 1
            except YandexSearchError as exc:
                logger.error("search '{}' p{} failed: {}", q.text, page, exc)
                stats.errors.append(f"«{q.text}»: {exc}")
                if "HTTP 401" in str(exc) or "HTTP 403" in str(exc):
                    stats.stopped_reason = "нет доступа к Search API (проверьте ключ и роль search-api.webSearch.user)"
                    break
                continue
            if mode != "deferred_v2":
                if len(items) < search_client.per_page:
                    completed = True
                    break  # выдача кончилась
                if page + 1 < q_pages and stop_paging(q, len(page_new), policy):
                    stats.pages_saved += q_pages - page - 1
                    completed = True
                    break  # страница без новых доменов — дальше не листаем
            await asyncio.sleep(1.1)  # квота 1 запрос/с
        else:
            completed = True
        if mode != "deferred_v2" and completed and not stats.stopped_reason:
            async with session_factory() as session:
                qq = await session.get(SearchQuery, q.id)
                if qq:
                    record_run(qq, q_found, q_new, utcnow())
                    await session.commit()
        if stats.stopped_reason:
            break

    if mode != "deferred_v2" and stats.requests:
        async with session_factory() as session:
            await autopause_niche(session, niche.id, policy, stats)
            await session.commit()

    await process_companies(session_factory, new_ids, stats, ai, notify)
    logger.info("discovery done: {}", stats.summary().replace("\n", " | "))
    return stats


async def poll_operations(
    session_factory: async_sessionmaker[AsyncSession],
    search_client: YandexSearchClient,
    ai: DeepSeekClient | None = None,
    notify: Notify | None = None,
) -> RunStats:
    """Забрать готовые отложенные запросы и обработать выдачу."""
    stats = RunStats()
    async with session_factory() as session:
        ops = list(
            (await session.execute(select(SearchOperation).where(SearchOperation.status == "pending"))).scalars()
        )
    if not ops:
        return stats
    seen: set[str] = set()
    new_ids: list[int] = []
    touched: set[tuple[str | None, int | None, int | None]] = set()
    for op in ops:
        try:
            items = await search_client.get_operation(op.op_id, op.page)
        except YandexSearchError as exc:
            items = None
            async with session_factory() as session:
                o = await session.get(SearchOperation, op.id)
                if o:
                    o.status, o.error, o.done_at = "failed", str(exc)[:1000], utcnow()
                    await session.commit()
            stats.errors.append(str(exc))
            continue
        async with session_factory() as session:
            o = await session.get(SearchOperation, op.id)
            if not o:
                continue
            if items is None:
                if utcnow() - o.created_at > OPERATION_TTL:
                    o.status, o.error, o.done_at = "failed", "expired", utcnow()
                    await session.commit()
                continue
            niche = await session.get(Niche, o.niche_id) if o.niche_id else await repo.get_active_niche(session)
            op_new: list[int] = []
            if niche:
                op_new = await ingest(session, niche, o.query_text, items, stats, seen)
            new_ids += op_new
            o.status, o.done_at = "done", utcnow()
            o.found_count, o.new_count = len(items), len(op_new)
            await session.commit()
        touched.add((op.run_tag, op.query_id, op.niche_id))
        await asyncio.sleep(0.15)  # квота 10 запросов/с на получение результатов

    # прогон фразы завершён, когда по её тегу не осталось ожидающих операций
    niches_to_check: set[int] = set()
    async with session_factory() as session:
        policy = await load_policy(session)
        for run_tag, query_id, niche_id in touched:
            if not query_id or not run_tag:
                continue
            group = list((await session.execute(select(SearchOperation).where(
                SearchOperation.run_tag == run_tag, SearchOperation.query_id == query_id))).scalars())
            if any(g.status == "pending" for g in group):
                continue
            q = await session.get(SearchQuery, query_id)
            if q:
                record_run(q, sum(g.found_count for g in group), sum(g.new_count for g in group), utcnow())
                if niche_id:
                    niches_to_check.add(niche_id)
        for niche_id in niches_to_check:
            await autopause_niche(session, niche_id, policy, stats)
        await session.commit()
    if stats.paused and notify:
        await notify("⏸ Фразы без новых сайтов поставлены на паузу: " + "; ".join(stats.paused[:10])
                     + "\nВключить обратно — в /queries.")

    await process_companies(session_factory, new_ids, stats, ai, notify)
    return stats
