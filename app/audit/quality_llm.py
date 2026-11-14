"""Слой качества через DeepSeek (раздел 7.4)."""
from __future__ import annotations

from typing import Literal

from loguru import logger
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.client import DeepSeekClient
from app.ai.prompts import QUALITY_SYSTEM, quality_user
from app.audit.runner import load_weights, niche_config
from app.audit.scoring import compute_score
from app.crm import service
from app.crm.statuses import Status
from app.db import repo


class Problem(BaseModel):
    title: str
    why_it_costs_money: str = ""
    severity: int = Field(default=2, ge=1, le=3)


class QualityResult(BaseModel):
    quality_score: int = Field(ge=0, le=100)
    summary: str = ""
    problems: list[Problem] = Field(default_factory=list)
    typos: list[str] = Field(default_factory=list)
    price_inconsistencies: list[str] = Field(default_factory=list)
    trust_gaps: list[str] = Field(default_factory=list)
    segment: Literal["b2b", "b2c", "mixed"] = "mixed"
    pitch_angle: str = ""
    is_good_site: bool = False

    @field_validator("segment", mode="before")
    @classmethod
    def _seg(cls, v: object) -> str:
        v = str(v or "").lower()
        return v if v in ("b2b", "b2c", "mixed") else "mixed"


GOOD_SITE_MAX_TECH = 25  # «тех. оценка низкая» для автоотмены good_site


async def analyze_company(
    session_factory: async_sessionmaker[AsyncSession], ai: DeepSeekClient, company_id: int
) -> QualityResult | None:
    """Запустить ИИ-анализ последнего аудита, обновить оценку. Может бросить LLMUnavailable."""
    async with session_factory() as session:
        company = await repo.get_company(session, company_id)
        audit = await repo.last_audit(session, company_id)
        if not company or not audit or not audit.raw_text_excerpt:
            return None
        niche_name = company.niche.name if company.niche else "—"
        user = quality_user(company.url, niche_name, audit.title, audit.description, audit.h1 or [],
                            audit.issues or [], audit.raw_text_excerpt)

    result = await ai.json("quality", QUALITY_SYSTEM, user, QualityResult)

    async with session_factory() as session:
        company = await repo.get_company(session, company_id)
        audit = await repo.last_audit(session, company_id)
        if not company or not audit:
            return result
        await apply_quality(session, company, audit, result)
        await session.commit()
    logger.info("quality {} → {} (good={})", company.domain, result.quality_score, result.is_good_site)
    return result


async def apply_quality(session: AsyncSession, company, audit, result: QualityResult) -> None:  # type: ignore[no-untyped-def]
    """Сохранить результат ИИ, пересчитать оценку, при хорошем сайте — отменить лида."""
    audit.llm_summary = result.model_dump()
    cfg = niche_config(company.niche)
    score = compute_score(
        audit.issues or [], await load_weights(session), inn=company.inn,
        segment=company.segment, priority_segment=cfg.get("priority_segment"),
        score_quality=result.quality_score,
    )
    audit.score_factors = score.factors
    company.score, company.score_tech, company.score_quality = score.score, score.score_tech, score.score_quality
    if company.segment == "unknown" and result.segment in ("b2b", "b2c"):
        company.segment = result.segment
    if result.is_good_site and (company.score_tech or 0) <= GOOD_SITE_MAX_TECH and company.status == Status.NEW.value:
        await service.change_status(session, company, Status.CANCELLED, payload={"reason": "good_site"})
