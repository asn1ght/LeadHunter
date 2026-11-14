from datetime import datetime

from app.audit.fetcher import FetchResult
from app.audit.tech_checks import detect_cms, run_checks

OLD_SITE = """<html><head><title>Газ</title>
<script src="/js/jquery-1.8.3.min.js"></script></head>
<body><table><tr><td><table><tr><td>
<h1>Газификация</h1><h1>Ещё</h1>
<img src="/wp-content/uploads/2015/03/a.jpg"><img src="b.jpg"><img src="c.jpg" alt="ok">
<a href="#order">Заказать</a><a href="#order">Оставить заявку</a><a href="#">Получить консультацию</a>
<p>Тел. 8 (812) 123-45-67</p>
</td></tr></table></td></tr></table><table></table>
<div>© 2014 ООО «Газ»</div></body></html>"""

GOOD_SITE = """<html><head><title>Газификация частного дома в СПб под ключ | ГазСевер</title>
<meta name="viewport" content="width=device-width"><meta name="description" content="Газ">
<script>ym(123, "init")</script></head><body><main>
<h1>Газификация</h1><img src="a.jpg" alt="a"><a href="tel:+78121234567">Позвонить</a>
<a href="https://t.me/gas">Telegram</a>
<form><input name="phone" type="tel"><button>Отправить</button></form>
<footer>© 2026</footer></main></body></html>"""


def codes(html, url="http://old.ru/", **kw):
    fr = FetchResult(requested_url=url, final_url=url, status=200, html=html, html_bytes=len(html), ttfb_ms=300, **kw)
    return run_checks(fr, now=datetime(2026, 11, 13))


def test_old_site():
    rep = codes(OLD_SITE)
    expected = {
        "no_https", "no_viewport", "no_metrika", "title_bad", "no_description", "h1_missing_or_multi",
        "images_no_alt", "old_copyright", "old_uploads", "old_jquery", "no_tel_link", "no_messengers",
        "no_form", "anchor_ctas", "table_layout",
    }
    assert expected <= rep.codes()
    assert rep.metrics["cms"] == "WordPress"
    assert rep.metrics["jquery_version"] == "1.8.3"
    assert rep.metrics["copyright_year"] == 2014


def test_good_site():
    rep = codes(GOOD_SITE, url="https://good.ru/", ssl_days_left=90)
    assert rep.codes() == set()


def test_down_and_ssl():
    fr = FetchResult(requested_url="https://x.ru", error="timeout")
    assert run_checks(fr).codes() == {"site_down"}
    assert "ssl_expiring" in codes(GOOD_SITE, url="https://good.ru/", ssl_days_left=5).codes()
    assert "ssl_invalid" in codes(GOOD_SITE, url="https://good.ru/", ssl_invalid=True).codes()


def test_noindex_and_slow():
    html = GOOD_SITE.replace("<head>", '<head><meta name="robots" content="noindex, nofollow">')
    fr = FetchResult(
        requested_url="https://g.ru/", final_url="https://g.ru/", status=200, html=html,
        html_bytes=len(html), ttfb_ms=2500, ssl_days_left=90,
    )
    assert run_checks(fr, datetime(2026, 11, 13)).codes() == {"noindex", "slow_load"}


def test_cms():
    assert detect_cms('<script src="https://static.tildacdn.com/x.js">') == "Tilda"
    assert detect_cms('<link href="/bitrix/templates/a.css">') == "1C-Bitrix"
    assert detect_cms("<html></html>") == "самопис"
