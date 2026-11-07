"""Клавиатуры и тексты кнопок."""

from __future__ import annotations

from aiogram.types import (
    CopyTextButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from app.config import Projects
from app.content import Page, truncate

BTN_REFRESH_ALL = "🔄 Обновить всё"
BTN_REFRESH = "🔄 Обновить"
BTN_MENU = "🏠 Меню"
BTN_PREV = "◀️ Назад"
BTN_NEXT = "▶️ Далее"
BTN_PUBLISHED = "✅ Опубликовал"
BTN_POSTPONE = "⏸ Отложить"
BTN_COPY = "📋 Копировать"
COPY_LIMIT = 256
TITLE_LIMIT = 24


def main_menu(projects: Projects) -> ReplyKeyboardMarkup:
    rows = [[KeyboardButton(text=a.title)] for a in projects.accounts]
    rows.append([KeyboardButton(text=BTN_REFRESH_ALL)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, is_persistent=True)


def item_button_text(number: int, title: str) -> str:
    return f"{number} · {truncate(title, TITLE_LIMIT)}"


def queue_keyboard(page: Page) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text=item_button_text(page.offset + i + 1, it.title))]
        for i, it in enumerate(page.items)
    ]
    nav = []
    if page.has_prev:
        nav.append(KeyboardButton(text=BTN_PREV))
    if page.has_next:
        nav.append(KeyboardButton(text=BTN_NEXT))
    if nav:
        rows.append(nav)
    rows.append([KeyboardButton(text=BTN_REFRESH), KeyboardButton(text=BTN_MENU)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, is_persistent=True)


def card_keyboard(post_id: int, caption: str, with_actions: bool = True) -> InlineKeyboardMarkup | None:
    rows: list[list[InlineKeyboardButton]] = []
    if len(caption) <= COPY_LIMIT:
        rows.append([InlineKeyboardButton(text=BTN_COPY, copy_text=CopyTextButton(text=caption))])
    if with_actions:
        rows.append(
            [
                InlineKeyboardButton(text=BTN_PUBLISHED, callback_data=f"pub:{post_id}"),
                InlineKeyboardButton(text=BTN_POSTPONE, callback_data=f"pp:{post_id}"),
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def published_keyboard(items: list[dict]) -> InlineKeyboardMarkup | None:
    if not items:
        return None
    rows = [
        [
            InlineKeyboardButton(
                text=f"↩️ {i + 1} · {truncate(it['title'], 28)}", callback_data=f"unpub:{it['id']}"
            )
        ]
        for i, it in enumerate(items)
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)
