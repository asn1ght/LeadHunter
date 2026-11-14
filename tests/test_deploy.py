"""Этап 5: прокси по сервисам, бэкапы, healthcheck, /netcheck."""
import gzip
import sqlite3
from datetime import datetime

import httpx
import pytest

from app import backup, health, net
from app.audit import fetcher
from app.config import Settings
from app.discovery import jobs


# ---------------------------------------------------------------- прокси


def test_mask_proxy_hides_password():
    assert net.mask_proxy("socks5://user:s3cret@1.2.3.4:1080") == "socks5://user:***@1.2.3.4:1080"
    assert "s3cret" not in net.mask_proxy("http://u:s3cret@proxy.example:3128")
    assert net.mask_proxy("socks5://127.0.0.1:1080") == "socks5://127.0.0.1:1080"
    assert net.mask_proxy("") == "напрямую"


def test_validate_proxy():
    assert net.validate_proxy("") is None
    assert net.validate_proxy("socks5://127.0.0.1:1080") is None
    assert net.validate_proxy("http://u:p@host:3128") is None
    assert "MTProto" in net.validate_proxy("https://t.me/proxy?server=1.2.3.4&port=443&secret=x")
    assert "схема" in net.validate_proxy("ftp://host:21")
    assert "порт" in net.validate_proxy("socks5://host")


def test_playwright_proxy():
    assert net.playwright_proxy("") is None
    assert net.playwright_proxy("socks5://127.0.0.1:1080") == {"server": "socks5://127.0.0.1:1080"}
    assert net.playwright_proxy("http://u:p@h:3128") == {"server": "http://h:3128", "username": "u", "password": "p"}
    assert net.playwright_proxy("socks5://u:p@h:1080") is None  # Chromium не умеет SOCKS5 с паролем


async def test_proxies_reach_clients(monkeypatch):
    s = Settings(YANDEX_SEARCH_API_KEY="k", YANDEX_FOLDER_ID="f", YANDEX_PROXY="socks5://127.0.0.1:1080",
                 AUDIT_PROXY="http://127.0.0.1:3128", TELEGRAM_PROXY="socks5://u:p@127.0.0.1:1081")
    monkeypatch.setattr(jobs, "get_settings", lambda: s)
    monkeypatch.setattr(fetcher, "get_settings", lambda: s)
    yc = jobs.make_search_client()
    assert any(isinstance(m, httpx.AsyncHTTPTransport) for m in yc._client._mounts.values())
    await yc.aclose()
    async with fetcher._client(verify=True) as client:
        assert client._mounts  # запросы аудита идут через AUDIT_PROXY
    from aiogram.client.session.aiohttp import AiohttpSession

    session = AiohttpSession(proxy=s.TELEGRAM_PROXY)  # aiohttp-socks установлен, SOCKS с паролем принимается
    await session.close()


# ---------------------------------------------------------------- бэкапы


def _db(path):
    with sqlite3.connect(path) as c:
        c.execute("create table companies (id integer primary key, domain text)")
        c.execute("insert into companies (domain) values ('gaz.ru')")
    c.close()


async def test_backup_and_rotation(tmp_path):
    src = tmp_path / "leadhunter.db"
    _db(src)
    out = tmp_path / "backups"
    now = datetime(2026, 11, 14, 3, 30)
    old = out / "leadhunter-20261020-033000.db.gz"
    fresh = out / "leadhunter-20261110-033000.db.gz"
    foreign = out / "notes.db.gz"
    out.mkdir()
    for p in (old, fresh, foreign):
        p.write_bytes(b"x")

    path = await backup.make_backup(src, out, keep_days=14, now=now)
    assert path.name == "leadhunter-20261114-033000.db.gz"
    restored = tmp_path / "restored.db"
    restored.write_bytes(gzip.decompress(path.read_bytes()))
    with sqlite3.connect(restored) as c:
        assert c.execute("select domain from companies").fetchall() == [("gaz.ru",)]
    c.close()
    assert not old.exists() and fresh.exists() and foreign.exists()  # чужие файлы не трогаем
    assert backup.list_backups(out) == [fresh, path]


async def test_backup_errors(tmp_path):
    assert backup.sqlite_path("postgresql+asyncpg://x/y") is None
    assert backup.sqlite_path("sqlite+aiosqlite:///C:/data/x.db").name == "x.db"
    with pytest.raises(RuntimeError, match="не найден"):
        await backup.make_backup(tmp_path / "nope.db", tmp_path / "b")


# ---------------------------------------------------------------- healthcheck


def test_heartbeat(tmp_path):
    hb = tmp_path / "heartbeat"
    assert not health.is_healthy(hb)
    health.beat(hb)
    assert health.is_healthy(hb)
    last = int(hb.read_text())
    assert not health.is_healthy(hb, now=last + health.MAX_AGE_S + 1)


# ---------------------------------------------------------------- /netcheck


async def test_netcheck_report(monkeypatch):
    async def fake_probe(url, proxy, timeout=10):
        return ("yandex" not in url, "HTTP 200" if "yandex" not in url else "таймаут", 120)

    async def fake_ip(proxy, timeout=10):
        return "5.6.7.8" if proxy else "1.2.3.4"

    monkeypatch.setattr(net, "probe", fake_probe)
    monkeypatch.setattr(net, "external_ip", fake_ip)
    monkeypatch.setattr(net, "get_settings", lambda: Settings(TELEGRAM_PROXY="socks5://u:pw@9.9.9.9:1080"))
    text = net.format_report(await net.run_checks())
    assert "✅ <b>Telegram</b> — HTTP 200, 120 мс" in text
    assert "socks5://u:***@9.9.9.9:1080" in text and "pw@" not in text
    assert "IP: <code>5.6.7.8</code>" in text and "IP: <code>1.2.3.4</code>" in text
    assert "⛔ <b>Yandex Search API</b> — таймаут" in text
    assert "YANDEX_PROXY" in text


async def test_scheduled_backup_failure_notifies(monkeypatch):
    from app import scheduler

    async def boom():
        raise RuntimeError("disk full")

    sent = []

    class Bot:
        async def send_message(self, chat_id, text, **kw):
            sent.append(text)

    monkeypatch.setattr(scheduler, "make_backup", boom)
    monkeypatch.setattr(scheduler, "get_settings", lambda: Settings(ADMIN_IDS="1"))
    await scheduler.scheduled_backup(Bot())
    assert sent and "disk full" in sent[0]

