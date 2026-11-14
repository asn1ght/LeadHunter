"""Загрузка сайта: редиректы, тайминги, SSL, robots.txt, вежливый краулинг (раздел 7.1)."""
from __future__ import annotations

import asyncio
import re
import ssl
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx
from loguru import logger
from selectolax.lexbor import LexborHTMLParser as HTMLParser

from app.config import get_settings
from app.net import open_tls

MAX_HTML_BYTES = 5 * 1024 * 1024
LINK_GROUPS = (  # по приоритету: ИНН чаще всего в «Реквизитах», «Контактах» или «О компании»
    re.compile(r"реквизит|rekvizit|requisite", re.I),
    re.compile(r"контакт|contact|kontakt", re.I),
    re.compile(r"услуг|uslug|services|service|price|цен", re.I),
    re.compile(r"о компании|o-kompanii|okompanii|about|o-nas|о нас", re.I),
)
INTERNAL_LINK_RE = re.compile("|".join(g.pattern for g in LINK_GROUPS), re.I)


@dataclass
class FetchResult:
    """Результат загрузки главной (и до 2 внутренних страниц)."""

    requested_url: str
    final_url: str | None = None
    status: int | None = None
    ttfb_ms: int | None = None
    load_ms: int | None = None
    html: str = ""
    html_bytes: int = 0
    error: str | None = None
    ssl_invalid: bool = False
    ssl_days_left: int | None = None
    robots_disallowed: bool = False
    extra_pages: list[tuple[str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.error is None and self.status is not None and self.status < 400


class DomainThrottle:
    """Не более 1 запроса в `interval` секунд на домен."""

    def __init__(self, interval: float = 1.0) -> None:
        self.interval = interval
        self._locks: dict[str, asyncio.Lock] = {}
        self._last: dict[str, float] = {}

    async def wait(self, host: str) -> None:
        host = host.lower().removeprefix("www.")
        lock = self._locks.setdefault(host, asyncio.Lock())
        async with lock:
            delta = time.monotonic() - self._last.get(host, 0)
            if delta < self.interval:
                await asyncio.sleep(self.interval - delta)
            self._last[host] = time.monotonic()


throttle = DomainThrottle()


def decode_html(content: bytes, header_charset: str | None) -> str:
    """Декодировать HTML: charset из заголовка → meta charset → utf-8 → cp1251."""
    candidates: list[str] = []
    if header_charset:
        candidates.append(header_charset)
    m = re.search(rb"<meta[^>]+charset=[\"']?([\w\-]+)", content[:4096], re.I)
    if m:
        candidates.append(m.group(1).decode("ascii", "ignore"))
    candidates += ["utf-8", "cp1251"]
    for enc in candidates:
        try:
            return content.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return content.decode("utf-8", errors="replace")


def _is_ssl_error(exc: Exception) -> bool:
    text = repr(exc).lower()
    return isinstance(exc, ssl.SSLError) or "ssl" in text or "certificate" in text


def _client(verify: bool) -> httpx.AsyncClient:
    s = get_settings()
    return httpx.AsyncClient(
        follow_redirects=True,
        timeout=httpx.Timeout(s.HTTP_TIMEOUT),
        verify=verify,
        headers={"User-Agent": s.USER_AGENT, "Accept-Language": "ru-RU,ru;q=0.9"},
        max_redirects=10,
        proxy=s.AUDIT_PROXY or None,
    )


async def _get(client: httpx.AsyncClient, url: str) -> tuple[httpx.Response, bytes, int, int]:
    """GET с замером TTFB и полного времени. Возвращает (response, body, ttfb_ms, total_ms)."""
    host = urlparse(url).hostname or ""
    await throttle.wait(host)
    start = time.perf_counter()
    async with client.stream("GET", url) as resp:
        ttfb = int((time.perf_counter() - start) * 1000)
        chunks: list[bytes] = []
        size = 0
        async for chunk in resp.aiter_bytes():
            chunks.append(chunk)
            size += len(chunk)
            if size > MAX_HTML_BYTES:
                break
        total = int((time.perf_counter() - start) * 1000)
    return resp, b"".join(chunks), ttfb, total


async def ssl_days_left(host: str, port: int = 443, timeout: float = 10) -> int | None:
    """Сколько дней осталось до истечения сертификата (None — не удалось узнать)."""
    try:
        ctx = ssl.create_default_context()
        _, writer = await asyncio.wait_for(open_tls(host, port, ctx, get_settings().AUDIT_PROXY), timeout)
        cert = writer.get_extra_info("peercert")
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass
        if not cert or "notAfter" not in cert:
            return None
        expires = datetime.fromtimestamp(ssl.cert_time_to_seconds(cert["notAfter"]), timezone.utc)
        return (expires - datetime.now(timezone.utc)).days
    except Exception as exc:  # noqa: BLE001
        logger.debug("ssl check failed for {}: {}", host, exc)
        return None


async def _robots(client: httpx.AsyncClient, base: str) -> RobotFileParser | None:
    try:
        resp, body, _, _ = await _get(client, urljoin(base, "/robots.txt"))
        if resp.status_code >= 400:
            return None
        rp = RobotFileParser()
        rp.parse(decode_html(body, resp.charset_encoding).splitlines())
        return rp
    except Exception:  # noqa: BLE001
        return None


def pick_internal_links(html: str, base_url: str, limit: int = 3) -> list[str]:
    """До `limit` внутренних ссылок: по одной на «Реквизиты», «Контакты», «Услуги», «О компании»."""
    tree = HTMLParser(html)
    base_host = (urlparse(base_url).hostname or "").removeprefix("www.")
    found: list[str] = []
    for a in tree.css("a[href]"):
        href = (a.attributes.get("href") or "").strip()
        if not href or href.startswith(("#", "tel:", "mailto:", "javascript:")):
            continue
        text = a.text(strip=True)
        hay = f"{href} {text}"
        group = next((i for i, g in enumerate(LINK_GROUPS) if g.search(hay)), None)
        if group is None:
            continue
        full = urljoin(base_url, href).split("#")[0]
        host = (urlparse(full).hostname or "").removeprefix("www.")
        if host != base_host or full.rstrip("/") == base_url.rstrip("/"):
            continue
        if re.search(r"\.(pdf|jpe?g|png|docx?|xlsx?|zip|rar)$", full, re.I):
            continue
        if all(full != f for f, _ in found):
            found.append((full, group))
    # по одной ссылке на группу в порядке приоритета, затем остальные
    picked = [next(f for f, g in found if g == grp) for grp in range(len(LINK_GROUPS)) if any(g == grp for _, g in found)]
    picked += [f for f, _ in found if f not in picked]
    return picked[:limit]


RETRY_DELAY_S = 3


async def fetch_site(url_or_domain: str, internal_pages: int = 3) -> FetchResult:
    """Загрузить сайт; при таймауте или ошибке соединения — одна повторная попытка.

    Сайт засчитывается недоступным только если не открылся оба раза.
    """
    res = await _fetch_once(url_or_domain, internal_pages)
    if res.error and (res.error == "timeout" or res.error.startswith("connect")):
        logger.info("fetch {} failed ({}), retry in {} s", url_or_domain, res.error, RETRY_DELAY_S)
        await asyncio.sleep(RETRY_DELAY_S)
        res = await _fetch_once(url_or_domain, internal_pages)
    return res


async def _fetch_once(url_or_domain: str, internal_pages: int = 3) -> FetchResult:
    """Загрузить главную страницу сайта и до `internal_pages` внутренних.

    Без схемы сначала пробуем https, при ошибке соединения — http.
    При ошибке сертификата повторяем с verify=False и ставим `ssl_invalid`.
    """
    raw = url_or_domain.strip()
    candidates = [raw] if raw.startswith(("http://", "https://")) else [f"https://{raw}", f"http://{raw}"]
    res = FetchResult(requested_url=candidates[0])
    s = get_settings()

    for url in candidates:
        res.requested_url = url
        verify_modes = [True, False]
        for verify in verify_modes:
            async with _client(verify) as client:
                try:
                    resp, body, ttfb, total = await _get(client, url)
                except httpx.TimeoutException:
                    res.error = "timeout"
                    break
                except Exception as exc:  # noqa: BLE001
                    if verify and _is_ssl_error(exc):
                        res.ssl_invalid = True
                        continue  # повтор с verify=False
                    res.error = f"connect: {type(exc).__name__}"
                    break
                res.error = None
                res.final_url = str(resp.url)
                res.status = resp.status_code
                res.ttfb_ms, res.load_ms = ttfb, total
                res.html_bytes = len(body)
                res.html = decode_html(body, resp.charset_encoding)

                final = urlparse(res.final_url)
                base = f"{final.scheme}://{final.netloc}/"
                rp = await _robots(client, base)
                if rp is not None and not rp.can_fetch(s.USER_AGENT, base):
                    res.robots_disallowed = True
                if res.ok and not res.robots_disallowed and internal_pages:
                    for link in pick_internal_links(res.html, res.final_url, internal_pages):
                        if rp is not None and not rp.can_fetch(s.USER_AGENT, link):
                            continue
                        try:
                            r2, b2, _, _ = await _get(client, link)
                            if r2.status_code < 400 and "html" in r2.headers.get("content-type", "html"):
                                res.extra_pages.append((str(r2.url), decode_html(b2, r2.charset_encoding)))
                        except Exception as exc:  # noqa: BLE001
                            logger.debug("internal page {} failed: {}", link, exc)
                break
        if res.final_url:  # главная получена
            break
        if res.error == "timeout":
            break  # http тоже, скорее всего, повиснет

    if res.final_url and res.final_url.startswith("https://") and not res.ssl_invalid:
        host = urlparse(res.final_url).hostname
        if host:
            res.ssl_days_left = await ssl_days_left(host)
    return res
