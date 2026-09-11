from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.enums import ChatType, ParseMode
from aiogram.filters import Command
from aiogram.types import ForceReply, Message

from config import Config
from keyboards.admin import SECURITY_BUTTONS
from security.service import SecurityService
from workers.download_worker import DownloadManager
from utils.html import escape_html


logger = logging.getLogger(__name__)

SECURITY_PROMPTS = {
    "security_mode": "🔐 Введіть режим доступу: public — усі, whitelist — білий список, private — лише адміністратор.",
    "ban": "⛔ Введіть ID користувача, за потреби — тривалість у хвилинах і причину.\nНаприклад: 123456789 60 спам\nБез тривалості або 0 — назавжди.",
    "unban": "✅ Введіть ID користувача для розблокування.",
    "allow": "➕ Введіть ID користувача для додавання в білий список.",
    "disallow": "➖ Введіть ID користувача для видалення з білого списку.",
}


def _admin_private(message: Message, config: Config) -> bool:
    if message.from_user is None:
        return False
    if config.ADMIN_ID <= 0 or message.from_user.id != config.ADMIN_ID:
        return False
    if config.ADMIN_PRIVATE_CHAT_ONLY and message.chat.type != ChatType.PRIVATE:
        return False
    return True


def _parts(message: Message) -> list[str]:
    return (message.text or "").strip().split()


def _parse_user_id(raw: str) -> int:
    try:
        user_id = int(raw)
    except ValueError as exc:
        raise ValueError("USER_ID має бути цілим числом") from exc
    if user_id <= 0:
        raise ValueError("USER_ID має бути додатним")
    return user_id


def create_security_router(
    *,
    config: Config,
    security: SecurityService,
    manager: DownloadManager,
) -> Router:
    router = Router(name="security-admin")

    @router.message(Command("security_status"))
    async def security_status(message: Message) -> None:
        if not _admin_private(message, config):
            return
        status = await security.status()
        active = await manager.active_job_count()
        queued = await manager.queued_job_count()
        await message.answer(
            "🛡 <b>Статус безпеки v3</b>\n"
            f"🔐 Режим доступу: <b>{status.access_mode}</b>\n"
            f"✅ Білий список: <b>{status.effective_allowlist_count}</b> "
            f"(env={status.env_allowlist_count}, БД={status.db_allowlist_count})\n"
            f"⛔ Активні блокування: <b>{status.active_bans}</b>, постійні: <b>{status.permanent_bans}</b>\n"
            f"👥 Security-записи у БД: <b>{status.tracked_security_users}</b>\n\n"
            f"📨 Ліміт одночасної обробки updates: <b>{config.POLLING_TASKS_CONCURRENCY_LIMIT}</b>\n"
            f"📥 Черга: очікують=<b>{queued}</b>, активні=<b>{active}</b>\n"
            f"👤 Hard cap черги/користувач: <b>{config.EFFECTIVE_MAX_QUEUE_SIZE}</b>\n"
            f"🌐 Глобальний hard cap черги: <b>{config.EFFECTIVE_MAX_GLOBAL_QUEUE_SIZE}</b>\n"
            f"⚙️ Активних завантажень/користувач: <b>{config.MAX_ACTIVE_DOWNLOADS_PER_USER}</b>\n"
            f"🔎 Search-сесій/користувач: <b>{config.MAX_SEARCH_SESSIONS_PER_USER}</b>\n"
            f"🧠 Search RAM cache hard cap: <b>{config.EFFECTIVE_SEARCH_MEMORY_CACHE_SIZE}</b>\n\n"
            f"🚦 Updates/користувач: <b>{config.SECURITY_GENERAL_USER_LIMIT}</b>/{config.SECURITY_GENERAL_WINDOW_SECONDS}s\n"
            f"⚡ Burst/користувач: <b>{config.SECURITY_BURST_USER_LIMIT}</b>/{config.SECURITY_BURST_WINDOW_SECONDS}s\n"
            f"🔎 Пошук/користувач: <b>{config.SECURITY_SEARCH_USER_LIMIT}</b>/{config.SECURITY_SEARCH_WINDOW_SECONDS}s\n"
            f"📚 Плейлист/користувач: <b>{config.SECURITY_PLAYLIST_USER_LIMIT}</b>/{config.SECURITY_PLAYLIST_WINDOW_SECONDS}s\n"
            f"⬇️ Download units/користувач: <b>{config.SECURITY_DOWNLOAD_USER_LIMIT}</b>/{config.SECURITY_DOWNLOAD_WINDOW_SECONDS}s",
            parse_mode=ParseMode.HTML,
        )

    @router.message(Command("security_mode"))
    async def security_mode(message: Message, arguments: list[str] | None = None) -> None:
        if not _admin_private(message, config):
            return
        parts = arguments if arguments is not None else _parts(message)
        if len(parts) != 2:
            await message.answer(
                "Використання: <code>/security_mode public|whitelist|private</code>",
                parse_mode=ParseMode.HTML,
            )
            return
        try:
            await security.set_access_mode(parts[1])
        except ValueError as exc:
            await message.answer(f"❌ {exc}")
            return
        await message.answer(f"✅ Режим доступу змінено на: <b>{security.access_mode}</b>", parse_mode=ParseMode.HTML)

    @router.message(Command("lockdown"))
    async def lockdown(message: Message) -> None:
        if not _admin_private(message, config):
            return
        await security.set_access_mode("private")
        result = await manager.cancel_non_admin(config.ADMIN_ID)
        logger.warning(
            "SECURITY LOCKDOWN admin_id=%s queued_cancelled=%s active=%s stopped=%s",
            config.ADMIN_ID,
            result.queued_cancelled,
            result.active_found,
            result.active_stopped,
        )
        await message.answer(
            "🚨 <b>LOCKDOWN увімкнено</b>\n"
            "Режим: <b>private</b>\n"
            f"Скасовано queued non-admin: <b>{result.queued_cancelled}</b>\n"
            f"Активних non-admin знайдено: <b>{result.active_found}</b>\n"
            f"Вже зупинено: <b>{result.active_stopped}</b>",
            parse_mode=ParseMode.HTML,
        )

    @router.message(Command("ban"))
    async def ban(message: Message, arguments: list[str] | None = None) -> None:
        if not _admin_private(message, config):
            return
        parts = arguments if arguments is not None else _parts(message)
        if len(parts) < 2:
            await message.answer(
                "Використання: <code>/ban USER_ID [MINUTES] [reason]</code>\n"
                "MINUTES=0 — permanent.",
                parse_mode=ParseMode.HTML,
            )
            return
        try:
            user_id = _parse_user_id(parts[1])
            minutes = 0
            reason_start = 2
            if len(parts) >= 3:
                try:
                    minutes = int(parts[2])
                    reason_start = 3
                except ValueError:
                    minutes = 0
                    reason_start = 2
            if minutes < 0:
                raise ValueError("MINUTES не може бути від'ємним")
            reason = " ".join(parts[reason_start:]).strip() or "Заблоковано адміністратором"
            state = await security.ban_user(user_id, minutes, reason)
            cancel = await manager.cancel_user(user_id)
        except ValueError as exc:
            await message.answer(f"❌ {exc}")
            return

        duration = "permanent" if state.permanent_ban else f"{minutes} хв"
        await message.answer(
            f"⛔ USER_ID <code>{user_id}</code> заблоковано: <b>{duration}</b>.\n"
            f"Причина: {escape_html(reason)}\n"
            f"Скасовано queued: {cancel.queued_cancelled}; active: {cancel.active_found}.",
            parse_mode=ParseMode.HTML,
        )

    @router.message(Command("unban"))
    async def unban(message: Message, arguments: list[str] | None = None) -> None:
        if not _admin_private(message, config):
            return
        parts = arguments if arguments is not None else _parts(message)
        if len(parts) != 2:
            await message.answer("Використання: <code>/unban USER_ID</code>", parse_mode=ParseMode.HTML)
            return
        try:
            user_id = _parse_user_id(parts[1])
            await security.unban_user(user_id)
        except ValueError as exc:
            await message.answer(f"❌ {exc}")
            return
        await message.answer(f"✅ USER_ID <code>{user_id}</code> розблоковано.", parse_mode=ParseMode.HTML)

    @router.message(Command("allow"))
    async def allow(message: Message, arguments: list[str] | None = None) -> None:
        if not _admin_private(message, config):
            return
        parts = arguments if arguments is not None else _parts(message)
        if len(parts) != 2:
            await message.answer("Використання: <code>/allow USER_ID</code>", parse_mode=ParseMode.HTML)
            return
        try:
            user_id = _parse_user_id(parts[1])
            await security.set_allowlisted(user_id, True, added_by=config.ADMIN_ID)
        except ValueError as exc:
            await message.answer(f"❌ {exc}")
            return
        await message.answer(f"✅ USER_ID <code>{user_id}</code> додано у whitelist.", parse_mode=ParseMode.HTML)

    @router.message(Command("disallow"))
    async def disallow(message: Message, arguments: list[str] | None = None) -> None:
        if not _admin_private(message, config):
            return
        parts = arguments if arguments is not None else _parts(message)
        if len(parts) != 2:
            await message.answer("Використання: <code>/disallow USER_ID</code>", parse_mode=ParseMode.HTML)
            return
        try:
            user_id = _parse_user_id(parts[1])
            await security.set_allowlisted(user_id, False, added_by=config.ADMIN_ID)
        except ValueError as exc:
            await message.answer(f"❌ {exc}")
            return
        if user_id in config.SECURITY_WHITELIST_IDS:
            await message.answer(
                f"⚠️ USER_ID <code>{user_id}</code> видалено з DB whitelist, але він лишається "
                "дозволеним через SECURITY_WHITELIST_IDS у .env.",
                parse_mode=ParseMode.HTML,
            )
        else:
            await message.answer(f"✅ USER_ID <code>{user_id}</code> видалено з whitelist.", parse_mode=ParseMode.HTML)

    actions = {
        "security_status": security_status, "security_mode": security_mode,
        "lockdown": lockdown, "ban": ban, "unban": unban,
        "allow": allow, "disallow": disallow,
    }

    @router.message(F.text.in_(SECURITY_BUTTONS))
    async def security_button(message: Message) -> None:
        if not _admin_private(message, config):
            return
        action = SECURITY_BUTTONS[message.text]
        if action in SECURITY_PROMPTS:
            await message.answer(
                SECURITY_PROMPTS[action],
                reply_markup=ForceReply(selective=True, input_field_placeholder="Введіть відповідь"),
            )
        else:
            await actions[action](message)

    @router.message(F.text, F.reply_to_message.text.in_(set(SECURITY_PROMPTS.values())))
    async def security_answer(message: Message, bot: Bot) -> None:
        if not _admin_private(message, config):
            return
        prompt = message.reply_to_message
        if prompt.from_user is None or prompt.from_user.id != bot.id:
            return
        action = next(key for key, text in SECURITY_PROMPTS.items() if text == prompt.text)
        await actions[action](message, [action, *message.text.strip().split()])

    return router
