from __future__ import annotations

import asyncio
import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from config import Config


logger = logging.getLogger(__name__)
_MB = 1024**2


@dataclass(frozen=True, slots=True)
class StorageSnapshot:
    bot_used_bytes: int
    reserved_bytes: int
    quota_bytes: int
    target_total_bytes: int
    target_used_bytes: int
    target_free_bytes: int
    temp_free_bytes: int
    system_reserve_bytes: int

    @property
    def quota_free_bytes(self) -> int:
        return max(0, self.quota_bytes - self.bot_used_bytes - self.reserved_bytes)

    @property
    def safe_disk_free_bytes(self) -> int:
        return max(
            0,
            min(self.target_free_bytes, self.temp_free_bytes)
            - self.system_reserve_bytes
            - self.reserved_bytes,
        )

    @property
    def available_for_bot_bytes(self) -> int:
        """Реально доступний обсяг з урахуванням і квоти, і фізичного диска."""
        return max(0, min(self.quota_free_bytes, self.safe_disk_free_bytes))


class StorageGuard:
    """Глобальна квота диска + резерв вільного місця для ОС.

    Квота рахує каталоги music/video/temp/downloads. Під час одночасних
    завантажень додатково враховуються reservations, щоб кілька задач не могли
    одночасно вирішити, що їм усім вистачає останніх гігабайтів.
    """

    def __init__(self, config: Config) -> None:
        self.config = config
        self._lock = asyncio.Lock()
        self._reservations: dict[str, int] = {}

    async def reserve(
        self,
        *,
        task_id: str,
        expected_bytes: int | None,
        media_type: str,
    ) -> int:
        estimate = int(expected_bytes or 0)
        minimum = self.config.MIN_DOWNLOAD_RESERVATION_MB * _MB
        unknown = self.config.UNKNOWN_DOWNLOAD_RESERVATION_MB * _MB
        # Для відомого розміру додаємо запас на мердж/постобробку. Для
        # невідомого розміру резервуємо окремий, значно більший бюджет і
        # далі передаємо його як hard-limit у yt-dlp.
        desired_required = (
            max(minimum, int(estimate * 1.35))
            if estimate > 0
            else max(minimum, unknown)
        )

        async with self._lock:
            snapshot = await self._snapshot_locked(media_type)
            available = snapshot.available_for_bot_bytes
            if estimate > 0:
                required = desired_required
                if required > available:
                    raise OSError(
                        28,
                        "Недостатньо дискового простору для завантаження: "
                        f"потрібно приблизно {required / _MB:.0f} МБ, "
                        f"доступно боту {available / _MB:.0f} МБ. "
                        f"Квота={self.config.BOT_STORAGE_QUOTA_GB} ГБ, "
                        f"системний резерв={self.config.SYSTEM_FREE_RESERVE_GB} ГБ",
                    )
            else:
                # Розмір невідомий: резервуємо до UNKNOWN_DOWNLOAD_RESERVATION_MB,
                # але якщо квоти лишилося менше — дозволяємо задачу з меншим
                # hard-limit, доки є хоча б базовий мінімум.
                if available < minimum:
                    raise OSError(
                        28,
                        "Недостатньо дискового простору для завантаження з "
                        "невідомим розміром",
                    )
                required = min(desired_required, available)

            self._reservations[task_id] = int(required)

        logger.info(
            "Зарезервовано місце task_id=%s media_type=%s reserve=%.1f МБ estimate=%s",
            task_id,
            media_type,
            required / _MB,
            f"{estimate / _MB:.1f} МБ" if estimate else "невідомо (hard-limit)",
        )
        return required

    async def reserve_exact(
        self,
        *,
        task_id: str,
        required_bytes: int,
        media_type: str,
        label: str = "temporary",
    ) -> int:
        required = max(1, int(required_bytes))
        async with self._lock:
            snapshot = await self._snapshot_locked(media_type)
            available = snapshot.available_for_bot_bytes
            if required > available:
                raise OSError(
                    28,
                    f"Недостатньо місця для {label}: потрібно "
                    f"{required / _MB:.0f} МБ, доступно боту {available / _MB:.0f} МБ",
                )
            self._reservations[task_id] = required

        logger.info(
            "Зарезервовано точний тимчасовий бюджет task_id=%s media_type=%s "
            "label=%s reserve=%.1f МБ",
            task_id,
            media_type,
            label,
            required / _MB,
        )
        return required

    async def release(self, task_id: str) -> None:
        async with self._lock:
            self._reservations.pop(task_id, None)

    async def snapshot(self, media_type: str = "video") -> StorageSnapshot:
        async with self._lock:
            return await self._snapshot_locked(media_type)

    async def _snapshot_locked(self, media_type: str) -> StorageSnapshot:
        target_dir = (
            self.config.MUSIC_DIR if media_type == "audio" else self.config.VIDEO_DIR
        )
        bot_used, target_usage, temp_usage = await asyncio.gather(
            asyncio.to_thread(self._calculate_bot_usage),
            asyncio.to_thread(shutil.disk_usage, target_dir),
            asyncio.to_thread(shutil.disk_usage, self.config.TEMP_DIR),
        )
        return StorageSnapshot(
            bot_used_bytes=bot_used,
            reserved_bytes=sum(self._reservations.values()),
            quota_bytes=self.config.BOT_STORAGE_QUOTA_BYTES,
            target_total_bytes=target_usage.total,
            target_used_bytes=target_usage.used,
            target_free_bytes=target_usage.free,
            temp_free_bytes=temp_usage.free,
            system_reserve_bytes=max(
                self.config.SYSTEM_FREE_RESERVE_BYTES,
                self.config.MIN_DISK_SPACE_MB * _MB,
            ),
        )

    def _calculate_bot_usage(self) -> int:
        roots = [
            self.config.MUSIC_DIR,
            self.config.VIDEO_DIR,
            self.config.TEMP_DIR,
            self.config.DOWNLOADS_DIR,
        ]
        unique_roots: list[Path] = []
        for root in roots:
            resolved = root.resolve()
            if any(
                resolved == existing or self._is_within(resolved, existing)
                for existing in unique_roots
            ):
                continue
            unique_roots = [
                existing
                for existing in unique_roots
                if not self._is_within(existing, resolved)
            ]
            unique_roots.append(resolved)

        total = 0
        for root in unique_roots:
            if not root.exists():
                continue
            for dirpath, _, filenames in os.walk(root):
                for filename in filenames:
                    path = Path(dirpath) / filename
                    try:
                        total += path.stat().st_size
                    except OSError:
                        continue
        return total

    @staticmethod
    def _is_within(path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False
