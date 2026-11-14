"""PDF мини-аудит (раздел 11): HTML-шаблон Jinja2 → PDF A4 через Chromium."""
from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.audit.browser import abs_path, html_to_pdf
from app.config import get_settings

TEMPLATES = Path(__file__).resolve().parent.parent / "templates"
_env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape(["html"]))

# Что техническая проблема значит для бизнеса — человеческим языком
ISSUE_IMPACT: dict[str, str] = {
    "site_down": "Клиент открывает сайт и видит ошибку — он просто звонит следующей компании из поиска.",
    "no_https": "Браузер помечает сайт как «Не защищено» — часть людей закрывает его, не дочитав.",
    "ssl_invalid": "Браузер показывает страшное предупреждение вместо сайта — до заявки почти никто не доходит.",
    "ssl_expiring": "Скоро браузеры начнут показывать предупреждение вместо сайта.",
    "noindex": "Сайт закрыт от поисковиков — его не найдут те, кто ищет ваши услуги в Яндексе.",
    "no_viewport": "На телефоне сайт показывается мелко, как на компьютере — а больше половины заходов сейчас с телефона.",
    "slow_load": "Пока страница грузится, посетитель уходит — особенно с рекламы, за клик по которой вы уже заплатили.",
    "heavy_page": "Страница долго открывается на мобильном интернете — особенно за городом, где связь слабее.",
    "no_metrika": "Не видно, сколько людей заходит и откуда приходят заявки — бюджет на рекламу тратится вслепую.",
    "title_bad": "Заголовок страницы плохо работает в поиске — сайт ниже конкурентов в выдаче.",
    "no_description": "В поиске показывается случайный кусок текста вместо продающего описания.",
    "h1_missing_or_multi": "Поисковики хуже понимают, о чём страница.",
    "images_no_alt": "Картинки не участвуют в поиске и хуже воспринимаются поисковиками.",
    "old_copyright": "Старый год в подвале — клиент думает, что компания уже не работает.",
    "old_uploads": "Сайт давно не обновлялся — это видно и клиентам, и поисковикам.",
    "old_jquery": "Устаревший код: возможны ошибки в современных браузерах и уязвимости.",
    "no_tel_link": "С телефона нельзя позвонить в одно касание — часть людей не будет переписывать номер.",
    "no_messengers": "Нет WhatsApp/Telegram — многим проще написать, чем звонить.",
    "no_form": "Нет формы заявки — тот, кто не готов звонить прямо сейчас, уходит без контакта.",
    "anchor_ctas": "Кнопки «Заказать» никуда не ведут — человек нажимает и ничего не происходит.",
    "mobile_overflow": "На телефоне страница едет вбок — пользоваться неудобно, человек закрывает сайт.",
    "flash_or_frames": "Устаревшие технологии: часть сайта не открывается в современных браузерах.",
    "table_layout": "Вёрстка 2000-х: сайт выглядит устаревшим и плохо работает на телефонах.",
}

COST_LINES: list[tuple[set[str], str]] = [
    ({"slow_load", "mobile_overflow", "no_viewport", "heavy_page"},
     "Посетитель с рекламы уходит за 5–10 секунд, если сайт медленный или неудобен на телефоне: "
     "клик оплачен, а заявку получает конкурент."),
    ({"no_form", "no_tel_link", "no_messengers", "anchor_ctas"},
     "Людям неудобно оставить контакт — часть готовых клиентов уходит, так и не связавшись."),
    ({"no_https", "ssl_invalid", "old_copyright", "old_uploads", "table_layout", "trust"},
     "Сайт вызывает недоверие — на дорогих услугах клиент выбирает ту компанию, которая выглядит надёжнее."),
    ({"noindex", "title_bad", "no_description", "no_metrika"},
     "Сайт теряет бесплатный трафик из поиска, а без аналитики непонятно, какая реклама окупается."),
]


@dataclass
class PdfPoint:
    title: str
    why: str


def build_points(audit: Any, limit: int = 5) -> list[PdfPoint]:
    """3–5 главных проблем: сначала из ИИ-анализа (с объяснением), затем технические."""
    llm = getattr(audit, "llm_summary", None) or {}
    points: list[tuple[int, PdfPoint]] = []
    for t in llm.get("typos") or []:
        points.append((3, PdfPoint(f"Опечатка: {t}", "Ошибки в тексте снижают доверие к компании.")))
    for p in llm.get("price_inconsistencies") or []:
        points.append((3, PdfPoint(f"Цены расходятся: {p}", "Клиент не понимает, сколько будет стоить, и уходит к тем, у кого понятно.")))
    for p in llm.get("problems") or []:
        points.append((int(p.get("severity", 2)), PdfPoint(str(p.get("title", "")), str(p.get("why_it_costs_money", "")))))
    for i in getattr(audit, "issues", None) or []:
        points.append((int(i.get("severity", 0)), PdfPoint(str(i.get("title", "")), ISSUE_IMPACT.get(i.get("code", ""), ""))))
    seen: set[str] = set()
    out: list[PdfPoint] = []
    for _, pt in sorted(points, key=lambda x: -x[0]):
        key = pt.title.lower()
        if pt.title and key not in seen:
            seen.add(key)
            out.append(pt)
        if len(out) >= limit:
            break
    return out


def build_costs(audit: Any) -> list[str]:
    codes = {i.get("code") for i in (getattr(audit, "issues", None) or [])}
    llm = getattr(audit, "llm_summary", None) or {}
    if llm.get("trust_gaps"):
        codes.add("trust")
    lines = [text for keys, text in COST_LINES if keys & codes]
    return lines[:3] or ["Сайт можно сделать так, чтобы он приносил больше заявок из того же трафика."]


def _img(rel: str | None) -> str | None:
    p = abs_path(rel)
    if not p:
        return None
    mime = "image/png" if p.suffix.lower() == ".png" else "image/jpeg"
    return f"data:{mime};base64," + base64.b64encode(p.read_bytes()).decode()


def render_html(company: Any, audit: Any, now: datetime | None = None) -> str:
    """HTML мини-аудита (для PDF и для тестов)."""
    s = get_settings()
    now = now or datetime.now(s.tz)
    llm = getattr(audit, "llm_summary", None) or {}
    return _env.get_template("audit_pdf.html").render(
        company_name=company.name or company.domain,
        domain=company.domain,
        url=company.url,
        date=now.strftime("%d.%m.%Y"),
        score=company.score,
        summary=llm.get("summary"),
        points=build_points(audit),
        costs=build_costs(audit),
        desktop=_img(getattr(audit, "screenshot_desktop", None)),
        mobile=_img(getattr(audit, "screenshot_mobile", None)),
        owner={"name": s.OWNER_NAME, "phone": s.OWNER_PHONE, "telegram": s.OWNER_TELEGRAM, "email": s.OWNER_EMAIL},
    )


async def render_pdf(company: Any, audit: Any) -> bytes:
    return await html_to_pdf(render_html(company, audit))
