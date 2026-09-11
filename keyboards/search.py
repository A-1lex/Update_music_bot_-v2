from __future__ import annotations

from typing import Any, Sequence

from aiogram import types
from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from utils.html import truncate_text


class AudioCallbackData(CallbackData, prefix="aud"):
    user_id: int
    video_id: str


class VideoCallbackData(CallbackData, prefix="vid"):
    user_id: int
    video_id: str


class PaginationCallbackData(CallbackData, prefix="page"):
    action: str
    user_id: int


class DownloadAllCallbackData(CallbackData, prefix="download_all"):
    user_id: int
    media_type: str


def _format_duration(value: Any) -> str:
    try:
        duration = float(value)
    except (TypeError, ValueError):
        return "?:??"

    if duration < 0:
        return "?:??"
    minutes = int(duration // 60)
    seconds = int(duration % 60)
    return f"{minutes}:{seconds:02d}"


def build_search_keyboard(
    *,
    entries: Sequence[dict[str, Any]],
    page: int,
    total_pages: int,
    start_index: int,
    user_id: int,
) -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardBuilder()
    video_buttons: list[types.InlineKeyboardButton] = []

    for index, entry in enumerate(entries, start=start_index + 1):
        video_id = str(entry.get("id") or "")
        if not video_id:
            continue
        title = truncate_text(entry.get("title") or "Невідомо", 38)
        duration = _format_duration(entry.get("duration"))

        keyboard.row(
            types.InlineKeyboardButton(
                text=f"🎵 {index}. {title} | {duration}",
                callback_data=AudioCallbackData(
                    user_id=user_id,
                    video_id=video_id,
                ).pack(),
            ),
        )
        video_buttons.append(
            types.InlineKeyboardButton(
                text=f"🎬 {index}",
                callback_data=VideoCallbackData(
                    user_id=user_id,
                    video_id=video_id,
                ).pack(),
            ),
        )

    if video_buttons:
        keyboard.row(*video_buttons, width=5)

    keyboard.row(
        types.InlineKeyboardButton(
            text="⏮",
            callback_data=PaginationCallbackData(action="first", user_id=user_id).pack(),
        ),
        types.InlineKeyboardButton(
            text="◀️",
            callback_data=PaginationCallbackData(action="prev", user_id=user_id).pack(),
        ),
        types.InlineKeyboardButton(
            text=f"📄 {page + 1}/{total_pages}",
            callback_data=PaginationCallbackData(action="info", user_id=user_id).pack(),
        ),
        types.InlineKeyboardButton(
            text="▶️",
            callback_data=PaginationCallbackData(action="next", user_id=user_id).pack(),
        ),
        types.InlineKeyboardButton(
            text="⏭",
            callback_data=PaginationCallbackData(action="last", user_id=user_id).pack(),
        ),
    )

    current_count = len(entries)
    if current_count:
        keyboard.row(
            types.InlineKeyboardButton(
                text=f"⬇️ {current_count} аудіо",
                callback_data=DownloadAllCallbackData(
                    user_id=user_id,
                    media_type="audio",
                ).pack(),
            ),
            types.InlineKeyboardButton(
                text=f"⬇️ {current_count} відео",
                callback_data=DownloadAllCallbackData(
                    user_id=user_id,
                    media_type="video",
                ).pack(),
            ),
        )

    return keyboard.as_markup()
