from __future__ import annotations

import datetime as dt
import json

import httpx
import pytest

from app.leads.monitor import Monitor, Watched, join_wait, lead_card
from app.leads.text import WordFilter, text_hash
from app.llm import LLM, LLMUnavailable, extract_json, parse_lead_answer
from app.storage import Storage
from app.userbot import Incoming, MessageDetails

UTC = dt.UTC
CHAT = 1234567890


@pytest.fixture
async def storage(tmp_path):
    s = Storage(tmp_path / "bot.db")
    await s.open()
    yield s
    await s.close()


def make_llm(storage: Storage, answer: object, limit: int = 300, calls: list | None = None) -> LLM:
    """ИИ с подменённым транспортом. answer: текст ответа, исключение или HTTP-код."""

    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(json.loads(request.content))
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, int):
            return httpx.Response(answer, text="error")
        return httpx.Response(200, json={"choices": [{"message": {"content": answer}}]})

    return LLM(
        storage, "https://llm.test/v1/", "sk-test", "m", 5, limit, transport=httpx.MockTransport(handler)
    )


class FakeIncoming(Incoming):
    def __init__(self, text: str, chat_id: int = CHAT, msg_id: int = 77) -> None:
        super().__init__(chat_id, msg_id, text)
        self.details_calls = 0

    async def details(self) -> MessageDetails:
        self.details_calls += 1
        return MessageDetails("ЖК «Лазурный» | Воронеж", "lazurny_vrn", "Иван <Петров>", "ivan_vrn")


def make_monitor(storage: Storage, llm: LLM) -> tuple[Monitor, list]:
    sent: list = []

    async def notify(text, markup):
        sent.append((text, markup))

    monitor = Monitor(storage, llm, notify)
    monitor.filter = WordFilter(["газел", "перевез", "грузоперевоз"], ["недорого", "звоните"])
    monitor.watched = {CHAT: Watched("Лазурный", "https://t.me/lazurny_vrn")}
    return monitor, sent


# ---------- разбор ответа ИИ ----------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('{"lead": true, "summary": "Перевезти шкаф, Северный → Шилово, суббота"}', (True, "Перевезти шкаф, Северный → Шилово, суббота")),
        ('```json\n{"lead": false, "summary": ""}\n```', (False, "")),
        ('Ответ: {"lead": "true", "summary": "  диван \\n завтра "} — вот.', (True, "диван завтра")),
        ('{"lead": false}', (False, "")),
        ('{"lead": true, "summary": null}', (True, "")),
    ],
)
def test_parse_lead_answer(raw, expected):
    assert parse_lead_answer(raw) == expected


@pytest.mark.parametrize("raw", ["да, это заявка", '{"summary": "x"}', '{"lead": 1}', "[true]", '{"lead": tru'])
def test_parse_lead_answer_rejects_garbage(raw):
    with pytest.raises(ValueError):
        parse_lead_answer(raw)


def test_extract_json_takes_first_object():
    assert extract_json('x {"a": {"b": 1}} y {"c": 2}') == {"a": {"b": 1}}


# ---------- ИИ: лимит, таймаут, ошибки ----------


async def test_llm_request_and_daily_limit(storage):
    calls: list = []
    llm = make_llm(storage, '{"lead": true, "summary": "шкаф"}', limit=2, calls=calls)
    assert await llm.classify_lead("нужна газель") == (True, "шкаф")
    assert calls[0]["model"] == "m" and calls[0]["temperature"] == 0
    assert "нужна газель" in calls[0]["messages"][1]["content"]
    assert await llm.remaining() == 1
    await llm.classify_lead("ещё")
    with pytest.raises(LLMUnavailable, match="лимит"):
        await llm.classify_lead("третье")
    assert len(calls) == 2  # сверх лимита запросов нет
    await llm.aclose()


async def test_llm_reserve_and_not_configured(storage):
    llm = make_llm(storage, "{}", limit=3)
    with pytest.raises(LLMUnavailable, match="лимит"):
        await llm.ask_json("s", "u", reserve=3)
    assert await llm.ask_json("s", "u", reserve=2) == {}
    await llm.aclose()
    off = LLM(storage, "", "", "")
    assert not off.configured and await off.remaining() == 0
    with pytest.raises(LLMUnavailable, match="не настроен"):
        await off.classify_lead("x")
    await off.aclose()


@pytest.mark.parametrize(
    "answer",
    [httpx.ReadTimeout("slow"), httpx.ConnectError("down"), 500, 401, "не json", '{"oops": 1}'],
)
async def test_llm_failures_become_unavailable(storage, answer):
    llm = make_llm(storage, answer)
    with pytest.raises(LLMUnavailable):
        await llm.classify_lead("нужна газель")
    await llm.aclose()


async def test_llm_falls_back_without_response_format(storage):
    seen: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append("response_format" in body)
        if "response_format" in body:
            return httpx.Response(400, text="unknown parameter")
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"lead": false}'}}]})

    llm = LLM(storage, "https://llm.test/v1", "k", "m", transport=httpx.MockTransport(handler))
    assert await llm.classify_lead("a") == (False, "")
    assert await llm.classify_lead("b") == (False, "")
    assert seen == [True, False, False]
    await llm.aclose()


# ---------- мониторинг ----------


async def test_lead_is_sent_in_card_format(storage):
    llm = make_llm(storage, '{"lead": true, "summary": "Перевезти шкаф, в субботу"}')
    monitor, sent = make_monitor(storage, llm)
    msg = FakeIncoming("нужна газель перевезти шкаф")
    assert await monitor.handle(msg)
    ((text, markup),) = sent
    assert text.splitlines() == [
        "🚚 <b>Заявка</b> · ЖК «Лазурный» | Воронеж",
        "👤 Иван &lt;Петров&gt; (@ivan_vrn)",
        "📝 Перевезти шкаф, в субботу",
        "«нужна газель перевезти шкаф»",
        '🔗 <a href="https://t.me/lazurny_vrn/77">Открыть сообщение</a>',
    ]
    buttons = markup.inline_keyboard[0]
    day = monitor.today()
    assert [b.text for b in buttons] == ["👍 Заявка", "👎 Мимо"]
    assert [b.callback_data for b in buttons] == [f"lv:u:{CHAT}:{day}", f"lv:d:{CHAT}:{day}"]
    assert (await storage.lead_totals(day))["leads"] == 1
    await llm.aclose()


async def test_competitor_ad_and_other_chats_are_ignored(storage):
    calls: list = []
    llm = make_llm(storage, '{"lead": true, "summary": "x"}', calls=calls)
    monitor, sent = make_monitor(storage, llm)
    ad = FakeIncoming("грузоперевозки недорого, звоните")
    assert not await monitor.handle(ad)
    assert not await monitor.handle(FakeIncoming("нужна газель", chat_id=999))  # чат не отслеживается
    assert not await monitor.handle(FakeIncoming("всем привет"))
    assert sent == [] and calls == [] and ad.details_calls == 0  # ни ИИ, ни запроса автора
    await llm.aclose()


async def test_llm_says_not_a_lead(storage):
    llm = make_llm(storage, '{"lead": false, "summary": ""}')
    monitor, sent = make_monitor(storage, llm)
    assert not await monitor.handle(FakeIncoming("газель сломалась, не приеду"))
    assert sent == []
    assert (await storage.lead_totals("2000-01-01"))["leads"] == 0
    await llm.aclose()


@pytest.mark.parametrize("answer", [httpx.ReadTimeout("slow"), 500, "мусор"])
async def test_llm_failure_sends_unchecked(storage, answer):
    llm = make_llm(storage, answer)
    monitor, sent = make_monitor(storage, llm)
    assert await monitor.handle(FakeIncoming("нужна газель перевезти шкаф"))
    text = sent[0][0]
    assert "⚠️ без проверки ИИ" in text and "📝" not in text
    await llm.aclose()


async def test_over_daily_limit_sends_unchecked(storage):
    llm = make_llm(storage, '{"lead": true, "summary": "ок"}', limit=1)
    monitor, sent = make_monitor(storage, llm)
    assert await monitor.handle(FakeIncoming("нужна газель перевезти шкаф"))
    assert await monitor.handle(FakeIncoming("нужна газель перевезти диван"))
    assert "без проверки" not in sent[0][0]
    assert "⚠️ без проверки ИИ" in sent[1][0]
    await llm.aclose()


async def test_duplicates_sent_once(storage):
    llm = make_llm(storage, '{"lead": true, "summary": "ок"}')
    monitor, sent = make_monitor(storage, llm)
    monitor.watched[555] = Watched("Другой чат", "https://t.me/other")
    assert await monitor.handle(FakeIncoming("Нужна газель перевезти шкаф!"))
    # то же сообщение, пересланное в другой чат, с другим регистром и пробелами
    assert not await monitor.handle(FakeIncoming("нужна  ГАЗЕЛЬ перевезти шкаф", chat_id=555, msg_id=5))
    assert len(sent) == 1
    await llm.aclose()


async def test_duplicate_hash_expires_after_7_days(storage):
    h = text_hash("нужна газель")
    t0 = dt.datetime(2026, 11, 14, 12, tzinfo=UTC)
    assert await storage.lead_is_new(h, t0)
    assert not await storage.lead_is_new(h, t0 + dt.timedelta(days=6, hours=23))
    assert await storage.lead_is_new(h, t0 + dt.timedelta(days=7, minutes=1))


async def test_no_texts_or_authors_in_sqlite(storage, tmp_path):
    llm = make_llm(storage, '{"lead": true, "summary": "Перевезти рояль"}')
    monitor, _ = make_monitor(storage, llm)
    assert await monitor.handle(FakeIncoming("нужна газель перевезти рояль срочно"))
    await llm.aclose()
    await storage.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    dump = "\n".join([line async for line in storage.conn.iterdump()])
    for secret in ("рояль", "Иван", "ivan_vrn", "Петров", "Лазурный"):
        assert secret not in dump
    assert text_hash("нужна газель перевезти рояль срочно") in dump


def test_card_truncates_long_text_and_escapes():
    text = lead_card("Чат <b>", "Аня", "", "", "слово " * 100, "https://t.me/c/1/2", checked=False)
    lines = text.splitlines()
    assert lines[0] == "🚚 <b>Заявка</b> · Чат &lt;b&gt;"
    assert lines[1] == "👤 Аня"
    quote = lines[3]
    assert quote.startswith("«слово слово") and quote.endswith("…»") and len(quote) == 302


async def test_votes_and_stats(storage):
    for field in ("leads", "leads", "up", "down", "up"):
        await storage.lead_count(CHAT, "2026-11-14", field)
    await storage.lead_count(5, "2026-10-01", "leads")
    assert await storage.lead_totals("2026-11-08") == {"leads": 2, "up": 2, "down": 1}
    assert await storage.lead_totals("2026-09-01") == {"leads": 3, "up": 2, "down": 1}
    assert await storage.lead_by_chat("2026-11-08") == {CHAT: {"leads": 2, "up": 2, "down": 1}}
    with pytest.raises(ValueError):
        await storage.lead_count(CHAT, "2026-11-14", "text")


# ---------- лимиты вступлений ----------

NOW = dt.datetime(2026, 11, 14, 12, 0, tzinfo=UTC)


def ago(**kw) -> dt.datetime:
    return NOW - dt.timedelta(**kw)


def test_join_wait_first_join_is_immediate():
    assert join_wait(NOW, [], 15, 240) == 0


def test_join_wait_pause_between_joins():
    assert join_wait(NOW, [ago(seconds=100)], 15, 240) == 140
    assert join_wait(NOW, [ago(seconds=240)], 15, 240) == 0
    assert join_wait(NOW, [ago(minutes=30), ago(seconds=10)], 15, 120) == 110


def test_join_wait_daily_limit():
    attempts = [ago(hours=23 - i) for i in range(15)]  # 15 вступлений за сутки, первое — 23 ч назад
    assert join_wait(NOW, attempts, 15, 120) == 3600  # ждать, пока самому старому исполнятся сутки
    assert join_wait(NOW, attempts, 16, 120) == 0
    assert join_wait(NOW, attempts[1:], 15, 120) == 0
    # попытки старше суток в лимит не входят
    assert join_wait(NOW, [ago(hours=30)] * 20, 15, 120) == 0


def test_join_wait_flood_and_disabled():
    assert join_wait(NOW, [], 15, 120, flood_until=NOW + dt.timedelta(seconds=900)) == 900
    assert join_wait(NOW, [ago(seconds=10)], 15, 120, flood_until=ago(seconds=5)) == 110
    assert join_wait(NOW, [], 0, 120) == float("inf")


async def test_join_queue_storage(storage):
    await storage.join_enqueue("https://t.me/a")
    await storage.join_enqueue("https://t.me/b")
    await storage.join_enqueue("https://t.me/a")  # повтор не дублирует
    assert await storage.join_next() == "https://t.me/a"

    await storage.join_attempt("https://t.me/a", NOW)
    await storage.join_set("https://t.me/a", "joined")
    assert await storage.join_next() == "https://t.me/b"
    assert await storage.join_attempts_since(ago(days=1)) == [NOW]
    assert await storage.join_attempts_since(NOW + dt.timedelta(seconds=1)) == []

    await storage.join_set("https://t.me/b", "manual", "вход по заявке")
    assert await storage.join_next() is None
    await storage.join_enqueue("https://t.me/b")  # сверка с файлом не возвращает manual в очередь
    assert await storage.join_next() is None
    await storage.join_enqueue("https://t.me/b", reset=True)  # а повторный /watch — возвращает
    assert await storage.join_next() == "https://t.me/b"

    await storage.join_prune(keep=set())  # monitor сняли в файле
    assert await storage.join_next() is None
    states = await storage.join_states()
    assert states["https://t.me/a"]["status"] == "joined"
    assert states["https://t.me/b"]["status"] == "removed"
    await storage.join_enqueue("https://t.me/b")  # monitor вернули
    assert await storage.join_next() == "https://t.me/b"
    # снятый с очереди чат всё равно учитывается в суточном лимите
    await storage.join_remove("https://t.me/a")
    assert await storage.join_attempts_since(ago(days=1)) == [NOW]


async def test_kv(storage):
    assert await storage.kv_get("x", "d") == "d"
    assert await storage.kv_incr("llm:2026-11-13") == 1
    assert await storage.kv_incr("llm:2026-11-14") == 1
    assert await storage.kv_incr("llm:2026-11-14") == 2
    await storage.kv_set("scout_last", "t")
    await storage.kv_delete_prefix("llm:", keep="llm:2026-11-14")
    assert await storage.kv_get("llm:2026-11-13") == ""
    assert await storage.kv_get("llm:2026-11-14") == "2"
    assert await storage.kv_get("scout_last") == "t"


# ---------- запасная модель и очередь к основной ----------


def fallback_llm(storage: Storage, primary: object, seen: list, **kw) -> LLM:
    def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        seen.append(model)
        if model == "free":
            if isinstance(primary, Exception):
                raise primary
            return httpx.Response(primary, text="error")
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"lead": true, "summary": "ок"}'}}]})

    return LLM(
        storage, "https://llm.test/v1", "k", "free", transport=httpx.MockTransport(handler), **kw
    )


@pytest.mark.parametrize("primary", [404, 429, 500, 503, httpx.ReadTimeout("slow")])
async def test_llm_fallback_model_on_429_5xx_timeout(storage, primary):
    seen: list = []
    llm = fallback_llm(storage, primary, seen, fallback_model="paid")
    assert await llm.classify_lead("нужна газель") == (True, "ок")
    # при 404 основная пробуется дважды: второй раз без response_format
    assert seen[0] == "free" and seen[-1] == "paid" and seen.count("paid") == 1
    assert await llm.used_today() == 1  # в дневной лимит идёт один вызов
    await llm.aclose()


@pytest.mark.parametrize("primary", [401, 403])
async def test_llm_no_fallback_on_auth_errors(storage, primary):
    seen: list = []
    llm = fallback_llm(storage, primary, seen, fallback_model="paid")
    with pytest.raises(LLMUnavailable, match=str(primary)):
        await llm.classify_lead("нужна газель")
    assert seen == ["free"]
    await llm.aclose()


async def test_llm_without_fallback_model(storage):
    seen: list = []
    llm = fallback_llm(storage, 429, seen)
    with pytest.raises(LLMUnavailable, match="429"):
        await llm.classify_lead("нужна газель")
    assert seen == ["free"]
    await llm.aclose()


async def test_llm_queue_15_per_minute(storage):
    now = [100.0]
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += seconds

    llm = make_llm(storage, '{"lead": false}')
    llm._interval, llm._sleep, llm._clock = 60 / 15, sleep, lambda: now[0]
    for _ in range(3):
        await llm.classify_lead("x")
    assert slept == [4.0, 4.0]  # первый сразу, дальше — раз в 4 секунды
    now[0] += 60
    await llm.classify_lead("x")
    assert slept == [4.0, 4.0]  # после паузы ждать не нужно
    await llm.aclose()
