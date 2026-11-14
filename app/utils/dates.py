"""Парсинг дат в свободной форме: «пт 14:30», «20.11 11:00», «завтра в 12»."""
from __future__ import annotations

import re
from datetime import datetime, time, timedelta, timezone

DEFAULT_TIME = time(10, 0)

_WEEKDAYS = {
    "пн": 0, "понедельник": 0, "понедельника": 0,
    "вт": 1, "вторник": 1, "вторника": 1,
    "ср": 2, "среда": 2, "среду": 2, "среды": 2,
    "чт": 3, "четверг": 3, "четверга": 3,
    "пт": 4, "пятница": 4, "пятницу": 4, "пятницы": 4,
    "сб": 5, "суббота": 5, "субботу": 5, "субботы": 5,
    "вс": 6, "воскресенье": 6, "воскресенья": 6,
}

_RELATIVE_DAYS = {"сегодня": 0, "завтра": 1, "послезавтра": 2}

_DATE_RE = re.compile(r"(?<!\d)(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?(?!\d)")
_IN_RE = re.compile(r"через\s+(\d+|пол)?\s*(минут\w*|мин|час\w*|ч|дн\w*|день|недел\w*)")


def parse_human_datetime(text: str, now: datetime) -> datetime | None:
    """Распарсить дату/время, введённые человеком.

    `now` — aware-datetime в часовом поясе владельца. Результат — aware в том же поясе.
    Без времени ставится 10:00. Дата без года в прошлом переносится на следующий год.
    Возвращает None, если ничего не распознано или время некорректно.
    """
    t = text.strip().lower().replace("ё", "е")
    t = re.sub(r"\s+", " ", t)
    if not t:
        return None
    tz = now.tzinfo or timezone.utc

    # «через 2 часа», «через 3 дня», «через неделю», «через полчаса»
    m = _IN_RE.search(t)
    if m:
        num_raw, unit = m.group(1), m.group(2)
        n = 0.5 if num_raw == "пол" else float(num_raw) if num_raw else 1
        if unit.startswith(("мин",)):
            return (now + timedelta(minutes=n)).replace(second=0, microsecond=0)
        if unit.startswith(("час", "ч")):
            return (now + timedelta(hours=n)).replace(second=0, microsecond=0)
        if unit.startswith(("дн", "день")):
            day = (now + timedelta(days=n)).date()
        else:  # неделя
            day = (now + timedelta(weeks=n)).date()
        rest = t[: m.start()] + t[m.end():]
        tm = _extract_time(rest) or DEFAULT_TIME
        return datetime.combine(day, tm, tz)
    if "полчаса" in t:
        return (now + timedelta(minutes=30)).replace(second=0, microsecond=0)

    day = None
    rest = t

    # явная дата 20.11 / 20.11.2026
    m = _DATE_RE.search(t)
    if m:
        d, mo = int(m.group(1)), int(m.group(2))
        y_raw = m.group(3)
        year = now.year
        if y_raw:
            year = int(y_raw) + (2000 if len(y_raw) == 2 else 0)
        try:
            day = datetime(year, mo, d).date()
        except ValueError:
            return None
        if not y_raw and day < now.date():
            day = day.replace(year=year + 1)
        rest = t[: m.start()] + " " + t[m.end():]
    else:
        for word, delta in _RELATIVE_DAYS.items():
            if re.search(rf"\b{word}\b", t):
                day = (now + timedelta(days=delta)).date()
                rest = re.sub(rf"\b{word}\b", " ", t)
                break
        else:
            for word, wd in sorted(_WEEKDAYS.items(), key=lambda kv: -len(kv[0])):
                if re.search(rf"\b{word}\b", t):
                    ahead = (wd - now.weekday()) % 7
                    day = (now + timedelta(days=ahead)).date()
                    rest = re.sub(rf"\b{word}\b", " ", t)
                    break

    tm = _extract_time(rest)
    if day is None and tm is None:
        return None
    if tm is None:
        tm = DEFAULT_TIME
    if day is None:
        day = now.date()
        if datetime.combine(day, tm, tz) <= now:
            day += timedelta(days=1)
        return datetime.combine(day, tm, tz)

    result = datetime.combine(day, tm, tz)
    # «пт 14:30» в пятницу после 14:30 — значит следующая пятница
    if result <= now and not m and not any(re.search(rf"\b{w}\b", t) for w in _RELATIVE_DAYS):
        result += timedelta(days=7)
    return result


def _extract_time(text: str) -> time | None:
    """Найти время «14:30», «в 12», «12.30» в остатке строки."""
    # приоритет — с минутами
    m = re.search(r"(?<!\d)(\d{1,2})[:.](\d{2})(?!\d)", text)
    if not m:
        m = re.search(r"(?:\bв\s*|^\s*|\s)(\d{1,2})(?:\s*(?:ч\w*|час\w*))?\s*$", text)
        if not m:
            m = re.search(r"\bв\s*(\d{1,2})\b", text)
        if not m:
            return None
        h, mi = int(m.group(1)), 0
    else:
        h, mi = int(m.group(1)), int(m.group(2))
    if "вечер" in text and h < 12:
        h += 12
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        return None
    return time(h, mi)


def fmt_dt(dt: datetime, tz) -> str:  # type: ignore[no-untyped-def]
    """Показать дату пользователю: «пт 20.11 14:30»."""
    local = dt.astimezone(tz)
    wd = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"][local.weekday()]
    return f"{wd} {local:%d.%m %H:%M}"
