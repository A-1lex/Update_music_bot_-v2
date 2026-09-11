from __future__ import annotations

import json
from collections import OrderedDict
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

import aiosqlite

from config import Config


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class CachedMedia:
    id: int
    user_id: Optional[int]
    video_id: str
    title: str
    file_path: Optional[str]
    file_id: Optional[str]
    sent_date: Optional[str]
    media_type: str


@dataclass(slots=True)
class CachedMediaPart:
    id: int
    video_id: str
    media_type: str
    part_number: int
    total_parts: int
    file_id: str
    file_size: Optional[int]
    created_at: str
    cache_version: int


@dataclass(slots=True)
class SearchRecord:
    id: int
    user_id: int
    query: str
    results: list[dict[str, Any]]
    page: int
    message_id: Optional[int]
    timestamp: str
    playlist_title: Optional[str]
    playlist_url: Optional[str]
    playlist_id: Optional[str]
    search_key: Optional[str]


@dataclass(slots=True)
class PaginationRecord:
    user_id: int
    table_name: str
    page: int
    message_id: Optional[int]
    timestamp: str


@dataclass(slots=True)
class SecurityUserStateRecord:
    user_id: int
    strikes: int
    last_strike_at: Optional[str]
    auto_ban_level: int
    ban_until: Optional[str]
    ban_reason: Optional[str]
    permanent_ban: bool
    updated_at: str


class Database:
    ADMIN_TABLES = {"users", "downloads", "search_results"}

    def __init__(self, config: Config) -> None:
        self.config = config
        self.users: Optional[aiosqlite.Connection] = None
        self.songs: Optional[aiosqlite.Connection] = None
        self.search: Optional[aiosqlite.Connection] = None
        self._search_results_memory: OrderedDict[int, list[dict[str, Any]]] = OrderedDict()

    async def connect(self) -> None:
        self.users = await aiosqlite.connect(self.config.USERS_DB)
        self.songs = await aiosqlite.connect(self.config.SONGS_DB)
        self.search = await aiosqlite.connect(self.config.SEARCH_RESULTS_DB)

        for conn in (self.users, self.songs, self.search):
            conn.row_factory = aiosqlite.Row
            await conn.execute("PRAGMA journal_mode=WAL;")
            await conn.execute("PRAGMA busy_timeout=5000;")
            await conn.execute("PRAGMA foreign_keys=ON;")
            await conn.commit()

        await self._migrate_users()
        await self._migrate_security()
        await self._migrate_downloads()
        await self._migrate_download_parts()
        await self._validate_download_parts_integrity()
        await self._migrate_search_results()
        await self._migrate_table_pagination()

        logger.info("Бази даних підключено, WAL і busy_timeout активовано")

    async def close(self) -> None:
        for conn in (self.users, self.songs, self.search):
            if conn is not None:
                await conn.close()

        self.users = None
        self.songs = None
        self.search = None
        self._search_results_memory.clear()
        logger.info("З'єднання з базами даних закрито")

    def _require_users(self) -> aiosqlite.Connection:
        if self.users is None:
            raise RuntimeError("users.db ще не підключено")
        return self.users

    def _require_songs(self) -> aiosqlite.Connection:
        if self.songs is None:
            raise RuntimeError("songs.db ще не підключено")
        return self.songs

    def _require_search(self) -> aiosqlite.Connection:
        if self.search is None:
            raise RuntimeError("search_results.db ще не підключено")
        return self.search

    @staticmethod
    async def _table_exists(conn: aiosqlite.Connection, table_name: str) -> bool:
        cursor = await conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
            (table_name,),
        )
        return await cursor.fetchone() is not None

    @staticmethod
    async def _table_info(
        conn: aiosqlite.Connection,
        table_name: str,
    ) -> list[aiosqlite.Row]:
        cursor = await conn.execute(f"PRAGMA table_info({table_name})")
        return list(await cursor.fetchall())

    async def _migrate_users(self) -> None:
        conn = self._require_users()
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                username TEXT,
                phone TEXT,
                location TEXT
            )
            """
        )

        columns = {row[1] for row in await self._table_info(conn, "users")}
        for name, sql_type in (
            ("username", "TEXT"),
            ("phone", "TEXT"),
            ("location", "TEXT"),
        ):
            if name not in columns:
                await conn.execute(f"ALTER TABLE users ADD COLUMN {name} {sql_type}")
                logger.info("Міграція users: додано стовпець %s", name)

        await conn.commit()

    async def _migrate_security(self) -> None:
        conn = self._require_users()
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS security_user_state (
                user_id INTEGER PRIMARY KEY,
                strikes INTEGER NOT NULL DEFAULT 0,
                last_strike_at TEXT,
                auto_ban_level INTEGER NOT NULL DEFAULT 0,
                ban_until TEXT,
                ban_reason TEXT,
                permanent_ban INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            )
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS security_allowlist (
                user_id INTEGER PRIMARY KEY,
                added_at TEXT NOT NULL,
                added_by INTEGER
            )
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS security_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_security_ban_until
            ON security_user_state(ban_until)
            """
        )
        await conn.commit()

    async def load_security_user_states(self) -> list[SecurityUserStateRecord]:
        conn = self._require_users()
        cursor = await conn.execute(
            """
            SELECT user_id, strikes, last_strike_at, auto_ban_level,
                   ban_until, ban_reason, permanent_ban, updated_at
            FROM security_user_state
            """
        )
        rows = await cursor.fetchall()
        return [
            SecurityUserStateRecord(
                user_id=int(row["user_id"]),
                strikes=int(row["strikes"] or 0),
                last_strike_at=row["last_strike_at"],
                auto_ban_level=int(row["auto_ban_level"] or 0),
                ban_until=row["ban_until"],
                ban_reason=row["ban_reason"],
                permanent_ban=bool(row["permanent_ban"]),
                updated_at=str(row["updated_at"] or ""),
            )
            for row in rows
        ]

    async def save_security_user_state(
        self,
        *,
        user_id: int,
        strikes: int,
        last_strike_at: Optional[str],
        auto_ban_level: int,
        ban_until: Optional[str],
        ban_reason: Optional[str],
        permanent_ban: bool,
    ) -> None:
        conn = self._require_users()
        now = datetime.now(timezone.utc).isoformat()
        await conn.execute(
            """
            INSERT INTO security_user_state(
                user_id, strikes, last_strike_at, auto_ban_level,
                ban_until, ban_reason, permanent_ban, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                strikes = excluded.strikes,
                last_strike_at = excluded.last_strike_at,
                auto_ban_level = excluded.auto_ban_level,
                ban_until = excluded.ban_until,
                ban_reason = excluded.ban_reason,
                permanent_ban = excluded.permanent_ban,
                updated_at = excluded.updated_at
            """,
            (
                user_id,
                max(0, int(strikes)),
                last_strike_at,
                max(0, int(auto_ban_level)),
                ban_until,
                ban_reason,
                1 if permanent_ban else 0,
                now,
            ),
        )
        await conn.commit()

    async def load_security_allowlist(self) -> set[int]:
        conn = self._require_users()
        cursor = await conn.execute("SELECT user_id FROM security_allowlist")
        return {int(row[0]) for row in await cursor.fetchall()}

    async def set_security_allowlist(
        self,
        *,
        user_id: int,
        allowed: bool,
        added_by: Optional[int] = None,
    ) -> None:
        conn = self._require_users()
        if allowed:
            await conn.execute(
                """
                INSERT INTO security_allowlist(user_id, added_at, added_by)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    added_at = excluded.added_at,
                    added_by = excluded.added_by
                """,
                (user_id, datetime.now(timezone.utc).isoformat(), added_by),
            )
        else:
            await conn.execute(
                "DELETE FROM security_allowlist WHERE user_id = ?",
                (user_id,),
            )
        await conn.commit()

    async def get_security_setting(self, key: str) -> Optional[str]:
        conn = self._require_users()
        cursor = await conn.execute(
            "SELECT value FROM security_settings WHERE key = ? LIMIT 1",
            (key,),
        )
        row = await cursor.fetchone()
        return str(row[0]) if row is not None else None

    async def set_security_setting(self, key: str, value: str) -> None:
        conn = self._require_users()
        await conn.execute(
            """
            INSERT INTO security_settings(key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            """,
            (key, value, datetime.now(timezone.utc).isoformat()),
        )
        await conn.commit()

    async def _migrate_downloads(self) -> None:
        conn = self._require_songs()
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS downloads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                video_id TEXT,
                title TEXT,
                file_path TEXT,
                file_id TEXT,
                sent_date TEXT,
                media_type TEXT,
                created_at TEXT,
                last_used_at TEXT,
                local_file_created_at TEXT
            )
            """
        )

        columns = {row[1] for row in await self._table_info(conn, "downloads")}
        additions = (
            ("user_id", "INTEGER"),
            ("video_id", "TEXT"),
            ("title", "TEXT"),
            ("file_path", "TEXT"),
            ("file_id", "TEXT"),
            ("sent_date", "TEXT"),
            ("media_type", "TEXT"),
            ("created_at", "TEXT"),
            ("last_used_at", "TEXT"),
            ("local_file_created_at", "TEXT"),
        )
        for name, sql_type in additions:
            if name not in columns:
                await conn.execute(f"ALTER TABLE downloads ADD COLUMN {name} {sql_type}")
                logger.info("Міграція downloads: додано стовпець %s", name)

        # Старі записи без media_type визначаємо лише тоді, коли це безпечно
        # можна зробити за розширенням локального файла.
        await conn.execute(
            """
            UPDATE downloads
            SET media_type = 'audio'
            WHERE (media_type IS NULL OR media_type = '')
              AND lower(COALESCE(file_path, '')) LIKE '%.mp3'
            """
        )
        await conn.execute(
            """
            UPDATE downloads
            SET media_type = 'video'
            WHERE (media_type IS NULL OR media_type = '')
              AND lower(COALESCE(file_path, '')) LIKE '%.mp4'
            """
        )

        # Щоб додати безпечну унікальність, старі дублікати не видаляємо:
        # найновіший запис лишається активним, старі виключаємо з cache-key,
        # встановивши media_type = NULL.
        cursor = await conn.execute(
            """
            SELECT video_id, media_type
            FROM downloads
            WHERE media_type IN ('audio', 'video')
            GROUP BY video_id, media_type
            HAVING COUNT(*) > 1
            """
        )
        duplicate_groups = await cursor.fetchall()
        for row in duplicate_groups:
            video_id = row[0]
            media_type = row[1]
            ids_cursor = await conn.execute(
                """
                SELECT id
                FROM downloads
                WHERE video_id = ? AND media_type = ?
                ORDER BY id DESC
                """,
                (video_id, media_type),
            )
            ids = [int(item[0]) for item in await ids_cursor.fetchall()]
            for old_id in ids[1:]:
                await conn.execute(
                    "UPDATE downloads SET media_type = NULL WHERE id = ?",
                    (old_id,),
                )
            logger.warning(
                "Міграція downloads: для video_id=%s media_type=%s "
                "залишено найновіший cache-запис, старі дані не видалено",
                video_id,
                media_type,
            )

        now = datetime.now(timezone.utc).isoformat()
        await conn.execute(
            """
            UPDATE downloads
            SET created_at = COALESCE(created_at, sent_date, ?),
                last_used_at = COALESCE(last_used_at, sent_date, created_at, ?),
                local_file_created_at = CASE
                    WHEN file_path IS NOT NULL AND file_path != ''
                    THEN COALESCE(local_file_created_at, sent_date, created_at, ?)
                    ELSE NULL
                END
            """,
            (now, now, now),
        )

        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_downloads_video_media
            ON downloads(video_id, media_type)
            """
        )
        await conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_downloads_video_media
            ON downloads(video_id, media_type)
            WHERE media_type IN ('audio', 'video')
            """
        )
        await conn.commit()

    async def _migrate_download_parts(self) -> None:
        conn = self._require_songs()
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS download_parts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                video_id TEXT NOT NULL,
                media_type TEXT NOT NULL,
                part_number INTEGER NOT NULL,
                total_parts INTEGER NOT NULL,
                file_id TEXT NOT NULL,
                file_size INTEGER,
                created_at TEXT NOT NULL,
                cache_version INTEGER NOT NULL DEFAULT 1,
                CHECK (media_type IN ('audio', 'video')),
                CHECK (part_number >= 1),
                CHECK (total_parts >= 1),
                CHECK (part_number <= total_parts),
                UNIQUE (video_id, media_type, part_number)
            )
            """
        )
        part_columns = {row[1] for row in await self._table_info(conn, "download_parts")}
        if "cache_version" not in part_columns:
            await conn.execute(
                "ALTER TABLE download_parts ADD COLUMN cache_version INTEGER NOT NULL DEFAULT 1"
            )
            logger.info("Міграція download_parts: додано cache_version")

        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_download_parts_video_media
            ON download_parts(video_id, media_type, part_number)
            """
        )
        await conn.commit()

    async def _validate_download_parts_integrity(self) -> None:
        """Перевіряє всі кеші сегментів при старті та видаляє неповні.

        Перевірка однакова для audio і video: нумерація має бути без пропусків
        1..N, кількість записів має дорівнювати N, total_parts має збігатися
        в усіх рядках, а кожен сегмент повинен мати непорожній Telegram file_id.
        """
        conn = self._require_songs()
        cursor = await conn.execute(
            """
            SELECT DISTINCT video_id, media_type
            FROM download_parts
            WHERE media_type IN ('audio', 'video')
            ORDER BY media_type, video_id
            """
        )
        groups = await cursor.fetchall()

        valid_groups = 0
        removed_groups = 0
        for row in groups:
            video_id = str(row["video_id"])
            media_type = str(row["media_type"])
            parts = await self.get_cached_parts(video_id, media_type)
            if parts:
                valid_groups += 1
            else:
                # Група була в початковому SELECT, тому порожній результат тут
                # означає, що get_cached_parts виявив пошкодження та очистив її.
                removed_groups += 1

        logger.info(
            "Перевірка цілісності cache сегментів: всього=%s, коректних=%s, "
            "очищено=%s (audio+video)",
            len(groups),
            valid_groups,
            removed_groups,
        )

    async def _create_search_table(
        self,
        conn: aiosqlite.Connection,
        table_name: str,
    ) -> None:
        await conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {table_name} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                query TEXT NOT NULL,
                results TEXT NOT NULL,
                page INTEGER NOT NULL DEFAULT 0,
                message_id INTEGER,
                timestamp TEXT NOT NULL,
                playlist_title TEXT,
                playlist_url TEXT,
                playlist_id TEXT,
                search_key TEXT
            )
            """
        )

    async def _migrate_search_results(self) -> None:
        conn = self._require_search()

        if not await self._table_exists(conn, "search_results"):
            await self._create_search_table(conn, "search_results")
        else:
            info = await self._table_info(conn, "search_results")
            id_is_primary = any(row[1] == "id" and int(row[5]) == 1 for row in info)

            if not id_is_primary:
                logger.info(
                    "Міграція search_results: перебудова таблиці для прив'язки стану до message_id"
                )
                await conn.execute("DROP TABLE IF EXISTS search_results_new")
                await self._create_search_table(conn, "search_results_new")

                cursor = await conn.execute("SELECT * FROM search_results")
                old_rows = await cursor.fetchall()
                old_columns = [description[0] for description in cursor.description or []]

                copy_rows: list[tuple[Any, ...]] = []
                now = datetime.now(timezone.utc).isoformat()
                for row in old_rows:
                    data = {name: row[index] for index, name in enumerate(old_columns)}
                    copy_rows.append(
                        (
                            int(data.get("user_id") or 0),
                            str(data.get("query") or ""),
                            str(data.get("results") or "[]"),
                            int(data.get("page") or 0),
                            data.get("message_id"),
                            str(data.get("timestamp") or now),
                            data.get("playlist_title"),
                            data.get("playlist_url"),
                            data.get("playlist_id"),
                            data.get("search_key"),
                        )
                    )

                if copy_rows:
                    await conn.executemany(
                        """
                        INSERT INTO search_results_new (
                            user_id,
                            query,
                            results,
                            page,
                            message_id,
                            timestamp,
                            playlist_title,
                            playlist_url,
                            playlist_id,
                            search_key
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        copy_rows,
                    )

                await conn.execute("ALTER TABLE search_results RENAME TO search_results_legacy")
                await conn.execute("ALTER TABLE search_results_new RENAME TO search_results")
                await conn.execute("DROP TABLE search_results_legacy")
            else:
                columns = {row[1] for row in info}
                additions = (
                    ("playlist_title", "TEXT"),
                    ("playlist_url", "TEXT"),
                    ("playlist_id", "TEXT"),
                    ("search_key", "TEXT"),
                )
                for name, sql_type in additions:
                    if name not in columns:
                        await conn.execute(
                            f"ALTER TABLE search_results ADD COLUMN {name} {sql_type}"
                        )
                        logger.info("Міграція search_results: додано %s", name)

        # Якщо у старій БД випадково є дубль message_id, лишаємо найновіший
        # прив'язаним до повідомлення, а старому прибираємо message_id.
        cursor = await conn.execute(
            """
            SELECT user_id, message_id
            FROM search_results
            WHERE message_id IS NOT NULL
            GROUP BY user_id, message_id
            HAVING COUNT(*) > 1
            """
        )
        duplicates = await cursor.fetchall()
        for row in duplicates:
            user_id = int(row[0])
            message_id = int(row[1])
            ids_cursor = await conn.execute(
                """
                SELECT id
                FROM search_results
                WHERE user_id = ? AND message_id = ?
                ORDER BY id DESC
                """,
                (user_id, message_id),
            )
            ids = [int(item[0]) for item in await ids_cursor.fetchall()]
            for old_id in ids[1:]:
                await conn.execute(
                    "UPDATE search_results SET message_id = NULL WHERE id = ?",
                    (old_id,),
                )

        await conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_search_message
            ON search_results(user_id, message_id)
            WHERE message_id IS NOT NULL
            """
        )
        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_search_timestamp
            ON search_results(timestamp)
            """
        )
        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_search_user_query
            ON search_results(user_id, query)
            """
        )
        await conn.commit()

    async def _migrate_table_pagination(self) -> None:
        conn = self._require_search()
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS table_pagination (
                user_id INTEGER,
                table_name TEXT,
                page INTEGER,
                message_id INTEGER,
                timestamp TEXT,
                PRIMARY KEY (user_id, table_name)
            )
            """
        )
        await conn.commit()

    async def save_user(self, user_id: int, username: Optional[str]) -> None:
        conn = self._require_users()
        await conn.execute(
            """
            INSERT INTO users(id, username)
            VALUES (?, ?)
            ON CONFLICT(id) DO UPDATE SET username = excluded.username
            """,
            (user_id, username),
        )
        await conn.commit()

    async def save_contact(self, user_id: int, phone: str) -> None:
        conn = self._require_users()
        await conn.execute(
            """
            INSERT INTO users(id, phone)
            VALUES (?, ?)
            ON CONFLICT(id) DO UPDATE SET phone = excluded.phone
            """,
            (user_id, phone),
        )
        await conn.commit()

    async def save_location(self, user_id: int, latitude: float, longitude: float) -> None:
        conn = self._require_users()
        location = f"{latitude},{longitude}"
        await conn.execute(
            """
            INSERT INTO users(id, location)
            VALUES (?, ?)
            ON CONFLICT(id) DO UPDATE SET location = excluded.location
            """,
            (user_id, location),
        )
        await conn.commit()

    async def get_cached_media(self, video_id: str, media_type: str) -> Optional[CachedMedia]:
        conn = self._require_songs()
        cursor = await conn.execute(
            """
            SELECT id, user_id, video_id, title, file_path, file_id, sent_date, media_type
            FROM downloads
            WHERE video_id = ? AND media_type = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (video_id, media_type),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return CachedMedia(
            id=int(row["id"]),
            user_id=row["user_id"],
            video_id=str(row["video_id"]),
            title=str(row["title"] or "Невідомо"),
            file_path=row["file_path"],
            file_id=row["file_id"],
            sent_date=row["sent_date"],
            media_type=str(row["media_type"]),
        )

    async def get_cached_parts(
        self,
        video_id: str,
        media_type: str,
    ) -> list[CachedMediaPart]:
        if media_type not in {"audio", "video"}:
            raise ValueError("media_type має бути audio або video")

        conn = self._require_songs()
        cursor = await conn.execute(
            """
            SELECT id, video_id, media_type, part_number, total_parts,
                   file_id, file_size, created_at, cache_version
            FROM download_parts
            WHERE video_id = ? AND media_type = ?
            ORDER BY part_number ASC
            """,
            (video_id, media_type),
        )
        rows = await cursor.fetchall()
        if not rows:
            return []

        parts = [
            CachedMediaPart(
                id=int(row["id"]),
                video_id=str(row["video_id"]),
                media_type=str(row["media_type"]),
                part_number=int(row["part_number"]),
                total_parts=int(row["total_parts"]),
                file_id=str(row["file_id"]),
                file_size=(
                    int(row["file_size"])
                    if row["file_size"] is not None
                    else None
                ),
                created_at=str(row["created_at"]),
                cache_version=int(row["cache_version"] or 1),
            )
            for row in rows
        ]

        total_parts = parts[0].total_parts
        expected_numbers = list(range(1, total_parts + 1))
        actual_numbers = [part.part_number for part in parts]
        structure_ok = (
            total_parts >= 1
            and len(parts) == total_parts
            and actual_numbers == expected_numbers
            and all(part.total_parts == total_parts for part in parts)
            and all(bool(part.file_id) for part in parts)
        )
        version_ok = all(
            part.cache_version == self.config.SEGMENT_CACHE_VERSION
            for part in parts
        )
        if structure_ok and version_ok:
            return parts

        if structure_ok and not version_ok:
            versions = sorted({part.cache_version for part in parts})
            logger.info(
                "Застаріла версія cache сегментів video_id=%s media_type=%s: "
                "cache_versions=%s, current=%s; cache буде перебудовано",
                video_id,
                media_type,
                versions,
                self.config.SEGMENT_CACHE_VERSION,
            )
        else:
            logger.warning(
                "Пошкоджений cache сегментів video_id=%s media_type=%s: "
                "очікувалось=%s, знайдено=%s; cache буде очищено",
                video_id,
                media_type,
                total_parts,
                actual_numbers,
            )
        await self.delete_cached_parts(video_id, media_type)
        return []

    async def replace_cached_parts(
        self,
        *,
        video_id: str,
        media_type: str,
        parts: Sequence[tuple[int, int, str, Optional[int]]],
    ) -> None:
        if media_type not in {"audio", "video"}:
            raise ValueError("media_type має бути audio або video")

        conn = self._require_songs()
        await conn.execute(
            "DELETE FROM download_parts WHERE video_id = ? AND media_type = ?",
            (video_id, media_type),
        )

        if parts:
            ordered = sorted(parts, key=lambda item: item[0])
            total_parts = ordered[0][1]
            expected_numbers = list(range(1, total_parts + 1))
            actual_numbers = [int(item[0]) for item in ordered]
            if (
                total_parts < 1
                or len(ordered) != total_parts
                or actual_numbers != expected_numbers
                or any(int(item[1]) != total_parts for item in ordered)
                or any(not str(item[2]).strip() for item in ordered)
            ):
                await conn.rollback()
                raise ValueError(
                    "Некоректний набір Telegram file_id для сегментованого файла"
                )

            now = datetime.now(timezone.utc).isoformat()
            await conn.executemany(
                """
                INSERT INTO download_parts(
                    video_id, media_type, part_number, total_parts,
                    file_id, file_size, created_at, cache_version
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        video_id,
                        media_type,
                        int(part_number),
                        int(part_total),
                        str(file_id),
                        int(file_size) if file_size is not None else None,
                        now,
                        self.config.SEGMENT_CACHE_VERSION,
                    )
                    for part_number, part_total, file_id, file_size in ordered
                ],
            )

        await conn.commit()

    async def delete_cached_parts(self, video_id: str, media_type: str) -> None:
        conn = self._require_songs()
        await conn.execute(
            "DELETE FROM download_parts WHERE video_id = ? AND media_type = ?",
            (video_id, media_type),
        )
        await conn.commit()

    async def save_download(
        self,
        *,
        user_id: int,
        video_id: str,
        title: str,
        media_type: str,
        file_path: Optional[str],
        file_id: Optional[str],
        refresh_local_file_age: bool = False,
    ) -> None:
        if media_type not in {"audio", "video"}:
            raise ValueError("media_type має бути audio або video")

        conn = self._require_songs()
        now = datetime.now(timezone.utc).isoformat()
        cursor = await conn.execute(
            """
            UPDATE downloads
            SET user_id = ?,
                title = ?,
                file_path = ?,
                file_id = ?,
                sent_date = ?,
                created_at = COALESCE(created_at, ?),
                last_used_at = ?,
                local_file_created_at = CASE
                    WHEN ? = 1 THEN ?
                    WHEN ? IS NULL OR ? = '' THEN NULL
                    ELSE local_file_created_at
                END
            WHERE video_id = ? AND media_type = ?
            """,
            (
                user_id, title, file_path, file_id, now, now, now,
                1 if refresh_local_file_age else 0,
                now,
                file_path, file_path,
                video_id, media_type,
            ),
        )

        if cursor.rowcount == 0:
            local_created = now if file_path else None
            await conn.execute(
                """
                INSERT INTO downloads(
                    user_id, video_id, title, file_path, file_id, sent_date, media_type,
                    created_at, last_used_at, local_file_created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id, video_id, title, file_path, file_id, now, media_type,
                    now, now, local_created,
                ),
            )

        await conn.commit()

    async def invalidate_file_id(self, video_id: str, media_type: str) -> None:
        conn = self._require_songs()
        await conn.execute(
            """
            UPDATE downloads
            SET file_id = NULL
            WHERE video_id = ? AND media_type = ?
            """,
            (video_id, media_type),
        )
        await conn.commit()

    async def clear_file_path(self, video_id: str, media_type: str) -> None:
        conn = self._require_songs()
        await conn.execute(
            """
            UPDATE downloads
            SET file_path = NULL, local_file_created_at = NULL
            WHERE video_id = ? AND media_type = ?
            """,
            (video_id, media_type),
        )
        await conn.commit()

    async def get_media_identity_by_file_path(
        self,
        file_path: str,
    ) -> Optional[tuple[str, str]]:
        conn = self._require_songs()
        cursor = await conn.execute(
            """
            SELECT video_id, media_type
            FROM downloads
            WHERE file_path = ?
              AND media_type IN ('audio', 'video')
            ORDER BY id DESC
            LIMIT 1
            """,
            (file_path,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return str(row[0]), str(row[1])

    async def clear_file_path_by_path(self, file_path: str) -> None:
        conn = self._require_songs()
        await conn.execute(
            "UPDATE downloads SET file_path = NULL, local_file_created_at = NULL WHERE file_path = ?",
            (file_path,),
        )
        await conn.commit()

    async def get_expired_local_files(self, cutoff_iso: str) -> list[tuple[str, str, str]]:
        conn = self._require_songs()
        cursor = await conn.execute(
            """
            SELECT video_id, media_type, file_path
            FROM downloads
            WHERE file_path IS NOT NULL
              AND media_type IN ('audio', 'video')
              AND datetime(COALESCE(local_file_created_at, sent_date, created_at)) < datetime(?)
            """,
            (cutoff_iso,),
        )
        return [
            (str(row[0]), str(row[1]), str(row[2]))
            for row in await cursor.fetchall()
        ]

    async def create_search_results(
        self,
        *,
        user_id: int,
        query: str,
        results: Sequence[dict[str, Any]],
        playlist_title: Optional[str] = None,
        playlist_url: Optional[str] = None,
        playlist_id: Optional[str] = None,
        search_key: Optional[str] = None,
    ) -> int:
        conn = self._require_search()
        now = datetime.now(timezone.utc).isoformat()
        result_list = [dict(item) for item in results if isinstance(item, dict)]
        cursor = await conn.execute(
            """
            INSERT INTO search_results(
                user_id,
                query,
                results,
                page,
                message_id,
                timestamp,
                playlist_title,
                playlist_url,
                playlist_id,
                search_key
            )
            VALUES (?, ?, ?, 0, NULL, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                query,
                json.dumps(result_list, ensure_ascii=False),
                now,
                playlist_title,
                playlist_url,
                playlist_id,
                search_key,
            ),
        )
        search_id = int(cursor.lastrowid)

        # Security v3: один користувач не може безмежно накопичувати великі
        # search sessions у SQLite. Лишаємо тільки найновіші N записів.
        limit = max(1, self.config.MAX_SEARCH_SESSIONS_PER_USER)
        ids_cursor = await conn.execute(
            """
            SELECT id
            FROM search_results
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT -1 OFFSET ?
            """,
            (user_id, limit),
        )
        stale_ids = [int(row[0]) for row in await ids_cursor.fetchall()]
        if stale_ids:
            placeholders = ",".join("?" for _ in stale_ids)
            await conn.execute(
                f"DELETE FROM search_results WHERE id IN ({placeholders})",
                stale_ids,
            )
            for stale_id in stale_ids:
                self._search_results_memory.pop(stale_id, None)

        await conn.commit()
        self._remember_search_results(search_id, result_list)
        return search_id

    async def bind_search_message(self, search_id: int, message_id: int) -> None:
        conn = self._require_search()
        await conn.execute(
            """
            UPDATE search_results
            SET message_id = ?, timestamp = ?
            WHERE id = ?
            """,
            (message_id, datetime.now(timezone.utc).isoformat(), search_id),
        )
        await conn.commit()

    async def update_search_page(self, search_id: int, page: int) -> None:
        conn = self._require_search()
        await conn.execute(
            """
            UPDATE search_results
            SET page = ?, timestamp = ?
            WHERE id = ?
            """,
            (page, datetime.now(timezone.utc).isoformat(), search_id),
        )
        await conn.commit()

    async def get_search_by_id(self, search_id: int) -> Optional[SearchRecord]:
        conn = self._require_search()
        cursor = await conn.execute(
            "SELECT * FROM search_results WHERE id = ? LIMIT 1",
            (search_id,),
        )
        row = await cursor.fetchone()
        return self._row_to_search_record(row)

    async def get_search_by_message(
        self,
        user_id: int,
        message_id: int,
    ) -> Optional[SearchRecord]:
        conn = self._require_search()
        cursor = await conn.execute(
            """
            SELECT *
            FROM search_results
            WHERE user_id = ? AND message_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (user_id, message_id),
        )
        row = await cursor.fetchone()
        return self._row_to_search_record(row)

    def _remember_search_results(
        self,
        search_id: int,
        results: list[dict[str, Any]],
    ) -> None:
        self._search_results_memory[search_id] = results
        self._search_results_memory.move_to_end(search_id)
        while len(self._search_results_memory) > self.config.EFFECTIVE_SEARCH_MEMORY_CACHE_SIZE:
            self._search_results_memory.popitem(last=False)

    def _row_to_search_record(
        self,
        row: Optional[aiosqlite.Row],
    ) -> Optional[SearchRecord]:
        if row is None:
            return None

        search_id = int(row["id"])
        cached_results = self._search_results_memory.get(search_id)
        if cached_results is not None:
            self._search_results_memory.move_to_end(search_id)
            normalized_results = cached_results
        else:
            raw_results = row["results"] or "[]"
            try:
                parsed = json.loads(raw_results)
            except json.JSONDecodeError as exc:
                raise ValueError("Пошкоджений JSON у search_results") from exc

            if not isinstance(parsed, list):
                raise ValueError("results у search_results має бути списком")

            normalized_results = [item for item in parsed if isinstance(item, dict)]
            self._remember_search_results(search_id, normalized_results)

        return SearchRecord(
            id=search_id,
            user_id=int(row["user_id"]),
            query=str(row["query"]),
            results=normalized_results,
            page=int(row["page"] or 0),
            message_id=row["message_id"],
            timestamp=str(row["timestamp"]),
            playlist_title=row["playlist_title"],
            playlist_url=row["playlist_url"],
            playlist_id=row["playlist_id"],
            search_key=row["search_key"],
        )

    async def cleanup_old_search_results(self, ttl_hours: int) -> int:
        conn = self._require_search()
        modifier = f"-{int(ttl_hours)} hours"
        cursor = await conn.execute(
            """
            DELETE FROM search_results
            WHERE datetime(timestamp) < datetime('now', ?)
            """,
            (modifier,),
        )
        await conn.execute(
            """
            DELETE FROM table_pagination
            WHERE timestamp IS NOT NULL
              AND datetime(timestamp) < datetime('now', ?)
            """,
            (modifier,),
        )
        await conn.commit()
        deleted = max(cursor.rowcount, 0)
        if deleted:
            self._search_results_memory.clear()
        return deleted

    async def save_pagination(
        self,
        *,
        user_id: int,
        table_name: str,
        page: int,
        message_id: Optional[int],
    ) -> None:
        conn = self._require_search()
        await conn.execute(
            """
            INSERT INTO table_pagination(user_id, table_name, page, message_id, timestamp)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id, table_name) DO UPDATE SET
                page = excluded.page,
                message_id = excluded.message_id,
                timestamp = excluded.timestamp
            """,
            (
                user_id,
                table_name,
                page,
                message_id,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        await conn.commit()

    async def load_pagination(
        self,
        user_id: int,
        table_name: str,
    ) -> Optional[PaginationRecord]:
        conn = self._require_search()
        cursor = await conn.execute(
            """
            SELECT user_id, table_name, page, message_id, timestamp
            FROM table_pagination
            WHERE user_id = ? AND table_name = ?
            LIMIT 1
            """,
            (user_id, table_name),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return PaginationRecord(
            user_id=int(row["user_id"]),
            table_name=str(row["table_name"]),
            page=int(row["page"] or 0),
            message_id=row["message_id"],
            timestamp=str(row["timestamp"] or ""),
        )

    async def export_admin_table(self, table_name: str) -> tuple[list[str], list[Sequence[Any]]]:
        if table_name not in self.ADMIN_TABLES:
            raise ValueError("Таблиця не входить до дозволеного whitelist")

        if table_name == "users":
            conn = self._require_users()
            headers = ["id", "username", "phone", "location"]
            cursor = await conn.execute(
                "SELECT id, username, phone, location FROM users ORDER BY id"
            )
            rows = await cursor.fetchall()
            return headers, [tuple(row) for row in rows]

        if table_name == "downloads":
            conn = self._require_songs()
            headers = [
                "id",
                "user_id",
                "video_id",
                "title",
                "file_path",
                "file_id",
                "sent_date",
                "media_type",
            ]
            cursor = await conn.execute(
                """
                SELECT id, user_id, video_id, title, file_path, file_id, sent_date, media_type
                FROM downloads
                ORDER BY id
                """
            )
            rows = await cursor.fetchall()
            return headers, [tuple(row) for row in rows]

        conn = self._require_search()
        headers = [
            "id",
            "user_id",
            "query",
            "page",
            "message_id",
            "timestamp",
            "playlist_title",
            "playlist_url",
            "playlist_id",
        ]
        cursor = await conn.execute(
            """
            SELECT id, user_id, query, page, message_id, timestamp,
                   playlist_title, playlist_url, playlist_id
            FROM search_results
            ORDER BY id
            """
        )
        rows = await cursor.fetchall()
        return headers, [tuple(row) for row in rows]
