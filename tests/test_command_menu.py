import unittest
from unittest.mock import AsyncMock

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SetMyCommands

from utils.command_menu import configure_command_menu


class CommandMenuTests(unittest.IsolatedAsyncioTestCase):
    async def test_admin_commands_are_registered_only_for_admin_chat(self):
        bot = Bot('123:TEST')
        bot.session = AsyncMock()
        await configure_command_menu(bot, 42)
        requests = [call.args[1] for call in bot.session.await_args_list]
        public, admin, button = requests
        self.assertEqual(public.scope.type, 'default')
        self.assertEqual({c.command for c in public.commands}, {'start', 'cancel'})
        self.assertEqual(admin.scope.type, 'chat')
        self.assertEqual(admin.scope.chat_id, 42)
        self.assertEqual({c.command for c in admin.commands}, {
            'start', 'cancel', 'music', 'video', 'status', 'show_tables', 'delete',
            'restart', 'security_status', 'security_mode', 'lockdown',
            'ban', 'unban', 'allow', 'disallow',
        })
        self.assertEqual(button.menu_button.type, 'commands')

    async def test_unconfigured_admin_has_no_private_scope(self):
        bot = Bot('123:TEST')
        bot.session = AsyncMock()
        await configure_command_menu(bot, 0)
        self.assertEqual(bot.session.await_count, 2)

    async def test_api_failure_is_logged_and_menu_button_still_configured(self):
        bot = Bot('123:TEST')
        bot.session = AsyncMock(side_effect=[
            True,
            TelegramBadRequest(method=SetMyCommands(commands=[]), message='chat not found'),
            True,
        ])
        with self.assertLogs('utils.command_menu', level='ERROR'):
            await configure_command_menu(bot, 42)
        self.assertEqual(bot.session.await_count, 3)
        self.assertEqual(bot.session.call_args.args[1].menu_button.type, 'commands')
