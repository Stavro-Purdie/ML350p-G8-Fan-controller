#!/bin/bash
# Dynamic fan controller for HP Gen8 + Tesla P40 with persistent fan speeds
# Saves last speeds to /opt/dynamic-fan-ui/fan_speeds.txt

# Configurable via env vars; sensible defaults preserved
ILO_IP="${ILO_IP:-192.168.1.100}"
ILO_USER="${ILO_USER:-admin}"
# If set and file exists, will be used with ssh -i; if empty, password or ssh config may be used
ILO_SSH_KEY="${ILO_SSH_KEY:-/root/.ssh/ilo_key}"
# Optional password auth (requires sshpass); leave empty to avoid using it
ILO_PASSWORD="${ILO_PASSWORD:-}"
USE_IPMI_TEMPS="${USE_IPMI_TEMPS:-0}"
# Enable legacy SSH algorithms for older iLO (OpenSSH 8.8+ compatibility)
ILO_SSH_LEGACY="${ILO_SSH_LEGACY:-0}"

FAN_IDS=("fan1" "fan2" "fan3" "fan4" "fan5")
# Allow override via space-separated env string, e.g. FAN_IDS_STR="fan1 fan2 fan3 fan4 fan5"
if [[ -n "${FAN_IDS_STR:-}" ]]; then
  read -r -a FAN_IDS <<<"$FAN_IDS_STR"
fi

CHECK_INTERVAL="${CHECK_INTERVAL:-5}"
MAX_STEP="${MAX_STEP:-10}"
FAN_SPEED_FILE="${FAN_SPEED_FILE:-/opt/dynamic-fan-ui/fan_speeds.txt}"
FAN_CURVE_FILE="${FAN_CURVE_FILE:-/opt/dynamic-fan-ui/fan_curve.json}"

declare -A LAST_SPEEDS

# Ensure directories exist
mkdir -p "$(dirname "$FAN_SPEED_FILE")" "$(dirname "$FAN_CURVE_FILE")"

# Build SSH helper
SSH_OPTS=("-o" "StrictHostKeyChecking=no")
if [[ -n "$ILO_SSH_KEY" && -f "$ILO_SSH_KEY" ]]; then
  SSH_OPTS+=("-i" "$ILO_SSH_KEY")
fi
if [[ "$ILO_SSH_LEGACY" == "1" ]]; then
  SSH_OPTS+=(
    "-o" "KexAlgorithms=+diffie-hellman-group14-sha1,diffie-hellman-group1-sha1"
    "-o" "HostKeyAlgorithms=+ssh-rsa"
    "-o" "PubkeyAcceptedAlgorithms=+ssh-rsa"
    "-o" "PubkeyAcceptedKeyTypes=+ssh-rsa"
    "-o" "Ciphers=+aes128-cbc,3des-cbc"
    "-o" "MACs=+hmac-sha1"
  )
fi
ssh_ilo() {
  local cmd=$1
  if [[ -n "$ILO_PASSWORD" ]]; then
    if command -v sshpass >/dev/null 2>&1; then
      sshpass -p "$ILO_PASSWORD" ssh "${SSH_OPTS[@]}" "$ILO_USER@$ILO_IP" "$cmd"
    else
      echo "sshpass not found but ILO_PASSWORD is set; install sshpass or use key-based auth" >&2
      return 1
    fi
  else
    ssh -o BatchMode=yes "${SSH_OPTS[@]}" "$ILO_USER@$ILO_IP" "$cmd"
  fi
}

# Load previous fan speeds if available
if [[ -f "$FAN_SPEED_FILE" ]]; then
  i=0
  while read line; do
    LAST_SPEEDS[${FAN_IDS[i]}]=$line
    ((i++))
  done < "$FAN_SPEED_FILE"
fi

# Load fan curve JSON (minTemp,maxTemp,minSpeed,maxSpeed)
if [[ ! -f "$FAN_CURVE_FILE" ]]; then
cat > "$FAN_CURVE_FILE" <<EOF
{
  "minTemp": 30,
  "maxTemp": 80,
  "minSpeed": 20,
  "maxSpeed": 100
}
EOF
fi

# Function to read temps
get_cpu_temp() {
  if [[ "$USE_IPMI_TEMPS" == "1" && -n "$ILO_IP" && -n "$ILO_USER" ]]; then
    if command -v ipmitool >/dev/null 2>&1; then
      # Read highest CPU temperature reported via SDRs; adjust grep if needed per platform
      ipmitool -I lanplus -H "$ILO_IP" -U "$ILO_USER" ${ILO_PASSWORD:+-P "$ILO_PASSWORD"} sdr type Temperature \
        | awk -F'|' '/CPU/ { if (match($2, /[0-9]+/, m)) print m[0]; }' | sort -nr | head -1
      return
    fi
  fi
  ssh_ilo "show /system1/sensors" | grep "CPU" | awk '{print $2}' | sort -nr | head -1
}

get_gpu_temp() {
  nvidia-smi --query-gpu=temperature.gpu --format=csv,noheader,nounits | sort -nr | head -1
}

# Calculate fan speed based on max temperature
calc_fan_speed_for() {
  local temp=$1 minT=$2 maxT=$3 minS=$4 maxS=$5
  if (( temp <= minT )); then echo $minS
  elif (( temp >= maxT )); then echo $maxS
  else
    echo $(( minS + (temp-minT)*(maxS-minS)/(maxT-minT) ))
  fi
}

calc_targets() {
  local cpu_t=$1 gpu_t=$2
  local cMin cMax cMinS cMaxS gMin gMax gMinS gMaxS
  cMin=$(jq -r '.minTemp // 30' $FAN_CURVE_FILE)
  cMax=$(jq -r '.maxTemp // 80' $FAN_CURVE_FILE)
  cMinS=$(jq -r '.minSpeed // 20' $FAN_CURVE_FILE)
  cMaxS=$(jq -r '.maxSpeed // 100' $FAN_CURVE_FILE)
  # GPU curve optional; fallback to CPU curve if not present
  gMin=$(jq -r '.gpu.minTemp // empty' $FAN_CURVE_FILE)
  if [[ -z "$gMin" ]]; then gMin=$cMin; gMax=$cMax; gMinS=$cMinS; gMaxS=$cMaxS; else
    gMax=$(jq -r '.gpu.maxTemp // 85' $FAN_CURVE_FILE)
    gMinS=$(jq -r '.gpu.minSpeed // 20' $FAN_CURVE_FILE)
    gMaxS=$(jq -r '.gpu.maxSpeed // 100' $FAN_CURVE_FILE)
  fi

  local cpu_target gpu_target
  cpu_target=$(calc_fan_speed_for "$cpu_t" "$cMin" "$cMax" "$cMinS" "$cMaxS")
  gpu_target=$(calc_fan_speed_for "$gpu_t" "$gMin" "$gMax" "$gMinS" "$gMaxS")
  echo "$cpu_target" "$gpu_target"
}

apply_fan_speed() {
  local target=$1
  for fan in "${FAN_IDS[@]}"; do
    CURRENT=${LAST_SPEEDS[$fan]:-20}
    DIFF=$(( target - CURRENT ))
    # Optional minimum change to avoid oscillation
    if command -v jq >/dev/null 2>&1; then
      MINC=$(jq -r '(.minChange // 0)' "$FAN_CURVE_FILE" 2>/dev/null || echo 0)
      [[ "$MINC" =~ ^-?[0-9]+$ ]] || MINC=0
      if (( DIFF != 0 && ${DIFF#-} < MINC )); then
        DIFF=$(( DIFF<0 ? -MINC : MINC ))
      fi
    fi
    if (( DIFF > MAX_STEP )); then DIFF=$MAX_STEP
    elif (( DIFF < -MAX_STEP )); then DIFF=-MAX_STEP; fi
    NEW=$(( CURRENT + DIFF ))
    ssh_ilo "set /system1/$fan speed=$NEW" >/dev/null
    LAST_SPEEDS[$fan]=$NEW
  done
  # Save current speeds
  for fan in "${FAN_IDS[@]}"; do
    echo ${LAST_SPEEDS[$fan]}
  done > $FAN_SPEED_FILE
}

# Main loop
while true; do
  CPU_TEMP=$(get_cpu_temp)
  GPU_TEMP=$(get_gpu_temp)
  read CPU_TARGET GPU_TARGET < <(calc_targets "$CPU_TEMP" "$GPU_TEMP")
  FAN_TARGET=$(( CPU_TARGET > GPU_TARGET ? CPU_TARGET : GPU_TARGET ))
  apply_fan_speed $FAN_TARGET
  # Optional runtime overrides from JSON
  if command -v jq >/dev/null 2>&1; then
    CI=$(jq -r '(.checkInterval // empty)' "$FAN_CURVE_FILE" 2>/dev/null || true)
    MS=$(jq -r '(.maxStep // empty)' "$FAN_CURVE_FILE" 2>/dev/null || true)
    [[ -n "$CI" && "$CI" =~ ^[0-9]+$ ]] && CHECK_INTERVAL=$CI
    [[ -n "$MS" && "$MS" =~ ^-?[0-9]+$ ]] && MAX_STEP=$MS
  fi
  sleep $CHECK_INTERVAL
done
