from __future__ import annotations

import asyncio
import inspect
import logging
import os

# Keep the usual IDE entry point, but let the supervisor own the actual bot.
if __name__ == "__main__" and not os.environ.get("MUSIC_BOT_RUN_DIR"):
    from supervisor import main as supervisor_main
    raise SystemExit(supervisor_main())

import psutil

from aiogram import Bot, Dispatcher
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.storage.memory import MemoryStorage

from config import Config
from database import Database
from handlers.admin import create_admin_router
from handlers.callbacks import create_callbacks_router
from handlers.downloads import create_downloads_router
from handlers.search import create_search_router
from handlers.security import create_security_router
from handlers.start import create_start_router
from security.middleware import SecurityMiddleware
from security.service import SecurityService
from services.downloader import DownloadService
from services.ffmpeg import FFmpegService
from services.storage import StorageGuard
from services.youtube import YouTubeService
from utils.logging import configure_logging
from utils.command_menu import configure_command_menu
from utils.process_lock import ProcessLock, SingleInstanceError
from utils.supervision import supervision
from workers.cleanup import CleanupWorker
from workers.download_worker import DownloadManager


async def _validate_channel(
    *,
    bot: Bot,
    target: str | None,
    label: str,
    strict: bool,
    logger: logging.Logger,
) -> None:
    if not target:
        message = f"{label}: не налаштовано"
        if strict:
            logger.critical(message)
            raise RuntimeError(message)
        logger.warning(message)
        return

    try:
        chat = await bot.get_chat(target)
        if chat.type != ChatType.CHANNEL:
            raise RuntimeError(f"target не є Telegram Channel (type={chat.type})")

        me = await bot.get_me()
        member = await bot.get_chat_member(chat.id, me.id)
        status = getattr(member, "status", None)
        status_value = getattr(status, "value", str(status or ""))
        can_post = getattr(member, "can_post_messages", None)

        if status_value not in {"administrator", "creator"}:
            raise RuntimeError(
                f"бот не є адміністратором каналу (status={status_value or 'unknown'})"
            )
        if can_post is False:
            raise RuntimeError("бот не має права can_post_messages")

        logger.info(
            "%s: OK target=%s chat_id=%s title=%r type=%s",
            label,
            target,
            chat.id,
            getattr(chat, "title", None),
            chat.type,
        )
    except (TelegramAPIError, RuntimeError) as exc:
        message = f"{label}: ПОМИЛКА target={target!r}: {exc}"
        if strict:
            logger.critical(message)
            raise RuntimeError(message) from exc
        logger.error(message)


async def main() -> None:
    config = Config.load()
    config.ensure_directories()
    config.validate_limits()
    configure_logging(config)
    logger = logging.getLogger(__name__)

    instance_lock = ProcessLock(config.BASE_DIR / "bot.instance.lock")
    try:
        instance_lock.acquire()
    except SingleInstanceError as exc:
        logger.critical("Запуск заблоковано: %s", exc)
        raise SystemExit(77)

    if config.ADMIN_ID == 0:
        logger.warning("ADMIN_ID не вказано: адміністративні команди будуть недоступні")

    if not config.CHANNEL_COMMANDS_ADMIN_ONLY:
        logger.warning(
            "CHANNEL_COMMANDS_ADMIN_ONLY=false проігноровано: Security v3 жорстко "
            "залишає /music і /video лише для ADMIN_ID"
        )

    if config.FFMPEG_EXE is None or config.FFPROBE_EXE is None:
        instance_lock.release()
        logger.critical(
            "FFmpeg/FFprobe не знайдено. Перевірте FFMPEG_EXE, FFPROBE_EXE або PATH."
        )
        raise RuntimeError("FFmpeg/FFprobe не знайдено")

    # Після захоплення single-instance lock будь-яка помилка конструктора має
    # звільнити lock. Інакше невдала ініціалізація могла залишити stale lock
    # до наступної спроби запуску.
    try:
        bot = Bot(token=config.BOT_TOKEN)
        dispatcher = Dispatcher(storage=MemoryStorage())
        database = Database(config)
        ffmpeg = FFmpegService(config)
        youtube = YouTubeService(config)
        storage = StorageGuard(config)
        security = SecurityService(config, database)
        download_service = DownloadService(
            bot=bot,
            config=config,
            database=database,
            youtube=youtube,
            ffmpeg=ffmpeg,
            storage=storage,
        )
        manager = DownloadManager(
            download_service,
            max_concurrent=config.MAX_CONCURRENT_DOWNLOADS,
            max_per_user=config.EFFECTIVE_MAX_QUEUE_SIZE,
            max_global=config.EFFECTIVE_MAX_GLOBAL_QUEUE_SIZE,
            max_active_per_user=config.MAX_ACTIVE_DOWNLOADS_PER_USER,
            dedup_cooldown_seconds=config.DEDUP_COOLDOWN_SECONDS,
        )
        cleanup = CleanupWorker(
            config=config,
            database=database,
            activity_checker=manager,
        )
    except Exception:
        instance_lock.release()
        logger.exception("Помилка ініціалізації компонентів бота")
        raise

    ready_task = None
    try:
        supervision.task = asyncio.create_task(supervision.watch(dispatcher, asyncio.current_task()))
        await database.connect()
        await security.initialize()

        ffmpeg_ok, ffprobe_ok = await ffmpeg.self_check()
        if not ffmpeg_ok or not ffprobe_ok:
            raise RuntimeError(
                "FFmpeg або FFprobe знайдено за шляхом, але перевірка запуску не пройдена"
            )
        runtime_info = await youtube.initialize()

        await _validate_channel(
            bot=bot,
            target=config.CHANNEL_MUSIC,
            label="CHANNEL_MUSIC",
            strict=config.STRICT_CHANNEL_CHECK,
            logger=logger,
        )
        await _validate_channel(
            bot=bot,
            target=config.CHANNEL_VIDEO,
            label="CHANNEL_VIDEO",
            strict=config.STRICT_CHANNEL_CHECK,
            logger=logger,
        )

        await manager.start()
        await cleanup.start()

        # Update-level middleware стоїть до router handlers: group/access/ban/
        # flood перевіряються раніше за yt-dlp, SQLite search та queue enqueue.
        dispatcher.update.outer_middleware(SecurityMiddleware(config, security, manager))

        dispatcher.include_router(create_start_router(config, database))
        dispatcher.include_router(
            create_downloads_router(
                config=config,
                youtube=youtube,
                manager=manager,
                security=security,
            )
        )
        dispatcher.include_router(
            create_security_router(
                config=config,
                security=security,
                manager=manager,
            )
        )
        dispatcher.include_router(
            create_admin_router(
                bot=bot,
                dispatcher=dispatcher,
                config=config,
                database=database,
                manager=manager,
                storage=storage,
            )
        )
        dispatcher.include_router(
            create_callbacks_router(
                bot=bot,
                config=config,
                database=database,
                manager=manager,
                security=security,
            )
        )
        # Catch-all текстовий пошук має бути останнім router-ом.
        dispatcher.include_router(
            create_search_router(
                bot=bot,
                config=config,
                database=database,
                youtube=youtube,
                security=security,
                manager=manager,
            )
        )

        storage_snapshot = await storage.snapshot("video")
        memory = psutil.virtual_memory()
        security_status = await security.status()
        gib = 1024**3

        logger.info("========================================")
        logger.info("Бот запускається")
        logger.info("FFmpeg: OK (%s)", config.FFMPEG_EXE)
        logger.info("FFprobe: OK (%s)", config.FFPROBE_EXE)
        if runtime_info.name:
            logger.info(
                "JS runtime: %s %s (%s)",
                runtime_info.name,
                runtime_info.version or "версія невідома",
                runtime_info.path,
            )
        else:
            logger.warning("JS runtime: Deno/Node не знайдено")
        if runtime_info.ejs_installed:
            logger.info("yt-dlp-ejs: встановлено (%s)", runtime_info.ejs_version)
        else:
            logger.warning(
                "yt-dlp-ejs: не встановлено; використовується remote_components=ejs:github"
            )
        logger.info("БД: OK")
        logger.info("CPU logical cores: %s", os.cpu_count() or "невідомо")
        logger.info(
            "RAM: total=%.1f ГБ, available=%.1f ГБ",
            memory.total / gib,
            memory.available / gib,
        )
        logger.info("MAX_CONCURRENT_DOWNLOADS: %s", config.MAX_CONCURRENT_DOWNLOADS)
        logger.info("MAX_ACTIVE_DOWNLOADS_PER_USER: %s", config.MAX_ACTIVE_DOWNLOADS_PER_USER)
        logger.info("MAX_CONCURRENT_FFMPEG: %s", config.MAX_CONCURRENT_FFMPEG)
        logger.info("MAX_CONCURRENT_YOUTUBE: %s", config.MAX_CONCURRENT_YOUTUBE)
        logger.info(
            "QUEUE: configured user=%s global=%s; effective security user=%s global=%s",
            config.MAX_QUEUE_SIZE,
            config.MAX_GLOBAL_QUEUE_SIZE,
            config.EFFECTIVE_MAX_QUEUE_SIZE,
            config.EFFECTIVE_MAX_GLOBAL_QUEUE_SIZE,
        )
        logger.info("DEDUP_COOLDOWN_SECONDS: %s", config.DEDUP_COOLDOWN_SECONDS)
        logger.info("MAX_SEARCH_RESULTS: %s", config.MAX_SEARCH_RESULTS)
        logger.info("MAX_PLAYLIST_RESULTS: %s", config.MAX_PLAYLIST_RESULTS)
        logger.info("MAX_SEARCH_SESSIONS_PER_USER: %s", config.MAX_SEARCH_SESSIONS_PER_USER)
        logger.info(
            "SEARCH_MEMORY_CACHE: configured=%s effective=%s",
            config.SEARCH_MEMORY_CACHE_SIZE,
            config.EFFECTIVE_SEARCH_MEMORY_CACHE_SIZE,
        )
        logger.info(
            "Security v3: mode=%s private_users=%s private_admin=%s polling_tasks=%s "
            "active_bans=%s whitelist=%s",
            security_status.access_mode,
            config.USER_PRIVATE_CHAT_ONLY,
            config.ADMIN_PRIVATE_CHAT_ONLY,
            config.POLLING_TASKS_CONCURRENCY_LIMIT,
            security_status.active_bans,
            security_status.effective_allowlist_count,
        )
        logger.info(
            "Storage: disk_total=%.1f ГБ, disk_free=%.1f ГБ, quota=%.1f ГБ, "
            "bot_used=%.2f ГБ, safe_free=%.2f ГБ, bot_available=%.2f ГБ, "
            "system_reserve=%s ГБ",
            storage_snapshot.target_total_bytes / gib,
            storage_snapshot.target_free_bytes / gib,
            config.BOT_STORAGE_QUOTA_BYTES / gib,
            storage_snapshot.bot_used_bytes / gib,
            storage_snapshot.safe_disk_free_bytes / gib,
            storage_snapshot.available_for_bot_bytes / gib,
            config.SYSTEM_FREE_RESERVE_GB,
        )
        logger.info("SEGMENT_CACHE_VERSION: %s", config.SEGMENT_CACHE_VERSION)
        logger.info("SingleInstanceLock: OK (%s)", config.BASE_DIR / "bot.instance.lock")
        logger.info("========================================")

        if "tasks_concurrency_limit" not in inspect.signature(
            dispatcher.start_polling
        ).parameters:
            raise RuntimeError(
                "Встановлена версія aiogram не підтримує tasks_concurrency_limit. "
                r"Оновіть залежності: .\.venv\Scripts\python.exe -m pip install -r requirements.txt"
            )

        async def report_ready() -> None:
            # Startup hooks run after our initialization. Yield until polling starts.
            await asyncio.sleep(0)
            supervision.ready = True
            if os.environ.get("MUSIC_BOT_NOTIFY_RESTART"):
                target = os.environ.get("MUSIC_BOT_NOTIFY_CHAT") or config.ADMIN_ID
                if target:
                    try:
                        await bot.send_message(int(target), "✅ Бот перезапущено й готовий до роботи.")
                    except Exception:
                        logger.exception("Не вдалося повідомити про готовність бота")

        async def on_startup(**kwargs) -> None:
            nonlocal ready_task
            await configure_command_menu(bot, config.ADMIN_ID)
            ready_task = asyncio.create_task(report_ready())

        dispatcher.startup.register(on_startup)
        await dispatcher.start_polling(
            bot,
            close_bot_session=False,
            handle_as_tasks=True,
            tasks_concurrency_limit=config.POLLING_TASKS_CONCURRENCY_LIMIT,
        )

    except Exception:
        logger.exception("Критична помилка під час роботи бота")
        raise
    finally:
        await supervision.close()
        if ready_task is not None:
            ready_task.cancel()
            await asyncio.gather(ready_task, return_exceptions=True)
        logger.info("Починаю коректне завершення бота")
        try:
            await cleanup.stop()
        except Exception:
            logger.exception("Помилка зупинки Cleaner")
        try:
            await manager.shutdown()
        except Exception:
            logger.exception("Помилка зупинки DownloadManager")
        try:
            await ffmpeg.shutdown()
        except Exception:
            logger.exception("Помилка shutdown FFmpeg")
        try:
            await cleanup.cleanup_all_download_temp()
        except Exception:
            logger.exception("Помилка cleanup temp")
        try:
            await database.close()
        except Exception:
            logger.exception("Помилка закриття БД")
        try:
            await bot.session.close()
        except Exception:
            logger.exception("Помилка закриття Telegram session")
        instance_lock.release()
        logger.info("Бот коректно завершено")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
