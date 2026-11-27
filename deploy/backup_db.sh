#!/usr/bin/env bash
# Ежедневная копия базы SlotMarket, хранится 7 дней (B5).
# Нужен sqlite3: sudo apt install sqlite3. Запуск от пользователя
# slotmarket по cron, см. deploy/crontab.example.
set -euo pipefail

DB_PATH="${DB_PATH:-/opt/slotmarket/slotmarket.db}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/slotmarket}"
KEEP_DAYS=7

# Без проверки sqlite3 молча создал бы пустую базу по неверному пути,
# и пустые копии за неделю вытеснили бы настоящие.
if [[ ! -f "$DB_PATH" ]]; then
    echo "$(date '+%F %T') база не найдена: $DB_PATH" >&2
    exit 1
fi

mkdir -p "$BACKUP_DIR"
target="$BACKUP_DIR/slotmarket-$(date +%F).db"

# .backup, а не cp: в режиме WAL копия файла при работающем боте может
# оказаться несогласованной, а .backup делает целостный снимок.
sqlite3 "$DB_PATH" ".backup '$target'"

# Старые копии удаляются только после успешной: при ошибке выше
# set -e остановит скрипт.
find "$BACKUP_DIR" -name 'slotmarket-*.db' -type f \
    -mtime +$((KEEP_DAYS - 1)) -delete

echo "$(date '+%F %T') копия готова: $target"
