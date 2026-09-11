from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler

from config import Config


_TOKEN_LIKE_RE = re.compile(r"(?<![A-Za-z0-9_])\d{6,12}:[A-Za-z0-9_-]{20,}(?![A-Za-z0-9_-])")


class RedactingFormatter(logging.Formatter):
    """Маскує BOT_TOKEN вже у повністю сформованому повідомленні/traceback."""

    def __init__(self, fmt: str, *, bot_token: str) -> None:
        super().__init__(fmt)
        self._bot_token = bot_token

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        if self._bot_token:
            text = text.replace(self._bot_token, "***BOT_TOKEN***")
        return _TOKEN_LIKE_RE.sub("***BOT_TOKEN***", text)


def configure_logging(config: Config) -> None:
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(logging.INFO)

    for handler in list(root.handlers):
        root.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass

    formatter = RedactingFormatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        bot_token=config.BOT_TOKEN,
    )

    file_handler = RotatingFileHandler(
        config.LOG_DIR / "bot.log",
        maxBytes=config.LOG_MAX_BYTES,
        backupCount=config.LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    root.addHandler(file_handler)
    root.addHandler(console_handler)

    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
