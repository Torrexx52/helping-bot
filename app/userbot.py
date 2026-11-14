"""Юзербот (Telethon): поиск чатов, сведения о чате, вступление и чтение сообщений.

Только читает (spec-leads §2): здесь нет ни одного вызова, который пишет в чат или в личку,
ставит реакции или приглашает. Не добавляйте их.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from telethon import TelegramClient, errors, events, functions, types, utils

from app.config import Settings

log = logging.getLogger(__name__)

LOGIN_COMMAND = "docker compose run --rm -it bot python -m app.userbot_login"
MANUAL_ERRORS = {
    "InviteRequestSentError": "вход по заявке",
    "ChannelsTooMuchError": "у аккаунта юзербота слишком много чатов",
    "ChannelPrivateError": "чат закрыт или юзербот в нём заблокирован",
    "InviteHashExpiredError": "ссылка недействительна",
    "UserBannedInChannelError": "аккаунту юзербота запрещено вступать",
    "ChannelInvalidError": "чат недоступен",
}


class UserbotError(Exception):
    pass


class FloodWait(UserbotError):
    def __init__(self, seconds: int) -> None:
        super().__init__(f"Telegram просит подождать {seconds} с")
        self.seconds = seconds


class JoinManual(UserbotError):
    """Вступить автоматически нельзя (заявка, капча, закрытый чат) — пометить join=manual."""


@dataclass
class FoundChat:
    tg_id: int
    username: str
    title: str
    kind: str  # group | channel
    members: int | None = None

    @property
    def link(self) -> str:
        return f"https://t.me/{self.username}"


@dataclass
class ChatInfo(FoundChat):
    about: str = ""
    pinned: str = ""
    can_write: bool = False
    slowmode: int = 0
    join_request: bool = False
    linked: str = ""  # юзернейм чата обсуждения у канала
    discussion: bool = False  # группа — чат обсуждения (комментарии) какого-то канала


@dataclass
class MessageDetails:
    chat_title: str
    chat_username: str
    author_name: str
    author_username: str


class Incoming:
    """Новое сообщение. Автора и чат запрашиваем лениво — только если текст прошёл фильтр."""

    def __init__(self, chat_id: int, msg_id: int, text: str, event: object = None) -> None:
        self.chat_id = chat_id
        self.msg_id = msg_id
        self.text = text
        self._event = event

    async def details(self) -> MessageDetails:
        chat = await self._event.get_chat()
        sender = await self._event.get_sender()
        title = getattr(chat, "title", "") or ""
        if isinstance(sender, types.User):
            name = " ".join(p for p in (sender.first_name, sender.last_name) if p) or "Без имени"
            username = sender.username or ""
        else:  # пост канала или анонимный админ
            name = getattr(sender, "title", "") or title
            username = getattr(sender, "username", "") or ""
        return MessageDetails(title, _username(chat) or "", name, username)


Handler = Callable[[Incoming], Awaitable[None]]


def make_client(settings: Settings) -> TelegramClient:
    return TelegramClient(
        str(settings.userbot_session),
        settings.TG_API_ID,
        settings.TG_API_HASH,
        device_model="Helping Bot",
        system_version="Linux",
        app_version="2.0",
        lang_code="ru",
        system_lang_code="ru",
    )


def _username(chat: object) -> str | None:
    name = getattr(chat, "username", None)
    if name:
        return name
    for u in getattr(chat, "usernames", None) or []:
        if getattr(u, "active", False):
            return u.username
    return None


def _found(chat: object) -> FoundChat | None:
    if not isinstance(chat, types.Channel):
        return None
    username = _username(chat)
    if not username or chat.scam or chat.fake or chat.restricted:
        return None
    return FoundChat(
        tg_id=chat.id,
        username=username,
        title=chat.title or "",
        kind="channel" if chat.broadcast else "group",
        members=getattr(chat, "participants_count", None),
    )


class Userbot:
    def __init__(self, settings: Settings) -> None:
        self.session: Path = settings.userbot_session
        self.client = make_client(settings)
        self.me_id = 0
        self._entities: dict[str, types.Channel] = {}
        self._handler: Handler | None = None

    async def start(self) -> bool:
        """Подключается по готовой сессии. False — сессия не авторизована (нужен userbot_login)."""
        await asyncio.wait_for(self.client.connect(), 60)
        if not await self.client.is_user_authorized():
            await self.client.disconnect()
            return False
        me = await self.client.get_me()
        self.me_id = me.id
        self.client.add_event_handler(self._on_new, events.NewMessage(incoming=True))
        log.info("Юзербот подключён (id %s)", me.id)
        return True

    async def stop(self) -> None:
        await self.client.disconnect()

    def set_handler(self, handler: Handler) -> None:
        self._handler = handler

    async def _on_new(self, event: events.NewMessage.Event) -> None:
        try:
            if event.is_private or self._handler is None:
                return
            text = event.message.raw_text or ""
            if not text:
                return
            chat_id, _ = utils.resolve_id(event.chat_id)
            await self._handler(Incoming(chat_id, event.message.id, text, event))
        except Exception:  # noqa: BLE001 — обработчик не должен ронять юзербота; текст в лог не пишем
            log.exception("Ошибка обработки сообщения из чата")

    async def _call(self, request: object) -> object:
        try:
            return await self.client(request)
        except errors.FloodWaitError as e:
            raise FloodWait(e.seconds) from e
        except errors.RPCError as e:
            raise UserbotError(type(e).__name__) from e

    async def _entity(self, username: str) -> types.Channel:
        cached = self._entities.get(username.lower())
        if cached is not None:
            return cached
        try:
            entity = await self.client.get_entity(username)
        except errors.FloodWaitError as e:
            raise FloodWait(e.seconds) from e
        except (ValueError, errors.RPCError) as e:
            raise UserbotError("чат не найден") from e
        if not isinstance(entity, types.Channel):
            raise UserbotError("это не группа и не канал")
        self._entities[username.lower()] = entity
        return entity

    # ---------- разведка ----------

    async def search(self, query: str, limit: int = 50) -> list[FoundChat]:
        """Публичные чаты и каналы по названию и юзернейму (contacts.Search)."""
        res = await self._call(functions.contacts.SearchRequest(q=query, limit=limit))
        out = []
        for chat in res.chats:
            found = _found(chat)
            if found is not None:
                self._entities[found.username.lower()] = chat
                out.append(found)
        return out

    async def chat_info(self, username: str) -> ChatInfo:
        """Всё, что видно без вступления: описание, закреп, права, медленный режим."""
        entity = await self._entity(username)
        full = await self._call(functions.channels.GetFullChannelRequest(channel=entity))
        fc = full.full_chat
        chat = next((c for c in full.chats if c.id == fc.id), entity)
        base = _found(chat) or _found(entity)
        if base is None:
            raise UserbotError("чат недоступен")
        pinned = ""
        if fc.pinned_msg_id:
            try:
                msg = await self.client.get_messages(chat, ids=fc.pinned_msg_id)
                pinned = (getattr(msg, "message", "") or "") if msg else ""
            except errors.FloodWaitError as e:
                raise FloodWait(e.seconds) from e
            except (ValueError, errors.RPCError):
                pinned = ""
        linked = ""
        if fc.linked_chat_id:
            linked = next((_username(c) or "" for c in full.chats if c.id == fc.linked_chat_id), "")
        banned = getattr(chat, "default_banned_rights", None)
        return ChatInfo(
            tg_id=base.tg_id,
            username=base.username,
            title=base.title,
            kind=base.kind,
            members=fc.participants_count or base.members,
            about=fc.about or "",
            pinned=pinned,
            can_write=base.kind == "group" and not (banned is not None and banned.send_messages),
            slowmode=fc.slowmode_seconds or 0,
            join_request=bool(getattr(chat, "join_request", False)),
            linked=linked,
            discussion=base.kind == "group" and bool(fc.linked_chat_id),
        )

    # ---------- вступление ----------

    async def join(self, username: str) -> FoundChat:
        entity = await self._entity(username)
        if getattr(entity, "join_request", False):
            raise JoinManual("вход по заявке")
        try:
            await self.client(functions.channels.JoinChannelRequest(entity))
        except errors.FloodWaitError as e:
            raise FloodWait(e.seconds) from e
        except errors.RPCError as e:
            name = type(e).__name__
            if name in MANUAL_ERRORS:
                raise JoinManual(MANUAL_ERRORS[name]) from e
            raise UserbotError(name) from e
        found = _found(entity)
        if found is None:
            raise UserbotError("чат недоступен")
        return found

    async def leave(self, username: str) -> None:
        entity = await self._entity(username)
        try:
            await self.client(functions.channels.LeaveChannelRequest(entity))
        except errors.FloodWaitError as e:
            raise FloodWait(e.seconds) from e
        except errors.UserNotParticipantError:
            return
        except errors.RPCError as e:
            raise UserbotError(type(e).__name__) from e

    async def joined_ids(self) -> set[int]:
        """id групп и каналов, в которых юзербот уже состоит (в том числе вступил вручную)."""
        try:
            dialogs = await self.client.get_dialogs()
        except errors.FloodWaitError as e:
            raise FloodWait(e.seconds) from e
        except errors.RPCError as e:
            raise UserbotError(type(e).__name__) from e
        return {d.entity.id for d in dialogs if isinstance(d.entity, types.Channel) and not d.entity.left}
