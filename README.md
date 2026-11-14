# Helping Bot

Личный Telegram-бот, который выдаёт готовые посты для соцсетей из приватных GitHub-репозиториев проектов. Открываешь аккаунт → видишь очередь по 5 постов → выбираешь номер → получаешь медиа, описание и подпись с хэштегами, которая копируется одним нажатием → публикуешь и жмёшь «✅ Опубликовал».

- Требования: [`spec.md`](spec.md) (v1, посты) и [`docs/spec-leads.md`](docs/spec-leads.md) (v2, поиск чатов и заявок)
- Задачи: [`docs/TASKS.md`](docs/TASKS.md)
- Формат постов (отдавать ИИ-агентам): [`docs/CONTENT_FORMAT.md`](docs/CONTENT_FORMAT.md)
- Сервер и деплой: [`docs/SERVER.md`](docs/SERVER.md)
- Правила для агента: [`CLAUDE.md`](CLAUDE.md)

## Подключить новый контент-репозиторий
1. Положить в него `docs/CONTENT_FORMAT.md` и папку `posts/`.
2. На сервере добавить блок в `/opt/helping-bot/config/projects.yml` и секрет webhook в `.env`
   (`openssl rand -hex 32`, имя переменной — из `webhook_secret_env`).
3. Доступ на чтение — один из вариантов:
   - **deploy key** (без PAT): `ssh-keygen -t ed25519 -N "" -f /opt/helping-bot/data/ssh/<repo_id>`,
     публичную часть добавить в GitHub → Settings → Deploy keys (Read-only),
     в `projects.yml` указать `deploy_key: /app/data/ssh/<repo_id>`;
   - **GITHUB_TOKEN** в `.env` (fine-grained PAT, Contents = Read-only) — тогда `deploy_key` не нужен.
4. Создать webhook в GitHub (см. `docs/SERVER.md`): только `push`, секрет из `.env`.
5. `docker compose restart bot` или команда `/reload` в боте.

## Команды бота
`/start` — меню аккаунтов · `/published` — последние 20 опубликованных с возвратом в очередь ·
`/errors` — невалидные посты и ошибки синхронизации · `/reload` — перечитать `projects.yml` и синхронизировать.

## Разработка
```bash
uv venv -p 3.12 .venv && uv pip install -e ".[dev]"
ruff check . && pytest
```

## Деплой
`ssh vibee-root 'cd /opt/helping-bot && ./scripts/deploy.sh'` — `git pull` → `docker compose up -d --build` → проверка `/health`.

Без `BOT_TOKEN` контейнер всё равно работает: синхронизация, webhook и `/health`. Telegram подключается, как только токен появится в `.env`.
Бэкап SQLite: `/etc/cron.d/helping-bot` → `scripts/backup.sh` каждый день в 03:30, копии в `/var/backups/helping-bot/` (14 дней).
