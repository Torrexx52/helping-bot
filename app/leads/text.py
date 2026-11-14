"""Чистые функции над текстом: фильтр слов, хэш для дублей, разбор ссылок t.me."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable

USERNAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{3,31}")
_LINK_RE = re.compile(
    r"^(?:(?:https?://)?(?:t|telegram)\.me/(?:s/)?|@)?(?P<name>[A-Za-z][A-Za-z0-9_]{3,31})(?:[/?#].*)?$"
)
_RESERVED = {"joinchat", "addstickers", "share", "proxy", "socks", "login", "addlist"}


def normalize(text: str) -> str:
    """Нижний регистр, ё→е, один пробел между словами."""
    return " ".join(text.lower().replace("ё", "е").split())


def text_hash(text: str) -> str:
    """Хэш для поиска дублей: не зависит от регистра, пробелов и знаков препинания."""
    core = re.sub(r"[^\w]+", " ", normalize(text)).strip()
    return hashlib.sha256(core.encode()).hexdigest()


def _pattern(words: Iterable[str]) -> re.Pattern[str] | None:
    """Слово из списка совпадает с началом слова в тексте: «газел» ловит «газель», «газели»."""
    parts = sorted({re.escape(normalize(w)) for w in words if normalize(w)}, key=len, reverse=True)
    if not parts:
        return None
    return re.compile(r"(?<!\w)(?:" + "|".join(parts) + ")")


class WordFilter:
    """Грубый фильтр: есть ключевое слово и нет ни одного стоп-слова."""

    def __init__(self, keywords: Iterable[str], stop_words: Iterable[str] = ()) -> None:
        self._keys = _pattern(keywords)
        self._stops = _pattern(stop_words)

    def matches(self, text: str) -> bool:
        t = normalize(text)
        if self._keys is None or not self._keys.search(t):
            return False
        return not (self._stops is not None and self._stops.search(t))


def has_word(text: str, words: Iterable[str]) -> bool:
    p = _pattern(words)
    return bool(p and p.search(normalize(text)))


def normalize_link(value: str) -> str | None:
    """`@name`, `t.me/name`, `https://t.me/name/123` → `https://t.me/name`.

    Приватные ссылки-приглашения (`t.me/+…`, `joinchat`) не поддерживаются: возвращается None.
    """
    m = _LINK_RE.match(value.strip())
    if not m or m.group("name").lower() in _RESERVED:
        return None
    return f"https://t.me/{m.group('name')}"


def link_username(link: str) -> str:
    return link.rstrip("/").rsplit("/", 1)[-1]


def link_key(link: str) -> str:
    """Ключ строки таблицы: ссылка без учёта регистра, протокола и хвостового слэша."""
    norm = normalize_link(link)
    return (norm or link).strip().rstrip("/").lower()
