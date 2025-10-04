#!/usr/bin/env bash
set -euo pipefail

# Installer for ML350p-G8-Fan-controller
# - Installs dynamic_fans.sh
# - Creates /opt/dynamic-fan-ui/ with proper permissions
# - Optionally installs systemd unit and enables service

PREFIX=${PREFIX:-/usr/local}
APP_DIR=${APP_DIR:-/opt/dynamic-fan-ui}
SERVICE_NAME=${SERVICE_NAME:-dynamic-fans.service}
UI_SERVICE_NAME=${UI_SERVICE_NAME:-dynamic-fan-ui.service}
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
ILO_SSH_LEGACY=${ILO_SSH_LEGACY:-0}
START_NOW=${START_NOW:-yes}

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
read -r -p "Enable legacy SSH algorithms for older iLO (fixes KEX error)? (y/n) [y]: " legacy
legacy=${legacy:-y}
if [[ "$legacy" =~ ^[Yy]$ ]]; then ILO_SSH_LEGACY=1; else ILO_SSH_LEGACY=0; fi
read -r -p "Install and enable systemd service? (y/n) [y]: " sysd
sysd=${sysd:-y}
if [[ "$sysd" =~ ^[Yy]$ ]]; then INSTALL_SYSTEMD=yes; else INSTALL_SYSTEMD=no; fi
read -r -p "Start services now after install? (y/n) [y]: " start_now
start_now=${start_now:-y}
if [[ "$start_now" =~ ^[Yy]$ ]]; then START_NOW=yes; else START_NOW=no; fi

create_dirs() {
  echo "Creating directories..."
  sudo mkdir -p "$APP_DIR"
  sudo chown "${USER}:${USER}" "$APP_DIR"
  sudo mkdir -p "$APP_DIR/app"
  sudo chown -R "${USER}:${USER}" "$APP_DIR/app"
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

install_ui() {
  echo "Installing Flask UI to $APP_DIR/app..."
  rsync -a --delete app.py "$APP_DIR/app/" 2>/dev/null || cp -f app.py "$APP_DIR/app/"
  mkdir -p "$APP_DIR/app/static" "$APP_DIR/app/templates"
  rsync -a static/ "$APP_DIR/app/static/" 2>/dev/null || cp -R static/* "$APP_DIR/app/static/" 2>/dev/null || true
  rsync -a templates/ "$APP_DIR/app/templates/" 2>/dev/null || cp -R templates/* "$APP_DIR/app/templates/" 2>/dev/null || true

  echo "Checking Python/Flask availability..."
  if ! command -v $PYTHON >/dev/null 2>&1; then
    echo "$PYTHON not found; attempting to install Python via package manager."
    local mgr; mgr=$(detect_pkg_mgr)
    case "$mgr" in
      apt) sudo apt-get update -y && sudo apt-get install -y python3 python3-pip ;;
      dnf) sudo dnf install -y python3 python3-pip ;;
      yum) sudo yum install -y python3 python3-pip ;;
      zypper) sudo zypper install -y python3 python3-pip ;;
      pacman) sudo pacman -Sy --noconfirm python python-pip ;;
      apk) sudo apk add --no-cache python3 py3-pip ;;
      *) echo "Please install Python 3 and pip manually." ;;
    esac
  fi
  if ! $PYTHON -c "import flask" >/dev/null 2>&1; then
    echo "Installing Flask via pip..."
    if command -v pip3 >/dev/null 2>&1; then
      sudo -H pip3 install Flask || echo "Warning: failed to install Flask automatically. Install it manually (pip3 install Flask)."
    else
      echo "pip3 not found. Please install Flask manually (pip3 install Flask)."
    fi
  fi
}

install_systemd() {
  if ! command -v systemctl >/dev/null 2>&1; then
    echo "systemd not detected. Skipping service installation."; return 0
  fi
  echo "Installing systemd unit..."
  sudo install -m 0644 systemd/$SERVICE_NAME \
    	/etc/systemd/system/$SERVICE_NAME

  echo "Installing UI systemd unit..."
  sudo install -m 0644 systemd/$UI_SERVICE_NAME \
    	/etc/systemd/system/$UI_SERVICE_NAME

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
Environment=ILO_SSH_LEGACY=$ILO_SSH_LEGACY
EOF

  # UI overrides
  sudo mkdir -p /etc/systemd/system/$UI_SERVICE_NAME.d
  sudo bash -c "cat > /etc/systemd/system/$UI_SERVICE_NAME.d/override.conf" <<EOF
[Service]
Environment=FAN_CURVE_FILE=$APP_DIR/fan_curve.json
Environment=FAN_SPEED_FILE=$APP_DIR/fan_speeds.txt
Environment=ILO_IP=$ILO_IP
Environment=ILO_USER=$ILO_USER
Environment=ILO_SSH_KEY=$ILO_SSH_KEY
Environment=ILO_PASSWORD=$ILO_PASSWORD
Environment=USE_IPMI_TEMPS=$USE_IPMI_TEMPS
Environment=ILO_SSH_LEGACY=$ILO_SSH_LEGACY
WorkingDirectory=$APP_DIR/app
EOF

  sudo systemctl daemon-reload
  sudo systemctl enable $SERVICE_NAME
  sudo systemctl enable $UI_SERVICE_NAME
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

connectivity_check() {
  echo "Testing iLO SSH connectivity..."
  local ssh_base=(ssh -o StrictHostKeyChecking=no -o BatchMode=yes -o ConnectTimeout=3 -o ConnectionAttempts=1)
  if [[ -n "$ILO_SSH_KEY" && -f "$ILO_SSH_KEY" ]]; then
    ssh_base+=( -i "$ILO_SSH_KEY" )
  fi
  local cmd=("${ssh_base[@]}" "$ILO_USER@$ILO_IP" echo ok)
  if [[ -n "$ILO_PASSWORD" && $(command -v sshpass) ]]; then
    cmd=(sshpass -p "$ILO_PASSWORD" "${ssh_base[@]}" "$ILO_USER@$ILO_IP" echo ok)
  fi
  set +e
  local out rc
  out=$("${cmd[@]}" 2>/dev/null)
  rc=$?
  set -e
  if [[ $rc -ne 0 || "$out" != "ok" ]]; then
    echo "Modern SSH failed; retrying with legacy algorithms..."
    ssh_base+=(
      -o KexAlgorithms=+diffie-hellman-group14-sha1 \
      -o HostKeyAlgorithms=+ssh-rsa \
      -o PubkeyAcceptedAlgorithms=+ssh-rsa \
      -o PubkeyAcceptedKeyTypes=+ssh-rsa \
      -o Ciphers=+aes128-cbc
    )
    cmd=("${ssh_base[@]}" "$ILO_USER@$ILO_IP" echo ok)
    if [[ -n "$ILO_PASSWORD" && $(command -v sshpass) ]]; then
      cmd=(sshpass -p "$ILO_PASSWORD" "${ssh_base[@]}" "$ILO_USER@$ILO_IP" echo ok)
    fi
    set +e
    out=$("${cmd[@]}" 2>/dev/null)
    rc=$?
    set -e
    if [[ $rc -eq 0 && "$out" == "ok" ]]; then
      echo "Legacy SSH works. Enabling ILO_SSH_LEGACY=1."
      ILO_SSH_LEGACY=1
    else
      echo "Warning: Unable to reach iLO via SSH. Verify IP/credentials and network."
    fi
  else
    echo "iLO SSH connectivity OK."
  fi
}

start_services() {
  if ! command -v systemctl >/dev/null 2>&1; then return 0; fi
  echo "Starting services..."
  sudo systemctl start $SERVICE_NAME || true
  sudo systemctl start $UI_SERVICE_NAME || true
  echo "Service status (first lines):"
  systemctl --no-pager --full status $SERVICE_NAME | sed -n '1,12p' || true
  systemctl --no-pager --full status $UI_SERVICE_NAME | sed -n '1,12p' || true
}

main() {
  create_dirs
  install_prereqs
  connectivity_check
  install_scripts
  install_ui
  if [[ "$INSTALL_SYSTEMD" == "yes" ]]; then
    install_systemd
    if [[ "$START_NOW" == "yes" ]]; then
      start_services
    else
      echo "Skipping auto-start as requested."
    fi
  else
    echo "Skipping systemd installation as requested."
  fi
  print_next_steps
}

main "$@"
