"""Точка входа: миграции → бот + планировщик.

Запуск: `python -m app.main`
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from pathlib import Path

# Позволяет запускать и как `python app/main.py` (кнопка ▶ в VS Code), а не только `python -m app.main`
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand
from alembic import command
from alembic.config import Config
from loguru import logger

from app.config import BASE_DIR, DATA_DIR, get_settings, mask_secret


def setup_logging() -> None:
    (DATA_DIR / "logs").mkdir(parents=True, exist_ok=True)
    logger.remove()
    logger.add(sys.stderr, level="INFO")
    logger.add(DATA_DIR / "logs" / "leadhunter.log", level="DEBUG", rotation="10 MB", retention=10, encoding="utf-8")


def run_migrations() -> None:
    """alembic upgrade head (до запуска event loop)."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    cfg = Config(str(BASE_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BASE_DIR / "alembic"))
    cfg.attributes["skip_logging"] = True
    command.upgrade(cfg, "head")


def build_dispatcher() -> Dispatcher:
    from app.bot.handlers import add, ai_actions, card_actions, discovery, fallback, leads, pdf, reports, start
    from app.bot.middlewares import AdminOnlyMiddleware

    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(AdminOnlyMiddleware(get_settings().admin_ids))
    # порядок важен: команды/меню → разделы → сценарии карточки → fallback
    dp.include_routers(
        start.router, leads.router, reports.router, discovery.router, add.router,
        ai_actions.router, pdf.router, card_actions.router, fallback.router,
    )
    return dp


COMMANDS = [
    BotCommand(command="start", description="Меню"),
    BotCommand(command="new", description="Новые лиды"),
    BotCommand(command="today", description="Что сделать сегодня"),
    BotCommand(command="leads", description="Все лиды"),
    BotCommand(command="find", description="Поиск лида"),
    BotCommand(command="add", description="Добавить сайты"),
    BotCommand(command="report", description="Отчёт за день"),
    BotCommand(command="week", description="Отчёт за неделю"),
    BotCommand(command="month", description="Отчёт за месяц"),
    BotCommand(command="run", description="Запустить поиск"),
    BotCommand(command="queries", description="Поисковые запросы"),
    BotCommand(command="niche", description="Ниши"),
    BotCommand(command="usage", description="Расход API"),
    BotCommand(command="cancel", description="Отменить ввод"),
    BotCommand(command="help", description="Справка"),
]


async def run() -> None:
    from app.db import repo
    from app.db.session import SessionLocal, engine
    from app.scheduler import setup_scheduler

    s = get_settings()
    async with SessionLocal() as session:
        await repo.sync_niches_from_config(session)
        await session.commit()

    bot = Bot(s.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = build_dispatcher()
    scheduler = await setup_scheduler(bot)
    scheduler.start()
    scheduler.get_job("reminders").modify(next_run_time=datetime.now(s.tz))
    await bot.set_my_commands(COMMANDS)
    logger.info("bot started, admins={}, token={}", sorted(s.admin_ids), mask_secret(s.BOT_TOKEN))
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        scheduler.shutdown(wait=False)
        from app.audit.browser import pool

        await pool.close()
        await bot.session.close()
        await engine.dispose()


def main() -> None:
    setup_logging()
    s = get_settings()
    if not s.BOT_TOKEN or not s.admin_ids:
        logger.error("Заполните BOT_TOKEN и ADMIN_IDS в .env (см. .env.example)")
        sys.exit(1)
    run_migrations()
    asyncio.run(run())


if __name__ == "__main__":
    main()
