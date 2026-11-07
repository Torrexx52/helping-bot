from __future__ import annotations

import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_REPO = ROOT / "examples" / "content-repo"
EXAMPLE_POST = "posts/2026-11-10-karta-dnya-bashnya"
ACCOUNTS = {"tiktok-esoteric"}


def write_post(
    folder: Path,
    *,
    title: str | None = "Пост",
    date: str | None = "2026-11-12",
    account: str | None = "tiktok-esoteric",
    caption: str | None = "Подпись #тег",
    media: tuple[str, ...] | list[str] = ("01.jpg",),
    extra: str = "## Звук\nОригинальный звук\n",
) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    fm = ["---"]
    if title is not None:
        fm.append(f"title: {title}")
    if date is not None:
        fm.append(f"date: {date}")
    if account is not None:
        fm.append(f"account: {account}")
    fm.append("---")
    body = "\n".join(fm) + "\n\n" + extra
    if caption is not None:
        body += f"\n## Подпись\n{caption}\n"
    (folder / "post.md").write_text(body, encoding="utf-8")
    for m in media:
        (folder / m).write_bytes(b"fake-" + m.encode())
    return folder


@pytest.fixture
def content_repo(tmp_path: Path) -> Path:
    """Копия examples/content-repo с добавленным видео (в примере медиа нет)."""
    dst = tmp_path / "repo"
    shutil.copytree(EXAMPLE_REPO, dst)
    (dst / EXAMPLE_POST / "video.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42")
    return dst
