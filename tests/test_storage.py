from __future__ import annotations

import datetime as dt

from app.content import Post, Section, sort_queue
from app.storage import MIGRATIONS, Storage


def mk(path, date="2026-11-10", title=None):
    return Post(
        path=path,
        title=title or path,
        date=dt.date.fromisoformat(date) if date else None,
        account="tiktok-esoteric",
        caption="cap",
        sections=[Section("Звук", "ориг")],
        media=["1.jpg"],
        content_hash="h",
    )


async def test_index_state_and_persistence(tmp_path):
    db = tmp_path / "bot.db"
    s = Storage(db)
    await s.open()
    added, removed = await s.replace_repo_index(
        "r",
        [mk("posts/a", "2026-11-10"), mk("posts/b", "2026-11-11"), mk("posts/c", "2026-11-12")],
        [("posts/bad", "нет post.md")],
    )
    assert (added, removed) == (3, 0)
    assert await s.error_count() == 1
    q = sort_queue(await s.queue("tiktok-esoteric"))
    assert [i.path for i in q] == ["posts/a", "posts/b", "posts/c"]

    await s.mark_published("r", "posts/a")
    await s.postpone("r", "posts/b")
    post = await s.get_post(q[1].post_id)
    assert post["sections"][0].title == "Звук"
    assert post["postponed_at"]
    await s.close()

    # Перезапуск: статусы сохранились, id постов стабильны после переиндексации
    s2 = Storage(db)
    await s2.open()
    added, _ = await s2.replace_repo_index("r", [mk("posts/a"), mk("posts/b"), mk("posts/c")], [])
    assert added == 0
    assert await s2.error_count() == 0
    q2 = sort_queue(await s2.queue("tiktok-esoteric"))
    assert [i.path for i in q2] == ["posts/c", "posts/b"]
    assert q2[1].postponed_at
    assert q2[1].post_id == q[1].post_id
    pub = await s2.published()
    assert [p["title"] for p in pub] == ["posts/a"]

    await s2.unpublish("r", "posts/a")
    assert len(await s2.queue("tiktok-esoteric")) == 3
    assert await s2.published() == []
    await s2.close()


async def test_removed_posts_and_unknown_repos(tmp_path):
    s = Storage(tmp_path / "bot.db")
    await s.open()
    await s.replace_repo_index("r", [mk("posts/a"), mk("posts/b")], [])
    _, removed = await s.replace_repo_index("r", [mk("posts/a")], [])
    assert removed == 1
    await s.replace_repo_index("old", [mk("posts/z")], [("posts/q", "x")])
    await s.drop_repos_except({"r"})
    assert [i.path for i in await s.queue("tiktok-esoteric")] == ["posts/a"]
    assert await s.error_count() == 0
    await s.close()


async def test_file_id_cache_and_sync_status(tmp_path):
    s = Storage(tmp_path / "bot.db")
    await s.open()
    assert await s.get_file_id("r", "posts/a/1.jpg", "sha") is None
    await s.put_file_id("r", "posts/a/1.jpg", "sha", "photo", "FILE1")
    assert await s.get_file_id("r", "posts/a/1.jpg", "sha") == "FILE1"
    assert await s.get_file_id("r", "posts/a/1.jpg", "sha-new") is None

    await s.record_sync("r", "c1", None)
    await s.record_sync("r", None, "boom")
    (st,) = await s.sync_status()
    assert (st["last_commit"], st["last_error"]) == ("c1", "boom")
    assert st["last_ok_at"]
    await s.close()


async def test_migrations_idempotent(tmp_path):
    s = Storage(tmp_path / "bot.db")
    await s.open()
    assert await s.migrate() == len(MIGRATIONS)
    cur = await s.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = {r[0] for r in await cur.fetchall()}
    assert {"posts", "post_state", "media_cache", "post_errors", "repo_sync"} <= tables
    await s.close()
