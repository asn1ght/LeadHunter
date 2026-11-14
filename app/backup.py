"""Бэкапы SQLite: ежедневно в data/backups/ (хранится BACKUP_KEEP_DAYS дней) и по команде /backup.

Копия снимается штатным online backup API SQLite — консистентно даже во время работы бота,
затем сжимается gzip. В файле — вся CRM (контакты, заметки), поэтому он остаётся только
на сервере и в личном чате владельца.
"""
from __future__ import annotations

import asyncio
import gzip
import shutil
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path

from loguru import logger

from app.config import DATA_DIR, get_settings

BACKUP_DIR = DATA_DIR / "backups"
PREFIX = "leadhunter-"
SUFFIX = ".db.gz"
TELEGRAM_FILE_LIMIT = 50 * 1024 * 1024  # бот может отправить документ до 50 МБ


def sqlite_path(url: str | None = None) -> Path | None:
    """Путь к файлу БД из DATABASE_URL; None — не SQLite (или in-memory)."""
    url = url or get_settings().DATABASE_URL
    for prefix in ("sqlite+aiosqlite:///", "sqlite:///"):
        if url.startswith(prefix):
            path = url[len(prefix):]
            return Path(path) if path and path != ":memory:" else None
    return None


def _backup_sync(src: Path, dst_dir: Path, now: datetime) -> Path:
    dst_dir.mkdir(parents=True, exist_ok=True)
    name = f"{PREFIX}{now:%Y%m%d-%H%M%S}"
    raw, gz = dst_dir / f"{name}.db", dst_dir / f"{name}{SUFFIX}"
    # sqlite3-соединение как контекстный менеджер только коммитит — закрываем явно (closing)
    with closing(sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True)) as source, \
            closing(sqlite3.connect(raw)) as target:
        source.backup(target)
    with raw.open("rb") as fin, gzip.open(gz, "wb", compresslevel=6) as fout:
        shutil.copyfileobj(fin, fout)
    raw.unlink()
    return gz


def rotate(dst_dir: Path, keep_days: int, now: datetime) -> list[Path]:
    """Удалить бэкапы старше keep_days (по дате в имени файла). Возвращает удалённые."""
    removed: list[Path] = []
    border = now - timedelta(days=keep_days)
    for p in sorted(dst_dir.glob(f"{PREFIX}*{SUFFIX}")):
        stamp = p.name.removeprefix(PREFIX).removesuffix(SUFFIX)
        try:
            made = datetime.strptime(stamp, "%Y%m%d-%H%M%S")
        except ValueError:
            continue  # чужой файл — не трогаем
        if made < border:
            p.unlink()
            removed.append(p)
    return removed


async def make_backup(
    src: Path | None = None, dst_dir: Path = BACKUP_DIR, keep_days: int | None = None, now: datetime | None = None
) -> Path:
    """Снять бэкап и удалить устаревшие. Возвращает путь к .db.gz."""
    src = src or sqlite_path()
    if src is None:
        raise RuntimeError("бэкап поддерживается только для SQLite (DATABASE_URL)")
    if not src.exists():
        raise RuntimeError(f"файл БД не найден: {src.name}")
    now = now or datetime.now(get_settings().tz).replace(tzinfo=None)
    keep = keep_days if keep_days is not None else get_settings().BACKUP_KEEP_DAYS
    path = await asyncio.to_thread(_backup_sync, src, dst_dir, now)
    removed = await asyncio.to_thread(rotate, dst_dir, keep, now)
    logger.info("backup {} ({} KB), removed old: {}", path.name, path.stat().st_size // 1024, len(removed))
    return path


def list_backups(dst_dir: Path = BACKUP_DIR) -> list[Path]:
    return sorted(dst_dir.glob(f"{PREFIX}*{SUFFIX}"))
