from __future__ import annotations

import datetime as dt

from app.config import Account
from app.content import QueueItem, media_badge, paginate, plural, sort_queue
from app.keyboards import BTN_NEXT, BTN_PREV, queue_keyboard
from app.render import queue_text


def item(pid, path, date=None, postponed=None, media=("1.jpg",), title=None):
    return QueueItem(
        pid,
        path,
        title or f"Пост {pid}",
        dt.date.fromisoformat(date) if date else None,
        list(media),
        postponed,
    )


def texts(kb):
    return [b.text for row in kb.keyboard for b in row]


def test_sort_order_spec_3():
    items = [
        item(1, "posts/c", "2026-11-12"),
        item(2, "posts/b", "2026-11-10"),
        item(3, "posts/a", "2026-11-10"),  # та же дата — по пути
        item(4, "posts/z", None),  # без даты — в конец неотложенных
        item(5, "posts/p1", "2026-11-01", postponed="2026-11-07T10:00:00+00:00"),
        item(6, "posts/p2", "2026-11-02", postponed="2026-11-07T09:00:00+00:00"),
    ]
    assert [i.post_id for i in sort_queue(items)] == [3, 2, 1, 4, 6, 5]


def test_repostpone_moves_to_end():
    a = item(1, "posts/a", "2026-11-10", postponed="2026-11-07T09:00:00+00:00")
    b = item(2, "posts/b", "2026-11-11", postponed="2026-11-07T10:00:00+00:00")
    assert [i.post_id for i in sort_queue([a, b])] == [1, 2]
    a.postponed_at = "2026-11-07T11:00:00+00:00"
    assert [i.post_id for i in sort_queue([a, b])] == [2, 1]


def test_paginate():
    items = [item(i, f"posts/{i:02}", "2026-11-10") for i in range(12)]
    p1 = paginate(items, 1)
    assert (p1.number, p1.total_pages, len(p1.items), p1.offset) == (1, 3, 5, 0)
    assert not p1.has_prev
    assert p1.has_next
    p3 = paginate(items, 3)
    assert (len(p3.items), p3.offset, p3.has_prev, p3.has_next) == (2, 10, True, False)
    assert paginate(items, 99).number == 3
    assert paginate(items, 0).number == 1
    empty = paginate([], 1)
    assert (empty.total_pages, empty.items, empty.has_next) == (1, [], False)


def test_queue_keyboard_nav_only_when_needed():
    long_title = "Очень длинное название поста про таро"
    items = [item(i, f"posts/{i:02}", "2026-11-10", title=long_title) for i in range(7)]
    k1 = texts(queue_keyboard(paginate(items, 1)))
    assert BTN_NEXT in k1
    assert BTN_PREV not in k1
    assert k1[0] == "1 · Очень длинное название…"
    assert len(k1[0]) <= len("1 · ") + 24
    k2 = texts(queue_keyboard(paginate(items, 2)))
    assert BTN_PREV in k2
    assert BTN_NEXT not in k2
    assert k2[0].startswith("6 · ")
    single = texts(queue_keyboard(paginate(items[:3], 1)))
    assert BTN_PREV not in single
    assert BTN_NEXT not in single


def test_queue_text():
    acc = Account(id="tiktok-esoteric", platform="tiktok", title="🔮 TikTok · Эзотерика")
    items = sort_queue(
        [
            item(1, "posts/a", "2026-11-10", media=("video.mp4",), title="Карта дня — Башня"),
            item(2, "posts/b", "2026-11-11", media=[f"{i}.jpg" for i in range(6)], title="Ретро <Меркурий>"),
            item(3, "posts/c", "2026-11-09", postponed="2026-11-07T10:00:00+00:00"),
        ]
        + [item(10 + i, f"posts/x{i}", "2026-12-01") for i in range(9)]
    )
    lines = queue_text(acc, paginate(items, 1), error_count=2).splitlines()
    assert lines[0] == "<b>🔮 TikTok · Эзотерика — к публикации 12 (стр. 1/3)</b>"
    assert lines[1] == "1. 10.11 · Карта дня — Башня 🎞"
    assert lines[2] == "2. 11.11 · Ретро &lt;Меркурий&gt; 🖼×6"
    assert lines[-1] == "⚠️ 2 поста с ошибками — /errors"
    last = queue_text(acc, paginate(items, 3), 0)
    assert "12. 09.11 · Пост 3 🖼 ⏸" in last
    assert "/errors" not in last


def test_badges_and_plural():
    assert media_badge(["a.mp4"]) == "🎞"
    assert media_badge(["a.jpg", "b.jpg"]) == "🖼×2"
    assert media_badge(["a.jpg", "b.mp4"]) == "📎×2"
    assert [plural(n, "пост", "поста", "постов") for n in (1, 2, 5, 11, 21, 22, 25)] == [
        "пост",
        "поста",
        "постов",
        "постов",
        "пост",
        "поста",
        "постов",
    ]
