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

# ---- Validate frontend build freshness (if frontend/ exists) ----------------
# The React SPA is built on the dev machine; the Pi never installs Node. We
# ship the dist/ tree via rsync. Without a freshness check, "git pull && bash
# update.sh" silently ships yesterday's dist whenever the developer forgot to
# rebuild. Compare mtimes of frontend/src + package-lock.json against
# frontend/dist/index.html and abort if anything's newer.
FRONTEND_DIR="$SRC_DIR/frontend"
if [[ -d "$FRONTEND_DIR/src" ]]; then
  if [[ ! -f "$FRONTEND_DIR/dist/index.html" ]]; then
    echo "ERROR: $FRONTEND_DIR/dist not built — run 'cd frontend && npm run build' first" >&2
    exit 1
  fi
  if [[ -n "$(find "$FRONTEND_DIR/src" -newer "$FRONTEND_DIR/dist/index.html" -print -quit 2>/dev/null)" ]]; then
    echo "ERROR: frontend/src has changes newer than frontend/dist — rebuild first:" >&2
    echo "       cd frontend && npm run build" >&2
    exit 1
  fi
  if [[ -f "$FRONTEND_DIR/package-lock.json" \
     && "$FRONTEND_DIR/package-lock.json" -nt "$FRONTEND_DIR/dist/index.html" ]]; then
    echo "ERROR: frontend/package-lock.json is newer than frontend/dist — reinstall + rebuild:" >&2
    echo "       cd frontend && npm ci && npm run build" >&2
    exit 1
  fi
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
# The frontend dist/ is excluded from the main rsync and deployed via an
# atomic swap below: rsync to dist.new/, then mv into place. This avoids
# a window where index.html references hashed assets that haven't synced yet.
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
  --exclude='frontend/node_modules' \
  --exclude='frontend/.vite' \
  --exclude='frontend/coverage' \
  --exclude='frontend/dist' \
  "$SRC_DIR/" "$APP_DIR/"

# Atomic frontend dist deploy (only when frontend/dist exists on the source).
if [[ -d "$FRONTEND_DIR/dist" ]]; then
  echo "==> Syncing frontend/dist → $APP_DIR/frontend/dist (atomic swap)"
  mkdir -p "$APP_DIR/frontend"
  rsync -a --delete "$FRONTEND_DIR/dist/" "$APP_DIR/frontend/dist.new/"
  # Atomic swap: rename old → .old, rename new → dist, remove .old. The
  # window where dist is missing is bounded by two renames (microseconds);
  # the web mount falls back to 503 not crash if a request hits inside it.
  if [[ -d "$APP_DIR/frontend/dist" ]]; then
    rm -rf "$APP_DIR/frontend/dist.old"
    mv "$APP_DIR/frontend/dist" "$APP_DIR/frontend/dist.old"
  fi
  mv "$APP_DIR/frontend/dist.new" "$APP_DIR/frontend/dist"
  rm -rf "$APP_DIR/frontend/dist.old"
fi

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
cp "$APP_DIR/systemd/notify-telegram@.service"      /etc/systemd/system/
systemctl daemon-reload

# ---- Reload nginx if the proxy config shipped in this sync ------------------
# The repo carries nginx-readsbstats.conf — if you keep it included from your
# site config (recommended), this picks up changes (asset cache, security
# headers) without manual intervention. `nginx -t` validates syntax first;
# only on success does the reload run.
if [[ -f "$APP_DIR/nginx-readsbstats.conf" ]] && command -v nginx >/dev/null 2>&1; then
  echo "==> Reloading nginx (validate then reload)"
  if nginx -t 2>/dev/null; then
    systemctl reload nginx
  else
    echo "WARNING: nginx config failed validation; not reloading. Run 'sudo nginx -t' to diagnose." >&2
  fi
fi

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
