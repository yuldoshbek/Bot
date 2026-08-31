"""Middleware авторизации.

На каждый апдейт открывает транзакцию, находит сотрудника по Telegram ID
и кладёт в контекст обработчика: session, user, grants, roles, organization.

Пользователь, не прошедший подтверждение, дальше экрана регистрации не проходит:
проверка выполняется здесь, а не в каждом обработчике по отдельности.
"""
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from app.core.db import session_scope
from app.core.timeutil import utcnow
from app.models.enums import UserStatus
from app.services.bootstrap import ensure_organization
from app.services.rbac import load_grants, user_role_codes
from app.services.registration import get_user_by_telegram_id

# Что доступно до подтверждения регистрации.
PUBLIC_COMMANDS = {"/start", "/help", "/id"}
PUBLIC_CALLBACK_PREFIXES = ("reg:",)


class AuthMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        telegram_user = data.get("event_from_user")
        if telegram_user is None:
            return await handler(event, data)

        async with session_scope() as session:
            organization = await ensure_organization(session)
            user = await get_user_by_telegram_id(session, telegram_user.id)

            data["session"] = session
            data["organization"] = organization
            data["user"] = user
            data["grants"] = await load_grants(session, user) if user else {}
            data["roles"] = await user_role_codes(session, user) if user else set()

            if user is not None:
                user.last_seen_at = utcnow()
                if user.telegram_username != telegram_user.username:
                    user.telegram_username = telegram_user.username

            if not self._is_allowed(event, user):
                await self._reject(event, user)
                return None

            return await handler(event, data)

    @staticmethod
    def _is_allowed(event: TelegramObject, user) -> bool:
        if user is not None and user.status == UserStatus.ACTIVE:
            return True

        if isinstance(event, Message):
            text = (event.text or "").split()[0] if event.text else ""
            if text in PUBLIC_COMMANDS:
                return True
            # Контакт нужен на последнем шаге регистрации.
            return event.contact is not None or user is None or user.status == UserStatus.PENDING
        if isinstance(event, CallbackQuery):
            return (event.data or "").startswith(PUBLIC_CALLBACK_PREFIXES)
        return False

    @staticmethod
    async def _reject(event: TelegramObject, user) -> None:
        if user is None:
            text = "Чтобы пользоваться системой, нажмите /start и пройдите регистрацию."
        elif user.status == UserStatus.PENDING:
            text = "Ваша заявка на рассмотрении у администратора. Мы сообщим, как только её подтвердят."
        elif user.status == UserStatus.SUSPENDED:
            text = "Доступ приостановлен. Обратитесь к администратору."
        else:
            text = "Доступ к системе не открыт. Обратитесь к администратору."

        if isinstance(event, Message):
            await event.answer(text)
        elif isinstance(event, CallbackQuery):
            await event.answer(text, show_alert=True)
