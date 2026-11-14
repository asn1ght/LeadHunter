"""DeepSeek: учёт токенов и бюджета, JSON-ретрай, слой качества, черновик письма."""
import json
from types import SimpleNamespace

import pytest

from app.ai import client as ai_client
from app.ai.client import DeepSeekClient, LLMUnavailable, month_spent
from app.ai.prompts import LETTER_SYSTEM, letter_user
from app.audit.quality_llm import QualityResult, analyze_company
from app.config import Settings
from app.db import repo

QUALITY = {
    "quality_score": 80, "summary": "Сайт устарел.",
    "problems": [{"title": "Нет цен на монтаж", "why_it_costs_money": "клиент уходит", "severity": 3}],
    "typos": ["Лениградской → Ленинградской"], "price_inconsistencies": [], "trust_gaps": ["нет отзывов"],
    "segment": "b2c", "pitch_angle": "сайт теряет заявки", "is_good_site": False,
}


class FakeOpenAI:
    def __init__(self, contents, pt=1000, ct=500):
        self.contents = list(contents)
        self.params = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))
        self.pt, self.ct = pt, ct

    async def create(self, **params):
        self.params.append(params)
        content = self.contents.pop(0)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
                               usage=SimpleNamespace(prompt_tokens=self.pt, completion_tokens=self.ct))


@pytest.fixture
async def factory(session_factory, monkeypatch):
    monkeypatch.setattr(ai_client, "get_settings", lambda: Settings(DEEPSEEK_API_KEY="k", DEEPSEEK_MODEL="deepseek-flash"))
    async with session_factory() as s:
        for k, v in {"LLM_ENABLED": "true", "LLM_MONTHLY_BUDGET_RUB": "1000", "LLM_PRICE_INPUT_PER_1M": "30",
                     "LLM_PRICE_OUTPUT_PER_1M": "120"}.items():
            await repo.set_setting(s, k, v)
        await s.commit()
    return session_factory


async def test_json_and_usage(factory):
    fake = FakeOpenAI([json.dumps(QUALITY)])
    ai = DeepSeekClient(factory, fake)
    res = await ai.json("quality", "sys json", "user", QualityResult)
    assert res.quality_score == 80
    p = fake.params[0]
    assert p["model"] == "deepseek-flash" and p["response_format"] == {"type": "json_object"}
    assert p["extra_body"] == {"thinking": {"type": "disabled"}}
    async with factory() as s:
        assert await month_spent(s) == pytest.approx((1000 * 30 + 500 * 120) / 1e6)


async def test_json_retry_then_fail(factory):
    fake = FakeOpenAI(["   ", json.dumps(QUALITY)])
    res = await DeepSeekClient(factory, fake).json("quality", "s", "u", QualityResult)
    assert res.pitch_angle == "сайт теряет заявки" and len(fake.params) == 2
    with pytest.raises(LLMUnavailable, match="JSON"):
        await DeepSeekClient(factory, FakeOpenAI(["{}", "не json"])).json("quality", "s", "u", QualityResult)


async def test_budget_and_disabled(factory):
    async with factory() as s:
        await repo.set_setting(s, "LLM_MONTHLY_BUDGET_RUB", "0.05")
        await s.commit()
    ai = DeepSeekClient(factory, FakeOpenAI(["ok", "ok"], pt=1_000_000, ct=0))
    assert await ai.text("letter", "s", "u") == "ok"  # 30 ₽ — бюджет превышен после вызова
    with pytest.raises(LLMUnavailable, match="Бюджет"):
        await ai.text("letter", "s", "u")
    async with factory() as s:
        await repo.set_setting(s, "LLM_MONTHLY_BUDGET_RUB", "1000")
        await repo.set_setting(s, "LLM_ENABLED", "false")
        await s.commit()
    with pytest.raises(LLMUnavailable, match="выключены"):
        await DeepSeekClient(factory, FakeOpenAI(["x"])).text("letter", "s", "u")


async def _company_with_audit(s, tech_issues, text="Газификация частного дома в СПб"):
    niche = await repo.get_niche_by_slug(s, "gas")
    c = await repo.create_company(s, domain="gazsever.com", url="https://gazsever.com", niche_id=niche.id,
                                  inn="7802860317", segment="b2c", status="NEW")
    await repo.add_audit(s, company_id=c.id, issues=tech_issues, raw_text_excerpt=text, title="ГазСевер", h1=["Газ"])
    await s.commit()
    return c.id


async def test_analyze_updates_score(factory):
    async with factory() as s:
        cid = await _company_with_audit(s, [{"code": "no_metrika", "severity": 2, "title": "Нет Метрики"}] * 5)
    ai = DeepSeekClient(factory, FakeOpenAI([json.dumps(QUALITY)]))
    await analyze_company(factory, ai, cid)
    async with factory() as s:
        c = await repo.get_company(s, cid)
        a = await repo.last_audit(s, cid)
        assert c.score_quality == 80 and c.score_tech == 40
        assert c.score == round(0.5 * 40 + 0.5 * 80) + 5 + 5  # + ИНН + приоритетный сегмент b2c (из niches.yaml)
        assert a.llm_summary["typos"] == ["Лениградской → Ленинградской"]


async def test_good_site_cancelled(factory):
    async with factory() as s:
        cid = await _company_with_audit(s, [{"code": "title_bad", "severity": 1, "title": "x"}])
    good = {**QUALITY, "quality_score": 10, "is_good_site": True}
    await analyze_company(factory, DeepSeekClient(factory, FakeOpenAI([json.dumps(good)])), cid)
    async with factory() as s:
        c = await repo.get_company(s, cid)
        assert c.status == "CANCELLED"
        assert (await repo.company_events(s, cid))[-1].payload == {"reason": "good_site"}


def test_letter_prompt_contains_rules():
    assert "600 знаков" in LETTER_SYSTEM and "Гранит Прогаз" in LETTER_SYSTEM
    assert "Если неактуально — просто ответьте, больше не напишу" in LETTER_SYSTEM
    u = letter_user("email", "ГазСевер", "gazsever.com", ["Нет цен"], "крючок",
                    {"name": "Тимур", "phone": "+7", "telegram": "", "email": ""}, shorter=True, previous="старый")
    assert "Нет цен" in u and "Тимур" in u and "короче" in u


async def test_generate_letter_saves_event(factory, monkeypatch):
    from app.bot.handlers import ai_actions
    monkeypatch.setattr(ai_actions, "SessionLocal", factory)
    fake = FakeOpenAI(["Здравствуйте! Текст письма.\nЕсли неактуально — просто ответьте, больше не напишу"])
    monkeypatch.setattr(ai_actions, "ai_client", lambda: DeepSeekClient(factory, fake))
    async with factory() as s:
        cid = await _company_with_audit(s, [{"code": "no_https", "severity": 3, "title": "Нет HTTPS"}])
    eid, text = await ai_actions.generate_letter(cid, "email")
    assert "Здравствуйте" in text
    assert "Нет HTTPS" in fake.params[0]["messages"][1]["content"]
    async with factory() as s:
        ev = (await repo.company_events(s, cid))[-1]
        assert ev.id == eid and ev.type == "ai_draft" and ev.payload["channel"] == "email"


async def test_letter_too_long_is_shortened(factory, monkeypatch):
    from app.bot.handlers import ai_actions
    monkeypatch.setattr(ai_actions, "SessionLocal", factory)
    fake = FakeOpenAI(["x" * 700, "короткий вариант"])
    monkeypatch.setattr(ai_actions, "ai_client", lambda: DeepSeekClient(factory, fake))
    async with factory() as s:
        cid = await _company_with_audit(s, [{"code": "no_https", "severity": 3, "title": "Нет HTTPS"}])
    _, text = await ai_actions.generate_letter(cid, "email")
    assert text == "короткий вариант" and len(fake.params) == 2
    assert "короче" in fake.params[1]["messages"][1]["content"]
