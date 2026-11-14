"""Оркестрация аудита: загрузка → извлечение → проверки → оценка → запись в БД."""
from __future__ import annotations

import asyncio
from datetime import datetime

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.audit.extract import Extracted, extract
from app.audit.fetcher import FetchResult, fetch_site
from app.audit.scoring import ScoreResult, Weights, compute_score
from app.audit.tech_checks import TechReport, run_checks
from app.db import repo
from app.db.models import Audit, Company, utcnow

_semaphore: asyncio.Semaphore | None = None


def _sem(concurrency: int) -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(concurrency)
    return _semaphore


async def load_weights(session: AsyncSession) -> Weights:
    """Веса оценки с учётом перекрытий из таблицы settings."""
    g = lambda k: repo.get_runtime_setting(session, k)  # noqa: E731
    return Weights(
        sev3=await g("SCORE_W_SEV3"),
        sev2=await g("SCORE_W_SEV2"),
        sev1=await g("SCORE_W_SEV1"),
        bonus_inn=await g("SCORE_BONUS_INN"),
        bonus_segment=await g("SCORE_BONUS_SEGMENT"),
        bonus_hot=await g("SCORE_BONUS_HOT"),
    )


def niche_config(niche: object | None) -> dict:
    """Настройки ниши (ключевые слова, маркеры региона, приоритетный сегмент).

    Источник — поля таблицы niches; если они пусты — config/niches.yaml по slug.
    """
    if niche is None:
        return {}
    slug = niche if isinstance(niche, str) else getattr(niche, "slug", "")
    cfg = dict(repo.load_niches_config().get(slug, {}) or {})
    if not isinstance(niche, str):
        if getattr(niche, "keywords", None):
            cfg["keywords"] = niche.keywords
        if getattr(niche, "region_markers", None):
            cfg["region_markers"] = niche.region_markers
        if getattr(niche, "priority_segment", None):
            cfg["priority_segment"] = niche.priority_segment
    return cfg


async def audit_company(session_factory: async_sessionmaker[AsyncSession], company_id: int) -> Audit | None:
    """Провести технический аудит компании и обновить её поля и оценку.

    Ошибки не пробрасываются: при сбое компания помечается тегом `audit_failed`.
    """
    async with session_factory() as session:
        company = await repo.get_company(session, company_id)
        if not company:
            return None
        url = company.url
        cfg = niche_config(company.niche)
        concurrency = await repo.get_runtime_setting(session, "AUDIT_CONCURRENCY")

    try:
        async with _sem(concurrency):
            fetch = await fetch_site(url)
        ex = extract(
            [(fetch.final_url or url, fetch.html), *fetch.extra_pages] if fetch.html else [],
            region_markers=cfg.get("region_markers", []),
            niche_keywords=cfg.get("keywords", []),
        )
        tech = run_checks(fetch, now=datetime.now())
    except Exception as exc:  # noqa: BLE001
        logger.exception("audit failed for {}: {}", url, exc)
        async with session_factory() as session:
            company = await repo.get_company(session, company_id)
            if company:
                company.tags = sorted({*(company.tags or []), "audit_failed"})
                company.last_audit_at = utcnow()
                audit = await repo.add_audit(session, company_id=company_id, error=repr(exc)[:2000])
                await session.commit()
                return audit
        return None

    async with session_factory() as session:
        company = await repo.get_company(session, company_id)
        if not company:
            return None
        weights = await load_weights(session)
        score = compute_score(
            [i.to_dict() for i in tech.issues],
            weights,
            inn=ex.inn,
            segment=ex.segment,
            priority_segment=cfg.get("priority_segment"),
        )
        audit = await save_audit(session, company, fetch, ex, tech, score)
        await session.commit()
        logger.info("audit {} → score {} ({} issues)", company.domain, score.score, len(tech.issues))
        return audit


async def save_audit(
    session: AsyncSession,
    company: Company,
    fetch: FetchResult,
    ex: Extracted,
    tech: TechReport,
    score: ScoreResult,
) -> Audit:
    """Записать строку audits и обновить поля компании."""
    m = tech.metrics
    audit = await repo.add_audit(
        session,
        company_id=company.id,
        http_status=m.get("http_status"),
        final_url=m.get("final_url"),
        https=m.get("https"),
        ssl_days_left=m.get("ssl_days_left"),
        load_ms=m.get("load_ms"),
        ttfb_ms=m.get("ttfb_ms"),
        html_kb=m.get("html_kb"),
        scripts_count=m.get("scripts_count"),
        images_count=m.get("images_count"),
        images_no_alt=m.get("images_no_alt"),
        title=(m.get("title") or "")[:500] or None,
        title_len=m.get("title_len"),
        description=m.get("description"),
        h1=m.get("h1") or [],
        robots_meta=m.get("robots_meta"),
        viewport=m.get("viewport"),
        has_metrika=m.get("has_metrika"),
        has_gtag=m.get("has_gtag"),
        cms=m.get("cms"),
        jquery_version=m.get("jquery_version"),
        copyright_year=m.get("copyright_year"),
        oldest_upload_year=m.get("oldest_upload_year"),
        tel_links=m.get("tel_links"),
        mail_links=m.get("mail_links"),
        messengers=m.get("messengers") or [],
        forms_count=m.get("forms_count"),
        form_fields=m.get("form_fields") or [],
        anchor_only_ctas=m.get("anchor_only_ctas"),
        issues=[i.to_dict() for i in tech.issues],
        score_factors=score.factors,
        raw_text_excerpt=ex.text or None,
        error=fetch.error,
    )
    if ex.name and not company.name:
        company.name = ex.name[:255]
    if ex.inn:
        company.inn = ex.inn
    company.phones = list(dict.fromkeys([*(company.phones or []), *ex.phones]))[:5]
    company.emails = list(dict.fromkeys([*(company.emails or []), *ex.emails]))[:5]
    company.messengers = ex.messengers or company.messengers or []
    if ex.address:
        company.address = ex.address
    company.region_detected = ex.region_detected
    if ex.segment != "unknown" or not company.segment:
        company.segment = ex.segment
    if fetch.final_url:
        company.url = fetch.final_url
    tags = set(company.tags or []) - {"audit_failed", "off_region", "off_niche", "robots_disallowed"}
    if ex.text and not ex.region_detected:
        tags.add("off_region")
    if ex.niche_match is False:
        tags.add("off_niche")
    if fetch.robots_disallowed:
        tags.add("robots_disallowed")
    company.tags = sorted(tags)
    company.score = score.score
    company.score_tech = score.score_tech
    company.score_quality = score.score_quality
    company.last_audit_at = utcnow()
    await session.flush()
    return audit
