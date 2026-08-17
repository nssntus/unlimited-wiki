#!/bin/sh
set -eu

PROJECT_ROOT=${WIKI_PROJECT_ROOT:-/opt/unlimited-wiki}
BACKUP_ROOT=${WIKI_BACKUP_ROOT:?WIKI_BACKUP_ROOT is required}
SERVICE_USER=${WIKI_SERVICE_USER:-unlimited-wiki}
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
DESTINATION="$BACKUP_ROOT/wiki-$STAMP"

install -d -m 700 -o "$SERVICE_USER" -g "$SERVICE_USER" "$BACKUP_ROOT"

stopped=0
restart_service() {
    if [ "$stopped" -eq 1 ]; then
        systemctl start unlimited-wiki.service
        stopped=0
    fi
}

trap restart_service EXIT
trap 'exit 130' INT TERM HUP
stopped=1
systemctl stop unlimited-wiki.service
runuser -u "$SERVICE_USER" -- "$PROJECT_ROOT/.venv/bin/python" "$PROJECT_ROOT/backup_restore.py" backup \
    --project-root "$PROJECT_ROOT" --output "$DESTINATION"
runuser -u "$SERVICE_USER" -- "$PROJECT_ROOT/.venv/bin/python" "$PROJECT_ROOT/backup_restore.py" verify "$DESTINATION"
