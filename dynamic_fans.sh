#!/bin/bash
PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:${PATH:-}"
# Dynamic fan controller for HP Gen8 + Tesla P40 with persistent fan speeds
# Saves last speeds to /opt/dynamic-fan-ui/fan_speeds.txt

# Configurable via env vars; sensible defaults preserved
ILO_IP="${ILO_IP:-192.168.1.100}"
ILO_USER="${ILO_USER:-admin}"
# If set and file exists, will be used with ssh -i; if empty, password or ssh config may be used
ILO_SSH_KEY="${ILO_SSH_KEY:-/root/.ssh/ilo_key}"
# Optional password auth (requires sshpass); leave empty to avoid using it
ILO_PASSWORD="${ILO_PASSWORD:-}"
# Enable legacy SSH algorithms for older iLO (OpenSSH 8.8+ compatibility)
ILO_SSH_LEGACY="${ILO_SSH_LEGACY:-0}"
# Force modded mode fixed ON
ILO_MODDED="${ILO_MODDED:-1}"
ILO_MODDED="1"
ILO_PID_OFFSET="${ILO_PID_OFFSET:--1}"
ILO_SSH_TTY="${ILO_SSH_TTY:-1}"
VERBOSE="${VERBOSE:-0}"
ILO_SSH_TIMEOUT="${ILO_SSH_TIMEOUT:-320}"
ILO_SSH_PERSIST="${ILO_SSH_PERSIST:-60}"
# Default pacing gap between iLO commands (ms) — fixed to 1 second per request
ILO_CMD_GAP_MS="${ILO_CMD_GAP_MS:-1000}"
ILO_BATCH_SIZE="${ILO_BATCH_SIZE:-1}"
# Simple millisecond sleep without external deps
sleep_ms() {
  local ms=$1
  [[ -z "$ms" ]] && return
  (( ms <= 0 )) && return
  local s=$(( ms/1000 ))
  local rem=$(( ms%1000 ))
  if (( s > 0 )); then sleep $s; fi
  if (( rem > 0 )); then
    local frac
    frac=$(printf "0.%03d" "$rem")
    sleep "$frac"
  fi
}

FAN_IDS=("fan1" "fan2" "fan3" "fan4" "fan5")
# Allow overriding the base path(s) to fan objects (used only for non-modded 'set' paths)
FAN_PATHS=("/system1" "/system1/fans1")
# Candidate property names to try if not specified (non-modded control)
PROP_CANDIDATES=("speed" "pwm" "duty" "duty_cycle" "fan_speed" "percentage")
# Allow override of fan property (non-modded)
ILO_FAN_PROP="${ILO_FAN_PROP:-}"
# Force controlling only three fans (skip disconnected fan1): fan2, fan3, fan4
FAN_IDS=("fan2" "fan3" "fan4")

# Discover fans using 'fan info' (avoids 'show' calls)
discover_from_fan_info() {
  local out nums=()
  out=$(ssh_ilo "fan info" 2>/dev/null || true)
  if [[ -n "$out" ]]; then
    # Extract occurrences like 'fan<number>' or 'Fan <number>'
    mapfile -t nums < <(echo "$out" | tr 'A-Z' 'a-z' | grep -oE 'fan[[:space:]]*[0-9]+' | sed -E 's/[^0-9]+//g' | sort -nu)
    if (( ${#nums[@]} > 0 )); then
      FAN_IDS=()
      for n in "${nums[@]}"; do
        [[ -n "$n" ]] && FAN_IDS+=("fan$n")
      done
      [[ "$VERBOSE" == "1" ]] && echo "[discover] fan info -> ${FAN_IDS[*]}" >&2
    fi
  fi
}

# Optional numeric P-IDs for modded iLO commands
P_IDS=()
if [[ -n "${FAN_P_IDS_STR:-}" ]]; then
  read -r -a P_IDS <<<"$FAN_P_IDS_STR"
fi
# If not provided, attempt discovery first, then derive from FAN_IDS
# Default P-IDs for the three fans (respect override if provided)
if (( ${#P_IDS[@]} == 0 )); then
  # Mapping per user: fan2->p1, fan3->p2, fan4->p3
  P_IDS=("1" "2" "3")
fi

# Helper to send exact max/min with retries and backoff
send_exact_modded() {
  local pid=$1 vmax=$2 vmin=$3 order=${4:-inc}
  local attempt gap ok_a ok_b cmd_a cmd_b label_a label_b
  gap=$ILO_CMD_GAP_MS
  if [[ "$order" == "dec" ]]; then
    cmd_a=(fan p $pid min $vmin); label_a="min $vmin"
    cmd_b=(fan p $pid max $vmax); label_b="max $vmax"
  else
    cmd_a=(fan p $pid max $vmax); label_a="max $vmax"
    cmd_b=(fan p $pid min $vmin); label_b="min $vmin"
  fi
  for attempt in 1 2 3; do
    ok_a=0; ok_b=0
    if ssh_ilo "${cmd_a[*]}" >/dev/null 2>&1; then ok_a=1; else echo "WARN: Failed: fan p $pid $label_a (try $attempt)" >&2; fi
    sleep_ms "$gap"
    if ssh_ilo "${cmd_b[*]}" >/dev/null 2>&1; then ok_b=1; else echo "WARN: Failed: fan p $pid $label_b (try $attempt)" >&2; fi
    sleep_ms "$gap"
    if (( ok_a == 1 && ok_b == 1 )); then return 0; fi
    # Exponential backoff up to ~800ms
    gap=$(( gap < 800 ? gap*2 : gap ))
  done
  return 1
}

# Note: We avoid 'show' and 'fans show' for discovery or detection to reduce iLO load.

CHECK_INTERVAL="${CHECK_INTERVAL:-1}"
MAX_STEP="${MAX_STEP:-10}"
FAN_SPEED_FILE="${FAN_SPEED_FILE:-/opt/dynamic-fan-ui/fan_speeds.txt}"
FAN_SPEED_BITS_FILE="${FAN_SPEED_BITS_FILE:-/opt/dynamic-fan-ui/fan_speeds_bits.txt}"
FAN_CURVE_FILE="${FAN_CURVE_FILE:-/opt/dynamic-fan-ui/fan_curve.json}"
PWM_UNITS="${PWM_UNITS:-bits}"

PREDICT_HISTORY="${PREDICT_HISTORY:-180}"
PREDICT_WINDOW="${PREDICT_WINDOW:-60}"
PREDICT_MIN_POINTS="${PREDICT_MIN_POINTS:-4}"
PREDICT_HORIZON="${PREDICT_HORIZON:-20}"
PREDICT_BLEND="${PREDICT_BLEND:-0.45}"
PREDICT_GPU_BLEND="${PREDICT_GPU_BLEND:-0.55}"
PREDICT_DEADBAND="${PREDICT_DEADBAND:-1}"
PREDICT_GPU_DEADBAND="${PREDICT_GPU_DEADBAND:-1}"
PREDICT_LEAD="${PREDICT_LEAD:-8}"
PREDICT_GPU_LEAD="${PREDICT_GPU_LEAD:-10}"
PREDICT_SLOPE_GAIN="${PREDICT_SLOPE_GAIN:-0.6}"
PREDICT_GPU_SLOPE_GAIN="${PREDICT_GPU_SLOPE_GAIN:-0.7}"
PREDICT_MAX_OFFSET="${PREDICT_MAX_OFFSET:-6}"
PREDICT_GPU_MAX_OFFSET="${PREDICT_GPU_MAX_OFFSET:-7}"
PREDICT_RATE_GAIN="${PREDICT_RATE_GAIN:-4}"
PREDICT_GPU_RATE_GAIN="${PREDICT_GPU_RATE_GAIN:-5}"
PREDICT_RATE_DEADBAND="${PREDICT_RATE_DEADBAND:-0.15}"
PREDICT_GPU_RATE_DEADBAND="${PREDICT_GPU_RATE_DEADBAND:-0.2}"
PREDICT_RATE_MAX="${PREDICT_RATE_MAX:-25}"
PREDICT_GPU_RATE_MAX="${PREDICT_GPU_RATE_MAX:-30}"
PREDICT_RATE_COOLDOWN="${PREDICT_RATE_COOLDOWN:-0.5}"
PREDICT_GPU_RATE_COOLDOWN="${PREDICT_GPU_RATE_COOLDOWN:-0.4}"

declare -a CPU_HISTORY=()
declare -a GPU_HISTORY=()

append_history() {
  local name=$1 ts=$2 value=$3
  [[ -z "$name" || -z "$ts" || -z "$value" ]] && return
  if [[ ! "$value" =~ ^-?[0-9]+(\.[0-9]+)?$ ]]; then
    return
  fi
  local -n arr=$name
  arr+=("$ts:$value")
  local len=${#arr[@]}
  if (( len > PREDICT_HISTORY )); then
    local start=$(( len - PREDICT_HISTORY ))
    arr=("${arr[@]:$start}")
  fi
}

forecast_temp() {
  local name=$1 fallback=$2
  local horizon=${3:-$PREDICT_HORIZON}
  local -n arr=$name
  local len=${#arr[@]}
  if (( len < PREDICT_MIN_POINTS )); then
    printf '%s %s\n' "$fallback" "0"
    return
  fi
  local result
  local tail_cmd=(cat)
  if (( PREDICT_WINDOW > 0 && len > PREDICT_WINDOW )); then
    tail_cmd=(tail -n "$PREDICT_WINDOW")
  fi
  result=$(printf '%s\n' "${arr[@]}" | "${tail_cmd[@]}" | awk -F':' -v horizon="$horizon" -v fallback="$fallback" -v min="$PREDICT_MIN_POINTS" '
    NF < 2 { next }
    {
      if (count == 0) base = $1
      x = ($1 - base)
      sumX += x
      sumY += $2
      sumXX += x * x
      sumXY += x * $2
      lastX = x
      count++
    }
    END {
      if (count < min) {
        printf("%s 0\n", fallback)
        exit
      }
      denom = count * sumXX - sumX * sumX
      if (denom == 0) {
        printf("%s 0\n", fallback)
        exit
      }
      slope = (count * sumXY - sumX * sumY) / denom
      intercept = (sumY - slope * sumX) / count
      forecast = intercept + slope * (lastX + horizon)
      if (forecast != forecast || forecast == "") {
        printf("%s 0\n", fallback)
        exit
      }
      printf("%.2f %.6f\n", forecast, slope)
    }
  ')
  if [[ -z "$result" ]]; then
    printf '%s %s\n' "$fallback" "0"
  else
    printf '%s\n' "$result"
  fi
}

blend_temp() {
  local current=$1 forecast=$2 weight=${3:-$PREDICT_BLEND}
  if [[ ! "$forecast" =~ ^-?[0-9]+(\.[0-9]+)?$ ]]; then
    printf '%s\n' "$current"
    return
  fi
  if [[ ! "$current" =~ ^-?[0-9]+(\.[0-9]+)?$ ]]; then
    printf '%s\n' "$forecast"
    return
  fi
  local blended
  blended=$(awk -v c="$current" -v f="$forecast" -v w="$weight" 'BEGIN {
    if (w < 0) w = 0;
    if (w > 1) w = 1;
    printf("%.2f\n", c * (1 - w) + f * w);
  }')
  if [[ -z "$blended" ]]; then
    printf '%s\n' "$forecast"
  else
    printf '%s\n' "$blended"
  fi
}

lead_adjust() {
  local slope=$1 lead=$2 gain=$3 max_off=$4
  [[ -z "$lead" ]] && lead=$PREDICT_LEAD
  [[ -z "$gain" ]] && gain=$PREDICT_SLOPE_GAIN
  [[ -z "$max_off" ]] && max_off=$PREDICT_MAX_OFFSET
  if [[ ! "$slope" =~ ^-?[0-9]+(\.[0-9]+)?$ ]]; then
    printf '0\n'
    return
  fi
  if [[ ! "$lead" =~ ^-?[0-9]+(\.[0-9]+)?$ ]]; then
    lead=0
  fi
  if [[ ! "$gain" =~ ^-?[0-9]+(\.[0-9]+)?$ ]]; then
    gain=1
  fi
  if [[ ! "$max_off" =~ ^-?[0-9]+(\.[0-9]+)?$ ]]; then
    max_off=$PREDICT_MAX_OFFSET
  fi
  local offset
  offset=$(awk -v s="$slope" -v l="$lead" -v g="$gain" -v m="$max_off" 'BEGIN {
    if (m < 0) m = -m;
    val = s * l * g;
    if (val > m) val = m;
    if (val < -m) val = -m;
    printf("%.2f\n", val);
  }')
  [[ -z "$offset" ]] && offset=0
  printf '%s\n' "$offset"
}

rate_adjust() {
  local rate=$1 gain=${2:-$PREDICT_RATE_GAIN} max_adj=${3:-$PREDICT_RATE_MAX} dead=${4:-$PREDICT_RATE_DEADBAND} cooldown=${5:-$PREDICT_RATE_COOLDOWN}
  if [[ ! "$rate" =~ ^-?[0-9]+(\.[0-9]+)?$ ]]; then
    printf '0\n'
    return
  fi
  if [[ ! "$gain" =~ ^-?[0-9]+(\.[0-9]+)?$ ]]; then
    gain=$PREDICT_RATE_GAIN
  fi
  if [[ ! "$max_adj" =~ ^-?[0-9]+(\.[0-9]+)?$ ]]; then
    max_adj=$PREDICT_RATE_MAX
  fi
  if [[ ! "$dead" =~ ^-?[0-9]+(\.[0-9]+)?$ ]]; then
    dead=$PREDICT_RATE_DEADBAND
  fi
  if [[ ! "$cooldown" =~ ^-?[0-9]+(\.[0-9]+)?$ ]]; then
    cooldown=$PREDICT_RATE_COOLDOWN
  fi
  local adj
  adj=$(awk -v rate="$rate" -v gain="$gain" -v maxa="$max_adj" -v dead="$dead" -v cool="$cooldown" 'BEGIN {
    if (maxa < 0) maxa = -maxa;
    if (dead < 0) dead = -dead;
    if (cool < 0) cool = -cool;
    absRate = rate;
    if (absRate < 0) absRate = -absRate;
    if (absRate <= dead) {
      printf("0\n");
      exit;
    }
    eff = absRate - dead;
    if (rate < 0 && cool > 0 && cool < 1) {
      eff = eff * (1 - cool);
    }
    val = (rate >= 0 ? eff : -eff) * gain;
    if (val > maxa) val = maxa;
    if (val < -maxa) val = -maxa;
    printf("%.2f\n", val);
  }')
  [[ -z "$adj" ]] && adj=0
  printf '%s\n' "$adj"
}

declare -A LAST_SPEEDS
declare -A LAST_BITS

# Ensure directories exist
mkdir -p "$(dirname "$FAN_SPEED_FILE")" "$(dirname "$FAN_CURVE_FILE")"
mkdir -p "$(dirname "$FAN_SPEED_BITS_FILE")" || true

# Build SSH helper
SSH_OPTS=("-o" "StrictHostKeyChecking=no")
# Apply ConnectTimeout, retries, and keep-alives
SSH_OPTS+=("-o" "ConnectTimeout=${ILO_SSH_TIMEOUT}" "-o" "ConnectionAttempts=3" "-o" "ServerAliveInterval=5" "-o" "ServerAliveCountMax=2")
# Reuse a control connection to avoid per-command handshake overhead
SSH_OPTS+=("-o" "ControlMaster=auto" "-o" "ControlPath=/tmp/ssh-ilo-%C" "-o" "ControlPersist=${ILO_SSH_PERSIST}")
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
# Some iLO shells behave better with a forced TTY (especially modded cmds)
if [[ "$ILO_SSH_TTY" == "1" || "$ILO_MODDED" == "1" ]]; then
  SSH_OPTS+=("-tt")
fi

ssh_ilo() {
  local cmd=$1
  [[ "$VERBOSE" == "1" ]] && echo "[ssh_ilo] CMD: $cmd" >&2
  local output rc
  # Cross-process lock to avoid concurrent iLO commands
  local LOCK_FILE="${LOCK_FILE:-/opt/dynamic-fan-ui/ilo.lock}"
  local have_flock=0
  command -v flock >/dev/null 2>&1 && have_flock=1
  if [[ -n "$ILO_PASSWORD" ]]; then
    if command -v sshpass >/dev/null 2>&1; then
      if (( have_flock )); then
        exec {FD}>"$LOCK_FILE"
        flock -w "$ILO_SSH_TIMEOUT" "$FD"
        output=$(sshpass -p "$ILO_PASSWORD" ssh "${SSH_OPTS[@]}" "$ILO_USER@$ILO_IP" "$cmd" 2>&1); rc=$?
        flock -u "$FD"; exec {FD}>&-
      else
        output=$(sshpass -p "$ILO_PASSWORD" ssh "${SSH_OPTS[@]}" "$ILO_USER@$ILO_IP" "$cmd" 2>&1); rc=$?
      fi
    else
      echo "sshpass not found but ILO_PASSWORD is set; install sshpass or use key-based auth" >&2
      return 1
    fi
  else
    if (( have_flock )); then
      exec {FD}>"$LOCK_FILE"
      flock -w "$ILO_SSH_TIMEOUT" "$FD"
      output=$(ssh -o BatchMode=yes "${SSH_OPTS[@]}" "$ILO_USER@$ILO_IP" "$cmd" 2>&1); rc=$?
      flock -u "$FD"; exec {FD}>&-
    else
      output=$(ssh -o BatchMode=yes "${SSH_OPTS[@]}" "$ILO_USER@$ILO_IP" "$cmd" 2>&1); rc=$?
    fi
  fi
  [[ "$VERBOSE" == "1" ]] && echo "[ssh_ilo] OUT: ${output//$'\n'/ } (rc=$rc)" >&2
  printf "%s" "$output"
  return $rc
}

# Optionally prewarm a control connection to reduce first-call latency
if [[ "${PREWARM:-1}" == "1" ]]; then
  ssh_ilo "echo prewarm" >/dev/null 2>&1 || true
fi

# Load previous fan speeds if available
if [[ -f "$FAN_SPEED_FILE" ]]; then
  i=0
  while read line; do
    LAST_SPEEDS[${FAN_IDS[i]}]=$line
    ((i++))
  done < "$FAN_SPEED_FILE"
fi
if [[ -f "$FAN_SPEED_BITS_FILE" ]]; then
  i=0
  while read line; do
    LAST_BITS[${FAN_IDS[i]}]=$line
    ((i++))
  done < "$FAN_SPEED_BITS_FILE"
fi

# Load fan curve JSON (minTemp,maxTemp,minSpeed,maxSpeed)
if [[ ! -f "$FAN_CURVE_FILE" ]]; then
cat > "$FAN_CURVE_FILE" <<EOF
{
  "minTemp": 30,
  "maxTemp": 80,
  "minSpeed": 20,
  "maxSpeed": 100,
  "checkInterval": 1,
  "maxStep": 20,
  "predict": {
    "horizon": 45,
    "history": 240,
    "minPoints": 6,
    "blend": 0.7,
    "gpuBlend": 0.75,
    "lead": 20,
    "gpuLead": 25,
    "slopeGain": 1.2,
    "gpuSlopeGain": 1.35,
    "maxOffset": 12,
    "gpuMaxOffset": 16,
    "deadband": 3,
    "gpuDeadband": 4,
    "rateGain": 5,
    "gpuRateGain": 6,
    "rateDeadband": 0.2,
    "gpuRateDeadband": 0.25,
    "rateMax": 30,
    "gpuRateMax": 35,
    "rateCooldown": 0.4,
    "gpuRateCooldown": 0.35
  }
}
EOF
fi

# Function to read temps
LAST_CPU_TS=0
LAST_CPU_VAL=35
SENSORS_WARNED=0
get_cpu_temp() {
  # Read CPU temperature via lm-sensors output (Package temperature)
  if ! command -v sensors >/dev/null 2>&1; then
    if (( SENSORS_WARNED == 0 )); then
      echo "ERROR: 'sensors' command not found in PATH ($PATH); install lm-sensors or adjust service environment." >&2
      SENSORS_WARNED=1
    fi
    echo "$LAST_CPU_VAL"
    return
  fi
  local now_ts; now_ts=$(date +%s)
  if (( LAST_CPU_TS > 0 && now_ts - LAST_CPU_TS < 5 && LAST_CPU_VAL > 0 )); then
    echo "$LAST_CPU_VAL"; return
  fi
  local sensors_out
  sensors_out=$(sensors 2>/dev/null)
  local val pkg hottest
  pkg=$(printf "%s\n" "$sensors_out" | awk '
    BEGIN { pkg = "" }
    /Package id/ {
      for (i = 1; i <= NF; i++) {
        if ($i ~ /\+[0-9]/) {
          val = $i
          gsub(/[^0-9.]/, "", val)
          if (val != "") {
            pkg = val + 0
            break
          }
        }
      }
    }
    END { if (pkg != "") printf "%d\n", int(pkg) }
  ')
  if [[ -n "$pkg" ]]; then
    val=$pkg
  else
    hottest=$(printf "%s\n" "$sensors_out" | awk '
      BEGIN { max = 0 }
      /\+[0-9]/ {
        for (i = 1; i <= NF; i++) {
          if ($i ~ /\+[0-9]/) {
            temp = $i
            gsub(/[^0-9.]/, "", temp)
            val = temp + 0
            if (val > max) { max = val }
          }
        }
      }
      END { if (max > 0) printf "%d\n", int(max) }
    ')
    [[ -n "$hottest" ]] && val=$hottest || val=""
  fi
  if [[ "$val" =~ ^[0-9]+$ ]]; then
    LAST_CPU_VAL=$val
    LAST_CPU_TS=$now_ts
    echo "$val"
  else
    echo "${LAST_CPU_VAL:-35}"
  fi
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
  local default_target=$1
  shift
  declare -A overrides=()
  local entry fan val
  for entry in "$@"; do
    [[ "$entry" == *=* ]] || continue
    fan=${entry%%=*}
    val=${entry#*=}
    [[ -n "$fan" ]] || continue
    overrides["$fan"]=$val
  done
  local updates_sent=0
  local updated_pids=()
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
    # If still not ok, try 'fans X' form
    if (( ok != 0 )); then
      if [[ "$fan" =~ ^fan([0-9]+)$ ]]; then
        local num=${BASH_REMATCH[1]}
        if [[ -n "$ILO_FAN_PROP" ]]; then
          if ssh_ilo "fans $num $ILO_FAN_PROP=$val" >/dev/null 2>&1; then ok=0; fi
          if (( ok != 0 )); then
            ssh_ilo "fans $num set $ILO_FAN_PROP $val" >/dev/null 2>&1 && ok=0
          fi
        fi
        if (( ok != 0 )); then
          for prop in "${PROP_CANDIDATES[@]}"; do
            if ssh_ilo "fans $num $prop=$val" >/dev/null 2>&1; then ok=0; ILO_FAN_PROP="$prop"; break; fi
            if (( ok != 0 )); then
              ssh_ilo "fans $num set $prop $val" >/dev/null 2>&1 && { ok=0; ILO_FAN_PROP="$prop"; break; }
            fi
          done
        fi
      fi
    fi
    return $ok
  }
  if [[ "$ILO_MODDED" == "1" ]]; then
    # Modded iLO: send max then min for each PID as separate commands with pacing
    for idx in "${!P_IDS[@]}"; do
      local fan_id=${P_IDS[$idx]}
      local fan=${FAN_IDS[$idx]:-fan$fan_id}
      CURRENT=${LAST_SPEEDS[$fan]:-20}
      local desired
      if [[ ${overrides[$fan]+_} ]]; then
        desired=${overrides[$fan]}
      else
        desired=$default_target
      fi
      if [[ ! "$desired" =~ ^-?[0-9]+$ ]]; then
        desired=$default_target
      fi
      (( desired < 0 )) && desired=0
      (( desired > 100 )) && desired=100
    # When operating in bits domain, do smoothing in bits
    if [[ "$PWM_UNITS" == "bits" ]]; then
        # current bits fallback from current percent
        local cur_b=${LAST_BITS[$fan]:-0}
        if (( cur_b <= 0 )); then cur_b=$(( (CURRENT * 255 + 50) / 100 )); fi
        (( cur_b < 1 )) && cur_b=1
        (( cur_b > 255 )) && cur_b=255
        local des_b=$(( (desired * 255 + 50) / 100 ))
        (( des_b < 1 )) && des_b=1
        (( des_b > 255 )) && des_b=255
        local DIFF_B=$(( des_b - cur_b ))
        # Optional minChange in bits
        if command -v jq >/dev/null 2>&1; then
          MINC=$(jq -r '(.minChange // 0)' "$FAN_CURVE_FILE" 2>/dev/null || echo 0)
          [[ "$MINC" =~ ^-?[0-9]+$ ]] || MINC=0
          local MINC_B=$(( (MINC * 255 + 50) / 100 ))
          if (( MINC_B < 0 )); then MINC_B=$(( -MINC_B )); fi
          if (( DIFF_B != 0 && ${DIFF_B#-} < MINC_B )); then
            DIFF_B=$(( DIFF_B<0 ? -MINC_B : MINC_B ))
          fi
        fi
        # Max step in bits
        local MAX_STEP_B=$(( (MAX_STEP * 255 + 50) / 100 ))
        (( MAX_STEP_B < 1 )) && MAX_STEP_B=1
        if (( DIFF_B > MAX_STEP_B )); then DIFF_B=$MAX_STEP_B
        elif (( DIFF_B < -MAX_STEP_B )); then DIFF_B=$(( -MAX_STEP_B )); fi
        local NEW_B=$(( cur_b + DIFF_B ))
        (( NEW_B < 1 )) && NEW_B=1
        (( NEW_B > 255 )) && NEW_B=255
        # Convert back to percent for status persistence
        local NEW=$(( (NEW_B * 100 + 127) / 255 ))
        # Skip unchanged
        if (( NEW_B != cur_b )); then
          local v255=$NEW_B
          local vmin=$(( NEW_B - 4 )); (( vmin < 1 )) && vmin=1
          local order="inc"
          if (( NEW_B < cur_b )); then order="dec"; fi
          if send_exact_modded "$fan_id" "$v255" "$vmin" "$order"; then
            ((updates_sent++))
            updated_pids+=("$fan_id")
            LAST_SPEEDS[$fan]=$NEW
            LAST_BITS[$fan]=$NEW_B
          fi
        else
          LAST_SPEEDS[$fan]=$NEW
          LAST_BITS[$fan]=$NEW_B
        fi
      else
        # Percent-domain smoothing (default)
        DIFF=$(( desired - CURRENT ))
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
        # Skip unchanged to avoid spamming iLO
        if (( NEW != CURRENT )); then
          # min is 8 steps below max (clamped)
          local vmin=$(( v255 - 4 ))
          (( vmin < 1 )) && vmin=1
          local order="inc"
          if (( NEW < CURRENT )); then order="dec"; fi
          if send_exact_modded "$fan_id" "$v255" "$vmin" "$order"; then
            ((updates_sent++))
            updated_pids+=("$fan_id")
            LAST_SPEEDS[$fan]=$NEW
            LAST_BITS[$fan]=$v255
          else
            :
          fi
        else
          LAST_SPEEDS[$fan]=$NEW
          # ensure bits snapshot stays in sync
          local v255_u=$(( (NEW * 255 + 50) / 100 ))
          (( v255_u < 1 )) && v255_u=1
          (( v255_u > 255 )) && v255_u=255
          LAST_BITS[$fan]=$v255_u
        fi
      fi
    done
  else
    for fan in "${FAN_IDS[@]}"; do
      CURRENT=${LAST_SPEEDS[$fan]:-20}
      local desired
      if [[ ${overrides[$fan]+_} ]]; then
        desired=${overrides[$fan]}
      else
        desired=$default_target
      fi
      if [[ ! "$desired" =~ ^-?[0-9]+$ ]]; then
        desired=$default_target
      fi
      (( desired < 0 )) && desired=0
      (( desired > 100 )) && desired=100
      DIFF=$(( desired - CURRENT ))
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
      if (( NEW != CURRENT )); then
        if ! ilo_set_speed "$fan" "$NEW"; then
          echo "WARN: Failed to set $fan to $NEW" >&2
        fi
        # separation between per-fan sets only when we send a command
        sleep_ms "$ILO_CMD_GAP_MS"
        LAST_SPEEDS[$fan]=$NEW
        ((updates_sent++))
      fi
    done
  fi
  if [[ "$VERBOSE" == "1" ]]; then
    if [[ "$ILO_MODDED" == "1" ]]; then
      echo "[loop] updates_sent=$updates_sent pids=[${updated_pids[*]}]" >&2
    else
      echo "[loop] updates_sent=$updates_sent" >&2
    fi
  fi
  # Save current speeds (percent) and raw bits
  for fan in "${FAN_IDS[@]}"; do
    echo ${LAST_SPEEDS[$fan]}
  done > "$FAN_SPEED_FILE"
  for fan in "${FAN_IDS[@]}"; do
    echo ${LAST_BITS[$fan]:-0}
  done > "$FAN_SPEED_BITS_FILE"
}

# Main loop
while true; do
  CPU_TEMP=$(get_cpu_temp)
  GPU_TEMP=$(get_gpu_temp)
  # Sanitize temps
  [[ "$CPU_TEMP" =~ ^[0-9]+$ ]] || CPU_TEMP=0
  [[ "$GPU_TEMP" =~ ^[0-9]+$ ]] || GPU_TEMP=0
  now_epoch=$(date +%s)
  append_history CPU_HISTORY "$now_epoch" "$CPU_TEMP"
  append_history GPU_HISTORY "$now_epoch" "$GPU_TEMP"
  read CPU_FORECAST CPU_SLOPE <<< "$(forecast_temp CPU_HISTORY "$CPU_TEMP" "$PREDICT_HORIZON")"
  read GPU_FORECAST GPU_SLOPE <<< "$(forecast_temp GPU_HISTORY "$GPU_TEMP" "$PREDICT_HORIZON")"
  [[ -z "$CPU_FORECAST" ]] && CPU_FORECAST=$CPU_TEMP
  [[ -z "$CPU_SLOPE" ]] && CPU_SLOPE="0"
  [[ -z "$GPU_FORECAST" ]] && GPU_FORECAST=$GPU_TEMP
  [[ -z "$GPU_SLOPE" ]] && GPU_SLOPE="0"
  CPU_RATE_PER_MIN=$(awk -v s="$CPU_SLOPE" 'BEGIN { printf("%.4f\n", s * 60.0); }')
  GPU_RATE_PER_MIN=$(awk -v s="$GPU_SLOPE" 'BEGIN { printf("%.4f\n", s * 60.0); }')
  CPU_EFFECTIVE=$(blend_temp "$CPU_TEMP" "$CPU_FORECAST" "$PREDICT_BLEND")
  GPU_EFFECTIVE=$(blend_temp "$GPU_TEMP" "$GPU_FORECAST" "$PREDICT_GPU_BLEND")
  [[ -z "$CPU_EFFECTIVE" ]] && CPU_EFFECTIVE=$CPU_TEMP
  [[ -z "$GPU_EFFECTIVE" ]] && GPU_EFFECTIVE=$GPU_TEMP
  CPU_FEED_OFFSET=$(lead_adjust "$CPU_SLOPE" "$PREDICT_LEAD" "$PREDICT_SLOPE_GAIN" "$PREDICT_MAX_OFFSET")
  GPU_FEED_OFFSET=$(lead_adjust "$GPU_SLOPE" "$PREDICT_GPU_LEAD" "$PREDICT_GPU_SLOPE_GAIN" "$PREDICT_GPU_MAX_OFFSET")
  CPU_RATE_GAINED=$(rate_adjust "$CPU_RATE_PER_MIN" "$PREDICT_RATE_GAIN" "$PREDICT_RATE_MAX" "$PREDICT_RATE_DEADBAND" "$PREDICT_RATE_COOLDOWN")
  GPU_RATE_GAINED=$(rate_adjust "$GPU_RATE_PER_MIN" "$PREDICT_GPU_RATE_GAIN" "$PREDICT_GPU_RATE_MAX" "$PREDICT_GPU_RATE_DEADBAND" "$PREDICT_GPU_RATE_COOLDOWN")
  CPU_EFFECTIVE_AHEAD=$(awk -v base="$CPU_EFFECTIVE" -v off="$CPU_FEED_OFFSET" 'BEGIN { printf("%.2f\n", base + off); }')
  GPU_EFFECTIVE_AHEAD=$(awk -v base="$GPU_EFFECTIVE" -v off="$GPU_FEED_OFFSET" 'BEGIN { printf("%.2f\n", base + off); }')
  if [[ "$CPU_EFFECTIVE_AHEAD" =~ ^-?[0-9]+(\.[0-9]+)?$ ]]; then
    CPU_TEMP_FOR_TARGET=$(printf '%.0f\n' "$CPU_EFFECTIVE_AHEAD")
  else
    CPU_TEMP_FOR_TARGET=$CPU_TEMP
  fi
  if [[ "$GPU_EFFECTIVE_AHEAD" =~ ^-?[0-9]+(\.[0-9]+)?$ ]]; then
    GPU_TEMP_FOR_TARGET=$(printf '%.0f\n' "$GPU_EFFECTIVE_AHEAD")
  else
    GPU_TEMP_FOR_TARGET=$GPU_TEMP
  fi
  read CPU_TARGET GPU_TARGET < <(calc_targets "$CPU_TEMP_FOR_TARGET" "$GPU_TEMP_FOR_TARGET")
  # Determine target for system fans (fan2, fan3) — default to CPU only
  SYSTEM_MODE="cpu"
  SYS_CW="1"
  SYS_GW="0"
  if command -v jq >/dev/null 2>&1; then
    SYSTEM_MODE=$(jq -r '(.systemFans.mode // "")' "$FAN_CURVE_FILE" 2>/dev/null | tr 'A-Z' 'a-z')
    SYS_CW=$(jq -r '(.systemFans.cpuWeight // "")' "$FAN_CURVE_FILE" 2>/dev/null || echo "")
    SYS_GW=$(jq -r '(.systemFans.gpuWeight // "")' "$FAN_CURVE_FILE" 2>/dev/null || echo "")
  fi
  [[ -z "$SYSTEM_MODE" || "$SYSTEM_MODE" == "null" ]] && SYSTEM_MODE="cpu"
  [[ "$SYS_CW" =~ ^-?[0-9]+(\.[0-9]+)?$ ]] || SYS_CW="1"
  [[ "$SYS_GW" =~ ^-?[0-9]+(\.[0-9]+)?$ ]] || SYS_GW="0"
  case "$SYSTEM_MODE" in
    cpu)
      SYSTEM_TARGET=$CPU_TARGET
      ;;
    gpu|gpu_only)
      SYSTEM_TARGET=$GPU_TARGET
      ;;
    weighted)
      if command -v awk >/dev/null 2>&1; then
        sum=$(awk -v cw="$SYS_CW" -v gw="$SYS_GW" 'BEGIN{printf("%f", cw+gw)}')
        if awk -v s="$sum" 'BEGIN{exit (s==0)}'; then
          weighted=$(awk -v c="$CPU_TARGET" -v g="$GPU_TARGET" -v cw="$SYS_CW" -v gw="$SYS_GW" 'BEGIN{sum=cw+gw; best=(c*cw + g*gw)/sum; if(best<0)best=0; if(best>100)best=100; printf("%d\n", int(best+0.5)); }')
        else
          weighted=$(( CPU_TARGET > GPU_TARGET ? CPU_TARGET : GPU_TARGET ))
        fi
        if [[ "$weighted" =~ ^[0-9]+$ ]]; then
          SYSTEM_TARGET=$weighted
        else
          SYSTEM_TARGET=$CPU_TARGET
        fi
      else
        SYSTEM_TARGET=$CPU_TARGET
      fi
      ;;
    max|*)
      SYSTEM_TARGET=$CPU_TARGET
      ;;
  esac
  if [[ "$CPU_RATE_GAINED" =~ ^-?[0-9]+(\.[0-9]+)?$ ]]; then
    CPU_RATE_DELTA=$(awk -v v="$CPU_RATE_GAINED" 'BEGIN { if (!(v == v)) { print 0; exit } if (v >= 0) { printf("%d\n", int(v + 0.5)); } else { printf("%d\n", int(v - 0.5)); } }')
    [[ -z "$CPU_RATE_DELTA" ]] && CPU_RATE_DELTA=0
    SYSTEM_TARGET=$(( SYSTEM_TARGET + CPU_RATE_DELTA ))
  fi

  # Dedicated GPU fan control (fan4) — isolate to GPU metrics only
  GPU_OFFSET="0"
  GPU_MIN=""
  GPU_MAX=""
  if command -v jq >/dev/null 2>&1; then
    GPU_OFFSET=$(jq -r '(.gpuFan.offset // .perFan.fan4.offset // 0)' "$FAN_CURVE_FILE" 2>/dev/null || echo 0)
    GPU_MIN=$(jq -r '(.gpuFan.min // .perFan.fan4.min // empty)' "$FAN_CURVE_FILE" 2>/dev/null || echo "")
    GPU_MAX=$(jq -r '(.gpuFan.max // .perFan.fan4.max // empty)' "$FAN_CURVE_FILE" 2>/dev/null || echo "")
  fi
  [[ "$GPU_OFFSET" =~ ^-?[0-9]+$ ]] || GPU_OFFSET="0"
  [[ "$GPU_MIN" =~ ^-?[0-9]+$ ]] || GPU_MIN=""
  [[ "$GPU_MAX" =~ ^-?[0-9]+$ ]] || GPU_MAX=""
  GPU_FAN_TARGET=$GPU_TARGET
  if [[ "$GPU_OFFSET" =~ ^-?[0-9]+$ ]]; then
    GPU_FAN_TARGET=$(( GPU_FAN_TARGET + GPU_OFFSET ))
  fi
  if [[ -n "$GPU_MIN" ]]; then
    if (( GPU_FAN_TARGET < GPU_MIN )); then
      GPU_FAN_TARGET=$GPU_MIN
    fi
  fi
  if [[ -n "$GPU_MAX" ]]; then
    if (( GPU_FAN_TARGET > GPU_MAX )); then
      GPU_FAN_TARGET=$GPU_MAX
    fi
  fi
  if [[ "$GPU_RATE_GAINED" =~ ^-?[0-9]+(\.[0-9]+)?$ ]]; then
    GPU_RATE_DELTA=$(awk -v v="$GPU_RATE_GAINED" 'BEGIN { if (!(v == v)) { print 0; exit } if (v >= 0) { printf("%d\n", int(v + 0.5)); } else { printf("%d\n", int(v - 0.5)); } }')
    [[ -z "$GPU_RATE_DELTA" ]] && GPU_RATE_DELTA=0
    GPU_FAN_TARGET=$(( GPU_FAN_TARGET + GPU_RATE_DELTA ))
  fi

  # Optional GPU boost when GPU temp exceeds threshold
  GB_T=$(jq -r '(.gpuBoost.threshold // empty)' "$FAN_CURVE_FILE" 2>/dev/null || true)
  GB_A=$(jq -r '(.gpuBoost.add // 0)' "$FAN_CURVE_FILE" 2>/dev/null || echo 0)
  if [[ -n "$GB_T" && "$GB_T" =~ ^[0-9]+$ && "$GPU_TEMP" -ge "$GB_T" ]]; then
    GPU_FAN_TARGET=$(( GPU_FAN_TARGET + GB_A ))
  fi
  (( SYSTEM_TARGET < 0 )) && SYSTEM_TARGET=0
  (( SYSTEM_TARGET > 100 )) && SYSTEM_TARGET=100
  (( GPU_FAN_TARGET < 0 )) && GPU_FAN_TARGET=0
  (( GPU_FAN_TARGET > 100 )) && GPU_FAN_TARGET=100
  if [[ "$PREDICT_DEADBAND" =~ ^[0-9]+$ && "$PREDICT_DEADBAND" -gt 0 ]]; then
    prev_sum=0
    prev_count=0
    for fan in "${FAN_IDS[@]}"; do
      [[ "$fan" == "fan4" ]] && continue
      prev_val=${LAST_SPEEDS[$fan]:-}
      if [[ "$prev_val" =~ ^-?[0-9]+$ ]]; then
        prev_sum=$(( prev_sum + prev_val ))
        (( prev_count++ ))
      fi
    done
    if (( prev_count > 0 )); then
      prev_avg=$(( prev_sum / prev_count ))
      diff=$(( SYSTEM_TARGET - prev_avg ))
      (( diff < 0 )) && diff=$(( -diff ))
      if (( diff <= PREDICT_DEADBAND )); then
        SYSTEM_TARGET=$prev_avg
      fi
    fi
  fi
  if [[ "$PREDICT_GPU_DEADBAND" =~ ^[0-9]+$ && "$PREDICT_GPU_DEADBAND" -gt 0 ]]; then
    prev_gpu=${LAST_SPEEDS[fan4]:-}
    if [[ "$prev_gpu" =~ ^-?[0-9]+$ ]]; then
      gdiff=$(( GPU_FAN_TARGET - prev_gpu ))
      (( gdiff < 0 )) && gdiff=$(( -gdiff ))
      if (( gdiff <= PREDICT_GPU_DEADBAND )); then
        GPU_FAN_TARGET=$prev_gpu
      fi
    fi
  fi
  [[ "$VERBOSE" == "1" ]] && echo "[loop] CPU=$CPU_TEMP (drive=$CPU_TEMP_FOR_TARGET forecast=$CPU_FORECAST slope=$CPU_SLOPE rate=${CPU_RATE_PER_MIN}/m feed=$CPU_FEED_OFFSET fanRate=$CPU_RATE_GAINED) GPU=$GPU_TEMP (drive=$GPU_TEMP_FOR_TARGET forecast=$GPU_FORECAST slope=$GPU_SLOPE rate=${GPU_RATE_PER_MIN}/m feed=$GPU_FEED_OFFSET fanRate=$GPU_RATE_GAINED) -> system=$SYSTEM_TARGET gpuFan=$GPU_FAN_TARGET" >&2
  apply_fan_speed "$SYSTEM_TARGET" "fan4=$GPU_FAN_TARGET"
  # Heartbeat for diagnostics
  echo "CPU=${CPU_TEMP}C/${CPU_TEMP_FOR_TARGET}C f=${CPU_FORECAST} s=${CPU_SLOPE} rate=${CPU_RATE_PER_MIN}/m feed=${CPU_FEED_OFFSET} fanRate=${CPU_RATE_GAINED} GPU=${GPU_TEMP}C/${GPU_TEMP_FOR_TARGET}C f=${GPU_FORECAST} s=${GPU_SLOPE} rate=${GPU_RATE_PER_MIN}/m feed=${GPU_FEED_OFFSET} fanRate=${GPU_RATE_GAINED} system=${SYSTEM_TARGET}% gpuFan=${GPU_FAN_TARGET}%" >&2
  # Optional runtime overrides from JSON
  if command -v jq >/dev/null 2>&1; then
    CI=$(jq -r '(.checkInterval // empty)' "$FAN_CURVE_FILE" 2>/dev/null || true)
    MS=$(jq -r '(.maxStep // empty)' "$FAN_CURVE_FILE" 2>/dev/null || true)
    [[ -n "$CI" && "$CI" =~ ^[0-9]+$ ]] && CHECK_INTERVAL=$CI
    [[ -n "$MS" && "$MS" =~ ^-?[0-9]+$ ]] && MAX_STEP=$MS
    if read -r PH PM PHIST PW PB PGB PD PGD PL PGLD PGN PGNG PMO PGMO PR PGPR PRD PGDR PRM PGMR PRC PGCR < <(jq -r '[.predict.horizon, .predict.minPoints, .predict.history, .predict.window, .predict.blend, .predict.gpuBlend, .predict.deadband, .predict.gpuDeadband, .predict.lead, .predict.gpuLead, .predict.slopeGain, .predict.gpuSlopeGain, .predict.maxOffset, .predict.gpuMaxOffset, .predict.rateGain, .predict.gpuRateGain, .predict.rateDeadband, .predict.gpuRateDeadband, .predict.rateMax, .predict.gpuRateMax, .predict.rateCooldown, .predict.gpuRateCooldown] | map(if . == null then "" else tostring end) | @tsv' "$FAN_CURVE_FILE" 2>/dev/null); then
      [[ -n "$PH" && "$PH" =~ ^[0-9]+$ ]] && PREDICT_HORIZON=$PH
      [[ -n "$PM" && "$PM" =~ ^[0-9]+$ ]] && PREDICT_MIN_POINTS=$PM
      [[ -n "$PHIST" && "$PHIST" =~ ^[0-9]+$ ]] && PREDICT_HISTORY=$PHIST
      [[ -n "$PW" && "$PW" =~ ^[0-9]+$ ]] && PREDICT_WINDOW=$PW
      if [[ -n "$PB" && "$PB" =~ ^-?[0-9]+(\.[0-9]+)?$ ]]; then
        PREDICT_BLEND=$PB
      fi
      if [[ -n "$PGB" && "$PGB" =~ ^-?[0-9]+(\.[0-9]+)?$ ]]; then
        PREDICT_GPU_BLEND=$PGB
      fi
      [[ -n "$PD" && "$PD" =~ ^[0-9]+$ ]] && PREDICT_DEADBAND=$PD
      [[ -n "$PGD" && "$PGD" =~ ^[0-9]+$ ]] && PREDICT_GPU_DEADBAND=$PGD
      if [[ -n "$PL" && "$PL" =~ ^-?[0-9]+(\.[0-9]+)?$ ]]; then
        PREDICT_LEAD=$PL
      fi
      if [[ -n "$PGLD" && "$PGLD" =~ ^-?[0-9]+(\.[0-9]+)?$ ]]; then
        PREDICT_GPU_LEAD=$PGLD
      fi
      if [[ -n "$PGN" && "$PGN" =~ ^-?[0-9]+(\.[0-9]+)?$ ]]; then
        PREDICT_SLOPE_GAIN=$PGN
      fi
      if [[ -n "$PGNG" && "$PGNG" =~ ^-?[0-9]+(\.[0-9]+)?$ ]]; then
        PREDICT_GPU_SLOPE_GAIN=$PGNG
      fi
      [[ -n "$PMO" && "$PMO" =~ ^-?[0-9]+(\.[0-9]+)?$ ]] && PREDICT_MAX_OFFSET=$PMO
      [[ -n "$PGMO" && "$PGMO" =~ ^-?[0-9]+(\.[0-9]+)?$ ]] && PREDICT_GPU_MAX_OFFSET=$PGMO
      if [[ -n "$PR" && "$PR" =~ ^-?[0-9]+(\.[0-9]+)?$ ]]; then
        PREDICT_RATE_GAIN=$PR
      fi
      if [[ -n "$PGPR" && "$PGPR" =~ ^-?[0-9]+(\.[0-9]+)?$ ]]; then
        PREDICT_GPU_RATE_GAIN=$PGPR
      fi
      if [[ -n "$PRD" && "$PRD" =~ ^-?[0-9]+(\.[0-9]+)?$ ]]; then
        PREDICT_RATE_DEADBAND=$PRD
      fi
      if [[ -n "$PGDR" && "$PGDR" =~ ^-?[0-9]+(\.[0-9]+)?$ ]]; then
        PREDICT_GPU_RATE_DEADBAND=$PGDR
      fi
      if [[ -n "$PRM" && "$PRM" =~ ^-?[0-9]+(\.[0-9]+)?$ ]]; then
        PREDICT_RATE_MAX=$PRM
      fi
      if [[ -n "$PGMR" && "$PGMR" =~ ^-?[0-9]+(\.[0-9]+)?$ ]]; then
        PREDICT_GPU_RATE_MAX=$PGMR
      fi
      if [[ -n "$PRC" && "$PRC" =~ ^-?[0-9]+(\.[0-9]+)?$ ]]; then
        PREDICT_RATE_COOLDOWN=$PRC
      fi
      if [[ -n "$PGCR" && "$PGCR" =~ ^-?[0-9]+(\.[0-9]+)?$ ]]; then
        PREDICT_GPU_RATE_COOLDOWN=$PGCR
      fi
    fi
  fi
  sleep $CHECK_INTERVAL
done
