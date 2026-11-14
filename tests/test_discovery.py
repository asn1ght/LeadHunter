"""Прогон поиска: выдача → фильтры → аудит → отсев; лимиты; отложенные запросы; утренняя рассылка."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from app.audit import runner as audit_runner
from app.audit.fetcher import FetchResult
from app.db import repo
from app.db.models import ApiUsage, Company, SearchOperation, SearchQuery
from app.discovery import jobs
from app.discovery import runner as disc
from app.discovery.yandex_search import SerpItem, YandexNetworkError, YandexSearchError

TZ = ZoneInfo("Europe/Moscow")
NIGHT = datetime(2026, 11, 16, 2, 0, tzinfo=TZ).astimezone(timezone.utc)
DAY = datetime(2026, 11, 16, 14, 0, tzinfo=TZ).astimezone(timezone.utc)

GOOD_LEAD = """<html><head><title>Газ</title></head><body><h1>Газификация частного дома</h1>
<p>Санкт-Петербург, тел. 8 (812) 903-57-72</p><div>© 2015</div></body></html>"""
OTHER_REGION = """<html><body><h1>Газификация в Москве</h1><p>Москва, тел. 8 (495) 123-45-67</p></body></html>"""
OFF_NICHE = """<html><body><h1>Ремонт квартир</h1><p>Санкт-Петербург</p></body></html>"""
PAGES = {"lead1.ru": GOOD_LEAD, "lead2.ru": GOOD_LEAD, "moscow-gaz.ru": OTHER_REGION, "remont.ru": OFF_NICHE}


class FakeSearch:
    per_page = 10

    def __init__(self, fail=None):
        self.calls = []
        self.fail = fail
        self.ops = {}

    def serp(self, query, page):
        urls = ["https://lead1.ru/", "https://www.lead1.ru/contacts", "https://spb.avito.ru/x",
                "https://moscow-gaz.ru/", "https://remont.ru/", "https://existing.ru/", "https://lead2.ru/"]
        return [SerpItem(url=u, domain="", title="", position=i + 1) for i, u in enumerate(urls)] if page == 0 else []

    async def search(self, query, page=0):
        self.calls.append((query, page))
        if self.fail:
            raise YandexSearchError(self.fail)
        return self.serp(query, page)

    async def start_async(self, query, page=0):
        op = f"op{len(self.ops)}"
        self.ops[op] = (query, page)
        return op

    async def get_operation(self, op_id, page=0):
        q, p = self.ops[op_id]
        return self.serp(q, p)


@pytest.fixture
async def env(session_factory, monkeypatch):
    async def fake_fetch(url, internal_pages=2):
        dom = url.split("//")[-1].split("/")[0].removeprefix("www.")
        html = PAGES.get(dom, GOOD_LEAD)
        return FetchResult(requested_url=url, final_url=f"http://{dom}/", status=200, html=html,
                           html_bytes=len(html), ttfb_ms=200)

    monkeypatch.setattr(audit_runner, "fetch_site", fake_fetch)
    monkeypatch.setattr(disc.asyncio, "sleep", _nosleep)
    async with session_factory() as s:
        niche = await repo.get_niche_by_slug(s, "gas")
        niche.keywords = ["газ", "газификац"]
        niche.region_markers = ["санкт-петербург", "спб"]
        niche.priority_segment = "b2c"
        s.add_all([SearchQuery(niche_id=niche.id, text="газификация спб"),
                   SearchQuery(niche_id=niche.id, text="газгольдер спб"),
                   SearchQuery(niche_id=niche.id, text="выключенный", is_active=False)])
        await repo.create_company(s, domain="existing.ru", url="existing.ru", niche_id=niche.id)
        for k, v in {"YANDEX_SEARCH_MODE": "night_v1", "YANDEX_PAGES_PER_QUERY": "2", "SEARCH_MONTHLY_LIMIT": "100",
                     "ALLOW_PAID_SEARCH": "false", "SCORE_THRESHOLD": "10", "LEADS_DAILY_LIMIT": "1",
                     "LLM_QUALITY_MIN_TECH": "20", "ACTIVE_NICHE": "gas"}.items():
            await repo.set_setting(s, k, v)
        await s.commit()
    return session_factory


async def _nosleep(*a, **kw):
    return None


async def test_night_run_filters(env, monkeypatch):
    monkeypatch.setattr(disc, "utcnow", lambda: NIGHT)
    search = FakeSearch()
    stats = await disc.run_discovery(env, search)
    # 2 активных запроса; выдача на 2-й странице пустая → короткая страница → 1 запрос на фразу
    assert search.calls == [("газификация спб", 0), ("газгольдер спб", 0)]
    assert stats.requests == 2
    assert stats.new_companies == 4  # lead1, moscow-gaz, remont, lead2 (avito и existing отсеяны)
    assert stats.cancelled == {"region": 1, "off_niche": 1}
    async with env() as s:
        rows = {c.domain: c for c in (await s.execute(select(Company))).scalars()}
        assert rows["lead1.ru"].status == "NEW" and rows["lead1.ru"].source == "yandex"
        assert rows["lead1.ru"].source_query == "газификация спб" and rows["lead1.ru"].serp_position == 1
        assert rows["moscow-gaz.ru"].status == "CANCELLED"
        assert rows["remont.ru"].status == "CANCELLED"
        assert "avito.ru" not in rows
        events = await repo.company_events(s, rows["moscow-gaz.ru"].id)
        assert events[-1].payload == {"reason": "region"}
        assert len((await s.execute(select(ApiUsage))).scalars().all()) == 2

    # повторный прогон не дублирует компании
    stats2 = await disc.run_discovery(env, FakeSearch())
    assert stats2.new_companies == 0


async def test_day_requires_force(env, monkeypatch):
    monkeypatch.setattr(disc, "utcnow", lambda: DAY)
    search = FakeSearch()
    stats = await disc.run_discovery(env, search)
    assert search.calls == [] and "ночного окна" in stats.stopped_reason
    stats = await disc.run_discovery(env, search, force=True)
    assert stats.requests == 2


async def test_monthly_limit(env, monkeypatch):
    monkeypatch.setattr(disc, "utcnow", lambda: NIGHT)
    async with env() as s:
        await repo.set_setting(s, "SEARCH_MONTHLY_LIMIT", "1")
        await s.commit()
    search = FakeSearch()
    stats = await disc.run_discovery(env, search)
    assert len(search.calls) == 1
    assert "лимит" in stats.stopped_reason
    async with env() as s:
        await repo.set_setting(s, "ALLOW_PAID_SEARCH", "true")
        await s.commit()
    search = FakeSearch()
    await disc.run_discovery(env, search)
    assert len(search.calls) == 2


async def test_access_denied_stops(env, monkeypatch):
    monkeypatch.setattr(disc, "utcnow", lambda: NIGHT)
    search = FakeSearch(fail="HTTP 403: Permission denied")
    stats = await disc.run_discovery(env, search)
    assert len(search.calls) == 1
    assert "search-api.webSearch.user" in stats.stopped_reason


async def test_deferred_mode(env, monkeypatch):
    monkeypatch.setattr(disc, "utcnow", lambda: NIGHT)
    async with env() as s:
        await repo.set_setting(s, "YANDEX_SEARCH_MODE", "deferred_v2")
        await s.commit()
    search = FakeSearch()
    stats = await disc.run_discovery(env, search)
    assert stats.ops_created == 4 and stats.new_companies == 0  # 2 фразы × 2 страницы
    stats = await disc.poll_operations(env, search)
    assert stats.new_companies == 4
    async with env() as s:
        ops = (await s.execute(select(SearchOperation))).scalars().all()
        assert {o.status for o in ops} == {"done"}
    # вне окна 00–06 отложенные не создаются
    monkeypatch.setattr(disc, "utcnow", lambda: DAY)
    assert "00:00–06:00" in (await disc.run_discovery(env, FakeSearch())).stopped_reason


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append(text)


async def test_network_error_stops_run(env, monkeypatch):
    monkeypatch.setattr(disc, "utcnow", lambda: NIGHT)

    class NetDown(FakeSearch):
        async def search(self, query, page=0):
            self.calls.append((query, page))
            raise YandexNetworkError("сеть: таймаут")

    search = NetDown()
    stats = await disc.run_discovery(env, search)
    assert len(search.calls) == 1  # не перебираем все фразы впустую
    assert "нет связи с Яндексом" in stats.stopped_reason


async def test_poll_network_error_keeps_operation(env, monkeypatch):
    monkeypatch.setattr(disc, "utcnow", lambda: NIGHT)
    async with env() as s:
        await repo.set_setting(s, "YANDEX_SEARCH_MODE", "deferred_v2")
        await s.commit()
    search = FakeSearch()
    await disc.run_discovery(env, search)

    async def net_down(op_id, page=0):
        raise YandexNetworkError("сеть: ConnectError")

    monkeypatch.setattr(search, "get_operation", net_down)
    stats = await disc.poll_operations(env, search)
    assert stats.errors
    async with env() as s:
        ops = (await s.execute(select(SearchOperation))).scalars().all()
        assert {o.status for o in ops} == {"pending"}  # оплаченные операции заберём при следующем опросе
