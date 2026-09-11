from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
import psutil


logger = logging.getLogger(__name__)
BASE_DIR = Path(__file__).resolve().parent


def _resolve_path(value: str | Path, base_dir: Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve()


def _resolve_executable(
    env_name: str,
    filename: str,
    base_dir: Path,
) -> Optional[Path]:
    configured = os.getenv(env_name)
    if configured:
        configured_path = _resolve_path(configured, base_dir)
        if configured_path.is_file():
            return configured_path
        logger.warning(
            "%s у .env вказано, але файл не знайдено: %s",
            env_name,
            configured_path,
        )

    local_path = (base_dir / filename).resolve()
    if local_path.is_file():
        return local_path

    found = shutil.which(filename) or shutil.which(Path(filename).stem)
    if found:
        return Path(found).resolve()

    return None


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} має бути цілим числом") from exc


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on", "так"}:
        return True
    if value in {"0", "false", "no", "off", "ні"}:
        return False
    raise ValueError(f"{name} має бути true/false")


def _env_access_mode(name: str, default: str) -> str:
    value = (os.getenv(name) or default).strip().lower()
    if value not in {"public", "whitelist", "private"}:
        raise ValueError(f"{name} має бути public, whitelist або private")
    return value


def _env_int_tuple(name: str, default: tuple[int, ...]) -> tuple[int, ...]:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    values: list[int] = []
    for item in raw.replace(";", ",").split(","):
        item = item.strip()
        if not item:
            continue
        try:
            value = int(item)
        except ValueError as exc:
            raise ValueError(f"{name} має містити лише цілі числа через кому") from exc
        if value <= 0:
            raise ValueError(f"{name} має містити лише числа > 0")
        values.append(value)
    if not values:
        raise ValueError(f"{name} не може бути порожнім")
    return tuple(values)


def _env_user_ids(name: str) -> frozenset[int]:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return frozenset()
    result: set[int] = set()
    for item in raw.replace(";", ",").split(","):
        item = item.strip()
        if not item:
            continue
        try:
            user_id = int(item)
        except ValueError as exc:
            raise ValueError(f"{name} має містити Telegram user_id через кому") from exc
        if user_id <= 0:
            raise ValueError(f"{name} має містити лише додатні Telegram user_id")
        result.add(user_id)
    return frozenset(result)


@dataclass(frozen=True, slots=True)
class Config:
    BASE_DIR: Path

    BOT_TOKEN: str
    ADMIN_ID: int
    CHANNEL_MUSIC: Optional[str]
    CHANNEL_VIDEO: Optional[str]
    BOT_USERNAME: str
    # Залишено для сумісності зі старим .env. У v3 /music і /video все одно
    # жорстко доступні лише ADMIN_ID.
    CHANNEL_COMMANDS_ADMIN_ONLY: bool
    STRICT_CHANNEL_CHECK: bool
    COLLECT_USER_CONTACT_DATA: bool

    MUSIC_DIR: Path
    VIDEO_DIR: Path
    DOWNLOADS_DIR: Path
    TEMP_DIR: Path
    LOG_DIR: Path

    USERS_DB: Path
    SONGS_DB: Path
    SEARCH_RESULTS_DB: Path

    FFMPEG_EXE: Optional[Path]
    FFPROBE_EXE: Optional[Path]

    MAX_SEGMENT_SIZE: int
    MAX_QUERY_LENGTH: int
    MAX_QUEUE_SIZE: int
    MAX_GLOBAL_QUEUE_SIZE: int
    MAX_CONCURRENT_DOWNLOADS: int
    MAX_CONCURRENT_FFMPEG: int
    MAX_CONCURRENT_YOUTUBE: int
    MIN_DISK_SPACE_MB: int
    BOT_STORAGE_QUOTA_GB: int
    SYSTEM_FREE_RESERVE_GB: int
    MIN_DOWNLOAD_RESERVATION_MB: int
    UNKNOWN_DOWNLOAD_RESERVATION_MB: int

    MAX_SEARCH_RESULTS: int
    MAX_PLAYLIST_RESULTS: int
    SEARCH_COOLDOWN_SECONDS: int
    SEARCH_MEMORY_CACHE_SIZE: int
    RESULTS_PAGE_SIZE: int
    TABLE_PAGE_SIZE: int

    FILE_RETENTION_SECONDS: int
    CLEANUP_INTERVAL_SECONDS: int
    SEARCH_CACHE_TTL_HOURS: int

    YTDLP_SOCKET_TIMEOUT: int
    TELEGRAM_REQUEST_TIMEOUT: int
    TELEGRAM_SEND_RETRIES: int
    TELEGRAM_RETRY_BASE_SECONDS: int

    SEGMENT_CACHE_VERSION: int

    LOG_MAX_BYTES: int
    LOG_BACKUP_COUNT: int

    # Security hardening v3
    USER_PRIVATE_CHAT_ONLY: bool
    ADMIN_PRIVATE_CHAT_ONLY: bool
    POLLING_TASKS_CONCURRENCY_LIMIT: int
    SECURITY_DEFAULT_ACCESS_MODE: str
    SECURITY_WHITELIST_IDS: frozenset[int]

    SECURITY_GENERAL_USER_LIMIT: int
    SECURITY_GENERAL_WINDOW_SECONDS: int
    SECURITY_BURST_USER_LIMIT: int
    SECURITY_BURST_WINDOW_SECONDS: int
    SECURITY_GLOBAL_UPDATE_LIMIT: int
    SECURITY_GLOBAL_UPDATE_WINDOW_SECONDS: int
    SECURITY_CALLBACK_USER_LIMIT: int
    SECURITY_CALLBACK_WINDOW_SECONDS: int

    SECURITY_SEARCH_USER_LIMIT: int
    SECURITY_SEARCH_WINDOW_SECONDS: int
    SECURITY_SEARCH_GLOBAL_LIMIT: int
    SECURITY_SEARCH_GLOBAL_WINDOW_SECONDS: int

    SECURITY_PLAYLIST_USER_LIMIT: int
    SECURITY_PLAYLIST_WINDOW_SECONDS: int
    SECURITY_PLAYLIST_GLOBAL_LIMIT: int
    SECURITY_PLAYLIST_GLOBAL_WINDOW_SECONDS: int

    SECURITY_DOWNLOAD_USER_LIMIT: int
    SECURITY_DOWNLOAD_WINDOW_SECONDS: int
    SECURITY_DOWNLOAD_GLOBAL_LIMIT: int
    SECURITY_DOWNLOAD_GLOBAL_WINDOW_SECONDS: int

    SECURITY_STRIKES_BEFORE_BAN: int
    SECURITY_STRIKE_DECAY_SECONDS: int
    SECURITY_STRIKE_COOLDOWN_SECONDS: int
    SECURITY_BAN_NOTICE_COOLDOWN_SECONDS: int
    SECURITY_AUTOBAN_MINUTES: tuple[int, ...]

    SECURITY_MAX_QUEUE_SIZE_PER_USER: int
    SECURITY_MAX_GLOBAL_QUEUE_SIZE: int
    MAX_ACTIVE_DOWNLOADS_PER_USER: int
    MAX_SEARCH_SESSIONS_PER_USER: int
    SECURITY_SEARCH_MEMORY_CACHE_HARD_CAP: int
    DEDUP_COOLDOWN_SECONDS: int

    @classmethod
    def load(cls, base_dir: Path = BASE_DIR) -> "Config":
        load_dotenv(base_dir / ".env")

        token = (os.getenv("BOT_TOKEN") or os.getenv("API_TOKEN") or "").strip()
        if not token:
            raise RuntimeError(
                "Не знайдено BOT_TOKEN. Додайте BOT_TOKEN у файл .env "
                "(API_TOKEN підтримується лише як fallback)."
            )

        admin_id = _env_int("ADMIN_ID", 0)
        logical_cpu = max(1, os.cpu_count() or 4)
        try:
            ram_gb = psutil.virtual_memory().total / 1024**3
        except Exception:
            ram_gb = 8.0

        if ram_gb < 8:
            default_downloads = max(2, min(4, logical_cpu))
            default_youtube = max(2, min(4, logical_cpu))
            default_ffmpeg = 1
        elif ram_gb < 16:
            default_downloads = max(2, min(6, logical_cpu))
            default_youtube = max(2, min(6, logical_cpu))
            default_ffmpeg = max(1, min(2, logical_cpu // 2 or 1))
        else:
            default_downloads = max(2, min(8, logical_cpu))
            default_youtube = max(2, min(8, logical_cpu))
            default_ffmpeg = max(1, min(2, logical_cpu // 2 or 1))

        music_dir = _resolve_path(os.getenv("MUSIC_DIR", "music"), base_dir)
        video_dir = _resolve_path(os.getenv("VIDEO_DIR", "video"), base_dir)
        downloads_dir = _resolve_path(os.getenv("DOWNLOADS_DIR", "downloads"), base_dir)
        temp_dir = _resolve_path(os.getenv("TEMP_DIR", "temp"), base_dir)
        log_dir = _resolve_path(os.getenv("LOG_DIR", "logs"), base_dir)

        return cls(
            BASE_DIR=base_dir.resolve(),
            BOT_TOKEN=token,
            ADMIN_ID=admin_id,
            CHANNEL_MUSIC=(os.getenv("CHANNEL_MUSIC") or "").strip() or None,
            CHANNEL_VIDEO=(os.getenv("CHANNEL_VIDEO") or "").strip() or None,
            BOT_USERNAME=(os.getenv("BOT_USERNAME") or "@AudioDownloaderBot").strip(),
            CHANNEL_COMMANDS_ADMIN_ONLY=_env_bool("CHANNEL_COMMANDS_ADMIN_ONLY", True),
            STRICT_CHANNEL_CHECK=_env_bool("STRICT_CHANNEL_CHECK", True),
            COLLECT_USER_CONTACT_DATA=_env_bool("COLLECT_USER_CONTACT_DATA", False),
            MUSIC_DIR=music_dir,
            VIDEO_DIR=video_dir,
            DOWNLOADS_DIR=downloads_dir,
            TEMP_DIR=temp_dir,
            LOG_DIR=log_dir,
            USERS_DB=_resolve_path(os.getenv("USERS_DB", "users.db"), base_dir),
            SONGS_DB=_resolve_path(os.getenv("SONGS_DB", "songs.db"), base_dir),
            SEARCH_RESULTS_DB=_resolve_path(
                os.getenv("SEARCH_RESULTS_DB", "search_results.db"),
                base_dir,
            ),
            FFMPEG_EXE=_resolve_executable("FFMPEG_EXE", "ffmpeg.exe", base_dir),
            FFPROBE_EXE=_resolve_executable("FFPROBE_EXE", "ffprobe.exe", base_dir),
            MAX_SEGMENT_SIZE=_env_int("MAX_SEGMENT_SIZE", 49 * 1024 * 1024),
            MAX_QUERY_LENGTH=_env_int("MAX_QUERY_LENGTH", 200),
            MAX_QUEUE_SIZE=_env_int("MAX_QUEUE_SIZE", 50),
            MAX_GLOBAL_QUEUE_SIZE=_env_int("MAX_GLOBAL_QUEUE_SIZE", 500),
            MAX_CONCURRENT_DOWNLOADS=_env_int("MAX_CONCURRENT_DOWNLOADS", default_downloads),
            MAX_CONCURRENT_FFMPEG=_env_int("MAX_CONCURRENT_FFMPEG", default_ffmpeg),
            MAX_CONCURRENT_YOUTUBE=_env_int("MAX_CONCURRENT_YOUTUBE", default_youtube),
            MIN_DISK_SPACE_MB=_env_int("MIN_DISK_SPACE_MB", 1024),
            BOT_STORAGE_QUOTA_GB=_env_int("BOT_STORAGE_QUOTA_GB", 50),
            SYSTEM_FREE_RESERVE_GB=_env_int("SYSTEM_FREE_RESERVE_GB", 10),
            MIN_DOWNLOAD_RESERVATION_MB=_env_int("MIN_DOWNLOAD_RESERVATION_MB", 512),
            UNKNOWN_DOWNLOAD_RESERVATION_MB=_env_int("UNKNOWN_DOWNLOAD_RESERVATION_MB", 4096),
            MAX_SEARCH_RESULTS=_env_int("MAX_SEARCH_RESULTS", 50),
            MAX_PLAYLIST_RESULTS=_env_int("MAX_PLAYLIST_RESULTS", 5000),
            SEARCH_COOLDOWN_SECONDS=_env_int("SEARCH_COOLDOWN_SECONDS", 2),
            SEARCH_MEMORY_CACHE_SIZE=_env_int("SEARCH_MEMORY_CACHE_SIZE", 100),
            RESULTS_PAGE_SIZE=_env_int("RESULTS_PAGE_SIZE", 10),
            TABLE_PAGE_SIZE=_env_int("TABLE_PAGE_SIZE", 10),
            FILE_RETENTION_SECONDS=_env_int("FILE_RETENTION_SECONDS", 7 * 24 * 3600),
            CLEANUP_INTERVAL_SECONDS=_env_int("CLEANUP_INTERVAL_SECONDS", 3600),
            SEARCH_CACHE_TTL_HOURS=_env_int("SEARCH_CACHE_TTL_HOURS", 48),
            YTDLP_SOCKET_TIMEOUT=_env_int("YTDLP_SOCKET_TIMEOUT", 30),
            TELEGRAM_REQUEST_TIMEOUT=_env_int("TELEGRAM_REQUEST_TIMEOUT", 120),
            TELEGRAM_SEND_RETRIES=_env_int("TELEGRAM_SEND_RETRIES", 3),
            TELEGRAM_RETRY_BASE_SECONDS=_env_int("TELEGRAM_RETRY_BASE_SECONDS", 1),
            SEGMENT_CACHE_VERSION=3,
            LOG_MAX_BYTES=_env_int("LOG_MAX_BYTES", 10 * 1024 * 1024),
            LOG_BACKUP_COUNT=_env_int("LOG_BACKUP_COUNT", 10),

            USER_PRIVATE_CHAT_ONLY=_env_bool("USER_PRIVATE_CHAT_ONLY", True),
            ADMIN_PRIVATE_CHAT_ONLY=_env_bool("ADMIN_PRIVATE_CHAT_ONLY", True),
            POLLING_TASKS_CONCURRENCY_LIMIT=_env_int("POLLING_TASKS_CONCURRENCY_LIMIT", 100),
            SECURITY_DEFAULT_ACCESS_MODE=_env_access_mode(
                "SECURITY_DEFAULT_ACCESS_MODE", "public"
            ),
            SECURITY_WHITELIST_IDS=_env_user_ids("SECURITY_WHITELIST_IDS"),

            SECURITY_GENERAL_USER_LIMIT=_env_int("SECURITY_GENERAL_USER_LIMIT", 60),
            SECURITY_GENERAL_WINDOW_SECONDS=_env_int("SECURITY_GENERAL_WINDOW_SECONDS", 60),
            SECURITY_BURST_USER_LIMIT=_env_int("SECURITY_BURST_USER_LIMIT", 12),
            SECURITY_BURST_WINDOW_SECONDS=_env_int("SECURITY_BURST_WINDOW_SECONDS", 5),
            SECURITY_GLOBAL_UPDATE_LIMIT=_env_int("SECURITY_GLOBAL_UPDATE_LIMIT", 1000),
            SECURITY_GLOBAL_UPDATE_WINDOW_SECONDS=_env_int(
                "SECURITY_GLOBAL_UPDATE_WINDOW_SECONDS", 60
            ),
            SECURITY_CALLBACK_USER_LIMIT=_env_int("SECURITY_CALLBACK_USER_LIMIT", 40),
            SECURITY_CALLBACK_WINDOW_SECONDS=_env_int("SECURITY_CALLBACK_WINDOW_SECONDS", 60),

            SECURITY_SEARCH_USER_LIMIT=_env_int("SECURITY_SEARCH_USER_LIMIT", 10),
            SECURITY_SEARCH_WINDOW_SECONDS=_env_int("SECURITY_SEARCH_WINDOW_SECONDS", 60),
            SECURITY_SEARCH_GLOBAL_LIMIT=_env_int("SECURITY_SEARCH_GLOBAL_LIMIT", 120),
            SECURITY_SEARCH_GLOBAL_WINDOW_SECONDS=_env_int(
                "SECURITY_SEARCH_GLOBAL_WINDOW_SECONDS", 60
            ),

            SECURITY_PLAYLIST_USER_LIMIT=_env_int("SECURITY_PLAYLIST_USER_LIMIT", 3),
            SECURITY_PLAYLIST_WINDOW_SECONDS=_env_int("SECURITY_PLAYLIST_WINDOW_SECONDS", 300),
            SECURITY_PLAYLIST_GLOBAL_LIMIT=_env_int("SECURITY_PLAYLIST_GLOBAL_LIMIT", 30),
            SECURITY_PLAYLIST_GLOBAL_WINDOW_SECONDS=_env_int(
                "SECURITY_PLAYLIST_GLOBAL_WINDOW_SECONDS", 300
            ),

            SECURITY_DOWNLOAD_USER_LIMIT=_env_int("SECURITY_DOWNLOAD_USER_LIMIT", 30),
            SECURITY_DOWNLOAD_WINDOW_SECONDS=_env_int("SECURITY_DOWNLOAD_WINDOW_SECONDS", 60),
            SECURITY_DOWNLOAD_GLOBAL_LIMIT=_env_int("SECURITY_DOWNLOAD_GLOBAL_LIMIT", 300),
            SECURITY_DOWNLOAD_GLOBAL_WINDOW_SECONDS=_env_int(
                "SECURITY_DOWNLOAD_GLOBAL_WINDOW_SECONDS", 60
            ),

            SECURITY_STRIKES_BEFORE_BAN=_env_int("SECURITY_STRIKES_BEFORE_BAN", 3),
            SECURITY_STRIKE_DECAY_SECONDS=_env_int("SECURITY_STRIKE_DECAY_SECONDS", 900),
            SECURITY_STRIKE_COOLDOWN_SECONDS=_env_int("SECURITY_STRIKE_COOLDOWN_SECONDS", 5),
            SECURITY_BAN_NOTICE_COOLDOWN_SECONDS=_env_int(
                "SECURITY_BAN_NOTICE_COOLDOWN_SECONDS", 30
            ),
            SECURITY_AUTOBAN_MINUTES=_env_int_tuple(
                "SECURITY_AUTOBAN_MINUTES", (5, 15, 60, 360)
            ),

            SECURITY_MAX_QUEUE_SIZE_PER_USER=_env_int(
                "SECURITY_MAX_QUEUE_SIZE_PER_USER", 30
            ),
            SECURITY_MAX_GLOBAL_QUEUE_SIZE=_env_int("SECURITY_MAX_GLOBAL_QUEUE_SIZE", 300),
            MAX_ACTIVE_DOWNLOADS_PER_USER=_env_int("MAX_ACTIVE_DOWNLOADS_PER_USER", 2),
            MAX_SEARCH_SESSIONS_PER_USER=_env_int("MAX_SEARCH_SESSIONS_PER_USER", 20),
            SECURITY_SEARCH_MEMORY_CACHE_HARD_CAP=_env_int(
                "SECURITY_SEARCH_MEMORY_CACHE_HARD_CAP", 64
            ),
            DEDUP_COOLDOWN_SECONDS=_env_int("DEDUP_COOLDOWN_SECONDS", 3),
        )

    @property
    def DOWNLOAD_TEMP_DIR(self) -> Path:
        return self.TEMP_DIR / "downloads"

    @property
    def SEGMENTS_TEMP_DIR(self) -> Path:
        return self.TEMP_DIR / "segments"

    @property
    def BOT_STORAGE_QUOTA_BYTES(self) -> int:
        return self.BOT_STORAGE_QUOTA_GB * 1024**3

    @property
    def SYSTEM_FREE_RESERVE_BYTES(self) -> int:
        return self.SYSTEM_FREE_RESERVE_GB * 1024**3

    @property
    def EFFECTIVE_MAX_QUEUE_SIZE(self) -> int:
        return min(self.MAX_QUEUE_SIZE, self.SECURITY_MAX_QUEUE_SIZE_PER_USER)

    @property
    def EFFECTIVE_MAX_GLOBAL_QUEUE_SIZE(self) -> int:
        return min(self.MAX_GLOBAL_QUEUE_SIZE, self.SECURITY_MAX_GLOBAL_QUEUE_SIZE)

    @property
    def EFFECTIVE_SEARCH_MEMORY_CACHE_SIZE(self) -> int:
        return min(self.SEARCH_MEMORY_CACHE_SIZE, self.SECURITY_SEARCH_MEMORY_CACHE_HARD_CAP)

    def ensure_directories(self) -> None:
        for path in (
            self.MUSIC_DIR,
            self.VIDEO_DIR,
            self.DOWNLOADS_DIR,
            self.TEMP_DIR,
            self.DOWNLOAD_TEMP_DIR,
            self.SEGMENTS_TEMP_DIR,
            self.LOG_DIR,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def validate_limits(self) -> None:
        checks = {
            "MAX_CONCURRENT_DOWNLOADS": self.MAX_CONCURRENT_DOWNLOADS,
            "MAX_CONCURRENT_FFMPEG": self.MAX_CONCURRENT_FFMPEG,
            "MAX_CONCURRENT_YOUTUBE": self.MAX_CONCURRENT_YOUTUBE,
            "MAX_QUEUE_SIZE": self.MAX_QUEUE_SIZE,
            "MAX_GLOBAL_QUEUE_SIZE": self.MAX_GLOBAL_QUEUE_SIZE,
            "RESULTS_PAGE_SIZE": self.RESULTS_PAGE_SIZE,
            "TABLE_PAGE_SIZE": self.TABLE_PAGE_SIZE,
            "MAX_SEARCH_RESULTS": self.MAX_SEARCH_RESULTS,
            "MAX_PLAYLIST_RESULTS": self.MAX_PLAYLIST_RESULTS,
            "SEARCH_MEMORY_CACHE_SIZE": self.SEARCH_MEMORY_CACHE_SIZE,
            "BOT_STORAGE_QUOTA_GB": self.BOT_STORAGE_QUOTA_GB,
            "SYSTEM_FREE_RESERVE_GB": self.SYSTEM_FREE_RESERVE_GB,
            "MIN_DOWNLOAD_RESERVATION_MB": self.MIN_DOWNLOAD_RESERVATION_MB,
            "UNKNOWN_DOWNLOAD_RESERVATION_MB": self.UNKNOWN_DOWNLOAD_RESERVATION_MB,
            "TELEGRAM_SEND_RETRIES": self.TELEGRAM_SEND_RETRIES,
            "TELEGRAM_RETRY_BASE_SECONDS": self.TELEGRAM_RETRY_BASE_SECONDS,
            "SEGMENT_CACHE_VERSION": self.SEGMENT_CACHE_VERSION,
            "POLLING_TASKS_CONCURRENCY_LIMIT": self.POLLING_TASKS_CONCURRENCY_LIMIT,
            "SECURITY_GENERAL_USER_LIMIT": self.SECURITY_GENERAL_USER_LIMIT,
            "SECURITY_GENERAL_WINDOW_SECONDS": self.SECURITY_GENERAL_WINDOW_SECONDS,
            "SECURITY_BURST_USER_LIMIT": self.SECURITY_BURST_USER_LIMIT,
            "SECURITY_BURST_WINDOW_SECONDS": self.SECURITY_BURST_WINDOW_SECONDS,
            "SECURITY_GLOBAL_UPDATE_LIMIT": self.SECURITY_GLOBAL_UPDATE_LIMIT,
            "SECURITY_GLOBAL_UPDATE_WINDOW_SECONDS": self.SECURITY_GLOBAL_UPDATE_WINDOW_SECONDS,
            "SECURITY_CALLBACK_USER_LIMIT": self.SECURITY_CALLBACK_USER_LIMIT,
            "SECURITY_CALLBACK_WINDOW_SECONDS": self.SECURITY_CALLBACK_WINDOW_SECONDS,
            "SECURITY_SEARCH_USER_LIMIT": self.SECURITY_SEARCH_USER_LIMIT,
            "SECURITY_SEARCH_WINDOW_SECONDS": self.SECURITY_SEARCH_WINDOW_SECONDS,
            "SECURITY_SEARCH_GLOBAL_LIMIT": self.SECURITY_SEARCH_GLOBAL_LIMIT,
            "SECURITY_SEARCH_GLOBAL_WINDOW_SECONDS": self.SECURITY_SEARCH_GLOBAL_WINDOW_SECONDS,
            "SECURITY_PLAYLIST_USER_LIMIT": self.SECURITY_PLAYLIST_USER_LIMIT,
            "SECURITY_PLAYLIST_WINDOW_SECONDS": self.SECURITY_PLAYLIST_WINDOW_SECONDS,
            "SECURITY_PLAYLIST_GLOBAL_LIMIT": self.SECURITY_PLAYLIST_GLOBAL_LIMIT,
            "SECURITY_PLAYLIST_GLOBAL_WINDOW_SECONDS": self.SECURITY_PLAYLIST_GLOBAL_WINDOW_SECONDS,
            "SECURITY_DOWNLOAD_USER_LIMIT": self.SECURITY_DOWNLOAD_USER_LIMIT,
            "SECURITY_DOWNLOAD_WINDOW_SECONDS": self.SECURITY_DOWNLOAD_WINDOW_SECONDS,
            "SECURITY_DOWNLOAD_GLOBAL_LIMIT": self.SECURITY_DOWNLOAD_GLOBAL_LIMIT,
            "SECURITY_DOWNLOAD_GLOBAL_WINDOW_SECONDS": self.SECURITY_DOWNLOAD_GLOBAL_WINDOW_SECONDS,
            "SECURITY_STRIKES_BEFORE_BAN": self.SECURITY_STRIKES_BEFORE_BAN,
            "SECURITY_STRIKE_DECAY_SECONDS": self.SECURITY_STRIKE_DECAY_SECONDS,
            "SECURITY_STRIKE_COOLDOWN_SECONDS": self.SECURITY_STRIKE_COOLDOWN_SECONDS,
            "SECURITY_BAN_NOTICE_COOLDOWN_SECONDS": self.SECURITY_BAN_NOTICE_COOLDOWN_SECONDS,
            "SECURITY_MAX_QUEUE_SIZE_PER_USER": self.SECURITY_MAX_QUEUE_SIZE_PER_USER,
            "SECURITY_MAX_GLOBAL_QUEUE_SIZE": self.SECURITY_MAX_GLOBAL_QUEUE_SIZE,
            "MAX_ACTIVE_DOWNLOADS_PER_USER": self.MAX_ACTIVE_DOWNLOADS_PER_USER,
            "MAX_SEARCH_SESSIONS_PER_USER": self.MAX_SEARCH_SESSIONS_PER_USER,
            "SECURITY_SEARCH_MEMORY_CACHE_HARD_CAP": self.SECURITY_SEARCH_MEMORY_CACHE_HARD_CAP,
        }
        for name, value in checks.items():
            if value < 1:
                raise ValueError(f"{name} має бути не менше 1")

        if self.SEARCH_COOLDOWN_SECONDS < 0:
            raise ValueError("SEARCH_COOLDOWN_SECONDS не може бути від'ємним")
        if self.DEDUP_COOLDOWN_SECONDS < 0:
            raise ValueError("DEDUP_COOLDOWN_SECONDS не може бути від'ємним")
        if self.UNKNOWN_DOWNLOAD_RESERVATION_MB < self.MIN_DOWNLOAD_RESERVATION_MB:
            raise ValueError(
                "UNKNOWN_DOWNLOAD_RESERVATION_MB не може бути меншим за "
                "MIN_DOWNLOAD_RESERVATION_MB"
            )
        if self.MAX_QUEUE_SIZE > self.MAX_GLOBAL_QUEUE_SIZE:
            raise ValueError("MAX_QUEUE_SIZE не може бути більшим за MAX_GLOBAL_QUEUE_SIZE")
        if self.SECURITY_MAX_QUEUE_SIZE_PER_USER > self.SECURITY_MAX_GLOBAL_QUEUE_SIZE:
            raise ValueError(
                "SECURITY_MAX_QUEUE_SIZE_PER_USER не може бути більшим за "
                "SECURITY_MAX_GLOBAL_QUEUE_SIZE"
            )
        if self.MAX_ACTIVE_DOWNLOADS_PER_USER > self.MAX_CONCURRENT_DOWNLOADS:
            raise ValueError(
                "MAX_ACTIVE_DOWNLOADS_PER_USER не може бути більшим за "
                "MAX_CONCURRENT_DOWNLOADS"
            )
        if self.MAX_SEGMENT_SIZE < 1024 * 1024:
            raise ValueError("MAX_SEGMENT_SIZE має бути не менше 1 МБ")
        if self.MIN_DISK_SPACE_MB < 128:
            raise ValueError("MIN_DISK_SPACE_MB має бути не менше 128 МБ")
        if any(value <= 0 for value in self.SECURITY_AUTOBAN_MINUTES):
            raise ValueError("SECURITY_AUTOBAN_MINUTES має містити лише додатні числа")
