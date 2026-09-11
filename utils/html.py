from __future__ import annotations

from html import escape


def escape_html(value: object) -> str:
    return escape(str(value or ""), quote=True)


def truncate_text(value: object, limit: int) -> str:
    text = str(value or "").strip()
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit == 1:
        return "…"
    return text[: limit - 1].rstrip() + "…"
