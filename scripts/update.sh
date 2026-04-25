#!/usr/bin/env bash
# readsbstats — update script.
# Run as root directly on the Raspberry Pi after syncing source from your Mac:
#
#   bash update.sh           # sync code + restart services
#   bash update.sh --db-only # sync code + re-download aircraft/airline database (no restart)
#   bash update.sh --full    # sync code + restart services + re-download database

set -euo pipefail
trap 'echo "ERROR: update failed at line $LINENO (exit $?)" >&2' ERR

APP_DIR="/opt/readsbstats"
SRC_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SERVICE_USER="readsbstats"

MODE="code"
if [[ "${1:-}" == "--db-only" ]]; then MODE="db"; fi
if [[ "${1:-}" == "--full"    ]]; then MODE="full"; fi

if [[ $EUID -ne 0 ]]; then
  echo "ERROR: run as root" >&2
  exit 1
fi

# ---- Validate source directory -----------------------------------------------
if [[ ! -f "$SRC_DIR/src/readsbstats/collector.py" || ! -f "$SRC_DIR/src/readsbstats/web.py" ]]; then
  echo "ERROR: $SRC_DIR does not look like the readsbstats source tree" >&2
  echo "       (missing src/readsbstats/collector.py or web.py)" >&2
  exit 1
fi

DB_FILE="/mnt/ext/readsbstats/history.db"

# ---- Backup database before any changes --------------------------------------
if [[ -f "$DB_FILE" ]]; then
  BACKUP="$DB_FILE.backup.$(date +%Y%m%d_%H%M%S)"
  echo "==> Backing up database → $BACKUP"
  cp "$DB_FILE" "$BACKUP"
  # Keep only the 3 most recent backups
  ls -1t "$DB_FILE".backup.* 2>/dev/null | tail -n +4 | xargs -r rm --
fi

# ---- Always sync code --------------------------------------------------------
echo "==> Syncing code: $SRC_DIR → $APP_DIR"
rsync -a --delete \
  --exclude='.git' \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  --exclude='venv' \
  --exclude='docs' \
  --exclude='*.db' \
  --exclude='*.db-wal' \
  --exclude='*.db-shm' \
  "$SRC_DIR/" "$APP_DIR/"
chown -R root:"$SERVICE_USER" "$APP_DIR"
chmod -R u=rwX,g=rX,o= "$APP_DIR"

echo "==> Installing Python dependencies"
"$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"
"$APP_DIR/venv/bin/pip" install -q -e "$APP_DIR"

echo "==> Reloading systemd"
cp "$APP_DIR/systemd/readsbstats-collector.service" /etc/systemd/system/
cp "$APP_DIR/systemd/readsbstats-web.service"       /etc/systemd/system/
cp "$APP_DIR/systemd/readsbstats-updater.service"   /etc/systemd/system/
cp "$APP_DIR/systemd/readsbstats-updater.timer"     /etc/systemd/system/
systemctl daemon-reload

# ---- Restart services (code + full mode) -------------------------------------
if [[ "$MODE" == "code" || "$MODE" == "full" ]]; then
  echo "==> Restarting services"
  systemctl restart readsbstats-collector readsbstats-web
  sleep 2
  echo "==> Service status"
  systemctl is-active readsbstats-collector readsbstats-web
fi

# ---- Update aircraft/airline database (db + full mode) ----------------------
# Stop the collector first — db_updater does a full DELETE + 620k-row re-insert
# of aircraft_db inside a single transaction.  Running that concurrently with the
# collector (which writes positions every 5 s) causes "database is locked" errors.
# The web server can stay up: it only reads and WAL mode allows concurrent reads.
if [[ "$MODE" == "db" || "$MODE" == "full" ]]; then
  echo "==> Stopping collector for database update"
  systemctl stop readsbstats-collector
  echo "==> Updating aircraft & airline databases"
  runuser -u "$SERVICE_USER" -- "$APP_DIR/venv/bin/python" -m readsbstats.db_updater
  echo "==> Starting collector"
  systemctl start readsbstats-collector
fi

echo ""
echo "Done."
echo "  Logs:  journalctl -u readsbstats-collector -n 20"
echo "         journalctl -u readsbstats-web -n 20"
