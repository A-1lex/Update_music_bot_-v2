from __future__ import annotations

import asyncio
import logging
import shutil
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import AsyncContextManager, Protocol

from config import Config
from database import Database
from utils.filenames import is_path_inside


logger = logging.getLogger(__name__)


class MediaActivityChecker(Protocol):
    async def is_media_pending_or_active(self, video_id: str, media_type: str) -> bool: ...

    def media_maintenance_guard(
        self,
        video_id: str,
        media_type: str,
    ) -> AsyncContextManager[None]: ...


class CleanupWorker:
    def __init__(
        self,
        config: Config,
        database: Database,
        activity_checker: MediaActivityChecker | None = None,
    ) -> None:
        self.config = config
        self.database = database
        self.activity_checker = activity_checker
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._loop(), name="cleanup-worker")
        logger.info(
            "Cleaner запущено: інтервал=%s сек, зберігання файлів=%s сек, search TTL=%s год",
            self.config.CLEANUP_INTERVAL_SECONDS,
            self.config.FILE_RETENTION_SECONDS,
            self.config.SEARCH_CACHE_TTL_HOURS,
        )

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)
        self._task = None
        logger.info("Cleaner зупинено")

    async def _loop(self) -> None:
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Помилка фонового очищення")

            await asyncio.sleep(self.config.CLEANUP_INTERVAL_SECONDS)

    async def run_once(self) -> None:
        deleted_search = await self.database.cleanup_old_search_results(
            self.config.SEARCH_CACHE_TTL_HOURS
        )
        if deleted_search:
            logger.info("Видалено прострочених search_results: %s", deleted_search)

        cutoff = datetime.now(timezone.utc) - timedelta(
            seconds=self.config.FILE_RETENTION_SECONDS
        )
        expired = await self.database.get_expired_local_files(cutoff.isoformat())

        for video_id, media_type, raw_path in expired:
            try:
                if self.activity_checker is None:
                    await self._delete_one(video_id, media_type, raw_path)
                    continue

                # Важливо: lock спільний із DownloadService. Після входу в lock
                # повторно перевіряємо manager, тому між check і unlink немає race-вікна.
                async with self.activity_checker.media_maintenance_guard(video_id, media_type):
                    if await self.activity_checker.is_media_pending_or_active(
                        video_id,
                        media_type,
                    ):
                        logger.info(
                            "Cleaner пропускає активний media cache media_type=%s video_id=%s",
                            media_type,
                            video_id,
                        )
                        continue
                    await self._delete_one(video_id, media_type, raw_path)
            except Exception:
                logger.exception(
                    "Cleaner: помилка обробки media_type=%s video_id=%s",
                    media_type,
                    video_id,
                )

        await self._cleanup_stale_temp_dirs()

    async def _delete_one(self, video_id: str, media_type: str, raw_path: str) -> None:
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            path = (self.config.BASE_DIR / path).resolve()
        else:
            path = path.resolve()

        expected_root = self.config.MUSIC_DIR if media_type == "audio" else self.config.VIDEO_DIR
        if not is_path_inside(path, [expected_root]):
            logger.warning(
                "Cleaner не видаляє шлях поза дозволеним каталогом: %s",
                path,
            )
            await self.database.clear_file_path(video_id, media_type)
            return

        try:
            if path.exists():
                await asyncio.to_thread(path.unlink)
                logger.info(
                    "Cleaner видалив старий файл media_type=%s video_id=%s path=%s",
                    media_type,
                    video_id,
                    path,
                )
        except OSError:
            logger.exception("Не вдалося видалити старий файл: %s", path)
            return

        await self.database.clear_file_path(video_id, media_type)

    async def _cleanup_stale_temp_dirs(self) -> None:
        cutoff_timestamp = time.time() - self.config.FILE_RETENTION_SECONDS
        for root in (self.config.DOWNLOAD_TEMP_DIR, self.config.SEGMENTS_TEMP_DIR):
            if not root.exists():
                continue
            await asyncio.to_thread(
                self._remove_stale_tree,
                root,
                cutoff_timestamp,
            )

    @staticmethod
    def _remove_stale_tree(root: Path, cutoff_timestamp: float) -> None:
        directories = sorted(
            (path for path in root.rglob("*") if path.is_dir()),
            key=lambda path: len(path.parts),
            reverse=True,
        )
        for path in directories:
            try:
                if path.exists() and path.stat().st_mtime < cutoff_timestamp:
                    shutil.rmtree(path, ignore_errors=True)
            except OSError:
                continue

    async def cleanup_all_download_temp(self) -> None:
        for root in (self.config.DOWNLOAD_TEMP_DIR, self.config.SEGMENTS_TEMP_DIR):
            if not root.exists():
                continue
            for child in list(root.iterdir()):
                try:
                    if child.is_dir():
                        await asyncio.to_thread(shutil.rmtree, child, True)
                    else:
                        await asyncio.to_thread(child.unlink)
                except OSError:
                    logger.exception(
                        "Не вдалося очистити тимчасовий шлях під час shutdown: %s",
                        child,
                    )
