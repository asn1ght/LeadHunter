"""Playwright (Chromium): время загрузки, скриншоты desktop/mobile, горизонтальный скролл на мобильном.

Один браузер на процесс, страницы открываются в отдельных контекстах. Если Chromium не установлен
или упал — аудит продолжается без браузерного слоя (только httpx-проверки).
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

from app.config import DATA_DIR, get_settings
from app.net import playwright_proxy

SCREENSHOT_DIR = DATA_DIR / "screenshots"
DESKTOP = {"width": 1366, "height": 800}
MOBILE = {"width": 375, "height": 812}
MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)
NAV_TIMEOUT_MS = 30_000
RESTART_EVERY = 50  # перезапуск Chromium раз в N выдач, чтобы не копил память
OVERFLOW_TOLERANCE_PX = 2  # погрешность округления
JPEG_QUALITY = 78


@dataclass
class BrowserResult:
    load_ms: int | None = None  # до события load на десктопе
    mobile_scroll_width: int | None = None
    mobile_overflow: bool | None = None
    screenshot_desktop: str | None = None  # путь относительно DATA_DIR
    screenshot_mobile: str | None = None
    error: str | None = None


class BrowserPool:
    """Ленивый общий браузер с ограничением параллельных страниц."""

    def __init__(self) -> None:
        self._pw: Any = None
        self._browser: Any = None
        self._lock = asyncio.Lock()
        self._sem: asyncio.Semaphore | None = None
        self.unavailable: str | None = None
        self._uses = 0  # сколько раз браузер выдан с последнего запуска
        self._active = 0  # сколько вызовов сейчас им пользуются

    @asynccontextmanager
    async def using(self) -> AsyncIterator[Any]:
        """Взять браузер на время работы (учёт нужен, чтобы перезапускать его, только когда он свободен)."""
        self._active += 1
        try:
            yield await self.browser()
        finally:
            self._active -= 1
            self._uses += 1

    async def browser(self) -> Any:
        async with self._lock:
            if self._browser is not None and self._browser.is_connected():
                if self._uses < RESTART_EVERY or self._active > 1:
                    return self._browser
                # Chromium со временем «пухнет» — раз в RESTART_EVERY сайтов перезапускаем, пока никто не занят
                logger.info("browser: restart after {} uses to free memory", self._uses)
                try:
                    await self._browser.close()
                except Exception:  # noqa: BLE001
                    pass
                self._browser = None
                self._uses = 0
            from playwright.async_api import async_playwright

            if self._pw is None:
                self._pw = await async_playwright().start()
            proxy = playwright_proxy(get_settings().AUDIT_PROXY)
            if get_settings().AUDIT_PROXY and proxy is None:
                logger.warning("Chromium не умеет SOCKS5 с паролем — скриншоты идут напрямую, без AUDIT_PROXY")
            self._browser = await self._pw.chromium.launch(
                headless=True, args=["--disable-dev-shm-usage"], **({"proxy": proxy} if proxy else {})
            )
            return self._browser

    def semaphore(self) -> asyncio.Semaphore:
        if self._sem is None:
            self._sem = asyncio.Semaphore(max(1, get_settings().BROWSER_CONCURRENCY))
        return self._sem

    async def close(self) -> None:
        async with self._lock:
            if self._browser is not None:
                await self._browser.close()
                self._browser = None
            if self._pw is not None:
                await self._pw.stop()
                self._pw = None


pool = BrowserPool()


async def _open(browser: Any, url: str, viewport: dict[str, int], mobile: bool) -> tuple[Any, Any, int | None]:
    s = get_settings()
    ctx = await browser.new_context(
        viewport=viewport,
        user_agent=MOBILE_UA if mobile else s.USER_AGENT,
        is_mobile=mobile,
        has_touch=mobile,
        device_scale_factor=2 if mobile else 1,
        locale="ru-RU",
        ignore_https_errors=True,  # проблемы SSL уже ловит httpx-слой
    )
    page = await ctx.new_page()
    start = time.perf_counter()
    load_ms: int | None = None
    try:
        await page.goto(url, wait_until="load", timeout=NAV_TIMEOUT_MS)
        load_ms = int((time.perf_counter() - start) * 1000)
    except Exception as exc:  # noqa: BLE001 — таймаут load: страница могла частично отрисоваться
        logger.debug("browser goto {} ({}): {}", url, "mobile" if mobile else "desktop", exc)
    try:
        await page.wait_for_load_state("networkidle", timeout=5_000)
    except Exception:  # noqa: BLE001
        pass
    return ctx, page, load_ms


def _add_error(res: BrowserResult, text: str) -> None:
    res.error = (res.error + "; " if res.error else "") + text


async def _desktop(browser: Any, url: str, out_dir: Path, res: BrowserResult) -> None:
    try:
        ctx, page, res.load_ms = await _open(browser, url, DESKTOP, mobile=False)
        try:
            path = out_dir / "desktop.jpg"
            await page.screenshot(path=str(path), type="jpeg", quality=JPEG_QUALITY, full_page=False, timeout=15_000)
            res.screenshot_desktop = path.relative_to(DATA_DIR).as_posix()
        finally:
            await ctx.close()
    except Exception as exc:  # noqa: BLE001
        _add_error(res, f"desktop: {type(exc).__name__}")
        logger.warning("browser desktop {} failed: {}", url, exc)


async def _mobile(browser: Any, url: str, out_dir: Path, res: BrowserResult) -> None:
    try:
        ctx, page, _ = await _open(browser, url, MOBILE, mobile=True)
        try:
            res.mobile_scroll_width = int(await page.evaluate(
                "() => Math.max(document.documentElement.scrollWidth, document.body ? document.body.scrollWidth : 0)"
            ))
            res.mobile_overflow = res.mobile_scroll_width > MOBILE["width"] + OVERFLOW_TOLERANCE_PX
            path = out_dir / "mobile.jpg"
            await page.screenshot(path=str(path), type="jpeg", quality=JPEG_QUALITY, full_page=False, timeout=15_000)
            res.screenshot_mobile = path.relative_to(DATA_DIR).as_posix()
        finally:
            await ctx.close()
    except Exception as exc:  # noqa: BLE001
        _add_error(res, f"mobile: {type(exc).__name__}")
        logger.warning("browser mobile {} failed: {}", url, exc)


async def capture(url: str, company_id: int) -> BrowserResult:
    """Открыть сайт в Chromium (desktop и mobile), сделать скриншоты, замерить загрузку и overflow."""
    res = BrowserResult()
    if pool.unavailable:
        res.error = pool.unavailable
        return res
    out_dir = SCREENSHOT_DIR / str(company_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    async with pool.semaphore():
        try:
            async with pool.using() as browser:
                # desktop, затем mobile — по очереди: пиковая память Chromium вдвое меньше, чем параллельно
                await _desktop(browser, url, out_dir, res)
                await _mobile(browser, url, out_dir, res)
        except Exception as exc:  # noqa: BLE001 — не запустился сам Chromium
            pool.unavailable = f"Chromium недоступен: {type(exc).__name__}"
            logger.error("playwright unavailable (run `python -m playwright install chromium`): {}", exc)
            res.error = pool.unavailable
    return res


def abs_path(rel: str | None) -> Path | None:
    """Абсолютный путь к скриншоту по пути из audits (относительно DATA_DIR)."""
    if not rel:
        return None
    p = DATA_DIR / rel
    return p if p.exists() else None


A4_HEIGHT_PX = 1122  # 297 мм при 96 dpi (с запасом на округление)


async def html_to_pdf(html: str, one_page: bool = True) -> bytes:
    """Отрендерить HTML в PDF A4 через Chromium.

    one_page — если содержимое выше листа, пропорционально уменьшить его, чтобы влезло на одну страницу.
    """
    async with pool.using() as browser:
        ctx = await browser.new_context(viewport={"width": 794, "height": A4_HEIGHT_PX})
        try:
            page = await ctx.new_page()
            await page.set_content(html, wait_until="load")
            scale = 1.0
            if one_page:
                await page.emulate_media(media="print")
                height = await page.evaluate("() => document.documentElement.scrollHeight")
                if height > A4_HEIGHT_PX:
                    scale = max(0.5, A4_HEIGHT_PX / height * 0.99)
            return await page.pdf(format="A4", print_background=True, scale=scale,
                                  page_ranges="1" if one_page else "",
                                  margin={"top": "0", "bottom": "0", "left": "0", "right": "0"})
        finally:
            await ctx.close()
