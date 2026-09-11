from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator, Optional

from services.downloader import CancellationToken, DownloadService, DownloadTask
from utils.errors import DownloadCancelledError


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class CancelResult:
    queued_cancelled: int
    active_found: int
    active_stopped: int

    @property
    def active_still_stopping(self) -> int:
        return max(0, self.active_found - self.active_stopped)


@dataclass(slots=True)
class EnqueueResult:
    requested: int
    added: int
    duplicate: int
    rejected_user_limit: int
    rejected_global_limit: int

    @property
    def rejected(self) -> int:
        return self.requested - self.added


@dataclass(slots=True)
class _JobState:
    download: DownloadTask
    token: CancellationToken
    runner: Optional[asyncio.Task[None]] = None
    phase: str = "queued"


class DownloadManager:
    """Глобальний scheduler з dedup, hard caps і fairness per user."""

    def __init__(
        self,
        service: DownloadService,
        *,
        max_concurrent: int,
        max_per_user: int,
        max_global: int,
        max_active_per_user: int = 2,
        dedup_cooldown_seconds: int = 3,
    ) -> None:
        self.service = service
        self.max_concurrent = max_concurrent
        self.max_per_user = max_per_user
        self.max_global = max_global
        self.max_active_per_user = max(1, min(max_active_per_user, max_concurrent))
        self.dedup_cooldown_seconds = max(0, dedup_cooldown_seconds)

        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._jobs: dict[str, _JobState] = {}
        self._jobs_by_user: dict[int, set[str]] = {}
        self._jobs_by_media: dict[tuple[str, str], set[str]] = {}
        self._dedup_keys: dict[tuple[int, str, str, str], str] = {}
        self._recent_dedup: dict[tuple[int, str, str, str], float] = {}
        self._lock = asyncio.Lock()
        self._stopping = False

        self._user_slot_guard = asyncio.Lock()
        self._user_semaphores: dict[int, asyncio.Semaphore] = {}
        self._user_semaphore_refs: dict[int, int] = {}

    async def start(self) -> None:
        self._stopping = False
        logger.info(
            "Глобальний менеджер завантажень запущено: одночасно=%s, "
            "active/user=%s, черга/user=%s, глобальна черга=%s, dedup cooldown=%s сек",
            self.max_concurrent,
            self.max_active_per_user,
            self.max_per_user,
            self.max_global,
            self.dedup_cooldown_seconds,
        )

    @staticmethod
    def _dedup_key(download: DownloadTask) -> tuple[int, str, str, str]:
        return (
            download.user_id,
            download.video_id,
            download.media_type.value,
            str(download.target_chat_id),
        )

    async def enqueue(self, download: DownloadTask) -> bool:
        return (await self.enqueue_many_detailed([download])).added == 1

    async def enqueue_detailed(self, download: DownloadTask) -> EnqueueResult:
        return await self.enqueue_many_detailed([download])

    async def enqueue_many(self, downloads: list[DownloadTask]) -> int:
        return (await self.enqueue_many_detailed(downloads)).added

    async def enqueue_many_detailed(self, downloads: list[DownloadTask]) -> EnqueueResult:
        if not downloads:
            return EnqueueResult(0, 0, 0, 0, 0)

        user_id = downloads[0].user_id
        if any(item.user_id != user_id for item in downloads):
            raise ValueError("enqueue_many приймає завдання лише одного користувача")

        added_states: list[_JobState] = []
        duplicates = 0
        user_rejected = 0
        global_rejected = 0
        now = time.monotonic()

        async with self._lock:
            if self._stopping:
                return EnqueueResult(len(downloads), 0, 0, 0, len(downloads))

            self._prune_recent_dedup(now)
            current_ids = self._jobs_by_user.setdefault(user_id, set())

            for download in downloads:
                key = self._dedup_key(download)
                recent_until = self._recent_dedup.get(key, 0.0)
                if key in self._dedup_keys or recent_until > now:
                    duplicates += 1
                    continue
                if len(current_ids) >= self.max_per_user:
                    user_rejected += 1
                    continue
                if len(self._jobs) >= self.max_global:
                    global_rejected += 1
                    continue

                state = _JobState(download=download, token=CancellationToken())
                self._jobs[download.task_id] = state
                current_ids.add(download.task_id)
                self._dedup_keys[key] = download.task_id
                media_key = (download.video_id, download.media_type.value)
                self._jobs_by_media.setdefault(media_key, set()).add(download.task_id)
                state.runner = asyncio.create_task(
                    self._run_job(state),
                    name=f"download-{download.task_id}",
                )
                added_states.append(state)

            if not current_ids:
                self._jobs_by_user.pop(user_id, None)

        for state in added_states:
            download = state.download
            logger.info(
                "Додано в чергу media_type=%s video_id=%s user_id=%s task_id=%s",
                download.media_type.value,
                download.video_id,
                download.user_id,
                download.task_id,
            )

        if duplicates:
            logger.info("Відхилено дублікатів завдань user_id=%s count=%s", user_id, duplicates)

        return EnqueueResult(
            requested=len(downloads),
            added=len(added_states),
            duplicate=duplicates,
            rejected_user_limit=user_rejected,
            rejected_global_limit=global_rejected,
        )

    async def _run_job(self, state: _JobState) -> None:
        global_acquired = False
        try:
            # Fairness: per-user slot береться ДО глобального semaphore. Завдання
            # одного user, що чекають, не займають усі 8 global slots.
            async with self._user_slot(state.download.user_id):
                await self._semaphore.acquire()
                global_acquired = True
                try:
                    async with self._lock:
                        if state.download.task_id not in self._jobs:
                            return
                        state.phase = "active"

                    state.token.raise_if_cancelled()
                    await self.service.process(state.download, state.token)
                finally:
                    if global_acquired:
                        self._semaphore.release()
                        global_acquired = False

        except (DownloadCancelledError, asyncio.CancelledError):
            logger.info(
                "Завдання зупинено user_id=%s task_id=%s phase=%s",
                state.download.user_id,
                state.download.task_id,
                state.phase,
            )
        except Exception:
            logger.exception(
                "Неочікувана помилка менеджера завантажень user_id=%s task_id=%s",
                state.download.user_id,
                state.download.task_id,
            )
        finally:
            if global_acquired:
                self._semaphore.release()
            await self._remove_state(state)

    @asynccontextmanager
    async def _user_slot(self, user_id: int) -> AsyncIterator[None]:
        async with self._user_slot_guard:
            semaphore = self._user_semaphores.get(user_id)
            if semaphore is None:
                semaphore = asyncio.Semaphore(self.max_active_per_user)
                self._user_semaphores[user_id] = semaphore
            self._user_semaphore_refs[user_id] = self._user_semaphore_refs.get(user_id, 0) + 1

        acquired = False
        try:
            await semaphore.acquire()
            acquired = True
            yield
        finally:
            if acquired:
                semaphore.release()
            async with self._user_slot_guard:
                remaining = self._user_semaphore_refs.get(user_id, 1) - 1
                if remaining <= 0:
                    self._user_semaphore_refs.pop(user_id, None)
                    self._user_semaphores.pop(user_id, None)
                else:
                    self._user_semaphore_refs[user_id] = remaining

    async def _remove_state(self, state: _JobState) -> None:
        async with self._lock:
            task_id = state.download.task_id
            self._jobs.pop(task_id, None)

            user_jobs = self._jobs_by_user.get(state.download.user_id)
            if user_jobs is not None:
                user_jobs.discard(task_id)
                if not user_jobs:
                    self._jobs_by_user.pop(state.download.user_id, None)

            media_key = (state.download.video_id, state.download.media_type.value)
            media_jobs = self._jobs_by_media.get(media_key)
            if media_jobs is not None:
                media_jobs.discard(task_id)
                if not media_jobs:
                    self._jobs_by_media.pop(media_key, None)

            key = self._dedup_key(state.download)
            if self._dedup_keys.get(key) == task_id:
                self._dedup_keys.pop(key, None)
                if self.dedup_cooldown_seconds > 0:
                    self._recent_dedup[key] = time.monotonic() + self.dedup_cooldown_seconds

    def _prune_recent_dedup(self, now: float) -> None:
        if not self._recent_dedup:
            return
        expired = [key for key, expires in self._recent_dedup.items() if expires <= now]
        for key in expired:
            self._recent_dedup.pop(key, None)

    async def cancel_user(self, user_id: int) -> CancelResult:
        return await self._cancel_matching(lambda state: state.download.user_id == user_id, label=f"user_id={user_id}")

    async def cancel_non_admin(self, admin_id: int) -> CancelResult:
        return await self._cancel_matching(
            lambda state: state.download.user_id != admin_id,
            label=f"lockdown non-admin (admin_id={admin_id})",
        )

    async def _cancel_matching(self, predicate, *, label: str) -> CancelResult:
        async with self._lock:
            states = [state for state in self._jobs.values() if predicate(state)]
            queued_states = [state for state in states if state.phase == "queued"]
            active_states = [state for state in states if state.phase == "active"]

            for state in states:
                state.token.cancel()
            for state in queued_states:
                if state.runner is not None and not state.runner.done():
                    state.runner.cancel()

            active_runners = [
                state.runner
                for state in active_states
                if state.runner is not None and not state.runner.done()
            ]
            queued_runners = [
                state.runner
                for state in queued_states
                if state.runner is not None
            ]

        if queued_runners:
            await asyncio.gather(*queued_runners, return_exceptions=True)

        active_stopped = 0
        if active_runners:
            done, _ = await asyncio.wait(active_runners, timeout=5)
            active_stopped = len(done)

        logger.info(
            "Скасування %s: queued=%s active=%s stopped=%s",
            label,
            len(queued_states),
            len(active_states),
            active_stopped,
        )
        return CancelResult(
            queued_cancelled=len(queued_states),
            active_found=len(active_states),
            active_stopped=active_stopped,
        )

    async def user_job_count(self, user_id: int) -> int:
        async with self._lock:
            return len(self._jobs_by_user.get(user_id, set()))

    async def global_job_count(self) -> int:
        async with self._lock:
            return len(self._jobs)

    async def active_job_count(self) -> int:
        async with self._lock:
            return sum(1 for state in self._jobs.values() if state.phase == "active")

    async def queued_job_count(self) -> int:
        async with self._lock:
            return sum(1 for state in self._jobs.values() if state.phase == "queued")

    async def is_media_pending_or_active(self, video_id: str, media_type: str) -> bool:
        async with self._lock:
            return bool(self._jobs_by_media.get((video_id, media_type)))

    @asynccontextmanager
    async def media_maintenance_guard(self, video_id: str, media_type: str):
        async with self.service.media_maintenance_guard(video_id, media_type):
            yield

    async def shutdown(self) -> None:
        async with self._lock:
            self._stopping = True
            states = list(self._jobs.values())
            for state in states:
                state.token.cancel()
                if state.phase == "queued" and state.runner is not None:
                    state.runner.cancel()
            runners = [state.runner for state in states if state.runner is not None]

        if runners:
            done, pending = await asyncio.wait(runners, timeout=10)
            if pending:
                logger.warning(
                    "Після запиту shutdown ще працює %s завдань; скасовую asyncio wrappers",
                    len(pending),
                )
                for runner in pending:
                    runner.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
            if done:
                await asyncio.gather(*done, return_exceptions=True)

        logger.info("Менеджер завантажень зупинено")
