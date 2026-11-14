"""Middleware доступа: всем, кроме ADMIN_IDS, бот не отвечает вообще."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, User
from loguru import logger


class AdminOnlyMiddleware(BaseMiddleware):
    def __init__(self, admin_ids: set[int]) -> None:
        self.admin_ids = admin_ids

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user: User | None = data.get("event_from_user")
        if user is None or user.id not in self.admin_ids:
            if user is not None:
                logger.info("ignored update from non-admin {}", user.id)
            return None
        return await handler(event, data)
