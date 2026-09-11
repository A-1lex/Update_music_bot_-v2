"""Register Telegram's command menu without changing message keyboards."""

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault, MenuButtonCommands


logger = logging.getLogger(__name__)

USER_COMMANDS = (
    ("start", "Відкрити головне меню"),
    ("cancel", "Скасувати свої завантаження"),
)
ADMIN_COMMANDS = (
    ("music", "Опублікувати аудіо в канал: /music URL"),
    ("video", "Опублікувати відео в канал: /video URL"),
    ("status", "Стан бота та завантажень"),
    ("show_tables", "Переглянути таблиці бази даних"),
    ("delete", "Вибрати файл для видалення"),
    ("restart", "Перезапустити бота"),
    ("security_status", "Стан безпеки та доступу"),
    ("security_mode", "Режим доступу: /security_mode public|whitelist|private"),
    ("lockdown", "Закрити доступ усім, крім адміністратора"),
    ("ban", "Заблокувати користувача: /ban ID"),
    ("unban", "Розблокувати користувача: /unban ID"),
    ("allow", "Додати в білий список: /allow ID"),
    ("disallow", "Видалити з білого списку: /disallow ID"),
)


async def configure_command_menu(bot: Bot, admin_id: int) -> None:
    scopes = [(BotCommandScopeDefault(), USER_COMMANDS)]
    if admin_id > 0:
        scopes.append((BotCommandScopeChat(chat_id=admin_id), USER_COMMANDS + ADMIN_COMMANDS))
    for scope, entries in scopes:
        try:
            await bot.set_my_commands(
                [BotCommand(command=name, description=description) for name, description in entries],
                scope=scope,
                request_timeout=10,
            )
        except TelegramAPIError:
            logger.exception("Не вдалося налаштувати команди меню: scope=%s", scope.type)

    try:
        await bot.set_chat_menu_button(menu_button=MenuButtonCommands(), request_timeout=10)
    except TelegramAPIError:
        logger.exception("Не вдалося налаштувати кнопку меню Telegram")
