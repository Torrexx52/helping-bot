"""Синхронизация контент-репозиториев через системный git и переиндексация."""

from __future__ import annotations

import asyncio
import base64
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from app.config import Projects, Repo
from app.content import scan_posts
from app.platforms import PLATFORMS
from app.storage import Storage

log = logging.getLogger(__name__)


class SyncError(Exception):
    pass


@dataclass
class SyncResult:
    repo_id: str
    ok: bool
    commit: str | None = None
    posts: int = 0
    errors: int = 0
    added: int = 0
    message: str = ""


class Redactor:
    """Вырезает секреты из любых строк, которые могут попасть в логи или сообщения."""

    def __init__(self, *secrets: str) -> None:
        self.secrets = [s for s in secrets if s]

    def __call__(self, text: str) -> str:
        for s in self.secrets:
            text = text.replace(s, "***")
            text = text.replace(base64.b64encode(f"x-access-token:{s}".encode()).decode(), "***")
        return text


class Syncer:
    def __init__(self, storage: Storage, projects: Projects, data_dir: Path, github_token: str = "") -> None:
        self.storage = storage
        self.projects = projects
        self.repos_dir = data_dir / "repos"
        self.ssh_dir = data_dir / "ssh"
        self.token = github_token
        self.redact = Redactor(github_token)
        self._locks: dict[str, asyncio.Lock] = {}

    def set_projects(self, projects: Projects) -> None:
        self.projects = projects

    def lock(self, repo_id: str) -> asyncio.Lock:
        return self._locks.setdefault(repo_id, asyncio.Lock())

    def repo_dir(self, repo: Repo) -> Path:
        return self.repos_dir / repo.id

    # ---------- git ----------

    def _remote_and_env(self, repo: Repo) -> tuple[str, dict[str, str]]:
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"}
        if repo.url:
            return repo.url, env
        if repo.deploy_key:
            self.ssh_dir.mkdir(parents=True, exist_ok=True)
            known = self.ssh_dir / "known_hosts"
            env["GIT_SSH_COMMAND"] = (
                f"ssh -i {repo.deploy_key} -o IdentitiesOnly=yes -o BatchMode=yes "
                f"-o StrictHostKeyChecking=accept-new -o UserKnownHostsFile={known}"
            )
            return f"git@github.com:{repo.github}.git", env
        url = f"https://github.com/{repo.github}.git"
        if self.token:
            # Токен идёт заголовком через переменные окружения git: его нет ни в .git/config,
            # ни в аргументах процесса, ни в выводе git.
            basic = base64.b64encode(f"x-access-token:{self.token}".encode()).decode()
            env.update(
                GIT_CONFIG_COUNT="1",
                GIT_CONFIG_KEY_0="http.https://github.com/.extraheader",
                GIT_CONFIG_VALUE_0=f"Authorization: Basic {basic}",
            )
        return url, env

    async def _git(self, args: list[str], cwd: Path, env: dict[str, str], timeout: float = 300) -> str:
        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=cwd,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout)
        except TimeoutError as e:
            proc.kill()
            await proc.wait()
            raise SyncError(f"git {args[0]}: превышено время ожидания") from e
        if proc.returncode != 0:
            msg = self.redact((err or out).decode(errors="replace").strip())
            raise SyncError(f"git {args[0]}: {msg[-500:]}")
        return out.decode(errors="replace").strip()

    async def fetch(self, repo: Repo) -> str:
        """git fetch --depth=50 + reset --hard. Возвращает хэш коммита."""
        path = self.repo_dir(repo)
        remote, env = self._remote_and_env(repo)
        if not (path / ".git").exists():
            path.mkdir(parents=True, exist_ok=True)
            await self._git(["init", "-q"], path, env)
            await self._git(["remote", "add", "origin", remote], path, env)
        else:
            await self._git(["remote", "set-url", "origin", remote], path, env)
        b = repo.branch
        await self._git(
            ["fetch", "--depth=50", "--no-tags", "-q", "origin", f"+refs/heads/{b}:refs/remotes/origin/{b}"],
            path,
            env,
        )
        await self._git(["reset", "-q", "--hard", f"origin/{b}"], path, env)
        await self._git(["clean", "-qffd"], path, env)
        return await self._git(["rev-parse", "HEAD"], path, env)

    # ---------- индексация ----------

    def _caption_limits(self) -> dict[str, int]:
        return {a.id: PLATFORMS[a.platform].caption_limit for a in self.projects.accounts}

    async def index(self, repo: Repo) -> tuple[int, int, int]:
        accounts = {a.id for a in self.projects.accounts}
        posts, errors = await asyncio.to_thread(
            scan_posts, self.repo_dir(repo), repo.posts_path, accounts, self._caption_limits()
        )
        for path, reason in errors:
            log.warning("Невалидный пост %s/%s: %s", repo.id, path, reason)
        added, _ = await self.storage.replace_repo_index(repo.id, posts, errors)
        return len(posts), len(errors), added

    async def sync_repo(self, repo: Repo, reason: str = "manual") -> SyncResult:
        async with self.lock(repo.id):
            try:
                commit = await self.fetch(repo)
                n, e, added = await self.index(repo)
            except (SyncError, OSError) as exc:
                msg = self.redact(str(exc))
                log.error("Синхронизация %s (%s) не удалась: %s", repo.id, reason, msg)
                await self.storage.record_sync(repo.id, None, msg)
                return SyncResult(repo.id, False, message=msg)
            await self.storage.record_sync(repo.id, commit, None)
            log.info(
                "Синхронизация %s (%s): %s, постов %d, ошибок %d, новых %d",
                repo.id,
                reason,
                commit[:8],
                n,
                e,
                added,
            )
            return SyncResult(repo.id, True, commit, n, e, added)

    async def sync_all(self, reason: str = "manual") -> list[SyncResult]:
        await self.storage.drop_repos_except({r.id for r in self.projects.repos})
        return list(await asyncio.gather(*(self.sync_repo(r, reason) for r in self.projects.repos)))

    async def poll_forever(self, interval_min: int) -> None:
        while True:
            await asyncio.sleep(interval_min * 60)
            try:
                await self.sync_all("poll")
            except Exception:  # noqa: BLE001 — фоновая задача не должна падать
                log.exception("Ошибка фонового опроса")
