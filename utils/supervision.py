"""Local, generation-scoped communication between bot and supervisor."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path

import psutil

logger = logging.getLogger(__name__)


def write_state(path: Path, value: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value), encoding="utf-8")
    temporary.replace(path)


def read_state(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


class BotSupervision:
    def __init__(self) -> None:
        directory = os.environ.get("MUSIC_BOT_RUN_DIR")
        self.directory = Path(directory) if directory else None
        self.ready = False
        self.restarting = False
        self.task: asyncio.Task | None = None

    def request_restart(self, chat_id: int) -> bool:
        if self.directory is None:
            raise RuntimeError("Бота запущено без наглядача")
        if self.restarting:
            return False
        write_state(self.directory / "restart.json", {"chat_id": chat_id})
        self.restarting = True
        return True

    async def watch(self, dispatcher, main_task=None) -> None:
        if self.directory is None:
            return
        try:
            await self._watch(dispatcher, main_task)
        except Exception:
            logger.exception("Задача контролю бота відмовила; починаю коректне завершення")
            if self.ready:
                try:
                    await asyncio.wait_for(dispatcher.stop_polling(), timeout=5)
                    return
                except Exception:
                    logger.exception("Не вдалося зупинити polling; скасовую головну задачу")
            if main_task is not None:
                main_task.cancel()
            else:
                raise

    async def _watch(self, dispatcher, main_task) -> None:
        failures = 0
        while True:
            parent_pid = os.environ.get("MUSIC_BOT_SUPERVISOR_PID")
            if parent_pid and main_task is not None:
                try:
                    parent = psutil.Process(int(parent_pid))
                    alive = parent.is_running() and parent.status() != psutil.STATUS_ZOMBIE
                    alive = alive and abs(parent.create_time() - float(
                        os.environ["MUSIC_BOT_SUPERVISOR_CREATED"]
                    )) < .01
                except (psutil.NoSuchProcess, psutil.ZombieProcess):
                    alive = False
                except psutil.AccessDenied:
                    alive = True
                if not alive:
                    main_task.cancel()
                    return
            # A stop request must remain usable even when heartbeat writes fail.
            if self.ready and (self.directory / "stop.json").exists():
                await dispatcher.stop_polling()
                return
            try:
                write_state(self.directory / "heartbeat.json", {
                    "time": time.monotonic(), "ready": self.ready,
                })
            except OSError:
                failures += 1
                logger.warning("Не вдалося записати heartbeat %s (спроба %s/3)",
                               self.directory / "heartbeat.json", failures, exc_info=True)
                if failures >= 3:
                    raise
            else:
                if failures:
                    logger.info("Запис heartbeat відновлено після %s помилок", failures)
                failures = 0
            await asyncio.sleep(2)

    async def close(self) -> None:
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)


supervision = BotSupervision()
