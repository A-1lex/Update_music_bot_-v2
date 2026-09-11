from __future__ import annotations

import json
import os
from pathlib import Path

import psutil

class SingleInstanceError(RuntimeError):
    pass


class ProcessLock:
    """OS lock held for the owner's lifetime; metadata also protects legacy bots."""

    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self._handle = None

    def acquire(self) -> None:
        if self._handle is not None:
            raise SingleInstanceError("Блокування вже захоплено")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.with_suffix(self.path.suffix + ".guard").open("a+b")
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise SingleInstanceError("Інший екземпляр уже запущено") from exc
        self._handle = handle
        try:
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
            if isinstance(data, dict) and data.get("pid"):
                try:
                    process = psutil.Process(int(data["pid"]))
                    expected = float(data.get("create_time") or 0)
                    alive = process.is_running() and process.status() != psutil.STATUS_ZOMBIE
                    if alive and (expected <= 0 or abs(process.create_time() - expected) < 0.01):
                        raise SingleInstanceError("Інший екземпляр уже запущено")
                except (psutil.NoSuchProcess, psutil.ZombieProcess, ValueError, TypeError):
                    pass
                except psutil.AccessDenied as exc:
                    raise SingleInstanceError("Не вдалося перевірити власника блокування") from exc
            self.path.write_text(json.dumps({
                "pid": os.getpid(), "create_time": psutil.Process().create_time(),
            }), encoding="utf-8")
        except BaseException:
            self._unlock()
            raise

    def _unlock(self) -> None:
        if self._handle is None:
            return
        handle, self._handle = self._handle, None
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def release(self) -> None:
        if self._handle is not None:
            try:
                self.path.unlink(missing_ok=True)
            finally:
                self._unlock()
