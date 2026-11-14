"""Надёжность: сетевые сбои Яндекса, фоновый /run, один поиск за раз, статус «отправлено»,
контакты на внутренних страницах, ручной ИНН, доставка напоминаний."""
from datetime import timedelta

import httpx
import pytest

from app.audit import runner as audit_runner
from app.audit.fetcher import FetchResult
from app.audit.tech_checks import run_checks
from app.bot.handlers import discovery as disc_handlers
from app.config import Settings
from app.crm import reminders as reminders_mod
from app.crm import service
from app.crm.statuses import Status
from app.db import repo
from app.db.models import utcnow
from app.discovery import jobs
from app.discovery import yandex_search as ys
from app.discovery.runner import RunStats


class FakeBot:
    def __init__(self, fail_times: int = 0):
        self.sent: list[str] = []
        self.fail_times = fail_times

    async def send_message(self, chat_id, text, **kw):
        if self.fail_times:
            self.fail_times -= 1
            raise RuntimeError("telegram down")
        self.sent.append(text)


async def _nosleep(*a, **kw):
    return None


# ---------------------------------------------------------------- 1. сеть Яндекса


def _client(handler, monkeypatch):
    monkeypatch.setattr(ys.asyncio, "sleep", _nosleep)
    c = ys.YandexSearchClient("key", "folder", 2)
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return c


async def test_yandex_connect_error_retried_then_network_error(monkeypatch):
    calls = []

    def handler(request):
        calls.append(1)
        raise httpx.ConnectError("no route")

    c = _client(handler, monkeypatch)
    with pytest.raises(ys.YandexNetworkError):
        await c.search("газ")
    assert len(calls) == 3  # соединения не было — повтор безопасен


async def test_yandex_timeout_not_retried(monkeypatch):
    calls = []

    def handler(request):
        calls.append(1)
        raise httpx.ReadTimeout("slow")

    c = _client(handler, monkeypatch)
    with pytest.raises(ys.YandexNetworkError, match="таймаут"):
        await c.search("газ")
    assert len(calls) == 1  # запрос мог дойти и быть оплачен — не дублируем
    with pytest.raises(ys.YandexNetworkError):
        await c.get_operation("op1")


async def test_scheduled_discovery_crash_is_reported(monkeypatch):
    async def boom(*a, **kw):
        raise RuntimeError("db is gone")

    monkeypatch.setattr(jobs, "discovery_job", boom)
    sent = []

    async def fake_notify(bot, text):
        sent.append(text)

    monkeypatch.setattr(jobs, "notify_admins", fake_notify)
    await jobs.scheduled_discovery(FakeBot())
    assert sent and "Ночной поиск упал" in sent[0] and "db is gone" in sent[0]


# ---------------------------------------------------------------- 2–3. /run и блокировка


async def test_manual_run_reports_crash(monkeypatch):
    async def boom(*a, **kw):
        raise RuntimeError("oops")

    monkeypatch.setattr(disc_handlers, "discovery_job", boom)
    bot = FakeBot()
    await disc_handlers.run_and_report(bot, 1)
    assert bot.sent and "Поиск прервался" in bot.sent[0] and "oops" in bot.sent[0]


async def test_one_discovery_at_a_time(monkeypatch):
    async with jobs.discovery_lock:
        stats = await jobs.discovery_job(FakeBot())
        assert stats.stopped_reason == jobs.BUSY
        bot = FakeBot()
        await disc_handlers.run_and_report(bot, 1)
        assert "уже идёт" in bot.sent[0]

    # опрос отложенных запросов во время прогона тоже не стартует
    called = []

    async def fake_poll(*a, **kw):
        called.append(1)
        return RunStats()

    monkeypatch.setattr(jobs, "poll_operations", fake_poll)
    monkeypatch.setattr(jobs, "make_search_client", lambda: _DummyClient())
    async with jobs.discovery_lock:
        await jobs.poll_job(FakeBot())
    assert called == []
    await jobs.poll_job(FakeBot())
    assert called == [1]


class _DummyClient:
    async def aclose(self):
        return None


# ---------------------------------------------------------------- 6. «отправлено» не откатывает статус


async def test_mark_sent_does_not_roll_back(session):
    c = await repo.create_company(session, domain="a.ru", url="a.ru", status=Status.NEW.value)
    assert await service.mark_sent(session, c) and c.status == Status.SENT.value
    assert not await service.mark_sent(session, c)  # уже отправлено
    await service.change_status(session, c, Status.TEST_AGREED)
    assert not await service.mark_sent(session, c)
    assert c.status == Status.TEST_AGREED.value
    c2 = await repo.create_company(session, domain="b.ru", url="b.ru", status=Status.IGNORED.value)
    assert await service.mark_sent(session, c2)  # повторная рассылка после игнора — можно


# ---------------------------------------------------------------- 7. контакты на внутренних страницах

MAIN = "<html><head><title>Газификация домов в СПб под ключ</title></head><body><h1>Газ</h1></body></html>"
CONTACTS = '<html><body><a href="tel:+78121234567">звонок</a><a href="https://wa.me/7812">WhatsApp</a></body></html>'


def test_contacts_on_internal_pages_count():
    fetch = FetchResult(requested_url="http://a.ru", final_url="http://a.ru/", status=200, html=MAIN,
                        html_bytes=len(MAIN), extra_pages=[("http://a.ru/contacts", CONTACTS)])
    codes = {i.code for i in run_checks(fetch).issues}
    assert "no_messengers" not in codes and "no_tel_link" not in codes
    assert "no_tel_link_main" in codes  # на главной номер всё же не кликабельный — мягкое замечание

    alone = FetchResult(requested_url="http://a.ru", final_url="http://a.ru/", status=200, html=MAIN,
                        html_bytes=len(MAIN))
    codes = {i.code for i in run_checks(alone).issues}
    assert {"no_messengers", "no_tel_link"} <= codes


# ---------------------------------------------------------------- 8. ручной ИНН


async def test_manual_inn_survives_reaudit(session_factory, monkeypatch):
    site = "<html><body><h1>Газификация</h1><p>СПб, ИНН 7802860317</p></body></html>"

    async def fake_fetch(url, internal_pages=3):
        return FetchResult(requested_url=url, final_url="http://a.ru/", status=200, html=site,
                           html_bytes=len(site), ttfb_ms=100)

    monkeypatch.setattr(audit_runner, "fetch_site", fake_fetch)
    async with session_factory() as s:
        c = await repo.create_company(s, domain="a.ru", url="a.ru", inn="602507797263",
                                      tags=[audit_runner.MANUAL_INN_TAG])
        plain = await repo.create_company(s, domain="b.ru", url="b.ru")
        await s.commit()
    await audit_runner.audit_company(session_factory, c.id)
    await audit_runner.audit_company(session_factory, plain.id)
    async with session_factory() as s:
        assert (await repo.get_company(s, c.id)).inn == "602507797263"
        assert audit_runner.MANUAL_INN_TAG in (await repo.get_company(s, c.id)).tags
        assert (await repo.get_company(s, plain.id)).inn == "7802860317"  # без тега — как раньше, с сайта


# ---------------------------------------------------------------- 9. доставка напоминаний


async def test_reminder_retried_until_delivered(session_factory, monkeypatch):
    monkeypatch.setattr(reminders_mod, "SessionLocal", session_factory)
    monkeypatch.setattr("app.bot.common.SessionLocal", session_factory)
    monkeypatch.setattr(reminders_mod, "get_settings", lambda: Settings(ADMIN_IDS="42"))
    async with session_factory() as s:
        c = await repo.create_company(s, domain="a.ru", url="a.ru")
        await service.schedule(s, c, utcnow() - timedelta(minutes=1), kind="remind", comment="позвонить")
        await s.commit()

    down = FakeBot(fail_times=1)
    await reminders_mod.check_reminders(down)
    assert down.sent == []
    async with session_factory() as s:
        assert len(await repo.due_reminders(s, utcnow())) == 1  # не потеряно

    up = FakeBot()
    await reminders_mod.check_reminders(up)
    assert len(up.sent) == 1 and "позвонить" in up.sent[0]
    await reminders_mod.check_reminders(up)
    assert len(up.sent) == 1  # больше не повторяется


async def test_reminder_given_up_after_a_day(session_factory, monkeypatch):
    monkeypatch.setattr(reminders_mod, "SessionLocal", session_factory)
    monkeypatch.setattr("app.bot.common.SessionLocal", session_factory)
    monkeypatch.setattr(reminders_mod, "get_settings", lambda: Settings(ADMIN_IDS="42"))
    async with session_factory() as s:
        c = await repo.create_company(s, domain="a.ru", url="a.ru")
        await service.schedule(s, c, utcnow() - timedelta(hours=25), kind="remind")
        await s.commit()
    await reminders_mod.check_reminders(FakeBot(fail_times=10))
    async with session_factory() as s:
        assert await repo.due_reminders(s, utcnow()) == []
        ev = (await repo.company_events(s, c.id))[-1]
        assert ev.type == "reminder_fired" and ev.payload.get("undelivered") is True

