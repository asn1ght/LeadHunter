"""Поиск лидов (/find): регистр, транслит, порядок слов, опечатки, телефоны, ИНН."""
import pytest

from app.crm.search import Doc, normalize, score, search
from app.db import repo

DOCS = [
    Doc(id=1, name="Газ Проект СПБ", domain="gaz-proekt-spb.ru", phones=("+79819793144",), emails=("info@gaz-proekt-spb.ru",)),
    Doc(id=2, name="ГазСевер", domain="gazsever.com", inn="7802860317", phones=("+78129035772",),
        address="г. Санкт-Петербург, Пулковское ш. 30к4", notes="[14.11 10:00] Попросил КП на почту"),
    Doc(id=3, name="Петербургская Газовая Компания", domain="pgk-spb.ru", contact="Иван Петров"),
    Doc(id=4, name="Металлообработка Плюс", domain="metall-plus.ru"),
]


def ids(q):
    return [i for i, _ in search(q, DOCS)]


@pytest.mark.parametrize(
    "q,first",
    [
        ("газ проект спб", 1),     # случай из жалобы
        ("ГАЗ ПРОЕКТ СПБ", 1),
        ("спб газ проект", 1),     # другой порядок слов
        ("газ-проект", 1),
        ("gaz proekt", 1),         # латиницей
        ("gaz-proekt-spb.ru", 1),  # домен целиком
        ("газпроект", 1),          # слитно
        ("газсевер", 2),
        ("gazsever", 2),
        ("север", 2),              # часть слитного названия
        ("Газ Севр", 2),           # опечатка
        ("пулковское", 2),         # адрес
        ("КП на почту", 2),        # заметка
        ("иван", 3),               # контакт
        ("петербургская газовая", 3),
        ("7802860317", 2),         # ИНН
        ("903-57-72", 2),          # хвост телефона
        ("8 981 979 31 44", 1),    # телефон в другом формате
        ("info@gaz-proekt-spb.ru", 1),
    ],
)
def test_finds(q, first):
    res = ids(q)
    assert res and res[0] == first, (q, res)


def test_ranking_and_no_match():
    assert ids("газ")[:2] == [1, 2] or set(ids("газ")[:2]) == {1, 2}
    assert 4 not in ids("газ")
    assert ids("бетон") == []
    assert ids("газ бетон") == []  # каждое слово должно найтись
    assert ids("") == []
    assert ids("12345") == []


def test_exact_name_wins():
    docs = [Doc(id=1, name="Газ Проект СПБ Плюс"), Doc(id=2, name="Газ Проект СПБ")]
    assert search("газ проект спб", docs)[0][0] == 2


def test_normalize():
    assert normalize("ООО «Газ-Проект»  Ёлка") == "ооо газ проект елка"
    assert score("ооо газ проект", DOCS[0]) > 0  # «ООО» игнорируется


async def test_repo_find_cyrillic(session):
    niche = await repo.get_niche_by_slug(session, "gas")
    await repo.create_company(session, domain="gaz-proekt-spb.ru", url="gaz-proekt-spb.ru", niche_id=niche.id,
                              name="Газ Проект СПБ", phones=["+79819793144"])
    await repo.create_company(session, domain="gazsever.com", url="gazsever.com", niche_id=niche.id, name="ГазСевер")
    await session.commit()
    found = await repo.find_companies(session, "газ проект спб")
    assert [c.domain for c in found] == ["gaz-proekt-spb.ru"]
    assert [c.domain for c in await repo.find_companies(session, "ГАЗСЕВЕР")] == ["gazsever.com"]
