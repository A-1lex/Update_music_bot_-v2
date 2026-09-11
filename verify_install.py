from __future__ import annotations

import argparse
import ast
import compileall
import importlib.metadata
import inspect
import re
import sqlite3
import sys
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
TOKEN_LIKE_RE = re.compile(r"(?<![A-Za-z0-9_])\d{6,12}:[A-Za-z0-9_-]{20,}(?![A-Za-z0-9_-])")

REQUIRED_FILES = (
    "bot.py",
    "config.py",
    "database.py",
    "restart_bot.py",
    "requirements.txt",
    "handlers/__init__.py",
    "handlers/start.py",
    "handlers/search.py",
    "handlers/downloads.py",
    "handlers/callbacks.py",
    "handlers/admin.py",
    "handlers/security.py",
    "services/__init__.py",
    "services/youtube.py",
    "services/downloader.py",
    "services/ffmpeg.py",
    "services/storage.py",
    "workers/__init__.py",
    "workers/download_worker.py",
    "workers/cleanup.py",
    "keyboards/__init__.py",
    "keyboards/search.py",
    "keyboards/admin.py",
    "utils/__init__.py",
    "utils/errors.py",
    "utils/filenames.py",
    "utils/html.py",
    "utils/logging.py",
    "utils/single_instance.py",
    "security/__init__.py",
    "security/service.py",
    "security/middleware.py",
)

REQUIRED_DIRS = (
    "handlers",
    "services",
    "workers",
    "keyboards",
    "utils",
    "security",
    "music",
    "video",
    "downloads",
    "temp",
    "temp/downloads",
    "temp/segments",
    "logs",
)


def ok(message: str) -> None:
    print(f"OK   {message}")


def warn(message: str) -> None:
    print(f"WARN {message}")


def fail(message: str, failures: list[str]) -> None:
    print(f"FAIL {message}")
    failures.append(message)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Перевірка синхронності проєкту")
    parser.add_argument(
        "--skip-deps",
        action="store_true",
        help="Не перевіряти встановлені Python-залежності (лише для offline-аудиту).",
    )
    return parser.parse_args()


def ast_tree(relative: str) -> ast.AST:
    return ast.parse((BASE_DIR / relative).read_text(encoding="utf-8"), filename=relative)


def class_method_args(relative: str, class_name: str, method_name: str) -> set[str]:
    tree = ast_tree(relative)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == method_name:
                    args = list(item.args.posonlyargs) + list(item.args.args) + list(item.args.kwonlyargs)
                    return {arg.arg for arg in args}
    return set()


def function_args(relative: str, function_name: str) -> set[str]:
    tree = ast_tree(relative)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name:
            args = list(node.args.posonlyargs) + list(node.args.args) + list(node.args.kwonlyargs)
            return {arg.arg for arg in args}
    return set()


def source_has(relative: str, *needles: str) -> bool:
    text = (BASE_DIR / relative).read_text(encoding="utf-8")
    return all(needle in text for needle in needles)


def check_db(path: Path) -> tuple[bool, str]:
    if not path.is_file():
        return True, "відсутня — буде створена ботом"
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        result = conn.execute("PRAGMA integrity_check").fetchone()
        value = str(result[0]) if result else "невідомо"
        return value.lower() == "ok", value
    finally:
        conn.close()


def version_tuple(value: str) -> tuple[int, ...]:
    numbers = re.findall(r"\d+", value)
    return tuple(int(item) for item in numbers[:4])


def main() -> int:
    args = parse_args()
    failures: list[str] = []

    print("=== FILES / DIRECTORIES ===")
    for relative in REQUIRED_FILES:
        path = BASE_DIR / relative
        if path.is_file():
            ok(f"file:{relative}")
        else:
            fail(f"відсутній файл: {relative}", failures)
    for relative in REQUIRED_DIRS:
        path = BASE_DIR / relative
        if path.is_dir():
            ok(f"dir:{relative}")
        else:
            fail(f"відсутня папка: {relative}", failures)

    print("\n=== SYNTAX ===")
    if compileall.compile_dir(str(BASE_DIR), quiet=1, force=True):
        ok("compileall")
    else:
        fail("compileall знайшов синтаксичні помилки", failures)

    print("\n=== CROSS-FILE CONTRACTS ===")
    ds_args = class_method_args("services/downloader.py", "DownloadService", "__init__")
    required = {"bot", "config", "database", "youtube", "ffmpeg", "storage"}
    if required <= ds_args:
        ok("DownloadService(storage= + dependencies)")
    else:
        fail(f"DownloadService.__init__ не має параметрів: {sorted(required - ds_args)}", failures)

    dm_args = class_method_args("workers/download_worker.py", "DownloadManager", "__init__")
    required = {"max_concurrent", "max_per_user", "max_global", "max_active_per_user", "dedup_cooldown_seconds"}
    if required <= dm_args:
        ok("DownloadManager security/fairness parameters")
    else:
        fail(f"DownloadManager.__init__ не має параметрів: {sorted(required - dm_args)}", failures)

    search_args = function_args("handlers/search.py", "create_search_router")
    if {"security", "manager"} <= search_args:
        ok("create_search_router(security=, manager=)")
    else:
        fail("create_search_router не синхронізований із Security v3", failures)

    if source_has(
        "bot.py",
        "tasks_concurrency_limit=config.POLLING_TASKS_CONCURRENCY_LIMIT",
        "SingleInstanceLock",
        "SecurityMiddleware",
        "create_security_router",
        "EFFECTIVE_MAX_QUEUE_SIZE",
        "EFFECTIVE_MAX_GLOBAL_QUEUE_SIZE",
    ):
        ok("bot.py Security v3 integration")
    else:
        fail("bot.py не містить повну Security v3 integration", failures)

    if source_has(
        "restart_bot.py",
        "--wait-create-time",
        "restart_child.log",
        "CREATE_NEW_PROCESS_GROUP",
        "DETACHED_PROCESS",
    ) and source_has("handlers/admin.py", "--wait-create-time", "DETACHED_PROCESS"):
        ok("detached /restart + PID/create_time protection")
    else:
        fail("restart chain не синхронізований", failures)

    if source_has(
        "database.py",
        "security_user_state",
        "security_allowlist",
        "security_settings",
        "MAX_SEARCH_SESSIONS_PER_USER",
    ):
        ok("SQLite security migrations + search session cap")
    else:
        fail("database.py не містить усі security migrations", failures)

    if source_has(
        "services/downloader.py",
        "media_maintenance_guard",
        ":split",
        "reserve_exact",
    ) and source_has("workers/cleanup.py", "media_maintenance_guard"):
        ok("media lock + Cleaner race protection + FFmpeg split reservation")
    else:
        fail("media maintenance/storage hardening неповний", failures)

    if source_has("handlers/downloads.py", "доступні лише адміністратору"):
        ok("/music і /video hard-admin-only")
    else:
        fail("не підтверджено hard-admin-only для /music і /video", failures)

    print("\n=== SOURCE SECRET SCAN ===")
    secret_hits: list[str] = []
    for path in BASE_DIR.rglob("*.py"):
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if TOKEN_LIKE_RE.search(text):
            secret_hits.append(str(path.relative_to(BASE_DIR)))
    if secret_hits:
        fail(f"схожий на Telegram token текст у source: {secret_hits}", failures)
    else:
        ok("у Python source немає token-like literals")

    print("\n=== DATABASE INTEGRITY (READ-ONLY) ===")
    for name in ("users.db", "songs.db", "search_results.db"):
        good, detail = check_db(BASE_DIR / name)
        if good:
            ok(f"{name}: {detail}")
        else:
            fail(f"{name}: integrity_check={detail}", failures)

    print("\n=== DEPENDENCIES ===")
    if args.skip_deps:
        warn("перевірка залежностей пропущена (--skip-deps)")
    else:
        dependencies = (
            ("aiogram", (3, 20)),
            ("aiosqlite", (0, 20)),
            ("python-dotenv", (1, 0)),
            ("psutil", (5, 9)),
            ("yt-dlp", (2026, 8, 19)),
            ("yt-dlp-ejs", (0, 8)),
        )
        for dist_name, minimum in dependencies:
            try:
                version = importlib.metadata.version(dist_name)
            except importlib.metadata.PackageNotFoundError:
                fail(f"не встановлено dependency: {dist_name}", failures)
                continue
            if version_tuple(version) < minimum:
                fail(f"{dist_name}={version}, потрібно >= {'.'.join(map(str, minimum))}", failures)
            else:
                ok(f"{dist_name}={version}")

        try:
            from aiogram import Dispatcher
            if "tasks_concurrency_limit" in inspect.signature(Dispatcher.start_polling).parameters:
                ok("aiogram Dispatcher.start_polling(tasks_concurrency_limit=)")
            else:
                fail("aiogram не підтримує tasks_concurrency_limit", failures)
        except Exception as exc:
            fail(f"не вдалося імпортувати aiogram Dispatcher: {exc}", failures)

    print("\n=== CONFIG ===")
    if (BASE_DIR / ".env").is_file():
        try:
            from config import Config
            config = Config.load(BASE_DIR)
            config.validate_limits()
            ok(".env прочитано, Config.validate_limits() = OK (BOT_TOKEN не виводиться)")
            if config.EFFECTIVE_MAX_QUEUE_SIZE <= config.MAX_QUEUE_SIZE:
                ok(f"effective queue/user={config.EFFECTIVE_MAX_QUEUE_SIZE}")
            if config.EFFECTIVE_MAX_GLOBAL_QUEUE_SIZE <= config.MAX_GLOBAL_QUEUE_SIZE:
                ok(f"effective global queue={config.EFFECTIVE_MAX_GLOBAL_QUEUE_SIZE}")
        except Exception as exc:
            fail(f"Config/.env: {exc}", failures)
    else:
        warn(".env не входить у пакет; залиште свій існуючий .env у корені проєкту")

    print("\n=== RESULT ===")
    if failures:
        print(f"INSTALLATION SYNC: FAIL ({len(failures)} проблем)")
        return 1
    print("INSTALLATION SYNC: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
