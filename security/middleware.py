from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any, Optional

from aiogram import BaseMiddleware, Bot
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, Message, TelegramObject, Update

from config import Config
from security.service import AccessDecision, RateDecision, SecurityService
from workers.download_worker import DownloadManager


logger = logging.getLogger(__name__)


class SecurityMiddleware(BaseMiddleware):
    """Перший захисний шар для всіх Telegram updates.

    Порядок навмисний: private-chat policy -> ban/access mode -> user limits ->
    global limit -> handlers. Тому заблокований/спамний user не витрачає
    global bucket і не доходить до дорогих yt-dlp/SQLite операцій.
    """

    def __init__(
        self,
        config: Config,
        security: SecurityService,
        manager: DownloadManager,
    ) -> None:
        self.config = config
        self.security = security
        self.manager = manager

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, Update):
            return await handler(event, data)

        user_id, chat_type, message, callback = self._context(event)
        if user_id is None:
            return await handler(event, data)

        is_admin = self.security.is_admin(user_id)

        # Групові update відсікаємо до rate buckets і без відповіді, щоб бот не
        # став response-amplifier у чужій групі.
        if is_admin:
            if self.config.ADMIN_PRIVATE_CHAT_ONLY and chat_type not in {None, ChatType.PRIVATE}:
                logger.warning(
                    "Security: admin update поза private chat проігноровано user_id=%s type=%s",
                    user_id,
                    chat_type,
                )
                return None
        elif self.config.USER_PRIVATE_CHAT_ONLY and chat_type not in {None, ChatType.PRIVATE}:
            return None

        access = await self.security.check_access(user_id)
        if not access.allowed:
            await self._notify_access_denied(
                user_id=user_id,
                access=access,
                message=message,
                callback=callback,
                bot=data.get("bot"),
            )
            return None

        rate = await self.security.check_update(
            user_id,
            is_callback=callback is not None,
        )
        if not rate.allowed:
            if rate.auto_banned:
                # Auto-ban має негайно прибрати вже набиту користувачем чергу.
                await self.manager.cancel_user(user_id)
            await self._notify_rate_denied(
                user_id=user_id,
                rate=rate,
                message=message,
                callback=callback,
                bot=data.get("bot"),
            )
            return None

        data["security_service"] = self.security
        return await handler(event, data)

    @staticmethod
    def _context(
        update: Update,
    ) -> tuple[Optional[int], Optional[ChatType], Optional[Message], Optional[CallbackQuery]]:
        callback = update.callback_query
        if callback is not None:
            chat = getattr(callback.message, "chat", None)
            return (
                callback.from_user.id,
                getattr(chat, "type", None),
                callback.message if isinstance(callback.message, Message) else None,
                callback,
            )

        message = update.message or update.edited_message
        if message is not None:
            user = message.from_user
            return (
                user.id if user is not None else None,
                message.chat.type,
                message,
                None,
            )

        # Для інших update-типів використовуємо event.from_user, якщо він є.
        inner = update.event
        user = getattr(inner, "from_user", None)
        chat = getattr(inner, "chat", None)
        return (
            getattr(user, "id", None),
            getattr(chat, "type", None),
            None,
            None,
        )

    async def _notify_access_denied(
        self,
        *,
        user_id: int,
        access: AccessDecision,
        message: Optional[Message],
        callback: Optional[CallbackQuery],
        bot: Optional[Bot],
    ) -> None:
        if not await self.security.should_notify(user_id, "access-denied"):
            return
        text = self.security.format_access_denial(access)
        await self._safe_notify(text=text, message=message, callback=callback, bot=bot, user_id=user_id)

    async def _notify_rate_denied(
        self,
        *,
        user_id: int,
        rate: RateDecision,
        message: Optional[Message],
        callback: Optional[CallbackQuery],
        bot: Optional[Bot],
    ) -> None:
        category = "auto-ban" if rate.auto_banned else "rate-limit"
        cooldown = 5 if rate.auto_banned else self.config.SECURITY_BAN_NOTICE_COOLDOWN_SECONDS
        if not await self.security.should_notify(user_id, category, cooldown=cooldown):
            return
        text = self.security.format_rate_denial(rate)
        await self._safe_notify(text=text, message=message, callback=callback, bot=bot, user_id=user_id)

    @staticmethod
    async def _safe_notify(
        *,
        text: str,
        message: Optional[Message],
        callback: Optional[CallbackQuery],
        bot: Optional[Bot],
        user_id: int,
    ) -> None:
        try:
            if callback is not None:
                await callback.answer(text=text[:180], show_alert=True)
                return
            if message is not None:
                await message.answer(text)
                return
            if bot is not None:
                await bot.send_message(user_id, text)
        except TelegramAPIError:
            logger.debug(
                "Security: не вдалося надіслати обмежувальне повідомлення user_id=%s",
                user_id,
                exc_info=True,
            )
