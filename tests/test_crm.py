from datetime import timedelta

from app.config import Settings
from app.crm import reminders as reminders_mod
from app.crm import service
from app.crm.statuses import Status
from app.db import repo
from app.db.models import utcnow


async def _company(session, domain="gazsever.com"):
    niche = await repo.get_niche_by_slug(session, "gas")
    c = await repo.create_company(session, domain=domain, url=domain, niche_id=niche.id, status="NEW")
    await session.commit()
    return c


async def test_change_status_writes_event(session):
    c = await _company(session)
    await service.change_status(session, c, Status.SENT)
    await session.commit()
    events = await repo.company_events(session, c.id)
    assert c.status == "SENT"
    assert [(e.type, e.from_status, e.to_status) for e in events] == [("status_change", "NEW", "SENT")]


async def test_full_pipeline(session):
    """NEW → SENT → REPLIED → звонок №1 (тест) → звонок №2 назначен → перенос → проведён (согласились)."""
    c = await _company(session)
    await service.change_status(session, c, Status.SENT)
    await service.change_status(session, c, Status.REPLIED)
    await service.log_call(session, c, 1, "TEST_AGREED", note="Готов к мини-аудиту")
    assert c.status == "TEST_AGREED"
    due = utcnow() + timedelta(days=2)
    await service.schedule(session, c, due, kind="call2", set_status=Status.CALL2_SCHEDULED)
    assert c.status == "CALL2_SCHEDULED"
    assert c.next_action_at == due
    await service.log_call(session, c, 2, "RESCHEDULED")
    await service.schedule(session, c, due + timedelta(days=1), kind="call2", set_status=Status.CALL2_SCHEDULED)
    assert c.status == "CALL2_SCHEDULED"
    await service.log_call(session, c, 2, "AGREED")
    await session.commit()

    assert c.status == "AGREED"
    assert c.next_action_at is None
    assert "Готов к мини-аудиту" in c.notes
    events = await repo.company_events(session, c.id)
    status_path = [e.to_status for e in events if e.type == "status_change"]
    assert status_path == ["SENT", "REPLIED", "TEST_AGREED", "CALL2_SCHEDULED", "AGREED"]
    calls = [(e.payload["stage"], e.payload["outcome"]) for e in events if e.type == "call_logged"]
    assert calls == [(1, "TEST_AGREED"), (2, "RESCHEDULED"), (2, "AGREED")]
    assert await repo.open_reminders_before(session, utcnow() + timedelta(days=30)) == []


async def test_callback_with_contact(session):
    c = await _company(session)
    due = utcnow() + timedelta(hours=3)
    await service.schedule(
        session, c, due, kind="callback", contact="Иван +7 911 123-45-67", comment="после обеда",
        set_status=Status.CALLBACK,
    )
    await session.commit()
    assert c.status == "CALLBACK"
    assert c.contact_person == "Иван"
    assert c.contact_phone == "+79111234567"
    rem = await repo.open_reminders_before(session, due + timedelta(minutes=1))
    assert len(rem) == 1 and rem[0].text == "Иван +7 911 123-45-67 · после обеда"
    # новое напоминание заменяет старое
    await service.schedule(session, c, due + timedelta(days=1), kind="callback")
    await session.commit()
    assert len(await repo.open_reminders_before(session, due + timedelta(days=2))) == 1


async def test_note(session):
    c = await _company(session)
    await service.add_note(session, c, "первая")
    await service.add_note(session, c, "вторая")
    await session.commit()
    assert c.notes.count("\n") == 1 and c.notes.endswith("вторая")
    assert [e.type for e in await repo.company_events(session, c.id)] == ["note", "note"]


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text, kw.get("reply_markup")))


async def test_check_reminders(session_factory, monkeypatch):
    monkeypatch.setattr(reminders_mod, "SessionLocal", session_factory)
    monkeypatch.setattr("app.bot.common.SessionLocal", session_factory)
    monkeypatch.setattr(reminders_mod, "get_settings", lambda: Settings(ADMIN_IDS="42"))

    async with session_factory() as s:
        c = await _company(s)
        await service.schedule(s, c, utcnow() - timedelta(minutes=1), kind="callback", comment="перезвонить",
                               set_status=Status.CALLBACK)
        c2 = await _company(s, "other.ru")
        await service.schedule(s, c2, utcnow() + timedelta(minutes=10), kind="call2",
                               set_status=Status.CALL2_SCHEDULED)
        await s.commit()

    bot = FakeBot()
    await reminders_mod.check_reminders(bot)
    assert len(bot.sent) == 2
    texts = [t for _, t, _ in bot.sent]
    assert any("Напоминание: созвониться" in t and "перезвонить" in t for t in texts)
    assert any("звонок №2" in t for t in texts)
    assert all(chat == 42 for chat, _, _ in bot.sent)

    # повторный запуск ничего не шлёт
    await reminders_mod.check_reminders(bot)
    assert len(bot.sent) == 2

    async with session_factory() as s:
        evs = await repo.company_events(s, c.id)
        assert evs[-1].type == "reminder_fired"
