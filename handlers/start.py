from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import Message

from config import Config
from database import Database
from keyboards.admin import build_main_keyboard


logger = logging.getLogger(__name__)


def create_start_router(config: Config, database: Database) -> Router:
    router = Router(name="start")

    @router.message(Command("start"))
    async def start_handler(message: Message) -> None:
        if message.from_user is None:
            return

        user_id = message.from_user.id
        try:
            await database.save_user(user_id, message.from_user.username)
            welcome_message = (
                f"👋 Вітаю! Я {config.BOT_USERNAME} — бот для завантаження "
                "аудіо та відео з YouTube.\n\n"
                "📌 <b>Функціонал:</b>\n"
                "• URL відео, Shorts або плейлиста YouTube.\n"
                f"• Пошук — до {config.MAX_SEARCH_RESULTS} результатів.\n"
                f"• Плейлисти — до {config.MAX_PLAYLIST_RESULTS} треків.\n"
                "• YouTube Mix із конкретним відео обробляється як одне відео.\n"
                "• <code>/cancel</code> — скасувати свої завантаження."
            )
            if user_id == config.ADMIN_ID:
                welcome_message += (
                    "\n\n🛡 <b>Адміністратор:</b>\n"
                    "• <code>/music URL</code>, <code>/video URL</code> — публікація в канали.\n"
                    "• <code>/delete</code>, <code>/restart</code>, <code>/show_tables</code>, "
                    "<code>/status</code>.\n"
                    "• <code>/security_status</code>, <code>/security_mode</code>, "
                    "<code>/lockdown</code>, <code>/ban</code>, <code>/unban</code>, "
                    "<code>/allow</code>, <code>/disallow</code>."
                )
            await message.answer(
                welcome_message,
                reply_markup=build_main_keyboard(
                    user_id == config.ADMIN_ID,
                    collect_user_contact_data=config.COLLECT_USER_CONTACT_DATA,
                ),
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            logger.exception("Помилка /start user_id=%s", user_id)
            await message.answer("❌ Не вдалося виконати команду /start.")


    @router.message(F.text == "🔎 Пошук")
    async def search_hint_handler(message: Message) -> None:
        await message.answer("Надішліть назву пісні, відео або YouTube-посилання.")

    @router.message(F.contact)
    async def contact_handler(message: Message) -> None:
        if not config.COLLECT_USER_CONTACT_DATA:
            await message.answer("ℹ️ Збір контактних даних вимкнено адміністратором.")
            return
        if message.from_user is None or message.contact is None:
            return

        user_id = message.from_user.id
        try:
            if message.contact.user_id != user_id:
                await message.answer("❌ Можна зберегти лише ваш власний контакт.")
                return

            await database.save_user(user_id, message.from_user.username)
            await database.save_contact(user_id, message.contact.phone_number)
            await message.answer("✅ Дякую за надання номера телефону.")
            logger.info("Оновлено номер телефону user_id=%s", user_id)
        except Exception:
            logger.exception("Помилка збереження контакту user_id=%s", user_id)
            await message.answer("❌ Не вдалося обробити контакт.")

    @router.message(F.location)
    async def location_handler(message: Message) -> None:
        if not config.COLLECT_USER_CONTACT_DATA:
            await message.answer("ℹ️ Збір геолокації вимкнено адміністратором.")
            return
        if message.from_user is None or message.location is None:
            return

        user_id = message.from_user.id
        try:
            await database.save_user(user_id, message.from_user.username)
            await database.save_location(
                user_id,
                message.location.latitude,
                message.location.longitude,
            )
            await message.answer("✅ Дякую за надання місця розташування.")
            logger.info("Оновлено локацію user_id=%s", user_id)
        except Exception:
            logger.exception("Помилка збереження локації user_id=%s", user_id)
            await message.answer("❌ Не вдалося обробити локацію.")

    return router
