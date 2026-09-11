"""Run with python supervisor.py; stop with python supervisor.py --stop."""
from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

import psutil

from utils.process_lock import ProcessLock, SingleInstanceError
from utils.supervision import read_state, write_state

BASE_DIR = Path(__file__).resolve().parent
logger = logging.getLogger("supervisor")


class Supervisor:
    def __init__(self, base_dir: Path = BASE_DIR, *, heartbeat_timeout: float = 90,
                 startup_timeout: float = 180, stop_timeout: float = 45) -> None:
        self.base_dir = base_dir
        self.runtime = base_dir / ".runtime"
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.stop_file = self.runtime / "supervisor-stop.json"
        self.heartbeat_timeout = heartbeat_timeout
        self.startup_timeout = startup_timeout
        self.stop_timeout = stop_timeout
        self.child: subprocess.Popen | None = None
        self.stopping = False
        self.notify_chat = ""
        self.notify_restart = False

    def stop_requested(self) -> bool:
        return self.stopping or self.stop_file.exists()

    def stop_child(self, run_dir: Path) -> None:
        child = self.child
        if child is None:
            return
        descendants = []
        if child.poll() is None:
            try:
                descendants = psutil.Process(child.pid).children(recursive=True)
            except psutil.NoSuchProcess:
                pass
            write_state(run_dir / "stop.json", {})
            try:
                child.wait(timeout=self.stop_timeout)
            except subprocess.TimeoutExpired:
                logger.warning("Бот не завершився за %.0f с; terminate pid=%s",
                               self.stop_timeout, child.pid)
                child.terminate()
                try:
                    child.wait(timeout=7)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)
        # These are captured descendants of our child, never processes selected by name.
        for process in descendants:
            try:
                if process.is_running():
                    process.terminate()
            except psutil.NoSuchProcess:
                pass
        _, alive = psutil.wait_procs(descendants, timeout=3)
        for process in alive:
            try:
                process.kill()
            except psutil.NoSuchProcess:
                pass
        self.child = None

    @staticmethod
    def capture_output(pipe) -> None:
        output = logging.getLogger("bot_output")
        try:
            for line in iter(pipe.readline, b""):
                output.info(line.decode("utf-8", errors="replace").rstrip())
        finally:
            pipe.close()

    def run_generation(self, run_dir: Path) -> tuple[bool, bool]:
        env = os.environ.copy()
        env.update(MUSIC_BOT_RUN_DIR=str(run_dir), PYTHONUNBUFFERED="1",
                   MUSIC_BOT_SUPERVISOR_PID=str(os.getpid()),
                   MUSIC_BOT_SUPERVISOR_CREATED=str(psutil.Process().create_time()),
                   PYTHONIOENCODING="utf-8", MUSIC_BOT_NOTIFY_CHAT=self.notify_chat,
                   MUSIC_BOT_NOTIFY_RESTART="1" if self.notify_restart else "")
        kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
        self.child = subprocess.Popen(
            [sys.executable, str(self.base_dir / "bot.py")], cwd=self.base_dir,
            env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, **kwargs,
        )
        child = self.child
        reader = threading.Thread(target=self.capture_output, args=(child.stdout,), daemon=True)
        reader.start()
        logger.info("Запущено bot.py pid=%s", child.pid)
        started = last_beat = time.monotonic()
        last_value = None
        ready_at = None
        requested = False
        try:
            while not self.stop_requested():
                if child.poll() is not None:
                    logger.warning("bot.py завершився: code=%s", child.returncode)
                    if child.returncode == 77:
                        logger.error("Інший бот уже працює; наглядач зупиняється")
                        self.stopping = True
                    break
                now = time.monotonic()
                heartbeat = read_state(run_dir / "heartbeat.json")
                if heartbeat.get("time") is not None and heartbeat["time"] != last_value:
                    last_value = heartbeat["time"]
                    last_beat = now
                    if heartbeat.get("ready") and ready_at is None:
                        ready_at = now
                        logger.info("Бот готовий, pid=%s", child.pid)
                        self.notify_restart = False
                        self.notify_chat = ""
                request = read_state(run_dir / "restart.json")
                if request:
                    self.notify_chat = str(request.get("chat_id") or "")
                    requested = True
                    logger.info("Прийнято запит рестарту pid=%s", child.pid)
                    break
                if now - last_beat > self.heartbeat_timeout:
                    logger.error("Відсутній heartbeat; перезапуск pid=%s", child.pid)
                    break
                if ready_at is None and now - started > self.startup_timeout:
                    logger.error("Перевищено час ініціалізації pid=%s", child.pid)
                    break
                time.sleep(0.25)
        finally:
            self.stop_child(run_dir)
            reader.join(timeout=2)
        stable = ready_at is not None and time.monotonic() - ready_at >= 300
        return requested, stable

    def run(self) -> int:
        lock = ProcessLock(self.runtime / "supervisor.lock")
        try:
            lock.acquire()
        except SingleInstanceError as exc:
            logger.error("%s", exc)
            return 1
        try:
            self.stop_file.unlink(missing_ok=True)
            failures = 0
            while not self.stop_requested():
                with tempfile.TemporaryDirectory(prefix="run-", dir=self.runtime) as directory:
                    try:
                        requested, stable = self.run_generation(Path(directory))
                    except KeyboardInterrupt:
                        self.stopping = True
                        break
                if self.stop_requested():
                    break
                self.notify_restart = True
                if requested or stable:
                    failures = 0
                else:
                    failures += 1
                delay = 1 if requested else min(300, 2 ** min(failures + 1, 9))
                logger.info("Наступний запуск через %s с", delay)
                deadline = time.monotonic() + delay
                while time.monotonic() < deadline and not self.stop_requested():
                    time.sleep(0.25)
            return 0
        except KeyboardInterrupt:
            return 0
        finally:
            lock.release()


def configure_logging() -> None:
    directory = BASE_DIR / "logs"
    directory.mkdir(exist_ok=True)
    logger.setLevel(logging.INFO)
    handler = RotatingFileHandler(directory / "supervisor.log", maxBytes=2_000_000,
                                  backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    if sys.stderr is not None:
        logger.addHandler(logging.StreamHandler())
    output = logging.getLogger("bot_output")
    output.setLevel(logging.INFO)
    output.propagate = False
    output.addHandler(RotatingFileHandler(directory / "restart_child.log",
                                         maxBytes=5_000_000, backupCount=3, encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Наглядач музичного бота")
    parser.add_argument("--stop", action="store_true", help="Зупинити наглядач і бота")
    args = parser.parse_args()
    configure_logging()
    supervisor = Supervisor()
    if args.stop:
        write_state(supervisor.stop_file, {})
        return 0
    try:
        return supervisor.run()
    except Exception:
        logger.exception("Критична помилка наглядача")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
