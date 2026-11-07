from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from app.config import Projects
from app.storage import Storage
from app.sync import Redactor, Syncer
from tests.conftest import write_post

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="нужен git")


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def origin(tmp_path, content_repo) -> Path:
    git(content_repo, "init", "-q", "-b", "main")
    git(content_repo, "add", "-A")
    git(content_repo, "commit", "-qm", "init")
    return content_repo


def projects_for(origin: Path) -> Projects:
    return Projects.model_validate(
        {
            "accounts": [{"id": "tiktok-esoteric", "platform": "tiktok", "title": "🔮 TikTok"}],
            "repos": [{"id": "esoteric", "github": "o/n", "url": origin.as_uri(), "branch": "main"}],
        }
    )


async def test_sync_and_reindex(tmp_path, origin):
    storage = Storage(tmp_path / "data" / "bot.db")
    await storage.open()
    projects = projects_for(origin)
    syncer = Syncer(storage, projects, tmp_path / "data")

    (res,) = await syncer.sync_all("test")
    assert res.ok, res.message
    assert (res.posts, res.errors, res.added) == (1, 0, 1)
    assert res.commit == git(origin, "rev-parse", "HEAD")

    # Новый пост и невалидный пост в следующем коммите
    write_post(origin / "posts" / "2026-11-11-new", title="Новый")
    write_post(origin / "posts" / "2026-11-12-broken", caption=None)
    git(origin, "add", "-A")
    git(origin, "commit", "-qm", "more")
    res = await syncer.sync_repo(projects.repos[0], "test")
    assert (res.posts, res.errors, res.added) == (2, 1, 1)
    assert [e["path"] for e in await storage.errors()] == ["posts/2026-11-12-broken"]

    # Удаление поста в origin убирает его из индекса и рабочей копии
    git(origin, "rm", "-rq", "posts/2026-11-11-new")
    git(origin, "commit", "-qm", "rm")
    res = await syncer.sync_repo(projects.repos[0], "test")
    assert res.posts == 1
    assert not (tmp_path / "data" / "repos" / "esoteric" / "posts" / "2026-11-11-new").exists()
    await storage.close()


async def test_sync_failure_is_recorded_and_redacted(tmp_path):
    storage = Storage(tmp_path / "bot.db")
    await storage.open()
    projects = projects_for(tmp_path / "missing")
    syncer = Syncer(storage, projects, tmp_path, github_token="ghp_SECRET")
    (res,) = await syncer.sync_all("test")
    assert not res.ok
    (st,) = await storage.sync_status()
    assert st["last_error"]
    assert "ghp_SECRET" not in st["last_error"]
    await storage.close()


def test_token_never_in_remote_url(tmp_path):
    projects = Projects.model_validate(
        {
            "accounts": [{"id": "a", "platform": "tiktok", "title": "A"}],
            "repos": [{"id": "r", "github": "Torrexx52/tiktok-esoteric"}],
        }
    )
    syncer = Syncer(None, projects, tmp_path, github_token="ghp_SECRET")  # type: ignore[arg-type]
    url, env = syncer._remote_and_env(projects.repos[0])
    assert url == "https://github.com/Torrexx52/tiktok-esoteric.git"
    assert "ghp_SECRET" not in url
    assert env["GIT_CONFIG_VALUE_0"].startswith("Authorization: Basic ")
    assert (
        syncer.redact(f"fatal: {env['GIT_CONFIG_VALUE_0']} ghp_SECRET")
        == "fatal: Authorization: Basic *** ***"
    )


def test_deploy_key_uses_ssh(tmp_path):
    projects = Projects.model_validate(
        {
            "accounts": [{"id": "a", "platform": "tiktok", "title": "A"}],
            "repos": [{"id": "r", "github": "o/n", "deploy_key": "/app/data/ssh/r"}],
        }
    )
    url, env = Syncer(None, projects, tmp_path)._remote_and_env(projects.repos[0])  # type: ignore[arg-type]
    assert url == "git@github.com:o/n.git"
    assert "-i /app/data/ssh/r" in env["GIT_SSH_COMMAND"]


def test_redactor_ignores_empty():
    assert Redactor("", None)("text") == "text"  # type: ignore[arg-type]
