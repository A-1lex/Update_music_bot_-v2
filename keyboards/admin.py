from __future__ import annotations

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardMarkup, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

SECURITY_BUTTONS = {
    "🛡 Статус безпеки": "security_status",
    "🔐 Режим доступу": "security_mode",
    "🚨 Закрити доступ усім, крім адміна": "lockdown",
    "⛔ Заблокувати користувача": "ban",
    "✅ Розблокувати користувача": "unban",
    "➕ Додати в білий список": "allow",
    "➖ Видалити з білого списку": "disallow",
}


class TablePaginationCallbackData(CallbackData, prefix="table_page"):
    action: str
    user_id: int
    table_name: str


class DeleteFileCallbackData(CallbackData, prefix="delete_file"):
    token: str
    index: int


class BulkDeleteCallbackData(CallbackData, prefix="bulk_delete"):
    action: str
    media_type: str
    token: str


class TableSelectCallbackData(CallbackData, prefix="table_select"):
    table_name: str


def build_main_keyboard(
    is_admin: bool,
    *,
    collect_user_contact_data: bool = False,
) -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()

    if collect_user_contact_data:
        builder.button(
            text="Поділитися номером телефону",
            request_contact=True,
        )
        builder.button(
            text="Поділитися місцем розташування",
            request_location=True,
        )

    # Пошук потрібний і звичайному користувачу, і адміністратору.
    builder.button(text="🔎 Пошук")

    if is_admin:
        builder.button(text="Перезапустити бота")
        builder.button(text="Видалити файл")
        builder.button(text="Показати таблиці")
        builder.button(text="Статус бота")
        for label in SECURITY_BUTTONS:
            builder.button(text=label)

    builder.adjust(1)
    return builder.as_markup(resize_keyboard=True, one_time_keyboard=False)


def build_table_select_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for table_name in ("users", "downloads", "search_results"):
        builder.button(
            text=f"Таблиця {table_name}",
            callback_data=TableSelectCallbackData(table_name=table_name).pack(),
        )
    builder.adjust(1)
    return builder.as_markup()
