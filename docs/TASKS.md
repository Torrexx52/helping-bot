# Задачи

Подробные требования — в `spec.md`. Задачи выполнять по порядку и отмечать `[x]`.

## Этап 0. Каркас
- [x] Структура Python-проекта: `app/` (bot, handlers, keyboards, content, sync, storage, webhook), `tests/`, `pyproject.toml` (aiogram 3, aiosqlite, python-frontmatter, PyYAML, pydantic-settings; dev: pytest, pytest-asyncio, ruff).
- [x] Загрузка настроек из `.env` (pydantic-settings) и `config/projects.yml` с валидацией и понятными ошибками.
- [x] `Dockerfile` (python:3.12-slim + git) и `docker-compose.yml` по `spec.md` §7.
- [x] Миграции SQLite: таблицы `posts`, `post_state`, `media_cache`, `post_errors`, `repo_sync`.

## Этап 1. Контент
- [x] Клонирование и синхронизация репозиториев (`git fetch` + `reset --hard`) с блокировкой на репозиторий; токен не попадает в логи.
- [x] Парсер `post.md`: frontmatter, разделы `##`, подпись; валидация по `docs/CONTENT_FORMAT.md`.
- [x] Индексатор: обход `posts_path`, запись в `posts` и `post_errors`.
- [x] Очередь: сортировка и пагинация по `spec.md` §3.
- [x] Тесты на парсер, валидацию, сортировку и пагинацию; фикстуры — пример из `examples/content-repo/`.

## Этап 2. Бот
- [x] Middleware: доступ только для `OWNER_TELEGRAM_ID`.
- [x] Главное меню, очередь с нижней клавиатурой, листание, «Обновить», «Меню».
- [x] Карточка поста: медиа (одиночное, альбом, больше 10 файлов), информация, подпись в `<pre>` с HTML-экранированием, `CopyTextButton` при подписи ≤256 символов.
- [x] Кэш `file_id`.
- [x] «✅ Опубликовал», «⏸ Отложить», `/published` с возвратом в очередь, `/errors`, `/reload`.

## Этап 3. Синхронизация по событиям
- [x] aiohttp-сервер: `POST /hooks/helping-bot/github` (проверка `X-Hub-Signature-256`, событие `push`, выбор репозитория по `repository.full_name`), `GET /health`.
- [x] Фоновый опрос каждые `SYNC_INTERVAL_MIN` минут.
- [x] Тест на проверку подписи.

## Этап 4. Деплой на `vibee-root`
- [x] Клон в `/opt/helping-bot` (репозиторий бота публичный — по HTTPS, read-only), `.env` (chmod 600), `config/projects.yml`. Read-only deploy key для контент-репозитория: `data/ssh/esoteric`.
- [x] `scripts/deploy.sh`, запуск контейнера, healthcheck.
- [x] Location в Nginx по `docs/SERVER.md` с проверками. Cargo должен по-прежнему отдавать 200.
- [x] Webhook в контент-репозитории (push → очередь за ~4 с).
- [x] Бэкап SQLite (cron, ротация 14 дней).
- [x] Сквозная проверка по критериям `spec.md` §10 и отчёт владельцу.

## Этап 5. Поиск чатов и заявок — v2 (`docs/spec-leads.md`)
- [ ] Зависимости: Telethon, OpenAI-совместимый клиент (httpx). Настройки v2 в `Settings`, `config/leads.example.yml`, загрузка `config/leads.yml` с валидацией.
- [ ] `app/userbot_login.py` — разовый вход юзербота, сессия `data/userbot.session` (600, в .gitignore).
- [ ] Юзербот (Telethon) в том же процессе, что и aiogram; запускается, только если есть `TG_API_ID` и сессия. Без них v1 работает как раньше.
- [ ] `app/llm.py` — вызов ИИ с JSON-ответом, таймаут, дневной лимит, режим «без проверки» при ошибке.
- [ ] Разведка (`/scout`): поиск по запросам, гео-фильтр, сбор данных о чате без вступления, правила рекламы и контакт админа через ИИ, сообщение-итог.
- [ ] Репозиторий данных: клон `helping-bot-data` по deploy key с записью, слияние `chats/telegram.csv` по `Ссылка` без затирания ручных колонок, коммит и push.
- [ ] Мониторинг: `/watch`, `/unwatch`, `/watching`; очередь вступлений с лимитами и FloodWait; фильтр слов → ИИ → карточка владельцу; кнопки 👍/👎; дубли по хэшу 7 дней; `/leadstats`.
- [ ] Миграции SQLite: `lead_seen` (хэш, дата), `lead_stats` (чат, дата, заявки, 👍, 👎), `join_queue`. Никаких текстов и авторов.
- [ ] Тесты: слияние CSV, фильтр слов, разбор ответа ИИ, лимиты вступлений, дубли.
- [ ] Деплой на `vibee-root`, вход юзербота, первая разведка, сквозная проверка по `docs/spec-leads.md` §10.

## Нужно от владельца
- [x] Токен бота от @BotFather.
- [x] Свой Telegram ID (узнать можно у @userinfobot).
- [x] Адрес контент-репозитория проекта «Эзотерика» и путь к папке с постами.
- [x] ~~Fine-grained GitHub PAT~~ не понадобился (deploy key). Fine-grained GitHub PAT: Contents = Read-only для контент-репозиториев. Если агент сам создаёт webhook, нужно ещё Webhooks = Read and write.
- [ ] **v2:** отдельный аккаунт Telegram для юзербота (не личный) и `api_id` / `api_hash` с my.telegram.org под ним.
- [ ] **v2:** ключ ИИ (OpenAI / OpenRouter / DeepSeek) — `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`.
- [ ] **v2:** создать приватный репозиторий `Torrexx52/helping-bot-data` (пустой, с README).
