#!/usr/bin/env bash
# readsbstats — first-time installation script.
# Run as root directly on the Raspberry Pi:
#
#   bash install.sh
#
# Source files are expected in the parent directory of this script
# (i.e. project root, wherever you rsynced from your Mac).

set -euo pipefail
trap 'echo "ERROR: install failed at line $LINENO (exit $?)" >&2' ERR

APP_DIR="/opt/readsbstats"
DATA_DIR="/mnt/ext/readsbstats"
SRC_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SERVICE_USER="readsbstats"
AIRCRAFT_JSON="/run/readsb/aircraft.json"
PYTHON="python3"

echo "=== readsbstats installer ==="
echo "    Source : $SRC_DIR"
echo "    App    : $APP_DIR"
echo "    Data   : $DATA_DIR"
echo ""

# ---- Must be root ------------------------------------------------------------
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

# ---- Python 3.10+ ------------------------------------------------------------
PY_VERSION=$($PYTHON -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null || echo "0.0")
PY_MAJOR=$(echo "$PY_VERSION" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VERSION" | cut -d. -f2)
if [[ $PY_MAJOR -lt 3 || ($PY_MAJOR -eq 3 && $PY_MINOR -lt 10) ]]; then
  echo "ERROR: Python 3.10+ required (found $PY_VERSION)" >&2
  exit 1
fi
echo "Python $PY_VERSION — OK"

# ---- System user -------------------------------------------------------------
if ! id "$SERVICE_USER" &>/dev/null; then
  useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
  echo "Created system user: $SERVICE_USER"
else
  echo "User $SERVICE_USER already exists"
fi

# ---- Directories -------------------------------------------------------------
mkdir -p "$APP_DIR" "$DATA_DIR"
chown "$SERVICE_USER:$SERVICE_USER" "$DATA_DIR"
echo "Directories ready"

# ---- Copy application files --------------------------------------------------
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
echo "Application files copied"

# ---- Python virtualenv -------------------------------------------------------
if [[ ! -f "$APP_DIR/venv/bin/activate" ]]; then
  echo "Creating Python virtualenv…"
  $PYTHON -m venv "$APP_DIR/venv"
fi
"$APP_DIR/venv/bin/pip" install -q --upgrade pip
"$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"
"$APP_DIR/venv/bin/pip" install -q -e "$APP_DIR"
echo "Python dependencies installed"

# ---- Read access to /run/readsb/ ---------------------------------------------
if getent group readsb &>/dev/null; then
  usermod -aG readsb "$SERVICE_USER"
  echo "Added $SERVICE_USER to group 'readsb'"
elif command -v setfacl &>/dev/null && [[ -e "$AIRCRAFT_JSON" ]]; then
  setfacl -m "u:${SERVICE_USER}:r" "$AIRCRAFT_JSON"
  echo "Set ACL read permission on $AIRCRAFT_JSON"
else
  echo "WARNING: could not grant read access to $AIRCRAFT_JSON"
  echo "         Fix manually after install — see README"
fi

# ---- Systemd services --------------------------------------------------------
cp "$APP_DIR/systemd/readsbstats-collector.service" /etc/systemd/system/
cp "$APP_DIR/systemd/readsbstats-web.service"       /etc/systemd/system/
cp "$APP_DIR/systemd/readsbstats-updater.service"   /etc/systemd/system/
cp "$APP_DIR/systemd/readsbstats-updater.timer"     /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now readsbstats-collector.service
systemctl enable --now readsbstats-web.service
systemctl enable --now readsbstats-updater.timer
echo "Services enabled and started"

# ---- nginx -------------------------------------------------------------------
echo "nginx proxy config: $APP_DIR/nginx-readsbstats.conf"
if ! grep -qr "readsbstats" /etc/nginx/ 2>/dev/null; then
  echo "Add to your nginx server {} block:"
  echo "  include /opt/readsbstats/nginx-readsbstats.conf;"
fi

# ---- Initial database download -----------------------------------------------
echo ""
echo "Downloading aircraft & airline databases (~30 s)…"
runuser -u "$SERVICE_USER" -- "$APP_DIR/venv/bin/python" -m readsbstats.db_updater \
  && echo "Database downloaded and imported" \
  || echo "WARNING: db_updater failed — run manually: bash scripts/update.sh --db-only"

# ---- Done --------------------------------------------------------------------
LAN_IP=$(hostname -I | awk '{print $1}')
echo ""
echo "=== Installation complete ==="
echo ""
echo "  Web UI   http://${LAN_IP}/stats/"
echo ""
echo "  Logs     journalctl -u readsbstats-collector -f"
echo "           journalctl -u readsbstats-web -f"
echo ""
echo "  Update   bash ${SRC_DIR}/scripts/update.sh"
