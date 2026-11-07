"""Настройки: секреты из .env (pydantic-settings) и проекты из config/projects.yml."""

from __future__ import annotations

import os
import re
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.platforms import PLATFORMS


class ConfigError(Exception):
    """Ошибка конфигурации с понятным текстом для логов."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    BOT_TOKEN: str = ""
    OWNER_TELEGRAM_ID: int = 0
    GITHUB_TOKEN: str = ""
    WEBHOOK_HOST: str = "127.0.0.1"
    WEBHOOK_PORT: int = 8081
    SYNC_INTERVAL_MIN: int = Field(default=15, ge=1)
    LOG_LEVEL: str = "INFO"
    TZ: str = "Europe/Moscow"
    DATA_DIR: Path = Path("data")
    PROJECTS_FILE: Path = Path("config/projects.yml")

    @field_validator("OWNER_TELEGRAM_ID", mode="before")
    @classmethod
    def _empty_owner(cls, v: object) -> object:
        return 0 if v in ("", None) else v


class Account(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    platform: str
    title: str

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", v):
            raise ValueError("id аккаунта: только латиница в нижнем регистре, цифры, '-' и '_'")
        return v

    @field_validator("platform")
    @classmethod
    def _platform(cls, v: str) -> str:
        if v not in PLATFORMS:
            raise ValueError(f"неизвестная платформа {v!r}, поддерживаются: {', '.join(PLATFORMS)}")
        return v


class Repo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    project: str = ""
    github: str
    branch: str = "main"
    posts_path: str = "posts"
    webhook_secret_env: str | None = None
    # Необязательно: путь к приватному deploy key (внутри контейнера). Если задан,
    # репозиторий клонируется по SSH, иначе — по HTTPS с GITHUB_TOKEN.
    deploy_key: str | None = None
    # Только для тестов и локальной отладки: явный URL/путь вместо github.com.
    url: str | None = None

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", v):
            raise ValueError("id репозитория: только латиница в нижнем регистре, цифры, '-' и '_'")
        return v

    @field_validator("github")
    @classmethod
    def _github(cls, v: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", v):
            raise ValueError("github должен быть в виде owner/name")
        return v

    @field_validator("posts_path")
    @classmethod
    def _posts_path(cls, v: str) -> str:
        v = v.strip().strip("/")
        if ".." in Path(v).parts:
            raise ValueError("posts_path не может выходить за пределы репозитория")
        return v or "."

    @property
    def webhook_secret(self) -> str | None:
        if not self.webhook_secret_env:
            return None
        return os.environ.get(self.webhook_secret_env) or None


class Projects(BaseModel):
    model_config = ConfigDict(extra="forbid")

    accounts: list[Account]
    repos: list[Repo]

    @model_validator(mode="after")
    def _unique(self) -> Projects:
        for name, items in (("аккаунтов", self.accounts), ("репозиториев", self.repos)):
            ids = [i.id for i in items]
            dup = {i for i in ids if ids.count(i) > 1}
            if dup:
                raise ValueError(f"повторяются id {name}: {', '.join(sorted(dup))}")
        if not self.accounts:
            raise ValueError("нужен хотя бы один аккаунт")
        return self

    def account(self, account_id: str) -> Account | None:
        return next((a for a in self.accounts if a.id == account_id), None)

    def repo(self, repo_id: str) -> Repo | None:
        return next((r for r in self.repos if r.id == repo_id), None)

    def repo_by_full_name(self, full_name: str) -> Repo | None:
        full_name = full_name.lower()
        return next((r for r in self.repos if r.github.lower() == full_name), None)


def load_projects(path: Path) -> Projects:
    if not path.exists():
        raise ConfigError(f"Файл проектов не найден: {path}. Скопируйте config/projects.example.yml.")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"{path}: ошибка YAML: {e}") from e
    try:
        projects = Projects.model_validate(raw)
    except ValidationError as e:
        lines = []
        for err in e.errors():
            loc = ".".join(str(p) for p in err["loc"]) or "<корень>"
            lines.append(f"  {loc}: {err['msg']}")
        raise ConfigError(f"{path}: неверная конфигурация:\n" + "\n".join(lines)) from e
    return projects
