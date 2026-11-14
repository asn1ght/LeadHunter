"""Этап 4: Playwright (скриншоты, mobile_overflow, время загрузки), slow_load, PDF мини-аудит."""
import functools
import http.server
import re
import threading
from datetime import datetime
from types import SimpleNamespace

import pytest

from app.audit import browser as br
from app.audit.browser import BrowserResult
from app.audit.fetcher import FetchResult
from app.audit.pdf import build_costs, build_points, render_html
from app.audit.tech_checks import run_checks

NORMAL = """<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Газ</title></head>
<body style="margin:0"><h1>Газификация</h1><p>Нормальная страница</p></body></html>"""
WIDE = """<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Газ</title></head>
<body style="margin:0"><div style="width:900px;background:#eee">Таблица цен шириной 900px</div></body></html>"""

LLM = {
    "quality_score": 85, "summary": "Сайт устарел, цены противоречат друг другу, нет отзывов и лицензий. " * 2,
    "problems": [{"title": f"Проблема {i}", "why_it_costs_money": "Клиент уходит к конкуренту, потому что " * 3,
                  "severity": 3} for i in range(5)],
    "typos": ["Лениградской → Ленинградской"], "price_inconsistencies": ["74 000 ₽ / 120 000 ₽"],
    "trust_gaps": ["нет отзывов"], "segment": "b2c", "pitch_angle": "крючок", "is_good_site": False,
}


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    root = tmp_path_factory.mktemp("site")
    (root / "normal.html").write_text(NORMAL, encoding="utf-8")
    (root / "wide.html").write_text(WIDE, encoding="utf-8")
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
    handler.log_message = lambda *a: None
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture
def shots_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(br, "SCREENSHOT_DIR", tmp_path / "screenshots")
    monkeypatch.setattr(br, "DATA_DIR", tmp_path)
    return tmp_path


@pytest.fixture
async def fresh_pool(monkeypatch):
    pool = br.BrowserPool()
    monkeypatch.setattr(br, "pool", pool)
    yield pool
    await pool.close()


@pytest.mark.real_browser
async def test_capture_normal_and_wide(site, shots_dir, fresh_pool):
    ok = await br.capture(f"{site}/normal.html", 1)
    assert ok.error is None and ok.mobile_overflow is False and ok.mobile_scroll_width == 375
    assert ok.load_ms is not None and ok.load_ms < 4000
    assert (shots_dir / ok.screenshot_desktop).stat().st_size > 1000
    assert (shots_dir / ok.screenshot_mobile).read_bytes()[:3] == b"\xff\xd8\xff"  # JPEG

    wide = await br.capture(f"{site}/wide.html", 2)
    assert wide.mobile_overflow is True and wide.mobile_scroll_width >= 900
    fr = FetchResult(requested_url=site, final_url="https://x.ru/", status=200, html=WIDE,
                     html_bytes=len(WIDE), ttfb_ms=100, ssl_days_left=90)
    rep = run_checks(fr, datetime(2026, 11, 14), browser=wide)
    issue = next(i for i in rep.issues if i.code == "mobile_overflow")
    assert issue.severity == 3 and "900" in issue.title or "шире экрана" in issue.title
    assert rep.metrics["screenshot_mobile"] == wide.screenshot_mobile


async def test_unavailable_browser_does_not_break(shots_dir, monkeypatch):
    pool = br.BrowserPool()

    async def boom():
        raise RuntimeError("no chromium")

    monkeypatch.setattr(pool, "browser", boom)
    monkeypatch.setattr(br, "pool", pool)
    res = await br.capture("http://example.invalid", 3)
    assert res.error and "Chromium" in res.error
    assert (await br.capture("http://example.invalid", 3)).error == res.error  # больше не пытается


def test_slow_load_sources():
    fr = FetchResult(requested_url="https://g.ru", final_url="https://g.ru/", status=200, html=NORMAL,
                     html_bytes=len(NORMAL), ttfb_ms=2000, ssl_days_left=90)
    slow = run_checks(fr, browser=BrowserResult(load_ms=11700, mobile_overflow=False))
    i = next(i for i in slow.issues if i.code == "slow_load")
    assert i.title == "Загрузка 11.7 с (норма до 3)" and slow.metrics["load_ms"] == 11700
    # браузер быстрый — TTFB-проверка не дублирует
    fast = run_checks(fr, browser=BrowserResult(load_ms=1200))
    assert "slow_load" not in fast.codes()
    # без браузера — по TTFB
    assert "Медленный ответ сервера" in next(i.title for i in run_checks(fr).issues if i.code == "slow_load")


def test_pdf_content():
    audit = SimpleNamespace(
        issues=[{"code": "no_https", "severity": 3, "title": "Нет HTTPS"},
                {"code": "no_form", "severity": 2, "title": "Нет формы заявки"}],
        llm_summary=LLM, screenshot_desktop=None, screenshot_mobile=None,
    )
    pts = build_points(audit)
    assert len(pts) == 5 and pts[0].title.startswith("Опечатка")
    assert any("Цены расходятся" in p.title for p in pts)
    assert build_costs(audit)
    no_llm = SimpleNamespace(issues=audit.issues, llm_summary=None, screenshot_desktop=None, screenshot_mobile=None)
    assert [p.title for p in build_points(no_llm)] == ["Нет HTTPS", "Нет формы заявки"]
    assert "Не защищено" in build_points(no_llm)[0].why
    company = SimpleNamespace(name="Газ <Север>", domain="gazsever.com", url="https://gazsever.com", score=78)
    html = render_html(company, audit, datetime(2026, 11, 14))
    assert "Газ &lt;Север&gt;" in html  # экранирование
    assert "14.11.2026" in html and "78/100" in html and "Что это стоит компании" in html


@pytest.mark.real_browser
async def test_pdf_fits_one_page(site, shots_dir, fresh_pool):
    shots = await br.capture(f"{site}/normal.html", 4)
    audit = SimpleNamespace(
        issues=[{"code": c, "severity": 2, "title": f"Проблема {c}"} for c in ("slow_load", "no_form", "no_https")],
        llm_summary=LLM, screenshot_desktop=shots.screenshot_desktop, screenshot_mobile=shots.screenshot_mobile,
    )
    company = SimpleNamespace(name="Очень длинное название газовой компании Санкт-Петербурга", domain="gazsever.com",
                              url="https://gazsever.com", score=78)
    from app.audit.pdf import render_pdf

    pdf = await render_pdf(company, audit)
    assert pdf[:4] == b"%PDF"
    assert len(re.findall(rb"/Type\s*/Page[^s]", pdf)) == 1  # ровно одна страница A4
