from __future__ import annotations

import errno

from aiogram.exceptions import TelegramAPIError, TelegramBadRequest


class DownloadCancelledError(Exception):
    """Користувач або shutdown скасував завантаження."""


class FFmpegError(RuntimeError):
    """Помилка FFmpeg/FFprobe з технічними деталями лише для логів."""


def is_invalid_file_id_error(error: Exception) -> bool:
    if not isinstance(error, TelegramBadRequest):
        return False

    text = str(error).lower()
    markers = (
        "wrong file identifier",
        "wrong remote file identifier",
        "file_id",
        "file identifier",
        "failed to get http url content",
    )
    return any(marker in text for marker in markers)


def get_user_friendly_error(error: Exception, media_type: str = "файл") -> str:
    if isinstance(error, DownloadCancelledError):
        return "🛑 Завантаження скасовано."

    if isinstance(error, OSError) and error.errno == errno.ENOSPC:
        return "💾 На диску недостатньо вільного місця для завантаження."

    text = str(error).lower()

    if isinstance(error, FileNotFoundError):
        return (
            "❌ Фінальний файл не знайдено після завантаження.\n"
            "Спробуйте ще раз."
        )

    if isinstance(error, TelegramAPIError):
        if any(marker in text for marker in ("file is too big", "too large", "request entity too large")):
            return "❌ Файл занадто великий для Telegram."
        if "timeout" in text or "timed out" in text:
            return "⏱️ Telegram не встиг прийняти файл. Спробуйте ще раз."
        return "❌ Не вдалося надіслати файл у Telegram. Спробуйте ще раз."

    if any(marker in text for marker in ("private video", "video is private", "this video is private")):
        return "🔒 Це відео приватне. YouTube не дозволяє його завантажити."

    if any(
        marker in text
        for marker in (
            "video unavailable",
            "this video is unavailable",
            "video has been removed",
            "this video has been removed",
        )
    ):
        return "❌ Відео недоступне або було видалене з YouTube."

    if any(
        marker in text
        for marker in (
            "confirm you're not a bot",
            "confirm you’re not a bot",
            "sign in to confirm",
        )
    ):
        return (
            "🔐 YouTube вимагає додаткового підтвердження доступу.\n"
            "Спробуйте інше відео або повторіть пізніше."
        )

    if any(marker in text for marker in ("age-restricted", "age restricted", "confirm your age")):
        return "🔞 Відео має вікове обмеження і зараз не може бути завантажене."

    if any(marker in text for marker in ("members-only", "members only")):
        return "🔒 Це відео доступне лише учасникам каналу YouTube."

    if "copyright" in text or "blocked" in text:
        return "🚫 YouTube заблокував доступ до цього відео."

    if any(marker in text for marker in ("http error 403", "403 forbidden", "forbidden")):
        return (
            "⚠️ YouTube тимчасово відмовив у завантаженні.\n"
            "Спробуйте ще раз пізніше."
        )

    if any(marker in text for marker in ("requested format is not available", "format is not available")):
        return f"❌ Потрібний формат {media_type} недоступний для цього відео."

    if isinstance(error, FFmpegError) or "ffmpeg" in text or "ffprobe" in text:
        return "❌ Не вдалося обробити файл через FFmpeg. Спробуйте інше відео."

    if any(marker in text for marker in ("no space left on device", "disk full")):
        return "💾 На диску недостатньо вільного місця для завантаження."

    if any(marker in text for marker in ("timed out", "timeout", "socket")):
        return "⏱️ Час очікування YouTube вичерпано. Спробуйте ще раз."

    if any(marker in text for marker in ("network", "connection", "temporary failure")):
        return "🌐 Помилка з'єднання з YouTube. Спробуйте ще раз."

    return f"❌ Не вдалося завантажити {media_type}. Спробуйте ще раз або виберіть інше відео."
