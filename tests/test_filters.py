import pytest

from app.discovery.filters import extract_urls, is_blacklisted, normalize_domain


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("https://www.GazSever.com/contacts", "gazsever.com"),
        ("gaz-proekt-spb.ru", "gaz-proekt-spb.ru"),
        ("spb.avito.ru/uslugi", "avito.ru"),
        ("company.spb.ru", "company.spb.ru"),
        ("http://sub.example.co.uk", "example.co.uk"),
        # хостинги и конструкторы: каждый поддомен — отдельный сайт
        ("https://ci697493.tw1.ru/#", "ci697493.tw1.ru"),
        ("http://www.CI697493.tw1.ru/contacts", "ci697493.tw1.ru"),
        ("gazmaster.tilda.ws", "gazmaster.tilda.ws"),
        ("https://gaz-spb.nethouse.ru/", "gaz-spb.nethouse.ru"),
        ("tw1.ru", "tw1.ru"),               # сам домен платформы без поддомена
        ("www.timeweb.ru", "timeweb.ru"),   # www — не отдельный сайт
        ("not a domain", None),
        ("localhost", None),
    ],
)
def test_normalize(raw, expected):
    assert normalize_domain(raw) == expected


def test_blacklist():
    assert is_blacklisted("avito.ru")
    assert is_blacklisted("gov.spb.ru")
    assert is_blacklisted("gazprom-mrg.ru")
    assert is_blacklisted("peterburggaz.ru")
    assert not is_blacklisted("gazsever.com")
    assert is_blacklisted("mysite.ru", extra={"mysite.ru"})


def test_platform_subdomains_are_distinct():
    a, b = normalize_domain("https://ci697493.tw1.ru/"), normalize_domain("https://ci111111.tw1.ru/")
    assert a != b
    assert extract_urls("https://ci697493.tw1.ru/# ci111111.tw1.ru") == ["https://ci697493.tw1.ru/#", "ci111111.tw1.ru"]
    # чёрный список по поддомену не задевает соседей на той же платформе
    assert is_blacklisted("ci697493.tw1.ru", extra={"ci697493.tw1.ru"})
    assert not is_blacklisted("ci111111.tw1.ru", extra={"ci697493.tw1.ru"})


def test_extract_urls():
    text = "gazsever.com\nhttps://gaz-proekt-spb.ru/, www.gazsever.com; просто текст"
    assert extract_urls(text) == ["gazsever.com", "https://gaz-proekt-spb.ru/"]
