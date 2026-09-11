from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from aiogram import Bot, F, Router
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import Message

from config import Config
from database import Database, SearchRecord
from keyboards.search import build_search_keyboard
from services.youtube import YouTubeService
from security.service import SecurityService
from workers.download_worker import DownloadManager
from utils.errors import get_user_friendly_error
from utils.html import escape_html, truncate_text


logger = logging.getLogger(__name__)


async def render_results_page(
    *,
    bot: Bot,
    config: Config,
    database: Database,
    message: Message,
    search_id: int,
) -> None:
    record = await database.get_search_by_id(search_id)
    if record is None:
        await message.answer("❌ Немає даних для відображення.")
        return

    page_size = config.RESULTS_PAGE_SIZE
    total_items = len(record.results)
    total_pages = max(1, (total_items + page_size - 1) // page_size)
    page = max(0, min(record.page, total_pages - 1))
    if page != record.page:
        await database.update_search_page(record.id, page)
        record.page = page

    start = page * page_size
    end = min(start + page_size, total_items)
    entries = record.results[start:end]

    message_text = _build_page_text(record, page, total_pages, total_items)
    reply_markup = build_search_keyboard(
        entries=entries,
        page=page,
        total_pages=total_pages,
        start_index=start,
        user_id=record.user_id,
    )

    if record.message_id is not None:
        try:
            await bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=record.message_id,
                text=message_text,
                reply_markup=reply_markup,
                parse_mode=ParseMode.HTML,
            )
            return
        except TelegramBadRequest as exc:
            error_text = str(exc).lower()
            if "message is not modified" in error_text:
                try:
                    await bot.edit_message_reply_markup(
                        chat_id=message.chat.id,
                        message_id=record.message_id,
                        reply_markup=reply_markup,
                    )
                except TelegramBadRequest as markup_exc:
                    if "message is not modified" not in str(markup_exc).lower():
                        raise
                return

            editable_errors = (
                "message to edit not found",
                "message can't be edited",
                "message identifier is not specified",
            )
            if not any(marker in error_text for marker in editable_errors):
                raise

    sent = await bot.send_message(
        chat_id=message.chat.id,
        text=message_text,
        reply_markup=reply_markup,
        parse_mode=ParseMode.HTML,
    )
    await database.bind_search_message(record.id, sent.message_id)


def _build_page_text(
    record: SearchRecord,
    page: int,
    total_pages: int,
    total_items: int,
) -> str:
    if record.playlist_url:
        safe_title = truncate_text(record.playlist_title or "Добірка YouTube", 120)
        header = (
            "🎵 <b>Добірка YouTube</b>\n\n"
            f'📌 <a href="{escape_html(record.playlist_url)}">'
            f"{escape_html(safe_title)}</a>\n"
        )
    else:
        header = f"<b>{escape_html(truncate_text(record.query, 150))}</b>\n"

    return (
        f"{header}"
        f"📄 Сторінка <b>{page + 1}/{total_pages}</b>\n"
        f"🎵 Всього: <b>{total_items}</b>"
    )


def create_search_router(
    *,
    bot: Bot,
    config: Config,
    database: Database,
    youtube: YouTubeService,
    security: SecurityService,
    manager: DownloadManager,
) -> Router:
    router = Router(name="search")
    last_search_at: dict[int, float] = {}
    rate_lock = asyncio.Lock()

    async def allow_expensive(message: Message, user_id: int, action: str) -> bool:
        decision = await security.check_action(user_id, action)
        if decision.allowed:
            return True
        if decision.auto_banned:
            await manager.cancel_user(user_id)
        if await security.should_notify(user_id, f"action:{action}", cooldown=10):
            await message.answer(security.format_rate_denial(decision))
        return False

    @router.message(F.text)
    async def text_search_handler(message: Message) -> None:
        if message.from_user is None:
            return

        user_id = message.from_user.id
        query = (message.text or "").strip()
        if not query:
            await message.answer("❌ Введіть пошуковий запит або YouTube-посилання.")
            return

        if query.startswith("/"):
            await message.answer("❌ Невідома команда.")
            return

        if len(query) > config.MAX_QUERY_LENGTH:
            await message.answer(
                f"❌ Запит занадто довгий. Максимум: {config.MAX_QUERY_LENGTH} символів."
            )
            return

        if config.SEARCH_COOLDOWN_SECONDS > 0:
            now = time.monotonic()
            remaining = 0.0
            async with rate_lock:
                previous = last_search_at.get(user_id, 0.0)
                remaining = config.SEARCH_COOLDOWN_SECONDS - (now - previous)
                if remaining <= 0:
                    last_search_at[user_id] = now
                    if len(last_search_at) > 10_000:
                        cutoff = now - max(60, config.SEARCH_COOLDOWN_SECONDS * 10)
                        stale = [uid for uid, ts in last_search_at.items() if ts < cutoff]
                        for uid in stale:
                            last_search_at.pop(uid, None)
            if remaining > 0:
                if await security.should_notify(
                    user_id,
                    "search-cooldown",
                    cooldown=max(1, config.SEARCH_COOLDOWN_SECONDS),
                ):
                    await message.answer(
                        f"⏳ Зачекайте приблизно {max(1, int(remaining + 0.99))} сек. перед наступним пошуком."
                    )
                return

        try:
            await database.save_user(user_id, message.from_user.username)

            reference = youtube.parse_reference(query)
            if reference.is_youtube:
                if reference.is_mix and reference.video_id:
                    if not await allow_expensive(message, user_id, "search"):
                        return
                    await _handle_single_video(
                        bot=bot,
                        config=config,
                        database=database,
                        youtube=youtube,
                        message=message,
                        user_id=user_id,
                        video_id=reference.video_id,
                    )
                    return

                if reference.is_mix and not reference.video_id:
                    await message.answer(
                        "⚠️ Для YouTube Mix надішліть посилання, яке містить конкретне відео. "
                        "Так бот не розгорне Mix у величезну автоматичну добірку."
                    )
                    return

                if reference.playlist_id:
                    if not await allow_expensive(message, user_id, "playlist"):
                        return
                    await _handle_playlist(
                        bot=bot,
                        config=config,
                        database=database,
                        youtube=youtube,
                        message=message,
                        user_id=user_id,
                        playlist_id=reference.playlist_id,
                    )
                    return

                if reference.video_id:
                    if not await allow_expensive(message, user_id, "search"):
                        return
                    await _handle_single_video(
                        bot=bot,
                        config=config,
                        database=database,
                        youtube=youtube,
                        message=message,
                        user_id=user_id,
                        video_id=reference.video_id,
                    )
                    return

                await message.answer("❌ Некоректне посилання YouTube.")
                return

            if youtube.looks_like_url(query):
                await message.answer("❌ Підтримуються лише посилання YouTube.")
                return

            if not await allow_expensive(message, user_id, "search"):
                return

            await _handle_text_search(
                bot=bot,
                config=config,
                database=database,
                youtube=youtube,
                message=message,
                user_id=user_id,
                query=query,
            )

        except Exception as exc:
            logger.exception("Помилка обробки повідомлення user_id=%s", user_id)
            await message.answer(get_user_friendly_error(exc, "запит"))

    return router


async def _handle_text_search(
    *,
    bot: Bot,
    config: Config,
    database: Database,
    youtube: YouTubeService,
    message: Message,
    user_id: int,
    query: str,
) -> None:
    status = await message.answer("🔎 Шукаю на YouTube...")
    try:
        entries = await youtube.search(query)
        if not entries:
            await status.edit_text("❌ Нічого не знайдено.")
            return

        search_id = await database.create_search_results(
            user_id=user_id,
            query=query,
            results=entries,
        )
        await render_results_page(
            bot=bot,
            config=config,
            database=database,
            message=message,
            search_id=search_id,
        )
        await _safe_delete_status(status)
    except Exception as exc:
        logger.exception("Помилка YouTube-пошуку user_id=%s query=%r", user_id, query)
        await _safe_edit_status(status, get_user_friendly_error(exc, "результати пошуку"))


async def _handle_single_video(
    *,
    bot: Bot,
    config: Config,
    database: Database,
    youtube: YouTubeService,
    message: Message,
    user_id: int,
    video_id: str,
) -> None:
    status = await message.answer("⏳ Отримую інформацію про відео...")
    try:
        entry = await youtube.get_video_metadata(video_id)
        if entry is None:
            await status.edit_text("❌ Відео недоступне.")
            return

        search_id = await database.create_search_results(
            user_id=user_id,
            query="Відео YouTube",
            results=[entry],
        )
        await render_results_page(
            bot=bot,
            config=config,
            database=database,
            message=message,
            search_id=search_id,
        )
        await _safe_delete_status(status)
    except Exception as exc:
        logger.exception("Помилка метаданих video_id=%s user_id=%s", video_id, user_id)
        await _safe_edit_status(status, get_user_friendly_error(exc, "відео"))


async def _handle_playlist(
    *,
    bot: Bot,
    config: Config,
    database: Database,
    youtube: YouTubeService,
    message: Message,
    user_id: int,
    playlist_id: str,
) -> None:
    status = await message.answer(
        "⏳ Отримую список треків із плейлиста...\n"
        "Великі плейлисти можуть оброблятися довше."
    )
    try:
        playlist = await youtube.get_playlist_metadata(playlist_id)
        entries = playlist.get("entries") or []
        if not entries:
            await status.edit_text(
                "❌ Не вдалося отримати треки з плейлиста.\n\n"
                "Можливо, плейлист приватний, видалений або YouTube тимчасово обмежив доступ."
            )
            return

        search_id = await database.create_search_results(
            user_id=user_id,
            query="Добірка YouTube",
            results=entries,
            playlist_title=str(playlist.get("title") or "Добірка YouTube"),
            playlist_url=str(playlist.get("url") or youtube.playlist_url(playlist_id)),
            playlist_id=playlist_id,
        )
        await render_results_page(
            bot=bot,
            config=config,
            database=database,
            message=message,
            search_id=search_id,
        )
        await _safe_delete_status(status)
    except Exception as exc:
        logger.exception("Помилка плейлиста playlist_id=%s user_id=%s", playlist_id, user_id)
        await _safe_edit_status(status, get_user_friendly_error(exc, "плейлист"))


async def _safe_delete_status(message: Message) -> None:
    try:
        await message.delete()
    except TelegramAPIError:
        pass


async def _safe_edit_status(message: Message, text: str) -> None:
    try:
        await message.edit_text(text)
    except TelegramAPIError:
        try:
            await message.answer(text)
        except TelegramAPIError:
            logger.exception("Не вдалося показати користувачу повідомлення про помилку")
