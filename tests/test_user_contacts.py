import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite
from aiogram import Bot, Dispatcher
from aiogram.types import Update

from database import Database
from handlers.start import create_start_router


class UserContactTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.config = SimpleNamespace(
            COLLECT_USER_CONTACT_DATA=True, ADMIN_ID=99, BOT_USERNAME='test',
            MAX_SEARCH_RESULTS=50, MAX_PLAYLIST_RESULTS=5000,
        )
        self.database = Database(self.config)
        self.database.users = await aiosqlite.connect(':memory:')
        await self.database._migrate_users()
        self.bot = Bot('123:TEST')
        self.bot.session = AsyncMock()
        self.dispatcher = Dispatcher()
        self.dispatcher.include_router(create_start_router(self.config, self.database))

    async def asyncTearDown(self):
        await self.database.close()

    async def send(self, username='first', **payload):
        message = {
            'message_id': 1, 'date': 1, 'chat': {'id': 42, 'type': 'private'},
            'from': {'id': 42, 'is_bot': False, 'first_name': 'Test', 'username': username},
            **payload,
        }
        await self.dispatcher.feed_update(
            self.bot, Update.model_validate({'update_id': 1, 'message': message}),
        )

    async def rows(self):
        async with self.database.users.execute(
            'SELECT id, username, phone, location FROM users ORDER BY id'
        ) as cursor:
            return await cursor.fetchall()

    async def test_profile_updates_preserve_contact_and_location(self):
        await self.send(text='/start')
        keyboard = self.bot.session.call_args.args[1].reply_markup
        buttons = [button for row in keyboard.keyboard for button in row]
        self.assertTrue(any(button.request_contact for button in buttons))
        self.assertTrue(any(button.request_location for button in buttons))
        await self.send(contact={'phone_number': '+380000000000', 'first_name': 'Test', 'user_id': 42})
        await self.send(location={'latitude': 50.45, 'longitude': 30.52})
        await self.send(username='renamed', text='/start')
        self.assertEqual(await self.rows(), [(42, 'renamed', '+380000000000', '50.45,30.52')])
        await self.send(username=None, text='/start')
        self.assertEqual(await self.rows(), [(42, None, '+380000000000', '50.45,30.52')])

    async def test_foreign_and_unverified_contacts_cannot_overwrite_phone(self):
        await self.send(contact={'phone_number': '+380000000000', 'first_name': 'Test', 'user_id': 42})
        for identity in ({'user_id': 77}, {}):
            await self.send(contact={'phone_number': '+380111111111', 'first_name': 'Other', **identity})
        self.assertEqual(await self.rows(), [(42, 'first', '+380000000000', None)])

    async def test_disabled_collection_does_not_store_contact_or_location(self):
        self.config.COLLECT_USER_CONTACT_DATA = False
        await self.send(contact={'phone_number': '+380000000000', 'first_name': 'Test', 'user_id': 42})
        await self.send(location={'latitude': 50.45, 'longitude': 30.52})
        self.assertEqual(await self.rows(), [])
