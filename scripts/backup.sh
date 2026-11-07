#!/usr/bin/env bash
# Ежедневная копия SQLite (cron), хранится 14 дней.
set -euo pipefail
SRC=/opt/helping-bot/data/bot.db
DST=/var/backups/helping-bot
mkdir -p "$DST"
chmod 700 "$DST"
[ -f "$SRC" ] || exit 0
OUT="$DST/bot-$(date +%F).db"
sqlite3 "$SRC" ".backup '$OUT'"
sqlite3 "$OUT" "PRAGMA integrity_check;" | grep -qx ok
chmod 600 "$OUT"
find "$DST" -name 'bot-*.db' -type f -mtime +14 -delete
