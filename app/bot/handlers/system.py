"""/backup — файл БД в чат, /netcheck — доступность сервисов и внешний IP (этап 5)."""
from __future__ import annotations

from html import escape

from aiogram import Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import FSInputFile, Message
from loguru import logger

from app import net
from app.backup import TELEGRAM_FILE_LIMIT, list_backups, make_backup

router = Router(name="system")


@router.message(Command("backup"), StateFilter("*"))
async def cmd_backup(message: Message, state: FSMContext) -> None:
    await state.clear()
    status = await message.answer("💾 Делаю бэкап базы…")
    try:
        path = await make_backup()
    except Exception as exc:  # noqa: BLE001
        logger.exception("backup failed: {}", exc)
        await status.edit_text(f"⚠️ Бэкап не удался: {escape(str(exc))}")
        return
    size = path.stat().st_size
    kept = len(list_backups(path.parent))
    if size > TELEGRAM_FILE_LIMIT:
        await status.edit_text(f"💾 Бэкап сохранён на сервере: {path.name} ({size // 1024 // 1024} МБ) — "
                               "больше 50 МБ, Telegram такой файл не примет.")
        return
    await message.answer_document(
        FSInputFile(path, filename=path.name),
        caption=(f"💾 Бэкап базы · {size // 1024} КБ · на сервере хранится копий: {kept}\n"
                 "⚠️ Внутри все контакты и заметки — не пересылайте."),
    )
    await status.delete()


@router.message(Command("netcheck"), StateFilter("*"))
async def cmd_netcheck(message: Message, state: FSMContext) -> None:
    await state.clear()
    status = await message.answer("🌐 Проверяю доступ к сервисам…")
    items = await net.run_checks()
    await status.edit_text(net.format_report(items))
