# Сервер для Helping Bot

## Хост

VPS №2223763 · Франкфурт · 1 vCPU / 2 ГБ RAM / 30 ГБ NVMe · Debian 12.15 · часовой пояс `Europe/Moscow`.
IP `2.26.64.167`, имя `vm2223763.vds.as210546.net`. На нём уже работает Cargo-сайт `https://vibee.ddns.net/` (Nginx + PHP-FPM + MariaDB). Docker CE 29 установлен, контейнеров нет. UFW открывает 22, 80 и 443; Fail2Ban с jail `sshd` активен.

Оплачен до 21.11.2026, автопродление включено.

## Вход (с Windows-машины владельца)

Алиас уже настроен в `%USERPROFILE%\.ssh\config`:

```sshconfig
Host vibee-root
    HostName 2.26.64.167
    User root
    Port 22
    IdentityFile ~/.ssh/vibee_vps
    IdentitiesOnly yes
```

Проверка:
```powershell
ssh -o BatchMode=yes -o StrictHostKeyChecking=yes vibee-root 'id -u; hostname -f'
# ожидается: 0 и vm2223763.vds.as210546.net
```

- Аутентификация только по ключу, пароли отключены.
- Отпечаток ключа клиента: `SHA256:g2jBm5v8HkdgsDVXoGI1sVOILhcsBjNFVrudQaXN2+I`.
- Ключ хоста ED25519: `SHA256:AgsLssHaEm+vLhR4zT7uuHMO8ABkOBA8fO00jpPLCN0`. При первом подключении из новой среды сверь его.
- Если агент работает не на этой машине, его публичный ключ нужно добавить в `/root/.ssh/authorized_keys` через доверенный канал. Приватные ключи в репозиторий не кладутся.

## Размещение Helping Bot

```
/opt/helping-bot/                 # клон этого репозитория (deploy key, read-only)
/opt/helping-bot/.env             # секреты, chmod 600
/opt/helping-bot/config/projects.yml
/opt/helping-bot/data/            # SQLite + клоны контент-репозиториев
/var/backups/helping-bot/         # ежедневные копии SQLite, 14 дней
```

Контейнер публикует только `127.0.0.1:8081`, наружу порты не открываются, UFW не меняется.

## Nginx: webhook GitHub

В `/etc/nginx/sites-enabled/cargo`, внутри `server { listen 443 ... }`, добавить:

```nginx
location = /hooks/helping-bot/github {
    proxy_pass http://127.0.0.1:8081;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    client_max_body_size 5m;
}
```

Порядок: `cp` конфига в `/root/nginx-backups/cargo.<дата>` → правка → `nginx -t` → `systemctl reload nginx` → `curl -sI https://vibee.ddns.net/` (200) → `curl -s -o /dev/null -w '%{http_code}' -X POST https://vibee.ddns.net/hooks/helping-bot/github` (ожидается 401 или 403 без подписи).

## Webhook в GitHub (на каждый контент-репозиторий)

Settings → Webhooks → Add webhook:
- Payload URL: `https://vibee.ddns.net/hooks/helping-bot/github`
- Content type: `application/json`
- Secret: значение переменной из `.env` (например `GH_SECRET_ESOTERIC`)
- Events: только `push`

Можно создать через `gh api`, если у владельца есть `gh` с нужными правами.

## Полезные команды

```bash
cd /opt/helping-bot && ./scripts/deploy.sh
docker compose -f /opt/helping-bot/docker-compose.yml logs -f --tail=100
curl -s 127.0.0.1:8081/health
systemctl is-active nginx php8.2-fpm mariadb
```
