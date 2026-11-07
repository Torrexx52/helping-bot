from __future__ import annotations

import datetime as dt

import pytest

from app.content import PostError, natural_key, parse_post, scan_posts, split_sections
from tests.conftest import ACCOUNTS, EXAMPLE_POST, write_post


def test_parse_example_post(content_repo):
    post = parse_post(content_repo / EXAMPLE_POST, EXAMPLE_POST, ACCOUNTS)
    assert post.title == "Карта дня — Башня"
    assert post.date == dt.date(2026, 11, 10)
    assert post.account == "tiktok-esoteric"
    assert post.caption.startswith("Башня — это не катастрофа")
    assert post.caption.endswith("#матрицасудьбы")
    assert [s.title for s in post.sections] == ["О чём пост", "Звук"]
    assert post.sections[1].body == "Оригинальный звук"
    assert post.media == ["video.mp4"]
    assert len(post.content_hash) == 64


def test_example_without_media_is_invalid(content_repo):
    (content_repo / EXAMPLE_POST / "video.mp4").unlink()
    with pytest.raises(PostError, match="нет медиафайлов"):
        parse_post(content_repo / EXAMPLE_POST, EXAMPLE_POST, ACCOUNTS)


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"caption": None}, "нет раздела «## Подпись»"),
        ({"caption": ""}, "пустой"),
        ({"account": "instagram-x"}, "неизвестный аккаунт"),
        ({"account": None}, "нет поля account"),
        ({"title": None}, "нет поля title"),
        ({"media": ()}, "нет медиафайлов"),
        ({"date": "10.11.2026"}, "date: ожидается"),
        ({"caption": "x" * 2201}, "длиннее 2200"),
    ],
)
def test_invalid_posts(tmp_path, kwargs, reason):
    folder = write_post(tmp_path / "p", **kwargs)
    with pytest.raises(PostError, match=reason):
        parse_post(folder, "posts/p", ACCOUNTS)


def test_missing_post_md(tmp_path):
    (tmp_path / "p").mkdir()
    with pytest.raises(PostError, match="нет post.md"):
        parse_post(tmp_path / "p", "posts/p", ACCOUNTS)


def test_no_frontmatter(tmp_path):
    (tmp_path / "p").mkdir()
    (tmp_path / "p" / "post.md").write_text("# Заголовок\n\n## Подпись\nтекст\n", encoding="utf-8")
    (tmp_path / "p" / "1.jpg").write_bytes(b"x")
    with pytest.raises(PostError, match="нет frontmatter"):
        parse_post(tmp_path / "p", "posts/p", ACCOUNTS)


def test_date_optional_and_caption_limit_exact(tmp_path):
    folder = write_post(tmp_path / "p", date=None, caption="x" * 2200)
    post = parse_post(folder, "posts/p", ACCOUNTS)
    assert post.date is None
    assert len(post.caption) == 2200


def test_media_natural_order_and_filter(tmp_path):
    folder = write_post(
        tmp_path / "p", media=("10.jpg", "2.jpg", "1.PNG", "03.mp4", "notes.txt", "cover.psd")
    )
    post = parse_post(folder, "posts/p", ACCOUNTS)
    assert post.media == ["1.PNG", "2.jpg", "03.mp4", "10.jpg"]


def test_natural_key():
    assert sorted(["10.jpg", "2.jpg", "1.jpg"], key=natural_key) == ["1.jpg", "2.jpg", "10.jpg"]


def test_sections_keep_order_and_ignore_h3_and_fences():
    body = (
        "intro\n## О чём\nтекст\n### мелкий\nещё\n```\n## не заголовок\n```\n"
        "## Подпись\nподпись\n## Заметки\nз\n"
    )
    secs = split_sections(body)
    assert [s.title for s in secs] == ["О чём", "Подпись", "Заметки"]
    assert "### мелкий" in secs[0].body
    assert "## не заголовок" in secs[0].body


def test_caption_section_case_insensitive(tmp_path):
    folder = write_post(tmp_path / "p", caption=None, extra="## ПОДПИСЬ\nтекст #тег\n")
    assert parse_post(folder, "posts/p", ACCOUNTS).caption == "текст #тег"


def test_scan_posts_collects_errors(content_repo):
    posts_dir = content_repo / "posts"
    write_post(posts_dir / "2026-11-11-ok", title="Второй")
    write_post(posts_dir / "2026-11-12-bad", caption=None)
    write_post(posts_dir / "_drafts", title="Черновик")  # служебные папки пропускаются
    (posts_dir / "README.md").write_text("файл, не пост", encoding="utf-8")
    posts, errors = scan_posts(content_repo, "posts", ACCOUNTS)
    assert [p.path for p in posts] == [EXAMPLE_POST, "posts/2026-11-11-ok"]
    assert errors == [("posts/2026-11-12-bad", "нет раздела «## Подпись»")]


def test_scan_missing_posts_dir(tmp_path):
    posts, errors = scan_posts(tmp_path, "posts", ACCOUNTS)
    assert posts == []
    assert "не найдена" in errors[0][1]


def test_colon_in_title_without_quotes(tmp_path):
    folder = write_post(tmp_path / "p", title="Метод 3-6-9: как писать желания 11:11")
    post = parse_post(folder, "posts/p", ACCOUNTS)
    assert post.title == "Метод 3-6-9: как писать желания 11:11"
    assert post.date == dt.date(2026, 11, 12)
    assert post.caption == "Подпись #тег"


def test_quoted_title_and_broken_frontmatter(tmp_path):
    folder = write_post(tmp_path / "p", title='"Выбери карту: декабрь"')
    assert parse_post(folder, "posts/p", ACCOUNTS).title == "Выбери карту: декабрь"
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "post.md").write_text("---\ntitle: [unclosed\n- list item\n---\n## Подпись\nx\n", encoding="utf-8")
    (bad / "1.jpg").write_bytes(b"x")
    with pytest.raises(PostError, match="ошибка во frontmatter"):
        parse_post(bad, "posts/bad", ACCOUNTS)
