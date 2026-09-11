from __future__ import annotations

import os
import asyncio
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from pathlib import Path

from supervisor import Supervisor
from utils.process_lock import ProcessLock, SingleInstanceError
from utils.supervision import BotSupervision, read_state, write_state


FAKE_BOT = '''
import json, os, time
from pathlib import Path
directory = Path(os.environ["MUSIC_BOT_RUN_DIR"])
mode = Path(__file__).with_name("mode").read_text()
if mode == "sequence":
    counter = Path(__file__).with_name("counter")
    count = int(counter.read_text()) + 1 if counter.exists() else 1
    counter.write_text(str(count))
    if count < 3:
        mode = "restart"
    else:
        (directory.parent / "supervisor-stop.json").write_text('{}')
if mode == "crash":
    raise SystemExit(3)
if mode == "hung":
    time.sleep(60)
while True:
    temporary = directory / "heartbeat.tmp"
    temporary.write_text(json.dumps({"time": time.monotonic(), "ready": mode != "starting"}))
    temporary.replace(directory / "heartbeat.json")
    if mode == "restart":
        (directory / "restart.json").write_text('{"chat_id": 42}')
    if (directory / "stop.json").exists():
        break
    time.sleep(.05)
'''


class SupervisorTests(unittest.TestCase):
    def run_case(self, mode):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / "bot.py").write_text(FAKE_BOT)
            (base / "mode").write_text(mode)
            run = base / "run"
            run.mkdir()
            supervisor = Supervisor(base, heartbeat_timeout=1.5,
                                    startup_timeout=2, stop_timeout=.2)
            result = supervisor.run_generation(run)
            self.assertIsNone(supervisor.child)
            return supervisor, result

    def test_restart_request_preserves_recipient(self):
        supervisor, result = self.run_case("restart")
        self.assertTrue(result[0])
        self.assertEqual(supervisor.notify_chat, "42")

    def test_hung_child_is_terminated(self):
        _, result = self.run_case("hung")
        self.assertEqual(result, (False, False))

    def test_startup_deadline_applies_even_with_heartbeat(self):
        _, result = self.run_case("starting")
        self.assertEqual(result, (False, False))

    def test_crash_is_detected(self):
        _, result = self.run_case("crash")
        self.assertEqual(result, (False, False))

    def test_restart_deduplication(self):
        with tempfile.TemporaryDirectory() as directory:
            client = BotSupervision()
            client.directory = Path(directory)
            self.assertTrue(client.request_restart(42))
            self.assertFalse(client.request_restart(43))
            self.assertIn("42", (Path(directory) / "restart.json").read_text())

    def test_three_generations_then_explicit_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / "bot.py").write_text(FAKE_BOT)
            (base / "mode").write_text("sequence")
            supervisor = Supervisor(base, heartbeat_timeout=2,
                                    startup_timeout=3, stop_timeout=.5)
            self.assertEqual(supervisor.run(), 0)
            self.assertEqual((base / "counter").read_text(), "3")
            self.assertIsNone(supervisor.child)
            self.assertFalse((base / ".runtime" / "supervisor.lock").exists())

    def test_os_lock_excludes_other_process_and_recovers_after_crash(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "instance.lock"
            script = (
                "import sys,time; from pathlib import Path; "
                "from utils.process_lock import ProcessLock; "
                "lock=ProcessLock(Path(sys.argv[1])); lock.acquire(); "
                "print('locked',flush=True); time.sleep(60)"
            )
            kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
            child = subprocess.Popen([sys.executable, "-c", script, str(path)],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs)
            try:
                self.assertEqual(child.stdout.readline().strip(), b"locked")
                contender = ProcessLock(path)
                with self.assertRaises(SingleInstanceError):
                    contender.acquire()
            finally:
                child.kill()
                child.communicate(timeout=5)
            recovered = ProcessLock(path)
            recovered.acquire()
            recovered.release()
            recovered.acquire()
            recovered.release()


class ClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_graceful_stop(self):
        class Dispatcher:
            stopped = False

            async def stop_polling(self):
                self.stopped = True

        with tempfile.TemporaryDirectory() as directory:
            client = BotSupervision()
            client.directory = Path(directory)
            client.ready = True
            (client.directory / "stop.json").write_text("{}")
            dispatcher = Dispatcher()
            await asyncio.wait_for(client.watch(dispatcher), timeout=1)
            self.assertTrue(dispatcher.stopped)

    async def test_transient_heartbeat_failure_recovers(self):
        with tempfile.TemporaryDirectory() as directory:
            client = BotSupervision()
            client.directory = Path(directory)
            client.ready = True
            dispatcher = AsyncMock()
            attempts = 0

            def flaky_write(path, value):
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise PermissionError("temporary sharing failure")
                write_state(path, value)
                (client.directory / "stop.json").write_text("{}")

            with patch("utils.supervision.write_state", side_effect=flaky_write), \
                    patch("utils.supervision.asyncio.sleep", new_callable=AsyncMock), \
                    self.assertLogs("utils.supervision", level="INFO") as logs:
                await asyncio.wait_for(client.watch(dispatcher), timeout=1)
            self.assertTrue(read_state(client.directory / "heartbeat.json")["ready"])
            self.assertTrue(any("PermissionError" in line for line in logs.output))
            self.assertTrue(any("відновлено" in line for line in logs.output))
            dispatcher.stop_polling.assert_awaited_once()

    async def test_persistent_heartbeat_failure_stops_polling(self):
        with tempfile.TemporaryDirectory() as directory:
            client = BotSupervision()
            client.directory = Path(directory)
            client.ready = True
            dispatcher = AsyncMock()
            with patch("utils.supervision.write_state", side_effect=PermissionError("denied")), \
                    patch("utils.supervision.asyncio.sleep", new_callable=AsyncMock), \
                    self.assertLogs("utils.supervision", level="ERROR") as logs:
                await asyncio.wait_for(client.watch(dispatcher), timeout=1)
            dispatcher.stop_polling.assert_awaited_once()
            self.assertTrue(any("PermissionError" in line for line in logs.output))

    async def test_failure_cancels_main_and_runs_cleanup(self):
        for ready in (False, True):
            with self.subTest(ready=ready), tempfile.TemporaryDirectory() as directory:
                client = BotSupervision()
                client.directory = Path(directory)
                client.ready = ready
                cleaned = asyncio.Event()
                started = asyncio.Event()

                async def main():
                    try:
                        started.set()
                        await asyncio.Event().wait()
                    finally:
                        cleaned.set()

                main_task = asyncio.create_task(main())
                await started.wait()
                dispatcher = AsyncMock()
                dispatcher.stop_polling.side_effect = RuntimeError("not polling")
                try:
                    with patch("utils.supervision.write_state", side_effect=RuntimeError("unexpected")), \
                            self.assertLogs("utils.supervision", level="ERROR"):
                        await asyncio.wait_for(client.watch(dispatcher, main_task), timeout=1)
                    await asyncio.wait_for(cleaned.wait(), timeout=1)
                    self.assertTrue(main_task.cancelled())
                finally:
                    main_task.cancel()
                    await asyncio.gather(main_task, return_exceptions=True)

    async def test_stop_request_survives_heartbeat_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            client = BotSupervision()
            client.directory = Path(directory)
            client.ready = True
            dispatcher = AsyncMock()

            def fail_write(path, value):
                (client.directory / "stop.json").write_text("{}")
                raise PermissionError("denied")

            with patch("utils.supervision.write_state", side_effect=fail_write), \
                    patch("utils.supervision.asyncio.sleep", new_callable=AsyncMock), \
                    self.assertLogs("utils.supervision", level="WARNING"):
                await asyncio.wait_for(client.watch(dispatcher), timeout=1)
            dispatcher.stop_polling.assert_awaited_once()

    async def test_stalled_stop_falls_back_to_main_cancellation(self):
        with tempfile.TemporaryDirectory() as directory:
            client = BotSupervision()
            client.directory = Path(directory)
            client.ready = True
            main_task = asyncio.create_task(asyncio.Event().wait())
            dispatcher = AsyncMock()
            dispatcher.stop_polling.side_effect = asyncio.Event().wait
            try:
                with patch("utils.supervision.write_state", side_effect=RuntimeError("failed")), \
                        self.assertLogs("utils.supervision", level="ERROR"):
                    await asyncio.wait_for(client.watch(dispatcher, main_task), timeout=7)
                await asyncio.gather(main_task, return_exceptions=True)
                self.assertTrue(main_task.cancelled())
            finally:
                main_task.cancel()
                await asyncio.gather(main_task, return_exceptions=True)

    async def test_normal_close_does_not_cancel_main(self):
        with tempfile.TemporaryDirectory() as directory:
            client = BotSupervision()
            client.directory = Path(directory)
            main_task = asyncio.create_task(asyncio.Event().wait())
            client.task = asyncio.create_task(client.watch(AsyncMock(), main_task))
            try:
                await asyncio.sleep(0)
                with self.assertNoLogs("utils.supervision", level="ERROR"):
                    await client.close()
                self.assertFalse(main_task.done())
            finally:
                main_task.cancel()
                await asyncio.gather(main_task, return_exceptions=True)

    async def test_lost_supervisor_cancels_bot_initialization(self):
        with tempfile.TemporaryDirectory() as directory:
            client = BotSupervision()
            client.directory = Path(directory)
            task = asyncio.create_task(asyncio.sleep(60))
            with patch.dict(os.environ, {"MUSIC_BOT_SUPERVISOR_PID": str(os.getpid()),
                                        "MUSIC_BOT_SUPERVISOR_CREATED": "1"}):
                await client.watch(None, task)
            await asyncio.gather(task, return_exceptions=True)
            self.assertTrue(task.cancelled())


if __name__ == "__main__":
    unittest.main()
