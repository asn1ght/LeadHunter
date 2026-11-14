"""Отчёты: фикстура с событиями за разные дни → проверка цифр."""
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from app.db import repo
from app.db.models import Event
from app.reports.builder import Period, ReportData, build_report, parse_report_date
from app.reports.formatter import format_report
from app.scheduler import report_triggers

TZ = ZoneInfo("Europe/Moscow")


def msk(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=TZ).astimezone(timezone.utc)


def st(cid, frm, to, when):
    return Event(company_id=cid, type="status_change", from_status=frm, to_status=to, payload={}, created_at=when)


def call(cid, stage, outcome, when):
    return Event(company_id=cid, type="call_logged", payload={"stage": stage, "outcome": outcome}, created_at=when)


@pytest.fixture
async def filled(session):
    niche = await repo.get_niche_by_slug(session, "gas")
    c = []
    for i, (score, created) in enumerate([
        (80, msk(2026, 11, 13, 9)),
        (60, msk(2026, 11, 13, 10)),
        (30, msk(2026, 11, 13, 11)),   # ниже порога
        (70, msk(2026, 11, 12, 23, 59)),  # вчера по МСК
        (90, msk(2026, 11, 14, 0, 1)),  # завтра
    ]):
        c.append(await repo.create_company(session, domain=f"d{i}.ru", url=f"d{i}.ru", niche_id=niche.id,
                                           score=score, created_at=created))
    a, b, d, e = c[0].id, c[1].id, c[3].id, c[4].id
    session.add_all([
        # 13.11 (день отчёта)
        st(a, "NEW", "SENT", msk(2026, 11, 13, 10)),
        st(b, "NEW", "SENT", msk(2026, 11, 13, 10, 5)),
        st(b, "SENT", "SENT", msk(2026, 11, 13, 10, 6)),  # повторная установка — не удваивает
        st(a, "SENT", "REPLIED", msk(2026, 11, 13, 12)),
        call(a, 1, "TEST_AGREED", msk(2026, 11, 13, 14)),
        st(a, "REPLIED", "TEST_AGREED", msk(2026, 11, 13, 14)),
        st(a, "TEST_AGREED", "CALL2_SCHEDULED", msk(2026, 11, 13, 14, 1)),
        call(b, 1, "no_answer", msk(2026, 11, 13, 15)),
        st(b, "SENT", "IGNORED", msk(2026, 11, 13, 15)),
        st(d, "NEW", "CALLBACK", msk(2026, 11, 13, 16)),
        call(d, 2, "RESCHEDULED", msk(2026, 11, 13, 17)),
        call(d, 2, "REFUSED", msk(2026, 11, 13, 18)),
        st(d, "CALL2_SCHEDULED", "REFUSED", msk(2026, 11, 13, 18)),
        Event(company_id=a, type="note", payload={"text": "x"}, created_at=msk(2026, 11, 13, 19)),
        # 12.11 — другой день, та же неделя
        st(d, "NEW", "SENT", msk(2026, 11, 12, 23, 30)),
        call(d, 2, "AGREED", msk(2026, 11, 12, 12)),
        st(d, "CALL2_SCHEDULED", "AGREED", msk(2026, 11, 12, 12)),
        # 14.11 00:30 МСК — уже следующий день (в UTC это ещё 13.11)
        st(e, "NEW", "SENT", msk(2026, 11, 14, 0, 30)),
        # прошлая неделя
        st(e, "NEW", "SENT", msk(2026, 11, 5, 12)),
        st(e, "SENT", "REPLIED", msk(2026, 11, 5, 13)),
    ])
    await session.commit()
    return session


async def test_day(filled):
    r = await build_report(filled, Period.day(date(2026, 11, 13)), TZ, threshold=55)
    assert r.as_dict() == {
        "found_new": 2, "sent": 2, "replied": 1, "callback": 1,
        "call1_done": 2, "test_agreed": 1, "call1_refused": 0, "call1_ignored": 1,
        "call2_scheduled": 1, "call2_done": 1, "call2_refused": 1, "call2_ignored": 0, "call2_rescheduled": 1,
        "agreed": 0, "thinking": 0, "refused": 1, "ignored": 1,
    }


async def test_other_day_and_tz_border(filled):
    r12 = await build_report(filled, Period.day(date(2026, 11, 12)), TZ, threshold=55)
    assert (r12.found_new, r12.sent, r12.agreed, r12.call2_done) == (1, 1, 1, 1)
    r14 = await build_report(filled, Period.day(date(2026, 11, 14)), TZ, threshold=55)
    assert (r14.found_new, r14.sent) == (1, 1)


async def test_week_and_compare(filled):
    week = Period.week(date(2026, 11, 13))
    assert (week.start, week.end) == (date(2026, 11, 9), date(2026, 11, 16))
    r = await build_report(filled, week, TZ, threshold=55)
    assert (r.found_new, r.sent, r.agreed) == (4, 4, 1)
    prev = await build_report(filled, week.prev(), TZ, threshold=55)
    assert (prev.sent, prev.replied) == (1, 1)
    text = format_report(week, r, prev)
    assert "- Отправлено рассылок — 4 (▲3)" in text
    assert "- Получено ответов в чате — 1 (=)" in text
    assert "09.11–15.11.2026" in text


def test_format_exact_day():
    r = ReportData(found_new=3, sent=10, replied=2, callback=1, call1_done=2, test_agreed=1, call1_ignored=1,
                   agreed=1, thinking=1, refused=1)
    assert format_report(Period.day(date(2026, 11, 13)), r) == """📊 Отчёт за 13.11.2026

📍Общий
- Найдено новых лидов — 3
- Отправлено рассылок — 10
- Получено ответов в чате — 2
- Попросили созвон позже — 1

📍Звонок №1 (знакомство)
- Звонков совершено — 2
- Согласны на тест — 1
- Отказ / Игнор — 0 / 1

📍Звонок №2 (после теста)
- Назначено звонков — 0
- Звонков совершено — 0
- Отказ / Игнор / Перенос — 0 / 0 / 0

📍Результат дня
- Готовы сотрудничать — 1
- Думают / Перезвон — 1 / 1
- Отказ / Игнор — 1 / 0

📈 Конверсия: рассылка → ответ 20% · ответ → звонок 100% · звонок → сделка 50%"""


def test_format_zero():
    text = format_report(Period.month(date(2026, 11, 13)), ReportData())
    assert text.startswith("📊 Отчёт за ноябрь 2026")
    assert "рассылка → ответ — · ответ → звонок — · звонок → сделка —" in text


def test_periods():
    m = Period.month(date(2026, 12, 31))
    assert (m.start, m.end) == (date(2026, 12, 1), date(2027, 1, 1))
    assert m.prev().start == date(2026, 11, 1)
    assert Period.day(date(2026, 11, 13)).next().start == date(2026, 11, 14)


def test_parse_report_date():
    today = date(2026, 11, 13)
    assert parse_report_date("вчера", today) == date(2026, 11, 12)
    assert parse_report_date("2026-11-13", today) == today
    assert parse_report_date("13.11", today) == today
    assert parse_report_date("20.12", today) == date(2025, 12, 20)
    assert parse_report_date("31.02", today) is None
    assert parse_report_date("абв", today) is None


def test_triggers():
    tr = report_triggers("21:00", "sun", "last", TZ)
    start = datetime(2026, 11, 13, 12, tzinfo=TZ)
    assert tr["day"].get_next_fire_time(None, start) == datetime(2026, 11, 13, 21, 0, tzinfo=TZ)
    assert tr["week"].get_next_fire_time(None, start) == datetime(2026, 11, 15, 21, 1, tzinfo=TZ)
    assert tr["month"].get_next_fire_time(None, start) == datetime(2026, 11, 30, 21, 2, tzinfo=TZ)
