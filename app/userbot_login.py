"""Разовый вход юзербота. Запускает владелец на сервере, код из Telegram вводит сам:

    docker compose run --rm -it bot python -m app.userbot_login && docker compose restart bot

Сессия сохраняется в data/userbot.session (права 600, в Git не попадает).
"""

from __future__ import annotations

import asyncio
import contextlib
import getpass
import os
import sys

from app.config import Settings
from app.userbot import make_client


async def login() -> int:
    settings = Settings()
    if not settings.userbot_configured:
        print("В .env не заданы TG_API_ID и TG_API_HASH (my.telegram.org, аккаунт юзербота).")
        return 2
    session = settings.userbot_session
    session.parent.mkdir(parents=True, exist_ok=True)
    client = make_client(settings)
    await client.connect()
    try:
        if await client.is_user_authorized():
            print("Сессия уже есть, повторный вход не нужен.")
        else:
            print("Вход юзербота. Нужен ОТДЕЛЬНЫЙ аккаунт Telegram, не личный.")
            await client.start(
                phone=lambda: input("Телефон юзербота (+7…): ").strip(),
                code_callback=lambda: input("Код из Telegram: ").strip(),
                password=lambda: getpass.getpass("Пароль двухэтапной проверки: "),
            )
        me = await client.get_me()
        name = " ".join(p for p in (me.first_name, me.last_name) if p)
        print(f"Готово: {name} (@{me.username or '—'}, id {me.id}).")
        print("Осталось перезапустить бота: docker compose restart bot")
    finally:
        await client.disconnect()
        with contextlib.suppress(OSError):
            os.chmod(session, 0o600)
    return 0


def main() -> None:
    try:
        sys.exit(asyncio.run(login()))
    except KeyboardInterrupt:
        print("\nОтменено.")
        sys.exit(1)


if __name__ == "__main__":
    main()
