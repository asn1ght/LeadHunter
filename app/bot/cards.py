"""Рендер карточки лида (раздел 8.4)."""
from __future__ import annotations

from html import escape

from app.audit.extract import format_phone
from app.config import get_settings
from app.crm.statuses import label
from app.db.models import Audit, Company
from app.utils.dates import fmt_dt

TAG_WARNINGS = {
    "audit_failed": "⚠️ Аудит не удался — сайт проверить вручную",
    "off_region": "⚠️ На сайте не найден регион СПб/ЛО",
    "off_niche": "⚠️ Нет ключевых слов ниши",
    "robots_disallowed": "ℹ️ robots.txt запрещает обход — проверена только главная",
}


def score_emoji(score: int | None, threshold: int) -> str:
    if score is None:
        return "⚪️"
    if score >= threshold:
        return "🔥"
    if score >= threshold - 20:
        return "🟡"
    return "⚪️"


def render_card(company: Company, audit: Audit | None, threshold: int, prefix: str = "") -> str:
    """HTML-текст карточки лида."""
    tz = get_settings().tz
    name = escape(company.name or company.domain)
    score = f"{company.score}/100" if company.score is not None else "—/100"
    lines: list[str] = []
    if prefix:
        lines += [prefix, ""]
    lines.append(f"{score_emoji(company.score, threshold)} <b>{name}</b> — {score}   {label(company.status)}")

    meta = [escape(company.domain)]
    if company.niche:
        meta.append(escape(company.niche.name))
    if company.segment and company.segment != "unknown":
        meta.append(company.segment.upper())
    if audit and audit.cms:
        meta.append(escape(audit.cms))
    lines.append("🌐 " + " · ".join(meta))
    if company.inn:
        lines.append(f"🏢 ИНН {company.inn}")
    if company.address:
        lines.append(f"📍 {escape(company.address)}")

    contacts: list[str] = []
    if company.phones:
        contacts.append("📞 " + format_phone(company.phones[0]))
    if company.emails:
        contacts.append("✉️ " + escape(company.emails[0]))
    contacts += company.messengers or []
    if contacts:
        lines.append(" · ".join(contacts))
    if company.contact_person or company.contact_phone:
        person = " · ".join(
            x for x in (escape(company.contact_person or ""), format_phone(company.contact_phone or "")) if x
        )
        lines.append(f"👤 {person}")
    if company.next_action_at:
        lines.append(f"⏰ Следующее действие: {fmt_dt(company.next_action_at, tz)}")

    for tag in company.tags or []:
        if tag in TAG_WARNINGS:
            lines.append(TAG_WARNINGS[tag])

    points = main_points(audit)
    llm = (audit.llm_summary if audit else None) or {}
    if llm.get("summary"):
        lines += ["", f"🧠 <i>{escape(llm['summary'])}</i>"]
    if points:
        lines += ["", "<b>Главное:</b>"]
        lines += [f"• {escape(p)}" for p in points[:5]]
        if len(points) > 5:
            lines.append(f"<i>…и ещё {len(points) - 5}</i>")
    elif audit and not audit.error:
        lines += ["", "Явных технических проблем не найдено."]

    if audit and audit.llm_summary and audit.llm_summary.get("pitch_angle"):
        lines += ["", f"💡 Крючок: «{escape(audit.llm_summary['pitch_angle'])}»"]

    notes = (company.notes or "").strip().splitlines()
    if notes:
        shown = notes[-3:]
        more = f"\n<i>…ещё {len(notes) - 3}</i>" if len(notes) > 3 else ""
        lines += ["", "📝 Заметки:" + more] + [escape(n[:300]) for n in shown]
    else:
        lines += ["", "📝 Заметки: —"]

    text = "\n".join(lines)
    return text[:4000]


def main_points(audit: Audit | None) -> list[str]:
    """Пункты «Главное»: опечатки и цены от ИИ, проблемы ИИ и технические проблемы — по важности."""
    if not audit:
        return []
    llm = audit.llm_summary or {}
    scored: list[tuple[int, str]] = []
    for t in llm.get("typos") or []:
        scored.append((3, f"Опечатка: {t}"))
    for p in llm.get("price_inconsistencies") or []:
        scored.append((3, f"Цены расходятся: {p}"))
    for p in llm.get("problems") or []:
        scored.append((int(p.get("severity", 2)), str(p.get("title", ""))))
    for i in audit.issues or []:
        scored.append((int(i.get("severity", 0)), str(i.get("title", ""))))
    seen: set[str] = set()
    out: list[str] = []
    for _, text in sorted(scored, key=lambda x: -x[0]):
        if text and text.lower() not in seen:
            seen.add(text.lower())
            out.append(text)
    return out


def render_why(company: Company, audit: Audit | None) -> str:
    """Расшифровка оценки по факторам."""
    lines = [f"❓ <b>Почему {company.score if company.score is not None else '—'}/100</b> — {escape(company.domain)}", ""]
    factors = (audit.score_factors if audit else []) or []
    if not factors:
        lines.append("Факторов нет — аудит не проводился или проблем не найдено.")
    for f in factors:
        pts = int(f.get("points", 0))
        sign = f"+{pts}" if pts else "  "
        lines.append(f"<code>{sign:>4}</code> {escape(str(f.get('label')))}")
    lines += ["", f"Тех. оценка: {company.score_tech if company.score_tech is not None else '—'} (макс. 100)"]
    if company.score_quality is not None:
        lines.append(f"Качество (ИИ): {company.score_quality}")
    return "\n".join(lines)
