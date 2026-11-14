"""Чекко: разбор ответа, формулировки, обогащение с учётом лимита, карточка, поиск ИНН на сайте."""
from datetime import date

import pytest
from sqlalchemy import select

from app.audit.extract import find_inn
from app.audit.fetcher import pick_internal_links
from app.bot.cards import render_card, render_requisites
from app.db import repo
from app.db.models import ApiUsage
from app.discovery.runner import month_requests
from app.integrations import checko

COMPANY = {
    "ОГРН": "1127847000000", "ИНН": "7802860317", "НаимСокр": 'ООО "ГАЗ ПРОЕКТ"',
    "НаимПолн": 'ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ "ГАЗ ПРОЕКТ"', "ДатаРег": "2012-03-21",
    "Статус": {"Код": "001", "Наим": "Действует"}, "ЮрАдрес": {"АдресРФ": "г. Санкт-Петербург, ул. Газовая, д. 1"},
    "ОКВЭД": {"Код": "43.22", "Наим": "Работы по монтажу систем газоснабжения"}, "СЧР": 14,
    "УстКап": {"Сумма": 10000}, "РМСП": {"Кат": "МИКРОПРЕДПРИЯТИЕ"}, "Налоги": {"ОсобРежим": ["УСН"], "СумНедоим": "0.00"},
    "Руковод": [{"ФИО": "Романов Алексей Владимирович", "ИНН": "602507797263", "НаимДолжн": "ГЕНЕРАЛЬНЫЙ ДИРЕКТОР",
                 "ДатаЗаписи": "2012-03-21"}],
    "Учред": {"ФЛ": [{"ФИО": "Романов Алексей Владимирович", "Доля": {"Процент": 100}}]},
    "ЕФРСБ": [], "НедобПост": False, "МассРуковод": False,
}
FINANCES = {
    "data": {"2024": {"2110": 48_200_000, "2400": 1_000_000}, "2025": {"2110": 45_800_000, "2400": 1_200_000}},
    "bo.nalog.ru": {"Отчет": {"2025": "https://bo.nalog.gov.ru/download/bfo/pdf/1"}},
}
ENTREPRENEUR = {"ОГРНИП": "312470000000000", "ИНН": "602507797263", "ФИО": "Иванов Иван Иванович",
                "ДатаРег": "2020-01-10", "Статус": {"Наим": "Действует"}, "Налоги": {"ОсобРежим": ["ПСН"]}}


class FakeClient:
    def __init__(self, company=COMPANY):
        self.calls = []
        self.company = company

    async def get(self, method, **params):
        self.calls.append((method, params["inn"]))
        if method == "company":
            return {"data": self.company, "meta": {"status": "ok"}}
        if method == "finances":
            return {**FINANCES, "meta": {"status": "ok"}}
        return {"data": ENTREPRENEUR, "meta": {"status": "ok"}}


def test_formatting():
    assert checko.money(45_800_000) == "45,8 млн ₽"
    assert checko.money(1_230_000_000) == "1,2 млрд ₽"
    assert checko.money(850_000) == "850 тыс. ₽"
    assert checko.money(-300_000) == "−300 тыс. ₽"
    assert checko.money(400) == "400 ₽"
    assert checko.human_date("2012-03-21") == "21 марта 2012 г."
    assert checko.age("2012-03-21", date(2026, 11, 14)) == "14 лет"
    assert checko.age("2025-03-21", date(2026, 11, 14)) == "1 год"
    assert checko.first_names("Романов Алексей Владимирович") == "Алексей Владимирович"


def test_summary_and_revenue():
    s = checko.summarize_company(COMPANY, FINANCES)
    assert s["name"] == "ООО «ГАЗ ПРОЕКТ»" and s["active"] and s["employees"] == 14
    assert s["director"]["post"] == "Генеральный директор" and s["director"]["since"] == "2012-03-21"
    fin = s["finance"]
    assert fin["year"] == 2025
    assert checko.revenue_line(fin) == "Выручка снизилась до 45,8 млн ₽ (📉 −5%)"
    assert checko.profit_line(fin) == "Чистая прибыль 1,2 млн ₽ (+20%)"
    assert checko.revenue_line({"revenue": 0}) == "Выручка 0 ₽ — нулевая отчётность"
    # ФНС не передаёт нулевые строки: берём последний год, отсутствие 2110 = 0
    zero = checko.summarize_finances({"data": {"2018": {"2110": 5}, "2024": {"1600": 10000}}})
    assert (zero["year"], zero["revenue"], zero["revenue_prev"]) == (2024, 0, None)
    assert checko.profit_line(zero) is None
    assert "выросла" in checko.revenue_line({"revenue": 110, "revenue_prev": 100})

    closed = checko.summarize_company({**COMPANY, "Статус": {"Наим": "Ликвидирована"},
                                       "Ликвид": {"Дата": "2023-05-01"}, "ЕФРСБ": [{"x": 1}]})
    assert not closed["active"] and "банкротстве" in closed["risks"][0]
    assert checko.status_line(closed).startswith("⛔ Ликвидирована с 1 мая 2023 г.")


async def test_enrich_and_limits(session_factory):
    async with session_factory() as s:
        c = await repo.create_company(s, domain="gaz.ru", url="gaz.ru", inn="7802860317")
        await repo.set_setting(s, "CHECKO_DAILY_LIMIT", "3")
        await s.commit()
    client = FakeClient()
    summary = await checko.enrich_company(session_factory, c.id, client=client)
    assert client.calls == [("company", "7802860317"), ("finances", "7802860317")]
    assert summary["finance"]["revenue"] == 45_800_000

    # данные свежие — повторно не запрашиваем
    await checko.enrich_company(session_factory, c.id, client=client)
    assert len(client.calls) == 2
    # принудительное обновление упирается в лимит дня (2 из 3, нужно ещё 2)
    with pytest.raises(checko.CheckoLimit):
        await checko.enrich_company(session_factory, c.id, client=client, force=True)

    async with session_factory() as s:
        assert await checko.requests_today(s) == 2
        assert await month_requests(s) == 0  # лимит Яндекса Чекко не съедает
        company = await repo.get_company(s, c.id)
        assert company.name == "ООО «ГАЗ ПРОЕКТ»"
        card = render_card(company, None, threshold=55)
        assert "🏛 <b>ООО «ГАЗ ПРОЕКТ»</b> · ИНН 7802860317" in card
        assert "👔 Генеральный директор — Романов Алексей Владимирович, с 21.03.2012" in card
        assert "📊 2025: Выручка снизилась до 45,8 млн ₽ (📉 −5%) · чистая прибыль 1,2 млн ₽ (+20%)" in card
        assert "👋 Обращение к руководителю: Алексей Владимирович" in card
        detail = render_requisites(company)
        assert "📊 <b>Финансовая отчётность за 2025 год</b>" in detail
        assert "Романов Алексей Владимирович\nИНН 602507797263\nс 21 марта 2012 г." in detail
        assert "🛡 Рисков не найдено" in detail


async def test_entrepreneur_and_not_found(session_factory):
    async with session_factory() as s:
        ip = await repo.create_company(s, domain="ip.ru", url="ip.ru", inn="602507797263")
        nf = await repo.create_company(s, domain="nf.ru", url="nf.ru", inn="7802860317")
        await s.commit()
    client = FakeClient()
    s_ip = await checko.enrich_company(session_factory, ip.id, client=client)
    assert client.calls == [("entrepreneur", "602507797263")]
    assert s_ip["name"] == "ИП Иванов Иван Иванович" and s_ip["finance"] is None

    empty = FakeClient(company={})
    s_nf = await checko.enrich_company(session_factory, nf.id, client=empty)
    assert s_nf["not_found"] and empty.calls == [("company", "7802860317")]  # без запроса финансов
    async with session_factory() as s:
        assert "не найден" in render_card(await repo.get_company(s, nf.id), None, threshold=55)
        assert len((await s.execute(select(ApiUsage))).scalars().all()) == 2


def test_find_inn_variants():
    assert find_inn("ИНН/КПП 7802860317/780201001") == "7802860317"
    assert find_inn("ИНН: 7802860317") == "7802860317"
    assert find_inn("И.Н.Н. 7802860317") == "7802860317"
    assert find_inn("ИНН 7802860317") == "7802860317"
    assert find_inn("тел. 7802860317") is None


def test_internal_links_priority():
    html = """<a href="/uslugi">Услуги</a><a href="/price">Цены</a><a href="/about">О нас</a>
    <a href="/contacts">Контакты</a><a href="/rekvizity">Реквизиты</a>"""
    assert pick_internal_links(html, "https://gaz.ru/") == [
        "https://gaz.ru/rekvizity", "https://gaz.ru/contacts", "https://gaz.ru/uslugi"]
