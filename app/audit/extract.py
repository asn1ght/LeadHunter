"""Извлечение данных компании со страницы (раздел 7.3).

Персональные данные не собираем: только общие телефоны/email компании, ФИО не парсим.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from selectolax.lexbor import LexborHTMLParser as HTMLParser

TEXT_LIMIT = 8000

_PHONE_RE = re.compile(
    r"(?<![\d\w])(?:\+\s*7|8)[\s\- ]*\(?\s*(\d{3})\s*\)?[\s\- ]*(\d{3})[\s\- ]*(\d{2})[\s\- ]*(\d{2})(?!\d)"
)
_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
_EMAIL_BAD_TLD = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".js", ".css")
_INN_RE = re.compile(r"ИНН[\s:№ ]*(\d{10}|\d{12})(?!\d)", re.IGNORECASE)
_OGRN_RE = re.compile(r"ОГРН(?:ИП)?[\s:№ ]*(\d{13}|\d{15})(?!\d)", re.IGNORECASE)
_ORG_RE = re.compile(r"\b(?:ООО|АО|ЗАО|ОАО|ПАО)\s*[«\"“]([^»\"”]{2,60})[»\"”]")
_ADDRESS_RE = re.compile(
    r"((?:\d{6},?\s*)?(?:г\.\s*)?(?:Санкт-Петербург|СПб|Ленинградская\s+обл\w*\.?)"
    r"[^\n]{0,80}?(?:ул\.|улица|пр\.|пр-т|проспект|наб\.|набережная|ш\.|шоссе|пер\.|переулок|бульвар|б-р)"
    r"[^\n]{0,60}?(?:д\.|дом)?\s*\d+[\w/]*(?:[,\s]*(?:корп\.|к\.|стр\.|лит\.|литера|оф\.|офис)\s*[\w\d]+)*)",
    re.IGNORECASE,
)

MESSENGER_PATTERNS: dict[str, tuple[str, ...]] = {
    "WhatsApp": ("wa.me/", "api.whatsapp.com", "whatsapp://", "web.whatsapp.com"),
    "Telegram": ("t.me/", "telegram.me/", "tg://"),
    "Viber": ("viber://",),
}

B2C_MARKERS = ("частный дом", "частного дома", "дача", "дачи", "дачу", "коттедж")
B2B_MARKERS = ("промышленн", "объект", "тендер", "застройщик")


@dataclass
class Extracted:
    """Данные компании, извлечённые со страниц сайта."""

    name: str | None = None
    inn: str | None = None
    ogrn: str | None = None
    phones: list[str] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)
    messengers: list[str] = field(default_factory=list)
    address: str | None = None
    segment: str = "unknown"
    region_detected: str | None = None
    niche_match: bool | None = None
    text: str = ""


def normalize_phone(raw: str) -> str | None:
    """Нормализовать RU-номер в +7XXXXXXXXXX. Вернуть None, если это не номер."""
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 11 and digits[0] in "78":
        digits = "7" + digits[1:]
    elif len(digits) == 10:
        digits = "7" + digits
    else:
        return None
    if digits[1] not in "3489":  # коды РФ: 3xx, 4xx, 8xx, 9xx
        return None
    return "+" + digits


def format_phone(phone: str) -> str:
    """+78129035772 → +7 812 903-57-72."""
    d = re.sub(r"\D", "", phone)
    if len(d) != 11:
        return phone
    return f"+7 {d[1:4]} {d[4:7]}-{d[7:9]}-{d[9:11]}"


def find_phones(text: str) -> list[str]:
    """Все телефоны из текста, нормализованные, без дублей, в порядке появления."""
    out: list[str] = []
    for m in _PHONE_RE.finditer(text):
        p = normalize_phone("".join(m.groups()))
        if p and p not in out:
            out.append(p)
    return out


def find_emails(text: str) -> list[str]:
    out: list[str] = []
    for e in _EMAIL_RE.findall(text):
        e = e.lower().strip(".")
        if e.endswith(_EMAIL_BAD_TLD) or "example" in e or "sentry" in e:
            continue
        if e not in out:
            out.append(e)
    return out


def inn_is_valid(inn: str) -> bool:
    """Проверить контрольную сумму ИНН (10 или 12 цифр)."""
    if not inn.isdigit() or len(inn) not in (10, 12):
        return False
    d = [int(c) for c in inn]

    def ctrl(weights: list[int]) -> int:
        return sum(w * x for w, x in zip(weights, d)) % 11 % 10

    if len(inn) == 10:
        return ctrl([2, 4, 10, 3, 5, 9, 4, 6, 8]) == d[9]
    return (
        ctrl([7, 2, 4, 10, 3, 5, 9, 4, 6, 8]) == d[10]
        and ctrl([3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8]) == d[11]
    )


def ogrn_is_valid(ogrn: str) -> bool:
    """Проверить контрольную цифру ОГРН (13) / ОГРНИП (15)."""
    if not ogrn.isdigit():
        return False
    if len(ogrn) == 13:
        return int(ogrn[:12]) % 11 % 10 == int(ogrn[12])
    if len(ogrn) == 15:
        return int(ogrn[:14]) % 13 % 10 == int(ogrn[14])
    return False


def find_inn(text: str) -> str | None:
    """Первый валидный ИНН рядом со словом «ИНН»."""
    for m in _INN_RE.finditer(text):
        if inn_is_valid(m.group(1)):
            return m.group(1)
    return None


def find_ogrn(text: str) -> str | None:
    for m in _OGRN_RE.finditer(text):
        if ogrn_is_valid(m.group(1)):
            return m.group(1)
    return None


def find_address(text: str) -> str | None:
    m = _ADDRESS_RE.search(text)
    if not m:
        return None
    return re.sub(r"\s+", " ", m.group(1)).strip(" ,.;")[:200]


def visible_text(tree: HTMLParser) -> str:
    """Видимый текст страницы без скриптов/стилей, схлопнутые пробелы."""
    tree = HTMLParser(tree.html or "")
    for sel in ("script", "style", "noscript", "svg", "template", "iframe"):
        for node in tree.css(sel):
            node.decompose()
    body = tree.body or tree.root
    if body is None:
        return ""
    text = body.text(separator=" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()


def detect_segment(text: str) -> str:
    """b2c / b2b / unknown по маркерам в тексте (если есть оба — тот, что чаще)."""
    low = text.lower()
    b2c = sum(low.count(m) for m in B2C_MARKERS)
    b2b = sum(low.count(m) for m in B2B_MARKERS)
    if b2c == 0 and b2b == 0:
        return "unknown"
    return "b2c" if b2c >= b2b else "b2b"


def detect_name(tree: HTMLParser, text: str) -> str | None:
    """Название компании.

    Порядок: og:site_name → JSON-LD (Organization/LocalBusiness/WebSite) → бренд-часть title
    («Услуги | Бренд») → alt логотипа → «ООО «…»» в тексте → короткий title.
    """
    node = tree.css_first('meta[property="og:site_name"]')
    if node and (val := (node.attributes.get("content") or "").strip()):
        return val[:120]
    if ld := _jsonld_name(tree):
        return ld
    title_node = tree.css_first("title")
    title = title_node.text(strip=True) if title_node else ""
    if title:
        parts = [p.strip() for p in re.split(r"\s[|—–\-:]\s|\s\|\s?|\|", title) if p.strip()]
        if len(parts) > 1:
            # бренд обычно короткий и стоит в конце или начале
            cand = min((parts[-1], parts[0]), key=len)
            if len(cand) <= 40:
                return cand
    for img in tree.css("img"):
        attrs = img.attributes
        hint = f"{attrs.get('class') or ''} {attrs.get('src') or ''} {attrs.get('id') or ''}".lower()
        alt = (attrs.get("alt") or "").strip()
        if "logo" in hint and 2 <= len(alt) <= 40 and "logo" not in alt.lower() and "логотип" not in alt.lower():
            return alt
    m = _ORG_RE.search(text)
    if m:
        return m.group(1).strip()
    if title and len(title) <= 40:
        return title
    return None


_LD_TYPES = {"Organization", "LocalBusiness", "WebSite", "Corporation", "HomeAndConstructionBusiness", "GeneralContractor"}


def _jsonld_name(tree: HTMLParser) -> str | None:
    """Имя организации из JSON-LD."""
    for node in tree.css('script[type="application/ld+json"]'):
        try:
            data = json.loads(node.text() or "")
        except ValueError:
            continue
        stack: list[Any] = [data]
        while stack:
            item = stack.pop(0)
            if isinstance(item, list):
                stack += item
            elif isinstance(item, dict):
                types = item.get("@type")
                types = set(types) if isinstance(types, list) else {types}
                name = item.get("name")
                if types & _LD_TYPES and isinstance(name, str) and 2 <= len(name.strip()) <= 60:
                    return name.strip()
                stack += [v for v in item.values() if isinstance(v, (dict, list))]
    return None


def detect_messengers(html: str) -> list[str]:
    low = html.lower()
    return [name for name, pats in MESSENGER_PATTERNS.items() if any(p in low for p in pats)]


def detect_region(text: str, phones: list[str], markers: list[str]) -> str | None:
    """Регион СПб/ЛО по маркерам в тексте или кодам телефонов 812/813."""
    low = text.lower()
    if any(p.startswith(("+7812", "+7813")) for p in phones):
        return "СПб/ЛО"
    if any(m.lower() in low for m in markers if not m.isdigit()):
        return "СПб/ЛО"
    return None


def extract(
    pages: list[tuple[str, str]],
    region_markers: list[str] | None = None,
    niche_keywords: list[str] | None = None,
) -> Extracted:
    """Извлечь данные из главной и внутренних страниц.

    pages: [(url, html)], первая — главная.
    """
    if not pages:
        return Extracted()
    main_tree = HTMLParser(pages[0][1])
    main_text = visible_text(main_tree)
    all_text = main_text
    all_html = pages[0][1]
    tel_hrefs: list[str] = []
    mail_hrefs: list[str] = []
    for i, (_, html) in enumerate(pages):
        tree = main_tree if i == 0 else HTMLParser(html)
        if i:
            all_text += "\n" + visible_text(tree)
            all_html += "\n" + html
        for a in tree.css("a[href]"):
            href = (a.attributes.get("href") or "").strip()
            if href.lower().startswith("tel:"):
                tel_hrefs.append(href[4:])
            elif href.lower().startswith("mailto:"):
                mail_hrefs.append(href[7:].split("?")[0])

    phones: list[str] = []
    for raw in tel_hrefs:
        p = normalize_phone(raw)
        if p and p not in phones:
            phones.append(p)
    for p in find_phones(all_text):
        if p not in phones:
            phones.append(p)

    emails = find_emails(" ".join(mail_hrefs) + " " + all_text)

    low = all_text.lower()
    niche_match = None
    if niche_keywords:
        niche_match = any(k.lower() in low for k in niche_keywords)

    return Extracted(
        name=detect_name(main_tree, main_text),
        inn=find_inn(all_text),
        ogrn=find_ogrn(all_text),
        phones=phones[:5],
        emails=emails[:5],
        messengers=detect_messengers(all_html),
        address=find_address(all_text),
        segment=detect_segment(all_text),
        region_detected=detect_region(all_text, phones, region_markers or []),
        niche_match=niche_match,
        text=main_text[:TEXT_LIMIT],
    )


def same_site(url: str, base_host: str) -> bool:
    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    return host == base_host.lower().removeprefix("www.")
