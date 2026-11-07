"""Платформы как адаптеры (spec §9).

В v1 есть только TikTok с ручной публикацией. Будущие платформы (telegram_channel,
instagram, youtube_shorts) добавляются новым классом и записью в PLATFORMS:
у них может быть свой лимит подписи и автоматическая публикация.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Platform:
    id: str
    name: str
    caption_limit: int
    # "manual" — владелец публикует сам и жмёт «✅ Опубликовал»;
    # "auto" — в будущем бот будет публиковать сам.
    publish_mode: str = "manual"
    caption_label: str = "Подпись"


TIKTOK = Platform(id="tiktok", name="TikTok", caption_limit=2200, caption_label="Подпись для TikTok")

PLATFORMS: dict[str, Platform] = {TIKTOK.id: TIKTOK}
