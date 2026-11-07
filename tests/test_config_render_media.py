from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from app.config import Account, ConfigError, load_projects
from app.content import Section
from app.keyboards import BTN_COPY, card_keyboard
from app.media import MediaFile, classify, plan_media
from app.render import caption_text, info_text, split_text
from tests.conftest import ROOT

ACC = Account(id="tiktok-esoteric", platform="tiktok", title="🔮 TikTok · Эзотерика")


def test_example_projects_valid(tmp_path):
    raw = (ROOT / "config" / "projects.example.yml").read_text(encoding="utf-8")
    f = tmp_path / "projects.yml"
    f.write_text(raw.replace("ЗАМЕНИТЬ-content-repo", "content"), encoding="utf-8")
    p = load_projects(f)
    assert p.account("tiktok-esoteric").platform == "tiktok"
    assert p.repo("esoteric").posts_path == "posts"
    assert p.repo_by_full_name(p.repo("esoteric").github.upper()) is not None


@pytest.mark.parametrize(
    ("yml", "msg"),
    [
        ("accounts: [{id: a, platform: myspace, title: A}]\nrepos: []", "неизвестная платформа"),
        ("accounts: []\nrepos: []", "хотя бы один аккаунт"),
        (
            "accounts: [{id: a, platform: tiktok, title: A}, {id: a, platform: tiktok, title: B}]\nrepos: []",
            "повторяются id",
        ),
        ("accounts: [{id: a, platform: tiktok, title: A}]\nrepos: [{id: r, github: bad}]", "owner/name"),
        (
            "accounts: [{id: a, platform: tiktok, title: A}]\nrepos: [{id: r, github: o/n, extra: 1}]",
            "repos.0.extra",
        ),
        ("accounts: [", "ошибка YAML"),
    ],
)
def test_projects_errors(tmp_path: Path, yml, msg):
    f = tmp_path / "projects.yml"
    f.write_text(yml, encoding="utf-8")
    with pytest.raises(ConfigError, match=msg):
        load_projects(f)


def test_projects_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="не найден"):
        load_projects(tmp_path / "nope.yml")


def test_caption_in_pre_escaped():
    text = caption_text("Текст <b>не жирный</b> & #тег", ACC)
    assert text == "<b>Подпись для TikTok</b>\n<pre>Текст &lt;b&gt;не жирный&lt;/b&gt; &amp; #тег</pre>"


def test_info_text_has_all_sections():
    text = info_text(
        "Заголовок <1>", dt.date(2026, 11, 10), [Section("О чём", "идея"), Section("Звук", "")], ACC
    )
    assert text.splitlines()[:2] == ["<b>Заголовок &lt;1&gt;</b>", "📅 10.11.2026"]
    assert "<b>О чём</b>\nидея" in text
    assert "<b>Звук</b>" in text


def test_copy_button_threshold():
    short = card_keyboard(7, "x" * 256)
    assert short.inline_keyboard[0][0].text == BTN_COPY
    assert short.inline_keyboard[0][0].copy_text.text == "x" * 256
    assert [b.callback_data for b in short.inline_keyboard[1]] == ["pub:7", "pp:7"]
    long = card_keyboard(7, "x" * 257)
    assert len(long.inline_keyboard) == 1
    assert long.inline_keyboard[0][0].callback_data == "pub:7"
    assert card_keyboard(7, "x" * 300, with_actions=False) is None


def test_split_text():
    assert split_text("abc", 10) == ["abc"]
    parts = split_text("\n".join(["x" * 6] * 5), 14)
    assert all(len(p) <= 14 for p in parts)
    assert "".join(parts).replace("\n", "") == "x" * 30
    assert split_text("y" * 25, 10) == ["y" * 10, "y" * 10, "y" * 5]


def mf(name, size):
    return MediaFile(name, Path(name), name, size, classify(name, size))


def test_plan_media_albums_docs_and_limits():
    mb = 1024 * 1024
    files = [mf(f"{i:02}.jpg", mb) for i in range(12)] + [mf("big.png", 11 * mb), mf("huge.mp4", 51 * mb)]
    groups, docs, too_big = plan_media(files)
    assert [len(g) for g in groups] == [10, 2]
    assert [d.name for d in docs] == ["big.png"]
    assert [d.name for d in too_big] == ["huge.mp4"]
    assert classify("v.mp4", 40 * mb) == "video"
    assert classify("p.jpg", 10 * mb) == "photo"
