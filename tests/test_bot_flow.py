"""Сквозной прогон хендлеров бота на фейковой Telegram-сессии (без сети)."""
from datetime import datetime, timezone
from itertools import count

import pytest
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.methods import (
    AnswerCallbackQuery,
    EditMessageReplyMarkup,
    EditMessageText,
    SendMessage,
    TelegramMethod,
)
from aiogram.types import CallbackQuery, Chat, Message, Update, User

import app.main as main_mod
from app.audit import runner
from app.audit.fetcher import FetchResult
from app.config import Settings
from app.db import repo

ADMIN = 42
SITE = """<html><head><title>Газ</title></head><body>
<h1>Газификация частного дома</h1><p>Санкт-Петербург, ИНН 7802860317, тел. 8 (812) 903-57-72</p>
<div>© 2015</div></body></html>"""


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls: list[TelegramMethod] = []
        self.ids = count(100)

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        chat = Chat(id=ADMIN, type="private")
        now = datetime.now(timezone.utc)
        if isinstance(method, SendMessage):
            return Message(message_id=next(self.ids), date=now, chat=chat, text=method.text)
        if isinstance(method, (EditMessageText, EditMessageReplyMarkup)):
            return Message(message_id=method.message_id or 1, date=now, chat=chat,
                           text=getattr(method, "text", "") or "")
        if isinstance(method, AnswerCallbackQuery):
            return True
        return True

    async def close(self):
        pass

    async def stream_content(self, *a, **kw):  # pragma: no cover
        yield b""

    def texts(self):
        return [getattr(c, "text", "") or "" for c in self.calls if isinstance(c, (SendMessage, EditMessageText))]


@pytest.fixture
async def env(session_factory, monkeypatch):
    for mod in ("app.bot.common", "app.bot.handlers.add", "app.bot.handlers.leads",
                "app.bot.handlers.card_actions", "app.bot.handlers.start", "app.crm.reminders",
                "app.reports.service", "app.bot.handlers.discovery", "app.bot.handlers.ai_actions",
                "app.discovery.jobs"):
        monkeypatch.setattr(f"{mod}.SessionLocal", session_factory)
    monkeypatch.setattr(main_mod, "get_settings", lambda: Settings(ADMIN_IDS=str(ADMIN), BOT_TOKEN="1:x"))

    async def fake_fetch(url, internal_pages=2):
        return FetchResult(requested_url=url, final_url="http://gazsever.com/", status=200, html=SITE,
                           html_bytes=len(SITE), ttfb_ms=200)

    monkeypatch.setattr(runner, "fetch_site", fake_fetch)
    session = FakeSession()
    bot = Bot("1:x", session=session, default=DefaultBotProperties(parse_mode="HTML"))
    dp = main_mod.build_dispatcher()
    return dp, bot, session, session_factory


user = User(id=ADMIN, is_bot=False, first_name="T")
upd_ids = count(1)


def msg_update(text, uid=ADMIN):
    u = User(id=uid, is_bot=False, first_name="X")
    return Update(update_id=next(upd_ids), message=Message(
        message_id=next(upd_ids), date=datetime.now(timezone.utc),
        chat=Chat(id=uid, type="private"), from_user=u, text=text))


def cb_update(data, message_id=500):
    msg = Message(message_id=message_id, date=datetime.now(timezone.utc), chat=Chat(id=ADMIN, type="private"), text="card")
    return Update(update_id=next(upd_ids), callback_query=CallbackQuery(
        id=str(next(upd_ids)), from_user=user, chat_instance="c", message=msg, data=data))


async def test_flow(env):
    dp, bot, tg, factory = env
    feed = lambda u: dp.feed_update(bot, u)  # noqa: E731

    # чужой пользователь — тишина
    await feed(msg_update("/start", uid=7))
    assert tg.calls == []

    await feed(msg_update("/start"))
    await feed(msg_update("/add gazsever.com"))
    assert any("/100" in t and "Главное" in t for t in tg.texts()), tg.texts()

    async with factory() as s:
        c = await repo.get_company_by_domain(s, "gazsever.com")
        assert c and c.inn == "7802860317" and c.score is not None
    cid = c.id

    # повторное добавление — «уже в базе»
    await feed(msg_update("/add https://www.gazsever.com/"))
    assert any("уже в базе" in t for t in tg.texts())

    await feed(cb_update(f"c:st_SENT:{cid}:"))
    assert "Отправлено" in tg.texts()[-1]

    # ⏰ Позже → Завтра 10:00 → контакт → пропустить комментарий
    await feed(cb_update(f"c:later:{cid}:"))
    await feed(cb_update("d:tm10"))
    await feed(msg_update("Иван 8 911 123-45-67"))
    await feed(cb_update("f:skip"))
    assert any("Напомню" in t for t in tg.texts())

    # кнопка меню в середине сценария — выходит из сценария
    await feed(cb_update(f"c:note:{cid}:"))
    await feed(msg_update("📋 Лиды"))
    await feed(msg_update("просто текст"))
    assert "Выберите действие" in tg.texts()[-1]

    # ручная дата с ошибкой, потом корректная
    await feed(cb_update(f"c:later:{cid}:"))
    await feed(cb_update("d:manual"))
    await feed(msg_update("когда-нибудь"))
    assert "Не понял дату" in tg.texts()[-1]
    await feed(msg_update("20.12 11:00"))
    await feed(cb_update("f:skip"))
    await feed(cb_update("f:skip"))

    # 📞 Звонок №1 проведён → 🧪 Тест → заметка
    await feed(cb_update(f"c:call1:{cid}:"))
    await feed(cb_update(f"c:out1:{cid}:TEST_AGREED"))
    await feed(msg_update("Согласен на мини-аудит"))

    # заметка
    await feed(cb_update(f"c:note:{cid}:"))
    await feed(msg_update("Попросил КП на почту"))

    async with factory() as s:
        c = await repo.get_company(s, cid)
        assert c.status == "TEST_AGREED"
        assert c.contact_person == "Иван"
        assert "мини-аудит" in c.notes and "КП" in c.notes
        types = [e.type for e in await repo.company_events(s, cid)]
        assert types.count("call_logged") == 1 and "note" in types

    # остальные разделы не падают
    for t in ("/leads", "/leads новый", "/new", "/today", "🔥 Новые", "📞 Сегодня", "/find газ", "/find 9035772",
              "/help", "📊 Отчёт", "⚙️ Настройки"):
        await feed(msg_update(t))
    await feed(cb_update("l::0:1"))
    await feed(cb_update("l:TEST_AGREED:0:0"))
    await feed(cb_update(f"c:why:{cid}:"))
    await feed(cb_update(f"c:stmenu:{cid}:"))
    await feed(cb_update(f"c:set:{cid}:CALL2_SCHEDULED"))
    await feed(cb_update("d:w1"))
    await feed(cb_update(f"c:blacklist:{cid}:"))
    async with factory() as s:
        c = await repo.get_company(s, cid)
        assert c.status == "CANCELLED"
        assert "gazsever.com" in await repo.blacklisted_domains(s)
    assert not any("ничего не найдено" in t for t in tg.texts())
    n = len(tg.texts())
    await feed(msg_update("/find Газ"))  # один результат → сразу карточка
    assert "/100" in tg.texts()[n] and "gazsever.com" in tg.texts()[n]

    # отчёты: события этого прогона видны в отчёте за сегодня
    await feed(msg_update("/report"))
    rep = tg.texts()[-1]
    assert rep.startswith("📊 Отчёт за")
    assert "- Отправлено рассылок — 1" in rep
    assert "- Согласны на тест — 1" in rep
    assert "- Звонков совершено — 1" in rep
    for t in ("/week", "/month", "/report 2026-11-01", "/report абв", "📊 Отчёт"):
        await feed(msg_update(t))
    assert "Отчёт за ноябрь" in tg.texts()[-5] or "Отчёт за" in tg.texts()[-5]
    await feed(cb_update("rp:pick:"))
    await feed(msg_update("вчера"))
    assert tg.texts()[-1].startswith("📊 Отчёт за")
    await feed(cb_update("rp:week:20261113"))
    assert "неделю 09.11–15.11.2026" in tg.texts()[-1]
    await feed(cb_update("rp:month:20261001"))
    assert "октябрь 2026" in tg.texts()[-1]

    # этап 3: запросы, ниши, расход, запуск поиска (экран подтверждения)
    async with factory() as s:
        from app.db.models import SearchQuery
        niche = await repo.get_niche_by_slug(s, "gas")
        s.add(SearchQuery(niche_id=niche.id, text="газификация спб"))
        await s.commit()
    await feed(msg_update("/queries"))
    assert "Запросы ниши «Газификация»" in tg.texts()[-1] and "активных 1 из 1" in tg.texts()[-1]
    await feed(cb_update("q:add:0:0"))
    await feed(msg_update("газгольдер спб" + chr(10) + "монтаж газопровода спб" + chr(10) + "газгольдер спб"))
    assert "Добавлено запросов: 2" in tg.texts()[-2]
    async with factory() as s:
        qs = await repo.niche_queries(s, niche.id)
    await feed(cb_update(f"q:toggle:{qs[0].id}:0"))
    assert "активных 2 из 3" in tg.texts()[-1]
    await feed(cb_update(f"q:del:{qs[1].id}:0"))
    assert "активных 1 из 2" in tg.texts()[-1]
    await feed(msg_update("/niche"))
    await feed(cb_update("ni:new:0"))
    await feed(msg_update("Металлообработка"))
    await feed(msg_update("металлообработ, фрезер"))
    assert "создана и сделана активной" in tg.texts()[-1]
    async with factory() as s:
        n = await repo.get_active_niche(s)
        assert n.slug == "metalloobrabotka" and n.keywords == ["металлообработ", "фрезер"]
    await feed(cb_update(f"ni:use:{niche.id}"))
    await feed(msg_update("/usage"))
    assert "Yandex Search API" in tg.texts()[-1] and "DeepSeek" in tg.texts()[-1]
    await feed(msg_update("/run"))
    assert "Поиск по нише «Газификация»" in tg.texts()[-1] or "Режим отложенных" in tg.texts()[-1]
    # письмо: выбор канала на карточке
    await feed(cb_update(f"c:letter:{cid}:"))
