"""Хендлеры Telegram: меню, очередь, карточка поста, статусы, служебные команды."""

from __future__ import annotations

import contextlib
import logging
import re
from html import escape

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message

from app import keyboards as kb
from app.config import ConfigError
from app.content import paginate, sort_queue
from app.context import Context, View
from app.media import MediaSender
from app.render import caption_text, fmt_ts, info_text, queue_text, split_text

log = logging.getLogger(__name__)
router = Router(name="main")

ITEM_RE = re.compile(r"^(\d+) · ")


# ---------- общие функции ----------


async def show_menu(message: Message, ctx: Context, text: str = "Выбери аккаунт 👇") -> None:
    await message.answer(text, reply_markup=kb.main_menu(ctx.projects))


async def show_queue(message: Message, ctx: Context, account_id: str, page: int = 1) -> None:
    account = ctx.projects.account(account_id)
    if account is None:
        await show_menu(message, ctx, "Аккаунт больше не настроен. Выбери другой 👇")
        return
    items = sort_queue(await ctx.storage.queue(account_id))
    pg = paginate(items, page)
    ctx.views[message.chat.id] = View(
        account_id, pg.number, {pg.offset + i + 1: it.post_id for i, it in enumerate(pg.items)}
    )
    text = queue_text(account, pg, await ctx.storage.error_count())
    await message.answer(text, reply_markup=kb.queue_keyboard(pg))


def sync_summary(results) -> str:
    if not results:
        return "Репозитории не настроены."
    lines = []
    for r in results:
        if r.ok:
            lines.append(f"✅ {escape(r.repo_id)}: постов {r.posts}, ошибок {r.errors}, новых {r.added}")
        else:
            lines.append(f"❌ {escape(r.repo_id)}: {escape(r.message[:300])}")
    return "\n".join(lines)


# ---------- команды ----------


@router.message(CommandStart())
@router.message(F.text == kb.BTN_MENU)
async def cmd_start(message: Message, ctx: Context) -> None:
    ctx.views.pop(message.chat.id, None)
    await show_menu(message, ctx)


@router.message(Command("help"))
async def cmd_help(message: Message, ctx: Context) -> None:
    await message.answer(
        "Выбери аккаунт в меню → номер поста → получишь медиа, описание и подпись.\n\n"
        "/published — последние опубликованные\n/errors — посты с ошибками\n"
        "/reload — перечитать projects.yml и синхронизировать\n/start — меню",
        reply_markup=kb.main_menu(ctx.projects),
    )


@router.message(Command("errors"))
async def cmd_errors(message: Message, ctx: Context) -> None:
    errors = await ctx.storage.errors()
    sync_fail = [s for s in await ctx.storage.sync_status() if s["last_error"]]
    if not errors and not sync_fail:
        await message.answer("Ошибок нет ✅")
        return
    lines = []
    if sync_fail:
        lines.append("<b>Синхронизация</b>")
        lines += [f"• {escape(s['repo_id'])}: {escape(s['last_error'][:300])}" for s in sync_fail]
        lines.append("")
    if errors:
        lines.append(f"<b>Посты с ошибками: {len(errors)}</b>")
        lines += [
            f"• <code>{escape(e['repo_id'])}/{escape(e['path'])}</code> — {escape(e['reason'])}"
            for e in errors
        ]
    for part in split_text("\n".join(lines)):
        await message.answer(part)


@router.message(Command("reload"))
async def cmd_reload(message: Message, ctx: Context) -> None:
    try:
        ctx.reload_projects()
    except ConfigError as e:
        await message.answer(f"❌ Конфиг не перечитан:\n<pre>{escape(str(e))[:3500]}</pre>")
        return
    results = await ctx.syncer.sync_all("reload")
    ctx.views.pop(message.chat.id, None)
    await show_menu(message, ctx, "🔁 projects.yml перечитан\n" + sync_summary(results))


@router.message(Command("published"))
async def cmd_published(message: Message, ctx: Context) -> None:
    text, markup = await published_view(ctx)
    await message.answer(text, reply_markup=markup)


async def published_view(ctx: Context):
    items = await ctx.storage.published(20)
    if not items:
        return "Опубликованных постов пока нет.", None
    lines = ["<b>Последние опубликованные</b>"]
    for i, it in enumerate(items, start=1):
        acc = ctx.projects.account(it["account"])
        acc_title = acc.title if acc else it["account"]
        lines.append(
            f"{i}. {escape(it['title'])} — {fmt_ts(it['published_at'], ctx.settings.TZ)} · {escape(acc_title)}"
        )
    return "\n".join(lines), kb.published_keyboard(items)


# ---------- кнопки нижней клавиатуры ----------


@router.message(F.text == kb.BTN_REFRESH_ALL)
async def refresh_all(message: Message, ctx: Context) -> None:
    await message.answer("🔄 Синхронизирую…")
    results = await ctx.syncer.sync_all("button")
    await show_menu(message, ctx, sync_summary(results))


@router.message(F.text == kb.BTN_REFRESH)
async def refresh(message: Message, ctx: Context) -> None:
    view = ctx.views.get(message.chat.id)
    results = await ctx.syncer.sync_all("button")
    failed = [r for r in results if not r.ok]
    if failed:
        await message.answer(sync_summary(failed))
    if view is None:
        await show_menu(message, ctx, "Обновлено ✅")
    else:
        await show_queue(message, ctx, view.account_id, view.page)


@router.message(F.text.in_({kb.BTN_PREV, kb.BTN_NEXT}))
async def paginate_queue(message: Message, ctx: Context) -> None:
    view = ctx.views.get(message.chat.id)
    if view is None:
        await show_menu(message, ctx)
        return
    delta = -1 if message.text == kb.BTN_PREV else 1
    await show_queue(message, ctx, view.account_id, view.page + delta)


@router.message(F.text.regexp(ITEM_RE))
async def open_post(message: Message, ctx: Context, bot: Bot) -> None:
    view = ctx.views.get(message.chat.id)
    m = ITEM_RE.match(message.text or "")
    post_id = view.numbers.get(int(m.group(1))) if (view and m) else None
    if post_id is None:
        await show_menu(message, ctx, "Очередь устарела — открой аккаунт заново 👇")
        return
    await send_card(message, ctx, bot, post_id)


@router.message(F.text)
async def by_text(message: Message, ctx: Context) -> None:
    account = next((a for a in ctx.projects.accounts if a.title == message.text), None)
    if account is not None:
        await show_queue(message, ctx, account.id)
        return
    await show_menu(message, ctx, "Не понял. Выбери аккаунт 👇")


# ---------- карточка поста ----------


async def send_card(message: Message, ctx: Context, bot: Bot, post_id: int) -> None:
    post = await ctx.storage.get_post(post_id)
    if post is None:
        await message.answer("Пост не найден — возможно, его удалили из репозитория. Обнови очередь.")
        return
    account = ctx.projects.account(post["account"])
    if account is None:
        await message.answer("Аккаунт поста не настроен.")
        return
    sender = MediaSender(bot, ctx.storage, ctx.syncer.repos_dir)
    try:
        warnings = await sender.send(message.chat.id, post["repo_id"], post["path"], post["media"])
    except FileNotFoundError:
        await message.answer("⚠️ Медиафайлы не найдены на сервере — нажми «🔄 Обновить».")
        return
    except TelegramBadRequest as e:
        log.exception("Ошибка отправки медиа поста %s", post_id)
        warnings = [f"⚠️ Не удалось отправить медиа: {escape(e.message)}"]
    for w in warnings:
        await message.answer(w)

    for part in split_text(info_text(post["title"], post["date"], post["sections"], account)):
        await message.answer(part)

    text = caption_text(post["caption"], account)
    markup = kb.card_keyboard(post_id, post["caption"])
    if len(text) <= 4096:
        await message.answer(text, reply_markup=markup)
    else:
        parts = split_text(escape(post["caption"]))
        for p in parts[:-1]:
            await message.answer(p)
        await message.answer(parts[-1], reply_markup=markup)


async def _after_action(call: CallbackQuery, ctx: Context, post: dict, note: str) -> None:
    msg = call.message
    if isinstance(msg, Message):
        with contextlib.suppress(TelegramBadRequest):
            await msg.edit_reply_markup(reply_markup=kb.card_keyboard(post["id"], post["caption"], False))
        await msg.answer(note)
        view = ctx.views.get(msg.chat.id)
        page = view.page if view and view.account_id == post["account"] else 1
        await show_queue(msg, ctx, post["account"], page)


@router.callback_query(F.data.startswith("pub:"))
async def cb_published(call: CallbackQuery, ctx: Context) -> None:
    post = await ctx.storage.get_post(int(call.data.split(":", 1)[1]))
    if post is None:
        await call.answer("Пост не найден", show_alert=True)
        return
    await ctx.storage.mark_published(post["repo_id"], post["path"])
    await call.answer("Отмечено ✅")
    await _after_action(call, ctx, post, "Отмечено ✅")


@router.callback_query(F.data.startswith("pp:"))
async def cb_postpone(call: CallbackQuery, ctx: Context) -> None:
    post = await ctx.storage.get_post(int(call.data.split(":", 1)[1]))
    if post is None:
        await call.answer("Пост не найден", show_alert=True)
        return
    await ctx.storage.postpone(post["repo_id"], post["path"])
    await call.answer("Отложено ⏸")
    await _after_action(call, ctx, post, "Отложено ⏸ — пост в конце очереди")


@router.callback_query(F.data.startswith("unpub:"))
async def cb_unpublish(call: CallbackQuery, ctx: Context) -> None:
    post = await ctx.storage.get_post(int(call.data.split(":", 1)[1]))
    if post is None:
        await call.answer("Пост не найден", show_alert=True)
        return
    await ctx.storage.unpublish(post["repo_id"], post["path"])
    await call.answer("↩️ Возвращён в очередь")
    if isinstance(call.message, Message):
        text, markup = await published_view(ctx)
        with contextlib.suppress(TelegramBadRequest):
            await call.message.edit_text(text, reply_markup=markup)
