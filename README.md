# Helping Bot

Личный Telegram-бот, который выдаёт готовые посты для соцсетей из приватных GitHub-репозиториев проектов. Открываешь аккаунт → видишь очередь по 5 постов → выбираешь номер → получаешь медиа, описание и подпись с хэштегами, которая копируется одним нажатием → публикуешь и жмёшь «✅ Опубликовал».

- Требования: [`spec.md`](spec.md)
- Задачи: [`docs/TASKS.md`](docs/TASKS.md)
- Формат постов (отдавать ИИ-агентам): [`docs/CONTENT_FORMAT.md`](docs/CONTENT_FORMAT.md)
- Сервер и деплой: [`docs/SERVER.md`](docs/SERVER.md)
- Правила для агента: [`CLAUDE.md`](CLAUDE.md)

## Подключить новый контент-репозиторий
1. Положить в него `docs/CONTENT_FORMAT.md` и папку `posts/`.
2. На сервере добавить блок в `/opt/helping-bot/config/projects.yml` и секрет в `.env`.
3. Создать webhook в GitHub (см. `docs/SERVER.md`) и выдать GitHub-токену доступ к репозиторию.
4. `docker compose restart bot` или команда `/reload` в боте.

## Деплой
`ssh vibee-root 'cd /opt/helping-bot && ./scripts/deploy.sh'`
