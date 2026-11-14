from __future__ import annotations

import datetime as dt
import shutil

import pytest

from app.bot import build_dispatcher
from app.config import Projects, Settings
from app.context import Context
from app.keyboards import BTN_LEADS, main_menu
from app.leads import table as T
from app.leads.service import Leads
from app.storage import Storage
from app.userbot import (
    ChatInfo,
    FloodWait,
    FoundChat,
    Incoming,
    JoinManual,
    MessageDetails,
    Userbot,
    UserbotError,
)
from tests.conftest import ROOT

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="нужен git")

PROJECTS = Projects.model_validate(
    {"accounts": [{"id": "tiktok-esoteric", "platform": "tiktok", "title": "🔮 TikTok"}], "repos": []}
)
OPEN = ChatInfo(tg_id=1001, username="lazurny_vrn", title="ЖК Лазурный Воронеж", kind="group", members=900, can_write=True)
CLOSED = ChatInfo(tg_id=1002, username="closed_vrn", title="Закрытый Воронеж", kind="group", join_request=True)


class FakeUserbot:
    def __init__(self) -> None:
        self.chats = {c.username: c for c in (OPEN, CLOSED)}
        self.joined: set[int] = set()
        self.join_error: Exception | None = None
        self.left: list[str] = []

    async def chat_info(self, username: str) -> ChatInfo:
        if username not in self.chats:
            raise UserbotError("чат не найден")
        return self.chats[username]

    async def join(self, username: str) -> FoundChat:
        if self.join_error is not None:
            raise self.join_error
        chat = self.chats[username]
        if chat.join_request:
            raise JoinManual("вход по заявке")
        self.joined.add(chat.tg_id)
        return chat

    async def leave(self, username: str) -> None:
        self.left.append(username)
        self.joined.discard(self.chats[username].tg_id)

    async def joined_ids(self) -> set[int]:
        return set(self.joined)


class Msg(Incoming):
    async def details(self) -> MessageDetails:
        return MessageDetails("ЖК Лазурный Воронеж", "lazurny_vrn", "Иван", "ivan_vrn")


def settings_for(tmp_path, monkeypatch, **env: str) -> Settings:
    monkeypatch.chdir(tmp_path)  # без .env
    for name in ("TG_API_ID", "TG_API_HASH", "LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL", "SCOUT_CRON"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return Settings(
        DATA_DIR=tmp_path / "data", LEADS_FILE=ROOT / "config" / "leads.example.yml", OWNER_TELEGRAM_ID=111
    )


@pytest.fixture
async def leads(tmp_path, monkeypatch):
    storage = Storage(tmp_path / "data" / "bot.db")
    await storage.open()
    service = Leads(settings_for(tmp_path, monkeypatch, TG_API_ID="1", TG_API_HASH="h"), storage)
    service.load_config()
    service.userbot = FakeUserbot()
    service.userbot_note = "подключён"
    service.sent = []

    async def send(text, markup=None):
        service.sent.append(text)

    service._send = send
    service.monitor.notify = send
    yield service
    await service.stop()
    await storage.close()


async def csv_row(leads: Leads, link: str) -> dict[str, str]:
    table, _ = await leads.repo.read()
    return table.find(link)


async def test_watch_join_monitor_unwatch(leads):
    link = "https://t.me/lazurny_vrn"
    text = await leads.watch("@lazurny_vrn")
    assert "вступлю по очереди" in text and "лимит 15" in text
    row = await csv_row(leads, link)
    assert (row[T.MONITOR], row[T.JOINED], row[T.NAME], row[T.STATUS]) == ("yes", "", OPEN.title, "Не трогали")
    assert (row[T.TG_ID], row[T.QUERY]) == ("1001", "/watch")

    # Пока не вступил — сообщения из чата не слушаем
    assert leads.monitor.watched == {}
    await leads._join_step()
    assert leads.userbot.joined == {1001}
    assert (await csv_row(leads, link))[T.JOINED] == "yes"
    assert 1001 in leads.monitor.watched
    assert any("вступил" in s for s in leads.sent)

    # Второе вступление сразу не делается: пауза 2–6 минут
    await leads.storage.join_enqueue("https://t.me/closed_vrn")
    await leads._join_step()
    assert leads.userbot.joined == {1001}

    # Заявка из чата доходит до владельца (ИИ не настроен — «без проверки»), реклама — нет
    assert await leads.monitor.handle(Msg(1001, 5, "нужна газель перевезти шкаф"))
    assert not await leads.monitor.handle(Msg(1001, 6, "грузоперевозки недорого, звоните"))
    card = leads.sent[-1]
    assert "🚚 <b>Заявка</b> · ЖК Лазурный Воронеж" in card and "без проверки ИИ" in card
    assert 'href="https://t.me/lazurny_vrn/5"' in card

    watching = await leads.watching_text()
    assert "Отслеживаю чатов: 1" in watching and "вступил в 1" in watching
    assert "заявок за 7 дн.: 1" in watching and "✅" in watching
    stats = await leads.leadstats_text()
    assert "За 7 дн.: 1 · 👍 0 · 👎 0 · точность —" in stats

    text = await leads.unwatch(link)
    assert "больше не слежу" in text and "вышел из чата" in text
    assert leads.userbot.left == ["lazurny_vrn"] and leads.monitor.watched == {}
    row = await csv_row(leads, link)
    assert (row[T.MONITOR], row[T.JOINED]) == ("", "")
    assert not await leads.monitor.handle(Msg(1001, 7, "нужна газель перевезти диван"))
    assert "ни за одним чатом" in await leads.watching_text()


async def test_watch_request_chat_is_manual_until_owner_joins(leads):
    link = "https://t.me/closed_vrn"
    text = await leads.watch(link)
    assert "вступить сам не могу" in text and "вход по заявке" in text
    assert await leads.storage.join_next() is None  # в очередь вступлений не попал
    assert (await csv_row(leads, link))[T.JOIN] == "request"
    assert "✋" in await leads.watching_text()

    # Владелец вступил вручную под аккаунтом юзербота — сверка это замечает
    leads.userbot.joined.add(1002)
    await leads.reconcile()
    assert (await csv_row(leads, link))[T.JOINED] == "yes"
    assert 1002 in leads.monitor.watched


async def test_monitor_flag_set_by_hand_in_file_is_picked_up(leads):
    await leads.watch("lazurny_vrn")
    await leads.unwatch("lazurny_vrn")
    assert await leads.storage.join_next() is None

    def by_hand(t):
        t.find("https://t.me/lazurny_vrn")[T.MONITOR] = "yes"

    await leads.repo.edit(by_hand, "руками")
    await leads.reconcile()
    assert await leads.storage.join_next() == "https://t.me/lazurny_vrn"


async def test_join_flood_wait_and_failures(leads):
    await leads.watch("lazurny_vrn")
    leads.userbot.join_error = FloodWait(600)
    await leads._join_step()
    assert leads.userbot.joined == set() and leads._flood_until is not None
    assert await leads.storage.join_next() == "https://t.me/lazurny_vrn"  # остался в очереди

    leads._flood_until = None
    leads._pause = 0
    leads.userbot.join_error = JoinManual("чат закрыт или юзербот в нём заблокирован")
    await leads._join_step()
    row = await csv_row(leads, "https://t.me/lazurny_vrn")
    assert row[T.JOIN] == "manual" and "чат закрыт" in row[T.NOTES]
    assert await leads.storage.join_next() is None
    assert any("Не вступил" in s for s in leads.sent)


async def test_join_respects_daily_limit(leads):
    leads.settings.JOIN_PER_DAY = 1
    leads._pause = 0
    await leads.watch("lazurny_vrn")
    now = dt.datetime.now(dt.UTC)
    await leads.storage.join_set("https://t.me/x", "joined")
    await leads.storage.join_attempt("https://t.me/x", now - dt.timedelta(hours=5))
    await leads._join_step()
    assert leads.userbot.joined == set()  # лимит на сутки исчерпан
    await leads.storage.join_attempt("https://t.me/x", now - dt.timedelta(hours=25))
    await leads._join_step()
    assert leads.userbot.joined == {1001}


async def test_watch_bad_links_and_unknown_chat(leads):
    assert "Формат" in await leads.watch("")
    assert "Формат" in await leads.watch("https://t.me/+AbCdEf123")
    assert "чат не найден" in await leads.watch("https://t.me/nothing_here")
    assert "нет в файле" in await leads.unwatch("https://t.me/nothing_here")
    assert "Формат" in await leads.unwatch("")


async def test_scout_blockers(leads):
    assert await leads.scout_blocker() == ""
    assert await leads.scout_advice() == ""
    # Недавняя разведка — совет подождать, но не запрет: запустить вручную можно.
    await leads.storage.kv_set("scout_last", dt.datetime.now(dt.UTC).isoformat(timespec="seconds"))
    assert await leads.scout_blocker() == ""
    assert "не чаще раза в сутки" in await leads.scout_advice()
    old = dt.datetime.now(dt.UTC) - dt.timedelta(hours=21)
    await leads.storage.kv_set("scout_last", old.isoformat(timespec="seconds"))
    assert await leads.scout_advice() == ""
    leads.userbot = None
    leads.userbot_note = "нет сессии — нужен первый вход"
    assert "app.userbot_login" in await leads.scout_blocker()


# ---------- без TG_API_ID и сессии бот работает как v1 ----------


async def test_v1_unchanged_without_userbot(tmp_path, monkeypatch):
    settings = settings_for(tmp_path, monkeypatch)
    storage = Storage(tmp_path / "data" / "bot.db")
    await storage.open()
    leads = Leads(settings, storage)
    ctx = Context(settings, PROJECTS, storage, None, leads=leads)  # type: ignore[arg-type]

    await leads.start(bot=None)  # type: ignore[arg-type]
    assert leads.userbot is None and leads._tasks == []
    assert not (tmp_path / "data" / "userbot.session").exists()  # сессию сам не создаёт
    assert not ctx.leads_enabled

    texts = [b.text for row in main_menu(ctx.projects, ctx.leads_enabled).keyboard for b in row]
    assert texts == ["🔮 TikTok", "🔄 Обновить всё"]  # меню как в v1
    assert "TG_API_ID" in await leads.scout_blocker()
    assert "Юзербот: не заданы TG_API_ID" in await leads.panel_text()
    build_dispatcher(ctx)  # роутеры v1 и v2 собираются вместе
    await leads.stop()
    await storage.close()


async def test_configured_but_no_session_does_not_start(tmp_path, monkeypatch):
    settings = settings_for(tmp_path, monkeypatch, TG_API_ID="12345", TG_API_HASH="hash")
    storage = Storage(tmp_path / "data" / "bot.db")
    await storage.open()
    leads = Leads(settings, storage)
    ctx = Context(settings, PROJECTS, storage, None, leads=leads)  # type: ignore[arg-type]
    await leads.start(bot=None)  # type: ignore[arg-type]
    assert leads.userbot is None and "нет сессии" in leads.userbot_note
    assert ctx.leads_enabled
    assert BTN_LEADS in [b.text for row in main_menu(ctx.projects, True).keyboard for b in row]
    assert "docker compose run --rm -it bot python -m app.userbot_login" in await leads.scout_blocker()
    Userbot(settings)  # клиент Telethon создаётся без сети и без входа
    await leads.stop()
    await storage.close()
