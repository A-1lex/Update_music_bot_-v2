from __future__ import annotations

import asyncio
import copy
import errno
import importlib.metadata
import logging
import os
import re
import shutil
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

import yt_dlp

from config import Config
from utils.errors import DownloadCancelledError


logger = logging.getLogger(__name__)
_CANCEL_MARKER = "Завантаження скасовано користувачем"
_STORAGE_LIMIT_MARKER = "Перевищено зарезервований ліміт завантаження"
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_PLAYLIST_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


@dataclass(slots=True)
class RuntimeInfo:
    name: Optional[str]
    path: Optional[Path]
    version: Optional[str]
    ejs_installed: bool
    ejs_version: Optional[str]


@dataclass(slots=True)
class YoutubeReference:
    is_youtube: bool
    video_id: Optional[str]
    playlist_id: Optional[str]
    is_mix: bool


@dataclass(slots=True)
class YoutubeDownloadResult:
    video_id: str
    title: str
    final_path: Path


class YouTubeService:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.runtime = RuntimeInfo(
            name=None,
            path=None,
            version=None,
            ejs_installed=False,
            ejs_version=None,
        )
        self._extract_semaphore = asyncio.Semaphore(config.MAX_CONCURRENT_YOUTUBE)

    async def initialize(self) -> RuntimeInfo:
        runtime_name, runtime_path, runtime_version = await self._find_js_runtime()

        try:
            ejs_version = importlib.metadata.version("yt-dlp-ejs")
            ejs_installed = bool(ejs_version)
        except importlib.metadata.PackageNotFoundError:
            ejs_version = None
            ejs_installed = False

        self.runtime = RuntimeInfo(
            name=runtime_name,
            path=runtime_path,
            version=runtime_version,
            ejs_installed=ejs_installed,
            ejs_version=ejs_version,
        )

        if runtime_name and runtime_path:
            logger.info(
                "YouTube JS runtime: %s %s (%s)",
                runtime_name,
                runtime_version or "",
                runtime_path,
            )
        else:
            logger.warning(
                "YouTube JS runtime не знайдено. Рекомендовано Deno >= 2.3 або Node >= 22."
            )

        if ejs_installed:
            logger.info("yt-dlp-ejs: встановлено (%s)", ejs_version)
        else:
            logger.warning(
                "yt-dlp-ejs не знайдено; yt-dlp використовуватиме remote_components=ejs:github"
            )

        return self.runtime

    async def _find_js_runtime(self) -> tuple[Optional[str], Optional[Path], Optional[str]]:
        candidates: list[tuple[str, Path, int]] = []

        deno = shutil.which("deno")
        if deno:
            candidates.append(("deno", Path(deno), 2))

        for path in (
            Path.home() / ".deno" / "bin" / "deno.exe",
            Path.home() / ".deno" / "bin" / "deno",
        ):
            candidates.append(("deno", path, 2))

        node = shutil.which("node")
        if node:
            candidates.append(("node", Path(node), 22))
        candidates.append(("node", Path(r"C:\Program Files\nodejs\node.exe"), 22))

        seen: set[Path] = set()
        for name, path, min_major in candidates:
            path = path.expanduser()
            if path in seen or not path.is_file():
                continue
            seen.add(path)

            version = await self._read_runtime_version(path)
            if not version:
                continue

            match = re.search(r"(\d+)(?:\.(\d+))?", version)
            major = int(match.group(1)) if match else 0
            minor = int(match.group(2) or 0) if match else 0

            if name == "deno":
                if (major, minor) < (2, 3):
                    logger.warning("Знайдено застарілий Deno %s: %s", version, path)
                    continue
            elif major < min_major:
                logger.warning("Знайдено застарілий Node.js %s: %s", version, path)
                continue

            return name, path.resolve(), version

        return None, None, None

    @staticmethod
    async def _read_runtime_version(path: Path) -> Optional[str]:
        try:
            process = await asyncio.create_subprocess_exec(
                str(path),
                "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=5)
            if process.returncode != 0:
                return None
            return (stdout or stderr).decode("utf-8", errors="replace").strip().splitlines()[0]
        except (OSError, asyncio.TimeoutError, IndexError):
            return None

    def _base_opts(self, *, noplaylist: bool = True) -> dict[str, Any]:
        opts: dict[str, Any] = {
            "noplaylist": noplaylist,
            "ignoreerrors": False,
            "quiet": True,
            "no_warnings": False,
            "retries": 5,
            "fragment_retries": 5,
            "file_access_retries": 3,
            "extractor_retries": 3,
            "socket_timeout": self.config.YTDLP_SOCKET_TIMEOUT,
        }

        if self.config.FFMPEG_EXE is not None:
            opts["ffmpeg_location"] = str(self.config.FFMPEG_EXE)

        if self.runtime.name and self.runtime.path:
            # ВАЖЛИВО: актуальний yt-dlp очікує key='path', а не 'args'.
            opts["js_runtimes"] = {
                self.runtime.name: {"path": str(self.runtime.path)}
            }

        if not self.runtime.ejs_installed:
            opts["remote_components"] = {"ejs:github"}

        return opts

    @staticmethod
    def _is_403(error: Exception) -> bool:
        text = str(error).lower()
        return any(
            marker in text
            for marker in (
                "http error 403",
                "403 forbidden",
                "http status 403",
            )
        )

    @staticmethod
    def _extract_sync(
        url: str,
        options: dict[str, Any],
        download: bool,
    ) -> dict[str, Any]:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=download)
        if not info:
            raise yt_dlp.DownloadError("YouTube не повернув інформацію")
        return info

    async def _extract_with_fallback(
        self,
        url: str,
        options: dict[str, Any],
        *,
        download: bool,
    ) -> dict[str, Any]:
        async with self._extract_semaphore:
            try:
                return await asyncio.to_thread(self._extract_sync, url, options, download)
            except DownloadCancelledError:
                raise
            except yt_dlp.DownloadError as exc:
                lowered = str(exc).lower()
                if _CANCEL_MARKER.lower() in lowered:
                    raise DownloadCancelledError(_CANCEL_MARKER) from exc
                if _STORAGE_LIMIT_MARKER.lower() in lowered:
                    raise OSError(errno.ENOSPC, _STORAGE_LIMIT_MARKER) from exc
                if not self._is_403(exc):
                    raise

                fallback = copy.deepcopy(options)
                fallback["cachedir"] = False
                fallback["force_ipv4"] = True
                fallback["extractor_args"] = {
                    "youtube": {
                        "player_client": ["android", "web"],
                    }
                }

                logger.warning("YouTube HTTP 403: запускаю безпечний fallback для %s", url)
                try:
                    return await asyncio.to_thread(
                        self._extract_sync,
                        url,
                        fallback,
                        download,
                    )
                except yt_dlp.DownloadError as fallback_exc:
                    lowered = str(fallback_exc).lower()
                    if _CANCEL_MARKER.lower() in lowered:
                        raise DownloadCancelledError(_CANCEL_MARKER) from fallback_exc
                    if _STORAGE_LIMIT_MARKER.lower() in lowered:
                        raise OSError(errno.ENOSPC, _STORAGE_LIMIT_MARKER) from fallback_exc
                    raise

    @staticmethod
    def _normalize_entries(
        info: dict[str, Any],
        max_results: Optional[int],
    ) -> list[dict[str, Any]]:
        entries = info.get("entries") or []
        if max_results is not None:
            entries = entries[:max_results]

        result: list[dict[str, Any]] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            video_id = entry.get("id")
            if not video_id:
                continue
            result.append(
                {
                    "id": str(video_id),
                    "title": str(entry.get("title") or "Невідомо"),
                    "duration": entry.get("duration"),
                }
            )
        return result

    async def search(self, query: str) -> list[dict[str, Any]]:
        options = self._base_opts(noplaylist=True)
        options["extract_flat"] = True
        info = await self._extract_with_fallback(
            f"ytsearch{self.config.MAX_SEARCH_RESULTS}:{query}",
            options,
            download=False,
        )
        return self._normalize_entries(info, self.config.MAX_SEARCH_RESULTS)

    async def get_video_metadata(self, video_id: str) -> Optional[dict[str, Any]]:
        if not _VIDEO_ID_RE.fullmatch(video_id):
            return None

        options = self._base_opts(noplaylist=True)
        info = await self._extract_with_fallback(
            self.video_url(video_id),
            options,
            download=False,
        )
        resolved_id = str(info.get("id") or "")
        if not resolved_id:
            return None
        return {
            "id": resolved_id,
            "title": str(info.get("title") or "Невідомо"),
            "duration": info.get("duration"),
        }


    async def estimate_download_size(
        self,
        video_id: str,
        media_type: str,
    ) -> Optional[int]:
        """Повертає консервативну оцінку розміру майбутнього файла у байтах."""
        if media_type not in {"audio", "video"}:
            raise ValueError("media_type має бути audio або video")
        if not _VIDEO_ID_RE.fullmatch(video_id):
            return None

        options = self._base_opts(noplaylist=True)
        if media_type == "audio":
            options["format"] = "bestaudio/best"
        else:
            options.update(
                {
                    "format": (
                        "bestvideo[ext=mp4]+bestaudio[ext=m4a]"
                        "/best[ext=mp4]/best"
                    ),
                    "merge_output_format": "mp4",
                }
            )

        info = await self._extract_with_fallback(
            self.video_url(video_id),
            options,
            download=False,
        )

        def _size(item: Any) -> int:
            if not isinstance(item, dict):
                return 0
            for key in ("filesize", "filesize_approx"):
                value = item.get(key)
                try:
                    if value is not None and int(value) > 0:
                        return int(value)
                except (TypeError, ValueError):
                    pass
            return 0

        selected = info.get("requested_formats")
        if isinstance(selected, list) and selected:
            source_size = sum(_size(item) for item in selected)
        else:
            source_size = _size(info)

        duration = info.get("duration")
        try:
            duration_seconds = float(duration) if duration is not None else 0.0
        except (TypeError, ValueError):
            duration_seconds = 0.0

        if media_type == "audio" and duration_seconds > 0:
            # MP3 кодується приблизно в 192 кбіт/с. Додаємо запас на теги/контейнер.
            mp3_estimate = int(duration_seconds * 192_000 / 8 * 1.15)
            source_size = max(source_size, mp3_estimate)

        if source_size <= 0:
            return None

        # Мердж відео/аудіо та постобробка можуть трохи збільшити результат.
        return int(source_size * 1.20)

    async def get_playlist_metadata(self, playlist_id: str) -> dict[str, Any]:
        if not _PLAYLIST_ID_RE.fullmatch(playlist_id):
            raise ValueError("Некоректний ID плейлиста")

        playlist_url = self.playlist_url(playlist_id)
        options = self._base_opts(noplaylist=False)
        options.update(
            {
                "extract_flat": True,
                "playlistend": self.config.MAX_PLAYLIST_RESULTS,
                "ignoreerrors": True,
            }
        )
        info = await self._extract_with_fallback(
            playlist_url,
            options,
            download=False,
        )
        entries = self._normalize_entries(info, self.config.MAX_PLAYLIST_RESULTS)
        title = str(info.get("title") or "Добірка YouTube").strip()
        return {
            "title": title or "Добірка YouTube",
            "url": playlist_url,
            "playlist_id": playlist_id,
            "entries": entries,
        }

    async def download_media(
        self,
        *,
        video_id: str,
        media_type: str,
        work_dir: Path,
        cancel_event: threading.Event,
        max_source_size: Optional[int] = None,
    ) -> YoutubeDownloadResult:
        if media_type not in {"audio", "video"}:
            raise ValueError("media_type має бути audio або video")
        if not _VIDEO_ID_RE.fullmatch(video_id):
            raise ValueError("Некоректний YouTube video_id")

        work_dir.mkdir(parents=True, exist_ok=True)
        captured_paths: list[Path] = []
        downloaded_by_stream: dict[str, int] = {}

        def cancellation_hook(data: dict[str, Any]) -> None:
            if cancel_event.is_set():
                raise DownloadCancelledError(_CANCEL_MARKER)

            if max_source_size is None or max_source_size <= 0:
                return

            # yt-dlp може завантажувати video+audio окремими потоками.
            # downloaded_bytes у кожному потоці починається з нуля, тому
            # відстежуємо їх окремо та контролюємо сумарний обсяг.
            info_dict = data.get("info_dict") or {}
            stream_key = str(
                data.get("filename")
                or info_dict.get("filepath")
                or info_dict.get("format_id")
                or "stream"
            )
            try:
                downloaded = int(data.get("downloaded_bytes") or 0)
            except (TypeError, ValueError):
                downloaded = 0
            if downloaded > downloaded_by_stream.get(stream_key, 0):
                downloaded_by_stream[stream_key] = downloaded

            if sum(downloaded_by_stream.values()) > int(max_source_size):
                raise RuntimeError(_STORAGE_LIMIT_MARKER)

        def postprocessor_hook(data: dict[str, Any]) -> None:
            if cancel_event.is_set():
                raise DownloadCancelledError(_CANCEL_MARKER)
            if data.get("status") != "finished":
                return
            info_dict = data.get("info_dict") or {}
            candidate = info_dict.get("filepath") or info_dict.get("_filename")
            if candidate:
                captured_paths.append(Path(str(candidate)))

        options = self._base_opts(noplaylist=True)
        options.update(
            {
                "outtmpl": str(work_dir / "%(id)s.%(ext)s"),
                "progress_hooks": [cancellation_hook],
                "postprocessor_hooks": [postprocessor_hook],
            }
        )
        if max_source_size is not None and max_source_size > 0:
            options["max_filesize"] = int(max_source_size)

        if media_type == "audio":
            options.update(
                {
                    "format": "bestaudio/best",
                    "postprocessors": [
                        {
                            "key": "FFmpegExtractAudio",
                            "preferredcodec": "mp3",
                            "preferredquality": "192",
                        }
                    ],
                }
            )
            expected_suffix = ".mp3"
        else:
            options.update(
                {
                    "format": (
                        "bestvideo[ext=mp4]+bestaudio[ext=m4a]"
                        "/best[ext=mp4]/best"
                    ),
                    "merge_output_format": "mp4",
                }
            )
            expected_suffix = ".mp4"

        try:
            info = await self._extract_with_fallback(
                self.video_url(video_id),
                options,
                download=True,
            )
        except RuntimeError as exc:
            if _STORAGE_LIMIT_MARKER.lower() in str(exc).lower():
                raise OSError(errno.ENOSPC, _STORAGE_LIMIT_MARKER) from exc
            raise

        if cancel_event.is_set():
            raise DownloadCancelledError(_CANCEL_MARKER)

        resolved_id = str(info.get("id") or video_id)
        title = str(info.get("title") or "Невідомо")
        expected = work_dir / f"{resolved_id}{expected_suffix}"

        final_path: Optional[Path] = expected if expected.is_file() else None

        if final_path is None:
            exact_candidates: list[Path] = []
            for candidate in captured_paths:
                candidate = candidate.resolve()
                if (
                    candidate.is_file()
                    and candidate.parent == work_dir.resolve()
                    and candidate.suffix.lower() == expected_suffix
                ):
                    exact_candidates.append(candidate)

            exact_candidates.extend(
                path.resolve()
                for path in work_dir.glob(f"*{expected_suffix}")
                if path.is_file()
            )

            unique_candidates = list(dict.fromkeys(exact_candidates))
            if len(unique_candidates) == 1:
                final_path = unique_candidates[0]

        if final_path is None or not final_path.is_file():
            label = "MP3" if media_type == "audio" else "MP4"
            raise FileNotFoundError(
                f"{label} не знайдено після завантаження у {work_dir}"
            )

        logger.info(
            "yt-dlp створив точний фінальний файл media_type=%s video_id=%s path=%s",
            media_type,
            resolved_id,
            final_path,
        )
        return YoutubeDownloadResult(
            video_id=resolved_id,
            title=title,
            final_path=final_path,
        )

    @staticmethod
    def video_url(video_id: str) -> str:
        return f"https://www.youtube.com/watch?v={video_id}"

    @staticmethod
    def playlist_url(playlist_id: str) -> str:
        return f"https://www.youtube.com/playlist?list={playlist_id}"

    @staticmethod
    def parse_reference(value: str) -> YoutubeReference:
        raw = value.strip()
        if not raw:
            return YoutubeReference(False, None, None, False)

        normalized = raw
        if "://" not in normalized:
            normalized = "https://" + normalized.lstrip("/")

        try:
            parsed = urlparse(normalized)
        except ValueError:
            return YoutubeReference(False, None, None, False)

        host = (parsed.hostname or "").lower()
        youtube_hosts = {
            "youtube.com",
            "www.youtube.com",
            "m.youtube.com",
            "music.youtube.com",
            "youtu.be",
            "www.youtu.be",
        }
        if host not in youtube_hosts:
            return YoutubeReference(False, None, None, False)

        query = parse_qs(parsed.query)
        video_id: Optional[str] = None
        playlist_id: Optional[str] = None

        if host.endswith("youtu.be"):
            candidate = parsed.path.strip("/").split("/")[0] if parsed.path.strip("/") else ""
            if _VIDEO_ID_RE.fullmatch(candidate):
                video_id = candidate
        else:
            path_parts = [part for part in parsed.path.split("/") if part]
            if parsed.path == "/watch":
                candidate = (query.get("v") or [""])[0]
                if _VIDEO_ID_RE.fullmatch(candidate):
                    video_id = candidate
            elif len(path_parts) >= 2 and path_parts[0] in {"shorts", "embed", "live"}:
                candidate = path_parts[1]
                if _VIDEO_ID_RE.fullmatch(candidate):
                    video_id = candidate

        list_candidate = (query.get("list") or [""])[0]
        if list_candidate and _PLAYLIST_ID_RE.fullmatch(list_candidate):
            playlist_id = list_candidate

        is_mix = bool(playlist_id and playlist_id.upper().startswith("RD"))
        return YoutubeReference(True, video_id, playlist_id, is_mix)

    @staticmethod
    def looks_like_url(value: str) -> bool:
        text = value.strip().lower()
        return text.startswith(("http://", "https://", "www.", "youtube.com/", "youtu.be/"))
