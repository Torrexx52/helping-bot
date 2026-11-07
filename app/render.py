"""Тексты сообщений (HTML parse_mode). Чистые функции — легко тестировать."""

from __future__ import annotations

import contextlib
import datetime as dt
from html import escape
from zoneinfo import ZoneInfo

from app.config import Account
from app.content import Page, Section, media_badge, plural
from app.platforms import PLATFORMS

TG_LIMIT = 4096


def fmt_short_date(d: dt.date | None) -> str:
    return d.strftime("%d.%m") if d else "без даты"


def fmt_ts(iso: str | None, tz: str) -> str:
    if not iso:
        return ""
    t = dt.datetime.fromisoformat(iso)
    with contextlib.suppress(Exception):  # неизвестный TZ: показываем как есть
        t = t.astimezone(ZoneInfo(tz))
    return t.strftime("%d.%m %H:%M")


def queue_text(account: Account, page: Page, error_count: int) -> str:
    head = f"{escape(account.title)} — к публикации {page.total_items}"
    if page.total_pages > 1:
        head += f" (стр. {page.number}/{page.total_pages})"
    lines = [f"<b>{head}</b>"]
    if not page.items:
        lines.append("Очередь пуста 🎉")
    for i, it in enumerate(page.items, start=page.offset + 1):
        mark = " ⏸" if it.postponed_at else ""
        lines.append(f"{i}. {fmt_short_date(it.date)} · {escape(it.title)} {media_badge(it.media)}{mark}")
    if error_count:
        word = plural(error_count, "пост", "поста", "постов")
        lines.append("")
        lines.append(f"⚠️ {error_count} {word} с ошибками — /errors")
    return "\n".join(lines)


def split_text(text: str, limit: int = TG_LIMIT) -> list[str]:
    """Режет длинный текст по строкам, чтобы не превысить лимит Telegram."""
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    buf = ""
    for line in text.split("\n"):
        while len(line) > limit:
            if buf:
                parts.append(buf)
                buf = ""
            parts.append(line[:limit])
            line = line[limit:]
        candidate = f"{buf}\n{line}" if buf else line
        if len(candidate) > limit:
            parts.append(buf)
            buf = line
        else:
            buf = candidate
    if buf:
        parts.append(buf)
    return parts


def info_text(title: str, date: dt.date | None, sections: list[Section], account: Account) -> str:
    lines = [f"<b>{escape(title)}</b>", f"📅 {date.strftime('%d.%m.%Y') if date else 'без даты'}"]
    lines.append(f"👤 {escape(account.title)}")
    for s in sections:
        lines.append("")
        lines.append(f"<b>{escape(s.title)}</b>")
        if s.body:
            lines.append(escape(s.body))
    return "\n".join(lines)


def caption_text(caption: str, account: Account) -> str:
    label = PLATFORMS[account.platform].caption_label
    return f"<b>{escape(label)}</b>\n<pre>{escape(caption)}</pre>"
