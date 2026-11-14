from __future__ import annotations

import pytest

from app.config import ConfigError, Settings, load_leads
from app.leads import table as T
from app.leads.table import COLUMNS, Table
from app.leads.text import WordFilter, has_word, normalize_link, text_hash
from tests.conftest import ROOT

EXAMPLE = ROOT / "config" / "leads.example.yml"


# ---------- конфиг ----------


def test_example_leads_config_valid():
    cfg = load_leads(EXAMPLE)
    assert "ЖК Воронеж" in cfg.scout.queries and "ЖК Озерки" in cfg.scout.all_queries()
    assert cfg.scout.only_types == ["ЖК-группа ТГ"] and cfg.scout.target == 60
    assert "воронеж" in cfg.scout.geo_words
    assert cfg.monitor.keywords and cfg.monitor.stop_words


@pytest.mark.parametrize(
    ("yml", "msg"),
    [
        ("scout: {queries: [], geo_words: [воронеж]}\nmonitor: {keywords: [газель]}", "scout.queries"),
        ("scout: {queries: [a], geo_words: [b]}\nmonitor: {keywords: [' ']}", "monitor.keywords"),
        ("scout: {queries: [a], geo_words: [b]}\nmonitor: {keywords: [c], extra: 1}", "monitor.extra"),
        ("scout: [", "ошибка YAML"),
    ],
)
def test_leads_config_errors(tmp_path, yml, msg):
    f = tmp_path / "leads.yml"
    f.write_text(yml, encoding="utf-8")
    with pytest.raises(ConfigError, match=msg):
        load_leads(f)


def test_leads_config_missing(tmp_path):
    with pytest.raises(ConfigError, match="leads.example.yml"):
        load_leads(tmp_path / "leads.yml")


def test_settings_v2_defaults_and_empty_values(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # без .env
    for name in ("TG_API_ID", "JOIN_PER_DAY", "LLM_DAILY_LIMIT"):
        monkeypatch.setenv(name, "")
    s = Settings()
    assert (s.TG_API_ID, s.JOIN_PER_DAY, s.LLM_DAILY_LIMIT) == (0, 15, 300)
    assert not s.userbot_configured
    assert s.userbot_session.name == "userbot.session"
    monkeypatch.setenv("TG_API_ID", "12345")
    monkeypatch.setenv("TG_API_HASH", "abc")
    assert Settings().userbot_configured


# ---------- фильтр слов ----------


@pytest.fixture(scope="module")
def word_filter() -> WordFilter:
    cfg = load_leads(EXAMPLE)
    return WordFilter(cfg.monitor.keywords, cfg.monitor.stop_words)


@pytest.mark.parametrize(
    "text",
    [
        "нужна газель перевезти шкаф",
        "Соседи, посоветуйте грузчиков на субботу",
        "Кто может ПЕРЕВЕЗТИ диван и холодильник с Северного в Шилово?",
        "Ищу Газель, переезжаем в ЖК Лазурный",
        "нужно вывезти строительный мусор",
        "Переезжаем, ищем машину ещё на завтра",
    ],
)
def test_filter_passes_requests(word_filter, text):
    assert word_filter.matches(text)


@pytest.mark.parametrize(
    "text",
    [
        "грузоперевозки недорого, звоните",
        "Предлагаю услуги грузчиков, газель 4 метра",
        "Переезды по городу и области. Обращайтесь!",
        "Продам диван, самовывоз",
        "Всем доброе утро!",
        "",
    ],
)
def test_filter_rejects_ads_and_noise(word_filter, text):
    assert not word_filter.matches(text)


def test_filter_matches_word_start_only():
    f = WordFilter(["груз"], ["звоните"])
    assert f.matches("нужен грузчик")
    assert not f.matches("идёт загрузка файла")  # «груз» в середине слова
    assert not f.matches("нужен грузчик, звоните")
    assert f.matches("нужен грузчик, позвоните")  # стоп-слово тоже сравнивается с началом слова
    assert not WordFilter([], []).matches("газель")


def test_has_word_geo():
    geo = ["воронеж", "левый берег"]
    assert has_word("ЖК «Лазурный» | Воронеж", geo)
    assert has_word("Чат Воронежа и области", geo)
    assert has_word("Соседи. Левый Берег", geo)
    assert not has_word("Барахолка Липецк", geo)


def test_text_hash_ignores_case_spaces_punctuation():
    a = text_hash("Нужна газель, перевезти шкаф!")
    assert a == text_hash("  нужна ГАЗЕЛЬ перевезти\nшкаф ")
    assert a != text_hash("Нужна газель перевезти диван")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://t.me/vrn_chat", "https://t.me/vrn_chat"),
        ("t.me/vrn_chat/1234", "https://t.me/vrn_chat"),
        ("@vrn_chat", "https://t.me/vrn_chat"),
        ("vrn_chat", "https://t.me/vrn_chat"),
        ("http://telegram.me/s/vrn_chat?before=5", "https://t.me/vrn_chat"),
        ("https://t.me/+AbCdEf123456", None),
        ("https://t.me/joinchat/AbCdEf", None),
        ("https://vk.com/vrn_chat", None),
        ("", None),
    ],
)
def test_normalize_link(value, expected):
    assert normalize_link(value) == expected


# ---------- слияние CSV ----------


def row(link: str, **kw: str) -> dict[str, str]:
    base = dict.fromkeys(COLUMNS, "")
    base.update(
        {
            T.NAME: "Чат",
            T.TYPE: "Другое ТГ",
            T.PLATFORM: "Telegram",
            T.LINK: link,
            T.STATUS: T.STATUS_NEW,
            T.FOUND_AT: "2026-11-14",
            T.CHECKED_AT: "2026-11-14",
            T.QUERY: "Воронеж ЖК",
        }
    )
    base.update(kw)
    return base


def test_merge_adds_and_updates_without_touching_manual_columns():
    table = Table()
    assert table.merge([row("https://t.me/a", **{T.MEMBERS: "100", T.RULES: "платно — 500 ₽"})]) == (1, 0)

    # Владелец правит файл руками (и ссылку — в другом регистре)
    mine = table.rows[0]
    mine.update({T.STATUS: "Разместили", T.POSTED: "2026-11-20", T.MONITOR: "yes", T.JOINED: "yes"})
    mine[T.LINK] = "https://t.me/A"

    fresh = row(
        "https://t.me/a",
        **{T.NAME: "Чат (новое имя)", T.MEMBERS: "250", T.RULES: "", T.QUERY: "другой", T.FOUND_AT: "2026-12-20"},
    )
    fresh[T.CHECKED_AT] = "2026-12-20"
    assert table.merge([fresh, row("https://t.me/b")]) == (1, 1)

    assert len(table.rows) == 2
    assert mine[T.NAME] == "Чат (новое имя)" and mine[T.MEMBERS] == "250"
    assert mine[T.CHECKED_AT] == "2026-12-20"
    # ручные колонки и то, что ставится один раз, не тронуты
    assert (mine[T.STATUS], mine[T.POSTED], mine[T.MONITOR]) == ("Разместили", "2026-11-20", "yes")
    assert (mine[T.JOINED], mine[T.QUERY], mine[T.FOUND_AT]) == ("yes", "Воронеж ЖК", "2026-11-14")
    # пустой ответ ИИ не стирает прежние правила
    assert mine[T.RULES] == "платно — 500 ₽"
    assert mine[T.LINK] == "https://t.me/A"


def test_merge_keeps_manual_join_mark():
    table = Table([row("https://t.me/a", **{T.JOIN: "manual"})])
    table.merge([row("https://t.me/a", **{T.JOIN: "open"})])
    assert table.rows[0][T.JOIN] == "manual"
    table.merge([row("https://t.me/a", **{T.JOIN: "request"})])
    assert table.rows[0][T.JOIN] == "request"


def test_csv_roundtrip_bom_columns_and_extra(tmp_path):
    path = tmp_path / "chats" / "telegram.csv"
    table = Table([row("https://t.me/a", **{T.NAME: 'ЖК «Лазурный», "чат"', T.NOTES: "строка\nвторая"})])
    table.save(path)
    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    header = raw.decode("utf-8-sig").splitlines()[0]
    assert header == ",".join(COLUMNS)
    assert header.startswith(
        "Название,Тип,Подписчики,Площадка,Ссылка,Статус,Правила рекламы,Контакт админа,Дата размещения,Заметки"
    )

    # Владелец добавил свою колонку — она сохраняется
    text = raw.decode("utf-8-sig")
    lines = text.split("\n", 1)
    path.write_text(lines[0] + ",Моё\n" + lines[1].rstrip("\n") + ",заметка\n", encoding="utf-8-sig")
    loaded = Table.load(path)
    assert loaded.extra_columns == ["Моё"]
    assert loaded.rows[0][T.NAME] == 'ЖК «Лазурный», "чат"'
    assert loaded.rows[0]["Моё"] == "заметка"
    loaded.merge([row("https://t.me/b")])
    loaded.save(path)
    again = Table.load(path)
    assert again.rows[0]["Моё"] == "заметка" and again.rows[1]["Моё"] == ""
    assert Table.load(tmp_path / "nope.csv").rows == []


def test_monitored_rows():
    table = Table([row("https://t.me/a", monitor="yes"), row("https://t.me/b"), row("https://t.me/c", monitor="Да")])
    assert [r[T.LINK] for r in table.monitored()] == ["https://t.me/a", "https://t.me/c"]
