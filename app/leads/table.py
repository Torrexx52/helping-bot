"""Файл площадок `chats/telegram.csv`: чтение, слияние по «Ссылка», запись (spec-leads §6)."""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from pathlib import Path

from app.leads.text import link_key

NAME, TYPE, MEMBERS, PLATFORM, LINK, STATUS, RULES, ADMIN, POSTED, NOTES = (
    "Название",
    "Тип",
    "Подписчики",
    "Площадка",
    "Ссылка",
    "Статус",
    "Правила рекламы",
    "Контакт админа",
    "Дата размещения",
    "Заметки",
)
TG_ID, KIND, CAN_WRITE, JOIN, MONITOR, JOINED, QUERY, FOUND_AT, CHECKED_AT = (
    "tg_id",
    "kind",
    "can_write",
    "join",
    "monitor",
    "joined",
    "query",
    "found_at",
    "checked_at",
)
TACTIC, MSGS_7D, AUTHORS_7D, LAST_MSG, ADS_SEEN = "tactic", "msgs_7d", "authors_7d", "last_msg", "ads_seen"
COLUMNS = [
    NAME, TYPE, MEMBERS, PLATFORM, LINK, STATUS, RULES, ADMIN, POSTED, NOTES,
    TG_ID, KIND, CAN_WRITE, JOIN, MONITOR, JOINED, QUERY, FOUND_AT, CHECKED_AT,
    TACTIC, MSGS_7D, AUTHORS_7D, LAST_MSG, ADS_SEEN,
]  # fmt: skip

# Ведёт владелец — бот не перезаписывает никогда.
MANUAL = (STATUS, POSTED, MONITOR)
# Ставятся один раз (при находке) или ведутся мониторингом, а не разведкой.
KEEP_EXISTING = (*MANUAL, JOINED, QUERY, FOUND_AT)
# Выводы ИИ: пустым значением не затираем то, что уже было записано.
KEEP_IF_EMPTY = (RULES, ADMIN, TYPE, MEMBERS, TG_ID, TACTIC, MSGS_7D, AUTHORS_7D, LAST_MSG, ADS_SEEN)

TYPES = (
    "ЖК-группа ТГ", "Районный чат ТГ", "Городской чат ТГ", "Тематический чат ТГ", "Доска услуг ТГ",
    "Барахолка ТГ", "Канал ТГ", "Другое ТГ",
)  # fmt: skip
STATUS_NEW = "Не трогали"
YES = "yes"


def is_yes(value: str | None) -> bool:
    return (value or "").strip().lower() in ("yes", "да", "1", "true", "y")


@dataclass
class Table:
    rows: list[dict[str, str]] = field(default_factory=list)
    extra_columns: list[str] = field(default_factory=list)  # колонки, добавленные руками

    @property
    def fieldnames(self) -> list[str]:
        return COLUMNS + self.extra_columns

    def find(self, link: str) -> dict[str, str] | None:
        key = link_key(link)
        return next((r for r in self.rows if link_key(r.get(LINK, "")) == key), None)

    def monitored(self) -> list[dict[str, str]]:
        return [r for r in self.rows if is_yes(r.get(MONITOR)) and r.get(LINK)]

    def merge(self, new_rows: list[dict[str, str]]) -> tuple[int, int]:
        """Сливает свежие строки разведки. Возвращает (добавлено, обновлено)."""
        added = updated = 0
        for new in new_rows:
            if not new.get(LINK):
                continue
            old = self.find(new[LINK])
            if old is None:
                self.rows.append({c: new.get(c, "") for c in self.fieldnames})
                added += 1
                continue
            for col in COLUMNS:
                if col in KEEP_EXISTING or col == LINK:
                    continue
                value = new.get(col, "")
                if not value and col in KEEP_IF_EMPTY:
                    continue
                if col == JOIN and old.get(JOIN) == "manual" and value == "open":
                    continue  # «manual» ставит очередь вступлений (капча и т. п.) — разведка этого не видит
                old[col] = value
            updated += 1
        return added, updated

    def overlay(self, local: Table) -> None:
        """Накладывает локальные (неотправленные) строки бота на версию из репозитория.

        Нужна, когда бот писал без связи с GitHub. «Статус» и «Дата размещения» остаются как в
        репозитории, остальное берётся из локальной копии.
        """
        self.extra_columns += [c for c in local.extra_columns if c not in self.extra_columns]
        for row in local.rows:
            if not row.get(LINK):
                continue
            mine = self.find(row[LINK])
            if mine is None:
                self.rows.append({c: row.get(c, "") for c in self.fieldnames})
                continue
            for col in self.fieldnames:
                if col == LINK or (col in (STATUS, POSTED) and mine.get(col)):
                    continue
                mine[col] = row.get(col, "")

    # ---------- файл ----------

    @classmethod
    def loads(cls, text: str) -> Table:
        reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
        names = [n for n in (reader.fieldnames or []) if n]
        extra = [n for n in names if n not in COLUMNS]
        rows = []
        for raw in reader:
            row = {c: (raw.get(c) or "").strip() for c in COLUMNS + extra}
            if any(row.values()):
                rows.append(row)
        return cls(rows, extra)

    @classmethod
    def load(cls, path: Path) -> Table:
        if not path.exists():
            return cls()
        return cls.loads(path.read_text(encoding="utf-8-sig"))

    def dumps(self) -> str:
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=self.fieldnames, lineterminator="\n", extrasaction="ignore")
        writer.writeheader()
        for row in self.rows:
            writer.writerow({c: row.get(c, "") for c in self.fieldnames})
        return buf.getvalue()

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        # UTF-8 с BOM — чтобы файл сразу правильно открывался в Excel.
        path.write_bytes(b"\xef\xbb\xbf" + self.dumps().encode("utf-8"))


README = """# helping-bot-data

Данные Helping Bot (v2). Репозиторий приватный: сюда пишет бот, отсюда Claude переносит площадки
в таблицу «База площадок для объявлений — Воронеж». Заявки (тексты, авторы) сюда не пишутся.

## chats/telegram.csv

UTF-8 с BOM, разделитель — запятая, одна строка — одна площадка, ключ строки — `Ссылка`.
Первые 10 колонок совпадают с листом «Площадки».

| Колонка | Что пишет бот |
| --- | --- |
| Название | название чата |
| Тип | `ЖК-группа ТГ` / `Районный чат ТГ` / `Городской чат ТГ` / `Тематический чат ТГ` / `Доска услуг ТГ` / `Барахолка ТГ` / `Канал ТГ` / `Другое ТГ` |
| Подписчики | число участников |
| Площадка | `Telegram` |
| Ссылка | `https://t.me/<username>` |
| Статус | `Не трогали` для новых; дальше ведёт владелец, бот не меняет |
| Правила рекламы | разрешена / запрещена / платно / через админа + одна фраза |
| Контакт админа | только если указан в описании или закрепе |
| Дата размещения | ведёт владелец, бот не меняет |
| Заметки | `писать нельзя`, `медленный режим 60 с`, `вход по заявке` и т. п. |
| tg_id | числовой id чата |
| kind | `group` / `channel` |
| can_write | `yes` / `no` — может ли писать обычный участник |
| join | `open` / `request` / `manual` |
| monitor | `yes` / пусто — следить ли за заявками (ставит владелец или `/watch`) |
| joined | `yes` / пусто — юзербот уже в чате |
| query | по какому запросу найдена |
| found_at, checked_at | даты, `ГГГГ-ММ-ДД` |
| tactic | подсказка бота: `мониторинг` (живой чат — ловить заявки), `объявление` (можно бесплатно разместить), `оба`, `мёртвый`, `шаблон` (клон из сети одинаковых чатов) |
| msgs_7d, authors_7d | сообщений людей и разных авторов за 7 дней (по последним ~100 сообщениям) |
| last_msg | сколько дней назад писал человек |
| ads_seen | сколько разных участников публикуют объявления услуг (из них перевозчики — в «Заметках») |

Руками можно менять `Статус`, `Дата размещения` и `monitor` — бот их не перезаписывает.
"""
