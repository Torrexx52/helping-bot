"""Сборка и запуск: Telegram long polling + aiohttp webhook + фоновый опрос в одном процессе."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand, BotCommandScopeChat
from aiohttp import web

from app.access import AccessMiddleware, AccessPolicy
from app.config import ConfigError, Settings, load_projects
from app.context import Context
from app.handlers import router
from app.storage import Storage
from app.sync import Redactor, Syncer
from app.webhook import make_app

log = logging.getLogger("helping_bot")

COMMANDS = [
    BotCommand(command="start", description="Меню аккаунтов"),
    BotCommand(command="published", description="Последние опубликованные"),
    BotCommand(command="errors", description="Посты с ошибками"),
    BotCommand(command="reload", description="Перечитать projects.yml"),
    BotCommand(command="help", description="Справка"),
]


class RedactingFilter(logging.Filter):
    def __init__(self, redact: Redactor) -> None:
        super().__init__()
        self.redact = redact

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        clean = self.redact(msg)
        if clean != msg:
            record.msg, record.args = clean, None
        return True


def setup_logging(settings: Settings) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(RedactingFilter(Redactor(settings.BOT_TOKEN, settings.GITHUB_TOKEN)))
    logging.basicConfig(level=settings.LOG_LEVEL.upper(), handlers=[handler], force=True)
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)


def build_dispatcher(ctx: Context) -> Dispatcher:
    dp = Dispatcher(ctx=ctx)
    access = AccessMiddleware(AccessPolicy(ctx.settings.OWNER_TELEGRAM_ID))
    dp.message.outer_middleware(access)
    dp.callback_query.outer_middleware(access)
    dp.include_router(router)
    return dp


async def run() -> None:
    settings = Settings()
    setup_logging(settings)
    try:
        projects = load_projects(settings.PROJECTS_FILE)
    except ConfigError as e:
        log.error("%s", e)
        raise SystemExit(2) from e
    for repo in projects.repos:
        if repo.webhook_secret_env and not repo.webhook_secret:
            log.warning("Для %s не задан %s — webhook будет отклоняться", repo.id, repo.webhook_secret_env)
    if not settings.OWNER_TELEGRAM_ID:
        log.warning("OWNER_TELEGRAM_ID не задан — бот никому не ответит")

    storage = Storage(settings.DATA_DIR / "bot.db")
    await storage.open()
    syncer = Syncer(storage, projects, settings.DATA_DIR, settings.GITHUB_TOKEN)
    ctx = Context(settings, projects, storage, syncer)

    app = make_app(lambda: ctx.projects, lambda repo_id: _sync_one(ctx, repo_id))
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, settings.WEBHOOK_HOST, settings.WEBHOOK_PORT).start()
    log.info("HTTP: %s:%s (/health, webhook)", settings.WEBHOOK_HOST, settings.WEBHOOK_PORT)

    background = [
        asyncio.create_task(syncer.sync_all("startup")),
        asyncio.create_task(syncer.poll_forever(settings.SYNC_INTERVAL_MIN)),
    ]
    try:
        if settings.BOT_TOKEN:
            bot = Bot(settings.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
            dp = build_dispatcher(ctx)
            if settings.OWNER_TELEGRAM_ID:
                with contextlib.suppress(Exception):
                    await bot.set_my_commands(
                        COMMANDS, scope=BotCommandScopeChat(chat_id=settings.OWNER_TELEGRAM_ID)
                    )
            me = await bot.get_me()
            log.info("Telegram: @%s, long polling", me.username)
            await dp.start_polling(bot, handle_signals=True)
        else:
            log.warning("BOT_TOKEN не задан — работают только синхронизация, webhook и /health")
            await asyncio.Event().wait()
    finally:
        for t in background:
            t.cancel()
        await runner.cleanup()
        await storage.close()


async def _sync_one(ctx: Context, repo_id: str) -> None:
    repo = ctx.projects.repo(repo_id)
    if repo is not None:
        await ctx.syncer.sync_repo(repo, "webhook")


def main() -> None:
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run())
