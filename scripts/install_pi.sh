#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-$HOME/controller/loggerhead}"
REPO_URL="${REPO_URL:-https://github.com/RealBlueSky227/loggerhead.git}"
SERVICE_NAME="${SERVICE_NAME:-loggerhead.service}"

if [[ "$(id -un)" != "reef" ]]; then
  echo "Run this as the reef user so files land under /home/reef." >&2
  exit 1
fi

if command -v python3.13 >/dev/null 2>&1; then
  PYTHON_BIN="python3.13"
else
  echo "python3.13 was not found. Loggerhead targets Python 3.13 per the SRS." >&2
  echo "Install Python 3.13 for Raspberry Pi OS, then rerun this script." >&2
  exit 1
fi

sudo apt-get update
sudo apt-get install -y \
  git \
  i2c-tools \
  pigpio \
  python3-pigpio \
  python3-smbus \
  python3-venv \
  raspi-config

sudo systemctl enable --now pigpiod

mkdir -p "$(dirname "$APP_DIR")"
if [[ -d "$APP_DIR/.git" ]]; then
  git -C "$APP_DIR" pull --ff-only
else
  git clone "$REPO_URL" "$APP_DIR"
fi

cd "$APP_DIR"
"$PYTHON_BIN" -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip wheel
python -m pip install -r requirements.txt

mkdir -p config data
python -m loggerhead --config config/loggerhead.json --data-dir data --simulation --host 127.0.0.1 --port 18080 &
BOOT_PID=$!
sleep 3
kill "$BOOT_PID" >/dev/null 2>&1 || true
wait "$BOOT_PID" >/dev/null 2>&1 || true

sudo cp systemd/loggerhead.service "/etc/systemd/system/$SERVICE_NAME"
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"

cat <<EOF
Loggerhead is installed at:
  $APP_DIR

Review generated config before starting live hardware:
  nano $APP_DIR/config/loggerhead.json

Start and inspect the service:
  sudo systemctl start $SERVICE_NAME
  sudo systemctl status $SERVICE_NAME
  journalctl -u $SERVICE_NAME -f

Dashboard:
  http://$(hostname -I | awk '{print $1}'):8080
EOF
