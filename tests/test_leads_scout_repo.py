from __future__ import annotations

import datetime as dt
import json
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest

from app.config import ScoutConfig
from app.leads import table as T
from app.leads.datarepo import DataRepo
from app.leads.scout import build_row, guess_type, parse_analysis, run_scout, summary_text
from app.leads.service import parse_scout_time
from app.leads.table import Table
from app.llm import LLM
from app.storage import Storage
from app.userbot import ChatInfo, FloodWait, FoundChat, UserbotError

TODAY = dt.date(2026, 11, 14)
CFG = ScoutConfig(queries=["Воронеж ЖК", "переезд Воронеж", "барахолка Воронеж"], geo_words=["воронеж", "лазурный"])


def info(username: str, title: str, **kw) -> ChatInfo:
    base = {"tg_id": abs(hash(username)) % 10**9, "kind": "group", "members": 100, "can_write": True}
    base.update(kw)
    return ChatInfo(username=username, title=title, **base)


class FakeUserbot:
    def __init__(self, results: dict[str, list[ChatInfo]], fail: dict[str, Exception] | None = None) -> None:
        self.results = results
        self.fail = fail or {}
        self.searched: list[str] = []
        self.info_calls: list[str] = []

    async def search(self, query: str, limit: int = 50) -> list[FoundChat]:
        self.searched.append(query)
        if query in self.fail:
            raise self.fail.pop(query)
        return [FoundChat(c.tg_id, c.username, c.title, c.kind, c.members) for c in self.results.get(query, [])]

    async def chat_info(self, username: str) -> ChatInfo:
        self.info_calls.append(username)
        if username in self.fail:
            raise self.fail.pop(username)
        return next(c for chats in self.results.values() for c in chats if c.username == username)


async def no_sleep(_: float) -> None:
    return None


@pytest.fixture
async def storage(tmp_path):
    s = Storage(tmp_path / "bot.db")
    await s.open()
    yield s
    await s.close()


def llm_with(storage: Storage, answers: dict[str, dict] | None) -> LLM:
    """ИИ, отвечающий по названию чата из запроса; answers=None — ИИ не настроен."""
    if answers is None:
        return LLM(storage, "", "", "")

    def handler(request: httpx.Request) -> httpx.Response:
        prompt = json.loads(request.content)["messages"][1]["content"]
        answer = next((a for title, a in answers.items() if f"Название: {title}" in prompt), {})
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(answer)}}]})

    return LLM(storage, "https://llm.test/v1", "k", "m", transport=httpx.MockTransport(handler))


# ---------- разведка ----------


async def test_scout_builds_rows_with_ai(storage):
    lazur = info(
        "lazurny_vrn",
        "ЖК Лазурный | соседи",
        members=2400,
        about="Чат жителей. Реклама платная, по вопросам @lazur_admin",
        pinned="Правила: реклама только через админа",
        slowmode=60,
    )
    channel = info("vrn_news", "Новости Воронежа", kind="channel", members=50000, can_write=False, linked="vrn_talk")
    lipetsk = info("lip_bar", "Барахолка Липецк", members=9000)
    hidden_geo = info("sosedi_36", "Соседи 36", members=300, about="Наш район у водохранилища")
    userbot = FakeUserbot({"Воронеж ЖК": [lazur, channel, lipetsk], "переезд Воронеж": [lazur, hidden_geo]})
    llm = llm_with(
        storage,
        {
            "ЖК Лазурный": {
                "voronezh": True,
                "type": "ЖК-группа ТГ",
                "ads": "платно",
                "ads_note": "через админа",
                "admin": "@lazur_admin",
            },
            "Новости Воронежа": {"voronezh": True, "type": "Барахолка ТГ", "ads": "как-нибудь", "admin": "@ghost"},
            "Барахолка Липецк": {"voronezh": False, "type": "Барахолка ТГ"},
            "Соседи 36": {"voronezh": True, "type": "Районный чат ТГ"},
        },
    )
    out = await run_scout(userbot, llm, CFG, Table(), today=TODAY, queries_per_run=2, sleep=no_sleep)
    await llm.aclose()

    assert userbot.searched == ["Воронеж ЖК", "переезд Воронеж"]  # лимит запросов за запуск
    assert (out.queries_done, out.found, out.skipped_fresh) == (2, 4, 0)
    rows = {r[T.LINK]: r for r in out.rows}
    assert set(rows) == {"https://t.me/lazurny_vrn", "https://t.me/vrn_news", "https://t.me/sosedi_36"}

    r = rows["https://t.me/lazurny_vrn"]
    assert r[T.NAME] == "ЖК Лазурный | соседи" and r[T.TYPE] == "ЖК-группа ТГ"
    assert (r[T.MEMBERS], r[T.PLATFORM], r[T.STATUS]) == ("2400", "Telegram", "Не трогали")
    assert r[T.RULES] == "платно — через админа" and r[T.ADMIN] == "@lazur_admin"
    assert r[T.NOTES] == "медленный режим 60 с"
    assert (r[T.KIND], r[T.CAN_WRITE], r[T.JOIN], r[T.MONITOR], r[T.JOINED]) == ("group", "yes", "open", "", "")
    assert (r[T.QUERY], r[T.FOUND_AT], r[T.CHECKED_AT]) == ("Воронеж ЖК", "2026-11-14", "2026-11-14")
    assert r[T.TG_ID] == str(lazur.tg_id)

    c = rows["https://t.me/vrn_news"]
    assert c[T.TYPE] == "Канал ТГ" and c[T.CAN_WRITE] == "no"  # канал остаётся каналом, что бы ни сказал ИИ
    assert c[T.RULES] == "" and c[T.ADMIN] == ""  # выдуманные правила и контакт отброшены
    assert c[T.NOTES] == "обсуждение: https://t.me/vrn_talk"
    assert rows["https://t.me/sosedi_36"][T.TYPE] == "Районный чат ТГ"  # гео-слов нет, но ИИ признал


async def test_scout_without_ai_uses_geo_words_and_heuristics(storage):
    chats = [
        info("zhk_vrn", "ЖК Европейский Воронеж", about="вход после проверки, капча"),
        info("bar_vrn", "Барахолка Воронеж | куплю-продам"),
        info("lb_vrn", "Левый берег Воронеж", can_write=False, join_request=True),
        info("sosedi_36", "Соседи 36"),
    ]
    userbot = FakeUserbot({"Воронеж ЖК": chats})
    llm = llm_with(storage, None)
    out = await run_scout(userbot, llm, CFG, Table(), today=TODAY, queries_per_run=1, sleep=no_sleep)
    await llm.aclose()
    rows = {r[T.LINK].rsplit("/", 1)[1]: r for r in out.rows}
    # без гео-слов и без ИИ — отсеяно; барахолка — пропущена
    assert set(rows) == {"zhk_vrn", "lb_vrn"}
    assert out.skipped_offtopic == 1
    assert rows["zhk_vrn"][T.TYPE] == "ЖК-группа ТГ" and rows["zhk_vrn"][T.JOIN] == "manual"
    assert rows["lb_vrn"][T.TYPE] == "Районный чат ТГ" and rows["lb_vrn"][T.JOIN] == "request"
    assert rows["lb_vrn"][T.NOTES] == "писать нельзя; вход по заявке"
    assert all(r[T.RULES] == "" and r[T.ADMIN] == "" for r in out.rows)
    assert any("ИИ недоступен" in w for w in out.warnings)


async def test_scout_skips_fresh_rechecks_stale_and_rotates_queries(storage):
    fresh, stale = info("fresh_vrn", "Воронеж свежий"), info("stale_vrn", "Воронеж старый")
    existing = Table(
        [
            {T.LINK: "https://t.me/fresh_vrn", T.CHECKED_AT: "2026-10-20"},
            {T.LINK: "https://t.me/Stale_VRN", T.CHECKED_AT: "2026-10-01"},
        ]
    )
    userbot = FakeUserbot({"барахолка Воронеж": [fresh, stale]})
    llm = llm_with(storage, None)
    out = await run_scout(
        userbot, llm, CFG, existing, today=TODAY, queries_per_run=2, offset=2, sleep=no_sleep
    )
    await llm.aclose()
    assert userbot.searched == ["барахолка Воронеж", "Воронеж ЖК"]  # с места прошлой остановки, по кругу
    assert userbot.info_calls == ["stale_vrn"]  # проверялась больше 30 дней назад
    assert out.skipped_fresh == 1 and [r[T.LINK] for r in out.rows] == ["https://t.me/stale_vrn"]


async def test_scout_flood_wait_and_errors(storage):
    a, b = info("a_vrn", "Воронеж А", members=500), info("b_vrn", "Воронеж Б", members=50)
    userbot = FakeUserbot(
        {"Воронеж ЖК": [a], "переезд Воронеж": [b]},
        fail={"Воронеж ЖК": FloodWait(30), "барахолка Воронеж": UserbotError("X"), "b_vrn": FloodWait(5000)},
    )
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    llm = llm_with(storage, None)
    out = await run_scout(userbot, llm, CFG, Table(), today=TODAY, sleep=sleep)
    await llm.aclose()
    assert 35 in slept  # короткий FloodWait: подождали, сколько сказано (+5 с), и повторили запрос
    assert userbot.searched == ["Воронеж ЖК", "Воронеж ЖК", "переезд Воронеж", "барахолка Воронеж"]
    assert out.queries_done == 2
    assert [r[T.LINK] for r in out.rows] == ["https://t.me/a_vrn"]  # до длинного FloodWait успели одну
    assert any("5000" in w for w in out.warnings) and any("барахолка" in w for w in out.warnings)


async def test_scout_limits_chats_per_run(storage):
    chats = [info(f"chat{i}_vrn", f"Воронеж {i}", members=i) for i in range(1, 8)]
    userbot = FakeUserbot({"Воронеж ЖК": chats})
    llm = llm_with(storage, None)
    out = await run_scout(userbot, llm, CFG, Table(), today=TODAY, queries_per_run=1, max_chats=3, sleep=no_sleep)
    await llm.aclose()
    assert userbot.info_calls == ["chat7_vrn", "chat6_vrn", "chat5_vrn"]  # сначала самые большие
    assert any("3 из 7" in w for w in out.warnings)


def test_parse_analysis_checks_contact_against_source():
    chat = info("x_vrn", "Чат", about="Админ: @Real_Admin, тел. +7 (900) 123-45-67", pinned="")
    assert parse_analysis({"admin": "@real_admin"}, chat).admin == "@real_admin"
    assert parse_analysis({"admin": "8 900 123 45 67"}, chat).admin == "8 900 123 45 67"
    assert parse_analysis({"admin": "@someone_else"}, chat).admin == ""
    assert parse_analysis({"admin": "https://t.me/real_admin"}, chat).admin == "https://t.me/real_admin"
    a = parse_analysis({"voronezh": "да", "type": "Форум", "ads": "Запрещена", "ads_note": " бан  сразу "}, chat)
    assert (a.voronezh, a.type, a.rules) == (False, "", "запрещена — бан сразу")


def test_guess_type():
    assert guess_type(info("a", "Новости", kind="channel")) == "Канал ТГ"
    assert guess_type(info("a", "ЖК Лазурный")) == "ЖК-группа ТГ"
    assert guess_type(info("a", "Отдам даром Воронеж")) == "Барахолка ТГ"
    assert guess_type(info("a", "Соседи Северного района")) == "Районный чат ТГ"
    assert guess_type(info("a", "Воронеж")) == "Другое ТГ"


async def test_summary_text(storage):
    userbot = FakeUserbot(
        {"Воронеж ЖК": [info(f"c{i}_vrn", f"Воронеж <{i}>", members=1000 * i) for i in range(1, 13)]}
    )
    llm = llm_with(storage, None)
    out = await run_scout(userbot, llm, CFG, Table(), today=TODAY, queries_per_run=1, sleep=no_sleep)
    await llm.aclose()
    new = {r[T.LINK] for r in out.rows} - {"https://t.me/c12_vrn"}
    lines = summary_text(out, 11, 1, new, ["push не удался"]).splitlines()
    assert lines[0] == "🔎 <b>Разведка</b>: найдено 11 новых, 1 обновлено"
    top = [line for line in lines if line[:1].isdigit()]
    assert len(top) == 10  # не больше 10 лучших, обновлённая c12 не в списке новых
    assert top[0] == '1. <a href="https://t.me/c11_vrn">Воронеж &lt;11&gt;</a> — 11 000 · Другое ТГ'
    assert lines[-1] == "⚠️ push не удался"


@pytest.mark.parametrize(
    ("value", "expected"), [("", None), ("04:30", (4, 30)), ("30 4 * * *", (4, 30)), (" 9:05 ", (9, 5))]
)
def test_parse_scout_time(value, expected):
    assert parse_scout_time(value) == expected


@pytest.mark.parametrize("value", ["25:00", "*/5 * * * *", "утром", "0 4 * * 1"])
def test_parse_scout_time_errors(value):
    with pytest.raises(ValueError):
        parse_scout_time(value)


# ---------- репозиторий данных ----------

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="нужен git")


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "core.autocrlf=false", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()


@pytest.fixture
def origin(tmp_path) -> Path:
    """«GitHub»: пустой bare-репозиторий с README в main, как его создаст владелец."""
    bare = tmp_path / "origin.git"
    bare.mkdir()
    git(bare, "init", "-q", "--bare", "-b", "main")
    seed = tmp_path / "seed"
    seed.mkdir()
    git(seed, "init", "-q", "-b", "main")
    (seed / "README.md").write_text("# helping-bot-data\n", encoding="utf-8")
    git(seed, "add", "-A")
    git(seed, "commit", "-qm", "init")
    git(seed, "push", "-q", bare.as_posix(), "main")
    return bare


def owner_clone(tmp_path: Path, origin: Path, name: str = "owner") -> Path:
    path = tmp_path / name
    git(tmp_path, "clone", "-q", origin.as_posix(), name)
    return path


def new_row(link: str, name: str, **kw: str) -> dict[str, str]:
    row = build_row(info(link.rsplit("/", 1)[1], name), None, "Воронеж ЖК", "2026-11-14")
    row.update(kw)
    return row


@needs_git
async def test_data_repo_commit_push_and_manual_columns_survive(tmp_path, origin):
    repo = DataRepo(tmp_path / "data" / "leads-data", url=origin.as_posix())
    rows = [new_row("https://t.me/a_vrn", "Воронеж А"), new_row("https://t.me/b_vrn", "Воронеж Б")]
    (added, updated), warning = await repo.edit(
        lambda t: t.merge(rows), lambda r: f"scout: +{r[0]} новых, {r[1]} обновлено"
    )
    assert (added, updated, warning) == (2, 0, None)
    assert git(origin, "log", "-1", "--format=%s", "main") == "scout: +2 новых, 0 обновлено"
    assert git(origin, "log", "-1", "--format=%an", "main") == "helping-bot"
    files = git(origin, "ls-tree", "-r", "--name-only", "main").splitlines()
    assert files == ["README.md", "chats/telegram.csv"]
    assert "| monitor |" in git(origin, "show", "main:README.md")  # описание колонок

    # Владелец правит файл в GitHub: статус, дата размещения, monitor
    owner = owner_clone(tmp_path, origin)
    csv_path = owner / "chats" / "telegram.csv"
    assert csv_path.read_bytes().startswith(b"\xef\xbb\xbf")
    table = Table.load(csv_path)
    table.find("https://t.me/a_vrn").update({T.STATUS: "Договорились", T.POSTED: "2026-11-20", T.MONITOR: "yes"})
    table.save(csv_path)
    git(owner, "commit", "-qam", "руками")
    git(owner, "push", "-q", "origin", "main")

    # Следующая разведка: имя и участники обновились, ручные колонки — нет
    again = [new_row("https://t.me/a_vrn", "Воронеж А+", **{T.MEMBERS: "999"}), new_row("https://t.me/c_vrn", "В")]
    (added, updated), warning = await repo.edit(lambda t: t.merge(again), "scout: +1 новых, 1 обновлено")
    assert (added, updated, warning) == (1, 1, None)
    git(owner, "pull", "-q")
    final = Table.load(csv_path)
    a = final.find("https://t.me/a_vrn")
    assert (a[T.NAME], a[T.MEMBERS]) == ("Воронеж А+", "999")
    assert (a[T.STATUS], a[T.POSTED], a[T.MONITOR]) == ("Договорились", "2026-11-20", "yes")
    assert [r[T.LINK].rsplit("/", 1)[1] for r in final.rows] == ["a_vrn", "b_vrn", "c_vrn"]
    assert git(origin, "rev-list", "--count", "main") == "4"

    # Без изменений нового коммита нет
    _, warning = await repo.edit(lambda t: None, "пусто")
    assert warning is None and git(origin, "rev-list", "--count", "main") == "4"
    table, warning = await repo.read()
    assert warning is None and len(table.monitored()) == 1


@needs_git
async def test_data_repo_works_offline_and_pushes_later(tmp_path, origin):
    hidden = tmp_path / "hidden.git"
    origin.rename(hidden)  # «GitHub недоступен»
    repo = DataRepo(tmp_path / "leads-data", url=origin.as_posix())
    _, warning = await repo.edit(lambda t: t.merge([new_row("https://t.me/a_vrn", "А")]), "scout: офлайн")
    assert warning and "локально" in warning
    table, warning = await repo.read()
    assert warning and len(table.rows) == 1  # читается локальная копия

    # Сеть вернулась. Локальная история началась без GitHub и с ним не связана — rebase невозможен.
    hidden.rename(origin)
    _, warning = await repo.edit(lambda t: t.merge([new_row("https://t.me/b_vrn", "Б")]), "scout: онлайн")
    assert warning is None
    subjects = git(origin, "log", "--format=%s", "main").splitlines()
    assert subjects == ["scout: онлайн", "sync: локальные изменения бота", "init"]

    # Снова без сети: бот ставит monitor, владелец в это время правит статус в GitHub.
    origin.rename(hidden)

    def watch(t: Table) -> None:
        t.find("https://t.me/a_vrn")[T.MONITOR] = "yes"

    _, warning = await repo.edit(watch, "watch: a")
    assert warning
    hidden.rename(origin)
    owner = owner_clone(tmp_path, origin)
    csv_path = owner / "chats" / "telegram.csv"
    table = Table.load(csv_path)
    table.find("https://t.me/b_vrn")[T.STATUS] = "Отказ"
    table.save(csv_path)
    (owner / "notes.txt").write_text("x", encoding="utf-8")
    git(owner, "add", "-A")
    git(owner, "commit", "-qm", "руками")
    git(owner, "push", "-q", "origin", "main")

    _, warning = await repo.edit(lambda t: None, "пусто")  # правок нет, но локальный коммит уходит
    assert warning is None
    git(owner, "pull", "-q")
    final = Table.load(csv_path)
    assert final.find("https://t.me/a_vrn")[T.MONITOR] == "yes"
    assert final.find("https://t.me/b_vrn")[T.STATUS] == "Отказ"
    assert (owner / "notes.txt").exists() and len(final.rows) == 2


def test_overlay_keeps_remote_status():
    remote = Table([{T.LINK: "https://t.me/a", T.STATUS: "Разместили", T.POSTED: "2026-11-20", T.NAME: "А"}])
    local = Table(
        [
            {T.LINK: "https://t.me/A", T.STATUS: "Не трогали", T.NAME: "А2", T.MONITOR: "yes", T.JOINED: "yes"},
            {T.LINK: "https://t.me/b", T.STATUS: "Не трогали", T.NAME: "Б"},
        ]
    )
    remote.overlay(local)
    a, b = remote.rows
    assert (a[T.STATUS], a[T.POSTED], a[T.NAME], a[T.MONITOR], a[T.JOINED]) == (
        "Разместили",
        "2026-11-20",
        "А2",
        "yes",
        "yes",
    )
    assert b[T.NAME] == "Б" and b[T.STATUS] == "Не трогали"


@needs_git
async def test_data_repo_without_key_is_local_only(tmp_path):
    repo = DataRepo(tmp_path / "leads-data", "torrexx52/helping-bot-data", tmp_path / "ssh" / "leads-data")
    assert repo.remote is None
    _, warning = await repo.edit(lambda t: t.merge([new_row("https://t.me/a_vrn", "А")]), "scout: локально")
    assert warning and "deploy key" in warning
    assert git(repo.path, "log", "-1", "--format=%s") == "scout: локально"
    assert (await repo.read())[0].rows[0][T.LINK] == "https://t.me/a_vrn"


async def test_scout_skips_flea_markets_and_ranks_residents_first(storage):
    chats = [
        info("vrn_baraholka", "Барахолка Воронеж", members=9000),
        info("lazurny_bar", "ЖК Лазурный барахолка", members=5000),
        info("lev_bereg", "Левый берег Воронеж — соседи", members=800),
        info("jk_lazurny", "ЖК Лазурный Воронеж", members=300),
    ]
    ub = FakeUserbot({"Воронеж ЖК": chats})
    llm = llm_with(storage, None)
    out = await run_scout(ub, llm, CFG, Table([]), today=TODAY, sleep=no_sleep)
    names = [r[T.NAME] for r in out.rows]
    assert "Барахолка Воронеж" not in names and "ЖК Лазурный барахолка" not in names
    assert out.skipped_offtopic == 2
    text = summary_text(out, len(out.rows), 0, {r[T.LINK] for r in out.rows}, [])
    assert text.index("ЖК Лазурный Воронеж") < text.index("Левый берег")
    assert "не подошли" in text


def test_guess_type_flea_title_wins_over_jk():
    assert guess_type(info("x", "ЖК Озерки барахолка")) == "Барахолка ТГ"
    assert guess_type(info("y", "Чат жильцов дома на Шишкова")) == "ЖК-группа ТГ"
