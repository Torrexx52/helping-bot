"""Общие сервисы приложения, доступные хендлерам."""

from __future__ import annotations

from dataclasses import dataclass, field

from app.config import Projects, Settings, load_projects
from app.leads.service import Leads
from app.storage import Storage
from app.sync import Syncer


@dataclass
class View:
    """Что сейчас открыто у пользователя: нужен, чтобы понять нажатую кнопку с номером."""

    account_id: str
    page: int = 1
    numbers: dict[int, int] = field(default_factory=dict)  # номер в очереди -> post_id


@dataclass
class Context:
    settings: Settings
    projects: Projects
    storage: Storage
    syncer: Syncer
    views: dict[int, View] = field(default_factory=dict)
    leads: Leads | None = None  # v2: поиск чатов и заявок

    @property
    def leads_enabled(self) -> bool:
        """Показывать ли в меню v2: только когда в .env настроен юзербот."""
        return self.leads is not None and self.settings.userbot_configured

    def reload_projects(self) -> Projects:
        projects = load_projects(self.settings.PROJECTS_FILE)
        self.projects = projects
        self.syncer.set_projects(projects)
        return projects
