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


def test_extract_urls():
    text = "gazsever.com\nhttps://gaz-proekt-spb.ru/, www.gazsever.com; просто текст"
    assert extract_urls(text) == ["gazsever.com", "https://gaz-proekt-spb.ru/"]
