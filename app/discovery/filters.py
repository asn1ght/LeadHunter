"""Нормализация и отсев доменов (раздел 6.3).

На этапе 1 используется нормализация (для /add) и проверка чёрного списка.
"""
from __future__ import annotations

import fnmatch
import re
from functools import lru_cache
from urllib.parse import urlparse

import tldextract
import yaml

from app.config import CONFIG_DIR

_extract = tldextract.TLDExtract(suffix_list_urls=(), include_psl_private_domains=True)


@lru_cache
def load_platforms() -> frozenset[str]:
    """Хостинги/конструкторы из config/platforms.yaml: каждый их поддомен — отдельный сайт."""
    path = CONFIG_DIR / "platforms.yaml"
    if not path.exists():
        return frozenset()
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return frozenset(str(d).lower().strip() for group in data.values() for d in (group or []))


def normalize_domain(url_or_host: str) -> str | None:
    """Ключ сайта: регистрируемый домен (eTLD+1) в нижнем регистре.

    «https://www.GazSever.com/contacts» → «gazsever.com».
    Для поддоменов хостингов и конструкторов (config/platforms.yaml) — полный адрес:
    «https://ci697493.tw1.ru/#» → «ci697493.tw1.ru», потому что там каждый поддомен — отдельный клиент.
    None — если это не домен.
    """
    s = url_or_host.strip().strip("<>\"'").lower()
    if not s:
        return None
    if "://" not in s:
        s = "http://" + s
    host = urlparse(s).hostname or ""
    host = host.removeprefix("www.").rstrip(".")
    if not host or not re.fullmatch(r"[a-z0-9а-яё.\-]+", host):
        return None
    ext = _extract(host)
    if not ext.suffix or not ext.domain:
        return None
    registered = f"{ext.domain}.{ext.suffix}"
    if ext.subdomain and registered in load_platforms():
        return host
    return registered


def extract_urls(text: str) -> list[str]:
    """Найти в тексте URL/домены (по одному на токен), без дублей по домену."""
    out: list[str] = []
    seen: set[str] = set()
    for token in re.split(r"[\s,;]+", text):
        token = token.strip().strip("()[]<>\"'")
        if not token or token.startswith("/"):
            continue
        if "." not in token:
            continue
        dom = normalize_domain(token)
        if dom and dom not in seen:
            seen.add(dom)
            out.append(token if "://" in token else token)
    return out


@lru_cache
def load_blacklist() -> tuple[str, ...]:
    """Паттерны доменов из config/blacklist.yaml (поддерживаются * и ?)."""
    path = CONFIG_DIR / "blacklist.yaml"
    if not path.exists():
        return ()
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    patterns: list[str] = []
    for group in data.values():
        patterns += [str(p).lower() for p in (group or [])]
    return tuple(patterns)


def is_blacklisted(domain: str, extra: set[str] | None = None) -> bool:
    """Домен в чёрном списке (yaml-паттерны + домены из БД)?"""
    d = domain.lower()
    if extra and d in extra:
        return True
    for p in load_blacklist():
        if fnmatch.fnmatch(d, p) or (not any(c in p for c in "*?") and (d == p or d.endswith("." + p))):
            return True
    return False
