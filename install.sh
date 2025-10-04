#!/usr/bin/env bash
set -euo pipefail

# Installer for ML350p-G8-Fan-controller
# - Installs dynamic_fans.sh
# - Creates /opt/dynamic-fan-ui/ with proper permissions
# - Optionally installs systemd unit and enables service

PREFIX=${PREFIX:-/usr/local}
APP_DIR=${APP_DIR:-/opt/dynamic-fan-ui}
SERVICE_NAME=${SERVICE_NAME:-dynamic-fans.service}
PYTHON=${PYTHON:-python3}

prompt() {
  local q default var
  q="$1"; default="$2"; var="$3"
  local ans
  read -r -p "$q [$default]: " ans || true
  ans=${ans:-$default}
  printf -v "$var" %s "$ans"
}

# Gather settings interactively (env vars act as defaults)
ILO_IP=${ILO_IP:-192.168.1.100}
ILO_USER=${ILO_USER:-admin}
ILO_SSH_KEY=${ILO_SSH_KEY:-/root/.ssh/ilo_key}
ILO_PASSWORD=${ILO_PASSWORD:-}
USE_IPMI_TEMPS=${USE_IPMI_TEMPS:-0}
INSTALL_SYSTEMD=${INSTALL_SYSTEMD:-yes}

echo "\n=== ML350p-G8-Fan-controller Installer ==="
prompt "iLO IP" "$ILO_IP" ILO_IP
prompt "iLO username" "$ILO_USER" ILO_USER
read -r -p "Use SSH key auth? (y/n) [y]: " use_key
use_key=${use_key:-y}
if [[ "$use_key" =~ ^[Yy]$ ]]; then
  prompt "Path to iLO SSH key" "$ILO_SSH_KEY" ILO_SSH_KEY
else
  ILO_SSH_KEY=""
fi
read -r -s -p "iLO password (leave blank to skip password auth): " ILO_PASSWORD
echo ""
read -r -p "Use IPMI for CPU temps (requires ipmitool)? (y/n) [n]: " use_ipmi
use_ipmi=${use_ipmi:-n}
if [[ "$use_ipmi" =~ ^[Yy]$ ]]; then USE_IPMI_TEMPS=1; else USE_IPMI_TEMPS=0; fi
read -r -p "Install and enable systemd service? (y/n) [y]: " sysd
sysd=${sysd:-y}
if [[ "$sysd" =~ ^[Yy]$ ]]; then INSTALL_SYSTEMD=yes; else INSTALL_SYSTEMD=no; fi

create_dirs() {
  echo "Creating directories..."
  sudo mkdir -p "$APP_DIR"
  sudo chown "${USER}:${USER}" "$APP_DIR"
}

install_scripts() {
  echo "Installing dynamic_fans.sh to $PREFIX/bin..."
  sudo install -m 0755 dynamic_fans.sh "$PREFIX/bin/dynamic_fans.sh"

  echo "Creating initial files in $APP_DIR..."
  [[ -f "$APP_DIR/fan_curve.json" ]] || cat > "$APP_DIR/fan_curve.json" <<EOF
{
  "minTemp": 30,
  "maxTemp": 80,
  "minSpeed": 20,
  "maxSpeed": 100
}
EOF
  [[ -f "$APP_DIR/fan_speeds.txt" ]] || : > "$APP_DIR/fan_speeds.txt"
}

install_systemd() {
  if ! command -v systemctl >/dev/null 2>&1; then
    echo "systemd not detected. Skipping service installation."; return 0
  fi
  echo "Installing systemd unit..."
  sudo install -m 0644 systemd/$SERVICE_NAME \
    	/etc/systemd/system/$SERVICE_NAME

  # Environment overrides file
  sudo mkdir -p /etc/systemd/system/$SERVICE_NAME.d
  sudo bash -c "cat > /etc/systemd/system/$SERVICE_NAME.d/override.conf" <<EOF
[Service]
Environment=ILO_IP=$ILO_IP
Environment=ILO_USER=$ILO_USER
Environment=ILO_SSH_KEY=$ILO_SSH_KEY
Environment=ILO_PASSWORD=$ILO_PASSWORD
Environment=FAN_CURVE_FILE=$APP_DIR/fan_curve.json
Environment=FAN_SPEED_FILE=$APP_DIR/fan_speeds.txt
Environment=CHECK_INTERVAL=5
Environment=MAX_STEP=10
Environment=USE_IPMI_TEMPS=$USE_IPMI_TEMPS
EOF

  sudo systemctl daemon-reload
  sudo systemctl enable $SERVICE_NAME
  echo "To start now: sudo systemctl start $SERVICE_NAME"
}

detect_pkg_mgr() {
  if command -v apt-get >/dev/null 2>&1; then echo apt; return; fi
  if command -v dnf >/dev/null 2>&1; then echo dnf; return; fi
  if command -v yum >/dev/null 2>&1; then echo yum; return; fi
  if command -v zypper >/dev/null 2>&1; then echo zypper; return; fi
  if command -v pacman >/dev/null 2>&1; then echo pacman; return; fi
  if command -v apk >/dev/null 2>&1; then echo apk; return; fi
  echo unknown
}

install_prereqs() {
  echo "Checking prerequisites..."
  local need_jq=0 need_sshpass=0 need_ipmitool=0
  command -v jq >/dev/null 2>&1 || need_jq=1
  if [[ -n "$ILO_PASSWORD" ]]; then
    command -v sshpass >/dev/null 2>&1 || need_sshpass=1
  fi
  if [[ "$USE_IPMI_TEMPS" == "1" ]]; then
    command -v ipmitool >/dev/null 2>&1 || need_ipmitool=1
  fi

  local mgr
  mgr=$(detect_pkg_mgr)
  if [[ "$mgr" == "unknown" ]]; then
    echo "Couldn't detect package manager. Please ensure the following are installed:"
    ((need_jq)) && echo " - jq"
    ((need_sshpass)) && echo " - sshpass (required for password-based iLO auth)"
    ((need_ipmitool)) && echo " - ipmitool (required for IPMI temps)"
  else
    # Build install list
    local pkgs=()
    ((need_jq)) && pkgs+=(jq)
    ((need_sshpass)) && pkgs+=(sshpass)
    ((need_ipmitool)) && pkgs+=(ipmitool)
    if (( ${#pkgs[@]} > 0 )); then
      echo "Installing packages: ${pkgs[*]} (via $mgr)"
      case "$mgr" in
        apt)
          sudo apt-get update -y
          sudo apt-get install -y "${pkgs[@]}"
          ;;
        dnf)
          sudo dnf install -y "${pkgs[@]}"
          ;;
        yum)
          sudo yum install -y "${pkgs[@]}"
          ;;
        zypper)
          sudo zypper install -y "${pkgs[@]}"
          ;;
        pacman)
          sudo pacman -Sy --noconfirm "${pkgs[@]}"
          ;;
        apk)
          sudo apk add --no-cache "${pkgs[@]}"
          ;;
      esac
    else
      echo "All required packages already installed."
    fi
  fi

  if ! command -v nvidia-smi >/dev/null 2>&1; then
    echo "Warning: nvidia-smi not found. GPU temperature will be unavailable."
  fi
}

print_next_steps() {
  cat <<MSG
Install complete.
- Edit /etc/systemd/system/$SERVICE_NAME.d/override.conf to adjust ILO_* and thresholds.
- Ensure iLO access works: either provide a valid key at ILO_SSH_KEY or set ILO_PASSWORD and install sshpass.
- Run the Flask app for UI: $PYTHON app.py (or deploy with your WSGI server)
- Start the control loop: sudo systemctl start $SERVICE_NAME
MSG
}

main() {
  create_dirs
  install_prereqs
  install_scripts
  if [[ "$INSTALL_SYSTEMD" == "yes" ]]; then
    install_systemd
  else
    echo "Skipping systemd installation as requested."
  fi
  print_next_steps
}

main "$@"
