"""Прерванный прогон: сохранённые, но не проверенные сайты доделываются; аудит не держит в памяти всё сразу."""
import asyncio

import pytest

from app.audit import runner as audit_runner
from app.audit.browser import BrowserResult
from app.audit.fetcher import FetchResult
from app.config import Settings
from app.db import repo
from app.db.models import utcnow
from app.discovery import jobs

SITE = """<html><head><title>Газификация частного дома в СПб</title></head><body>
<h1>Газификация</h1><p>Санкт-Петербург, тел. 8 (812) 903-57-72</p><div>© 2015</div></body></html>"""


class FakeBot:
    def __init__(self):
        self.sent: list[str] = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append(text)


@pytest.fixture
async def env(session_factory, monkeypatch):
    async def fake_fetch(url, internal_pages=3):
        return FetchResult(requested_url=url, final_url=f"http://{url}/", status=200, html=SITE,
                           html_bytes=len(SITE), ttfb_ms=100)

    monkeypatch.setattr(audit_runner, "fetch_site", fake_fetch)
    monkeypatch.setattr(jobs, "get_settings", lambda: Settings(ADMIN_IDS="1", LLM_ENABLED=False))
    async with session_factory() as s:
        niche = await repo.get_niche_by_slug(s, "gas")
        niche.keywords, niche.region_markers = ["газ"], ["санкт-петербург"]
        for k, v in {"SCORE_THRESHOLD": "10", "LEADS_WEEKLY_LIMIT": "2", "LLM_ENABLED": "false"}.items():
            await repo.set_setting(s, k, v)
        for i in range(3):  # прогон сохранил выдачу и умер до аудита
            await repo.create_company(s, domain=f"lead{i}.ru", url=f"lead{i}.ru", niche_id=niche.id,
                                      source="yandex", status="NEW")
        done = await repo.create_company(s, domain="done.ru", url="done.ru", niche_id=niche.id, source="yandex",
                                         status="NEW", score=50)
        done.last_audit_at = utcnow()  # уже проверен — повторно не трогаем
        await s.commit()
    return session_factory


async def test_resume_audits_and_plans(env):
    bot = FakeBot()
    assert await jobs.resume_unprocessed(bot, env) == 3
    assert "Нашёл 3 сайтов без проверки" in bot.sent[0]
    final = bot.sent[-1]
    assert "Доделал прерванный поиск" in final and "Проверено сайтов: 3" in final
    assert "В план недели добавлено: 2" in final  # лимит недели — 2
    async with env() as s:
        for i in range(3):
            c = await repo.get_company_by_domain(s, f"lead{i}.ru")
            assert c.last_audit_at is not None and c.score is not None
    # повторный запуск — делать нечего, молчит
    quiet = FakeBot()
    assert await jobs.resume_unprocessed(quiet, env) == 0
    assert quiet.sent == []


async def test_resume_waits_for_running_discovery(env):
    async with jobs.discovery_lock:
        assert await jobs.resume_unprocessed(FakeBot(), env) == 0
    async with env() as s:
        assert (await repo.get_company_by_domain(s, "lead0.ru")).last_audit_at is None


async def test_audit_pipeline_is_bounded(session_factory, monkeypatch):
    """В памяти одновременно не больше AUDIT_CONCURRENCY сайтов — включая ожидание Chromium."""
    in_flight = peak = 0

    async def fake_fetch(url, internal_pages=3):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.01)
        return FetchResult(requested_url=url, final_url=f"http://{url}/", status=200, html=SITE,
                           html_bytes=len(SITE), ttfb_ms=100)

    async def slow_capture(url, company_id):
        nonlocal in_flight
        await asyncio.sleep(0.02)  # «Chromium» медленнее загрузки — раньше тут копилась очередь
        in_flight -= 1
        return BrowserResult()

    monkeypatch.setattr(audit_runner, "fetch_site", fake_fetch)
    monkeypatch.setattr(audit_runner, "capture", slow_capture)
    monkeypatch.setattr(audit_runner, "_semaphore", None)
    async with session_factory() as s:
        await repo.set_setting(s, "AUDIT_CONCURRENCY", "2")
        ids = [(await repo.create_company(s, domain=f"s{i}.ru", url=f"s{i}.ru")).id for i in range(10)]
        await s.commit()
    await asyncio.gather(*(audit_runner.audit_company(session_factory, cid) for cid in ids))
    assert peak <= 2
    monkeypatch.setattr(audit_runner, "_semaphore", None)  # не оставлять семафор на 2 другим тестам
