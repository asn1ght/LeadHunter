"""Поиск лидов для /find: регистр, ё/е, транслит (газ ↔ gaz), слова в любом порядке, опечатки.

SQLite не умеет lower() для кириллицы, поэтому сопоставление делается в Python —
для CRM на тысячи компаний это мгновенно.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

_RU_LAT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z", "и": "i",
    "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t",
    "у": "u", "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "",
    "э": "e", "ю": "yu", "я": "ya",
}
# латиница → кириллица (грубо, для «gazsever» → «газсевер»); многобуквенные — первыми
_LAT_RU = [("sch", "щ"), ("shch", "щ"), ("zh", "ж"), ("kh", "х"), ("ch", "ч"), ("sh", "ш"), ("yu", "ю"),
           ("ya", "я"), ("yo", "е"), ("ts", "ц")] + [(l, r) for r, l in _RU_LAT.items() if len(l) == 1]

STOP = {"ооо", "ао", "зао", "оао", "пао", "ип", "компания", "ru", "com", "рф", "www", "http", "https"}


def normalize(text: str) -> str:
    """Нижний регистр, ё→е, всё кроме букв/цифр → пробел."""
    t = text.casefold().replace("ё", "е")
    return " ".join(re.sub(r"[^0-9a-zа-я]+", " ", t).split())


def to_lat(text: str) -> str:
    return "".join(_RU_LAT.get(ch, ch) for ch in text)


def to_ru(text: str) -> str:
    out = text
    for lat, ru in _LAT_RU:
        out = out.replace(lat, ru)
    return out


def tokens(query: str) -> list[str]:
    toks = [t for t in normalize(query).split() if t not in STOP]
    return toks or normalize(query).split()


@dataclass
class Doc:
    """Поля компании, по которым ищем."""

    id: int
    name: str = ""
    domain: str = ""
    inn: str = ""
    phones: tuple[str, ...] = ()
    emails: tuple[str, ...] = ()
    contact: str = ""
    address: str = ""
    notes: str = ""


def _fuzzy_word(tok: str, words: list[str]) -> bool:
    """Опечатка: токен от 4 букв похож на слово (или начало слова) на ≥ 0.8."""
    if len(tok) < 4:
        return False
    for w in words:
        if abs(len(w) - len(tok)) <= 2 and SequenceMatcher(None, tok, w).ratio() >= 0.8:
            return True
        if len(w) > len(tok):
            # кусок слитного слова: «севр» ≈ «север» внутри «газсевер»
            for size in (len(tok), len(tok) + 1):
                for i in range(0, len(w) - size + 1):
                    if SequenceMatcher(None, tok, w[i : i + size]).ratio() >= 0.85:
                        return True
    return False


def _token_score(tok: str, field: str, weight: float) -> float:
    """Вес совпадения одного токена с полем (с учётом транслита). 0 — нет совпадения."""
    if not field:
        return 0.0
    variants = {tok, to_lat(tok), to_ru(tok)}
    field_lat = to_lat(field)
    hay = f" {field} "
    hay_lat = f" {field_lat} "
    compact = field.replace(" ", "")
    compact_lat = field_lat.replace(" ", "")
    for v in variants:
        if not v:
            continue
        if f" {v} " in hay or f" {v} " in hay_lat:
            return weight * 1.0  # целое слово
        if f" {v}" in hay or f" {v}" in hay_lat:
            return weight * 0.85  # начало слова
        if len(v) >= 3 and (v in compact or v in compact_lat):
            return weight * 0.7  # внутри слитного слова: «север» в «газсевер»
    if _fuzzy_word(tok, field.split()) or _fuzzy_word(to_lat(tok), field_lat.split()):
        return weight * 0.5
    return 0.0


def score(query: str, doc: Doc) -> float:
    """Релевантность компании запросу; 0 — не подходит. Каждое слово запроса должно где-то найтись."""
    q = query.strip()
    if not q:
        return 0.0
    digits = re.sub(r"\D", "", q)
    letters = re.sub(r"[\d\s+()\-.]", "", q)
    if len(digits) >= 5 and not letters:  # телефон или ИНН
        best = 0.0
        if doc.inn and digits in doc.inn:
            best = 100.0 if digits == doc.inn else 80.0
        tail = digits[-10:]
        for p in doc.phones:
            if tail in re.sub(r"\D", "", p):
                best = max(best, 90.0)
        return best

    fields = [
        (normalize(doc.name), 10.0),
        (normalize(doc.domain.rsplit(".", 1)[0]), 9.0),
        (normalize(doc.contact), 6.0),
        (normalize(" ".join(doc.emails)), 5.0),
        (normalize(doc.address), 3.0),
        (normalize(doc.notes), 2.0),
    ]
    total = 0.0
    for tok in tokens(q):
        best = max(_token_score(tok, f, w) for f, w in fields)
        if best == 0:
            return 0.0
        total += best
    full = normalize(q)
    if full and full == normalize(doc.name):
        total += 20  # точное совпадение названия
    elif full and normalize(doc.name).startswith(full):
        total += 8
    return total


def search(query: str, docs: list[Doc], limit: int = 20) -> list[tuple[int, float]]:
    """[(id, релевантность)] по убыванию."""
    scored = [(d.id, s) for d in docs if (s := score(query, d)) > 0]
    scored.sort(key=lambda x: -x[1])
    return scored[:limit]
