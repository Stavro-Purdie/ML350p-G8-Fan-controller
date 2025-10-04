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
ILO_MODDED="${ILO_MODDED:-0}"
ILO_PID_OFFSET="${ILO_PID_OFFSET:--1}"

FAN_IDS=("fan1" "fan2" "fan3" "fan4" "fan5")
# Allow overriding the base path(s) to fan objects
FAN_PATHS=("/system1" "/system1/fans1")
# Candidate property names to try if not specified
PROP_CANDIDATES=("speed" "pwm" "duty" "duty_cycle" "fan_speed" "percentage")
# Allow override of fan property
ILO_FAN_PROP="${ILO_FAN_PROP:-}"
# Allow override via space-separated env string, e.g. FAN_IDS_STR="fan1 fan2 fan3 fan4 fan5"
if [[ -n "${FAN_IDS_STR:-}" ]]; then
  read -r -a FAN_IDS <<<"$FAN_IDS_STR"
fi
# Optional numeric P-IDs for modded iLO commands
P_IDS=()
if [[ -n "${FAN_P_IDS_STR:-}" ]]; then
  read -r -a P_IDS <<<"$FAN_P_IDS_STR"
fi
# Derive P-IDs from FAN_IDS if not provided (strip 'fan' prefix)
if (( ${#P_IDS[@]} == 0 )); then
  for f in "${FAN_IDS[@]}"; do
    if [[ "$f" =~ ^fan([0-9]+)$ ]]; then
      num=${BASH_REMATCH[1]}
      pid=$(( num + ILO_PID_OFFSET ))
      (( pid < 0 )) && pid=0
      P_IDS+=("$pid")
    fi
  done
fi

# Attempt auto-discovery of fan objects if FAN_IDS appears invalid
auto_discover_fans() {
  local discovered=()
  for p in "${FAN_PATHS[@]}"; do
    local out
    out=$(ssh_ilo "show $p" 2>/dev/null || true)
    if [[ -n "$out" ]]; then
      # Look for tokens like fan1, fan2 etc.
      while read -r tok; do
        [[ -n "$tok" ]] && discovered+=("$tok")
      done < <(echo "$out" | awk '{for(i=1;i<=NF;i++) if ($i ~ /^fan[0-9]+$/) print $i}' | sort -u)
    fi
    if (( ${#discovered[@]} > 0 )); then
      echo "Auto-discovered fans at $p: ${discovered[*]}" >&2
      FAN_IDS=("${discovered[@]}")
      return
    fi
  done
}

# Discover a usable fan property (percent-like) from iLO output
detect_fan_prop() {
  local fan_sample="${FAN_IDS[0]}" prop=""
  for prefix in "${FAN_PATHS[@]}"; do
    local out
    out=$(ssh_ilo "show -a $prefix/$fan_sample" 2>/dev/null || ssh_ilo "show $prefix/$fan_sample" 2>/dev/null || true)
    if [[ -z "$out" ]]; then continue; fi
    # Look for key=value style fields with names containing speed/pwm/duty and numeric 0-100
    prop=$(echo "$out" | awk '
      BEGIN{IGNORECASE=1}
      {
        # split on separators
        n=split($0, a, /[=:]/);
        if (n>=2) {
          key=a[1]; val=a[2];
          gsub(/^[ \\t]+|[ \\t]+$/, "", key);
          gsub(/^[ \\t]+|[ \\t]+$/, "", val);
          if (key ~ /(speed|pwm|duty)/ && match(val, /[0-9]{1,3}/, m)) {
            v=m[0]+0; if (v>=0 && v<=100) { print tolower(key); exit; }
          }
        }
      }
    ' | head -1)
    if [[ -n "$prop" ]]; then
      echo "Detected fan property: $prop on $prefix/$fan_sample" >&2
      ILO_FAN_PROP="$prop"
      return 0
    fi
  done
  return 1
}

# Validate that first fan is queryable; otherwise try to discover
if ! ssh_ilo "show /system1/${FAN_IDS[0]}" >/dev/null 2>&1 && ! ssh_ilo "show /system1/fans1/${FAN_IDS[0]}" >/dev/null 2>&1; then
  auto_discover_fans
fi
if [[ -z "$ILO_FAN_PROP" ]]; then
  detect_fan_prop || true
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
      # Read highest CPU temperature via SDRs; prefer values with trailing C or 'degrees C'
      ipmitool -I lanplus -H "$ILO_IP" -U "$ILO_USER" ${ILO_PASSWORD:+-P "$ILO_PASSWORD"} sdr type Temperature \
        | awk '
          ($0 ~ /[Cc][Pp][Uu]|[Pp]roc|[Pp]rocessor/) {
            if (match($0, /([0-9]+)[ ]*[Cc]/, m)) { print m[1]; }
            else if (match($0, /([0-9]+)/, m)) { print m[1]; }
          }
        ' | sort -nr | head -1
      return
    fi
  fi
  # Fallback to iLO sensors over SSH; prefer values with trailing C
  ssh_ilo "show /system1/sensors" | awk '
    ($0 ~ /[Cc][Pp][Uu]|[Pp]roc|[Pp]rocessor/) {
      if (match($0, /([0-9]+)[ ]*[Cc]/, m)) { print m[1]; }
      else {
        for (i=1;i<=NF;i++) if (match($i, /^([0-9]+)$/, n)) print n[1];
      }
    }
  ' | sort -nr | head -1
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
  # Helper to set speed with path fallbacks
  ilo_set_speed() {
    local fan=$1 val=$2
    local ok=1
    for prefix in "${FAN_PATHS[@]}"; do
      if [[ -n "$ILO_FAN_PROP" ]]; then
        if ssh_ilo "set $prefix/$fan $ILO_FAN_PROP=$val" >/dev/null 2>&1; then ok=0; break; fi
      fi
      for prop in "${PROP_CANDIDATES[@]}"; do
        if ssh_ilo "set $prefix/$fan $prop=$val" >/dev/null 2>&1; then ok=0; ILO_FAN_PROP="$prop"; break; fi
      done
      [[ $ok -eq 0 ]] && break
    done
    return $ok
  }
  if [[ "$ILO_MODDED" == "1" ]]; then
    # Modded iLO: use "fan p <id> min YY" and "fan p <id> max YY" with YY in [1..255]
    for idx in "${!P_IDS[@]}"; do
      local fan_id=${P_IDS[$idx]}
      local fan=${FAN_IDS[$idx]:-fan$fan_id}
      CURRENT=${LAST_SPEEDS[$fan]:-20}
      DIFF=$(( target - CURRENT ))
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
      (( NEW < 0 )) && NEW=0
      (( NEW > 100 )) && NEW=100
      # Map percent to 1..255 (avoid 0)
      local v255=$(( (NEW * 255 + 50) / 100 ))
      (( v255 < 1 )) && v255=1
      (( v255 > 255 )) && v255=255
      # Set exact control: min = max = v255
      if ! ssh_ilo "fan p $fan_id min $v255" >/dev/null 2>&1; then
        echo "WARN: Failed to set fan p $fan_id min=$v255" >&2
      fi
      if ! ssh_ilo "fan p $fan_id max $v255" >/dev/null 2>&1; then
        echo "WARN: Failed to set fan p $fan_id max=$v255" >&2
      fi
      LAST_SPEEDS[$fan]=$NEW
    done
  else
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
    # Clamp to [0,100]
    (( NEW < 0 )) && NEW=0
    (( NEW > 100 )) && NEW=100
    if ! ilo_set_speed "$fan" "$NEW"; then
      echo "WARN: Failed to set $fan to $NEW" >&2
    fi
    LAST_SPEEDS[$fan]=$NEW
    done
  fi
  # Save current speeds
  for fan in "${FAN_IDS[@]}"; do
    echo ${LAST_SPEEDS[$fan]}
  done > $FAN_SPEED_FILE
}

# Main loop
while true; do
  CPU_TEMP=$(get_cpu_temp)
  GPU_TEMP=$(get_gpu_temp)
  # Sanitize temps
  [[ "$CPU_TEMP" =~ ^[0-9]+$ ]] || CPU_TEMP=0
  [[ "$GPU_TEMP" =~ ^[0-9]+$ ]] || GPU_TEMP=0
  read CPU_TARGET GPU_TARGET < <(calc_targets "$CPU_TEMP" "$GPU_TEMP")
  # Blend mode and GPU boost
  BLEND_MODE=$(jq -r '(.blend.mode // "max")' "$FAN_CURVE_FILE" 2>/dev/null || echo max)
  if [[ "$BLEND_MODE" == "weighted" ]]; then
    CW=$(jq -r '(.blend.cpuWeight // 0.5)' "$FAN_CURVE_FILE" 2>/dev/null || echo 0.5)
    GW=$(jq -r '(.blend.gpuWeight // 0.5)' "$FAN_CURVE_FILE" 2>/dev/null || echo 0.5)
    ZERO=$(awk -v cw="$CW" -v gw="$GW" 'BEGIN{print (cw+gw==0)?1:0}')
    if [[ "$ZERO" == "1" ]]; then
      FAN_TARGET=$(( CPU_TARGET > GPU_TARGET ? CPU_TARGET : GPU_TARGET ))
    else
      FAN_TARGET=$(awk -v c="$CPU_TARGET" -v g="$GPU_TARGET" -v cw="$CW" -v gw="$GW" 'BEGIN{printf("%d", int(c*cw + g*gw + 0.5))}')
    fi
  else
    FAN_TARGET=$(( CPU_TARGET > GPU_TARGET ? CPU_TARGET : GPU_TARGET ))
  fi
  # Optional GPU boost when GPU temp exceeds threshold
  GB_T=$(jq -r '(.gpuBoost.threshold // empty)' "$FAN_CURVE_FILE" 2>/dev/null || true)
  GB_A=$(jq -r '(.gpuBoost.add // 0)' "$FAN_CURVE_FILE" 2>/dev/null || echo 0)
  if [[ -n "$GB_T" && "$GB_T" =~ ^[0-9]+$ && "$GPU_TEMP" -ge "$GB_T" ]]; then
    FAN_TARGET=$(( FAN_TARGET + GB_A ))
  fi
  (( FAN_TARGET < 0 )) && FAN_TARGET=0
  (( FAN_TARGET > 100 )) && FAN_TARGET=100
  apply_fan_speed $FAN_TARGET
  # Log a lightweight heartbeat for diagnostics (visible in journalctl)
  echo "CPU=${CPU_TEMP}C GPU=${GPU_TEMP}C -> target=${FAN_TARGET}% speeds=[${FAN_IDS[*]}]" | sed 's/ /,/g' >&2
  # Optional runtime overrides from JSON
  if command -v jq >/dev/null 2>&1; then
    CI=$(jq -r '(.checkInterval // empty)' "$FAN_CURVE_FILE" 2>/dev/null || true)
    MS=$(jq -r '(.maxStep // empty)' "$FAN_CURVE_FILE" 2>/dev/null || true)
    [[ -n "$CI" && "$CI" =~ ^[0-9]+$ ]] && CHECK_INTERVAL=$CI
    [[ -n "$MS" && "$MS" =~ ^-?[0-9]+$ ]] && MAX_STEP=$MS
  fi
  sleep $CHECK_INTERVAL
done
