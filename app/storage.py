"""SQLite: миграции, индекс постов, статусы, кэш file_id, ошибки, состояние синхронизации."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import aiosqlite

from app.content import Post, QueueItem, Section

MIGRATIONS: list[str] = [
    # 1 — базовая схема v1
    """
    CREATE TABLE posts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        repo_id TEXT NOT NULL,
        path TEXT NOT NULL,
        account TEXT NOT NULL,
        title TEXT NOT NULL,
        date TEXT,
        caption TEXT NOT NULL,
        sections TEXT NOT NULL,
        media TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        indexed_at TEXT NOT NULL,
        UNIQUE (repo_id, path)
    );
    CREATE INDEX posts_account ON posts(account);

    CREATE TABLE post_state (
        repo_id TEXT NOT NULL,
        path TEXT NOT NULL,
        status TEXT,
        published_at TEXT,
        postponed_at TEXT,
        PRIMARY KEY (repo_id, path)
    );

    CREATE TABLE media_cache (
        repo_id TEXT NOT NULL,
        path TEXT NOT NULL,
        sha256 TEXT NOT NULL,
        kind TEXT NOT NULL,
        file_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (repo_id, path, sha256)
    );

    CREATE TABLE post_errors (
        repo_id TEXT NOT NULL,
        path TEXT NOT NULL,
        reason TEXT NOT NULL,
        detected_at TEXT NOT NULL,
        PRIMARY KEY (repo_id, path)
    );

    CREATE TABLE repo_sync (
        repo_id TEXT PRIMARY KEY,
        last_commit TEXT,
        last_sync_at TEXT,
        last_ok_at TEXT,
        last_error TEXT
    );
    """,
    # 2 — видео раньше отправлялись без width/height; сбрасываем их file_id, чтобы перезалить с размерами
    """
    DELETE FROM media_cache WHERE kind = 'video';
    """,
    # 3 — v2, заявки. Без текстов и авторов: только хэш для дублей, счётчики и очередь вступлений.
    """
    CREATE TABLE lead_seen (
        hash TEXT PRIMARY KEY,
        seen_at TEXT NOT NULL
    );

    CREATE TABLE lead_stats (
        chat_id INTEGER NOT NULL,
        day TEXT NOT NULL,
        leads INTEGER NOT NULL DEFAULT 0,
        up INTEGER NOT NULL DEFAULT 0,
        down INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (chat_id, day)
    );

    CREATE TABLE join_queue (
        link TEXT PRIMARY KEY,
        status TEXT NOT NULL DEFAULT 'pending',
        added_at TEXT NOT NULL,
        attempted_at TEXT,
        note TEXT NOT NULL DEFAULT ''
    );

    CREATE TABLE kv (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    """,
]

LEAD_SEEN_DAYS = 7


def now_iso() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


class Storage:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.db: aiosqlite.Connection | None = None

    async def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = await aiosqlite.connect(self.path)
        self.db.row_factory = aiosqlite.Row
        await self.db.execute("PRAGMA journal_mode=WAL")
        await self.db.execute("PRAGMA foreign_keys=ON")
        await self.migrate()

    async def close(self) -> None:
        if self.db is not None:
            await self.db.close()
            self.db = None

    @property
    def conn(self) -> aiosqlite.Connection:
        assert self.db is not None, "Storage.open() не вызван"
        return self.db

    async def migrate(self) -> int:
        cur = await self.conn.execute("PRAGMA user_version")
        (version,) = await cur.fetchone()
        for i, sql in enumerate(MIGRATIONS[version:], start=version + 1):
            await self.conn.executescript(sql)
            await self.conn.execute(f"PRAGMA user_version = {i}")
            await self.conn.commit()
        return len(MIGRATIONS)

    # ---------- индекс ----------

    async def replace_repo_index(
        self, repo_id: str, posts: list[Post], errors: list[tuple[str, str]]
    ) -> tuple[int, int]:
        """Пересобирает индекс репозитория. Возвращает (добавлено новых, удалено)."""
        db = self.conn
        ts = now_iso()
        cur = await db.execute("SELECT path FROM posts WHERE repo_id = ?", (repo_id,))
        existing = {r["path"] for r in await cur.fetchall()}
        current = {p.path for p in posts}
        for p in posts:
            await db.execute(
                """
                INSERT INTO posts (repo_id, path, account, title, date, caption, sections, media,
                                   content_hash, indexed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (repo_id, path) DO UPDATE SET
                    account = excluded.account, title = excluded.title, date = excluded.date,
                    caption = excluded.caption, sections = excluded.sections, media = excluded.media,
                    content_hash = excluded.content_hash, indexed_at = excluded.indexed_at
                """,
                (
                    repo_id,
                    p.path,
                    p.account,
                    p.title,
                    p.date.isoformat() if p.date else None,
                    p.caption,
                    json.dumps([[s.title, s.body] for s in p.sections], ensure_ascii=False),
                    json.dumps(p.media, ensure_ascii=False),
                    p.content_hash,
                    ts,
                ),
            )
        removed = existing - current
        for path in removed:
            await db.execute("DELETE FROM posts WHERE repo_id = ? AND path = ?", (repo_id, path))
        await db.execute("DELETE FROM post_errors WHERE repo_id = ?", (repo_id,))
        await db.executemany(
            "INSERT OR REPLACE INTO post_errors (repo_id, path, reason, detected_at) VALUES (?, ?, ?, ?)",
            [(repo_id, path, reason, ts) for path, reason in errors],
        )
        await db.commit()
        return len(current - existing), len(removed)

    async def drop_repos_except(self, repo_ids: set[str]) -> None:
        """Убирает из индекса репозитории, которых больше нет в конфиге (статусы сохраняются)."""
        q = ",".join("?" * len(repo_ids)) or "''"
        await self.conn.execute(f"DELETE FROM posts WHERE repo_id NOT IN ({q})", tuple(repo_ids))
        await self.conn.execute(f"DELETE FROM post_errors WHERE repo_id NOT IN ({q})", tuple(repo_ids))
        await self.conn.commit()

    # ---------- очередь и посты ----------

    async def queue(self, account: str) -> list[QueueItem]:
        cur = await self.conn.execute(
            """
            SELECT p.id, p.path, p.title, p.date, p.media, s.postponed_at
            FROM posts p LEFT JOIN post_state s ON s.repo_id = p.repo_id AND s.path = p.path
            WHERE p.account = ? AND (s.status IS NULL OR s.status != 'published')
            """,
            (account,),
        )
        return [
            QueueItem(
                post_id=r["id"],
                path=r["path"],
                title=r["title"],
                date=dt.date.fromisoformat(r["date"]) if r["date"] else None,
                media=json.loads(r["media"]),
                postponed_at=r["postponed_at"],
            )
            for r in await cur.fetchall()
        ]

    async def get_post(self, post_id: int) -> dict | None:
        cur = await self.conn.execute(
            """
            SELECT p.*, s.status, s.published_at, s.postponed_at
            FROM posts p LEFT JOIN post_state s ON s.repo_id = p.repo_id AND s.path = p.path
            WHERE p.id = ?
            """,
            (post_id,),
        )
        r = await cur.fetchone()
        if r is None:
            return None
        d = dict(r)
        d["sections"] = [Section(t, b) for t, b in json.loads(d["sections"])]
        d["media"] = json.loads(d["media"])
        d["date"] = dt.date.fromisoformat(d["date"]) if d["date"] else None
        return d

    async def _upsert_state(self, repo_id: str, path: str, **fields: object) -> None:
        cols = ", ".join(fields)
        sets = ", ".join(f"{k} = excluded.{k}" for k in fields)
        await self.conn.execute(
            f"INSERT INTO post_state (repo_id, path, {cols}) VALUES (?, ?, {','.join('?' * len(fields))}) "
            f"ON CONFLICT (repo_id, path) DO UPDATE SET {sets}",
            (repo_id, path, *fields.values()),
        )
        await self.conn.commit()

    async def mark_published(self, repo_id: str, path: str) -> None:
        await self._upsert_state(repo_id, path, status="published", published_at=now_iso())

    async def postpone(self, repo_id: str, path: str) -> None:
        await self._upsert_state(repo_id, path, postponed_at=now_iso())

    async def unpublish(self, repo_id: str, path: str) -> None:
        await self._upsert_state(repo_id, path, status=None, published_at=None)

    async def published(self, limit: int = 20) -> list[dict]:
        cur = await self.conn.execute(
            """
            SELECT p.id, p.title, p.account, s.published_at
            FROM post_state s JOIN posts p ON p.repo_id = s.repo_id AND p.path = s.path
            WHERE s.status = 'published'
            ORDER BY s.published_at DESC LIMIT ?
            """,
            (limit,),
        )
        return [dict(r) for r in await cur.fetchall()]

    # ---------- ошибки ----------

    async def errors(self) -> list[dict]:
        cur = await self.conn.execute(
            "SELECT repo_id, path, reason, detected_at FROM post_errors ORDER BY repo_id, path"
        )
        return [dict(r) for r in await cur.fetchall()]

    async def error_count(self) -> int:
        cur = await self.conn.execute("SELECT COUNT(*) FROM post_errors")
        (n,) = await cur.fetchone()
        return n

    # ---------- кэш file_id ----------

    async def get_file_id(self, repo_id: str, path: str, sha256: str) -> str | None:
        cur = await self.conn.execute(
            "SELECT file_id FROM media_cache WHERE repo_id = ? AND path = ? AND sha256 = ?",
            (repo_id, path, sha256),
        )
        r = await cur.fetchone()
        return r["file_id"] if r else None

    async def put_file_id(self, repo_id: str, path: str, sha256: str, kind: str, file_id: str) -> None:
        await self.conn.execute(
            "INSERT OR REPLACE INTO media_cache (repo_id, path, sha256, kind, file_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (repo_id, path, sha256, kind, file_id, now_iso()),
        )
        await self.conn.commit()

    # ---------- синхронизация ----------

    async def record_sync(self, repo_id: str, commit: str | None, error: str | None) -> None:
        ts = now_iso()
        await self.conn.execute(
            """
            INSERT INTO repo_sync (repo_id, last_commit, last_sync_at, last_ok_at, last_error)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (repo_id) DO UPDATE SET
                last_commit = COALESCE(excluded.last_commit, repo_sync.last_commit),
                last_sync_at = excluded.last_sync_at,
                last_ok_at = COALESCE(excluded.last_ok_at, repo_sync.last_ok_at),
                last_error = excluded.last_error
            """,
            (repo_id, commit, ts, ts if error is None else None, error),
        )
        await self.conn.commit()

    async def sync_status(self) -> list[dict]:
        cur = await self.conn.execute("SELECT * FROM repo_sync ORDER BY repo_id")
        return [dict(r) for r in await cur.fetchall()]

    # ---------- v2: ключ-значение (счётчик ИИ, время разведки) ----------

    async def kv_get(self, key: str, default: str = "") -> str:
        cur = await self.conn.execute("SELECT value FROM kv WHERE key = ?", (key,))
        r = await cur.fetchone()
        return r["value"] if r else default

    async def kv_set(self, key: str, value: str) -> None:
        await self.conn.execute(
            "INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await self.conn.commit()

    async def kv_incr(self, key: str) -> int:
        await self.conn.execute(
            "INSERT INTO kv (key, value) VALUES (?, '1') "
            "ON CONFLICT (key) DO UPDATE SET value = CAST(CAST(kv.value AS INTEGER) + 1 AS TEXT)",
            (key,),
        )
        await self.conn.commit()
        return int(await self.kv_get(key, "0"))

    async def kv_delete_prefix(self, prefix: str, keep: str) -> None:
        await self.conn.execute(
            "DELETE FROM kv WHERE substr(key, 1, ?) = ? AND key != ?", (len(prefix), prefix, keep)
        )
        await self.conn.commit()

    # ---------- v2: дубли заявок (только хэш текста, 7 дней) ----------

    async def lead_is_new(self, text_hash: str, now: dt.datetime | None = None) -> bool:
        """True, если такого хэша не было за последние 7 дней; хэш запоминается."""
        now = now or dt.datetime.now(dt.UTC)
        border = (now - dt.timedelta(days=LEAD_SEEN_DAYS)).isoformat(timespec="seconds")
        await self.conn.execute("DELETE FROM lead_seen WHERE seen_at < ?", (border,))
        cur = await self.conn.execute(
            "INSERT OR IGNORE INTO lead_seen (hash, seen_at) VALUES (?, ?)",
            (text_hash, now.isoformat(timespec="seconds")),
        )
        await self.conn.commit()
        return cur.rowcount == 1

    # ---------- v2: счётчики заявок по чатам ----------

    async def lead_count(self, chat_id: int, day: str, field: str = "leads") -> None:
        if field not in ("leads", "up", "down"):
            raise ValueError(field)
        await self.conn.execute(
            f"INSERT INTO lead_stats (chat_id, day, {field}) VALUES (?, ?, 1) "
            f"ON CONFLICT (chat_id, day) DO UPDATE SET {field} = {field} + 1",
            (chat_id, day),
        )
        await self.conn.commit()

    async def lead_totals(self, since_day: str) -> dict[str, int]:
        cur = await self.conn.execute(
            "SELECT COALESCE(SUM(leads), 0) AS leads, COALESCE(SUM(up), 0) AS up, "
            "COALESCE(SUM(down), 0) AS down FROM lead_stats WHERE day >= ?",
            (since_day,),
        )
        return dict(await cur.fetchone())

    async def lead_by_chat(self, since_day: str) -> dict[int, dict[str, int]]:
        cur = await self.conn.execute(
            "SELECT chat_id, SUM(leads) AS leads, SUM(up) AS up, SUM(down) AS down "
            "FROM lead_stats WHERE day >= ? GROUP BY chat_id",
            (since_day,),
        )
        return {
            r["chat_id"]: {"leads": r["leads"], "up": r["up"], "down": r["down"]} for r in await cur.fetchall()
        }

    # ---------- v2: очередь вступлений ----------

    async def join_enqueue(self, link: str, reset: bool = False) -> None:
        """Ставит чат в очередь. reset=True возвращает в очередь и после manual/failed."""
        if reset:
            await self.conn.execute(
                "INSERT INTO join_queue (link, added_at) VALUES (?, ?) ON CONFLICT (link) DO UPDATE SET "
                "status = CASE WHEN join_queue.status = 'joined' THEN 'joined' ELSE 'pending' END, note = ''",
                (link, now_iso()),
            )
        else:
            await self.conn.execute(
                "INSERT INTO join_queue (link, added_at) VALUES (?, ?) ON CONFLICT (link) DO UPDATE SET "
                "status = CASE WHEN join_queue.status = 'removed' THEN 'pending' ELSE join_queue.status END",
                (link, now_iso()),
            )
        await self.conn.commit()

    async def join_next(self) -> str | None:
        cur = await self.conn.execute(
            "SELECT link FROM join_queue WHERE status = 'pending' ORDER BY added_at, link LIMIT 1"
        )
        r = await cur.fetchone()
        return r["link"] if r else None

    async def join_attempt(self, link: str, now: dt.datetime) -> None:
        await self.conn.execute(
            "UPDATE join_queue SET attempted_at = ? WHERE link = ?",
            (now.isoformat(timespec="seconds"), link),
        )
        await self.conn.commit()

    async def join_set(self, link: str, status: str, note: str = "") -> None:
        await self.conn.execute(
            "INSERT INTO join_queue (link, status, added_at, note) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (link) DO UPDATE SET status = excluded.status, note = excluded.note",
            (link, status, now_iso(), note),
        )
        await self.conn.commit()

    async def join_attempts_since(self, since: dt.datetime) -> list[dt.datetime]:
        cur = await self.conn.execute(
            "SELECT attempted_at FROM join_queue WHERE attempted_at >= ? ORDER BY attempted_at",
            (since.isoformat(timespec="seconds"),),
        )
        return [dt.datetime.fromisoformat(r["attempted_at"]) for r in await cur.fetchall()]

    async def join_states(self) -> dict[str, dict[str, str]]:
        cur = await self.conn.execute("SELECT link, status, note FROM join_queue")
        return {r["link"]: {"status": r["status"], "note": r["note"]} for r in await cur.fetchall()}

    async def join_remove(self, link: str) -> None:
        """Убирает чат из очереди. Время попытки остаётся в учёте суточного лимита."""
        await self.conn.execute(
            "UPDATE join_queue SET status = 'removed', note = '' WHERE link = ?", (link,)
        )
        await self.conn.commit()

    async def join_prune(self, keep: set[str]) -> None:
        """Снимает с ожидания чаты, у которых в файле площадок убрали monitor."""
        cur = await self.conn.execute("SELECT link FROM join_queue WHERE status = 'pending'")
        for r in await cur.fetchall():
            if r["link"] not in keep:
                await self.join_remove(r["link"])
