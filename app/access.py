"""Проверка доступа отдельным middleware (spec §9: позже — несколько пользователей)."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, Message, TelegramObject, User

log = logging.getLogger(__name__)

PRIVATE_TEXT = "Бот приватный"


class AccessPolicy:
    def __init__(self, owner_id: int) -> None:
        self.owner_id = owner_id

    def is_allowed(self, user_id: int) -> bool:
        return bool(self.owner_id) and user_id == self.owner_id


class AccessMiddleware(BaseMiddleware):
    def __init__(self, policy: AccessPolicy) -> None:
        self.policy = policy
        self._answered: set[int] = set()

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user: User | None = data.get("event_from_user")
        if user is not None and self.policy.is_allowed(user.id):
            return await handler(event, data)
        uid = user.id if user else None
        log.info("Доступ запрещён: user_id=%s", uid)
        # Отвечаем чужому один раз за запуск, чтобы не тратить лимиты на спам.
        if uid is not None and uid not in self._answered:
            self._answered.add(uid)
            try:
                if isinstance(event, Message) and event.chat.type == "private":
                    await event.answer(PRIVATE_TEXT)
                elif isinstance(event, CallbackQuery):
                    await event.answer(PRIVATE_TEXT, show_alert=False)
            except TelegramAPIError as e:  # чужой заблокировал бота и т. п. — не важно
                log.info("Не удалось ответить user_id=%s: %s", uid, e)
        return None
