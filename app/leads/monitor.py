"""Мониторинг заявок (spec-leads §4): фильтр слов → дубли → ИИ → карточка владельцу.

Текст и автор живут только в памяти, пока собирается карточка: в SQLite уходит лишь хэш текста
и счётчики, в логи — ничего.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from html import escape
from zoneinfo import ZoneInfo

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.leads.text import WordFilter, text_hash
from app.llm import LLM, LLMUnavailable
from app.storage import Storage
from app.userbot import Incoming, MessageDetails

log = logging.getLogger(__name__)

QUOTE_LIMIT = 300
BTN_UP = "👍 Заявка"
BTN_DOWN = "👎 Мимо"


@dataclass
class Watched:
    title: str
    link: str


Notify = Callable[[str, InlineKeyboardMarkup], Awaitable[None]]


def message_link(details: MessageDetails, fallback_link: str, chat_id: int, msg_id: int) -> str:
    if details.chat_username:
        return f"https://t.me/{details.chat_username}/{msg_id}"
    if fallback_link:
        return f"{fallback_link.rstrip('/')}/{msg_id}"
    return f"https://t.me/c/{chat_id}/{msg_id}"


def lead_card(
    chat_title: str,
    author_name: str,
    author_username: str,
    summary: str,
    text: str,
    link: str,
    checked: bool = True,
) -> str:
    quote = " ".join(text.split())
    if len(quote) > QUOTE_LIMIT:
        quote = quote[: QUOTE_LIMIT - 1].rstrip() + "…"
    author = escape(author_name)
    if author_username:
        author += f" (@{escape(author_username)})"
    lines = [f"🚚 <b>Заявка</b> · {escape(chat_title)}", f"👤 {author}"]
    if checked and summary:
        lines.append(f"📝 {escape(summary)}")
    if not checked:
        lines.append("⚠️ без проверки ИИ")
    lines.append(f"«{escape(quote)}»")
    lines.append(f'🔗 <a href="{escape(link)}">Открыть сообщение</a>')
    return "\n".join(lines)


def vote_keyboard(chat_id: int, day: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=BTN_UP, callback_data=f"lv:u:{chat_id}:{day}"),
                InlineKeyboardButton(text=BTN_DOWN, callback_data=f"lv:d:{chat_id}:{day}"),
            ]
        ]
    )


def join_wait(
    now: dt.datetime,
    attempts: list[dt.datetime],
    per_day: int,
    pause: float,
    flood_until: dt.datetime | None = None,
) -> float:
    """Сколько секунд ждать до следующего вступления (0 — можно сейчас).

    attempts — время попыток за последние сутки; per_day — лимит в сутки; pause — пауза после
    предыдущей попытки (2–6 минут, выбирается случайно); flood_until — до какого времени Telegram
    велел не делать запросов. При per_day <= 0 вступления выключены (inf).
    """
    if per_day <= 0:
        return float("inf")
    day_ago = now - dt.timedelta(days=1)
    recent = sorted(a for a in attempts if a > day_ago)
    waits = [0.0]
    if flood_until is not None:
        waits.append((flood_until - now).total_seconds())
    if recent:
        waits.append((recent[-1] + dt.timedelta(seconds=pause) - now).total_seconds())
    if len(recent) >= per_day:
        waits.append((recent[-per_day] + dt.timedelta(days=1) - now).total_seconds())
    return max(waits)


class Monitor:
    def __init__(self, storage: Storage, llm: LLM, notify: Notify, tz: str = "Europe/Moscow") -> None:
        self.storage = storage
        self.llm = llm
        self.notify = notify
        self.tz = ZoneInfo(tz)
        self.filter: WordFilter | None = None
        self.watched: dict[int, Watched] = {}

    def today(self) -> str:
        return dt.datetime.now(self.tz).date().isoformat()

    async def handle(self, msg: Incoming) -> bool:
        """Обрабатывает новое сообщение. True — карточка отправлена владельцу."""
        chat = self.watched.get(msg.chat_id)
        if chat is None or self.filter is None or not self.filter.matches(msg.text):
            return False
        if not await self.storage.lead_is_new(text_hash(msg.text)):
            return False
        checked, summary = True, ""
        try:
            lead, summary = await self.llm.classify_lead(msg.text)
        except LLMUnavailable as e:
            log.info("Заявка без проверки ИИ: %s", e)
            checked = False
        else:
            if not lead:
                return False
        details = await msg.details()
        card = lead_card(
            details.chat_title or chat.title,
            details.author_name,
            details.author_username,
            summary,
            msg.text,
            message_link(details, chat.link, msg.chat_id, msg.msg_id),
            checked,
        )
        day = self.today()
        await self.notify(card, vote_keyboard(msg.chat_id, day))
        await self.storage.lead_count(msg.chat_id, day, "leads")
        return True
