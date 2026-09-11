from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import Message

from config import Config
from services.downloader import DownloadTask, MediaType
from services.youtube import YouTubeService
from security.service import SecurityService
from workers.download_worker import DownloadManager


logger = logging.getLogger(__name__)


def _command_argument(message: Message) -> str:
    text = (message.text or "").strip()
    parts = text.split(maxsplit=1)
    return parts[1].strip() if len(parts) == 2 else ""


async def _enqueue_channel_command(
    *,
    message: Message,
    config: Config,
    youtube: YouTubeService,
    manager: DownloadManager,
    security: SecurityService,
    media_type: MediaType,
) -> None:
    if message.from_user is None:
        return

    # Security v3: channel publishing НІКОЛИ не відкривається звичайним
    # користувачам навіть якщо старий CHANNEL_COMMANDS_ADMIN_ONLY=false лишився в .env.
    if config.ADMIN_ID <= 0 or message.from_user.id != config.ADMIN_ID:
        await message.answer(
            "⛔ Команди /music і /video для публікації в канали "
            "доступні лише адміністратору."
        )
        return

    url = _command_argument(message)
    if not url:
        command = "/music" if media_type is MediaType.AUDIO else "/video"
        await message.answer(
            f"❌ Використання: <code>{command} URL</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    channel = (
        config.CHANNEL_MUSIC
        if media_type is MediaType.AUDIO
        else config.CHANNEL_VIDEO
    )
    if not channel:
        variable = "CHANNEL_MUSIC" if media_type is MediaType.AUDIO else "CHANNEL_VIDEO"
        await message.answer(f"❌ У .env не налаштовано {variable}.")
        return

    reference = youtube.parse_reference(url)
    if not reference.is_youtube or not reference.video_id:
        await message.answer(
            "❌ Для цієї команди потрібне посилання на конкретне YouTube-відео."
        )
        return

    security_decision = await security.check_action(
        message.from_user.id,
        "download",
        units=1,
    )
    if not security_decision.allowed:
        await message.answer(security.format_rate_denial(security_decision))
        return

    task = DownloadTask(
        user_id=message.from_user.id,
        video_id=reference.video_id,
        media_type=media_type,
        target_chat_id=channel,
        notify_chat_id=message.chat.id,
    )
    enqueue_result = await manager.enqueue_detailed(task)
    if enqueue_result.added == 0:
        if enqueue_result.duplicate:
            await message.answer("ℹ️ Це завдання вже є у вашій черзі або виконується.")
        elif enqueue_result.rejected_global_limit:
            await message.answer(
                "⚠️ Загальна черга бота заповнена. Спробуйте трохи пізніше."
            )
        else:
            await message.answer(
                "⚠️ Ваша черга заповнена. Дочекайтеся завершення частини завантажень."
            )
        return

    label = "Аудіо" if media_type is MediaType.AUDIO else "Відео"
    await message.answer(
        f"⏳ {label} додано в чергу для публікації в канал. "
        "Бот продовжує приймати інші команди."
    )


def create_downloads_router(
    *,
    config: Config,
    youtube: YouTubeService,
    manager: DownloadManager,
    security: SecurityService,
) -> Router:
    router = Router(name="downloads")

    @router.message(Command("music"))
    async def music_handler(message: Message) -> None:
        try:
            await _enqueue_channel_command(
                message=message,
                config=config,
                youtube=youtube,
                manager=manager,
                security=security,
                media_type=MediaType.AUDIO,
            )
        except Exception:
            logger.exception(
                "Помилка /music user_id=%s",
                message.from_user.id if message.from_user else None,
            )
            await message.answer("❌ Не вдалося додати аудіо в чергу.")

    @router.message(Command("video"))
    async def video_handler(message: Message) -> None:
        try:
            await _enqueue_channel_command(
                message=message,
                config=config,
                youtube=youtube,
                manager=manager,
                security=security,
                media_type=MediaType.VIDEO,
            )
        except Exception:
            logger.exception(
                "Помилка /video user_id=%s",
                message.from_user.id if message.from_user else None,
            )
            await message.answer("❌ Не вдалося додати відео в чергу.")

    @router.message(Command("cancel"))
    async def cancel_handler(message: Message) -> None:
        if message.from_user is None:
            return

        user_id = message.from_user.id
        try:
            result = await manager.cancel_user(user_id)
            if result.queued_cancelled == 0 and result.active_found == 0:
                await message.answer("ℹ️ У вас немає завантажень для скасування.")
                return

            lines = [f"✅ Скасовано завдань у черзі: {result.queued_cancelled}."]
            if result.active_found == 0:
                lines.append("▶️ Активних завантажень не було.")
            elif result.active_still_stopping == 0:
                lines.append(
                    f"⏹ Активних завантажень зупинено: {result.active_stopped}."
                )
            else:
                lines.append(
                    f"⏹ Активних завантажень уже зупинено: {result.active_stopped} "
                    f"із {result.active_found}."
                )
                lines.append(
                    f"⏳ Ще завершують безпечне скасування: "
                    f"{result.active_still_stopping}."
                )

            await message.answer("\n".join(lines))
        except Exception:
            logger.exception("Помилка /cancel user_id=%s", user_id)
            await message.answer("❌ Не вдалося скасувати завантаження.")

    return router
