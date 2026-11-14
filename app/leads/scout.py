"""Разведка площадок (spec-leads §3): поиск → гео-фильтр → сведения о чате → ИИ → строки таблицы."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import random
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from html import escape

from app.config import ScoutConfig
from app.leads import table as T
from app.leads.table import Table
from app.leads.text import has_word, normalize
from app.llm import LLM, LLMUnavailable
from app.userbot import ChatInfo, FloodWait, FoundChat, UserbotError

log = logging.getLogger(__name__)

STALE_DAYS = 30
MAX_FLOOD_WAIT = 600  # дольше не ждём: разведка заканчивается тем, что успела
MAX_AI_GEO_CHECKS = 10  # сколько площадок без гео-слов за запуск показываем ИИ
AD_RULES = ("разрешена", "запрещена", "платно", "через админа")
FLEA_WORDS = ("барахолк", "объявлени", "куплю", "продам", "купи-продай", "купи продай", "отдам даром")
# Порядок в «Лучших новых»: сначала чаты жильцов, потом районы.
TYPE_PRIORITY = {"ЖК-группа ТГ": 0, "Районный чат ТГ": 1, "Другое ТГ": 2, "Канал ТГ": 3, "Барахолка ТГ": 4}
CAPTCHA_WORDS = ("капч", "captcha", "антиспам-бот", "пройти проверку", "пройдите проверку")

SCOUT_SYSTEM = (
    "Ты помогаешь сервису грузоперевозок в Воронеже разобрать Telegram-чаты и каналы как площадки "
    "для объявлений. Отвечай строго одним JSON-объектом, без пояснений. Ничего не выдумывай: "
    "если данных нет — пустая строка."
)
SCOUT_QUESTION = """По названию, описанию и закреплённому сообщению ответь JSON с полями:
"voronezh": true|false — площадка про Воронеж или Воронежскую область (город, район, ЖК);
"type": одно из "ЖК-группа ТГ", "Районный чат ТГ", "Барахолка ТГ", "Канал ТГ", "Другое ТГ":
  "ЖК-группа ТГ" — чат жильцов дома или ЖК; "Районный чат ТГ" — чат жителей района, посёлка, микрорайона;
  "Барахолка ТГ" — чат, где в основном продают и покупают вещи (купи-продай, объявления, отдам даром),
  даже если он привязан к району или ЖК;
"ads": "разрешена" | "запрещена" | "платно" | "через админа" | "" — правила рекламы, если о них сказано;
"ads_note": одна короткая фраза о правилах рекламы, по тексту, или "";
"admin": контакт админа (@юзернейм, ссылка или телефон), только если он прямо указан в описании или закрепе, иначе "".
"""

Sleep = Callable[[float], Awaitable[None]]


@dataclass
class Analysis:
    voronezh: bool = False
    type: str = ""
    rules: str = ""
    admin: str = ""


@dataclass
class ScoutOutcome:
    rows: list[dict[str, str]] = field(default_factory=list)
    queries_done: int = 0
    found: int = 0
    skipped_fresh: int = 0
    skipped_offtopic: int = 0
    skipped_discussion: int = 0
    warnings: list[str] = field(default_factory=list)
    llm_used: bool = False


class ScoutUserbot:
    """Что разведке нужно от юзербота (в тестах — подделка)."""

    async def search(self, query: str, limit: int = 50) -> list[FoundChat]: ...

    async def chat_info(self, username: str) -> ChatInfo: ...


def guess_type(info: ChatInfo) -> str:
    """Тип площадки без ИИ — по названию и описанию."""
    if info.kind == "channel":
        return "Канал ТГ"
    text = normalize(f"{info.title} {info.about}")
    if has_word(normalize(info.title), FLEA_WORDS):
        return "Барахолка ТГ"
    if has_word(text, ("жк", "жилой комплекс", "жилого комплекса", "жильц", "домовой чат", "чат дома")):
        return "ЖК-группа ТГ"
    if has_word(text, FLEA_WORDS):
        return "Барахолка ТГ"
    if has_word(text, ("район", "микрорайон", "сосед", "жител", "берег")):
        return "Районный чат ТГ"
    return "Другое ТГ"


def _contact_in_source(contact: str, source: str) -> bool:
    """Контакт принимаем, только если он дословно есть в описании или закрепе."""
    src = source.lower()
    tokens = re.findall(r"@?[a-z][a-z0-9_]{3,31}", contact.lower())
    tokens = [t.lstrip("@") for t in tokens if t.lstrip("@") not in ("https", "http", "telegram")]
    if any(t in src for t in tokens):
        return True
    digits = re.sub(r"\D", "", contact)
    return len(digits) >= 7 and digits[-7:] in re.sub(r"\D", "", source)


def parse_analysis(obj: dict, info: ChatInfo) -> Analysis:
    """Проверяет ответ ИИ: неизвестные значения отбрасываются, контакт сверяется с исходным текстом."""
    kind = obj.get("type") if obj.get("type") in T.TYPES else ""
    if info.kind == "channel":
        kind = "Канал ТГ"
    ads = str(obj.get("ads") or "").strip().lower()
    note = " ".join(str(obj.get("ads_note") or "").split())[:160]
    rules = ""
    if ads in AD_RULES:
        rules = f"{ads} — {note}" if note else ads
    admin = " ".join(str(obj.get("admin") or "").split())[:100]
    if admin and not _contact_in_source(admin, f"{info.about}\n{info.pinned}"):
        admin = ""
    return Analysis(voronezh=obj.get("voronezh") is True, type=kind, rules=rules, admin=admin)


def notes_for(info: ChatInfo) -> str:
    notes = []
    if info.kind == "group" and not info.can_write:
        notes.append("писать нельзя")
    if info.slowmode:
        notes.append(f"медленный режим {info.slowmode} с")
    if info.join_request:
        notes.append("вход по заявке")
    if needs_captcha(info):
        notes.append("возможна капча")
    if info.kind == "channel" and info.linked:
        notes.append(f"обсуждение: https://t.me/{info.linked}")
    return "; ".join(notes)


def needs_captcha(info: ChatInfo) -> bool:
    text = normalize(f"{info.about} {info.pinned}")
    return any(w in text for w in CAPTCHA_WORDS)


def join_mode(info: ChatInfo) -> str:
    if info.join_request:
        return "request"
    return "manual" if needs_captcha(info) else "open"


def build_row(info: ChatInfo, analysis: Analysis | None, query: str, today: str) -> dict[str, str]:
    a = analysis or Analysis()
    return {
        T.NAME: " ".join(info.title.split()),
        T.TYPE: a.type or guess_type(info),
        T.MEMBERS: str(info.members) if info.members else "",
        T.PLATFORM: "Telegram",
        T.LINK: info.link,
        T.STATUS: T.STATUS_NEW,
        T.RULES: a.rules,
        T.ADMIN: a.admin,
        T.POSTED: "",
        T.NOTES: notes_for(info),
        T.TG_ID: str(info.tg_id),
        T.KIND: info.kind,
        T.CAN_WRITE: "yes" if info.can_write else "no",
        T.JOIN: join_mode(info),
        T.MONITOR: "",
        T.JOINED: "",
        T.QUERY: query,
        T.FOUND_AT: today,
        T.CHECKED_AT: today,
    }


def is_fresh(row: dict[str, str] | None, today: dt.date) -> bool:
    if row is None:
        return False
    try:
        checked = dt.date.fromisoformat(row.get(T.CHECKED_AT, ""))
    except ValueError:
        return False
    return (today - checked).days <= STALE_DAYS


async def _with_flood[R](call: Callable[[], Awaitable[R]], sleep: Sleep) -> R:
    """FloodWait — ждём, сколько сказал Telegram, и повторяем один раз."""
    try:
        return await call()
    except FloodWait as e:
        if e.seconds > MAX_FLOOD_WAIT:
            raise
        log.warning("Разведка: FloodWait %s с — жду", e.seconds)
        await sleep(e.seconds + 5)
        return await call()


async def analyze(llm: LLM, info: ChatInfo, reserve: int) -> Analysis | None:
    text = (
        f"{SCOUT_QUESTION}\nНазвание: {info.title}\nТип: {'канал' if info.kind == 'channel' else 'группа'}\n"
        f"Описание: {info.about[:1200] or '—'}\nЗакреп: {info.pinned[:1500] or '—'}"
    )
    try:
        return parse_analysis(await llm.ask_json(SCOUT_SYSTEM, text, reserve=reserve), info)
    except LLMUnavailable as e:
        log.info("Разведка без ИИ: %s", e)
        return None


async def run_scout(
    userbot: ScoutUserbot,
    llm: LLM,
    cfg: ScoutConfig,
    existing: Table,
    *,
    today: dt.date,
    queries_per_run: int = 20,
    max_chats: int = 60,
    offset: int = 0,
    sleep: Sleep = asyncio.sleep,
) -> ScoutOutcome:
    """Один запуск разведки. Ничего не пишет — возвращает строки для слияния с таблицей."""
    out = ScoutOutcome()
    n = len(cfg.queries)
    queries = [cfg.queries[(offset + i) % n] for i in range(min(queries_per_run, n))]

    candidates: dict[str, tuple[FoundChat, str]] = {}
    for i, query in enumerate(queries):
        if i:
            await sleep(random.uniform(3, 8))
        try:
            found = await _with_flood(lambda q=query: userbot.search(q), sleep)
        except FloodWait as e:
            out.warnings.append(f"Telegram ограничил поиск на {e.seconds} с — часть запросов отложена")
            break
        except UserbotError as e:
            out.warnings.append(f"запрос «{query}»: {e}")
            continue
        out.queries_done += 1
        for chat in found:
            candidates.setdefault(chat.username.lower(), (chat, query))
    out.found = len(candidates)

    todo = []
    for chat, query in candidates.values():
        if is_fresh(existing.find(chat.link), today):
            out.skipped_fresh += 1
        else:
            todo.append((chat, query))
    todo.sort(key=lambda p: p[0].members or 0, reverse=True)
    if len(todo) > max_chats:
        out.warnings.append(f"площадок много: проверены {max_chats} из {len(todo)}, остальные — в следующий раз")
        todo = todo[:max_chats]

    # Треть дневного лимита ИИ оставляем мониторингу заявок.
    reserve = llm.daily_limit // 3
    ai_geo_checks = 0
    for i, (chat, query) in enumerate(todo):
        if i:
            await sleep(random.uniform(2, 5))
        try:
            info = await _with_flood(lambda c=chat: userbot.chat_info(c.username), sleep)
        except FloodWait as e:
            out.warnings.append(f"Telegram ограничил запросы на {e.seconds} с — остальное в следующий раз")
            break
        except UserbotError as e:
            log.info("Разведка: %s пропущен (%s)", chat.link, e)
            continue
        if info.discussion:
            # Чат комментариев канала: людей много, но говорят о постах канала, а не о быте.
            out.skipped_discussion += 1
            continue
        geo = has_word(f"{info.title} {info.about}", cfg.geo_words)
        if not geo:
            if ai_geo_checks >= MAX_AI_GEO_CHECKS:
                continue
            ai_geo_checks += 1
        analysis = await analyze(llm, info, reserve)
        out.llm_used = out.llm_used or analysis is not None
        if not geo and not (analysis and analysis.voronezh):
            continue
        row = build_row(info, analysis, query, today.isoformat())
        if row[T.TYPE] in cfg.skip_types or has_word(f"{info.title} {info.about}", cfg.skip_words):
            out.skipped_offtopic += 1
            continue
        out.rows.append(row)
    if out.rows and not out.llm_used:
        out.warnings.append("ИИ недоступен — «Правила рекламы» и «Контакт админа» не заполнены")
    return out


def summary_text(out: ScoutOutcome, added: int, updated: int, new_links: set[str], extra: list[str]) -> str:
    """Итог владельцу: «найдено N новых, M обновлено» и ВСЕ новые площадки (ЖК → районы → прочее)."""
    lines = [f"🔎 <b>Разведка</b>: найдено {added} новых, {updated} обновлено"]
    skipped = ""
    if out.skipped_offtopic:
        skipped += f" · барахолки и т. п.: {out.skipped_offtopic}"
    if out.skipped_discussion:
        skipped += f" · чаты каналов: {out.skipped_discussion}"
    lines.append(
        f"Запросов: {out.queries_done} · площадок в поиске: {out.found} · свежих пропущено: {out.skipped_fresh}"
        + (f"\nНе подошли{skipped}" if skipped else "")
    )
    new_rows = sorted(
        (r for r in out.rows if r[T.LINK] in new_links),
        key=lambda r: (TYPE_PRIORITY.get(r[T.TYPE], 2), -int(r[T.MEMBERS] or 0)),
    )
    current_type = None
    for i, r in enumerate(new_rows, start=1):
        if r[T.TYPE] != current_type:
            current_type = r[T.TYPE]
            lines.append("")
            lines.append(f"<b>{escape(current_type)}</b>")
        members = f"{int(r[T.MEMBERS]):,}".replace(",", " ") if r[T.MEMBERS] else "?"
        extra_note = f" · {escape(r[T.RULES])}" if r[T.RULES] else ""
        lines.append(f'{i}. <a href="{escape(r[T.LINK])}">{escape(r[T.NAME])}</a> — {members}{extra_note}')
    for w in [*out.warnings, *extra]:
        lines.append(f"⚠️ {escape(w)}")
    return "\n".join(lines)
