from __future__ import annotations

from types import SimpleNamespace

from aiogram.types import Chat, Message, User

from app.access import PRIVATE_TEXT, AccessMiddleware, AccessPolicy

OWNER = 111


def make_message(user_id: int) -> Message:
    msg = Message.model_construct(
        message_id=1,
        date=0,
        chat=Chat(id=user_id, type="private"),
        from_user=User(id=user_id, is_bot=False, first_name="u"),
        text="/start",
    )
    return msg


async def test_owner_passes_stranger_blocked(monkeypatch):
    mw = AccessMiddleware(AccessPolicy(OWNER))
    handled: list[int] = []
    answers: list[str] = []

    async def handler(event, data):
        handled.append(data["event_from_user"].id)
        return "ok"

    async def fake_answer(self, text, **kw):
        answers.append(text)

    monkeypatch.setattr(Message, "answer", fake_answer)

    owner = make_message(OWNER)
    assert await mw(handler, owner, {"event_from_user": owner.from_user}) == "ok"

    stranger = make_message(222)
    for _ in range(3):
        assert await mw(handler, stranger, {"event_from_user": stranger.from_user}) is None
    assert handled == [OWNER]
    assert answers == [PRIVATE_TEXT]  # чужому отвечаем один раз


async def test_no_owner_configured_blocks_everyone():
    policy = AccessPolicy(0)
    assert not policy.is_allowed(0)
    assert not policy.is_allowed(OWNER)
    mw = AccessMiddleware(policy)

    async def handler(event, data):  # pragma: no cover — не должен вызываться
        raise AssertionError

    assert await mw(handler, SimpleNamespace(), {"event_from_user": None}) is None
