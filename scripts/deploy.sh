#!/usr/bin/env bash
# Деплой Helping Bot на сервере: git pull → docker compose up -d --build → проверки.
set -euo pipefail
cd "$(dirname "$0")/.."

test -f .env || { echo "Нет .env (см. .env.example)"; exit 1; }
test -f config/projects.yml || { echo "Нет config/projects.yml (см. config/projects.example.yml)"; exit 1; }
chmod 600 .env
mkdir -p data

git pull --ff-only
# v2: слова и запросы для заявок. Свой config/leads.yml не перезаписывается.
if [ ! -f config/leads.yml ]; then
    cp config/leads.example.yml config/leads.yml
    echo "Создан config/leads.yml из примера"
fi
[ ! -f data/userbot.session ] || chmod 600 data/userbot.session
docker compose up -d --build --remove-orphans
docker image prune -f >/dev/null

echo "Жду healthcheck…"
for _ in $(seq 1 30); do
    if curl -fsS http://127.0.0.1:8081/health >/dev/null 2>&1; then
        docker compose ps
        echo "OK: /health отвечает"
        exit 0
    fi
    sleep 2
done
docker compose ps
docker compose logs --tail=50 bot
echo "ОШИБКА: /health не ответил за 60 секунд"
exit 1
