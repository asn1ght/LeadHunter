"""Экономия запросов: глубина листания по новизне, статистика фраз, мягкая автопауза."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from app.audit import runner as audit_runner
from app.audit.fetcher import FetchResult
from app.db import repo
from app.db.models import SearchOperation, SearchQuery
from app.discovery import runner as disc
from app.discovery.query_policy import (
    Policy,
    apply_autopause,
    is_paused,
    pages_for,
    record_run,
    resume,
    stop_paging,
)
from app.discovery.yandex_search import SerpItem

NOW = datetime(2026, 11, 16, 2, 0, tzinfo=ZoneInfo("Europe/Moscow")).astimezone(timezone.utc)
P = Policy(full_depth_every=4, zero_runs_to_pause=2, pause_days=28, min_active=3)


def q(id_, **kw):
    base = dict(id=id_, text=f"q{id_}", is_active=True, runs_count=0, last_new=None, total_new=0,
                zero_streak=0, paused_until=None, results_count=0)
    base.update(kw)
    return SearchQuery(**base)


# ---------------------------------------------------------------- чистые правила


def test_depth_rules():
    assert pages_for(q(1), 3, P) == 3                              # первый прогон — полная глубина
    assert pages_for(q(1, runs_count=1, last_new=5), 3, P) == 3    # прошлый раз были новые
    assert pages_for(q(1, runs_count=1, last_new=0), 3, P) == 1    # новых не было — только 1-я страница
    assert pages_for(q(1, runs_count=4, last_new=0), 3, P) == 3    # каждый 4-й — полная глубина
    assert stop_paging(q(1, runs_count=2), 0, P)
    assert not stop_paging(q(1, runs_count=2), 3, P)
    assert not stop_paging(q(1, runs_count=0), 0, P)
    assert not stop_paging(q(1, runs_count=8), 0, P)


def test_record_run():
    x = q(1)
    record_run(x, found=30, new=4, now=NOW)
    record_run(x, found=28, new=0, now=NOW)
    assert (x.runs_count, x.results_count, x.last_new, x.total_new, x.zero_streak) == (2, 28, 0, 4, 1)
    record_run(x, found=29, new=1, now=NOW)
    assert x.zero_streak == 0 and x.total_new == 5


def test_autopause_soft():
    qs = [q(i, zero_streak=2 if i <= 3 else 0, total_new=i) for i in range(1, 6)]  # 5 работают, 3 «пустые»
    paused = apply_autopause(qs, NOW, P)
    # min_active=3 → можно поставить на паузу только 2 из 3 кандидатов, сначала с меньшим вкладом
    assert [x.id for x in paused] == [1, 2]
    assert all(is_paused(x, NOW) for x in paused) and not is_paused(qs[2], NOW)
    assert paused[0].paused_until == NOW + timedelta(days=28)
    assert paused[0].zero_streak == 0
    # пауза временная
    assert not is_paused(paused[0], NOW + timedelta(days=29))
    resume(paused[1])
    assert not is_paused(paused[1], NOW)


def test_autopause_ignores_manual_off_and_short_streak():
    qs = [q(1, zero_streak=1), q(2, is_active=False, zero_streak=5), *[q(i) for i in range(3, 7)]]
    assert apply_autopause(qs, NOW, P) == []
    assert apply_autopause([q(1, zero_streak=9)], NOW, Policy(zero_runs_to_pause=0)) == []


# ---------------------------------------------------------------- прогон


class PagedSearch:
    """Выдача по 10 результатов на страницу; домены зависят от фразы и страницы."""

    per_page = 10

    def __init__(self):
        self.calls = []
        self.ops = {}

    def serp(self, query, page):
        return [SerpItem(url=f"https://{query.split()[0]}-p{page}-{i}.ru/", domain="", title="", position=i)
                for i in range(10)]

    async def search(self, query, page=0):
        self.calls.append((query, page))
        return self.serp(query, page)

    async def start_async(self, query, page=0):
        op = f"op{len(self.ops)}"
        self.ops[op] = (query, page)
        self.calls.append((query, page))
        return op

    async def get_operation(self, op_id, page=0):
        return self.serp(*self.ops[op_id])


@pytest.fixture
async def env(session_factory, monkeypatch):
    html = "<html><body><h1>Газификация</h1><p>Санкт-Петербург, тел. 8 (812) 903-57-72</p></body></html>"

    async def fake_fetch(url, internal_pages=2):
        return FetchResult(requested_url=url, final_url=url, status=200, html=html, html_bytes=len(html), ttfb_ms=100)

    async def nosleep(*a, **kw):
        return None

    monkeypatch.setattr(audit_runner, "fetch_site", fake_fetch)
    monkeypatch.setattr(disc.asyncio, "sleep", nosleep)
    monkeypatch.setattr(disc, "utcnow", lambda: NOW)
    monkeypatch.setattr("app.discovery.query_policy.datetime", datetime, raising=False)
    async with session_factory() as s:
        niche = await repo.get_niche_by_slug(s, "gas")
        niche.keywords, niche.region_markers = ["газ"], ["санкт-петербург"]
        s.add_all([SearchQuery(niche_id=niche.id, text="альфа газ"), SearchQuery(niche_id=niche.id, text="бета газ")])
        for k, v in {"YANDEX_SEARCH_MODE": "night_v1", "YANDEX_PAGES_PER_QUERY": "3", "SEARCH_MONTHLY_LIMIT": "1000",
                     "LLM_QUALITY_MIN_TECH": "999", "QUERY_MIN_ACTIVE": "1", "ACTIVE_NICHE": "gas"}.items():
            await repo.set_setting(s, k, v)
        await s.commit()
    return session_factory


async def queries(f):
    async with f() as s:
        return {x.text: x for x in (await s.execute(select(SearchQuery))).scalars()}


async def test_sync_paging_and_autopause(env):
    # прогон 1: первый — полная глубина, все домены новые
    s1 = PagedSearch()
    st = await disc.run_discovery(env, s1)
    assert len(s1.calls) == 6 and st.new_companies == 60
    qs = await queries(env)
    assert (qs["альфа газ"].results_count, qs["альфа газ"].last_new, qs["альфа газ"].total_new) == (30, 30, 30)

    # прогон 2: всё уже известно → 1-я страница без новых, листание остановлено
    s2 = PagedSearch()
    st = await disc.run_discovery(env, s2)
    assert s2.calls == [("альфа газ", 0), ("бета газ", 0)]
    assert st.pages_saved == 4 and st.new_companies == 0
    qs = await queries(env)
    assert qs["альфа газ"].zero_streak == 1 and qs["альфа газ"].last_new == 0

    # прогон 3: второй пустой подряд → автопауза, но не ниже QUERY_MIN_ACTIVE=1
    st = await disc.run_discovery(env, PagedSearch())
    assert len(st.paused) == 1
    qs = await queries(env)
    paused = [x for x in qs.values() if is_paused(x, NOW)]
    assert len(paused) == 1 and paused[0].paused_until == NOW + timedelta(days=28)

    # прогон 4: фраза на паузе пропускается, вторая — снова только 1-я страница
    working = next(x.text for x in qs.values() if not is_paused(x, NOW))
    s4 = PagedSearch()
    st = await disc.run_discovery(env, s4)
    assert st.skipped_paused == 1 and s4.calls == [(working, 0)]
    assert "Сэкономлено запросов" in st.summary() and "фраз на паузе пропущено: 1" in st.summary()

    # прогон 5 (раз в 4 прогона): полная глубина, даже если новых давно нет
    s5 = PagedSearch()
    await disc.run_discovery(env, s5)
    assert s5.calls == [(working, 0), (working, 1), (working, 2)]


async def test_sync_new_on_first_page_keeps_paging(env):
    await disc.run_discovery(env, PagedSearch())

    class Fresh(PagedSearch):
        def serp(self, query, page):
            return [SerpItem(url=f"https://new-{query.split()[0]}-{page}-{i}.ru/", domain="", title="", position=i)
                    for i in range(10)]

    s = Fresh()
    await disc.run_discovery(env, s)
    assert len(s.calls) == 6  # на каждой странице новые домены → листаем дальше


async def test_deferred_stats_and_depth(env):
    async with env() as s:
        await repo.set_setting(s, "YANDEX_SEARCH_MODE", "deferred_v2")
        await s.commit()
    search = PagedSearch()
    st = await disc.run_discovery(env, search)
    assert st.ops_created == 6
    await disc.poll_operations(env, search)
    qs = await queries(env)
    assert (qs["альфа газ"].runs_count, qs["альфа газ"].last_new, qs["альфа газ"].results_count) == (1, 30, 30)

    # прогон 2: в прошлый раз были новые → снова 3 страницы; всё уже известно → last_new = 0
    st = await disc.run_discovery(env, search)
    assert st.ops_created == 6
    await disc.poll_operations(env, search)
    qs = await queries(env)
    assert qs["альфа газ"].last_new == 0 and qs["альфа газ"].zero_streak == 1

    # прогон 3: новых не было → только 1-я страница
    st = await disc.run_discovery(env, search)
    assert st.ops_created == 2 and st.pages_saved == 4
    notes = []

    async def notify(t):
        notes.append(t)

    await disc.poll_operations(env, search, notify=notify)
    async with env() as s:
        ops = (await s.execute(select(SearchOperation))).scalars().all()
        assert all(o.status == "done" for o in ops)
    assert notes and notes[0].startswith("⏸ Фразы без новых сайтов поставлены на паузу")
