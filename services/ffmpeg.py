from __future__ import annotations

import asyncio
import logging
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from config import Config
from utils.errors import DownloadCancelledError, FFmpegError


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class SplitResult:
    temp_dir: Path
    parts: list[Path]


class FFmpegService:
    def __init__(self, config: Config) -> None:
        self.config = config
        self._processes: dict[int, asyncio.subprocess.Process] = {}
        self._process_lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(config.MAX_CONCURRENT_FFMPEG)

    async def self_check(self) -> tuple[bool, bool]:
        ffmpeg_ok = await self._check_binary(self.config.FFMPEG_EXE)
        ffprobe_ok = await self._check_binary(self.config.FFPROBE_EXE)
        return ffmpeg_ok, ffprobe_ok

    async def _check_binary(self, executable: Optional[Path]) -> bool:
        if executable is None or not executable.is_file():
            return False
        try:
            process = await asyncio.create_subprocess_exec(
                str(executable),
                "-version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(process.communicate(), timeout=10)
            return process.returncode == 0
        except (OSError, asyncio.TimeoutError):
            return False

    async def get_duration(
        self,
        file_path: Path,
        cancel_event: Optional[asyncio.Event] = None,
    ) -> float:
        ffprobe = self.config.FFPROBE_EXE
        if ffprobe is None:
            raise FFmpegError("FFprobe не знайдено")

        stdout = await self._run(
            [
                str(ffprobe),
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(file_path),
            ],
            cancel_event=cancel_event,
        )
        try:
            duration = float(stdout.strip())
        except ValueError as exc:
            raise FFmpegError(f"FFprobe повернув некоректну тривалість: {stdout!r}") from exc

        if duration <= 0:
            raise FFmpegError("FFprobe повернув нульову тривалість")
        return duration

    async def split_audio(
        self,
        file_path: Path,
        max_size: int,
        cancel_event: Optional[asyncio.Event] = None,
    ) -> SplitResult:
        return await self._split_media(
            file_path=file_path,
            max_size=max_size,
            media_type="audio",
            cancel_event=cancel_event,
        )

    async def split_video(
        self,
        file_path: Path,
        max_size: int,
        cancel_event: Optional[asyncio.Event] = None,
    ) -> SplitResult:
        return await self._split_media(
            file_path=file_path,
            max_size=max_size,
            media_type="video",
            cancel_event=cancel_event,
        )

    async def _split_media(
        self,
        *,
        file_path: Path,
        max_size: int,
        media_type: str,
        cancel_event: Optional[asyncio.Event],
    ) -> SplitResult:
        ffmpeg = self.config.FFMPEG_EXE
        if ffmpeg is None:
            raise FFmpegError("FFmpeg не знайдено")
        if not file_path.is_file():
            raise FileNotFoundError(str(file_path))

        file_size = file_path.stat().st_size
        if file_size <= max_size:
            raise ValueError("Файл не потребує поділу")

        duration = await self.get_duration(file_path, cancel_event)
        # 82% дає запас на нерівномірний бітрейт і контейнерні накладні витрати.
        initial_part_duration = max(
            0.5,
            duration * (max_size / file_size) * 0.82,
        )

        temp_dir = (
            self.config.TEMP_DIR
            / "segments"
            / f"{media_type}_{uuid.uuid4().hex}"
        )
        temp_dir.mkdir(parents=True, exist_ok=True)
        parts: list[Path] = []

        try:
            start = 0.0
            part_number = 1
            while start < duration - 0.01:
                if cancel_event is not None and cancel_event.is_set():
                    raise DownloadCancelledError("Завантаження скасовано користувачем")

                remaining = duration - start
                segment_duration = min(initial_part_duration, remaining)
                output = temp_dir / (
                    f"part_{part_number}.mp3"
                    if media_type == "audio"
                    else f"part_{part_number}.mp4"
                )

                success = False
                for _ in range(10):
                    if output.exists():
                        output.unlink(missing_ok=True)

                    command = self._build_split_command(
                        ffmpeg=ffmpeg,
                        input_path=file_path,
                        output_path=output,
                        start=start,
                        duration=segment_duration,
                        media_type=media_type,
                    )
                    await self._run(command, cancel_event=cancel_event)

                    if output.is_file() and 0 < output.stat().st_size <= max_size:
                        success = True
                        break

                    segment_duration *= 0.72
                    if segment_duration < 0.10:
                        break

                if not success:
                    raise FFmpegError(
                        f"Не вдалося створити {media_type}-сегмент менше {max_size} байтів"
                    )

                parts.append(output)
                start += segment_duration
                part_number += 1

            if not parts:
                raise FFmpegError("FFmpeg не створив жодної частини")

            return SplitResult(temp_dir=temp_dir, parts=parts)

        except Exception:
            await asyncio.to_thread(shutil.rmtree, temp_dir, True)
            raise

    @staticmethod
    def _build_split_command(
        *,
        ffmpeg: Path,
        input_path: Path,
        output_path: Path,
        start: float,
        duration: float,
        media_type: str,
    ) -> list[str]:
        base = [
            str(ffmpeg),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            f"{start:.3f}",
            "-t",
            f"{duration:.3f}",
            "-i",
            str(input_path),
        ]

        if media_type == "audio":
            return base + [
                "-map",
                "0:a:0?",
                "-c",
                "copy",
                str(output_path),
            ]

        return base + [
            "-map",
            "0:v:0?",
            "-map",
            "0:a:0?",
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(output_path),
        ]

    async def cleanup_split_result(self, result: SplitResult) -> None:
        await asyncio.to_thread(shutil.rmtree, result.temp_dir, True)

    async def _run(
        self,
        command: list[str],
        *,
        cancel_event: Optional[asyncio.Event] = None,
    ) -> str:
        if cancel_event is not None and cancel_event.is_set():
            raise DownloadCancelledError("Завантаження скасовано користувачем")

        await self._semaphore.acquire()
        try:
            if cancel_event is not None and cancel_event.is_set():
                raise DownloadCancelledError("Завантаження скасовано користувачем")
            try:
                process = await asyncio.create_subprocess_exec(
                    *command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except OSError as exc:
                raise FFmpegError(f"Не вдалося запустити процес: {exc}") from exc

            process_key = process.pid or id(process)
            async with self._process_lock:
                self._processes[process_key] = process

            communicate_task = asyncio.create_task(process.communicate())
            cancel_task: Optional[asyncio.Task[bool]] = None

            try:
                if cancel_event is not None:
                    cancel_task = asyncio.create_task(cancel_event.wait())
                    done, _ = await asyncio.wait(
                        {communicate_task, cancel_task},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if cancel_task in done and cancel_event.is_set() and not communicate_task.done():
                        await self._terminate_process(process)
                        communicate_task.cancel()
                        await asyncio.gather(communicate_task, return_exceptions=True)
                        raise DownloadCancelledError("Завантаження скасовано користувачем")

                stdout, stderr = await communicate_task
                if process.returncode != 0:
                    details = stderr.decode("utf-8", errors="replace").strip()
                    raise FFmpegError(
                        f"FFmpeg/FFprobe завершився з кодом {process.returncode}: {details}"
                    )

                return stdout.decode("utf-8", errors="replace")

            except asyncio.CancelledError:
                await self._terminate_process(process)
                raise
            finally:
                if cancel_task is not None:
                    cancel_task.cancel()
                    await asyncio.gather(cancel_task, return_exceptions=True)
                async with self._process_lock:
                    self._processes.pop(process_key, None)

        finally:
            self._semaphore.release()

    @staticmethod
    async def _terminate_process(process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        try:
            process.terminate()
        except ProcessLookupError:
            return

        try:
            await asyncio.wait_for(process.wait(), timeout=3)
        except asyncio.TimeoutError:
            try:
                process.kill()
            except ProcessLookupError:
                return
            await process.wait()

    async def shutdown(self) -> None:
        async with self._process_lock:
            processes = list(self._processes.values())

        for process in processes:
            await self._terminate_process(process)

        logger.info("Активні FFmpeg/FFprobe процеси завершено")
