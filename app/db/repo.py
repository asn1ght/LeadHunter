"""Все запросы к БД. Бот и сервисы работают с БД только через этот модуль."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import CONFIG_DIR, get_settings
from app.db.models import (
    Audit,
    BlacklistDomain,
    Company,
    Event,
    Niche,
    Reminder,
    SearchQuery,
    SettingKV,
)

# ---------------------------------------------------------------- settings


async def get_setting_raw(session: AsyncSession, key: str) -> str | None:
    """Значение из таблицы settings или None."""
    row = await session.get(SettingKV, key)
    return row.value if row else None


async def set_setting(session: AsyncSession, key: str, value: str) -> None:
    """Сохранить значение настройки (перекрывает `.env`)."""
    row = await session.get(SettingKV, key)
    if row:
        row.value = value
    else:
        session.add(SettingKV(key=key, value=value))
    await session.flush()


async def get_runtime_setting(session: AsyncSession, key: str) -> Any:
    """Эффективное значение настройки: таблица settings, иначе `.env`.

    Значение из БД приводится к типу соответствующего поля Settings.
    """
    default = getattr(get_settings(), key)
    raw = await get_setting_raw(session, key)
    if raw is None:
        return default
    try:
        if isinstance(default, bool):
            return raw.strip().lower() in {"1", "true", "yes", "да", "on"}
        if isinstance(default, int):
            return int(raw)
        if isinstance(default, float):
            return float(raw)
    except ValueError:
        return default
    return raw


# ---------------------------------------------------------------- niches


async def get_niche_by_slug(session: AsyncSession, slug: str) -> Niche | None:
    return (await session.execute(select(Niche).where(Niche.slug == slug))).scalar_one_or_none()


async def get_active_niche(session: AsyncSession) -> Niche | None:
    """Активная ниша (настройка ACTIVE_NICHE), иначе первая активная."""
    slug = await get_runtime_setting(session, "ACTIVE_NICHE")
    niche = await get_niche_by_slug(session, slug)
    if niche:
        return niche
    return (
        await session.execute(select(Niche).where(Niche.is_active.is_(True)).order_by(Niche.id))
    ).scalars().first()


def load_niches_config(path: Path | None = None) -> dict[str, dict[str, Any]]:
    """Прочитать config/niches.yaml."""
    path = path or CONFIG_DIR / "niches.yaml"
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


async def sync_niches_from_config(session: AsyncSession) -> None:
    """Создать ниши и поисковые запросы из niches.yaml, если их ещё нет в БД."""
    for slug, cfg in load_niches_config().items():
        niche = await get_niche_by_slug(session, slug)
        if not niche:
            niche = Niche(slug=slug, name=cfg.get("name", slug), is_active=True)
            session.add(niche)
            await session.flush()
        if not niche.keywords and cfg.get("keywords"):
            niche.keywords = list(cfg["keywords"])
        if not niche.region_markers and cfg.get("region_markers"):
            niche.region_markers = list(cfg["region_markers"])
        if not niche.priority_segment and cfg.get("priority_segment"):
            niche.priority_segment = cfg["priority_segment"]
        existing = set(
            (await session.execute(select(SearchQuery.text).where(SearchQuery.niche_id == niche.id))).scalars()
        )
        for q in cfg.get("queries", []) or []:
            if q not in existing:
                session.add(SearchQuery(niche_id=niche.id, text=q))
    await session.flush()


# ---------------------------------------------------------------- companies


async def get_company(session: AsyncSession, company_id: int) -> Company | None:
    return await session.get(Company, company_id)


async def get_company_by_domain(session: AsyncSession, domain: str) -> Company | None:
    return (await session.execute(select(Company).where(Company.domain == domain))).scalar_one_or_none()


async def create_company(session: AsyncSession, **fields: Any) -> Company:
    """Создать компанию (лида)."""
    company = Company(**fields)
    session.add(company)
    await session.flush()
    return company


async def list_companies(
    session: AsyncSession,
    status: str | None = None,
    niche_id: int | None = None,
    offset: int = 0,
    limit: int = 10,
) -> tuple[list[Company], int]:
    """Список лидов с фильтром и пагинацией, по убыванию оценки. Возвращает (лиды, всего)."""
    conds = []
    if status:
        conds.append(Company.status == status)
    if niche_id:
        conds.append(Company.niche_id == niche_id)
    total = (await session.execute(select(func.count(Company.id)).where(*conds))).scalar_one()
    rows = (
        await session.execute(
            select(Company)
            .where(*conds)
            .order_by(Company.score.desc().nulls_last(), Company.id.desc())
            .offset(offset)
            .limit(limit)
        )
    ).scalars().all()
    return list(rows), total


async def count_by_status(session: AsyncSession) -> dict[str, int]:
    rows = await session.execute(select(Company.status, func.count(Company.id)).group_by(Company.status))
    return {s: c for s, c in rows.all()}


async def find_companies(session: AsyncSession, query: str, limit: int = 20) -> list[Company]:
    """Поиск по названию, домену, телефону, ИНН, контакту, email, адресу и заметкам.

    Регистр, ё/е, транслит и порядок слов не важны, мелкие опечатки допускаются
    (см. app.crm.search). Результат — по убыванию релевантности.
    """
    from app.crm.search import Doc, search

    if not query.strip():
        return []
    companies = list((await session.execute(select(Company))).scalars())
    docs = [
        Doc(
            id=c.id, name=c.name or "", domain=c.domain, inn=c.inn or "",
            phones=tuple([*(c.phones or []), c.contact_phone or ""]), emails=tuple(c.emails or []),
            contact=c.contact_person or "", address=c.address or "", notes=c.notes or "",
        )
        for c in companies
    ]
    by_id = {c.id: c for c in companies}
    return [by_id[cid] for cid, _ in search(query, docs, limit)]


# ---------------------------------------------------------------- audits


async def add_audit(session: AsyncSession, **fields: Any) -> Audit:
    audit = Audit(**fields)
    session.add(audit)
    await session.flush()
    return audit


async def last_audit(session: AsyncSession, company_id: int) -> Audit | None:
    return (
        await session.execute(
            select(Audit).where(Audit.company_id == company_id).order_by(Audit.id.desc()).limit(1)
        )
    ).scalar_one_or_none()


# ---------------------------------------------------------------- events


async def add_event(session: AsyncSession, **fields: Any) -> Event:
    """Записать событие. Смену статуса делать только через crm.service.change_status()."""
    ev = Event(**fields)
    session.add(ev)
    await session.flush()
    return ev


async def company_events(session: AsyncSession, company_id: int) -> list[Event]:
    return list(
        (
            await session.execute(select(Event).where(Event.company_id == company_id).order_by(Event.id))
        ).scalars()
    )


# ---------------------------------------------------------------- reminders


async def add_reminder(session: AsyncSession, **fields: Any) -> Reminder:
    r = Reminder(**fields)
    session.add(r)
    await session.flush()
    return r


async def get_reminder(session: AsyncSession, reminder_id: int) -> Reminder | None:
    return await session.get(Reminder, reminder_id)


async def close_open_reminders(session: AsyncSession, company_id: int) -> None:
    """Закрыть все открытые напоминания компании (новое заменяет старые)."""
    await session.execute(
        update(Reminder)
        .where(Reminder.company_id == company_id, Reminder.is_done.is_(False))
        .values(is_done=True)
    )


async def due_reminders(session: AsyncSession, now: datetime) -> list[Reminder]:
    """Наступившие, ещё не отправленные напоминания."""
    return list(
        (
            await session.execute(
                select(Reminder)
                .where(Reminder.is_done.is_(False), Reminder.fired_at.is_(None), Reminder.due_at <= now)
                .order_by(Reminder.due_at)
            )
        ).scalars()
    )


async def upcoming_call2(session: AsyncSession, now: datetime, until: datetime) -> list[Reminder]:
    """Звонки №2 в ближайшем окне, о которых ещё не предупреждали."""
    return list(
        (
            await session.execute(
                select(Reminder).where(
                    Reminder.kind == "call2",
                    Reminder.is_done.is_(False),
                    Reminder.pre_notified_at.is_(None),
                    Reminder.fired_at.is_(None),
                    Reminder.due_at > now,
                    Reminder.due_at <= until,
                )
            )
        ).scalars()
    )


async def open_reminders_before(session: AsyncSession, until: datetime) -> list[Reminder]:
    """Открытые напоминания со сроком до `until` (включая просроченные)."""
    return list(
        (
            await session.execute(
                select(Reminder)
                .where(Reminder.is_done.is_(False), Reminder.due_at < until)
                .order_by(Reminder.due_at)
            )
        ).scalars()
    )


# ---------------------------------------------------------------- blacklist


async def add_blacklist(session: AsyncSession, domain: str, reason: str | None = None) -> None:
    if not await session.get(BlacklistDomain, domain):
        session.add(BlacklistDomain(domain=domain, reason=reason))
        await session.flush()


async def blacklisted_domains(session: AsyncSession) -> set[str]:
    return set((await session.execute(select(BlacklistDomain.domain))).scalars())


async def list_niches(session: AsyncSession) -> list[Niche]:
    return list((await session.execute(select(Niche).order_by(Niche.id))).scalars())


async def niche_queries(session: AsyncSession, niche_id: int) -> list[SearchQuery]:
    return list(
        (await session.execute(select(SearchQuery).where(SearchQuery.niche_id == niche_id).order_by(SearchQuery.id))).scalars()
    )
