"""Настройки: секреты из .env (pydantic-settings) и проекты из config/projects.yml."""

from __future__ import annotations

import os
import re
from pathlib import Path

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)
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

    # v2: поиск чатов и заявок (docs/spec-leads.md). Всё необязательно: без TG_API_ID и сессии
    # юзербот не запускается и бот работает как v1.
    TG_API_ID: int = 0
    TG_API_HASH: str = ""
    LLM_BASE_URL: str = ""
    LLM_API_KEY: str = ""
    LLM_MODEL: str = ""
    LLM_TIMEOUT: float = Field(default=20, gt=0)
    LLM_DAILY_LIMIT: int = Field(default=300, ge=0)
    LEADS_DATA_REPO: str = "torrexx52/helping-bot-data"
    LEADS_DATA_BRANCH: str = "main"
    LEADS_FILE: Path = Path("config/leads.yml")
    JOIN_PER_DAY: int = Field(default=15, ge=0)
    SCOUT_QUERIES_PER_RUN: int = Field(default=20, ge=1)
    SCOUT_MAX_CHATS: int = Field(default=60, ge=1)
    SCOUT_CRON: str = ""

    @field_validator("OWNER_TELEGRAM_ID", "TG_API_ID", mode="before")
    @classmethod
    def _empty_owner(cls, v: object) -> object:
        return 0 if v in ("", None) else v

    @field_validator(
        "LLM_TIMEOUT",
        "LLM_DAILY_LIMIT",
        "JOIN_PER_DAY",
        "SCOUT_QUERIES_PER_RUN",
        "SCOUT_MAX_CHATS",
        "LEADS_DATA_BRANCH",
        "LEADS_FILE",
        mode="before",
    )
    @classmethod
    def _empty_default(cls, v: object, info: ValidationInfo) -> object:
        """Пустая строка в .env («JOIN_PER_DAY=») означает значение по умолчанию."""
        if isinstance(v, str) and not v.strip():
            return cls.model_fields[info.field_name].default
        return v

    @field_validator("LEADS_DATA_REPO")
    @classmethod
    def _data_repo(cls, v: str) -> str:
        v = v.strip()
        if v and not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", v):
            raise ValueError("LEADS_DATA_REPO должен быть в виде owner/name")
        return v

    @property
    def userbot_configured(self) -> bool:
        return bool(self.TG_API_ID and self.TG_API_HASH)

    @property
    def userbot_session(self) -> Path:
        return self.DATA_DIR / "userbot.session"

    @property
    def leads_data_key(self) -> Path:
        return self.DATA_DIR / "ssh" / "leads-data"


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


def _load_yaml[M: BaseModel](path: Path, model: type[M], missing: str) -> M:
    if not path.exists():
        raise ConfigError(missing)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"{path}: ошибка YAML: {e}") from e
    try:
        return model.model_validate(raw)
    except ValidationError as e:
        lines = []
        for err in e.errors():
            loc = ".".join(str(p) for p in err["loc"]) or "<корень>"
            lines.append(f"  {loc}: {err['msg']}")
        raise ConfigError(f"{path}: неверная конфигурация:\n" + "\n".join(lines)) from e


def load_projects(path: Path) -> Projects:
    return _load_yaml(
        path, Projects, f"Файл проектов не найден: {path}. Скопируйте config/projects.example.yml."
    )


# ---------- v2: config/leads.yml ----------


def _words(v: object) -> object:
    """Список непустых строк без повторов (порядок сохраняется)."""
    if not isinstance(v, list):
        return v
    out: list[str] = []
    for item in v:
        if not isinstance(item, str | int | float):
            raise ValueError("элементы списка должны быть строками")
        s = str(item).strip()
        if s and s.lower() not in {o.lower() for o in out}:
            out.append(s)
    return out


class ScoutConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    queries: list[str] = Field(min_length=1)
    geo_words: list[str] = Field(min_length=1)

    _clean = field_validator("queries", "geo_words", mode="before")(_words)


class MonitorConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    keywords: list[str] = Field(min_length=1)
    stop_words: list[str] = []

    _clean = field_validator("keywords", "stop_words", mode="before")(_words)


class LeadsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scout: ScoutConfig
    monitor: MonitorConfig


def load_leads(path: Path) -> LeadsConfig:
    return _load_yaml(
        path, LeadsConfig, f"Файл {path} не найден. Скопируйте config/leads.example.yml в config/leads.yml."
    )
