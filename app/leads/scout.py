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
from app.leads import activity as A
from app.leads import table as T
from app.leads.activity import Activity
from app.leads.table import Table
from app.leads.text import has_word, normalize
from app.llm import LLM, LLMUnavailable
from app.userbot import ChatInfo, FloodWait, FoundChat, UserbotError

log = logging.getLogger(__name__)

STALE_DAYS = 30
MAX_FLOOD_WAIT = 600  # дольше не ждём: разведка заканчивается тем, что успела
MAX_AI_GEO_CHECKS = 10  # сколько площадок без гео-слов за запуск показываем ИИ
AD_RULES = ("разрешена", "запрещена", "платно", "через админа")
FLEA_WORDS = ("барахолк", "куплю", "продам", "купи-продай", "купи продай", "отдам даром")
SERVICE_WORDS = ("услуг", "мастер", "объявлени", "реклам", "бизнес", "предпринимател")
# Порядок групп в итоге: сначала то, с чем можно работать сразу.
TACTIC_ORDER = {A.BOTH: 0, A.MONITOR: 1, A.POST: 2, "": 3, A.TEMPLATE: 4, A.DEAD: 5}
TYPE_PRIORITY = {"ЖК-группа ТГ": 0, "Районный чат ТГ": 1, "Городской чат ТГ": 2, "Тематический чат ТГ": 3}
TACTIC_TITLE = {
    A.BOTH: "🎯📢 Живые и реклама терпима — оба способа",
    A.MONITOR: "🎯 Живые — ловить заявки (/watch)",
    A.POST: "📢 Можно дать объявление бесплатно",
    "": "❔ Без вывода — смотреть руками",
    A.TEMPLATE: "🧬 Шаблонные клоны (сеть одинаковых чатов)",
    A.DEAD: "💀 Мёртвые — меньше недели жизни",
}
HIDDEN_TACTICS = (A.DEAD, A.TEMPLATE)  # владельцу не показываем: работать с ними нельзя
CAPTCHA_WORDS = ("капч", "captcha", "антиспам-бот", "пройти проверку", "пройдите проверку")

SCOUT_SYSTEM = (
    "Ты помогаешь сервису грузоперевозок в Воронеже разобрать Telegram-чаты и каналы как площадки "
    "для объявлений. Отвечай строго одним JSON-объектом, без пояснений. Ничего не выдумывай: "
    "если данных нет — пустая строка."
)
SCOUT_QUESTION = """По названию, описанию и закреплённому сообщению ответь JSON с полями:
"voronezh": true|false — площадка про Воронеж или Воронежскую область (город, район, ЖК);
"type": одно из "ЖК-группа ТГ", "Районный чат ТГ", "Городской чат ТГ", "Тематический чат ТГ",
  "Доска услуг ТГ", "Барахолка ТГ", "Канал ТГ", "Другое ТГ":
  "ЖК-группа ТГ" — чат жильцов дома или ЖК; "Районный чат ТГ" — чат жителей района, посёлка, микрорайона;
  "Городской чат ТГ" — общий чат жителей города для общения;
  "Тематический чат ТГ" — люди одной темы: аренда жилья, мамы, студенты, дачники, ремонт, автомобилисты;
  "Доска услуг ТГ" — чат, куда мастера и компании публикуют объявления об услугах;
  "Барахолка ТГ" — чат, где в основном продают и покупают вещи (купи-продай, отдам даром),
  даже если он привязан к району или ЖК;
"ads": "разрешена" | "запрещена" | "платно" | "через админа" | "" — правила рекламы, если о них сказано;
"ads_note": одна короткая фраза о правилах рекламы, по тексту, или "";
"admin": контакт админа (@юзернейм, ссылка или телефон), только если он прямо указан в описании или закрепе, иначе "";
"free_ads": "да" | "нет" | "неясно" — может ли обычный участник бесплатно опубликовать объявление об услуге.
  "да" — так прямо разрешено, или в последних сообщениях участники публикуют объявления услуг и их не удаляют;
  "нет" — реклама запрещена или только платно; иначе "неясно";
"free_ads_why": короткая фраза, на чём основан вывод о "free_ads", или "".
"""

Sleep = Callable[[float], Awaitable[None]]


@dataclass
class Analysis:
    voronezh: bool = False
    type: str = ""
    rules: str = ""
    admin: str = ""
    free_ads: str = ""  # да / нет / неясно
    free_ads_why: str = ""


@dataclass
class ScoutOutcome:
    rows: list[dict[str, str]] = field(default_factory=list)
    queries_done: int = 0
    found: int = 0
    skipped_fresh: int = 0
    skipped_offtopic: int = 0
    skipped_discussion: int = 0
    skipped_channels: int = 0
    templates: int = 0
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
    if has_word(normalize(info.title), SERVICE_WORDS):
        return "Доска услуг ТГ"
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
    free = str(obj.get("free_ads") or "").strip().lower()
    free = free if free in ("да", "нет", "неясно") else ""
    why = " ".join(str(obj.get("free_ads_why") or "").split())[:120] if free in ("да", "нет") else ""
    return Analysis(
        voronezh=obj.get("voronezh") is True,
        type=kind,
        rules=rules,
        admin=admin,
        free_ads=free,
        free_ads_why=why,
    )


def notes_for(info: ChatInfo, act: Activity | None = None, analysis: Analysis | None = None) -> str:
    notes = []
    if info.kind == "group" and not info.can_write:
        notes.append("писать нельзя")
    if info.slowmode:
        notes.append(f"медленный режим {info.slowmode} с")
    if info.join_request:
        notes.append("вход по заявке")
    if needs_captcha(info) or (act and act.captcha):
        notes.append("капча на входе")
    if act is not None and info.kind == "group":
        if act.competitors:
            notes.append(f"перевозчики публикуются: {act.competitors}")
        elif act.ads_seen:
            notes.append(f"объявления услуг: {act.ads_seen} авт.")
        if act.ads_banned:
            notes.append("в чате пишут «реклама запрещена»")
    if analysis and analysis.free_ads_why:
        notes.append(f"ИИ: {analysis.free_ads_why}")
    if info.kind == "channel" and info.linked:
        notes.append(f"обсуждение: https://t.me/{info.linked}")
    return "; ".join(notes)


def needs_captcha(info: ChatInfo) -> bool:
    text = normalize(f"{info.about} {info.pinned}")
    return any(w in text for w in CAPTCHA_WORDS)


def join_mode(info: ChatInfo, act: Activity | None = None) -> str:
    if info.join_request:
        return "request"
    return "manual" if needs_captcha(info) or (act and act.captcha) else "open"


def thresholds(cfg: ScoutConfig) -> A.Thresholds:
    return A.Thresholds(
        monitor_week=cfg.monitor_week,
        monitor_authors=cfg.monitor_authors,
        dead_week=cfg.dead_week,
        ads_min=cfg.ads_min,
    )


def build_row(
    info: ChatInfo,
    analysis: Analysis | None,
    query: str,
    today: str,
    act: Activity | None = None,
    th: A.Thresholds | None = None,
) -> dict[str, str]:
    a = analysis or Analysis()
    has_act = act is not None and info.kind == "group"
    tactic = A.choose_tactic(info.kind, info.can_write, act, a.rules, a.free_ads, th or A.Thresholds())
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
        T.NOTES: notes_for(info, act, analysis),
        T.TG_ID: str(info.tg_id),
        T.KIND: info.kind,
        T.CAN_WRITE: "yes" if info.can_write else "no",
        T.JOIN: join_mode(info, act),
        T.MONITOR: "",
        T.JOINED: "",
        T.QUERY: query,
        T.FOUND_AT: today,
        T.CHECKED_AT: today,
        T.TACTIC: tactic,
        T.MSGS_7D: str(act.msgs_7d) if has_act else "",
        T.AUTHORS_7D: str(act.authors_7d) if has_act else "",
        T.LAST_MSG: (str(act.last_days) if act.last_days is not None else "нет") if has_act else "",
        T.ADS_SEEN: str(act.ads_seen) if has_act else "",
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


async def analyze(llm: LLM, info: ChatInfo, reserve: int, act: Activity | None = None) -> Analysis | None:
    text = (
        f"{SCOUT_QUESTION}\nНазвание: {info.title}\nТип: {'канал' if info.kind == 'channel' else 'группа'}\n"
        f"Описание: {info.about[:1200] or '—'}\nЗакреп: {info.pinned[:1500] or '—'}"
    )
    if act is not None and act.sample:
        lines = "\n".join(f"- {m}" for m in act.sample)
        text += f"\nПоследние сообщения участников (новые сверху):\n{lines[:4000]}"
    try:
        return parse_analysis(await llm.ask_json(SCOUT_SYSTEM, text, reserve=reserve), info)
    except LLMUnavailable as e:
        log.info("Разведка без ИИ: %s", e)
        return None


async def _activity(userbot: ScoutUserbot, info: ChatInfo, cfg: ScoutConfig, sleep: Sleep) -> Activity | None:
    """История группы без вступления; нет метода, канал или ошибка — None (тактика не считается)."""
    recent = getattr(userbot, "recent_messages", None)
    if recent is None or info.kind != "group" or cfg.history <= 0:
        return None
    try:
        msgs = await _with_flood(lambda: recent(info.username, cfg.history), sleep)
    except UserbotError as e:
        log.info("Разведка: история %s недоступна (%s)", info.link, e)
        return None
    return A.measure(msgs, dt.datetime.now(dt.UTC))


def mark_templates(
    rows: list[dict[str, str]], keys: dict[str, str], th: A.Thresholds, min_size: int = 3
) -> int:
    """Чаты с одинаковым (без названия) описанием — сеть клонов. Возвращает, сколько помечено."""
    groups: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        key = keys.get(row[T.LINK], "")
        if key:
            groups.setdefault(key, []).append(row)
    marked = 0
    for same in groups.values():
        if len(same) < min_size:
            continue
        for row in same:
            row[T.NOTES] = "; ".join(filter(None, [row[T.NOTES], f"шаблонная сеть: {len(same)} одинаковых"]))
            if row[T.TACTIC] not in (A.MONITOR, A.BOTH, A.DEAD):
                row[T.TACTIC] = A.TEMPLATE
                marked += 1
    return marked


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
    skipped_channels: set[str] = set()
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
            if chat.kind == "channel":
                skipped_channels.add(chat.username.lower())  # каналы не нужны: объявление туда не дать
                continue
            candidates.setdefault(chat.username.lower(), (chat, query))
    out.found = len(candidates)
    out.skipped_channels = len(skipped_channels)

    todo = []
    for chat, query in candidates.values():
        if is_fresh(existing.find(chat.link), today):
            out.skipped_fresh += 1
        else:
            todo.append((chat, query))
    todo.sort(key=lambda p: p[0].members or 0, reverse=True)
    if len(todo) > max_chats:
        out.warnings.append(
            f"площадок много: проверены {max_chats} из {len(todo)}, остальные — в следующий раз"
        )
        todo = todo[:max_chats]

    # Треть дневного лимита ИИ оставляем мониторингу заявок.
    reserve = llm.daily_limit // 3
    ai_geo_checks = 0
    th = thresholds(cfg)
    keys: dict[str, str] = {}
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
        if info.kind == "channel":
            out.skipped_channels += 1
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
        analysis = None
        if not geo:
            analysis = await analyze(llm, info, reserve)  # сначала дёшево: воронежский ли вообще
            if not (analysis and analysis.voronezh):
                out.llm_used = out.llm_used or analysis is not None
                continue
        act = await _activity(userbot, info, cfg, sleep)
        if act is not None and act.msgs_7d < cfg.dead_week:
            pass  # мёртвый чат: ИИ не тратим, правила рекламы там неважны
        else:
            analysis = await analyze(llm, info, reserve, act)
            out.llm_used = out.llm_used or analysis is not None
        row = build_row(info, analysis, query, today.isoformat(), act, th)
        keys[row[T.LINK]] = A.template_keys(info.title, info.about)
        if row[T.TYPE] in cfg.skip_types or has_word(f"{info.title} {info.about}", cfg.skip_words):
            out.skipped_offtopic += 1
            continue
        out.rows.append(row)
    out.templates = mark_templates(out.rows, keys, th)
    if out.rows and not out.llm_used:
        out.warnings.append("ИИ недоступен — «Правила рекламы» и «Контакт админа» не заполнены")
    return out


def _stats(r: dict[str, str]) -> str:
    """«2 400 уч. · 340 сообщ./нед · 57 авт.» — по тому, что известно."""
    parts = [f"{int(r[T.MEMBERS]):,}".replace(",", " ") + " уч." if r[T.MEMBERS] else "? уч."]
    if r.get(T.MSGS_7D):
        parts.append(f"{r[T.MSGS_7D]} сообщ./нед · {r[T.AUTHORS_7D]} авт.")
    return " · ".join(parts)


def summary_text(out: ScoutOutcome, added: int, updated: int, new_links: set[str], extra: list[str]) -> str:
    """Итог владельцу: все новые площадки, сгруппированные по подходящей тактике."""
    lines = [f"🔎 <b>Разведка</b>: найдено {added} новых, {updated} обновлено"]
    skipped = ""
    if out.skipped_offtopic:
        skipped += f" · барахолки и т. п.: {out.skipped_offtopic}"
    if out.skipped_discussion:
        skipped += f" · чаты каналов: {out.skipped_discussion}"
    if out.skipped_channels:
        skipped += f" · каналы: {out.skipped_channels}"
    lines.append(
        f"Запросов: {out.queries_done} · площадок в поиске: {out.found} · свежих пропущено: {out.skipped_fresh}"
        + (f"\nНе подошли{skipped}" if skipped else "")
    )
    fresh = [r for r in out.rows if r[T.LINK] in new_links]
    # Мёртвые и шаблонные клоны в таблице остаются (чтобы не проверять их заново), но в список не идут.
    hidden = [r for r in fresh if r.get(T.TACTIC) in HIDDEN_TACTICS]
    if hidden:
        dead = sum(r.get(T.TACTIC) == A.DEAD for r in hidden)
        lines.append(f"Скрыто мёртвых: {dead} · шаблонных клонов: {len(hidden) - dead}")
    new_rows = sorted(
        (r for r in fresh if r.get(T.TACTIC) not in HIDDEN_TACTICS),
        key=lambda r: (
            TACTIC_ORDER.get(r.get(T.TACTIC, ""), 3),
            -int(r.get(T.MSGS_7D) or 0),
            TYPE_PRIORITY.get(r[T.TYPE], 5),
            -int(r[T.MEMBERS] or 0),
        ),
    )
    current = None
    for i, r in enumerate(new_rows, start=1):
        tactic = r.get(T.TACTIC, "")
        if tactic != current:
            current = tactic
            lines.append("")
            lines.append(f"<b>{escape(TACTIC_TITLE.get(tactic, tactic))}</b>")
        name = f'<a href="{escape(r[T.LINK])}">{escape(r[T.NAME])}</a>'
        details = [escape(r[T.TYPE]), _stats(r)]
        if r[T.RULES]:
            details.append(escape(r[T.RULES]))
        if r[T.NOTES]:
            details.append(escape(r[T.NOTES]))
        lines.append(f"{i}. {name} — " + " · ".join(details))
    for w in [*out.warnings, *extra]:
        lines.append(f"⚠️ {escape(w)}")
    return "\n".join(lines)
