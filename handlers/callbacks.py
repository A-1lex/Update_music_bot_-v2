from __future__ import annotations

import logging

from aiogram import Bot, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, Message

from config import Config
from database import Database
from handlers.search import render_results_page
from keyboards.search import (
    AudioCallbackData,
    DownloadAllCallbackData,
    PaginationCallbackData,
    VideoCallbackData,
)
from services.downloader import DownloadTask, MediaType
from security.service import SecurityService
from workers.download_worker import DownloadManager


logger = logging.getLogger(__name__)


async def _answer_callback(
    callback: CallbackQuery,
    text: str | None = None,
    *,
    show_alert: bool = False,
) -> None:
    try:
        await callback.answer(text=text, show_alert=show_alert)
    except TelegramAPIError:
        logger.debug(
            "Не вдалося відповісти на callback callback_id=%s",
            callback.id,
            exc_info=True,
        )


def _callback_message(callback: CallbackQuery) -> Message | None:
    return callback.message if isinstance(callback.message, Message) else None


def _owned_by_user(callback: CallbackQuery, expected_user_id: int) -> bool:
    return callback.from_user.id == expected_user_id


async def _enqueue_single(
    *,
    callback: CallbackQuery,
    manager: DownloadManager,
    security: SecurityService,
    video_id: str,
    media_type: MediaType,
    owner_user_id: int,
) -> None:
    if not _owned_by_user(callback, owner_user_id):
        await _answer_callback(
            callback,
            "Ця кнопка належить іншому користувачу.",
            show_alert=True,
        )
        return

    message = _callback_message(callback)
    if message is None:
        await _answer_callback(
            callback,
            "Не вдалося визначити чат для завантаження.",
            show_alert=True,
        )
        return

    security_decision = await security.check_action(owner_user_id, "download", units=1)
    if not security_decision.allowed:
        if security_decision.auto_banned:
            await manager.cancel_user(owner_user_id)
        if await security.should_notify(owner_user_id, "action:download", cooldown=10):
            await _answer_callback(
                callback,
                security.format_rate_denial(security_decision),
                show_alert=True,
            )
        else:
            await _answer_callback(callback)
        return

    task = DownloadTask(
        user_id=owner_user_id,
        video_id=video_id,
        media_type=media_type,
        target_chat_id=message.chat.id,
    )
    enqueue_result = await manager.enqueue_detailed(task)
    if enqueue_result.added == 0:
        if enqueue_result.duplicate:
            text = "Цей файл уже є у вашій черзі або зараз завантажується."
        elif enqueue_result.rejected_global_limit:
            text = "Загальна черга бота заповнена. Спробуйте трохи пізніше."
        else:
            text = "Ваша черга заповнена. Дочекайтеся завершення частини завантажень."
        await _answer_callback(callback, text, show_alert=True)
        return

    label = "аудіо" if media_type is MediaType.AUDIO else "відео"
    await _answer_callback(callback, f"{label.capitalize()} додано в чергу.")


def create_callbacks_router(
    *,
    bot: Bot,
    config: Config,
    database: Database,
    manager: DownloadManager,
    security: SecurityService,
) -> Router:
    router = Router(name="callbacks")

    @router.callback_query(AudioCallbackData.filter())
    async def audio_callback(
        callback: CallbackQuery,
        callback_data: AudioCallbackData,
    ) -> None:
        try:
            await _enqueue_single(
                callback=callback,
                manager=manager,
                security=security,
                video_id=callback_data.video_id,
                media_type=MediaType.AUDIO,
                owner_user_id=callback_data.user_id,
            )
        except Exception:
            logger.exception(
                "Помилка AudioCallbackData user_id=%s video_id=%s",
                callback.from_user.id,
                callback_data.video_id,
            )
            await _answer_callback(
                callback,
                "Не вдалося додати аудіо в чергу.",
                show_alert=True,
            )

    @router.callback_query(VideoCallbackData.filter())
    async def video_callback(
        callback: CallbackQuery,
        callback_data: VideoCallbackData,
    ) -> None:
        try:
            await _enqueue_single(
                callback=callback,
                manager=manager,
                security=security,
                video_id=callback_data.video_id,
                media_type=MediaType.VIDEO,
                owner_user_id=callback_data.user_id,
            )
        except Exception:
            logger.exception(
                "Помилка VideoCallbackData user_id=%s video_id=%s",
                callback.from_user.id,
                callback_data.video_id,
            )
            await _answer_callback(
                callback,
                "Не вдалося додати відео в чергу.",
                show_alert=True,
            )

    @router.callback_query(PaginationCallbackData.filter())
    async def pagination_callback(
        callback: CallbackQuery,
        callback_data: PaginationCallbackData,
    ) -> None:
        try:
            if not _owned_by_user(callback, callback_data.user_id):
                await _answer_callback(
                    callback,
                    "Ця пагінація належить іншому користувачу.",
                    show_alert=True,
                )
                return

            message = _callback_message(callback)
            if message is None:
                await _answer_callback(
                    callback,
                    "Повідомлення пагінації недоступне.",
                    show_alert=True,
                )
                return

            record = await database.get_search_by_message(
                callback_data.user_id,
                message.message_id,
            )
            if record is None:
                await _answer_callback(
                    callback,
                    "Ці результати вже застаріли. Виконайте пошук ще раз.",
                    show_alert=True,
                )
                return

            total_pages = max(
                1,
                (len(record.results) + config.RESULTS_PAGE_SIZE - 1)
                // config.RESULTS_PAGE_SIZE,
            )
            action = callback_data.action

            if action == "info":
                await _answer_callback(
                    callback,
                    f"Сторінка {record.page + 1} з {total_pages}",
                )
                return
            if action == "first":
                page = 0
            elif action == "prev":
                page = max(0, record.page - 1)
            elif action == "next":
                page = min(total_pages - 1, record.page + 1)
            elif action == "last":
                page = total_pages - 1
            else:
                await _answer_callback(
                    callback,
                    "Невідома дія пагінації.",
                    show_alert=True,
                )
                return

            await database.update_search_page(record.id, page)
            await render_results_page(
                bot=bot,
                config=config,
                database=database,
                message=message,
                search_id=record.id,
            )
            await _answer_callback(callback)

        except Exception:
            logger.exception(
                "Помилка PaginationCallbackData user_id=%s action=%s",
                callback.from_user.id,
                callback_data.action,
            )
            await _answer_callback(
                callback,
                "Не вдалося переключити сторінку.",
                show_alert=True,
            )

    @router.callback_query(DownloadAllCallbackData.filter())
    async def download_all_callback(
        callback: CallbackQuery,
        callback_data: DownloadAllCallbackData,
    ) -> None:
        try:
            if not _owned_by_user(callback, callback_data.user_id):
                await _answer_callback(
                    callback,
                    "Ця кнопка належить іншому користувачу.",
                    show_alert=True,
                )
                return

            message = _callback_message(callback)
            if message is None:
                await _answer_callback(
                    callback,
                    "Не вдалося визначити повідомлення з результатами.",
                    show_alert=True,
                )
                return

            try:
                media_type = MediaType(callback_data.media_type)
            except ValueError:
                await _answer_callback(
                    callback,
                    "Невідомий тип медіа.",
                    show_alert=True,
                )
                return

            record = await database.get_search_by_message(
                callback_data.user_id,
                message.message_id,
            )
            if record is None:
                await _answer_callback(
                    callback,
                    "Ці результати вже застаріли. Виконайте пошук ще раз.",
                    show_alert=True,
                )
                return

            start = record.page * config.RESULTS_PAGE_SIZE
            end = min(start + config.RESULTS_PAGE_SIZE, len(record.results))
            entries = record.results[start:end]
            tasks = [
                DownloadTask(
                    user_id=callback_data.user_id,
                    video_id=str(entry.get("id")),
                    media_type=media_type,
                    target_chat_id=message.chat.id,
                )
                for entry in entries
                if entry.get("id")
            ]

            if tasks:
                security_decision = await security.check_action(
                    callback_data.user_id,
                    "download",
                    units=len(tasks),
                )
                if not security_decision.allowed:
                    if security_decision.auto_banned:
                        await manager.cancel_user(callback_data.user_id)
                    if await security.should_notify(
                        callback_data.user_id,
                        "action:download-all",
                        cooldown=10,
                    ):
                        await _answer_callback(
                            callback,
                            security.format_rate_denial(security_decision),
                            show_alert=True,
                        )
                    else:
                        await _answer_callback(callback)
                    return

            enqueue_result = await manager.enqueue_many_detailed(tasks)
            added = enqueue_result.added
            if added == 0:
                if enqueue_result.duplicate == len(tasks) and tasks:
                    text = "Усі файли з цієї сторінки вже є у вашій черзі."
                elif enqueue_result.rejected_global_limit:
                    text = "Загальна черга бота заповнена. Спробуйте трохи пізніше."
                else:
                    text = "Ваша черга заповнена. Дочекайтеся завершення частини завантажень."
                await _answer_callback(callback, text, show_alert=True)
                return

            media_label = "аудіо" if media_type is MediaType.AUDIO else "відео"
            details: list[str] = []
            if enqueue_result.duplicate:
                details.append(f"дублікатів пропущено: {enqueue_result.duplicate}")
            if enqueue_result.rejected_user_limit:
                details.append(f"ліміт користувача: {enqueue_result.rejected_user_limit}")
            if enqueue_result.rejected_global_limit:
                details.append(f"глобальний ліміт: {enqueue_result.rejected_global_limit}")

            text = f"Додано в чергу: {added} {media_label}."
            if details:
                text += " " + "; ".join(details) + "."
            await _answer_callback(callback, text, show_alert=bool(details))

        except Exception:
            logger.exception(
                "Помилка DownloadAllCallbackData user_id=%s media_type=%s",
                callback.from_user.id,
                callback_data.media_type,
            )
            await _answer_callback(
                callback,
                "Не вдалося додати сторінку в чергу.",
                show_alert=True,
            )

    return router
