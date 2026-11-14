"""План обзвона на неделю и очередь «Сегодня»: перенос необработанных, темп, лимит недели."""
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.config import Settings
from app.crm import service, workplan
from app.crm.statuses import Status
from app.db import repo
from app.discovery import jobs

TZ = ZoneInfo("Europe/Moscow")
MON = datetime(2026, 11, 16, 9, 30, tzinfo=TZ).astimezone(timezone.utc)
WED = MON + timedelta(days=2)
SAT = MON + timedelta(days=5)
NEXT_MON = MON + timedelta(days=7)


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append(text)


@pytest.fixture
async def env(session_factory, monkeypatch):
    clock = {"now": MON}
    monkeypatch.setattr(workplan, "utcnow", lambda: clock["now"])
    monkeypatch.setattr(jobs, "utcnow", lambda: clock["now"])
    monkeypatch.setattr(jobs, "get_settings", lambda: Settings(ADMIN_IDS="1"))
    async with session_factory() as s:
        niche = await repo.get_niche_by_slug(s, "gas")
        for i, score in enumerate([90, 80, 70, 40]):  # 40 — ниже порога
            await repo.create_company(s, domain=f"lead{i}.ru", url=f"lead{i}.ru", niche_id=niche.id,
                                      source="yandex", score=score, status="NEW")
        for k, v in {"SCORE_THRESHOLD": "55", "LEADS_WEEKLY_LIMIT": "2", "DAILY_QUEUE_REMINDER": "true",
                     "NEW_LEADS_PUSH_DOW": "mon"}.items():
            await repo.set_setting(s, k, v)
        await s.commit()
    return session_factory, clock


def test_pace():
    assert workplan.workdays_left(date(2026, 11, 16)) == 5   # пн
    assert workplan.workdays_left(date(2026, 11, 20)) == 1   # пт
    assert workplan.workdays_left(date(2026, 11, 21)) == 0   # сб
    assert workplan.daily_pace(38, date(2026, 11, 17)) == 10  # вт: 4 дня
    assert workplan.daily_pace(0, date(2026, 11, 17)) == 0
    assert workplan.daily_pace(5, date(2026, 11, 22)) is None


async def queue_domains(f):
    async with f() as s:
        return [c.domain for c in await workplan.call_queue(s)]


async def test_weekly_plan_and_carry_over(env):
    f, clock = env
    bot = FakeBot()
    assert await jobs.weekly_plan(bot, f) == 2
    text = bot.sent[0]
    assert "План на неделю" in text and "Новых в плане: 2" in text
    assert "В запасе ещё 1" in text and "Всего к обзвону: 2 · ≈1 в день" in text
    assert await queue_domains(f) == ["lead0.ru", "lead1.ru"]

    # обработали один лид — он уходит из очереди; «нужно созвониться» остаётся
    async with f() as s:
        c0 = await repo.get_company_by_domain(s, "lead0.ru")
        c1 = await repo.get_company_by_domain(s, "lead1.ru")
        await service.change_status(s, c0, Status.SENT)
        await service.change_status(s, c1, Status.CALL1_NEEDED)
        await s.commit()
    assert await queue_domains(f) == ["lead1.ru"]

    # следующий понедельник: необработанный переносится, из запаса берётся ещё 1 (выше порога остался один)
    clock["now"] = NEXT_MON
    bot2 = FakeBot()
    assert await jobs.weekly_plan(bot2, f) == 1
    assert "Не обработано с прошлых недель: 1" in bot2.sent[0]
    assert "Всего к обзвону: 2" in bot2.sent[0]
    assert await queue_domains(f) == ["lead1.ru", "lead2.ru"]  # давние — первыми


async def test_manual_run_respects_weekly_limit(env):
    f, clock = env
    bot = FakeBot()
    assert await jobs.add_found_to_plan(bot, 1, f) == 2
    assert "В план недели добавлено: 2" in bot.sent[0] and "лимит недели исчерпан" in bot.sent[0]
    # лимит недели уже выбран — повторный /run ничего не добавляет
    assert await jobs.add_found_to_plan(FakeBot(), 1, f) == 0
    async with f() as s:
        assert await workplan.assigned_this_week(s) == 2


async def test_manual_add_goes_to_queue(env):
    f, _ = env
    async with f() as s:
        await repo.create_company(s, domain="manual.ru", url="manual.ru", source="manual", status="NEW", score=10)
        await s.commit()
    assert "manual.ru" in await queue_domains(f)


async def test_daily_reminder(env):
    f, clock = env
    await jobs.weekly_plan(FakeBot(), f)
    clock["now"] = WED
    bot = FakeBot()
    await jobs.daily_reminder(bot, f)
    assert bot.sent == ["📞 К обзвону: 2 (≈1 сегодня) · напоминаний: 0"]
    clock["now"] = SAT
    weekend = FakeBot()
    await jobs.daily_reminder(weekend, f)
    assert weekend.sent == []
    clock["now"] = WED
    async with f() as s:
        await repo.set_setting(s, "DAILY_QUEUE_REMINDER", "false")
        await s.commit()
    off = FakeBot()
    await jobs.daily_reminder(off, f)
    assert off.sent == []


async def test_today_view(env, monkeypatch):
    f, clock = env
    from app.bot.handlers import leads

    monkeypatch.setattr(leads, "SessionLocal", f)
    monkeypatch.setattr(leads, "utcnow", lambda: clock["now"])
    await jobs.weekly_plan(FakeBot(), f)
    clock["now"] = NEXT_MON + timedelta(days=1)  # вторник следующей недели, лиды не обработаны
    text, kb = await leads.build_today()
    assert "К обзвону: 2 · ≈1 сегодня" in text
    assert "↪️ — перенесено с прошлой недели" in text
    assert "90 · lead0.ru · 🆕 с 16.11 ↪️" in text
    buttons = [b.text for row in kb.inline_keyboard for b in row]
    assert any("lead0.ru" in b for b in buttons)
