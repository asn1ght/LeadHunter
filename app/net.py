"""Сеть: прокси по сервисам и проверка доступности (/netcheck).

Бот должен работать с любого сервера: из РФ может быть недоступен Telegram, из-за рубежа —
Yandex Cloud API и часть российских сайтов. Поэтому прокси задаётся отдельно для каждого сервиса:
TELEGRAM_PROXY, YANDEX_PROXY, AUDIT_PROXY. Пусто = напрямую. DeepSeek и Чекко — всегда напрямую.
"""
from __future__ import annotations

import asyncio
import ssl
import time
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

import httpx

from app.config import get_settings

IP_URL = "https://api.ipify.org"
SUPPORTED_SCHEMES = ("http", "https", "socks5", "socks5h", "socks4")


def mask_proxy(url: str) -> str:
    """Прокси для логов и сообщений: пароль скрыт. «socks5://u:p@h:1080» → «socks5://u:***@h:1080»."""
    if not url:
        return "напрямую"
    try:
        parts = urlsplit(url)
    except ValueError:
        return "***"
    if parts.password is None:
        return url
    host = parts.hostname or ""
    netloc = f"{parts.username}:***@{host}" + (f":{parts.port}" if parts.port else "")
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def validate_proxy(url: str) -> str | None:
    """Ошибка в адресе прокси (текст) или None. MTProto-ссылки (t.me/proxy) Bot API не поддерживает."""
    if not url:
        return None
    if "t.me/proxy" in url or url.startswith("tg://"):
        return "MTProto-прокси (t.me/proxy) не подходит для Bot API — нужен SOCKS5 или HTTP"
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return "неверный адрес прокси"
    if parts.scheme not in SUPPORTED_SCHEMES:
        return f"неподдерживаемая схема «{parts.scheme}» (нужно http:// или socks5://)"
    if not parts.hostname or not port:
        return "в адресе прокси нужны хост и порт, например socks5://127.0.0.1:1080"
    return None


def httpx_proxy(url: str) -> str | None:
    """Значение для httpx `proxy=`: None — напрямую."""
    return url or None


def playwright_proxy(url: str) -> dict[str, str] | None:
    """Прокси для Chromium. SOCKS5 с логином/паролем Chromium не поддерживает — тогда None (напрямую)."""
    if not url:
        return None
    parts = urlsplit(url)
    scheme = "socks5" if parts.scheme == "socks5h" else parts.scheme
    if scheme.startswith("socks") and parts.username:
        return None
    proxy: dict[str, str] = {"server": f"{scheme}://{parts.hostname}:{parts.port}"}
    if parts.username:
        proxy["username"] = parts.username
        proxy["password"] = parts.password or ""
    return proxy


async def open_tls(
    host: str, port: int, ssl_ctx: ssl.SSLContext, proxy: str = ""
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """TLS-соединение (для проверки сертификата), при необходимости через SOCKS/HTTP-прокси."""
    if not proxy:
        return await asyncio.open_connection(host, port, ssl=ssl_ctx, server_hostname=host)
    from python_socks.async_.asyncio import Proxy

    rdns = proxy.startswith("socks5h://")
    sock = await Proxy.from_url(proxy.replace("socks5h://", "socks5://", 1), rdns=rdns).connect(
        dest_host=host, dest_port=port
    )
    return await asyncio.open_connection(sock=sock, ssl=ssl_ctx, server_hostname=host)


# ---------------------------------------------------------------- /netcheck


@dataclass
class Check:
    name: str
    url: str
    proxy: str
    ok: bool = False
    detail: str = ""
    ms: int | None = None
    ip: str | None = None


async def probe(url: str, proxy: str, timeout: float = 10) -> tuple[bool, str, int | None]:
    """Доступен ли адрес: любой HTTP-ответ (даже 401/404) = сеть есть. Возвращает (ok, подробности, мс)."""
    start = time.perf_counter()
    try:
        async with httpx.AsyncClient(proxy=httpx_proxy(proxy), timeout=timeout, follow_redirects=False) as client:
            resp = await client.get(url)
        return True, f"HTTP {resp.status_code}", int((time.perf_counter() - start) * 1000)
    except httpx.TimeoutException:
        return False, "таймаут", None
    except (httpx.HTTPError, OSError) as exc:
        return False, type(exc).__name__, None


async def external_ip(proxy: str, timeout: float = 10) -> str | None:
    """Внешний IP, с которого уходят запросы через этот прокси (или напрямую)."""
    try:
        async with httpx.AsyncClient(proxy=httpx_proxy(proxy), timeout=timeout) as client:
            resp = await client.get(IP_URL)
        text = resp.text.strip()
        return text if resp.status_code == 200 and len(text) <= 45 else None
    except (httpx.HTTPError, OSError):
        return None


def targets() -> list[Check]:
    s = get_settings()
    return [
        Check("Telegram", "https://api.telegram.org", s.TELEGRAM_PROXY),
        Check("Yandex Search API", "https://searchapi.api.cloud.yandex.net", s.YANDEX_PROXY),
        Check("Аудит сайтов", "https://ya.ru", s.AUDIT_PROXY),
        Check("DeepSeek", s.DEEPSEEK_BASE_URL or "https://api.deepseek.com", ""),
        Check("Чекко", "https://api.checko.ru", ""),
    ]


async def run_checks(items: list[Check] | None = None) -> list[Check]:
    """Проверить все сервисы параллельно; внешний IP узнаём один раз на каждый маршрут."""
    items = items if items is not None else targets()
    routes = sorted({c.proxy for c in items})
    ips = dict(zip(routes, await asyncio.gather(*(external_ip(r) for r in routes)), strict=True))
    results = await asyncio.gather(*(probe(c.url, c.proxy) for c in items))
    for c, (ok, detail, ms) in zip(items, results, strict=True):
        c.ok, c.detail, c.ms, c.ip = ok, detail, ms, ips.get(c.proxy)
    return items


def format_report(items: list[Check]) -> str:
    lines = ["🌐 <b>Проверка сети</b>", ""]
    for c in items:
        mark = "✅" if c.ok else "⛔"
        speed = f", {c.ms} мс" if c.ms is not None else ""
        error = validate_proxy(c.proxy)
        lines.append(f"{mark} <b>{c.name}</b> — {c.detail}{speed}")
        lines.append(f"    через: <code>{mask_proxy(c.proxy)}</code> · IP: <code>{c.ip or 'не определён'}</code>")
        if error:
            lines.append(f"    ⚠️ {error}")
    if not all(c.ok for c in items):
        lines += ["", "⛔ — сервис недоступен с этого сервера. Задайте для него прокси в .env "
                      "(TELEGRAM_PROXY / YANDEX_PROXY / AUDIT_PROXY) и перезапустите бота."]
    return "\n".join(lines)
