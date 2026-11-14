"""Репозиторий данных (`helping-bot-data`): клон по deploy key, правка CSV, коммит и push.

Без ключа или сети работает локально: коммиты копятся в `data/leads-data` и уходят при следующей
удачной записи. Ручные правки в GitHub не теряются: перед каждой записью берётся свежая версия.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from pathlib import Path

from app.leads.table import README, Table

log = logging.getLogger(__name__)

CSV_PATH = "chats/telegram.csv"
IDENTITY = ["-c", "user.name=helping-bot", "-c", "user.email=helping-bot@users.noreply.github.com"]


class DataRepoError(Exception):
    pass


class DataRepo:
    def __init__(
        self,
        path: Path,
        github: str = "",
        deploy_key: Path | None = None,
        branch: str = "main",
        url: str | None = None,
    ) -> None:
        self.path = path
        self.github = github
        self.deploy_key = deploy_key
        self.branch = branch
        self.url = url  # только для тестов: явный URL или путь вместо github.com
        self.lock = asyncio.Lock()

    # ---------- git ----------

    @property
    def remote(self) -> str | None:
        if self.url:
            return self.url
        if self.github and self.deploy_key and self.deploy_key.exists():
            return f"git@github.com:{self.github}.git"
        return None

    def why_local(self) -> str:
        if not self.github:
            return "LEADS_DATA_REPO не задан"
        return f"нет deploy key {self.deploy_key}"

    def _env(self) -> dict[str, str]:
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"}
        if self.deploy_key and not self.url:
            known = self.deploy_key.parent / "known_hosts"
            env["GIT_SSH_COMMAND"] = (
                f"ssh -i {self.deploy_key} -o IdentitiesOnly=yes -o BatchMode=yes "
                f"-o StrictHostKeyChecking=accept-new -o UserKnownHostsFile={known}"
            )
        return env

    async def _run(self, *args: str, timeout: float = 120) -> tuple[int, str]:
        proc = await asyncio.create_subprocess_exec(
            "git",
            *IDENTITY,
            *args,
            cwd=self.path,
            env=self._env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return 124, f"git {args[0]}: превышено время ожидания"
        return proc.returncode or 0, out.decode(errors="replace").strip()

    async def _git(self, *args: str) -> str:
        code, out = await self._run(*args)
        if code != 0:
            raise DataRepoError(f"git {args[0]}: {out[-300:]}")
        return out

    async def _pull(self) -> str | None:
        """Приводит рабочую копию к свежей версии. Возвращает предупреждение, если работаем локально."""
        if not (self.path / ".git").exists():
            self.path.mkdir(parents=True, exist_ok=True)
            await self._git("init", "-q")
            await self._git("symbolic-ref", "HEAD", f"refs/heads/{self.branch}")
        remote = self.remote
        if remote is None:
            return f"репозиторий данных не подключён ({self.why_local()}) — пишу только локально"
        code, _ = await self._run("remote", "get-url", "origin")
        await self._git("remote", "set-url" if code == 0 else "add", "origin", remote)
        ref = f"refs/remotes/origin/{self.branch}"
        code, out = await self._run("fetch", "--no-tags", "-q", "origin", f"+refs/heads/{self.branch}:{ref}")
        if code != 0:
            if "couldn't find remote ref" in out:
                return None  # пустой репозиторий: ветку создаст первый push
            log.warning("Репозиторий данных недоступен: %s", out[-300:])
            return "репозиторий данных недоступен — изменения сохранены локально и уйдут позже"
        has_head, _ = await self._run("rev-parse", "-q", "--verify", "HEAD")
        ahead = 0
        if has_head == 0:
            ahead = int(await self._git("rev-list", "--count", f"{ref}..HEAD"))
        if ahead == 0:
            await self._git("reset", "-q", "--hard", ref)
        else:
            # Есть неотправленные локальные коммиты (писали без сети или без ключа).
            code, _ = await self._run("rebase", "-q", ref)
            if code != 0:
                # Истории разошлись (например, локальный репозиторий создан до подключения ключа):
                # берём версию с GitHub и накладываем локальные строки поверх.
                await self._run("rebase", "--abort")
                local = await asyncio.to_thread(Table.load, self.csv_file)
                await self._git("reset", "-q", "--hard", ref)
                merged = await asyncio.to_thread(Table.load, self.csv_file)
                merged.overlay(local)
                await asyncio.to_thread(merged.save, self.csv_file)
                await self._git("add", "-A")
                if await self._git("status", "--porcelain"):
                    await self._git("commit", "-q", "-m", "sync: локальные изменения бота")
        return None

    async def _push(self) -> str | None:
        if self.remote is None:
            return None
        has_head, _ = await self._run("rev-parse", "-q", "--verify", "HEAD")
        if has_head != 0:
            return None
        code, out = await self._run("push", "-q", "origin", f"HEAD:refs/heads/{self.branch}")
        if code != 0:  # кто-то успел запушить — подтягиваем и пробуем ещё раз
            await self._pull()
            code, out = await self._run("push", "-q", "origin", f"HEAD:refs/heads/{self.branch}")
        if code != 0:
            log.warning("Push в репозиторий данных не удался: %s", out[-300:])
            return "не удалось отправить изменения в репозиторий данных — сохранены локально"
        return None

    # ---------- таблица ----------

    @property
    def csv_file(self) -> Path:
        return self.path / CSV_PATH

    async def read(self, pull: bool = True) -> tuple[Table, str | None]:
        """Возвращает таблицу и предупреждение (если свежую версию получить не удалось)."""
        async with self.lock:
            warning = None
            if pull:
                try:
                    warning = await self._pull()
                except (DataRepoError, OSError) as e:
                    warning = str(e)
            return await asyncio.to_thread(Table.load, self.csv_file), warning

    async def edit[T](
        self, mutate: Callable[[Table], T], message: str | Callable[[T], str]
    ) -> tuple[T, str | None]:
        """Свежая версия → mutate(table) → запись, коммит, push. Возвращает (результат, предупреждение)."""
        async with self.lock:
            try:
                warning = await self._pull()
            except OSError as e:
                raise DataRepoError(str(e)) from e
            table = await asyncio.to_thread(Table.load, self.csv_file)
            before = table.dumps()
            result = mutate(table)
            readme = self.path / "README.md"
            need_readme = not readme.exists() or "telegram.csv" not in readme.read_text(
                encoding="utf-8", errors="replace"
            )
            if table.dumps() != before or not self.csv_file.exists():
                await asyncio.to_thread(table.save, self.csv_file)
            if need_readme:
                readme.write_text(README, encoding="utf-8", newline="\n")
            await self._git("add", "-A")
            if await self._git("status", "--porcelain"):
                text = message if isinstance(message, str) else message(result)
                await self._git("commit", "-q", "-m", text)
            return result, warning or await self._push()
