"""Отправка медиа поста с кэшем file_id (spec §5)."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import FSInputFile, InputMediaPhoto, InputMediaVideo, Message

from app.content import media_kind
from app.storage import Storage

log = logging.getLogger(__name__)

MAX_FILE = 50 * 1024 * 1024
MAX_PHOTO = 10 * 1024 * 1024
ALBUM_SIZE = 10
UPLOAD_TIMEOUT = 300


@dataclass
class MediaFile:
    name: str
    path: Path
    rel: str  # путь относительно корня репозитория — ключ кэша
    size: int
    kind: str  # photo | video | document
    sha256: str = ""
    file_id: str | None = None


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def plan_media(files: list[MediaFile]) -> tuple[list[list[MediaFile]], list[MediaFile], list[MediaFile]]:
    """Делит файлы на (альбомы/одиночные для фото+видео, документы, слишком большие)."""
    too_big = [f for f in files if f.size > MAX_FILE]
    ok = [f for f in files if f.size <= MAX_FILE]
    docs = [f for f in ok if f.kind == "document"]
    visual = [f for f in ok if f.kind != "document"]
    groups = [visual[i : i + ALBUM_SIZE] for i in range(0, len(visual), ALBUM_SIZE)]
    return groups, docs, too_big


def classify(name: str, size: int) -> str:
    kind = media_kind(name)
    if kind == "photo" and size > MAX_PHOTO:
        return "document"
    return kind


def _extract_file_id(msg: Message) -> str | None:
    if msg.photo:
        return msg.photo[-1].file_id
    if msg.video:
        return msg.video.file_id
    if msg.document:
        return msg.document.file_id
    return None


class MediaSender:
    def __init__(self, bot: Bot, storage: Storage, repos_dir: Path) -> None:
        self.bot = bot
        self.storage = storage
        self.repos_dir = repos_dir

    async def _prepare(self, repo_id: str, post_path: str, names: list[str]) -> list[MediaFile]:
        base = self.repos_dir / repo_id / post_path
        files = []
        for name in names:
            p = base / name
            size = p.stat().st_size
            f = MediaFile(name, p, f"{post_path}/{name}", size, classify(name, size))
            if size <= MAX_FILE:
                f.sha256 = await asyncio.to_thread(_sha256, p)
                f.file_id = await self.storage.get_file_id(repo_id, f.rel, f.sha256)
            files.append(f)
        return files

    def _input(self, f: MediaFile, use_cache: bool) -> str | FSInputFile:
        return f.file_id if (use_cache and f.file_id) else FSInputFile(f.path, filename=f.name)

    async def _send_single(self, chat_id: int, f: MediaFile, use_cache: bool) -> Message:
        media = self._input(f, use_cache)
        if f.kind == "photo":
            return await self.bot.send_photo(chat_id, media, request_timeout=UPLOAD_TIMEOUT)
        if f.kind == "video":
            return await self.bot.send_video(
                chat_id, media, supports_streaming=True, request_timeout=UPLOAD_TIMEOUT
            )
        return await self.bot.send_document(chat_id, media, request_timeout=UPLOAD_TIMEOUT)

    async def _send_group(self, chat_id: int, group: list[MediaFile], use_cache: bool) -> list[Message]:
        if len(group) == 1:
            return [await self._send_single(chat_id, group[0], use_cache)]
        items = [
            InputMediaPhoto(media=self._input(f, use_cache))
            if f.kind == "photo"
            else InputMediaVideo(media=self._input(f, use_cache), supports_streaming=True)
            for f in group
        ]
        return await self.bot.send_media_group(chat_id, items, request_timeout=UPLOAD_TIMEOUT)

    async def _remember(self, repo_id: str, files: list[MediaFile], msgs: list[Message]) -> None:
        for f, m in zip(files, msgs, strict=False):
            fid = _extract_file_id(m)
            if fid and fid != f.file_id:
                await self.storage.put_file_id(repo_id, f.rel, f.sha256, f.kind, fid)
                f.file_id = fid

    async def send(self, chat_id: int, repo_id: str, post_path: str, names: list[str]) -> list[str]:
        """Отправляет медиа. Возвращает предупреждения (например, о слишком больших файлах)."""
        files = await self._prepare(repo_id, post_path, names)
        groups, docs, too_big = plan_media(files)
        for batch in [*groups, *([d] for d in docs)]:
            cached = any(f.file_id for f in batch)
            try:
                msgs = await self._send_group(chat_id, batch, use_cache=True)
            except TelegramBadRequest as e:
                if not cached:
                    raise
                # file_id устарел (например, сменился токен бота) — загружаем заново.
                log.warning("file_id не принят (%s), загружаю заново", e.message)
                msgs = await self._send_group(chat_id, batch, use_cache=False)
            await self._remember(repo_id, batch, msgs)
        return [f"⚠️ {f.name}: {f.size / 1024 / 1024:.1f} МБ — больше 50 МБ, не отправлен" for f in too_big]
