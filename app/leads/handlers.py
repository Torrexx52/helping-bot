"""Команды v2: /scout, /watch, /unwatch, /watching, /leadstats, кнопка «🚚 Заявки», оценки 👍/👎."""

from __future__ import annotations

import asyncio
import contextlib
import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Message,
)

from app import keyboards as kb
from app.context import Context
from app.leads.monitor import BTN_DOWN, BTN_UP
from app.render import split_text

log = logging.getLogger(__name__)
router = Router(name="leads")
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)
_tasks: set[asyncio.Task] = set()


async def _reply(message: Message, text: str) -> None:
    for part in split_text(text):
        await message.answer(part, link_preview_options=NO_PREVIEW)


@router.message(Command("leads"))
@router.message(F.text == kb.BTN_LEADS)
async def cmd_leads(message: Message, ctx: Context) -> None:
    await _reply(message, await ctx.leads.panel_text())


SCOUT_STARTED = "🔎 Разведка запущена. Это несколько минут — пришлю итог сообщением."
SCOUT_ANYWAY = InlineKeyboardMarkup(
    inline_keyboard=[[InlineKeyboardButton(text="🔎 Искать всё равно", callback_data="scout:go")]]
)


def _start_scout(ctx: Context) -> None:
    task = asyncio.create_task(ctx.leads.run_scout())
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


@router.message(Command("scout"))
async def cmd_scout(message: Message, ctx: Context) -> None:
    if reason := await ctx.leads.scout_blocker():
        await _reply(message, reason)
        return
    if advice := await ctx.leads.scout_advice():
        await message.answer(advice, reply_markup=SCOUT_ANYWAY)
        return
    _start_scout(ctx)
    await message.answer(SCOUT_STARTED)


@router.callback_query(F.data == "scout:go")
async def cb_scout_go(call: CallbackQuery, ctx: Context) -> None:
    """Кнопка «Искать всё равно»: разведка раньше, чем через сутки, по решению владельца."""
    if isinstance(call.message, Message):
        with contextlib.suppress(TelegramBadRequest):
            await call.message.edit_reply_markup(reply_markup=None)
    if reason := await ctx.leads.scout_blocker():
        await call.answer()
        if isinstance(call.message, Message):
            await _reply(call.message, reason)
        return
    _start_scout(ctx)
    await call.answer("Запускаю")
    if isinstance(call.message, Message):
        await call.message.answer(SCOUT_STARTED)


@router.message(Command("watch"))
async def cmd_watch(message: Message, ctx: Context, command: CommandObject) -> None:
    await _reply(message, await ctx.leads.watch(command.args or ""))


@router.message(Command("unwatch"))
async def cmd_unwatch(message: Message, ctx: Context, command: CommandObject) -> None:
    await _reply(message, await ctx.leads.unwatch(command.args or ""))


@router.message(Command("watching"))
async def cmd_watching(message: Message, ctx: Context) -> None:
    await _reply(message, await ctx.leads.watching_text())


@router.message(Command("leadstats"))
async def cmd_leadstats(message: Message, ctx: Context) -> None:
    await _reply(message, await ctx.leads.leadstats_text())


@router.callback_query(F.data.regexp(r"^lv:([ud]):(-?\d+):(\d{4}-\d{2}-\d{2})$").as_("m"))
async def cb_vote(call: CallbackQuery, ctx: Context, m) -> None:
    up = m.group(1) == "u"
    await ctx.storage.lead_count(int(m.group(2)), m.group(3), "up" if up else "down")
    await call.answer("Учёл 👍" if up else "Учёл 👎")
    if isinstance(call.message, Message):
        done = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text=f"{BTN_UP if up else BTN_DOWN} ✓", callback_data="lv:done")]]
        )
        with contextlib.suppress(TelegramBadRequest):
            await call.message.edit_reply_markup(reply_markup=done)


@router.callback_query(F.data == "lv:done")
async def cb_vote_done(call: CallbackQuery) -> None:
    await call.answer("Оценка уже учтена")
