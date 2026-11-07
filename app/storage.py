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
]


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
