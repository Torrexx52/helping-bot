from __future__ import annotations

import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from app.config import Projects
from app.webhook import WEBHOOK_PATH, make_app, sign, verify_signature

SECRET = "s3cr3t"


def test_verify_signature():
    body = b'{"a":1}'
    good = sign(SECRET, body)
    assert good.startswith("sha256=")
    assert verify_signature(SECRET, body, good)
    assert not verify_signature(SECRET, body + b" ", good)
    assert not verify_signature("other", body, good)
    assert not verify_signature(SECRET, body, None)
    assert not verify_signature(SECRET, body, good.replace("sha256=", "sha1="))
    assert not verify_signature(None, body, good)
    assert not verify_signature("", body, sign("", body))


@pytest.fixture
def projects(monkeypatch):
    monkeypatch.setenv("GH_SECRET_ESOTERIC", SECRET)
    return Projects.model_validate(
        {
            "accounts": [{"id": "tiktok-esoteric", "platform": "tiktok", "title": "🔮 TikTok"}],
            "repos": [
                {
                    "id": "esoteric",
                    "github": "Torrexx52/tiktok-esoteric",
                    "webhook_secret_env": "GH_SECRET_ESOTERIC",
                }
            ],
        }
    )


@pytest.fixture
async def client(projects):
    calls: list[str] = []

    async def on_push(repo_id: str) -> None:
        calls.append(repo_id)

    app = make_app(lambda: projects, on_push)
    async with TestClient(TestServer(app)) as c:
        c.calls = calls
        yield c


def payload(full_name="torrexx52/TikTok-Esoteric", ref="refs/heads/main") -> bytes:
    return json.dumps({"ref": ref, "after": "abc123", "repository": {"full_name": full_name}}).encode()


async def post(client, body: bytes, event="push", secret=SECRET, signature=None):
    headers = {"X-GitHub-Event": event, "Content-Type": "application/json"}
    sig = signature if signature is not None else sign(secret, body)
    if sig:
        headers["X-Hub-Signature-256"] = sig
    return await client.post(WEBHOOK_PATH, data=body, headers=headers)


async def test_health(client):
    r = await client.get("/health")
    assert r.status == 200
    assert await r.text() == "ok"


async def test_no_signature_401(client):
    r = await client.post(WEBHOOK_PATH)
    assert r.status == 401
    assert client.calls == []


async def test_bad_signature_403(client):
    r = await post(client, payload(), secret="wrong")
    assert r.status == 403
    assert client.calls == []


async def test_unknown_repo_403(client):
    r = await post(client, payload(full_name="someone/else"))
    assert r.status == 403


async def test_push_triggers_sync(client):
    r = await post(client, payload())
    assert r.status == 202
    # фоновая задача успевает выполниться за один оборот цикла
    import asyncio

    await asyncio.sleep(0)
    assert client.calls == ["esoteric"]


async def test_push_other_branch_ignored(client):
    r = await post(client, payload(ref="refs/heads/dev"))
    assert r.status == 202
    assert client.calls == []


async def test_ping_and_other_events(client):
    assert (await post(client, payload(), event="ping")).status == 200
    assert (await post(client, payload(), event="issues")).status == 204
    assert client.calls == []


async def test_bad_payload_400(client):
    r = await post(client, b"not json")
    assert r.status == 400


async def test_missing_secret_env_rejects(client, monkeypatch):
    monkeypatch.delenv("GH_SECRET_ESOTERIC")
    r = await post(client, payload())
    assert r.status == 403
