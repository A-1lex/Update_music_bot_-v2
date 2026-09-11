from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram import Bot, Dispatcher
from aiogram.types import Update

from handlers.security import SECURITY_PROMPTS, create_security_router
from keyboards.admin import SECURITY_BUTTONS, build_main_keyboard


class SecurityButtonTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = Bot("123:TEST")
        self.bot.session = AsyncMock()
        self.security = SimpleNamespace(
            unban_user=AsyncMock(), set_access_mode=AsyncMock(), access_mode="private",
            set_allowlisted=AsyncMock(),
            ban_user=AsyncMock(return_value=SimpleNamespace(permanent_ban=False)),
        )
        result = SimpleNamespace(queued_cancelled=0, active_found=0, active_stopped=0)
        self.manager = SimpleNamespace(
            cancel_user=AsyncMock(return_value=result), cancel_non_admin=AsyncMock(return_value=result),
        )
        self.dispatcher = Dispatcher()
        self.dispatcher.include_router(create_security_router(
            config=SimpleNamespace(ADMIN_ID=42, ADMIN_PRIVATE_CHAT_ONLY=True, SECURITY_WHITELIST_IDS=()),
            security=self.security, manager=self.manager,
        ))

    async def send(self, text, *, user=42, chat_type="private", action=None, author=123):
        message = {
            "message_id": 2, "date": 1, "chat": {"id": 42, "type": chat_type},
            "from": {"id": user, "is_bot": False, "first_name": "Test"}, "text": text,
        }
        if action:
            message["reply_to_message"] = {
                "message_id": 1, "date": 1, "chat": message["chat"],
                "from": {"id": author, "is_bot": True, "first_name": "Bot"},
                "text": SECURITY_PROMPTS[action],
            }
        await self.dispatcher.feed_update(self.bot, Update.model_validate({"update_id": 1, "message": message}))

    async def test_buttons_are_admin_only(self):
        def labels(admin):
            return {button.text for row in build_main_keyboard(admin).keyboard for button in row}
        self.assertTrue(set(SECURITY_BUTTONS) <= labels(True))
        self.assertFalse(set(SECURITY_BUTTONS) & labels(False))
        await self.send("🚨 Закрити доступ усім, крім адміна", user=99)
        await self.send("🚨 Закрити доступ усім, крім адміна", chat_type="group")
        self.security.set_access_mode.assert_not_awaited()

    async def test_button_prompts_and_reply_executes(self):
        await self.send("✅ Розблокувати користувача")
        request = self.bot.session.call_args.args[1]
        self.assertEqual(request.text, SECURITY_PROMPTS["unban"])
        self.assertTrue(request.reply_markup.force_reply)
        await self.send("77", action="unban")
        self.security.unban_user.assert_awaited_once_with(77)

    async def test_invalid_or_unauthorized_reply_does_not_mutate(self):
        await self.send("77", action="unban", user=99)
        await self.send("77", action="unban", author=999)
        await self.send("wrong", action="unban")
        await self.send("-1", action="unban")
        self.security.unban_user.assert_not_awaited()

    async def test_commands_still_work(self):
        await self.send("/unban 77")
        self.security.unban_user.assert_awaited_once_with(77)

    async def test_ban_arguments_and_allowlist_actions(self):
        await self.send("77 60 spam", action="ban")
        self.security.ban_user.assert_awaited_once_with(77, 60, "spam")
        self.manager.cancel_user.assert_awaited_once_with(77)
        await self.send("77", action="allow")
        self.security.set_allowlisted.assert_awaited_with(77, True, added_by=42)
        await self.send("77", action="disallow")
        self.security.set_allowlisted.assert_awaited_with(77, False, added_by=42)

    async def test_mode_and_lockdown(self):
        await self.send("private", action="security_mode")
        self.security.set_access_mode.assert_awaited_with("private")
        await self.send("🚨 Закрити доступ усім, крім адміна")
        self.manager.cancel_non_admin.assert_awaited_once_with(42)
