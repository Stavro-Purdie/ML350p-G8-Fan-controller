#!/usr/bin/env bash
# Uninstall Dynamic Fan UI and daemon (no package removals)
set -euo pipefail

confirm="yes"
keep_data="no"

usage() {
  cat <<'EOF'
Usage: ./uninstall.sh [-y] [--keep-data]
  -y            Non-interactive (assume yes)
  --keep-data   Keep /opt/dynamic-fan-ui data files (fan_curve.json, fan_speeds.txt)

This script:
  - Stops and disables systemd services (dynamic-fans.service, dynamic-fan-ui.service)
  - Removes systemd unit files and drop-ins
  - Removes installed app files under /opt/dynamic-fan-ui/app and /usr/local/bin/dynamic_fans.sh
  - Does NOT remove any OS packages (APT/YUM/etc.)
EOF
}

# Parse args
while [[ $# -gt 0 ]]; do
  case "$1" in
    -y) confirm="no"; shift ;;
    --keep-data) keep_data="yes"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage; exit 1 ;;
  esac
done

# Elevate if needed
if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  echo "Re-running with sudo..."
  exec sudo --preserve-env=PATH "$0" "$@"
fi

# Prompt
if [[ "$confirm" == "yes" ]]; then
  read -r -p "Remove Dynamic Fan UI services and files (no packages)? [y/N]: " ans
  case "${ans,,}" in
    y|yes) ;;
    *) echo "Aborted."; exit 0 ;;
  esac
fi

have_systemctl() { command -v systemctl >/dev/null 2>&1; }

stop_disable_service() {
  local svc="$1"
  systemctl stop "$svc" 2>/dev/null || true
  systemctl disable "$svc" 2>/dev/null || true
  systemctl reset-failed "$svc" 2>/dev/null || true
}

remove_unit() {
  local name="$1"
  rm -f "/etc/systemd/system/${name}" 2>/dev/null || true
  rm -rf "/etc/systemd/system/${name}.d" 2>/dev/null || true
  rm -f "/etc/systemd/system/multi-user.target.wants/${name}" 2>/dev/null || true
}

# Stop and remove services
if have_systemctl; then
  echo "Stopping services..."
  stop_disable_service "dynamic-fans.service"
  stop_disable_service "dynamic-fan-ui.service"
  echo "Removing unit files..."
  remove_unit "dynamic-fans.service"
  remove_unit "dynamic-fan-ui.service"
  systemctl daemon-reload
else
  echo "systemctl not found; skipping service stop/remove."
fi

# Remove installed files
BIN="/usr/local/bin/dynamic_fans.sh"
APP_DIR="/opt/dynamic-fan-ui/app"
DATA_DIR="/opt/dynamic-fan-ui"
CURVE="${DATA_DIR}/fan_curve.json"
SPEEDS="${DATA_DIR}/fan_speeds.txt"

echo "Removing installed files..."
rm -f "$BIN" 2>/dev/null || true
rm -rf "$APP_DIR" 2>/dev/null || true

if [[ "$keep_data" == "no" ]]; then
  rm -f "$CURVE" "$SPEEDS" 2>/dev/null || true
  rmdir "$DATA_DIR" 2>/dev/null || true
else
  echo "Keeping data in ${DATA_DIR}"
fi

echo "Uninstall completed."
