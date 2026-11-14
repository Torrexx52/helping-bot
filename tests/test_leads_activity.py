from __future__ import annotations

import datetime as dt

from app.config import ScoutConfig
from app.leads import activity as A
from app.leads import table as T
from app.leads.activity import Msg, choose_tactic, measure, template_keys
from app.leads.scout import run_scout, summary_text
from app.leads.table import Table
from app.llm import LLM
from app.userbot import ChatInfo, FoundChat

NOW = dt.datetime(2026, 11, 14, 12, tzinfo=dt.UTC)
TH = A.Thresholds(monitor_week=50, monitor_authors=10, dead_week=5, ads_min=2)


def talk(
    n: int, authors: int, days_ago: float = 0.5, text: str = "соседи, у кого лифт работает?"
) -> list[Msg]:
    return [
        Msg(NOW - dt.timedelta(days=days_ago, minutes=i), sender_id=i % authors + 1, text=text)
        for i in range(n)
    ]


def test_measure_counts_people_ads_competitors_and_captcha():
    msgs = [
        *talk(30, 12),
        Msg(NOW - dt.timedelta(days=20), 99, "старое сообщение"),  # вне недели
        Msg(NOW, 500, "Ремонт квартир под ключ, звоните +7 (900) 123-45-67"),
        Msg(NOW, 501, "Грузоперевозки, грузчики, недорого, пишите в лс"),
        Msg(NOW, 777, "Новичок, нажмите кнопку, чтобы подтвердить, что вы не бот", bot=True),
        Msg(NOW, 0, "", service="join"),
        Msg(NOW, 0, "", service="join"),
    ]
    act = measure(msgs, NOW)
    assert (act.msgs_7d, act.authors_7d, act.last_days) == (32, 14, 0)  # бот и служебные не считаются
    assert (act.ads_seen, act.competitors, act.joins_7d) == (2, 1, 2)
    assert act.captcha and not act.ads_banned
    assert len(act.sample) == 25 and "Ремонт квартир" in " ".join(act.sample)


def test_measure_dead_chat_and_ban_phrase():
    msgs = [
        Msg(NOW - dt.timedelta(days=40), 1, "Реклама запрещена, бан сразу", bot=True),
        Msg(NOW, 0, "", service="join"),
    ]
    act = measure(msgs, NOW)
    assert (act.msgs_7d, act.authors_7d, act.last_days) == (0, 0, None)
    assert act.ads_banned


def test_lead_request_is_not_an_ad():
    assert not A.is_ad("Подскажите, где заказать газель на субботу, переезд с Северного")
    assert A.is_ad("Газель с грузчиками 89001234567")


def test_choose_tactic():
    live = measure(talk(80, 25), NOW)
    quiet_ads = measure(
        [*talk(10, 5), Msg(NOW, 50, "Услуги электрика"), Msg(NOW, 51, "Маникюр, пишите в лс")], NOW
    )
    dead = measure(talk(2, 2), NOW)
    assert choose_tactic("group", True, live, "", "", TH) == A.MONITOR
    assert choose_tactic("group", True, live, "", "да", TH) == A.BOTH
    assert choose_tactic("group", True, quiet_ads, "", "", TH) == A.POST  # участники публикуют объявления
    assert choose_tactic("group", False, quiet_ads, "", "", TH) == ""  # писать нельзя
    assert choose_tactic("group", True, quiet_ads, "запрещена — бан", "", TH) == ""
    assert choose_tactic("group", True, quiet_ads, "", "нет", TH) == ""
    assert choose_tactic("group", True, dead, "разрешена", "да", TH) == A.DEAD
    assert choose_tactic("group", True, quiet_ads, "", "", TH, template=True) == A.TEMPLATE
    assert (
        choose_tactic("group", True, live, "", "", TH, template=True) == A.MONITOR
    )  # живой клон — всё равно живой
    assert choose_tactic("channel", False, live, "", "", TH) == ""
    assert choose_tactic("group", True, None, "", "", TH) == ""


def test_template_keys_ignore_title_words():
    about = "Чат жильцов ЖК {}. Правила в закрепе. Вход через бота-проверку. Добро пожаловать, соседи!"
    a = template_keys("ЖК Лазурный", about.format("Лазурный"))
    b = template_keys("ЖК Озерки", about.format("Озерки"))
    assert a and a == b
    assert template_keys("X", "коротко") == ""


# ---------- разведка с историей ----------


class HistoryUserbot:
    def __init__(self, chats: list[ChatInfo], history: dict[str, list[Msg]]) -> None:
        self.chats, self.history = chats, history

    async def search(self, query: str, limit: int = 50) -> list[FoundChat]:
        return [FoundChat(c.tg_id, c.username, c.title, c.kind, c.members) for c in self.chats]

    async def chat_info(self, username: str) -> ChatInfo:
        return next(c for c in self.chats if c.username == username)

    async def recent_messages(self, username: str, limit: int = 100) -> list[Msg]:
        return self.history.get(username, [])


async def no_sleep(_: float) -> None:
    return None


def chat(username: str, title: str, about: str = "", **kw) -> ChatInfo:
    return ChatInfo(tg_id=abs(hash(username)) % 10**9, username=username, title=title, kind="group",
                    members=kw.pop("members", 500), can_write=True, about=about, **kw)  # fmt: skip


async def test_scout_with_history_assigns_tactics_and_templates(tmp_path, monkeypatch):
    from app.storage import Storage

    monkeypatch.setattr(A, "measure", lambda msgs, now, sample_size=25, limit=0: _measure(msgs, sample_size))
    clone_about = "Чат жильцов ЖК {}. Вход через бота-проверку. Правила в закрепе. Реклама только у админа."
    chats = [
        chat("vrn_city", "Воронеж — общий чат", members=9000),
        chat("vrn_uslugi", "Услуги Воронеж", members=3000),
        *[chat(f"zhk{i}_vrn", f"ЖК Клон{i} Воронеж", clone_about.format(f"Клон{i}")) for i in range(4)],
    ]
    history = {
        "vrn_city": talk(150, 40),
        "vrn_uslugi": [
            *talk(15, 6),
            Msg(NOW, 70, "Сантехник, звоните 89001112233"),
            Msg(NOW, 71, "Окна, скидки"),
        ],
        "zhk0_vrn": talk(60, 15),  # живой клон
        "zhk1_vrn": talk(8, 4),
        "zhk2_vrn": talk(1, 1),
        "zhk3_vrn": talk(9, 4),
    }
    s = Storage(tmp_path / "bot.db")
    await s.open()
    llm = LLM(s, "", "", "")
    cfg = ScoutConfig(
        queries=["Воронеж"], geo_words=["воронеж"], monitor_week=50, monitor_authors=10, dead_week=5
    )
    out = await run_scout(HistoryUserbot(chats, history), llm, cfg, Table(), today=NOW.date(), sleep=no_sleep)
    await llm.aclose()
    await s.close()

    rows = {r[T.LINK].rsplit("/", 1)[1]: r for r in out.rows}
    assert rows["vrn_city"][T.TACTIC] == A.MONITOR
    assert (rows["vrn_city"][T.MSGS_7D], rows["vrn_city"][T.AUTHORS_7D], rows["vrn_city"][T.LAST_MSG]) == (
        "150",
        "40",
        "0",
    )
    assert rows["vrn_uslugi"][T.TYPE] == "Доска услуг ТГ" and rows["vrn_uslugi"][T.TACTIC] == A.POST
    assert "объявления услуг: 2 авт." in rows["vrn_uslugi"][T.NOTES]
    assert rows["zhk0_vrn"][T.TACTIC] == A.MONITOR
    assert rows["zhk2_vrn"][T.TACTIC] == A.DEAD
    assert rows["zhk1_vrn"][T.TACTIC] == rows["zhk3_vrn"][T.TACTIC] == A.TEMPLATE
    assert all("шаблонная сеть: 4 одинаковых" in rows[f"zhk{i}_vrn"][T.NOTES] for i in range(4))

    text = summary_text(out, len(out.rows), 0, {r[T.LINK] for r in out.rows}, [])
    order = [text.index(x) for x in ("🎯 Живые", "📢 Можно")]
    assert order == sorted(order)
    assert "Мёртвые" not in text and "Шаблонные" not in text and "zhk2_vrn" not in text  # не показываем
    assert "Скрыто мёртвых: 1 · шаблонных клонов: 2" in text
    assert "Воронеж — общий чат</a> — Городской чат ТГ" not in text  # тип без ИИ — «Другое ТГ»
    assert "150 сообщ./нед · 40 авт." in text


_real_measure = A.measure


def _measure(msgs, sample_size):
    return _real_measure(msgs, NOW, sample_size)


def test_measure_estimates_week_when_history_is_saturated():
    """Все 100 запрошенных сообщений написаны за 12 часов — за неделю их около 1400."""
    busy = [Msg(NOW - dt.timedelta(minutes=7.2 * i), 100 + i % 30, f"реплика {i}") for i in range(100)]
    assert _real_measure(busy, NOW).msgs_7d == 100  # без `limit` — как посчитано
    act = _real_measure(busy, NOW, limit=100)
    assert 1300 < act.msgs_7d < 1500 and act.authors_7d == 30
    quiet = [Msg(NOW - dt.timedelta(days=i), 1, "привет") for i in range(100)]
    assert _real_measure(quiet, NOW, limit=100).msgs_7d == 8  # выборка покрывает неделю — не оцениваем
