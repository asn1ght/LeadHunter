"""Обёртка DeepSeek (OpenAI-совместимый API): учёт токенов, бюджет, ретраи (раздел 10.1)."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, TypeVar

from loguru import logger
from openai import APIError, AsyncOpenAI, BadRequestError
from pydantic import BaseModel, ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.db import repo
from app.db.models import LLMUsage, utcnow

T = TypeVar("T", bound=BaseModel)


class LLMUnavailable(Exception):
    """ИИ выключен, бюджет исчерпан или API недоступен. Текст — для пользователя."""


def month_start(now: datetime | None = None) -> datetime:
    now = (now or utcnow()).astimezone(get_settings().tz)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)


async def month_spent(session: AsyncSession) -> float:
    """Расход на ИИ с начала месяца, ₽."""
    total = (
        await session.execute(select(func.coalesce(func.sum(LLMUsage.cost_rub), 0)).where(LLMUsage.created_at >= month_start()))
    ).scalar_one()
    return float(total)


class DeepSeekClient:
    """Два вызова: JSON по схеме (анализ сайта) и текст (письмо)."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession], openai_client: Any | None = None) -> None:
        s = get_settings()
        self.session_factory = session_factory
        self.model = s.DEEPSEEK_MODEL
        self._client = openai_client or AsyncOpenAI(
            api_key=s.DEEPSEEK_API_KEY or "missing", base_url=s.DEEPSEEK_BASE_URL, timeout=90, max_retries=0
        )
        self._extra: dict[str, Any] | None = None if s.DEEPSEEK_THINKING else {"thinking": {"type": "disabled"}}

    async def ensure_available(self) -> None:
        """Проверить, что ИИ включён и бюджет не исчерпан."""
        async with self.session_factory() as session:
            if not await repo.get_runtime_setting(session, "LLM_ENABLED"):
                raise LLMUnavailable("ИИ-функции выключены в настройках")
            if not get_settings().DEEPSEEK_API_KEY:
                raise LLMUnavailable("Не задан DEEPSEEK_API_KEY")
            budget = await repo.get_runtime_setting(session, "LLM_MONTHLY_BUDGET_RUB")
            spent = await month_spent(session)
        if spent >= budget:
            raise LLMUnavailable(f"Бюджет на ИИ исчерпан: {spent:.0f} из {budget:.0f} ₽ в этом месяце")

    async def _record(self, purpose: str, usage: Any) -> float:
        pt = int(getattr(usage, "prompt_tokens", 0) or 0)
        ct = int(getattr(usage, "completion_tokens", 0) or 0)
        async with self.session_factory() as session:
            pin = await repo.get_runtime_setting(session, "LLM_PRICE_INPUT_PER_1M")
            pout = await repo.get_runtime_setting(session, "LLM_PRICE_OUTPUT_PER_1M")
            cost = (pt * pin + ct * pout) / 1_000_000
            session.add(LLMUsage(purpose=purpose, prompt_tokens=pt, completion_tokens=ct, cost_rub=cost))
            await session.commit()
        return cost

    async def _create(self, purpose: str, messages: list[dict[str, str]], **kw: Any) -> str:
        """Вызов с 2 ретраями и backoff. Пишет расход в llm_usage."""
        await self.ensure_available()
        last: Exception | None = None
        for attempt in range(3):
            try:
                params: dict[str, Any] = {"model": self.model, "messages": messages, **kw}
                if self._extra:
                    params["extra_body"] = self._extra
                try:
                    resp = await self._client.chat.completions.create(**params)
                except BadRequestError as exc:
                    if self._extra and "thinking" in str(exc).lower():
                        logger.warning("deepseek: thinking param rejected, retry without it")
                        self._extra = None
                        continue
                    raise
                if resp.usage is not None:
                    await self._record(purpose, resp.usage)
                return (resp.choices[0].message.content or "").strip()
            except BadRequestError as exc:
                logger.error("deepseek bad request: {}", exc)
                raise LLMUnavailable("DeepSeek отклонил запрос (проверьте модель в DEEPSEEK_MODEL)") from exc
            except (APIError, asyncio.TimeoutError) as exc:
                last = exc
                logger.warning("deepseek attempt {} failed: {}", attempt + 1, exc)
                await asyncio.sleep(2 ** attempt)
        raise LLMUnavailable(f"DeepSeek недоступен: {type(last).__name__}")

    async def json(self, purpose: str, system: str, user: str, schema: type[T], max_tokens: int = 1500) -> T:
        """Ответ строго в JSON по pydantic-схеме; при невалидном ответе — 1 повтор."""
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        for attempt in range(2):
            content = await self._create(
                purpose, messages, response_format={"type": "json_object"}, max_tokens=max_tokens, temperature=0.3
            )
            try:
                return schema.model_validate(json.loads(content))
            except (ValueError, ValidationError) as exc:
                logger.warning("deepseek: invalid json (attempt {}): {} / {}", attempt + 1, exc, content[:500])
                messages = [*messages, {"role": "assistant", "content": content or "(пусто)"},
                            {"role": "user", "content": "Ответ невалиден. Верни только корректный JSON по схеме."}]
        raise LLMUnavailable("DeepSeek вернул невалидный JSON")

    async def text(self, purpose: str, system: str, user: str, max_tokens: int = 700, temperature: float = 0.8) -> str:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        content = await self._create(purpose, messages, max_tokens=max_tokens, temperature=temperature)
        if not content:
            raise LLMUnavailable("DeepSeek вернул пустой ответ")
        return content


async def budget_exceeded(session: AsyncSession) -> tuple[bool, float, float]:
    """(превышен ли бюджет, потрачено, бюджет)."""
    budget = await repo.get_runtime_setting(session, "LLM_MONTHLY_BUDGET_RUB")
    spent = await month_spent(session)
    return spent >= budget, spent, budget
