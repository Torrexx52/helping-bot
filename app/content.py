"""Разбор post.md, валидация по docs/CONTENT_FORMAT.md, очередь и пагинация (spec §3)."""

from __future__ import annotations

import datetime as dt
import hashlib
import math
import re
from dataclasses import dataclass, field
from pathlib import Path

import frontmatter

PHOTO_EXT = {".jpg", ".jpeg", ".png", ".webp"}
VIDEO_EXT = {".mp4", ".mov"}
MEDIA_EXT = PHOTO_EXT | VIDEO_EXT
CAPTION_SECTION = "подпись"
PAGE_SIZE = 5


class PostError(Exception):
    """Пост оформлен неверно; текст — причина для /errors."""


@dataclass
class Section:
    title: str
    body: str


@dataclass
class Post:
    path: str  # путь папки поста относительно корня репозитория, через '/'
    title: str
    date: dt.date | None
    account: str
    caption: str
    sections: list[Section] = field(default_factory=list)  # все разделы, кроме подписи
    media: list[str] = field(default_factory=list)  # имена файлов в папке, в порядке показа
    content_hash: str = ""


def natural_key(name: str) -> list[object]:
    """Естественная сортировка: '2.jpg' раньше '10.jpg'."""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


def media_kind(name: str) -> str:
    return "video" if Path(name).suffix.lower() in VIDEO_EXT else "photo"


def list_media(folder: Path) -> list[str]:
    files = [p.name for p in folder.iterdir() if p.is_file() and p.suffix.lower() in MEDIA_EXT]
    return sorted(files, key=natural_key)


def split_sections(body: str) -> list[Section]:
    """Делит markdown на разделы по заголовкам '## '. Текст до первого заголовка отбрасывается."""
    sections: list[Section] = []
    current: Section | None = None
    lines: list[str] = []
    in_fence = False
    for line in body.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        m = None if in_fence else re.match(r"^##\s+(.+?)\s*#*\s*$", line)
        if m and not line.startswith("###"):
            if current is not None:
                current.body = "\n".join(lines).strip()
                sections.append(current)
            current = Section(title=m.group(1).strip(), body="")
            lines = []
        elif current is not None:
            lines.append(line)
    if current is not None:
        current.body = "\n".join(lines).strip()
        sections.append(current)
    return sections


def _loose_frontmatter(text: str) -> tuple[dict[str, str], str] | None:
    """Запасной разбор frontmatter без YAML: только строки «ключ: значение»."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    try:
        end = next(i for i, ln in enumerate(lines[1:], start=1) if ln.strip() == "---")
    except StopIteration:
        return None
    meta: dict[str, str] = {}
    for ln in lines[1:end]:
        if not ln.strip() or ln.lstrip().startswith("#"):
            continue
        key, sep, value = ln.partition(":")
        if not sep or not re.fullmatch(r"[A-Za-z_][\w-]*", key.strip()):
            return None
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        meta[key.strip()] = value
    return meta, "\n".join(lines[end + 1 :])


def _parse_date(value: object) -> dt.date | None:
    if value is None or value == "":
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str):
        try:
            return dt.date.fromisoformat(value.strip())
        except ValueError:
            pass
    raise PostError(f"date: ожидается ГГГГ-ММ-ДД, получено {value!r}")


def parse_post(
    folder: Path,
    rel_path: str,
    known_accounts: set[str],
    caption_limit: int | dict[str, int] = 2200,
) -> Post:
    """Читает папку поста. Бросает PostError с причиной, если пост невалиден."""
    md = folder / "post.md"
    if not md.is_file():
        raise PostError("нет post.md")
    raw = md.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise PostError("post.md не в кодировке UTF-8") from e
    try:
        doc = frontmatter.loads(text)
        meta, content = doc.metadata, doc.content
    except Exception as e:  # noqa: BLE001 — yaml и прочие ошибки разбора frontmatter
        # Частая ошибка: двоеточие в title без кавычек («Метод 3-6-9: как…»).
        # Тогда читаем простые строки «ключ: значение» по первому ': '.
        loose = _loose_frontmatter(text)
        if loose is None:
            raise PostError(f"ошибка во frontmatter: {e}") from e
        meta, content = loose
    if not isinstance(meta, dict):
        raise PostError("frontmatter должен состоять из строк «ключ: значение»")
    if not meta:
        raise PostError("нет frontmatter (блок --- title/date/account --- в начале post.md)")
    title = str(meta.get("title") or "").strip()
    if not title:
        raise PostError("нет поля title")
    account = str(meta.get("account") or "").strip()
    if not account:
        raise PostError("нет поля account")
    if account not in known_accounts:
        raise PostError(f"неизвестный аккаунт {account!r}")
    date = _parse_date(meta.get("date"))

    sections = split_sections(content)
    caption_sec = next((s for s in sections if s.title.strip().lower() == CAPTION_SECTION), None)
    if caption_sec is None:
        raise PostError("нет раздела «## Подпись»")
    if not caption_sec.body:
        raise PostError("раздел «## Подпись» пустой")
    limit = caption_limit.get(account, 2200) if isinstance(caption_limit, dict) else caption_limit
    if len(caption_sec.body) > limit:
        raise PostError(f"подпись длиннее {limit} символов ({len(caption_sec.body)})")

    media = list_media(folder)
    if not media:
        raise PostError("нет медиафайлов (.jpg .jpeg .png .webp .mp4 .mov)")

    h = hashlib.sha256(raw)
    for name in media:
        st = (folder / name).stat()
        h.update(f"\0{name}\0{st.st_size}".encode())

    return Post(
        path=rel_path,
        title=title,
        date=date,
        account=account,
        caption=caption_sec.body,
        sections=[s for s in sections if s is not caption_sec],
        media=media,
        content_hash=h.hexdigest(),
    )


def scan_posts(
    repo_root: Path, posts_path: str, known_accounts: set[str], caption_limit: int | dict[str, int] = 2200
) -> tuple[list[Post], list[tuple[str, str]]]:
    """Обходит posts_path: каждая папка первого уровня — пост. Возвращает (посты, ошибки)."""
    base = (repo_root / posts_path).resolve()
    posts: list[Post] = []
    errors: list[tuple[str, str]] = []
    if not base.is_dir():
        errors.append((posts_path, f"папка {posts_path}/ не найдена в репозитории"))
        return posts, errors
    for folder in sorted(base.iterdir(), key=lambda p: natural_key(p.name)):
        if not folder.is_dir() or folder.name.startswith((".", "_")):
            continue
        rel = folder.relative_to(repo_root.resolve()).as_posix()
        try:
            posts.append(parse_post(folder, rel, known_accounts, caption_limit))
        except PostError as e:
            errors.append((rel, str(e)))
        except OSError as e:
            errors.append((rel, f"ошибка чтения: {e.strerror or e}"))
    return posts, errors


# ---------- Очередь ----------


@dataclass
class QueueItem:
    post_id: int
    path: str
    title: str
    date: dt.date | None
    media: list[str]
    postponed_at: str | None = None  # ISO-время откладывания


def sort_queue(items: list[QueueItem]) -> list[QueueItem]:
    """Неотложенные по date (без даты — в конце), при равенстве по пути; затем отложенные по postponed_at."""
    fresh = [i for i in items if not i.postponed_at]
    postponed = [i for i in items if i.postponed_at]
    fresh.sort(key=lambda i: (i.date is None, i.date or dt.date.max, i.path))
    postponed.sort(key=lambda i: (i.postponed_at, i.path))
    return fresh + postponed


@dataclass
class Page:
    items: list[QueueItem]
    number: int  # с 1
    total_pages: int
    total_items: int
    offset: int  # номер первого элемента страницы минус 1

    @property
    def has_prev(self) -> bool:
        return self.number > 1

    @property
    def has_next(self) -> bool:
        return self.number < self.total_pages


def paginate(items: list[QueueItem], page: int, size: int = PAGE_SIZE) -> Page:
    total_pages = max(1, math.ceil(len(items) / size))
    page = min(max(1, page), total_pages)
    offset = (page - 1) * size
    return Page(items[offset : offset + size], page, total_pages, len(items), offset)


def media_badge(media: list[str]) -> str:
    kinds = {media_kind(m) for m in media}
    icon = "🎞" if kinds == {"video"} else "🖼" if kinds == {"photo"} else "📎"
    return icon if len(media) == 1 else f"{icon}×{len(media)}"


def truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many
