#!/usr/bin/env bash
# Ежедневная копия SQLite (cron), хранится 14 дней.
# Использует системный python3 (модуль sqlite3) — отдельный пакет sqlite3 не нужен.
set -euo pipefail
SRC=${SRC:-/opt/helping-bot/data/bot.db}
DST=${DST:-/var/backups/helping-bot}
KEEP_DAYS=${KEEP_DAYS:-14}

mkdir -p "$DST"
chmod 700 "$DST"
[ -f "$SRC" ] || { echo "нет $SRC — пропуск"; exit 0; }
OUT="$DST/bot-$(date +%F).db"

python3 - "$SRC" "$OUT" <<'PY'
import sqlite3
import sys

src, out = sys.argv[1], sys.argv[2]
with sqlite3.connect(f"file:{src}?mode=ro", uri=True) as s, sqlite3.connect(out) as d:
    s.backup(d)
    ok = d.execute("PRAGMA integrity_check").fetchone()[0]
if ok != "ok":
    sys.exit(f"integrity_check: {ok}")
PY
chmod 600 "$OUT"
find "$DST" -name 'bot-*.db' -type f -mtime +"$KEEP_DAYS" -delete
echo "$(date -Is) backup ok: $OUT"
