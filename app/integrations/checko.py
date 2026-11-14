"""Чекко (checko.ru, API v2): реквизиты, руководитель, финансы и риски компании по ИНН.

Бесплатно — 100 запросов в сутки на все методы; списываются и ответы «не найдено».
ООО/АО (ИНН 10 цифр) — 2 запроса: /company + /finances. ИП (12 цифр) — 1 запрос: /entrepreneur.
В БД храним не сырой ответ, а сжатую сводку (`summarize_*`) — её и рисует карточка.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from typing import Any

import httpx
from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.db import repo
from app.db.models import ApiUsage, Company, utcnow

BASE_URL = "https://api.checko.ru/v2"
API_NAME = "checko"


class CheckoError(Exception):
    """Ошибка Чекко: сеть, ключ, лимит."""


class CheckoLimit(CheckoError):
    """Дневной лимит запросов исчерпан."""


# ---------------------------------------------------------------- клиент


class CheckoClient:
    def __init__(self, api_key: str, timeout: float = 20, base_url: str = BASE_URL) -> None:
        self.api_key = api_key
        self.timeout = timeout
        self.base_url = base_url.rstrip("/")

    async def get(self, method: str, **params: str) -> dict[str, Any]:
        """Вызов метода. Возвращает весь ответ ({data, meta, …}); `data` == {} — не найдено."""
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.get(f"{self.base_url}/{method}", params={"key": self.api_key, **params})
        except httpx.HTTPError as exc:
            raise CheckoError(f"нет связи с Чекко: {exc.__class__.__name__}") from exc
        if resp.status_code == 429:
            raise CheckoLimit("Чекко: слишком много запросов (429)")
        try:
            body = resp.json()
        except ValueError as exc:
            raise CheckoError(f"Чекко: HTTP {resp.status_code}, ответ не JSON") from exc
        meta = body.get("meta") or {}
        if resp.status_code >= 400 or meta.get("status") == "error":
            msg = str(meta.get("message") or f"HTTP {resp.status_code}")
            if "лимит" in msg.lower() or "limit" in msg.lower():
                raise CheckoLimit(f"Чекко: {msg}")
            raise CheckoError(f"Чекко: {msg}")
        return body


def make_client() -> CheckoClient | None:
    s = get_settings()
    if not s.CHEKKO_API_KEY:
        return None
    return CheckoClient(s.CHEKKO_API_KEY, timeout=s.HTTP_TIMEOUT)


# ---------------------------------------------------------------- разбор ответа


def _quotes(name: str) -> str:
    """ООО "ГАЗ СЕВЕР" → ООО «ГАЗ СЕВЕР»."""
    out, opened = [], False
    for ch in name:
        if ch == '"':
            out.append("»" if opened else "«")
            opened = not opened
        else:
            out.append(ch)
    return "".join(out)


def _okved(d: dict[str, Any]) -> str | None:
    o = d.get("ОКВЭД") or {}
    if not o.get("Код"):
        return None
    return f"{o['Код']} {o.get('Наим') or ''}".strip()


def _int(v: Any) -> int | None:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _common_risks(d: dict[str, Any]) -> list[str]:
    risks = []
    if d.get("ЕФРСБ"):
        risks.append("Есть сообщения о банкротстве (ЕФРСБ)")
    if d.get("НедобПост"):
        risks.append("В реестре недобросовестных поставщиков")
    if d.get("МассРуковод"):
        risks.append("Массовый руководитель")
    if d.get("МассУчред"):
        risks.append("Массовый учредитель")
    if d.get("ДисквЛица"):
        risks.append("В руководстве дисквалифицированные лица")
    if d.get("Санкции"):
        risks.append("Под санкциями")
    taxes = d.get("Налоги") or {}
    debt = _int(taxes.get("СумНедоим"))
    if debt:
        risks.append(f"Недоимка по налогам {money(debt)}")
    return risks


def summarize_company(d: dict[str, Any], fin: dict[str, Any] | None = None) -> dict[str, Any]:
    """Сводка по ООО/АО из ответа /company (+ /finances)."""
    status = (d.get("Статус") or {}).get("Наим") or ""
    liquid = d.get("Ликвид") or {}
    active = not liquid.get("Дата") and "действ" in status.lower()
    director = None
    leaders = d.get("Руковод") or []
    if leaders:
        r = leaders[0]
        director = {"post": (r.get("НаимДолжн") or "Руководитель").capitalize(), "fio": r.get("ФИО"),
                    "inn": r.get("ИНН"), "since": r.get("ДатаЗаписи")}
    if not director and (d.get("УпрОрг") or {}).get("НаимСокр"):
        director = {"post": "Управляющая организация", "fio": _quotes(d["УпрОрг"]["НаимСокр"]),
                    "inn": d["УпрОрг"].get("ИНН"), "since": d["УпрОрг"].get("ДатаЗаписи")}
    founders = []
    for f in ((d.get("Учред") or {}).get("ФЛ") or [])[:3]:
        founders.append({"name": f.get("ФИО"), "share": (f.get("Доля") or {}).get("Процент")})
    for f in ((d.get("Учред") or {}).get("РосОрг") or [])[: max(0, 3 - len(founders))]:
        founders.append({"name": _quotes(f.get("НаимСокр") or f.get("НаимПолн") or ""),
                         "share": (f.get("Доля") or {}).get("Процент")})
    risks = _common_risks(d)
    if (d.get("ЮрАдрес") or {}).get("Недост"):
        risks.append("Недостоверный юридический адрес")
    return {
        "kind": "ul",
        "inn": d.get("ИНН"),
        "ogrn": d.get("ОГРН"),
        "name": _quotes(d.get("НаимСокр") or d.get("НаимПолн") or ""),
        "full_name": _quotes(d.get("НаимПолн") or ""),
        "status": status or None,
        "active": active,
        "closed_date": liquid.get("Дата"),
        "reg_date": d.get("ДатаРег"),
        "address": (d.get("ЮрАдрес") or {}).get("АдресРФ"),
        "okved": _okved(d),
        "employees": _int(d.get("СЧР")),
        "msp": ((d.get("РМСП") or {}).get("Кат") or "").lower() or None,
        "tax": (d.get("Налоги") or {}).get("ОсобРежим") or [],
        "capital": _int((d.get("УстКап") or {}).get("Сумма")),
        "director": director,
        "founders": founders,
        "finance": summarize_finances(fin) if fin else None,
        "risks": risks,
    }


def summarize_entrepreneur(d: dict[str, Any]) -> dict[str, Any]:
    """Сводка по ИП из ответа /entrepreneur."""
    stop = d.get("Прекращ") or {}
    fio = d.get("ФИО") or ""
    return {
        "kind": "ip",
        "inn": d.get("ИНН"),
        "ogrn": d.get("ОГРНИП"),
        "name": f"ИП {fio}".strip(),
        "full_name": f"{d.get('Тип') or 'Индивидуальный предприниматель'} {fio}".strip(),
        "status": (d.get("Статус") or {}).get("Наим") or ("Прекратил деятельность" if stop.get("Дата") else "Действует"),
        "active": not stop.get("Дата"),
        "closed_date": stop.get("Дата"),
        "reg_date": d.get("ДатаРег"),
        "address": d.get("НасПункт"),
        "okved": _okved(d),
        "employees": None,
        "msp": ((d.get("РМСП") or {}).get("Кат") or "").lower() or None,
        "tax": (d.get("Налоги") or {}).get("ОсобРежим") or [],
        "capital": None,
        "director": {"post": "Индивидуальный предприниматель", "fio": fio, "inn": d.get("ИНН"),
                     "since": d.get("ДатаРег")},
        "founders": [],
        "finance": None,
        "risks": _common_risks(d),
    }


def summarize_finances(body: dict[str, Any]) -> dict[str, Any] | None:
    """Последний отчётный год: выручка (2110) и чистая прибыль (2400) + прошлый год для динамики.

    С 2019 года данные ФНС не содержат нулевых строк: если год есть, а строки 2110 нет — выручка 0.
    """
    data = body.get("data") or {}
    years = sorted((y for y in data if y.isdigit() and data[y]), key=int, reverse=True)
    if not years:
        return None
    y = years[0]
    row, prev = data[y], data.get(str(int(y) - 1)) or None

    def val(r: dict[str, Any] | None, code: str) -> int | None:
        return None if r is None else _int(r.get(code, 0))

    reports = (body.get("bo.nalog.ru") or {}).get("Отчет") or {}
    return {
        "year": int(y),
        "revenue": val(row, "2110"),
        "revenue_prev": val(prev, "2110"),
        "profit": val(row, "2400"),
        "profit_prev": val(prev, "2400"),
        "report_url": reports.get(y),
    }


# ---------------------------------------------------------------- форматирование


def money(rub: int | float) -> str:
    """45 800 000 → «45,8 млн ₽»."""
    v = abs(rub)
    sign = "−" if rub < 0 else ""
    for div, unit in ((1e9, "млрд"), (1e6, "млн"), (1e3, "тыс.")):
        if v >= div:
            num = v / div
            text = f"{num:.1f}" if num < 100 else f"{num:.0f}"
            text = text.removesuffix(".0").replace(".", ",")
            return f"{sign}{text} {unit} ₽"
    return f"{sign}{int(v)} ₽"


def change_pct(cur: int | None, prev: int | None) -> int | None:
    if cur is None or not prev:
        return None
    return round((cur - prev) / abs(prev) * 100)


MONTHS_GEN = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября",
              "октября", "ноября", "декабря")


def human_date(iso: str | None) -> str:
    """2012-03-21 → «21 марта 2012 г.»."""
    try:
        d = date.fromisoformat((iso or "")[:10])
    except ValueError:
        return iso or ""
    return f"{d.day} {MONTHS_GEN[d.month - 1]} {d.year} г."


def years_word(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "год"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return "года"
    return "лет"


def age(iso: str | None, today: date | None = None) -> str | None:
    """Сколько лет компании: «12 лет», «меньше года»."""
    try:
        d = date.fromisoformat((iso or "")[:10])
    except ValueError:
        return None
    today = today or date.today()
    n = today.year - d.year - ((today.month, today.day) < (d.month, d.day))
    return "меньше года" if n < 1 else f"{n} {years_word(n)}"


def first_names(fio: str | None) -> str | None:
    """«Романов Алексей Владимирович» → «Алексей Владимирович» (как обращаться при звонке)."""
    parts = (fio or "").split()
    return " ".join(parts[1:3]) if len(parts) >= 2 else None


def revenue_line(fin: dict[str, Any]) -> str:
    """«Выручка снизилась до 45,8 млн ₽ (📉 −5%)»."""
    rev, prev = fin.get("revenue"), fin.get("revenue_prev")
    if not rev:
        return "Выручка 0 ₽ — нулевая отчётность"
    pct = change_pct(rev, prev)
    if pct is None:
        return f"Выручка {money(rev)}"
    if pct > 0:
        return f"Выручка выросла до {money(rev)} (📈 +{pct}%)"
    if pct < 0:
        return f"Выручка снизилась до {money(rev)} (📉 −{abs(pct)}%)"
    return f"Выручка {money(rev)} (на уровне прошлого года)"


def profit_line(fin: dict[str, Any]) -> str | None:
    p = fin.get("profit")
    if not p:  # нет данных или ровно 0 — строка ничего не говорит
        return None
    word = "Чистая прибыль" if p >= 0 else "Чистый убыток"
    pct = change_pct(p, fin.get("profit_prev")) if p >= 0 and (fin.get("profit_prev") or 0) > 0 else None
    tail = f" ({'+' if pct > 0 else '−' if pct < 0 else '±'}{abs(pct)}%)" if pct is not None else ""
    return f"{word} {money(abs(p))}{tail}"


def status_line(s: dict[str, Any], today: date | None = None) -> str:
    parts = []
    if s.get("active"):
        parts.append("✅ Действует")
        a = age(s.get("reg_date"), today)
        if a:
            parts.append(f"на рынке {a}")
    else:
        closed = f" с {human_date(s['closed_date'])}" if s.get("closed_date") else ""
        parts.append(f"⛔ {s.get('status') or 'Не действует'}{closed}")
    if s.get("employees"):
        parts.append(f"👥 {s['employees']} сотр.")
    if s.get("msp"):
        parts.append(s["msp"])
    if s.get("tax"):
        parts.append(", ".join(s["tax"]))
    return " · ".join(parts)


# ---------------------------------------------------------------- обогащение компании


def today_start_utc(now: datetime | None = None) -> datetime:
    tz = get_settings().tz
    local = (now or utcnow()).astimezone(tz)
    return local.replace(hour=0, minute=0, second=0, microsecond=0)


async def requests_today(session: AsyncSession) -> int:
    q = select(func.coalesce(func.sum(ApiUsage.requests), 0)).where(
        ApiUsage.api == API_NAME, ApiUsage.created_at >= today_start_utc()
    )
    return int((await session.execute(q)).scalar_one())


def needs_refresh(company: Company, refresh_days: int, now: datetime | None = None) -> bool:
    if not company.inn:
        return False
    data = company.checko or {}
    if not company.checko_at or data.get("inn") != company.inn:
        return True
    return (now or utcnow()) - company.checko_at > timedelta(days=refresh_days)


_lock = asyncio.Lock()


async def enrich_company(
    session_factory: async_sessionmaker[AsyncSession],
    company_id: int,
    *,
    force: bool = False,
    client: CheckoClient | None = None,
) -> dict[str, Any] | None:
    """Подтянуть данные Чекко по ИНН компании и сохранить сводку в `companies.checko`.

    Возвращает сводку (или None, если ИНН нет / данные свежие и force=False).
    Бросает CheckoLimit / CheckoError — вызывающий решает, показывать ли ошибку.
    """
    async with _lock:  # запросы последовательно — чтобы честно считать дневной лимит
        async with session_factory() as session:
            company = await repo.get_company(session, company_id)
            if not company or not company.inn:
                return None
            refresh_days = await repo.get_runtime_setting(session, "CHECKO_REFRESH_DAYS")
            if not force and not needs_refresh(company, refresh_days):
                return company.checko
            if not await repo.get_runtime_setting(session, "CHECKO_ENABLED"):
                raise CheckoError("Чекко выключен (CHECKO_ENABLED=false)")
            client = client or make_client()
            if client is None:
                raise CheckoError("не задан ключ CHEKKO_API_KEY в .env")
            limit = await repo.get_runtime_setting(session, "CHECKO_DAILY_LIMIT")
            inn = company.inn
            cost = 2 if len(inn) == 10 else 1
            used = await requests_today(session)
            if used + cost > limit:
                raise CheckoLimit(f"лимит Чекко на сегодня исчерпан ({used}/{limit})")

            async def call(method: str) -> dict[str, Any]:
                body = await client.get(method, inn=inn)
                session.add(ApiUsage(api=API_NAME, requests=1, paid=False))
                await session.flush()
                return body

            try:
                if len(inn) == 12:
                    body = await call("entrepreneur")
                    summary = summarize_entrepreneur(body["data"]) if body.get("data") else None
                else:
                    body = await call("company")
                    summary = None
                    if body.get("data"):
                        summary = summarize_company(body["data"], await call("finances"))
            finally:
                await session.commit()  # учёт запросов сохраняем даже при ошибке
            if summary is None:
                summary = {"inn": inn, "not_found": True}
            summary["fetched_at"] = utcnow().isoformat()
            company.checko = summary
            company.checko_at = utcnow()
            if not company.name and summary.get("name"):
                company.name = summary["name"][:255]
            await session.commit()
            logger.info("checko {} ({}): {}", company.domain, inn,
                        "не найдено" if summary.get("not_found") else summary.get("status"))
            return summary


async def auto_enrich(session_factory: async_sessionmaker[AsyncSession], company_id: int) -> None:
    """Обогащение после аудита: тихо, без исключений (лимит/ошибки — только в лог)."""
    async with session_factory() as session:
        company = await repo.get_company(session, company_id)
        if not company or not company.inn or not get_settings().CHEKKO_API_KEY:
            return
        if {"off_region", "off_niche", "audit_failed"} & set(company.tags or []):
            return  # уйдёт в отсев — не тратим лимит
    try:
        await enrich_company(session_factory, company_id)
    except CheckoError as exc:
        logger.info("checko skipped for {}: {}", company_id, exc)
