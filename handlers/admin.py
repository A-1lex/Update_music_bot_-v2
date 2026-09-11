from __future__ import annotations

import asyncio
import csv
import logging
import os
import re
import secrets
import tempfile

import psutil
from dataclasses import dataclass
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router, types
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import CallbackQuery, FSInputFile, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import Config
from database import Database
from keyboards.admin import (
    BulkDeleteCallbackData,
    DeleteFileCallbackData,
    TablePaginationCallbackData,
    TableSelectCallbackData,
    build_table_select_keyboard,
)
from utils.filenames import is_path_inside, list_media_files
from utils.supervision import supervision
from services.storage import StorageGuard
from workers.download_worker import DownloadManager


logger = logging.getLogger(__name__)
DELETE_STATE_NAME = "files"
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


@dataclass(slots=True)
class DeleteState:
    token: str
    files: list[Path]


def _is_admin(user_id: int, config: Config) -> bool:
    return config.ADMIN_ID > 0 and user_id == config.ADMIN_ID


async def _deny_if_not_admin(message: Message, config: Config) -> bool:
    user_id = message.from_user.id if message.from_user else 0
    if not _is_admin(user_id, config):
        await message.answer("⛔ Ця команда доступна лише адміністратору.")
        return True
    if config.ADMIN_PRIVATE_CHAT_ONLY and message.chat.type != ChatType.PRIVATE:
        logger.warning(
            "Адмін-команду поза private chat заблоковано user_id=%s chat_id=%s",
            user_id,
            message.chat.id,
        )
        return True
    return False


def _admin_callback_allowed(callback: CallbackQuery, config: Config) -> bool:
    if not _is_admin(callback.from_user.id, config):
        return False
    if not config.ADMIN_PRIVATE_CHAT_ONLY:
        return True
    message = callback.message if isinstance(callback.message, Message) else None
    return message is not None and message.chat.type == ChatType.PRIVATE


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
            "Не вдалося відповісти на адміністративний callback id=%s",
            callback.id,
            exc_info=True,
        )


def _build_delete_keyboard(
    *,
    files: list[Path],
    token: str,
    page: int,
    page_size: int,
    user_id: int,
) -> types.InlineKeyboardMarkup:
    total_pages = max(1, (len(files) + page_size - 1) // page_size)
    start = page * page_size
    end = min(start + page_size, len(files))
    builder = InlineKeyboardBuilder()

    for index in range(start, end):
        path = files[index]
        size_mb = path.stat().st_size / (1024 * 1024) if path.is_file() else 0
        builder.row(
            types.InlineKeyboardButton(
                text=f"🗑 {path.name[:50]} ({size_mb:.1f} МБ)",
                callback_data=DeleteFileCallbackData(
                    token=token,
                    index=index,
                ).pack(),
            )
        )

    builder.row(
        types.InlineKeyboardButton(
            text="🧹 Видалити всі MP3",
            callback_data=BulkDeleteCallbackData(
                action="ask",
                media_type="audio",
                token=token,
            ).pack(),
        ),
        types.InlineKeyboardButton(
            text="🧹 Видалити всі MP4",
            callback_data=BulkDeleteCallbackData(
                action="ask",
                media_type="video",
                token=token,
            ).pack(),
        ),
    )

    builder.row(
        types.InlineKeyboardButton(
            text="🔄 Оновити список",
            callback_data=TablePaginationCallbackData(
                action="refresh", user_id=user_id, table_name=DELETE_STATE_NAME
            ).pack(),
        )
    )

    builder.row(
        types.InlineKeyboardButton(
            text="⏮",
            callback_data=TablePaginationCallbackData(
                action="first", user_id=user_id, table_name=DELETE_STATE_NAME
            ).pack(),
        ),
        types.InlineKeyboardButton(
            text="◀️",
            callback_data=TablePaginationCallbackData(
                action="prev", user_id=user_id, table_name=DELETE_STATE_NAME
            ).pack(),
        ),
        types.InlineKeyboardButton(
            text=f"📄 {page + 1}/{total_pages}",
            callback_data=TablePaginationCallbackData(
                action="info", user_id=user_id, table_name=DELETE_STATE_NAME
            ).pack(),
        ),
        types.InlineKeyboardButton(
            text="▶️",
            callback_data=TablePaginationCallbackData(
                action="next", user_id=user_id, table_name=DELETE_STATE_NAME
            ).pack(),
        ),
        types.InlineKeyboardButton(
            text="⏭",
            callback_data=TablePaginationCallbackData(
                action="last", user_id=user_id, table_name=DELETE_STATE_NAME
            ).pack(),
        ),
    )
    return builder.as_markup()


async def _show_delete_page(
    *,
    bot: Bot,
    config: Config,
    database: Database,
    message: Message,
    states: dict[int, DeleteState],
    user_id: int,
    page: int,
    force_new: bool = False,
) -> None:
    files = await asyncio.to_thread(
        list_media_files,
        config.MUSIC_DIR,
        config.VIDEO_DIR,
    )
    page_size = config.TABLE_PAGE_SIZE
    total_pages = max(1, (len(files) + page_size - 1) // page_size)
    page = max(0, min(page, total_pages - 1))
    state = DeleteState(token=secrets.token_hex(5), files=files)
    states[user_id] = state

    text = (
        f"🗑 <b>Локальні MP3/MP4 файли</b>\n"
        f"📄 Сторінка <b>{page + 1}/{total_pages}</b>\n"
        f"📦 Всього файлів: <b>{len(files)}</b>"
    )
    markup = _build_delete_keyboard(
        files=files,
        token=state.token,
        page=page,
        page_size=page_size,
        user_id=user_id,
    )

    logger.info(
        "Сканування локальних медіафайлів: music=%s, video=%s, знайдено=%s, force_new=%s",
        config.MUSIC_DIR,
        config.VIDEO_DIR,
        len(files),
        force_new,
    )

    pagination = None
    if not force_new:
        pagination = await database.load_pagination(user_id, DELETE_STATE_NAME)

    if pagination and pagination.message_id:
        try:
            await bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=pagination.message_id,
                text=text,
                reply_markup=markup,
                parse_mode=ParseMode.HTML,
            )
            await database.save_pagination(
                user_id=user_id,
                table_name=DELETE_STATE_NAME,
                page=page,
                message_id=pagination.message_id,
            )
            return
        except TelegramBadRequest as exc:
            lowered = str(exc).lower()
            if "message is not modified" in lowered:
                await database.save_pagination(
                    user_id=user_id,
                    table_name=DELETE_STATE_NAME,
                    page=page,
                    message_id=pagination.message_id,
                )
                return
            if not any(
                marker in lowered
                for marker in (
                    "message to edit not found",
                    "message can't be edited",
                    "message identifier is not specified",
                )
            ):
                raise

    sent = await message.answer(
        text,
        reply_markup=markup,
        parse_mode=ParseMode.HTML,
    )
    await database.save_pagination(
        user_id=user_id,
        table_name=DELETE_STATE_NAME,
        page=page,
        message_id=sent.message_id,
    )


def _csv_safe_cell(value: object) -> object:
    if not isinstance(value, str):
        return value
    # Excel/LibreOffice можуть виконувати значення, що починаються як формули.
    # Апостроф змушує трактувати такі дані як звичайний текст.
    if value.startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + value
    return value


def _format_size(bytes_value: int) -> str:
    mib = 1024**2
    gib = 1024**3
    if bytes_value >= gib:
        return f"{bytes_value / gib:.2f} ГБ"
    return f"{bytes_value / mib:.1f} МБ"


async def _media_identity_for_target(
    *,
    target: Path,
    database: Database,
) -> tuple[str, str] | None:
    identity = await database.get_media_identity_by_file_path(str(target))
    if identity is not None:
        return identity

    suffix = target.suffix.lower()
    if _VIDEO_ID_RE.fullmatch(target.stem) and suffix in {".mp3", ".mp4"}:
        return (
            target.stem,
            "audio" if suffix == ".mp3" else "video",
        )
    return None


async def _delete_local_media_file(
    *,
    target: Path,
    config: Config,
    database: Database,
    manager: DownloadManager,
) -> tuple[bool, str, int]:
    target = target.resolve()
    if not is_path_inside(target, [config.MUSIC_DIR, config.VIDEO_DIR]):
        logger.warning("Заблоковано path traversal під час delete: %s", target)
        return False, "unsafe", 0

    suffix = target.suffix.lower()
    if suffix not in {".mp3", ".mp4"}:
        return False, "unsupported", 0

    identity = await _media_identity_for_target(target=target, database=database)

    async def delete_inside_guard() -> tuple[bool, str, int]:
        if identity is not None:
            video_id, media_type = identity
            if await manager.is_media_pending_or_active(video_id, media_type):
                return False, "active", 0

        try:
            size = target.stat().st_size
        except FileNotFoundError:
            await database.clear_file_path_by_path(str(target))
            return False, "missing", 0

        await asyncio.to_thread(target.unlink)
        await database.clear_file_path_by_path(str(target))
        return True, "deleted", size

    if identity is None:
        return await delete_inside_guard()

    video_id, media_type = identity
    # Спільний media-lock із DownloadService + повторний active-check усередині
    # закриває race між перевіркою та unlink().
    async with manager.media_maintenance_guard(video_id, media_type):
        return await delete_inside_guard()


async def _write_csv(
    path: Path,
    headers: list[str],
    rows: list[tuple[object, ...]] | list[object],
) -> None:
    def _write() -> None:
        with path.open("w", newline="", encoding="utf-8-sig") as file:
            writer = csv.writer(file)
            writer.writerow(headers)
            writer.writerows(
                [_csv_safe_cell(value) for value in row]
                for row in rows
            )

    await asyncio.to_thread(_write)


def create_admin_router(
    *,
    bot: Bot,
    dispatcher: Dispatcher,
    config: Config,
    database: Database,
    manager: DownloadManager,
    storage: StorageGuard,
) -> Router:
    router = Router(name="admin")
    delete_states: dict[int, DeleteState] = {}

    async def show_tables(message: Message) -> None:
        if await _deny_if_not_admin(message, config):
            return
        await message.answer(
            "📊 Оберіть таблицю для експорту в CSV:",
            reply_markup=build_table_select_keyboard(),
        )

    async def show_status(message: Message) -> None:
        if await _deny_if_not_admin(message, config):
            return
        snapshot = await storage.snapshot("video")
        memory = psutil.virtual_memory()
        global_jobs = await manager.global_job_count()
        active_jobs = await manager.active_job_count()
        queued_jobs = await manager.queued_job_count()
        user_jobs = (
            await manager.user_job_count(message.from_user.id)
            if message.from_user is not None
            else 0
        )
        gib = 1024**3
        mib = 1024**2
        await message.answer(
            "🩺 <b>Статус бота</b>\n"
            f"📥 Завдання: <b>{global_jobs}/{config.EFFECTIVE_MAX_GLOBAL_QUEUE_SIZE}</b>\n"
            f"▶️ Активні: <b>{active_jobs}/{config.MAX_CONCURRENT_DOWNLOADS}</b>\n"
            f"⏳ Очікують: <b>{queued_jobs}</b>\n"
            f"👤 Ваші завдання: <b>{user_jobs}/{config.EFFECTIVE_MAX_QUEUE_SIZE}</b>\n"
            f"🧠 RAM: <b>{memory.total / gib:.1f} ГБ</b>, доступно <b>{memory.available / gib:.1f} ГБ</b>\n"
            f"⚙️ Одночасні завантаження: <b>{config.MAX_CONCURRENT_DOWNLOADS}</b>\n"
            f"🎞 FFmpeg одночасно: <b>{config.MAX_CONCURRENT_FFMPEG}</b>\n\n"
            f"💽 Диск: <b>{snapshot.target_total_bytes / gib:.1f} ГБ</b>\n"
            f"🆓 Вільно на диску: <b>{snapshot.target_free_bytes / gib:.1f} ГБ</b>\n"
            f"🔒 Резерв ОС: <b>{config.SYSTEM_FREE_RESERVE_GB} ГБ</b>\n"
            f"🛡 Безпечно вільно на диску: <b>{snapshot.safe_disk_free_bytes / gib:.2f} ГБ</b>\n\n"
            f"💾 Квота бота: <b>{config.BOT_STORAGE_QUOTA_GB} ГБ</b>\n"
            f"📦 Використано ботом: <b>{snapshot.bot_used_bytes / gib:.2f} ГБ</b>\n"
            f"🧾 Зарезервовано активними задачами: <b>{snapshot.reserved_bytes / mib:.0f} МБ</b>\n"
            f"🟢 Реально доступно боту: <b>{snapshot.available_for_bot_bytes / gib:.2f} ГБ</b>",
            parse_mode=ParseMode.HTML,
        )

    async def show_delete(message: Message) -> None:
        if await _deny_if_not_admin(message, config):
            return
        if message.from_user is None:
            return
        await _show_delete_page(
            bot=bot,
            config=config,
            database=database,
            message=message,
            states=delete_states,
            user_id=message.from_user.id,
            page=0,
            force_new=True,
        )

    async def restart(message: Message) -> None:
        if await _deny_if_not_admin(message, config):
            return
        try:
            accepted = supervision.request_restart(message.chat.id)
        except Exception:
            logger.exception("Не вдалося передати запит наглядачу")
            await message.answer("❌ Не вдалося передати запит наглядачу.")
            return
        if accepted:
            await message.answer("🔄 Перезапускаю. Повідомлю, коли бот буде готовий.")
        else:
            await message.answer("⏳ Перезапуск уже виконується.")

    @router.message(Command("show_tables"))
    async def show_tables_command(message: Message) -> None:
        try:
            await show_tables(message)
        except Exception:
            logger.exception("Помилка /show_tables")
            await message.answer("❌ Не вдалося показати таблиці.")

    @router.message(F.text == "Показати таблиці")
    async def show_tables_button(message: Message) -> None:
        try:
            await show_tables(message)
        except Exception:
            logger.exception("Помилка кнопки 'Показати таблиці'")
            await message.answer("❌ Не вдалося показати таблиці.")

    @router.message(Command("status"))
    async def status_command(message: Message) -> None:
        try:
            await show_status(message)
        except Exception:
            logger.exception("Помилка /status")
            await message.answer("❌ Не вдалося отримати статус бота.")

    @router.message(F.text == "Статус бота")
    async def status_button(message: Message) -> None:
        try:
            await show_status(message)
        except Exception:
            logger.exception("Помилка кнопки 'Статус бота'")
            await message.answer("❌ Не вдалося отримати статус бота.")

    @router.message(Command("delete"))
    async def delete_command(message: Message) -> None:
        try:
            await show_delete(message)
        except Exception:
            logger.exception("Помилка /delete")
            await message.answer("❌ Не вдалося отримати список файлів.")

    @router.message(F.text == "Видалити файл")
    async def delete_button(message: Message) -> None:
        try:
            await show_delete(message)
        except Exception:
            logger.exception("Помилка кнопки 'Видалити файл'")
            await message.answer("❌ Не вдалося отримати список файлів.")

    @router.message(Command("restart"))
    async def restart_command(message: Message) -> None:
        await restart(message)

    @router.message(F.text == "Перезапустити бота")
    async def restart_button(message: Message) -> None:
        await restart(message)

    @router.callback_query(TableSelectCallbackData.filter())
    async def table_select_callback(
        callback: CallbackQuery,
        callback_data: TableSelectCallbackData,
    ) -> None:
        temp_path: Path | None = None
        try:
            if not _admin_callback_allowed(callback, config):
                await _answer_callback(
                    callback,
                    "Недостатньо прав.",
                    show_alert=True,
                )
                return

            table_name = callback_data.table_name
            if table_name not in database.ADMIN_TABLES:
                await _answer_callback(
                    callback,
                    "Таблиця не дозволена.",
                    show_alert=True,
                )
                return

            headers, rows = await database.export_admin_table(table_name)
            fd, raw_path = tempfile.mkstemp(
                prefix=f"{table_name}_",
                suffix=".csv",
                dir=config.TEMP_DIR,
            )
            os.close(fd)
            temp_path = Path(raw_path)
            await _write_csv(temp_path, headers, rows)

            message = callback.message if isinstance(callback.message, Message) else None
            if message is None:
                await _answer_callback(
                    callback,
                    "Не вдалося визначити чат.",
                    show_alert=True,
                )
                return

            await message.answer_document(
                FSInputFile(temp_path, filename=f"{table_name}.csv"),
                caption=f"📄 Експорт таблиці {table_name}",
            )
            await _answer_callback(callback, "CSV сформовано.")
            logger.info(
                "Експортовано таблицю %s: %s рядків",
                table_name,
                len(rows),
            )

        except Exception:
            logger.exception(
                "Помилка TableSelectCallbackData table=%s",
                callback_data.table_name,
            )
            await _answer_callback(
                callback,
                "Не вдалося експортувати таблицю.",
                show_alert=True,
            )
        finally:
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    logger.warning(
                        "Не вдалося видалити тимчасовий CSV %s",
                        temp_path,
                        exc_info=True,
                    )

    @router.callback_query(TablePaginationCallbackData.filter())
    async def table_pagination_callback(
        callback: CallbackQuery,
        callback_data: TablePaginationCallbackData,
    ) -> None:
        try:
            if (
                not _admin_callback_allowed(callback, config)
                or callback_data.user_id != callback.from_user.id
            ):
                await _answer_callback(
                    callback,
                    "Недостатньо прав.",
                    show_alert=True,
                )
                return

            if callback_data.table_name != DELETE_STATE_NAME:
                await _answer_callback(
                    callback,
                    "Невідомий тип пагінації.",
                    show_alert=True,
                )
                return

            message = callback.message if isinstance(callback.message, Message) else None
            if message is None:
                await _answer_callback(
                    callback,
                    "Повідомлення недоступне.",
                    show_alert=True,
                )
                return

            pagination = await database.load_pagination(
                callback.from_user.id,
                DELETE_STATE_NAME,
            )
            if (
                pagination is None
                or pagination.message_id is None
                or pagination.message_id != message.message_id
            ):
                await _answer_callback(
                    callback,
                    "Ця панель застаріла. Запустіть /delete ще раз.",
                    show_alert=True,
                )
                return

            files = await asyncio.to_thread(
                list_media_files,
                config.MUSIC_DIR,
                config.VIDEO_DIR,
            )
            total_pages = max(
                1,
                (len(files) + config.TABLE_PAGE_SIZE - 1) // config.TABLE_PAGE_SIZE,
            )
            current = max(0, min(pagination.page, total_pages - 1))

            if callback_data.action == "info":
                await _answer_callback(
                    callback,
                    f"Сторінка {current + 1} з {total_pages}",
                )
                return
            if callback_data.action == "refresh":
                page = current
            elif callback_data.action == "first":
                page = 0
            elif callback_data.action == "prev":
                page = max(0, current - 1)
            elif callback_data.action == "next":
                page = min(total_pages - 1, current + 1)
            elif callback_data.action == "last":
                page = total_pages - 1
            else:
                await _answer_callback(
                    callback,
                    "Невідома дія.",
                    show_alert=True,
                )
                return

            await _show_delete_page(
                bot=bot,
                config=config,
                database=database,
                message=message,
                states=delete_states,
                user_id=callback.from_user.id,
                page=page,
            )
            await _answer_callback(callback)

        except Exception:
            logger.exception(
                "Помилка TablePaginationCallbackData action=%s",
                callback_data.action,
            )
            await _answer_callback(
                callback,
                "Не вдалося переключити сторінку.",
                show_alert=True,
            )

    @router.callback_query(BulkDeleteCallbackData.filter())
    async def bulk_delete_callback(
        callback: CallbackQuery,
        callback_data: BulkDeleteCallbackData,
    ) -> None:
        try:
            user_id = callback.from_user.id
            if not _admin_callback_allowed(callback, config):
                await _answer_callback(
                    callback,
                    "Недостатньо прав.",
                    show_alert=True,
                )
                return

            if callback_data.media_type not in {"audio", "video"}:
                await _answer_callback(
                    callback,
                    "Невідомий тип файлів.",
                    show_alert=True,
                )
                return

            state = delete_states.get(user_id)
            if state is None or state.token != callback_data.token:
                await _answer_callback(
                    callback,
                    "Панель застаріла. Запустіть /delete ще раз.",
                    show_alert=True,
                )
                return

            message = callback.message if isinstance(callback.message, Message) else None
            if message is None:
                await _answer_callback(
                    callback,
                    "Повідомлення недоступне.",
                    show_alert=True,
                )
                return

            suffix = ".mp3" if callback_data.media_type == "audio" else ".mp4"
            label = "MP3" if callback_data.media_type == "audio" else "MP4"

            if callback_data.action == "cancel":
                pagination = await database.load_pagination(user_id, DELETE_STATE_NAME)
                page = pagination.page if pagination else 0
                await _show_delete_page(
                    bot=bot,
                    config=config,
                    database=database,
                    message=message,
                    states=delete_states,
                    user_id=user_id,
                    page=page,
                )
                await _answer_callback(callback, "Скасовано.")
                return

            files = await asyncio.to_thread(
                list_media_files,
                config.MUSIC_DIR,
                config.VIDEO_DIR,
            )
            targets = [path for path in files if path.suffix.lower() == suffix]
            total_bytes = sum(
                path.stat().st_size for path in targets if path.is_file()
            )

            if callback_data.action == "ask":
                if not targets:
                    await _answer_callback(
                        callback,
                        f"Локальних {label} файлів немає.",
                        show_alert=True,
                    )
                    return

                confirm_builder = InlineKeyboardBuilder()
                confirm_builder.row(
                    types.InlineKeyboardButton(
                        text=f"✅ Так, видалити всі {label}",
                        callback_data=BulkDeleteCallbackData(
                            action="confirm",
                            media_type=callback_data.media_type,
                            token=state.token,
                        ).pack(),
                    )
                )
                confirm_builder.row(
                    types.InlineKeyboardButton(
                        text="↩️ Скасувати",
                        callback_data=BulkDeleteCallbackData(
                            action="cancel",
                            media_type=callback_data.media_type,
                            token=state.token,
                        ).pack(),
                    )
                )
                await message.edit_text(
                    "⚠️ <b>Підтвердження масового видалення</b>\n\n"
                    f"Тип: <b>{label}</b>\n"
                    f"Файлів: <b>{len(targets)}</b>\n"
                    f"Займають: <b>{_format_size(total_bytes)}</b>\n\n"
                    "Telegram file_id cache у БД залишиться. "
                    "Буде видалено лише локальні файли.",
                    reply_markup=confirm_builder.as_markup(),
                    parse_mode=ParseMode.HTML,
                )
                await _answer_callback(callback)
                return

            if callback_data.action != "confirm":
                await _answer_callback(
                    callback,
                    "Невідома дія.",
                    show_alert=True,
                )
                return

            # Масове видалення навмисно не запускаємо, поки є будь-які
            # активні/очікуючі завдання. Це усуває race між перевіркою
            # media identity та появою нового завдання під час циклу delete.
            active_jobs = await manager.global_job_count()
            if active_jobs:
                await _answer_callback(
                    callback,
                    f"Зараз є активні/очікуючі завдання: {active_jobs}. "
                    "Масове видалення доступне після їх завершення або /cancel.",
                    show_alert=True,
                )
                return

            deleted_count = 0
            skipped_count = 0
            error_count = 0
            freed_bytes = 0

            for target in targets:
                try:
                    deleted, reason, size = await _delete_local_media_file(
                        target=target,
                        config=config,
                        database=database,
                        manager=manager,
                    )
                    if deleted:
                        deleted_count += 1
                        freed_bytes += size
                    elif reason in {"active", "missing"}:
                        skipped_count += 1
                    else:
                        error_count += 1
                except Exception:
                    error_count += 1
                    logger.exception(
                        "Помилка масового видалення локального файла %s",
                        target,
                    )

            logger.info(
                "Масове видалення %s: deleted=%s skipped=%s errors=%s freed=%s bytes admin_id=%s",
                label,
                deleted_count,
                skipped_count,
                error_count,
                freed_bytes,
                user_id,
            )

            await _show_delete_page(
                bot=bot,
                config=config,
                database=database,
                message=message,
                states=delete_states,
                user_id=user_id,
                page=0,
            )
            result_text = (
                f"Видалено {label}: {deleted_count}; "
                f"звільнено {_format_size(freed_bytes)}"
            )
            if skipped_count:
                result_text += f"; пропущено {skipped_count}"
            if error_count:
                result_text += f"; помилок {error_count}"
            await _answer_callback(callback, result_text, show_alert=True)

        except Exception:
            logger.exception(
                "Помилка BulkDeleteCallbackData action=%s media_type=%s",
                callback_data.action,
                callback_data.media_type,
            )
            await _answer_callback(
                callback,
                "Не вдалося виконати масове видалення.",
                show_alert=True,
            )

    @router.callback_query(DeleteFileCallbackData.filter())
    async def delete_file_callback(
        callback: CallbackQuery,
        callback_data: DeleteFileCallbackData,
    ) -> None:
        try:
            user_id = callback.from_user.id
            if not _admin_callback_allowed(callback, config):
                await _answer_callback(
                    callback,
                    "Недостатньо прав.",
                    show_alert=True,
                )
                return

            state = delete_states.get(user_id)
            if state is None or state.token != callback_data.token:
                await _answer_callback(
                    callback,
                    "Список файлів застарів. Запустіть /delete ще раз.",
                    show_alert=True,
                )
                return

            if callback_data.index < 0 or callback_data.index >= len(state.files):
                await _answer_callback(
                    callback,
                    "Файл не знайдено у поточному списку.",
                    show_alert=True,
                )
                return

            target = state.files[callback_data.index].resolve()
            if not is_path_inside(target, [config.MUSIC_DIR, config.VIDEO_DIR]):
                logger.warning("Заблоковано path traversal під час delete: %s", target)
                await _answer_callback(
                    callback,
                    "Небезпечний шлях заблоковано.",
                    show_alert=True,
                )
                return

            suffix = target.suffix.lower()
            if suffix not in {".mp3", ".mp4"}:
                await _answer_callback(
                    callback,
                    "Дозволено видаляти лише MP3/MP4.",
                    show_alert=True,
                )
                return

            deleted, reason, _ = await _delete_local_media_file(
                target=target,
                config=config,
                database=database,
                manager=manager,
            )
            if not deleted:
                if reason == "active":
                    await _answer_callback(
                        callback,
                        "Файл зараз використовується активним завданням. Спробуйте після завершення.",
                        show_alert=True,
                    )
                    return
                if reason == "unsafe":
                    await _answer_callback(
                        callback,
                        "Небезпечний шлях заблоковано.",
                        show_alert=True,
                    )
                    return
                if reason == "unsupported":
                    await _answer_callback(
                        callback,
                        "Дозволено видаляти лише MP3/MP4.",
                        show_alert=True,
                    )
                    return

            if deleted:
                logger.info("Адміністратор видалив локальний файл %s", target)

            message = callback.message if isinstance(callback.message, Message) else None
            if message is not None:
                pagination = await database.load_pagination(user_id, DELETE_STATE_NAME)
                page = pagination.page if pagination else 0
                await _show_delete_page(
                    bot=bot,
                    config=config,
                    database=database,
                    message=message,
                    states=delete_states,
                    user_id=user_id,
                    page=page,
                )
            await _answer_callback(callback, "Файл видалено.")

        except FileNotFoundError:
            await _answer_callback(
                callback,
                "Файл уже видалено.",
                show_alert=True,
            )
        except Exception:
            logger.exception("Помилка DeleteFileCallbackData")
            await _answer_callback(
                callback,
                "Не вдалося видалити файл.",
                show_alert=True,
            )

    return router
