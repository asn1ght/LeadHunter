from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.utils.dates import parse_human_datetime

TZ = ZoneInfo("Europe/Moscow")
NOW = datetime(2026, 11, 13, 16, 0, tzinfo=TZ)  # пятница 16:00


def dt(m, d, h, mi=0, y=2026):
    return datetime(y, m, d, h, mi, tzinfo=TZ)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("завтра в 12", dt(11, 14, 12)),
        ("Завтра 12:30", dt(11, 14, 12, 30)),
        ("послезавтра", dt(11, 15, 10)),
        ("пт 14:30", dt(11, 20, 14, 30)),  # сегодня пятница, 14:30 уже прошло
        ("пт 17:00", dt(11, 13, 17)),
        ("в среду в 10", dt(11, 18, 10)),
        ("понедельник в 11", dt(11, 16, 11)),
        ("20.11 11:00", dt(11, 20, 11)),
        ("20.11", dt(11, 20, 10)),
        ("20.11.2026 9:15", dt(11, 20, 9, 15)),
        ("01.02", dt(2, 1, 10, y=2027)),
        ("14:30", dt(11, 14, 14, 30)),
        ("18:00", dt(11, 13, 18)),
        ("через 2 часа", dt(11, 13, 18)),
        ("через 3 дня", dt(11, 16, 10)),
        ("через неделю", dt(11, 20, 10)),
        ("завтра в 7 вечера", dt(11, 14, 19)),
    ],
)
def test_parse(text, expected):
    assert parse_human_datetime(text, NOW) == expected


@pytest.mark.parametrize("text", ["", "абв", "32.13 10:00", "25:00", "позвонить"])
def test_parse_invalid(text):
    assert parse_human_datetime(text, NOW) is None
