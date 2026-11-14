from __future__ import annotations

import datetime as dt

import httpx
import pytest

from app.config import ScoutConfig
from app.leads import complexes as C
from app.leads import table as T
from app.leads.activity import Msg
from app.leads.scout import run_scout
from app.leads.table import Table
from app.llm import LLM
from app.storage import Storage
from app.userbot import ChatInfo, FoundChat, UserbotError

TODAY = dt.date(2026, 11, 15)


@pytest.mark.parametrize(
    ("raw", "name", "ok"),
    [
        ('ЖК "Озерки" поз. 34', "Озерки", True),
        ('Клубный дом "Академический"', "Академический", True),
        ('ЖК " Квартал 45"', "Квартал 45", True),
        ('ЖК "Авиапарк', "Авиапарк", True),
        ("Жилой комплекс «Лазурный» 2 очередь", "Лазурный", True),
        ('ЖК "Современник" (поз. 5, 6)', "Современник", True),
        ("ЖК Яблоневые сады, дом 12", "Яблоневые сады", True),
        ("Микрорайон Боровое", "Боровое", True),
        ("Дом на набережной", "Дом на набережной", False),
        ("По ул. Одесская", "По ул. Одесская", False),
        ("Жилой комплекс 1719", "Жилой комплекс 1719", False),
        (None, "", False),
    ],
)
def test_clean_name_and_searchable(raw, name, ok):
    assert C.clean_name(raw) == name
    assert C.searchable(C.clean_name(raw)) is ok


def house(name, flats, ready, status=2, lat=51.66, lon=39.2, addr="Город Воронеж, Улица Шишкова"):
    return {
        "objCommercNm": name,
        "objElemLivingCnt": flats,
        "objReady100PercDt": ready,
        "objStatus": status,
        "latitude": lat,
        "longitude": lon,
        "objAddr": addr,
        "residentialBuildings": 1,
        "developer": {"groupName": "ДСК"},
    }


def test_registry_and_maps_are_merged_and_ranked():
    complexes: dict[str, C.Complex] = {}
    unnamed = C.add_domrf(
        complexes,
        [
            house('ЖК "Озерки" поз. 1', 600, "2026-06-30"),
            house('ЖК "Озерки" поз. 34', 336, "2027-09-30", status=0),
            house('ЖК "Старый"', 200, "2019-03-01"),
            house('ЖК "Будущий"', 900, "2029-12-31", status=0),
            house('ЖК "Дальний"', 600, "2026-06-30", lat=50.2, lon=39.57, addr="Город Россошь, Улица Мира"),
            house(None, 100, "2026-01-01"),
        ],
    )
    assert unnamed == 1 and set(complexes) == {"озерки", "старый", "будущий", "дальний"}
    matched, added = C.add_yandex(
        complexes,
        [
            {"t": "Озерки", "cat": ["Жилой комплекс"], "c": [39.2, 51.66], "rc": 500,
             "u": ["https://ozerki.example/"], "l": ["https://t.me/ozerki_vrn", "https://vk.ru/ozerki"]},
            {"t": "Олимпийский", "cat": ["Жилой комплекс"], "c": [39.15, 51.70], "rc": 7298, "a": "Воронеж, ул. Шишкова"},
            {"t": "По ул. Шишкова", "cat": ["Жилой комплекс"], "c": [39.2001, 51.6601], "rc": 3},
            {"t": "Риелтор", "cat": ["Агентство недвижимости"], "c": [39.2, 51.66]},
        ],
    )  # fmt: skip
    assert (matched, added) == (1, 1)  # «По ул. Шишкова» — дом уже известного ЖК рядом, агентство не берём

    rows = C.build_rows(complexes, TODAY)
    by = {r[C.NAME]: r for r in rows}
    assert [r[C.NAME] for r in rows][0] == "Озерки"  # заселяется, большой, в городе, строятся очереди
    oz = by["Озерки"]
    assert (oz[C.FLATS], oz[C.HOUSES], oz[C.STAGE], oz[C.GRADE]) == ("936", "2", "заселяется", "A")
    assert oz[C.CHATS] == "https://t.me/ozerki_vrn" and oz[C.SOCIAL] == "https://vk.ru/ozerki"
    assert oz[C.PLACE] == "Воронеж" and "строятся новые очереди" in oz[C.WHY]
    assert by["Дальний"][C.PLACE] == "Россошь" and int(by["Дальний"][C.SCORE]) < int(oz[C.SCORE])
    assert by["Будущий"][C.STAGE] == "строится" and by["Будущий"][C.GRADE] in "CD"
    assert int(by["Старый"][C.SCORE]) < int(by["Олимпийский"][C.SCORE]) < int(oz[C.SCORE])
    assert by["Олимпийский"][C.SOURCES] == "яндекс" and "оценок на Картах" in by["Олимпийский"][C.WHY]

    parsed = C.load_rows_text(C.dumps(rows))
    assert C.search_names(parsed)[0] == "Озерки" and len(C.search_names(parsed)) == 5
    assert C.locality("Воронежская обл., Новоусманский район, пос. Отрадное, ул. Осиновая") == "Отрадное"
    assert C.locality("Воронеж, ул. Шишкова") == "Воронеж" and C.locality("Воронежская обл., Лиски, ул. Титова") == "Лиски"
    assert C.clean_name("Инстеп.Скандинавия") == "Скандинавия" and C.clean_name("РИТМ") == "Ритм"
    garage: dict[str, C.Complex] = {}
    assert C.add_domrf(garage, [house("Гаражный комплекс", 0, "2020-01-01")]) == 1 and not garage
    assert C.seed_links(parsed) == ["https://t.me/ozerki_vrn"]


async def test_fetch_domrf_pages_through_registry():
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params["offset"])
        calls.append(offset)
        items = [{"objId": i} for i in range(offset, min(offset + 2, 5))]
        return httpx.Response(200, json={"data": {"total": 5, "list": items}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        got = await C.fetch_domrf(client, page=2, pause=0)
    assert calls == [0, 2, 4] and [o["objId"] for o in got] == [0, 1, 2, 3, 4]


class SeedUserbot:
    def __init__(self, chats: list[ChatInfo]) -> None:
        self.chats = {c.username: c for c in chats}
        self.searched: list[str] = []
        self.info_calls: list[str] = []

    async def search(self, query: str, limit: int = 50) -> list[FoundChat]:
        self.searched.append(query)
        return []

    async def chat_info(self, username: str) -> ChatInfo:
        self.info_calls.append(username)
        if username not in self.chats:
            raise UserbotError("чат не найден")
        return self.chats[username]

    async def recent_messages(self, username: str, limit: int = 100) -> list[Msg]:
        now = dt.datetime.now(dt.UTC)
        return [Msg(now - dt.timedelta(hours=i), i, "добрый день") for i in range(20)]


async def test_scout_checks_seed_links_first_and_searches_listed_names(tmp_path):
    chat = ChatInfo(tg_id=1, username="ozerki_vrn", title="ЖК Озерки Воронеж", kind="group", members=900, can_write=True)
    channel = ChatInfo(tg_id=2, username="dsk_vrn", title="ДСК Воронеж", kind="channel", members=5000)
    userbot = SeedUserbot([chat, channel])
    cfg = ScoutConfig(
        queries=["ЖК Воронеж"], geo_words=["воронеж"], complexes=["Гран-При"], complex_queries=["ЖК {name}"],
        only_types=["ЖК-группа ТГ"], dead_week=5,
    )  # fmt: skip
    storage = Storage(tmp_path / "bot.db")
    await storage.open()
    llm = LLM(storage, "", "", "")

    async def no_sleep(_: float) -> None:
        return None

    out = await run_scout(
        userbot, llm, cfg, Table(), today=TODAY, names=["Озерки", "Гран-При"],
        seeds=["https://t.me/ozerki_vrn", "https://t.me/dsk_vrn", "https://t.me/gone_chat"], sleep=no_sleep,
    )  # fmt: skip
    await llm.aclose()
    await storage.close()
    assert userbot.info_calls == ["ozerki_vrn", "dsk_vrn", "gone_chat"]
    assert [r[T.LINK] for r in out.rows] == ["https://t.me/ozerki_vrn"] and out.rows[0][T.QUERY] == "список ЖК"
    assert out.rejected == ["dsk_vrn", "gone_chat"] and out.seeds == 3  # канал и пропавший чат запомнены
    assert userbot.searched == ["ЖК Воронеж", "ЖК Озерки", "ЖК Гран-При"]  # названия из списка — первыми
