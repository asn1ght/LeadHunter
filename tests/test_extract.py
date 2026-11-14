from selectolax.lexbor import LexborHTMLParser

from app.audit.extract import (
    detect_name,
    detect_segment,
    extract,
    find_emails,
    find_inn,
    find_ogrn,
    find_phones,
    format_phone,
    inn_is_valid,
    normalize_phone,
)


def test_phones():
    text = "Звоните: +7 (812) 903-57-72, 8 921 123 45 67, 8-800-555-35-35; факс 903-57-72; ИНН 7802860317"
    assert find_phones(text) == ["+78129035772", "+79211234567", "+78005553535"]
    assert normalize_phone("tel:+78129035772") == "+78129035772"
    assert normalize_phone("12345") is None
    assert format_phone("+78129035772") == "+7 812 903-57-72"


def test_inn():
    assert inn_is_valid("7707083893")
    assert inn_is_valid("7802860317")
    assert inn_is_valid("500100732259")
    assert not inn_is_valid("7707083894")
    assert not inn_is_valid("1234567890")
    assert find_inn("ООО «Ромашка», ИНН: 7707083893, КПП 773601001") == "7707083893"
    assert find_inn("ИНН 1234567890") is None
    assert find_inn("Телефон 7707083893") is None  # без слова ИНН не берём


def test_ogrn():
    assert find_ogrn("ОГРН 1027700132195") == "1027700132195"
    assert find_ogrn("ОГРН 1027700132196") is None


def test_emails():
    assert find_emails("Пишите на Info@GazSever.com или logo@2x.png") == ["info@gazsever.com"]


def test_segment():
    assert detect_segment("Газификация частного дома и дачи") == "b2c"
    assert detect_segment("Газоснабжение промышленных объектов, работа с застройщиками") == "b2b"
    assert detect_segment("Мы работаем с 2005 года") == "unknown"


def test_name_priority():
    og = LexborHTMLParser('<meta property="og:site_name" content="ГазСевер"><title>Газ | Другое</title>')
    assert detect_name(og, "") == "ГазСевер"
    ld = LexborHTMLParser(
        "<title>Автономная газификация в Санкт-Петербурге от 120 т. р.</title>"
        '<script type="application/ld+json">{"@graph":[{"@type":"Organization","name":"ГазСевер"}]}</script>'
    )
    assert detect_name(ld, "") == "ГазСевер"
    title = LexborHTMLParser("<title>Монтаж газопровода в СПб | ГазМастер</title>")
    assert detect_name(title, "") == "ГазМастер"
    logo = LexborHTMLParser(
        '<title>Проектирование газопроводов в Санкт-Петербурге.</title><img class="logo" alt="Газ Проект СПБ">'
    )
    assert detect_name(logo, "") == "Газ Проект СПБ"


def test_extract_pages():
    main = """<html><head><title>ГазСевер | Газификация</title></head><body>
    <a href="tel:+78129035772">Позвонить</a> <a href="https://wa.me/79210000000">WA</a>
    <p>Газификация частного дома в Санкт-Петербурге</p></body></html>"""
    contacts = "<body>ИНН 7802860317. Адрес: г. Санкт-Петербург, ул. Ленина, д. 5. info@gazsever.com</body>"
    ex = extract(
        [("https://gazsever.com", main), ("https://gazsever.com/contacts", contacts)],
        ["санкт-петербург"],
        ["газ"],
    )
    assert ex.phones[0] == "+78129035772"
    assert ex.inn == "7802860317"
    assert ex.emails == ["info@gazsever.com"]
    assert ex.messengers == ["WhatsApp"]
    assert ex.segment == "b2c"
    assert ex.region_detected == "СПб/ЛО"
    assert ex.niche_match is True
    assert ex.address and "Ленина" in ex.address
