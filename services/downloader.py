from __future__ import annotations

import asyncio
import logging
import re
import shutil
import threading
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Awaitable, Callable, Optional, Union

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import (
    ClientDecodeError,
    TelegramAPIError,
    TelegramEntityTooLarge,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.types import FSInputFile, Message

from config import Config
from database import CachedMedia, CachedMediaPart, Database
from services.ffmpeg import FFmpegService
from services.youtube import YouTubeService
from services.storage import StorageGuard
from utils.errors import (
    DownloadCancelledError,
    get_user_friendly_error,
    is_invalid_file_id_error,
)
from utils.filenames import is_path_inside
from utils.html import escape_html


logger = logging.getLogger(__name__)
ChatTarget = Union[int, str]


class MediaType(str, Enum):
    AUDIO = "audio"
    VIDEO = "video"

    @property
    def user_label(self) -> str:
        return "аудіо" if self is MediaType.AUDIO else "відео"

    @property
    def extension(self) -> str:
        return ".mp3" if self is MediaType.AUDIO else ".mp4"


@dataclass(slots=True)
class DownloadTask:
    user_id: int
    video_id: str
    media_type: MediaType
    target_chat_id: ChatTarget
    notify_chat_id: Optional[int] = None
    task_id: str = field(default_factory=lambda: uuid.uuid4().hex)


@dataclass(slots=True, frozen=True)
class SentPart:
    part_number: int
    total_parts: int
    file_id: str
    file_size: Optional[int]


@dataclass(slots=True)
class SendResult:
    file_id: Optional[str] = None
    parts: list[SentPart] = field(default_factory=list)


class InvalidCachedPartError(RuntimeError):
    def __init__(
        self,
        *,
        failed_part: int,
        sent_prefix: list[CachedMediaPart],
        original: TelegramAPIError,
    ) -> None:
        super().__init__(
            f"Недійсний Telegram file_id сегмента {failed_part}: {original}"
        )
        self.failed_part = failed_part
        self.sent_prefix = sent_prefix
        self.original = original


class CancellationToken:
    def __init__(self) -> None:
        self.async_event = asyncio.Event()
        self.thread_event = threading.Event()

    def cancel(self) -> None:
        self.async_event.set()
        self.thread_event.set()

    @property
    def is_cancelled(self) -> bool:
        return self.async_event.is_set() or self.thread_event.is_set()

    def raise_if_cancelled(self) -> None:
        if self.is_cancelled:
            raise DownloadCancelledError("Завантаження скасовано користувачем")


class DownloadService:
    def __init__(
        self,
        *,
        bot: Bot,
        config: Config,
        database: Database,
        youtube: YouTubeService,
        ffmpeg: FFmpegService,
        storage: StorageGuard,
    ) -> None:
        self.bot = bot
        self.config = config
        self.database = database
        self.youtube = youtube
        self.ffmpeg = ffmpeg
        self.storage = storage
        self._media_locks: dict[tuple[str, MediaType], asyncio.Lock] = {}
        self._media_lock_refs: dict[tuple[str, MediaType], int] = {}
        self._media_locks_guard = asyncio.Lock()

    async def process(self, task: DownloadTask, token: CancellationToken) -> None:
        logger.info(
            "Розпочато завантаження media_type=%s video_id=%s user_id=%s task_id=%s",
            task.media_type.value,
            task.video_id,
            task.user_id,
            task.task_id,
        )

        try:
            async with self._media_lock(task.video_id, task.media_type):
                token.raise_if_cancelled()
                await self._process_locked(task, token)

        except DownloadCancelledError:
            logger.info(
                "Завантаження скасовано media_type=%s video_id=%s user_id=%s task_id=%s",
                task.media_type.value,
                task.video_id,
                task.user_id,
                task.task_id,
            )
            raise
        except Exception as exc:
            logger.exception(
                "Помилка завантаження media_type=%s video_id=%s user_id=%s task_id=%s",
                task.media_type.value,
                task.video_id,
                task.user_id,
                task.task_id,
            )
            await self._notify_error(task, exc)
        finally:
            logger.info(
                "Завершено завдання media_type=%s video_id=%s user_id=%s task_id=%s",
                task.media_type.value,
                task.video_id,
                task.user_id,
                task.task_id,
            )

    @asynccontextmanager
    async def _media_lock(self, video_id: str, media_type: MediaType):
        key = (video_id, media_type)
        async with self._media_locks_guard:
            lock = self._media_locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._media_locks[key] = lock
            self._media_lock_refs[key] = self._media_lock_refs.get(key, 0) + 1

        try:
            async with lock:
                yield
        finally:
            async with self._media_locks_guard:
                remaining = self._media_lock_refs.get(key, 1) - 1
                if remaining <= 0:
                    self._media_lock_refs.pop(key, None)
                    self._media_locks.pop(key, None)
                else:
                    self._media_lock_refs[key] = remaining

    @asynccontextmanager
    async def media_maintenance_guard(self, video_id: str, media_type: str):
        """Той самий media-lock для Cleaner/admin delete без race condition."""
        try:
            normalized = MediaType(media_type)
        except ValueError as exc:
            raise ValueError("media_type має бути audio або video") from exc
        async with self._media_lock(video_id, normalized):
            yield

    async def _process_locked(self, task: DownloadTask, token: CancellationToken) -> None:
        cached = await self.database.get_cached_media(
            task.video_id,
            task.media_type.value,
        )

        if cached and cached.file_id:
            token.raise_if_cancelled()
            try:
                await self._send_cached_file_id(task, cached, token)
                await self.database.save_download(
                    user_id=task.user_id,
                    video_id=task.video_id,
                    title=cached.title,
                    media_type=task.media_type.value,
                    file_path=cached.file_path,
                    file_id=cached.file_id,
                )
                await self.database.delete_cached_parts(
                    task.video_id,
                    task.media_type.value,
                )
                await self._notify_channel_success(task)
                logger.info(
                    "Файл надіслано з Telegram file_id cache media_type=%s video_id=%s",
                    task.media_type.value,
                    task.video_id,
                )
                return
            except TelegramAPIError as exc:
                if not is_invalid_file_id_error(exc):
                    raise
                logger.warning(
                    "Недійсний file_id; очищаю лише media_type=%s video_id=%s: %s",
                    task.media_type.value,
                    task.video_id,
                    exc,
                )
                await self.database.invalidate_file_id(
                    task.video_id,
                    task.media_type.value,
                )
                cached.file_id = None

        # Великі файли кешуються не одним file_id, а набором file_id сегментів.
        # Це дозволяє повторно надсилати файл без FFmpeg і без upload з ПК.
        resume_prefix: list[CachedMediaPart] = []
        cached_parts = await self.database.get_cached_parts(
            task.video_id,
            task.media_type.value,
        )
        if cached_parts:
            token.raise_if_cancelled()
            title = cached.title if cached else "Невідомо"
            try:
                await self._send_cached_parts(
                    task=task,
                    parts=cached_parts,
                    title=title,
                    token=token,
                )
                await self.database.save_download(
                    user_id=task.user_id,
                    video_id=task.video_id,
                    title=title,
                    media_type=task.media_type.value,
                    file_path=cached.file_path if cached else None,
                    file_id=None,
                )
                await self._notify_channel_success(task)
                logger.info(
                    "Файл надіслано з Telegram segment file_id cache "
                    "media_type=%s video_id=%s parts=%s",
                    task.media_type.value,
                    task.video_id,
                    len(cached_parts),
                )
                return
            except InvalidCachedPartError as exc:
                resume_prefix = exc.sent_prefix
                await self.database.delete_cached_parts(
                    task.video_id,
                    task.media_type.value,
                )
                logger.warning(
                    "Недійсний file_id сегмента %s; cache сегментів очищено. "
                    "Продовжую з локального/нового файла без повторної відправки "
                    "вже успішних сегментів=%s media_type=%s video_id=%s",
                    exc.failed_part,
                    len(resume_prefix),
                    task.media_type.value,
                    task.video_id,
                )

        local_path = self._valid_cached_local_path(cached, task.media_type)
        if local_path is not None:
            token.raise_if_cancelled()
            send_result = await self._send_local_file(
                task=task,
                file_path=local_path,
                title=cached.title if cached else "Невідомо",
                token=token,
                cached_prefix=resume_prefix,
            )
            await self.database.save_download(
                user_id=task.user_id,
                video_id=task.video_id,
                title=cached.title if cached else local_path.stem,
                media_type=task.media_type.value,
                file_path=str(local_path),
                file_id=send_result.file_id,
            )
            await self._save_send_cache(task, send_result)
            await self._notify_channel_success(task)
            logger.info(
                "Використано локальний cache media_type=%s video_id=%s path=%s%s",
                task.media_type.value,
                task.video_id,
                local_path,
                (
                    f"; збережено segment file_id cache parts={len(send_result.parts)}"
                    if send_result.parts
                    else ""
                ),
            )
            return

        if cached and cached.file_path:
            await self.database.clear_file_path(task.video_id, task.media_type.value)

        await self._ensure_disk_space(task.media_type)
        token.raise_if_cancelled()

        try:
            estimated_bytes = await self.youtube.estimate_download_size(
                task.video_id,
                task.media_type.value,
            )
        except Exception as exc:
            estimated_bytes = None
            logger.warning(
                "Не вдалося оцінити розмір до завантаження media_type=%s video_id=%s: %s",
                task.media_type.value,
                task.video_id,
                exc,
            )

        reserved_bytes = await self.storage.reserve(
            task_id=task.task_id,
            expected_bytes=estimated_bytes,
            media_type=task.media_type.value,
        )
        download_reservation_active = True

        work_dir = (
            self.config.DOWNLOAD_TEMP_DIR
            / str(task.user_id)
            / task.video_id
            / uuid.uuid4().hex
        )

        try:
            work_dir.mkdir(parents=True, exist_ok=True)
            source_limit_bytes = max(
                self.config.MIN_DOWNLOAD_RESERVATION_MB * 1024 * 1024,
                int(reserved_bytes / 1.35),
            )
            result = await self.youtube.download_media(
                video_id=task.video_id,
                media_type=task.media_type.value,
                work_dir=work_dir,
                cancel_event=token.thread_event,
                max_source_size=source_limit_bytes,
            )
            token.raise_if_cancelled()

            if result.video_id != task.video_id:
                logger.warning(
                    "yt-dlp повернув інший video_id: requested=%s resolved=%s",
                    task.video_id,
                    result.video_id,
                )

            persistent_dir = (
                self.config.MUSIC_DIR
                if task.media_type is MediaType.AUDIO
                else self.config.VIDEO_DIR
            )
            persistent_path = persistent_dir / f"{task.video_id}{task.media_type.extension}"
            await asyncio.to_thread(
                self._move_replace,
                result.final_path,
                persistent_path,
            )

            logger.info(
                "Фінальний файл переміщено media_type=%s video_id=%s path=%s",
                task.media_type.value,
                task.video_id,
                persistent_path,
            )

            # ВАЖЛИВО: реєструємо локальний файл ДО Telegram upload.
            # Якщо користувач натисне /cancel або Telegram тимчасово впаде вже
            # після завершення yt-dlp, наступна спроба повинна використати
            # готовий локальний файл, а не повторно качати його з YouTube.
            # Це також не залишає persistent media "сиротою" поза cleaner/БД.
            await self.database.save_download(
                user_id=task.user_id,
                video_id=task.video_id,
                title=result.title,
                media_type=task.media_type.value,
                file_path=str(persistent_path),
                file_id=None,
                refresh_local_file_age=True,
            )

            # Після переміщення persistent-файл уже входить у bot_used. Старий
            # yt-dlp reservation більше не потрібний інакше ми двічі рахуємо
            # той самий обсяг. Якщо буде split, він отримає власний temp reservation.
            await self.storage.release(task.task_id)
            download_reservation_active = False

            send_result = await self._send_local_file(
                task=task,
                file_path=persistent_path,
                title=result.title,
                token=token,
                cached_prefix=resume_prefix,
            )

            await self.database.save_download(
                user_id=task.user_id,
                video_id=task.video_id,
                title=result.title,
                media_type=task.media_type.value,
                file_path=str(persistent_path),
                file_id=send_result.file_id,
                refresh_local_file_age=False,
            )
            await self._save_send_cache(task, send_result)
            await self._notify_channel_success(task)
            logger.info(
                "Файл успішно надіслано в Telegram media_type=%s video_id=%s "
                "single_file_id=%s segment_parts=%s",
                task.media_type.value,
                task.video_id,
                bool(send_result.file_id),
                len(send_result.parts),
            )

        finally:
            await asyncio.to_thread(shutil.rmtree, work_dir, True)
            if download_reservation_active:
                await self.storage.release(task.task_id)

    def _valid_cached_local_path(
        self,
        cached: Optional[CachedMedia],
        media_type: MediaType,
    ) -> Optional[Path]:
        if cached is None or not cached.file_path:
            return None

        path = Path(cached.file_path).expanduser()
        if not path.is_absolute():
            path = (self.config.BASE_DIR / path).resolve()
        else:
            path = path.resolve()

        expected_root = (
            self.config.MUSIC_DIR
            if media_type is MediaType.AUDIO
            else self.config.VIDEO_DIR
        )
        if not is_path_inside(path, [expected_root]):
            logger.warning(
                "Cache file_path поза дозволеним каталогом, ігнорую: %s",
                path,
            )
            return None
        if not path.is_file() or path.suffix.lower() != media_type.extension:
            return None
        return path

    async def _sleep_or_cancel(
        self,
        delay: float,
        cancel_event: Optional[asyncio.Event],
    ) -> None:
        if cancel_event is None:
            await asyncio.sleep(delay)
            return

        if cancel_event.is_set():
            raise DownloadCancelledError("Завантаження скасовано користувачем")

        try:
            await asyncio.wait_for(cancel_event.wait(), timeout=delay)
        except asyncio.TimeoutError:
            return
        raise DownloadCancelledError("Завантаження скасовано користувачем")

    async def _await_telegram_send(
        self,
        factory: Callable[[], Awaitable[Message]],
        cancel_event: Optional[asyncio.Event],
    ) -> Message:
        if cancel_event is None:
            return await factory()
        if cancel_event.is_set():
            raise DownloadCancelledError("Завантаження скасовано користувачем")

        send_task = asyncio.create_task(factory())
        cancel_task = asyncio.create_task(cancel_event.wait())
        try:
            done, _ = await asyncio.wait(
                {send_task, cancel_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            # Якщо Telegram уже завершив відправку, файл фактично надіслано —
            # повертаємо результат. Якщо cancel прийшов раніше, обриваємо HTTP upload.
            if send_task in done:
                return await send_task

            send_task.cancel()
            await asyncio.gather(send_task, return_exceptions=True)
            raise DownloadCancelledError("Завантаження скасовано користувачем")
        finally:
            cancel_task.cancel()
            await asyncio.gather(cancel_task, return_exceptions=True)
            if not send_task.done():
                send_task.cancel()
                await asyncio.gather(send_task, return_exceptions=True)

    async def _telegram_send_with_retry(
        self,
        operation: str,
        factory: Callable[[], Awaitable[Message]],
        *,
        cancel_event: Optional[asyncio.Event] = None,
    ) -> Message:
        attempts = max(1, self.config.TELEGRAM_SEND_RETRIES)
        base_delay = max(1, self.config.TELEGRAM_RETRY_BASE_SECONDS)

        for attempt in range(1, attempts + 1):
            if cancel_event is not None and cancel_event.is_set():
                raise DownloadCancelledError("Завантаження скасовано користувачем")
            try:
                return await self._await_telegram_send(factory, cancel_event)
            except TelegramRetryAfter as exc:
                if attempt >= attempts:
                    raise
                retry_after = getattr(exc, "retry_after", base_delay)
                try:
                    delay = max(base_delay, float(retry_after))
                except (TypeError, ValueError):
                    delay = float(base_delay)
                logger.warning(
                    "Telegram rate limit під час %s: спроба=%s/%s, чекаю %.1f сек",
                    operation, attempt, attempts, delay,
                )
                await self._sleep_or_cancel(delay, cancel_event)
            except TelegramEntityTooLarge:
                # HTTP 413 — постійна помилка для цього payload; retry лише
                # повторно завантажить той самий завеликий файл.
                raise
            except (TelegramNetworkError, TelegramServerError, ClientDecodeError) as exc:
                if attempt >= attempts:
                    raise
                delay = float(base_delay * (2 ** (attempt - 1)))
                logger.warning(
                    "Тимчасова помилка Telegram під час %s: %s; "
                    "спроба=%s/%s, retry через %.1f сек",
                    operation, exc, attempt, attempts, delay,
                )
                await self._sleep_or_cancel(delay, cancel_event)

        raise RuntimeError(f"Telegram retry вичерпано: {operation}")

    async def _send_cached_file_id(
        self,
        task: DownloadTask,
        cached: CachedMedia,
        token: CancellationToken,
    ) -> Message:
        caption = self._caption(cached.title)
        if task.media_type is MediaType.AUDIO:
            async def send_audio() -> Message:
                return await self.bot.send_audio(
                    chat_id=task.target_chat_id,
                    audio=cached.file_id,
                    title=self._audio_track_title(cached.title),
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    request_timeout=self.config.TELEGRAM_REQUEST_TIMEOUT,
                )
            return await self._telegram_send_with_retry(
                "cached audio", send_audio, cancel_event=token.async_event
            )

        async def send_video() -> Message:
            return await self.bot.send_video(
                chat_id=task.target_chat_id,
                video=cached.file_id,
                caption=caption,
                parse_mode=ParseMode.HTML,
                supports_streaming=True,
                request_timeout=self.config.TELEGRAM_REQUEST_TIMEOUT,
            )
        return await self._telegram_send_with_retry(
            "cached video", send_video, cancel_event=token.async_event
        )

    async def _send_cached_parts(
        self,
        *,
        task: DownloadTask,
        parts: list[CachedMediaPart],
        title: str,
        token: CancellationToken,
    ) -> None:
        for position, part in enumerate(parts):
            token.raise_if_cancelled()
            try:
                await self._send_cached_part(
                    task=task,
                    file_id=part.file_id,
                    title=title,
                    part_number=part.part_number,
                    token=token,
                )
            except TelegramAPIError as exc:
                if not is_invalid_file_id_error(exc):
                    raise
                raise InvalidCachedPartError(
                    failed_part=part.part_number,
                    sent_prefix=list(parts[:position]),
                    original=exc,
                ) from exc

    async def _send_cached_part(
        self,
        *,
        task: DownloadTask,
        file_id: str,
        title: str,
        part_number: int,
        token: CancellationToken,
    ) -> Message:
        caption = self._caption(title, part_number)
        if task.media_type is MediaType.AUDIO:
            async def send_audio() -> Message:
                return await self.bot.send_audio(
                    chat_id=task.target_chat_id,
                    audio=file_id,
                    title=self._audio_track_title(title, part_number),
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    request_timeout=self.config.TELEGRAM_REQUEST_TIMEOUT,
                )
            return await self._telegram_send_with_retry(
                f"cached audio part {part_number}",
                send_audio,
                cancel_event=token.async_event,
            )

        async def send_video() -> Message:
            return await self.bot.send_video(
                chat_id=task.target_chat_id,
                video=file_id,
                caption=caption,
                parse_mode=ParseMode.HTML,
                supports_streaming=True,
                request_timeout=self.config.TELEGRAM_REQUEST_TIMEOUT,
            )
        return await self._telegram_send_with_retry(
            f"cached video part {part_number}",
            send_video,
            cancel_event=token.async_event,
        )

    async def _send_local_file(
        self,
        *,
        task: DownloadTask,
        file_path: Path,
        title: str,
        token: CancellationToken,
        cached_prefix: Optional[list[CachedMediaPart]] = None,
    ) -> SendResult:
        if not file_path.is_file():
            raise FileNotFoundError(str(file_path))

        prefix = list(cached_prefix or [])
        token.raise_if_cancelled()
        if file_path.stat().st_size <= self.config.MAX_SEGMENT_SIZE:
            if prefix:
                raise RuntimeError(
                    "Структура медіафайла змінилася: раніше він був сегментованим. "
                    "Повторіть команду ще раз."
                )
            message = await self._send_single_file(task, file_path, title, token=token)
            token.raise_if_cancelled()
            file_id = self._message_file_id(message, task.media_type)
            return SendResult(file_id=file_id)

        split_reservation_id = f"{task.task_id}:split"
        source_size = file_path.stat().st_size
        # Stream-copy сегменти сумарно близькі до оригіналу. 10% запасу + 16 МБ
        # покриває контейнерні накладні витрати без подвійного резервування GB.
        split_required = max(16 * 1024 * 1024, int(source_size * 1.10))
        await self.storage.reserve_exact(
            task_id=split_reservation_id,
            required_bytes=split_required,
            media_type=task.media_type.value,
            label="FFmpeg split",
        )
        try:
            split_result = (
                await self.ffmpeg.split_audio(
                    file_path,
                    self.config.MAX_SEGMENT_SIZE,
                    token.async_event,
                )
                if task.media_type is MediaType.AUDIO
                else await self.ffmpeg.split_video(
                    file_path,
                    self.config.MAX_SEGMENT_SIZE,
                    token.async_event,
                )
            )
        finally:
            await self.storage.release(split_reservation_id)

        total_parts = len(split_result.parts)
        if total_parts < 1:
            await self.ffmpeg.cleanup_split_result(split_result)
            raise RuntimeError("FFmpeg не створив жодного сегмента")

        if prefix:
            expected_prefix_numbers = list(range(1, len(prefix) + 1))
            actual_prefix_numbers = [part.part_number for part in prefix]
            if (
                actual_prefix_numbers != expected_prefix_numbers
                or any(part.total_parts != total_parts for part in prefix)
                or len(prefix) >= total_parts
            ):
                await self.ffmpeg.cleanup_split_result(split_result)
                raise RuntimeError(
                    "Структура сегментів змінилася після втрати Telegram cache. "
                    "Повторіть команду ще раз."
                )

        sent_parts = [
            SentPart(
                part_number=part.part_number,
                total_parts=total_parts,
                file_id=part.file_id,
                file_size=part.file_size,
            )
            for part in prefix
        ]

        try:
            for index, part in enumerate(split_result.parts, start=1):
                token.raise_if_cancelled()
                if part.stat().st_size > self.config.MAX_SEGMENT_SIZE:
                    raise RuntimeError(
                        f"Сегмент {part} перевищує MAX_SEGMENT_SIZE після перевірки"
                    )

                # Під час fallback після недійсного file_id попередні сегменти вже
                # були успішно надіслані в поточному запиті — не дублюємо їх.
                if index <= len(prefix):
                    continue

                message = await self._send_single_file(
                    task,
                    part,
                    title,
                    part_number=index,
                    token=token,
                )
                token.raise_if_cancelled()
                file_id = self._message_file_id(message, task.media_type)
                if not file_id:
                    raise RuntimeError(
                        f"Telegram не повернув file_id для сегмента {index}/{total_parts}"
                    )
                sent_parts.append(
                    SentPart(
                        part_number=index,
                        total_parts=total_parts,
                        file_id=file_id,
                        file_size=part.stat().st_size,
                    )
                )
        finally:
            await self.ffmpeg.cleanup_split_result(split_result)

        if len(sent_parts) != total_parts:
            raise RuntimeError(
                f"Не вдалося сформувати повний cache сегментів: "
                f"{len(sent_parts)}/{total_parts}"
            )

        return SendResult(parts=sent_parts)

    async def _save_send_cache(self, task: DownloadTask, result: SendResult) -> None:
        if result.parts:
            await self.database.replace_cached_parts(
                video_id=task.video_id,
                media_type=task.media_type.value,
                parts=[
                    (
                        part.part_number,
                        part.total_parts,
                        part.file_id,
                        part.file_size,
                    )
                    for part in result.parts
                ],
            )
            logger.info(
                "Збережено Telegram file_id cache сегментів "
                "media_type=%s video_id=%s parts=%s",
                task.media_type.value,
                task.video_id,
                len(result.parts),
            )
            return

        await self.database.delete_cached_parts(
            task.video_id,
            task.media_type.value,
        )

    @staticmethod
    def _message_file_id(message: Message, media_type: MediaType) -> Optional[str]:
        if media_type is MediaType.AUDIO:
            return message.audio.file_id if message.audio else None
        return message.video.file_id if message.video else None

    @staticmethod
    def _audio_track_title(title: str, part_number: Optional[int] = None) -> str:
        normalized = " ".join(str(title or "Невідомо").split()).strip() or "Невідомо"
        if part_number is not None:
            return f"{normalized} — Частина {part_number}"
        return normalized

    @staticmethod
    def _audio_upload_filename(title: str, part_number: Optional[int] = None) -> str:
        """Людське ім'я multipart-файла, яке Telegram зберігає в Audio.file_name."""
        normalized = " ".join(str(title or "Невідомо").split()).strip() or "Невідомо"
        cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1F]', "_", normalized).strip().rstrip(". ")
        if not cleaned:
            cleaned = "audio"

        suffix = f" — Частина {part_number}" if part_number is not None else ""
        # Тримаємо ім'я помірної довжини для Telegram/Windows-клієнтів.
        max_title_len = max(1, 120 - len(suffix))
        return f"{cleaned[:max_title_len]}{suffix}.mp3"

    async def _audio_duration_seconds(self, file_path: Path) -> Optional[int]:
        """Повертає тривалість MP3 для Telegram, щоб картка не показувала 00:00."""
        try:
            duration = await self.ffmpeg.get_duration(file_path)
            return max(1, int(round(duration)))
        except Exception as exc:
            logger.warning(
                "Не вдалося визначити тривалість аудіо %s: %s",
                file_path,
                exc,
            )
            return None

    async def _send_single_file(
        self,
        task: DownloadTask,
        file_path: Path,
        title: str,
        part_number: Optional[int] = None,
        *,
        token: CancellationToken,
    ) -> Message:
        caption = self._caption(title, part_number)
        if task.media_type is MediaType.AUDIO:
            duration = await self._audio_duration_seconds(file_path)
            upload_filename = self._audio_upload_filename(title, part_number)
            logger.info(
                "Надсилання аудіо в Telegram: video_id=%s title=%r filename=%r duration=%s",
                task.video_id,
                self._audio_track_title(title, part_number),
                upload_filename,
                duration,
            )

            async def send_audio() -> Message:
                return await self.bot.send_audio(
                    chat_id=task.target_chat_id,
                    audio=FSInputFile(file_path, filename=upload_filename),
                    title=self._audio_track_title(title, part_number),
                    duration=duration,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    request_timeout=self.config.TELEGRAM_REQUEST_TIMEOUT,
                )
            return await self._telegram_send_with_retry(
                f"audio upload part={part_number or 1}",
                send_audio,
                cancel_event=token.async_event,
            )

        async def send_video() -> Message:
            return await self.bot.send_video(
                chat_id=task.target_chat_id,
                video=FSInputFile(file_path),
                caption=caption,
                parse_mode=ParseMode.HTML,
                supports_streaming=True,
                request_timeout=self.config.TELEGRAM_REQUEST_TIMEOUT,
            )
        return await self._telegram_send_with_retry(
            f"video upload part={part_number or 1}",
            send_video,
            cancel_event=token.async_event,
        )

    def _caption(self, title: str, part_number: Optional[int] = None) -> str:
        part = f"\nЧастина {part_number}" if part_number is not None else ""
        return (
            f"{escape_html(title)}{part}\n"
            f"{escape_html(self.config.BOT_USERNAME)}"
        )

    async def _ensure_disk_space(self, media_type: MediaType) -> None:
        target_dir = (
            self.config.MUSIC_DIR
            if media_type is MediaType.AUDIO
            else self.config.VIDEO_DIR
        )
        usage = await asyncio.to_thread(shutil.disk_usage, target_dir)
        free_mb = usage.free / (1024 * 1024)
        if free_mb < self.config.MIN_DISK_SPACE_MB:
            raise OSError(28, "No space left on device")

    @staticmethod
    def _move_replace(source: Path, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            destination.unlink()
        shutil.move(str(source), str(destination))

    async def _notify_error(self, task: DownloadTask, error: Exception) -> None:
        chat_id: Optional[ChatTarget]
        if task.notify_chat_id is not None:
            chat_id = task.notify_chat_id
        elif isinstance(task.target_chat_id, int):
            chat_id = task.target_chat_id
        else:
            chat_id = None

        if chat_id is None:
            return

        try:
            await self.bot.send_message(
                chat_id,
                get_user_friendly_error(error, task.media_type.user_label),
            )
        except TelegramAPIError:
            logger.exception(
                "Не вдалося надіслати повідомлення про помилку user_id=%s task_id=%s",
                task.user_id,
                task.task_id,
            )

    async def _notify_channel_success(self, task: DownloadTask) -> None:
        if task.notify_chat_id is None:
            return
        if str(task.notify_chat_id) == str(task.target_chat_id):
            return

        text = (
            "✅ Аудіо успішно опубліковано в канал."
            if task.media_type is MediaType.AUDIO
            else "✅ Відео успішно опубліковано в канал."
        )
        try:
            await self.bot.send_message(task.notify_chat_id, text)
        except TelegramAPIError:
            logger.exception(
                "Не вдалося надіслати підтвердження публікації user_id=%s task_id=%s",
                task.user_id,
                task.task_id,
            )
