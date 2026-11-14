"""Живость чата и следы рекламы — по последним сообщениям, без вступления (spec-leads §3.3a).

Публичную группу Telegram отдаёт историю и тем, кто в ней не состоит. По последней сотне
сообщений видно: говорят ли там люди или чат пустой/заспамлен, публикуют ли участники
объявления услуг (значит, бесплатно можно), есть ли конкуренты-перевозчики, стоит ли бот-капча.

Тексты сообщений здесь только считаются и уходят в ИИ — нигде не хранятся.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field

from app.leads.text import has_word, normalize

WEEK = dt.timedelta(days=7)

# Объявление услуги: так пишут исполнители, а не те, кто ищет.
AD_WORDS = (
    "услуг", "звоните", "обращайтесь", "пишите в лс", "пишите в личку", "в лс", "недорого",
    "прайс", "скидк", "выполню", "выполним", "оказываем", "предлагаю", "предлагаем",
    "работаем без выходных", "опыт работы", "наш сайт",
)  # fmt: skip
CARGO_WORDS = ("груз", "переезд", "газел", "грузчик", "вывоз", "перевоз", "грузотакси", "манипулятор")
PHONE_RE = re.compile(r"(?:\+7|(?<!\d)8)[\s(\-]*\d{3}[\s)\-]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}")
CAPTCHA_WORDS = (
    "капч", "captcha", "не бот", "не робот", "подтвердите", "нажмите на кнопку", "нажмите кнопку",
    "пройдите проверку", "пройти проверку", "verify", "решите пример",
)  # fmt: skip
BAN_PHRASES = (
    "реклама запрещ", "рекламу запрещ", "без рекламы", "реклама не допуска", "бан за рекламу",
    "за рекламу бан", "рекламы нет", "никакой рекламы", "реклама только платн", "реклама платная",
)  # fmt: skip


@dataclass
class Msg:
    date: dt.datetime  # с часовым поясом (UTC)
    sender_id: int
    text: str = ""
    bot: bool = False  # автор — бот
    service: str = ""  # "join" / "leave" / "other" — служебное сообщение


@dataclass
class Activity:
    msgs_7d: int = 0  # сообщений людей за 7 дней
    authors_7d: int = 0  # разных авторов за 7 дней
    last_days: int | None = None  # сколько дней назад писал человек; None — не писал вовсе
    joins_7d: int = 0  # вступлений за 7 дней (служебные сообщения)
    ads_seen: int = 0  # сколько разных авторов публикуют объявления услуг
    competitors: int = 0  # из них — перевозчики и грузчики
    captcha: bool = False  # в чате работает бот-проверка новичков
    ads_banned: bool = False  # в сообщениях бота или участников — «реклама запрещена» и т. п.
    sample: list[str] = field(default_factory=list)  # несколько последних реплик для ИИ, без авторов


def is_ad(text: str) -> bool:
    t = normalize(text)
    return bool(PHONE_RE.search(text)) or has_word(t, AD_WORDS)


def measure(msgs: list[Msg], now: dt.datetime, sample_size: int = 25, limit: int = 0) -> Activity:
    """`limit` — сколько сообщений запрашивали. Если пришло столько же и все они моложе недели,
    чат активнее, чем видно в выборке: число сообщений за 7 дней оценивается по темпу."""
    act = Activity()
    week_ago = now - WEEK
    people = [m for m in msgs if not m.service and not m.bot and m.text.strip()]
    recent = [m for m in people if m.date >= week_ago]
    act.msgs_7d = len(recent)
    if limit and len(msgs) >= limit:
        oldest = min(m.date for m in msgs)
        if oldest > week_ago:
            span = max(now - oldest, dt.timedelta(hours=1))
            act.msgs_7d = round(len(recent) * (WEEK / span))
    act.authors_7d = len({m.sender_id for m in recent})
    if people:
        act.last_days = max(0, (now - max(m.date for m in people)).days)
    act.joins_7d = sum(1 for m in msgs if m.service == "join" and m.date >= week_ago)

    advertisers: set[int] = set()
    cargo: set[int] = set()
    for m in people:
        if is_ad(m.text):
            advertisers.add(m.sender_id)
            if has_word(normalize(m.text), CARGO_WORDS):
                cargo.add(m.sender_id)
    act.ads_seen, act.competitors = len(advertisers), len(cargo)

    for m in msgs:
        t = normalize(m.text)
        if m.bot and any(w in t for w in CAPTCHA_WORDS):
            act.captcha = True
        if any(p in t for p in BAN_PHRASES):
            act.ads_banned = True

    newest = sorted(people, key=lambda m: m.date, reverse=True)[:sample_size]
    act.sample = [" ".join(m.text.split())[:200] for m in newest]
    return act


@dataclass
class Thresholds:
    monitor_week: int = 100  # «живой» чат для мониторинга: сообщений людей за 7 дней…
    monitor_authors: int = 20  # …и разных авторов за 7 дней
    dead_week: int = 7  # меньше — чат мёртвый
    ads_min: int = 2  # столько разных авторов с объявлениями — значит, реклама терпима


MONITOR, POST, BOTH, DEAD, TEMPLATE = "мониторинг", "объявление", "оба", "мёртвый", "шаблон"


def choose_tactic(
    kind: str,
    can_write: bool,
    act: Activity | None,
    rules: str,
    free_ads: str,
    th: Thresholds,
    template: bool = False,
) -> str:
    """Какая тактика подходит площадке: следить за заявками, дать объявление, обе или никакая.

    Решение — подсказка для владельца: `monitor` и «Статус» он ставит сам.
    """
    if act is None or kind != "group":
        return ""
    if act.msgs_7d < th.dead_week:
        return DEAD
    live = act.msgs_7d >= th.monitor_week and act.authors_7d >= th.monitor_authors
    if template and not live:
        return TEMPLATE
    banned = act.ads_banned or rules.startswith("запрещена") or free_ads == "нет"
    allowed = rules.startswith("разрешена") or free_ads == "да" or act.ads_seen >= th.ads_min
    post = can_write and allowed and not banned and act.authors_7d >= 3
    if live and post:
        return BOTH
    if live:
        return MONITOR
    return POST if post else ""


def template_keys(title: str, about: str) -> str:
    """Отпечаток описания без слов из названия: у клонированных сетей чатов он совпадает."""
    own = set(re.findall(r"\w+", normalize(title)))
    words = [w for w in re.findall(r"\w+", normalize(about)) if w not in own and not w.isdigit()]
    return " ".join(words) if len(words) >= 5 else ""
