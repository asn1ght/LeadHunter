"""Технические проверки сайта (раздел 7.2): httpx-слой + браузерный слой Playwright (если есть)."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from app.audit.extract import detect_messengers
from app.audit.fetcher import FetchResult

if TYPE_CHECKING:
    from app.audit.browser import BrowserResult


@dataclass
class Issue:
    """Найденная проблема сайта."""

    code: str
    severity: int  # 1..3
    title: str
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TechReport:
    """Результат технического аудита: метрики для таблицы audits + список проблем."""

    issues: list[Issue] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    def codes(self) -> set[str]:
        return {i.code for i in self.issues}


CMS_MARKERS: list[tuple[str, tuple[str, ...]]] = [
    ("WordPress", ("/wp-content/", "/wp-includes/", 'name="generator" content="wordpress')),
    ("1C-Bitrix", ("/bitrix/", "bx.setcsslist", "bitrix_sessid")),
    ("Tilda", ("tilda.ws", "tildacdn", "data-tilda")),
    ("Joomla", ("/media/jui/", "/components/com_", 'content="joomla')),
    ("MODX", ("modx", "/assets/components/")),
    ("Wix", ("wix.com", "wixstatic.com", "_wixcssimports")),
    ("Nethouse", ("nethouse.ru", "nethouse")),
    ("OpenCart", ("catalog/view/theme", "route=common")),
    ("Drupal", ("drupal.settings", "/sites/default/files/")),
    ("UMI.CMS", ("umi.cms", "/umi/")),
]

SLOW_LOAD_MS = 4000  # Playwright: событие load дольше 4 с
CTA_RE = re.compile(r"заказ|заявк|консульт|рассчит|расчет|расчёт|звонок|оставить|получить|связат|узнать", re.I)
PHONE_FIELD_RE = re.compile(r"phone|tel|телефон|email|e-mail|почт|mail", re.I)


def detect_cms(html: str) -> str:
    low = html.lower()
    for name, markers in CMS_MARKERS:
        if any(m in low for m in markers):
            return name
    return "самопис"


def _jquery_version(html: str) -> str | None:
    m = re.search(r"jquery[.\-/]?(?:v|min\.)?(\d\.\d{1,2}(?:\.\d{1,2})?)(?:[.\-]min)?\.js", html, re.I)
    if not m:
        m = re.search(r"jquery\.js\?ver=(\d\.\d{1,2}(?:\.\d{1,2})?)", html, re.I)
    if not m:
        m = re.search(r"jQuery v(\d\.\d{1,2}(?:\.\d{1,2})?)", html)
    return m.group(1) if m else None


def _copyright_year(text: str) -> int | None:
    years: list[int] = []
    for m in re.finditer(r"(?:©|&copy;|\(c\)|copyright)[^\n]{0,40}", text, re.I):
        years += [int(y) for y in re.findall(r"(?<!\d)(19[89]\d|20[0-4]\d)(?!\d)", m.group(0))]
    return max(years) if years else None


def _newest_upload_year(html: str) -> int | None:
    years = [int(y) for y in re.findall(r"/uploads/(20[0-4]\d|199\d)/", html)]
    return max(years) if years else None


def run_checks(fetch: FetchResult, now: datetime | None = None, browser: BrowserResult | None = None) -> TechReport:
    """Прогнать все технические проверки.

    browser — результат Playwright: даёт реальное время загрузки (slow_load > 4 с), mobile_overflow
    и скриншоты. Без него slow_load считается по TTFB (> 1.5 с).
    """
    now = now or datetime.now()
    rep = TechReport()
    add = rep.issues.append
    m = rep.metrics
    m["http_status"] = fetch.status
    m["final_url"] = fetch.final_url
    m["load_ms"] = fetch.load_ms
    m["ttfb_ms"] = fetch.ttfb_ms
    m["ssl_days_left"] = fetch.ssl_days_left

    if not fetch.ok:
        what = (
            "не отвечает (таймаут)"
            if fetch.error == "timeout"
            else f"ошибка {fetch.status}"
            if fetch.status
            else "нет соединения"
        )
        title = (
            f"Сайт не открылся: {what} — проверьте вручную"
            if not fetch.status
            else f"Сайт не работает: {what}"
        )
        add(Issue("site_down", 3, title, fetch.error or ""))
        if not fetch.html:
            m["https"] = None
            return rep

    https = bool(fetch.final_url and fetch.final_url.startswith("https://"))
    m["https"] = https
    if fetch.final_url and not https:
        add(Issue("no_https", 3, "Нет HTTPS — браузер пишет «Не защищено»"))
    if https and fetch.ssl_invalid:
        add(Issue("ssl_invalid", 3, "Ошибка SSL-сертификата — браузер показывает предупреждение"))
    elif https and fetch.ssl_days_left is not None and fetch.ssl_days_left < 14:
        add(Issue("ssl_expiring", 2, f"SSL-сертификат истекает через {max(fetch.ssl_days_left, 0)} дн."))

    html = fetch.html
    low = html.lower()
    tree = HTMLParser(html)

    # meta
    robots = tree.css_first('meta[name="robots" i]')
    robots_val = (robots.attributes.get("content") or "") if robots else ""
    m["robots_meta"] = robots_val or None
    if "noindex" in robots_val.lower():
        add(Issue("noindex", 3, "Сайт закрыт от индексации (noindex)"))

    vp = tree.css_first('meta[name="viewport" i]')
    m["viewport"] = (vp.attributes.get("content") or "") if vp else None
    if not vp:
        add(Issue("no_viewport", 3, "Нет мобильной версии (meta viewport)"))

    if browser is not None and browser.load_ms is not None:
        m["load_ms"] = browser.load_ms
        if browser.load_ms > SLOW_LOAD_MS:
            add(Issue("slow_load", 2, f"Загрузка {browser.load_ms / 1000:.1f} с (норма до 3)"))
    elif fetch.ttfb_ms and fetch.ttfb_ms > 1500:
        add(Issue("slow_load", 2, f"Медленный ответ сервера: {fetch.ttfb_ms / 1000:.2f} с (норма до 1.5)"))

    if browser is not None:
        m["mobile_overflow"] = browser.mobile_overflow
        m["screenshot_desktop"] = browser.screenshot_desktop
        m["screenshot_mobile"] = browser.screenshot_mobile
        if browser.mobile_overflow:
            add(Issue("mobile_overflow", 3,
                      f"На телефоне страница шире экрана ({browser.mobile_scroll_width} px при 375) — "
                      "едет вбок"))

    scripts = tree.css("script")
    m["scripts_count"] = len(scripts)
    m["html_kb"] = round(fetch.html_bytes / 1024, 1)
    if m["html_kb"] > 300 or len(scripts) > 40:
        add(
            Issue(
                "heavy_page",
                1,
                f"Тяжёлая страница: {m['html_kb']:.0f} КБ HTML, {len(scripts)} скриптов",
            )
        )

    m["has_metrika"] = "mc.yandex.ru" in low or "ym(" in low
    m["has_gtag"] = "googletagmanager.com" in low or "gtag(" in low
    if not m["has_metrika"]:
        add(Issue("no_metrika", 2, "Нет Яндекс Метрики — не видно, откуда заявки"))

    title_node = tree.css_first("title")
    title = title_node.text(strip=True) if title_node else ""
    m["title"] = title or None
    m["title_len"] = len(title)
    if not title or len(title) < 20 or len(title) > 90:
        add(Issue("title_bad", 1, f"Плохой title ({len(title)} симв.)" if title else "Нет title"))

    desc = tree.css_first('meta[name="description" i]')
    desc_val = (desc.attributes.get("content") or "").strip() if desc else ""
    m["description"] = desc_val or None
    if not desc_val:
        add(Issue("no_description", 1, "Нет meta description"))

    h1 = [n.text(strip=True) for n in tree.css("h1")]
    m["h1"] = h1
    if len(h1) != 1:
        add(Issue("h1_missing_or_multi", 1, "Нет заголовка H1" if not h1 else f"{len(h1)} заголовков H1"))

    imgs = tree.css("img")
    no_alt = sum(1 for i in imgs if not (i.attributes.get("alt") or "").strip())
    m["images_count"], m["images_no_alt"] = len(imgs), no_alt
    if imgs and no_alt / len(imgs) > 0.5:
        add(Issue("images_no_alt", 1, f"{no_alt} из {len(imgs)} картинок без alt"))

    # возраст сайта
    body_text = tree.body.text(separator=" ") if tree.body else ""
    cy = _copyright_year(body_text) or _copyright_year(html)
    m["copyright_year"] = cy
    if cy and cy < now.year - 2:
        add(Issue("old_copyright", 2, f"В подвале © {cy} — сайт выглядит заброшенным"))

    uy = _newest_upload_year(html)
    m["oldest_upload_year"] = uy
    if uy and uy < now.year - 4:
        add(Issue("old_uploads", 2, f"Последние загрузки на сайт — {uy} год"))

    jq = _jquery_version(html)
    m["jquery_version"] = jq
    if jq and int(jq.split(".")[0]) < 3:
        add(Issue("old_jquery", 1, f"Устаревший jQuery {jq}"))

    # контакты и конверсия: главная + внутренние страницы (как и в извлечении контактов для карточки,
    # иначе карточка показывает WhatsApp, а аудит пишет «нет WhatsApp»)
    def hrefs_of(t: HTMLParser) -> list[str]:
        return [(a.attributes.get("href") or "").strip().lower() for a in t.css("a[href]")]

    hrefs = hrefs_of(tree)
    extra_hrefs = [h for _, page_html in fetch.extra_pages for h in hrefs_of(HTMLParser(page_html))]
    tel = sum(1 for h in hrefs if h.startswith("tel:"))
    tel_extra = sum(1 for h in extra_hrefs if h.startswith("tel:"))
    mail = sum(1 for h in hrefs + extra_hrefs if h.startswith("mailto:"))
    m["tel_links"], m["mail_links"] = tel + tel_extra, mail
    if not tel and not tel_extra:
        add(Issue("no_tel_link", 2, "Телефон не кликабельный (нет tel:)"))
    elif not tel:
        add(Issue("no_tel_link_main", 1, "На главной телефон не кликабельный — только на внутренних страницах"))

    all_html = "\n".join([html, *(page_html for _, page_html in fetch.extra_pages)])
    messengers = detect_messengers(all_html)
    m["messengers"] = messengers
    if not messengers:
        add(Issue("no_messengers", 1, "Нет WhatsApp / Telegram для связи"))

    forms = tree.css("form")
    m["forms_count"] = len(forms)
    fields: list[str] = []
    lead_form = False
    for f in forms:
        for inp in f.css("input, textarea"):
            itype = (inp.attributes.get("type") or "text").lower()
            if itype in ("hidden", "submit", "button", "checkbox", "radio"):
                continue
            name = " ".join(
                (inp.attributes.get(k) or "") for k in ("name", "placeholder", "type", "id")
            ).strip()
            fields.append(name[:60])
            if itype in ("tel", "email") or PHONE_FIELD_RE.search(name):
                lead_form = True
    # Tilda/виджеты рисуют формы без <form> — считаем input[type=tel] тоже
    if not lead_form and tree.css_first('input[type="tel"], input[type="email"]'):
        lead_form = True
    m["form_fields"] = fields[:20]
    if not lead_form:
        add(Issue("no_form", 2, "Нет формы заявки с телефоном/email"))

    anchor_ctas = sum(
        1
        for a in tree.css("a[href]")
        if (a.attributes.get("href") or "").startswith("#") and CTA_RE.search(a.text(strip=True))
    )
    m["anchor_only_ctas"] = anchor_ctas
    if anchor_ctas >= 3 and not lead_form:
        add(Issue("anchor_ctas", 1, f"{anchor_ctas} кнопок «Заказать» никуда не ведут"))

    if (
        tree.css_first("frameset")
        or re.search(r"<(object|embed)[^>]+(\.swf|shockwave)", low)
    ):
        add(Issue("flash_or_frames", 3, "Устаревшие технологии: Flash / фреймы"))

    if tree.css_first("table table") and len(tree.css("table")) >= 3 and not tree.css_first("main, section, article"):
        add(Issue("table_layout", 2, "Вёрстка таблицами — сайт 2000-х годов"))

    m["cms"] = detect_cms(html)
    return rep
