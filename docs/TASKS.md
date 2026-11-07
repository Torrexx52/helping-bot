# Задачи

Подробные требования — в `spec.md`. Задачи выполнять по порядку и отмечать `[x]`.

## Этап 0. Каркас
- [ ] Структура Python-проекта: `app/` (bot, handlers, keyboards, content, sync, storage, webhook), `tests/`, `pyproject.toml` (aiogram 3, aiosqlite, python-frontmatter, PyYAML, pydantic-settings; dev: pytest, pytest-asyncio, ruff).
- [ ] Загрузка настроек из `.env` (pydantic-settings) и `config/projects.yml` с валидацией и понятными ошибками.
- [ ] `Dockerfile` (python:3.12-slim + git) и `docker-compose.yml` по `spec.md` §7.
- [ ] Миграции SQLite: таблицы `posts`, `post_state`, `media_cache`, `post_errors`, `repo_sync`.

## Этап 1. Контент
- [ ] Клонирование и синхронизация репозиториев (`git fetch` + `reset --hard`) с блокировкой на репозиторий; токен не попадает в логи.
- [ ] Парсер `post.md`: frontmatter, разделы `##`, подпись; валидация по `docs/CONTENT_FORMAT.md`.
- [ ] Индексатор: обход `posts_path`, запись в `posts` и `post_errors`.
- [ ] Очередь: сортировка и пагинация по `spec.md` §3.
- [ ] Тесты на парсер, валидацию, сортировку и пагинацию; фикстуры — пример из `examples/content-repo/`.

## Этап 2. Бот
- [ ] Middleware: доступ только для `OWNER_TELEGRAM_ID`.
- [ ] Главное меню, очередь с нижней клавиатурой, листание, «Обновить», «Меню».
- [ ] Карточка поста: медиа (одиночное, альбом, больше 10 файлов), информация, подпись в `<pre>` с HTML-экранированием, `CopyTextButton` при подписи ≤256 символов.
- [ ] Кэш `file_id`.
- [ ] «✅ Опубликовал», «⏸ Отложить», `/published` с возвратом в очередь, `/errors`, `/reload`.

## Этап 3. Синхронизация по событиям
- [ ] aiohttp-сервер: `POST /hooks/helping-bot/github` (проверка `X-Hub-Signature-256`, событие `push`, выбор репозитория по `repository.full_name`), `GET /health`.
- [ ] Фоновый опрос каждые `SYNC_INTERVAL_MIN` минут.
- [ ] Тест на проверку подписи.

## Этап 4. Деплой на `vibee-root`
- [ ] Deploy key на сервере, клон в `/opt/helping-bot`, `.env` (chmod 600), `config/projects.yml`.
- [ ] `scripts/deploy.sh`, запуск контейнера, healthcheck.
- [ ] Location в Nginx по `docs/SERVER.md` с проверками. Cargo должен по-прежнему отдавать 200.
- [ ] Webhook в контент-репозитории.
- [ ] Бэкап SQLite (cron, ротация 14 дней).
- [ ] Сквозная проверка по критериям `spec.md` §10 и отчёт владельцу.

## Нужно от владельца
- [ ] Токен бота от @BotFather.
- [ ] Свой Telegram ID (узнать можно у @userinfobot).
- [ ] Адрес контент-репозитория проекта «Эзотерика» и путь к папке с постами.
- [ ] Fine-grained GitHub PAT: Contents = Read-only для контент-репозиториев. Если агент сам создаёт webhook, нужно ещё Webhooks = Read and write.
