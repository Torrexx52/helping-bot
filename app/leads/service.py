"""Сервис заявок: связывает юзербота, ИИ, репозиторий данных и бота.

Запускается всегда, но юзербот подключается, только если заданы TG_API_ID/TG_API_HASH и есть
сессия. Без них команды отвечают подсказкой, а очередь постов (v1) работает как раньше.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
import random
import re
from html import escape
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, LinkPreviewOptions

from app.config import ConfigError, LeadsConfig, Settings, load_leads
from app.leads import table as T
from app.leads.datarepo import DataRepo, DataRepoError
from app.leads.monitor import Monitor, Watched, join_wait
from app.leads.scout import build_row, run_scout, summary_text
from app.leads.table import Table, is_yes
from app.leads.text import WordFilter, link_key, link_username, normalize_link
from app.llm import LLM
from app.storage import Storage
from app.userbot import LOGIN_COMMAND, FloodWait, JoinManual, Userbot, UserbotError

log = logging.getLogger(__name__)

SCOUT_MIN_INTERVAL = dt.timedelta(hours=20)  # «не чаще раза в сутки» с запасом на расписание
RECONCILE_EVERY = 600
JOIN_TICK = 30
JOIN_PAUSE = (120, 360)  # 2–6 минут между вступлениями
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


def parse_scout_time(value: str) -> tuple[int, int] | None:
    """`SCOUT_CRON`: `ЧЧ:ММ` или cron-строка `М Ч * * *` (раз в сутки). Пусто — выключено."""
    value = value.strip()
    if not value:
        return None
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", value) or None
    if m:
        hour, minute = int(m.group(1)), int(m.group(2))
    else:
        m = re.fullmatch(r"(\d{1,2})\s+(\d{1,2})(?:\s+\*){3}", value)
        if not m:
            raise ValueError("SCOUT_CRON: нужно «ЧЧ:ММ» или «М Ч * * *»")
        minute, hour = int(m.group(1)), int(m.group(2))
    if hour > 23 or minute > 59:
        raise ValueError("SCOUT_CRON: время вне диапазона")
    return hour, minute


def _tg_id(row: dict[str, str]) -> int | None:
    try:
        return int(row.get(T.TG_ID, ""))
    except ValueError:
        return None


class Leads:
    def __init__(self, settings: Settings, storage: Storage) -> None:
        self.settings = settings
        self.storage = storage
        self.tz = ZoneInfo(settings.TZ)
        self.cfg: LeadsConfig | None = None
        self.cfg_error = ""
        self.llm = LLM(
            storage,
            settings.LLM_BASE_URL,
            settings.LLM_API_KEY,
            settings.LLM_MODEL,
            settings.LLM_TIMEOUT,
            settings.LLM_DAILY_LIMIT,
            settings.TZ,
            fallback_model=settings.LLM_FALLBACK_MODEL,
            rpm=settings.LLM_RPM,
        )
        self.repo = DataRepo(
            settings.DATA_DIR / "leads-data",
            settings.LEADS_DATA_REPO,
            settings.leads_data_key,
            settings.LEADS_DATA_BRANCH,
        )
        self.monitor = Monitor(storage, self.llm, self._send, settings.TZ)
        self.userbot: Userbot | None = None
        self.userbot_note = "юзербот не запускался"
        self.bot: Bot | None = None
        self._tasks: list[asyncio.Task] = []
        self._scout_lock = asyncio.Lock()
        self._flood_until: dt.datetime | None = None
        self._pause = random.uniform(*JOIN_PAUSE)

    # ---------- запуск и остановка ----------

    def load_config(self) -> None:
        try:
            self.cfg = load_leads(self.settings.LEADS_FILE)
            self.cfg_error = ""
        except ConfigError as e:
            self.cfg, self.cfg_error = None, str(e)
            self.monitor.filter = None
            return
        self.monitor.filter = WordFilter(self.cfg.monitor.keywords, self.cfg.monitor.stop_words)

    async def start(self, bot: Bot) -> None:
        """Никогда не бросает исключений: проблемы v2 не должны мешать очереди постов."""
        self.bot = bot
        try:
            await self._start()
        except Exception:  # noqa: BLE001
            log.exception("Заявки: юзербот не запущен")
            self.userbot_note = "ошибка подключения, подробности в логах"

    async def _start(self) -> None:
        s = self.settings
        self.load_config()
        if not s.userbot_configured:
            self.userbot_note = "не заданы TG_API_ID и TG_API_HASH"
            log.info("Заявки: TG_API_ID/TG_API_HASH не заданы — юзербот выключен, работает только v1")
            return
        if self.cfg_error:
            log.warning("Заявки: %s", self.cfg_error)
        if not s.userbot_session.exists():
            self.userbot_note = "нет сессии — нужен первый вход"
            log.warning("Заявки: нет %s — выполните: %s", s.userbot_session, LOGIN_COMMAND)
            return
        userbot = Userbot(s)
        if not await userbot.start():
            self.userbot_note = "сессия не авторизована — нужен вход"
            log.warning("Заявки: сессия юзербота не авторизована — выполните: %s", LOGIN_COMMAND)
            return
        userbot.set_handler(self.monitor.handle)
        self.userbot, self.userbot_note = userbot, "подключён"
        self._tasks.append(asyncio.create_task(self._every(RECONCILE_EVERY, self.reconcile, first=0)))
        self._tasks.append(asyncio.create_task(self._every(JOIN_TICK, self._join_step, first=JOIN_TICK)))
        try:
            at = parse_scout_time(s.SCOUT_CRON)
        except ValueError as e:
            log.warning("%s — расписание разведки выключено", e)
            at = None
        if at:
            self._tasks.append(asyncio.create_task(self._scout_schedule(*at)))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        self._tasks.clear()
        if self.userbot is not None:
            with contextlib.suppress(Exception):
                await self.userbot.stop()
        await self.llm.aclose()

    async def _every(self, seconds: float, step, first: float) -> None:
        await asyncio.sleep(first)
        while True:
            try:
                await step()
            except Exception:  # noqa: BLE001 — фоновая задача не должна падать
                log.exception("Заявки: ошибка фоновой задачи %s", getattr(step, "__name__", step))
            await asyncio.sleep(seconds)

    async def _send(self, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
        if self.bot is None or not self.settings.OWNER_TELEGRAM_ID:
            return
        await self.bot.send_message(
            self.settings.OWNER_TELEGRAM_ID, text, reply_markup=markup, link_preview_options=NO_PREVIEW
        )

    def now(self) -> dt.datetime:
        return dt.datetime.now(dt.UTC)

    def day_ago(self, days: int) -> str:
        return (dt.datetime.now(self.tz).date() - dt.timedelta(days=days - 1)).isoformat()

    def blocker(self) -> str:
        """Почему v2 сейчас не работает (пусто — всё готово)."""
        if self.userbot is None:
            text = f"Юзербот не подключён: {self.userbot_note}."
            if self.settings.userbot_configured:
                text += f"\nПервый вход на сервере:\n<code>{escape(LOGIN_COMMAND)} &amp;&amp; docker compose up -d --force-recreate bot</code>"
            else:
                text += "\nДобавь TG_API_ID и TG_API_HASH в .env (см. .env.example)."
            return text
        if self.cfg is None:
            return f"Не прочитан config/leads.yml:\n<pre>{escape(self.cfg_error)[:1500]}</pre>"
        return ""

    # ---------- сверка с файлом площадок ----------

    async def reconcile(self) -> None:
        """Перечитывает файл площадок: кого слушать, кого поставить в очередь вступления."""
        if self.userbot is None:
            return
        table, _ = await self.repo.read()
        rows = table.monitored()
        try:
            joined_ids = await self.userbot.joined_ids()
        except UserbotError as e:
            log.warning("Заявки: список чатов юзербота не получен: %s", e)
            joined_ids = None

        # Чаты, в которые владелец вступил под аккаунтом юзербота вручную (заявка, капча).
        newly = [
            r[T.LINK]
            for r in rows
            if joined_ids is not None and not is_yes(r.get(T.JOINED)) and _tg_id(r) in joined_ids
        ]
        if newly:
            keys = {link_key(x) for x in newly}

            def mark(t: Table) -> None:
                for r in t.rows:
                    if link_key(r.get(T.LINK, "")) in keys:
                        r[T.JOINED] = T.YES

            with contextlib.suppress(DataRepoError):
                await self.repo.edit(mark, f"watch: в чате — {len(newly)}")
            for link in newly:
                await self.storage.join_set(link, "joined")

        watched: dict[int, Watched] = {}
        pending: set[str] = set()
        for r in rows:
            link = normalize_link(r[T.LINK])
            if link is None:
                continue
            tg_id = _tg_id(r)
            if tg_id is not None:
                watched[tg_id] = Watched(r.get(T.NAME) or link, link)
            in_chat = is_yes(r.get(T.JOINED)) or r[T.LINK] in newly
            if not in_chat and r.get(T.JOIN) != "manual":
                pending.add(link)
                await self.storage.join_enqueue(link)
        await self.storage.join_prune(pending)
        self.monitor.watched = watched

    # ---------- очередь вступлений ----------

    async def _join_step(self) -> None:
        if self.userbot is None:
            return
        link = await self.storage.join_next()
        if link is None:
            return
        now = self.now()
        attempts = await self.storage.join_attempts_since(now - dt.timedelta(days=1))
        if join_wait(now, attempts, self.settings.JOIN_PER_DAY, self._pause, self._flood_until) > 0:
            return
        await self.storage.join_attempt(link, now)
        self._pause = random.uniform(*JOIN_PAUSE)
        try:
            chat = await self.userbot.join(link_username(link))
        except FloodWait as e:  # ждём, сколько сказал Telegram, и продолжаем
            self._flood_until = now + dt.timedelta(seconds=e.seconds + 5)
            log.warning("Вступление: FloodWait %s с", e.seconds)
            return
        except JoinManual as e:
            reason = str(e)
            await self.storage.join_set(link, "manual", reason)
            await self._update_row(link, f"watch: {link} — вручную", {T.JOIN: "manual"}, note=reason)
            await self._send(
                f"✋ Не вступил в {escape(link)}: {escape(reason)}.\n"
                "Вступи вручную под аккаунтом юзербота — мониторинг подхватит сам."
            )
            return
        except UserbotError as e:
            await self.storage.join_set(link, "failed", str(e))
            log.warning("Вступление в %s не удалось: %s", link, e)
            await self._send(f"⚠️ Не вступил в {escape(link)}: {escape(str(e))}. Повторить: /watch {escape(link)}")
            return
        await self.storage.join_set(link, "joined")
        await self._update_row(
            link, f"watch: вступил в {link}", {T.JOINED: T.YES, T.TG_ID: str(chat.tg_id)}, title=chat.title
        )
        self.monitor.watched[chat.tg_id] = Watched(chat.title or link, link)
        log.info("Юзербот вступил в %s", link)
        await self._send(f"✅ Юзербот вступил в {escape(chat.title or link)} — слежу за заявками.")

    async def _update_row(
        self, link: str, message: str, values: dict[str, str], note: str = "", title: str = ""
    ) -> str | None:
        def mutate(t: Table) -> None:
            row = t.find(link)
            if row is None:
                return
            row.update(values)
            if title and not row.get(T.NAME):
                row[T.NAME] = title
            if note and note not in row.get(T.NOTES, ""):
                row[T.NOTES] = "; ".join(p for p in (row.get(T.NOTES, ""), note) if p)

        try:
            _, warning = await self.repo.edit(mutate, message)
        except DataRepoError as e:
            log.warning("Файл площадок не обновлён: %s", e)
            return str(e)
        return warning

    # ---------- разведка ----------

    async def scout_blocker(self) -> str:
        if text := self.blocker():
            return text
        if self._scout_lock.locked():
            return "Разведка уже идёт — пришлю итог, когда закончу."
        last = await self.storage.kv_get("scout_last")
        if last:
            next_at = dt.datetime.fromisoformat(last) + SCOUT_MIN_INTERVAL
            if next_at > self.now():
                when = next_at.astimezone(self.tz).strftime("%d.%m %H:%M")
                return f"Разведка — не чаще раза в сутки (так безопаснее для аккаунта). Следующая — после {when}."
        return ""

    async def run_scout(self) -> None:
        """Полный запуск разведки; итог или ошибка уходят владельцу сообщением."""
        if self.userbot is None or self.cfg is None or self._scout_lock.locked():
            return
        async with self._scout_lock:
            await self.storage.kv_set("scout_last", self.now().isoformat(timespec="seconds"))
            try:
                text = await self._scout()
            except Exception as e:  # noqa: BLE001
                log.exception("Разведка не удалась")
                text = f"❌ Разведка не удалась: {escape(type(e).__name__)}. Подробности в логах."
            await self._send(text)

    async def _scout(self) -> str:
        s = self.settings
        existing, _ = await self.repo.read()
        offset = int(await self.storage.kv_get("scout_offset", "0"))
        out = await run_scout(
            self.userbot,
            self.llm,
            self.cfg.scout,
            existing,
            today=dt.datetime.now(self.tz).date(),
            queries_per_run=s.SCOUT_QUERIES_PER_RUN,
            max_chats=s.SCOUT_MAX_CHATS,
            offset=offset,
        )
        await self.storage.kv_set("scout_offset", str((offset + out.queries_done) % len(self.cfg.scout.queries)))
        new_links: set[str] = set()

        def merge(t: Table) -> tuple[int, int]:
            new_links.update(r[T.LINK] for r in out.rows if t.find(r[T.LINK]) is None)
            return t.merge(out.rows)

        (added, updated), warning = await self.repo.edit(
            merge, lambda r: f"scout: +{r[0]} новых, {r[1]} обновлено"
        )
        log.info("Разведка: +%d новых, %d обновлено, запросов %d", added, updated, out.queries_done)
        return summary_text(out, added, updated, new_links, [warning] if warning else [])

    async def _scout_schedule(self, hour: int, minute: int) -> None:
        while True:
            now = dt.datetime.now(self.tz)
            target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if target <= now:
                target += dt.timedelta(days=1)
            await asyncio.sleep((target - now).total_seconds())
            try:
                if not await self.scout_blocker():
                    await self.run_scout()
            except Exception:  # noqa: BLE001
                log.exception("Разведка по расписанию не удалась")

    # ---------- команды ----------

    async def watch(self, arg: str) -> str:
        link = normalize_link(arg)
        if link is None:
            return (
                "Формат: <code>/watch https://t.me/имя_чата</code> (или @имя_чата).\n"
                "Приватные ссылки-приглашения не поддерживаются: вступи в такой чат вручную под аккаунтом юзербота."
            )
        info, note = None, ""
        if self.userbot is not None:
            try:
                info = await self.userbot.chat_info(link_username(link))
            except FloodWait as e:
                note = f"сведения о чате не получены (Telegram просит подождать {e.seconds} с)"
            except UserbotError as e:
                return f"❌ {escape(link)}: {escape(str(e))}."
        today = dt.datetime.now(self.tz).date().isoformat()

        def mutate(t: Table) -> dict[str, str]:
            row = t.find(link)
            if row is None:
                if info is not None:
                    row = build_row(info, None, "/watch", today)
                else:
                    row = dict.fromkeys(t.fieldnames, "")
                    row.update({T.PLATFORM: "Telegram", T.LINK: link, T.STATUS: T.STATUS_NEW})
                    row.update({T.QUERY: "/watch", T.FOUND_AT: today})
                t.rows.append(row)
            row[T.MONITOR] = T.YES
            return dict(row)

        try:
            row, warning = await self.repo.edit(mutate, f"watch: {link}")
        except DataRepoError as e:
            return f"❌ Файл площадок не обновлён: {escape(str(e))}"
        name = escape(row.get(T.NAME) or link)
        lines = []
        if is_yes(row.get(T.JOINED)):
            lines.append(f"👀 {name}: уже в чате, слежу за заявками.")
            if (tg_id := _tg_id(row)) is not None:
                self.monitor.watched[tg_id] = Watched(row.get(T.NAME) or link, link)
        elif row.get(T.JOIN) in ("request", "manual"):
            await self.storage.join_set(link, "manual", row.get(T.NOTES, ""))
            lines.append(
                f"✋ {name}: добавлен, но вступить сам не могу ({escape(row.get(T.NOTES) or 'вход вручную')}).\n"
                "Вступи вручную под аккаунтом юзербота — мониторинг подхватит сам."
            )
        else:
            await self.storage.join_enqueue(link, reset=True)
            lines.append(f"👀 {name}: добавлен, вступлю по очереди (лимит {self.settings.JOIN_PER_DAY} в сутки).")
        if self.userbot is None:
            lines.append("⚠️ Юзербот пока не подключён — вступит после первого входа.")
        lines += [f"⚠️ {escape(w)}" for w in (note, warning) if w]
        return "\n".join(lines)

    async def unwatch(self, arg: str) -> str:
        link = normalize_link(arg)
        if link is None:
            return "Формат: <code>/unwatch https://t.me/имя_чата</code>"
        state: dict[str, str] = {}

        def mutate(t: Table) -> None:
            row = t.find(link)
            if row is None:
                return
            state.update(row)
            row[T.MONITOR] = ""
            row[T.JOINED] = ""

        try:
            _, warning = await self.repo.edit(mutate, f"unwatch: {link}")
        except DataRepoError as e:
            return f"❌ Файл площадок не обновлён: {escape(str(e))}"
        if not state:
            return f"{escape(link)} нет в файле площадок."
        await self.storage.join_remove(link)
        for tg_id, w in list(self.monitor.watched.items()):
            if link_key(w.link) == link_key(link):
                del self.monitor.watched[tg_id]
        lines = [f"🚫 {escape(state.get(T.NAME) or link)}: больше не слежу."]
        if self.userbot is not None and is_yes(state.get(T.JOINED)):
            try:
                await self.userbot.leave(link_username(link))
                lines.append("Юзербот вышел из чата.")
            except UserbotError as e:
                lines.append(f"⚠️ Выйти из чата не получилось: {escape(str(e))}")
        if warning:
            lines.append(f"⚠️ {escape(warning)}")
        return "\n".join(lines)

    async def watching_text(self) -> str:
        table, warning = await self.repo.read()
        rows = table.monitored()
        if not rows:
            return "Пока ни за одним чатом не слежу.\nДобавить: <code>/watch https://t.me/имя_чата</code>"
        stats = await self.storage.lead_by_chat(self.day_ago(7))
        states = await self.storage.join_states()
        joined = sum(1 for r in rows if is_yes(r.get(T.JOINED)))
        lines = [f"<b>Отслеживаю чатов: {len(rows)}</b> · вступил в {joined}", ""]
        for r in rows:
            link = normalize_link(r[T.LINK]) or r[T.LINK]
            st = states.get(link, {})
            if is_yes(r.get(T.JOINED)):
                mark, tail = "✅", ""
            elif r.get(T.JOIN) in ("request", "manual") or st.get("status") == "manual":
                mark, tail = "✋", " · вступи вручную"
            elif st.get("status") == "failed":
                mark, tail = "⚠️", f" · ошибка: {escape(st.get('note', ''))}"
            else:
                mark, tail = "⏳", " · в очереди"
            leads = stats.get(_tg_id(r) or 0, {}).get("leads", 0)
            lines.append(
                f'{mark} <a href="{escape(link)}">{escape(r.get(T.NAME) or link)}</a> — заявок за 7 дн.: {leads}{tail}'
            )
        if self.userbot is None:
            lines.append("\n⚠️ Юзербот не подключён — в чаты не вступаю и сообщения не читаю.")
        if warning:
            lines.append(f"\n⚠️ {escape(warning)}")
        return "\n".join(lines)

    async def leadstats_text(self) -> str:
        lines = ["<b>Заявки</b>"]
        for days in (7, 30):
            t = await self.storage.lead_totals(self.day_ago(days))
            votes = t["up"] + t["down"]
            precision = f"{round(100 * t['up'] / votes)}%" if votes else "—"
            lines.append(f"За {days} дн.: {t['leads']} · 👍 {t['up']} · 👎 {t['down']} · точность {precision}")
        by_chat = await self.storage.lead_by_chat(self.day_ago(30))
        if by_chat:
            table, _ = await self.repo.read(pull=False)
            names = {_tg_id(r): r.get(T.NAME) or r.get(T.LINK, "") for r in table.rows}
            lines += ["", "<b>По чатам за 30 дн.</b>"]
            top = sorted(by_chat.items(), key=lambda kv: kv[1]["leads"], reverse=True)[:15]
            for chat_id, st in top:
                lines.append(
                    f"• {escape(names.get(chat_id) or str(chat_id))}: {st['leads']} · 👍 {st['up']} · 👎 {st['down']}"
                )
        lines.append(f"\nИИ сегодня: {await self.llm.used_today()} из {self.llm.daily_limit} вызовов")
        return "\n".join(lines)

    async def panel_text(self) -> str:
        """Экран кнопки «🚚 Заявки»."""
        lines = ["🚚 <b>Заявки</b>", f"Юзербот: {escape(self.userbot_note)}"]
        if self.llm.configured:
            lines.append(f"ИИ: {escape(self.llm.model)}, сегодня {await self.llm.used_today()} из {self.llm.daily_limit}")
        else:
            lines.append("ИИ: не настроен — заявки приходят «без проверки»")
        if self.cfg_error:
            lines.append("⚠️ config/leads.yml не прочитан")
        table, _ = await self.repo.read(pull=False)
        rows = table.monitored()
        t = await self.storage.lead_totals(self.day_ago(7))
        lines.append(
            f"Площадок в файле: {len(table.rows)} · слежу за {len(rows)} "
            f"(вступил в {sum(1 for r in rows if is_yes(r.get(T.JOINED)))})"
        )
        lines.append(f"Заявок за 7 дн.: {t['leads']} · 👍 {t['up']} · 👎 {t['down']}")
        lines += [
            "",
            "/scout — разведка площадок",
            "/watch &lt;ссылка&gt; · /unwatch &lt;ссылка&gt; — следить или перестать",
            "/watching — за кем слежу",
            "/leadstats — заявки и точность",
        ]
        return "\n".join(lines)
