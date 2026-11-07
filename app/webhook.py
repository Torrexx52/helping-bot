"""aiohttp-сервер: GitHub webhook (push) и /health."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from collections.abc import Callable

from aiohttp import web

from app.config import Projects

log = logging.getLogger(__name__)

WEBHOOK_PATH = "/hooks/helping-bot/github"
MAX_BODY = 5 * 1024 * 1024


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def verify_signature(secret: str | None, body: bytes, header: str | None) -> bool:
    if not secret or not header or not header.startswith("sha256="):
        return False
    return hmac.compare_digest(sign(secret, body), header.strip())


def make_app(get_projects: Callable[[], Projects], on_push: Callable[[str], object]) -> web.Application:
    """on_push(repo_id) вызывается после проверки подписи; может вернуть корутину."""
    tasks: set[asyncio.Task] = set()

    async def health(_: web.Request) -> web.Response:
        return web.Response(text="ok")

    async def github(request: web.Request) -> web.Response:
        signature = request.headers.get("X-Hub-Signature-256")
        if not signature:
            return web.Response(status=401, text="signature required")
        body = await request.read()
        try:
            payload = json.loads(body)
            full_name = payload["repository"]["full_name"]
        except (ValueError, KeyError, TypeError):
            return web.Response(status=400, text="bad payload")
        repo = get_projects().repo_by_full_name(str(full_name))
        if repo is None or not verify_signature(repo.webhook_secret, body, signature):
            log.warning("Webhook: отклонён запрос для %s (неизвестный репозиторий или подпись)", full_name)
            return web.Response(status=403, text="forbidden")

        event = request.headers.get("X-GitHub-Event", "")
        if event == "ping":
            return web.Response(text="pong")
        if event != "push":
            return web.Response(status=204)
        ref = payload.get("ref")
        if ref != f"refs/heads/{repo.branch}":
            return web.Response(status=202, text="ignored ref")
        log.info("Webhook: push в %s (%s)", repo.id, payload.get("after", "")[:8])
        result = on_push(repo.id)
        if asyncio.iscoroutine(result):
            task = asyncio.create_task(result)
            tasks.add(task)
            task.add_done_callback(tasks.discard)
        return web.Response(status=202, text="sync scheduled")

    app = web.Application(client_max_size=MAX_BODY)
    app.router.add_get("/health", health)
    app.router.add_post(WEBHOOK_PATH, github)
    return app
