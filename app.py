from flask import Flask, render_template, request, redirect, jsonify
import json, os, subprocess, re, time, threading, shutil
from collections import deque
from typing import Optional, List, Tuple

app = Flask(__name__)

# Default to server install paths; can be overridden by env
FAN_CURVE_FILE = os.getenv("FAN_CURVE_FILE", "/opt/dynamic-fan-ui/fan_curve.json")
FAN_SPEED_FILE = os.getenv("FAN_SPEED_FILE", "/opt/dynamic-fan-ui/fan_speeds.txt")
UI_CONFIG_FILE = os.getenv("UI_CONFIG_FILE", "/opt/dynamic-fan-ui/ui_config.json")
FAN_IDS = ["fan2", "fan3", "fan4"]
# Allow override via env string: FAN_IDS_STR="fan2 fan3 fan4"
_ids_env = os.getenv("FAN_IDS_STR", "").strip()
if _ids_env:
    FAN_IDS = [x for x in _ids_env.split() if x]

# iLO/IPMI/GPU environment toggles (optional)
ILO_SSH_KEY = os.getenv("ILO_SSH_KEY", "/root/.ssh/ilo_key")
ILO_USER = os.getenv("ILO_USER", "admin")
ILO_IP = os.getenv("ILO_IP", "192.168.1.100")
ILO_PASSWORD = os.getenv("ILO_PASSWORD", "")
USE_IPMI_TEMPS = os.getenv("USE_IPMI_TEMPS", "0") == "1"
ILO_SSH_LEGACY = os.getenv("ILO_SSH_LEGACY", "0") == "1"
# Force modded mode fixed ON
ILO_MODDED = True
try:
    ILO_PID_OFFSET = int(os.getenv("ILO_PID_OFFSET", "-1"))
except Exception:
    ILO_PID_OFFSET = -1
ILO_FAN_PROP = os.getenv("ILO_FAN_PROP", "")
ILO_SSH_TTY = os.getenv("ILO_SSH_TTY", "1") == "1"  # some iLO shells require a TTY
try:
    ILO_SSH_TIMEOUT = int(os.getenv("ILO_SSH_TIMEOUT", "20"))
except Exception:
    ILO_SSH_TIMEOUT = 20
try:
    ILO_SSH_PERSIST = int(os.getenv("ILO_SSH_PERSIST", "60"))  # seconds to keep control connection alive
except Exception:
    ILO_SSH_PERSIST = 60
try:
    ILO_CMD_GAP_MS = int(os.getenv("ILO_CMD_GAP_MS", "75"))
except Exception:
    ILO_CMD_GAP_MS = 75
try:
    ILO_BATCH_SIZE = int(os.getenv("ILO_BATCH_SIZE", "1"))
except Exception:
    ILO_BATCH_SIZE = 1

# iLO fan object search paths and property candidates
FAN_PATHS = ["/system1", "/system1/fans1"]
PROP_CANDIDATES = ["speed", "pwm", "duty", "duty_cycle", "fan_speed", "percentage"]

# Optional explicit P-IDs for modded iLO: FAN_P_IDS_STR="1 2 3 4 5"
_pids_env = os.getenv("FAN_P_IDS_STR", "").strip()
_EXPLICIT_PIDS = [int(x) for x in _pids_env.split() if x.isdigit()] if _pids_env else []


def load_curve():
    # Base defaults
    data = {
        "minTemp": 30,
        "maxTemp": 80,
        "minSpeed": 20,
        "maxSpeed": 100,
        # Provide optional sections with safe defaults to satisfy older templates
        "blend": {"mode": "max", "cpuWeight": 0.5, "gpuWeight": 0.5},
        "gpu": {"minTemp": "", "maxTemp": "", "minSpeed": "", "maxSpeed": ""},
        "gpuBoost": {"threshold": "", "add": 0},
    }
    try:
        if os.path.exists(FAN_CURVE_FILE):
            with open(FAN_CURVE_FILE) as f:
                obj = json.load(f)
                if isinstance(obj, dict):
                    # Merge loaded values over defaults
                    for k, v in obj.items():
                        data[k] = v
    except Exception:
        pass
    # Ensure optional sections remain dicts with required keys
    if not isinstance(data.get("blend"), dict):
        data["blend"] = {"mode": "max", "cpuWeight": 0.5, "gpuWeight": 0.5}
    else:
        data["blend"].setdefault("mode", "max")
        data["blend"].setdefault("cpuWeight", 0.5)
        data["blend"].setdefault("gpuWeight", 0.5)
    if not isinstance(data.get("gpu"), dict):
        data["gpu"] = {"minTemp": "", "maxTemp": "", "minSpeed": "", "maxSpeed": ""}
    else:
        for k in ("minTemp", "maxTemp", "minSpeed", "maxSpeed"):
            data["gpu"].setdefault(k, "")
    if not isinstance(data.get("gpuBoost"), dict):
        data["gpuBoost"] = {"threshold": "", "add": 0}
    else:
        data["gpuBoost"].setdefault("threshold", "")
        data["gpuBoost"].setdefault("add", 0)
    return data


def get_fan_speeds():
    vals: List[int] = []
    try:
        if os.path.exists(FAN_SPEED_FILE):
            with open(FAN_SPEED_FILE) as f:
                for line in f:
                    s = line.strip()
                    try:
                        vals.append(int(s))
                    except Exception:
                        vals.append(0)
    except Exception:
        vals = []
    if len(vals) < len(FAN_IDS):
        vals += [0] * (len(FAN_IDS) - len(vals))
    else:
        vals = vals[:len(FAN_IDS)]
    return vals


def _build_ssh_base():
    base = [
        "ssh",
        "-o", "StrictHostKeyChecking=no",
        # Fewer flakes: retry TCP connect and keep the session alive
        "-o", "ConnectionAttempts=3",
        "-o", "ServerAliveInterval=5",
        "-o", "ServerAliveCountMax=2",
        # Reuse a control connection to avoid per-command handshake overhead
        "-o", "ControlMaster=auto",
        "-o", "ControlPath=/tmp/ssh-ilo-%C",
        "-o", f"ControlPersist={ILO_SSH_PERSIST}",
    ]
    if ILO_SSH_LEGACY:
        base += [
            "-o", "KexAlgorithms=+diffie-hellman-group14-sha1,diffie-hellman-group1-sha1",
            "-o", "HostKeyAlgorithms=+ssh-rsa",
            "-o", "PubkeyAcceptedAlgorithms=+ssh-rsa",
            "-o", "PubkeyAcceptedKeyTypes=+ssh-rsa",
            "-o", "Ciphers=+aes128-cbc,3des-cbc",
            "-o", "MACs=+hmac-sha1",
        ]
    if ILO_SSH_KEY and os.path.exists(ILO_SSH_KEY):
        base += ["-i", ILO_SSH_KEY]
    if ILO_SSH_TTY or ILO_MODDED:
        base += ["-tt"]
    return base


# ---------------- iLO helpers: discovery and control ----------------
_detect_lock = threading.Lock()
_DISCOVERED_FANS: Optional[List[str]] = None
_DETECTED_PROP: Optional[str] = None
_DETECTED_PATH: Optional[str] = None
_COMPUTED_PIDS: Optional[List[int]] = None
_ILO_RECENT = deque(maxlen=200)


def _load_ui_config():
    try:
        if os.path.exists(UI_CONFIG_FILE):
            with open(UI_CONFIG_FILE) as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _apply_ui_overrides(cfg: Optional[dict] = None):
    global ILO_SSH_LEGACY, ILO_SSH_TTY, ILO_SSH_TIMEOUT, ILO_SSH_PERSIST
    global ILO_MODDED, ILO_PID_OFFSET, _EXPLICIT_PIDS, ILO_FAN_PROP
    if cfg is None:
        cfg = _load_ui_config()
    if not isinstance(cfg, dict):
        return
    try:
        if "ILO_SSH_LEGACY" in cfg:
            ILO_SSH_LEGACY = bool(int(cfg["ILO_SSH_LEGACY"]))
        if "ILO_SSH_TTY" in cfg:
            ILO_SSH_TTY = bool(int(cfg["ILO_SSH_TTY"]))
        if "ILO_SSH_TIMEOUT" in cfg:
            ILO_SSH_TIMEOUT = int(cfg["ILO_SSH_TIMEOUT"])
        if "ILO_SSH_PERSIST" in cfg:
            ILO_SSH_PERSIST = int(cfg["ILO_SSH_PERSIST"])
        if "ILO_MODDED" in cfg:
            ILO_MODDED = bool(int(cfg["ILO_MODDED"]))
        if "ILO_PID_OFFSET" in cfg:
            ILO_PID_OFFSET = int(cfg["ILO_PID_OFFSET"])
        if "FAN_P_IDS_STR" in cfg:
            s = str(cfg["FAN_P_IDS_STR"]).strip()
            _EXPLICIT_PIDS = [int(x) for x in s.split() if x.isdigit()] if s else []
        if "ILO_FAN_PROP" in cfg:
            ILO_FAN_PROP = str(cfg["ILO_FAN_PROP"]).strip()
    except Exception:
        pass


# Apply any stored overrides on startup
_apply_ui_overrides()


def _ilo_run(cmd: str, timeout: Optional[int] = None) -> str:
    if timeout is None:
        timeout = ILO_SSH_TIMEOUT
    base = _build_ssh_base()
    ssh_cmd = base + [f"{ILO_USER}@{ILO_IP}", cmd]
    if ILO_PASSWORD:
        ssh_cmd = ["sshpass", "-p", ILO_PASSWORD] + ssh_cmd
    # Merge stderr for better diagnostics
    t0 = time.time()
    res = subprocess.run(ssh_cmd, text=True, timeout=timeout, capture_output=True)
    if res.returncode != 0:
        err = (res.stdout or "") + (res.stderr or "")
        _ILO_RECENT.append({"ts": time.time(), "cmd": cmd, "rc": res.returncode, "ms": int((time.time()-t0)*1000), "out": err[-2000:]})
        raise subprocess.CalledProcessError(res.returncode, ssh_cmd, output=err)
    out = res.stdout or ""
    _ILO_RECENT.append({"ts": time.time(), "cmd": cmd, "rc": 0, "ms": int((time.time()-t0)*1000), "out": (out + (res.stderr or ""))[-2000:]})
    return out


def _ilo_try(cmd: str, attempts: int = 2, timeout: Optional[int] = None) -> str:
    last_err: Optional[Exception] = None
    for i in range(max(1, attempts)):
        try:
            return _ilo_run(cmd, timeout=timeout)
        except Exception as e:
            last_err = e
            # Short backoff before retrying
            time.sleep(0.5)
            continue
    if last_err:
        raise last_err
    return ""


def _discover_fans() -> List[str]:
    global _DISCOVERED_FANS
    with _detect_lock:
        if _DISCOVERED_FANS is not None:
            return _DISCOVERED_FANS
        found: List[str] = []
        # Prefer concise 'fans show' listing if available
        try:
            out = _ilo_run("fans show")
            toks = re.findall(r"\bfan\d+\b", out.lower())
            if toks:
                _DISCOVERED_FANS = sorted(set(toks), key=lambda x: int(re.findall(r"\d+", x)[0]))
                return _DISCOVERED_FANS
        except Exception:
            pass
        for p in FAN_PATHS:
            try:
                out = _ilo_run(f"show {p}")
            except Exception:
                continue
            # Find tokens like fan1 fan2
            tokens = re.findall(r"\bfan\d+\b", out)
            if tokens:
                found = sorted(set(tokens), key=lambda x: int(re.findall(r"\d+", x)[0]))
                _DISCOVERED_FANS = found
                return found
        # fallback to configured list (clamped to three fans by FAN_IDS)
        _DISCOVERED_FANS = FAN_IDS[:]
        return _DISCOVERED_FANS


def _detect_fan_prop() -> Tuple[Optional[str], Optional[str]]:
    """Return (prop, path) to use for normal iLO percent setting, or (None,None)."""
    global _DETECTED_PROP, _DETECTED_PATH
    with _detect_lock:
        if _DETECTED_PROP is not None and _DETECTED_PATH is not None:
            return _DETECTED_PROP, _DETECTED_PATH
        # If user provided override, probe to confirm works on first fan
        fans = _discover_fans()
        sample = fans[0] if fans else "fan1"
        # Prefer explicit /system1/fanX read
        for cmd in (f"show /system1/{sample}", f"show -a /system1/{sample}"):
            try:
                out = _ilo_run(cmd)
                for line in out.splitlines():
                    if '=' in line or ':' in line:
                        parts = re.split(r"[:=]", line, maxsplit=1)
                        if len(parts) < 2:
                            continue
                        key = parts[0].strip().lower()
                        if any(k in key for k in ("speed","pwm","duty")):
                            _DETECTED_PROP, _DETECTED_PATH = key, "/system1"
                            return _DETECTED_PROP, _DETECTED_PATH
            except Exception:
                pass
        # Try 'fans X show' form
        mnum = re.search(r"(\d+)", sample)
        if mnum:
            try:
                out = _ilo_run(f"fans {mnum.group(1)} show")
                for line in out.splitlines():
                    if '=' in line or ':' in line:
                        parts = re.split(r"[:=]", line, maxsplit=1)
                        if len(parts) < 2:
                            continue
                        key = parts[0].strip().lower()
                        if any(k in key for k in ("speed","pwm","duty")):
                            _DETECTED_PROP, _DETECTED_PATH = key, "fans"
                            return _DETECTED_PROP, _DETECTED_PATH
            except Exception:
                pass
        if ILO_FAN_PROP:
            for prefix in FAN_PATHS:
                try:
                    # Attempt a no-op show to ensure path exists
                    _ilo_run(f"show {prefix}/{sample}")
                    _DETECTED_PROP, _DETECTED_PATH = ILO_FAN_PROP, prefix
                    return _DETECTED_PROP, _DETECTED_PATH
                except Exception:
                    continue
        # Otherwise attempt to read attributes and infer
        for prefix in FAN_PATHS:
            try:
                out = _ilo_run(f"show -a {prefix}/{sample}")
            except Exception:
                try:
                    out = _ilo_run(f"show {prefix}/{sample}")
                except Exception:
                    continue
            # Look for key=value with candidate names and plausible 0..100 values
            for line in out.splitlines():
                if '=' in line or ':' in line:
                    parts = re.split(r"[:=]", line, maxsplit=1)
                    if len(parts) < 2:
                        continue
                    key = parts[0].strip().lower()
                    if any(k in key for k in ("speed", "pwm", "duty")):
                        m = re.search(r"\b(\d{1,3})\b", parts[1])
                        if m:
                            v = int(m.group(1))
                            if 0 <= v <= 100:
                                _DETECTED_PROP, _DETECTED_PATH = key, prefix
                                return _DETECTED_PROP, _DETECTED_PATH
            # As a fallback, try known candidates
            for prop in PROP_CANDIDATES:
                try:
                    # Try a harmless set to the same value if we can read one
                    out = _ilo_run(f"show {prefix}/{sample}")
                    m = re.search(r"\b(\d{1,3})\b", out)
                    if not m:
                        continue
                    v = int(m.group(1))
                    _ilo_run(f"set {prefix}/{sample} {prop}={v}")
                    _DETECTED_PROP, _DETECTED_PATH = prop, prefix
                    return _DETECTED_PROP, _DETECTED_PATH
                except Exception:
                    continue
        return None, None


def _compute_pids(fans: List[str]) -> List[int]:
    global _COMPUTED_PIDS
    with _detect_lock:
        if _COMPUTED_PIDS is not None:
            return _COMPUTED_PIDS
        if _EXPLICIT_PIDS:
            _COMPUTED_PIDS = _EXPLICIT_PIDS
            return _COMPUTED_PIDS
        pids: List[int] = []
        for f in fans:
            m = re.search(r"(\d+)", f)
            if m:
                # Hard-code identity mapping: fanN -> P-ID N
                pid = int(m.group(1))
            else:
                pid = 0
            if pid < 0:
                pid = 0
            pids.append(pid)
        # Mapping per user: fan2->p1, fan3->p2, fan4->p4
        mapping = []
        for f in fans:
            if f == "fan2": mapping.append(1)
            elif f == "fan3": mapping.append(2)
            elif f == "fan4": mapping.append(4)
        _COMPUTED_PIDS = mapping
        return _COMPUTED_PIDS


def ilo_set_speed_percent_normal(fan: str, percent: int) -> bool:
    """Set percent via property on fan object."""
    global _DETECTED_PROP, _DETECTED_PATH
    prop, path = _detect_fan_prop()
    candidates = [prop] if prop else []
    for c in PROP_CANDIDATES:
        if c not in candidates:
            candidates.append(c)
    for pr in candidates:
        for prefix in (path,)+tuple(p for p in FAN_PATHS if p != path):
            if not prefix:
                continue
            try:
                _ilo_run(f"set {prefix}/{fan} {pr}={percent}")
                # Cache success
                with _detect_lock:
                    _DETECTED_PROP, _DETECTED_PATH = pr, prefix
                return True
            except Exception:
                continue
    # Fallback to 'fans X' CLI forms if fan name contains a number
    m = re.search(r"(\d+)", fan)
    if m:
        num = m.group(1)
        for pr in candidates:
            try:
                _ilo_run(f"fans {num} {pr}={percent}")
                with _detect_lock:
                    _DETECTED_PROP, _DETECTED_PATH = pr, "fans"
                return True
            except Exception:
                try:
                    _ilo_run(f"fans {num} set {pr} {percent}")
                    with _detect_lock:
                        _DETECTED_PROP, _DETECTED_PATH = pr, "fans"
                    return True
                except Exception:
                    continue
    return False


def ilo_set_speed_percent_modded(pid: int, percent: int) -> bool:
    """Set exact fan using separate max then min commands (1..255) for modded iLO."""
    v = max(0, min(100, int(percent)))
    v255 = max(1, min(255, (v * 255 + 50) // 100))
    vmin = max(1, v255 - 8)  # min 8 steps below max to avoid iLO quirks
    ok = True
    try:
        _ilo_run(f"fan p {pid} max {v255}")
    except Exception:
        ok = False
    try:
        # small delay to help iLO apply sequentially
        time.sleep(0.1)
        _ilo_run(f"fan p {pid} min {vmin}")
    except Exception:
        ok = False
    return ok


def get_temps():
    cpu_temp = ""
    # CPU from IPMI or iLO sensors
    if USE_IPMI_TEMPS:
        try:
            pw = f"-P {ILO_PASSWORD}" if ILO_PASSWORD else ""
            # Escape braces in awk with double {{ }} inside f-string
            cmd_str = (
                f"ipmitool -I lanplus -H {ILO_IP} -U {ILO_USER} {pw} sdr type Temperature "
                "| awk -F'|' '/CPU/ {{ if (match($2, /[0-9]+/, m)) print m[0]; }}' | sort -nr | head -1"
            )
            cmd = ["bash", "-lc", cmd_str]
            cpu_temp = subprocess.check_output(cmd, text=True, timeout=3).strip()
        except Exception:
            cpu_temp = ""
    if not cpu_temp:
        try:
            # iLO CPU temp is typically under /system1/sensor2, parse CurrentReading
            out = _ilo_run("show /system1/sensor2")
            m = re.search(r"CurrentReading\s*=\s*(\d{1,3})", out, re.IGNORECASE)
            cpu_temp = m.group(1) if m else ""
        except Exception:
            cpu_temp = ""
    # GPU via nvidia-smi
    gpu_temp = ""; gpu_name = ""; gpu_power = ""
    try:
        out = subprocess.check_output(
            "nvidia-smi --query-gpu=temperature.gpu,name,power.draw --format=csv,noheader,nounits",
            shell=True, text=True, timeout=3
        )
        best_t = -1
        for line in out.splitlines():
            parts = [p.strip() for p in line.split(',')]
            if parts and parts[0].isdigit():
                t = int(parts[0])
                if t > best_t:
                    best_t = t
                    gpu_temp = str(t)
                    gpu_name = parts[1] if len(parts) > 1 else ""
                    gpu_power = parts[2] if len(parts) > 2 else ""
    except Exception:
        pass
    return cpu_temp, gpu_temp, gpu_name, gpu_power


@app.route("/")
def index():
    # Provide current settings for UI
    settings = {
        "ILO_SSH_LEGACY": 1 if ILO_SSH_LEGACY else 0,
        "ILO_SSH_TTY": 1 if ILO_SSH_TTY else 0,
        "ILO_SSH_TIMEOUT": ILO_SSH_TIMEOUT,
        "ILO_SSH_PERSIST": ILO_SSH_PERSIST,
        "ILO_MODDED": 1 if ILO_MODDED else 0,
        "ILO_PID_OFFSET": ILO_PID_OFFSET,
        "FAN_P_IDS_STR": " ".join(str(x) for x in _EXPLICIT_PIDS) if _EXPLICIT_PIDS else "",
        "ILO_FAN_PROP": ILO_FAN_PROP or "",
        "ILO_CMD_GAP_MS": ILO_CMD_GAP_MS,
        "ILO_BATCH_SIZE": ILO_BATCH_SIZE,
    }
    return render_template(
        "index.html",
        fan_curve=load_curve(),
        fan_speeds=get_fan_speeds(),
        cpu_temp="",
        gpu_temp="",
        settings=settings,
    )


@app.route("/status")
def status():
    cpu, gpu, gpu_name, gpu_power = get_temps()
    sensors = []
    # Collect a few sensors (best-effort)
    try:
        if USE_IPMI_TEMPS:
            out = subprocess.check_output(
                ["bash", "-lc", f"ipmitool -I lanplus -H {ILO_IP} -U {ILO_USER} {'-P '+ILO_PASSWORD if ILO_PASSWORD else ''} sdr type Temperature"],
                text=True, timeout=3
            )
            for line in out.splitlines():
                parts = [p.strip() for p in line.split('|')]
                if len(parts) >= 2 and 'CPU' not in parts[0]:
                    m = re.search(r"(\d+)", parts[1])
                    if m:
                        sensors.append({"label": parts[0], "value": m.group(1)})
                if len(sensors) >= 5:
                    break
        else:
            try:
                stext = _ilo_run("show /system1/sensor2")
                m = re.search(r"CurrentReading\s*=\s*(\d{1,3})", stext, re.IGNORECASE)
                if m:
                    sensors.append({"label": "CPU (sensor2)", "value": m.group(1)})
            except Exception:
                pass
    except Exception:
        sensors = []

    # Detailed GPU info (best-effort)
    gpu_info = []
    try:
        fields = (
            "index,name,pci.bus_id,temperature.gpu,utilization.gpu,utilization.memory,"
            "utilization.encoder,utilization.decoder,memory.total,memory.used,power.draw,power.limit,"
            "pstate,fan.speed,clocks.gr,clocks.mem,clocks.video,pcie.link.gen.current,pcie.link.gen.max,"
            "pcie.link.width.current,pcie.link.width.max"
        )
        base = subprocess.check_output(
            f"nvidia-smi --query-gpu={fields} --format=csv,noheader,nounits",
            shell=True, text=True, timeout=3
        )
        for line in base.splitlines():
            p = [q.strip() for q in line.split(',')]
            if len(p) < 20:
                continue
            gpu_info.append({
                "index": p[0], "name": p[1], "bus_id": p[2], "temperature": p[3],
                "util_gpu": p[4], "util_mem": p[5], "util_enc": p[6] if len(p)>6 else "",
                "util_dec": p[7] if len(p)>7 else "", "mem_total": p[8], "mem_used": p[9],
                "power_draw": p[10], "power_limit": p[11], "pstate": p[12], "fan_speed": p[13],
                "clocks_gr": p[14], "clocks_mem": p[15], "clocks_video": p[16],
                "pcie_gen_cur": p[17], "pcie_gen_max": p[18],
                "pcie_width_cur": p[19] if len(p)>19 else "", "pcie_width_max": p[20] if len(p)>20 else "",
            })
    except Exception:
        gpu_info = []

    # NVENC sessions (best-effort, may require supported driver)
    try:
        enc = subprocess.check_output(
            "nvidia-smi encodersessions -q 2>/dev/null | cat",
            shell=True, text=True, timeout=3
        )
        # Count sessions per bus id
        current_bus = None
        counts = {}
        for line in enc.splitlines():
            m = re.search(r"GPU\s+([0-9a-fA-F:]+)", line)
            if m:
                current_bus = m.group(1)
                counts.setdefault(current_bus, 0)
            if "Session Id" in line and current_bus:
                counts[current_bus] = counts.get(current_bus, 0) + 1
        if counts and gpu_info:
            for g in gpu_info:
                g["encoder_sessions"] = counts.get(g.get("bus_id"), 0)
    except Exception:
        pass

    # Service status (best-effort)
    svc = {"present": False, "active": False}
    try:
        if shutil.which("systemctl"):
            out = subprocess.check_output(["systemctl", "is-active", "dynamic-fans.service"], text=True, timeout=2).strip()
            svc = {"present": True, "active": (out == "active")}
    except Exception:
        pass

    return jsonify({
        "cpu": cpu, "gpu": gpu, "gpu_name": gpu_name, "gpu_power": gpu_power,
        "fans": get_fan_speeds(), "sensors": sensors, "gpu_info": gpu_info,
        "service": svc, "ok": True,
    })


@app.route("/update_curve", methods=["POST"])
def update_curve():
    curve = load_curve()
    for key in ("minTemp", "maxTemp", "minSpeed", "maxSpeed"):
        v = request.form.get(key)
        if v is not None and v != "":
            try:
                curve[key] = int(v)
            except Exception:
                pass
    # Optional GPU curve
    if all(k in request.form for k in ("gpu_minTemp","gpu_maxTemp","gpu_minSpeed","gpu_maxSpeed")):
        try:
            curve["gpu"] = {
                "minTemp": int(request.form.get("gpu_minTemp") or 0),
                "maxTemp": int(request.form.get("gpu_maxTemp") or 0),
                "minSpeed": int(request.form.get("gpu_minSpeed") or 0),
                "maxSpeed": int(request.form.get("gpu_maxSpeed") or 0),
            }
        except Exception:
            pass
    # Optional blending
    try:
        mode = request.form.get("blend_mode", "max")
        cw = float(request.form.get("blend_cpuWeight", "0.5"))
        gw = float(request.form.get("blend_gpuWeight", "0.5"))
        curve["blend"] = {"mode": mode, "cpuWeight": cw, "gpuWeight": gw}
    except Exception:
        pass
    # Optional GPU boost
    try:
        t = request.form.get("gpuBoost_threshold")
        a = request.form.get("gpuBoost_add")
        boost = {}
        if t not in (None, ""): boost["threshold"] = int(t)
        if a not in (None, ""): boost["add"] = int(a)
        if boost: curve["gpuBoost"] = boost
    except Exception:
        pass
    # Optional runtime tunables used by daemon
    try:
        ci = request.form.get("checkInterval")
        ms = request.form.get("maxStep")
        mc = request.form.get("minChange")
        if ci not in (None, ""):
            curve["checkInterval"] = int(ci)
        if ms not in (None, ""):
            curve["maxStep"] = int(ms)
        if mc not in (None, ""):
            curve["minChange"] = int(mc)
    except Exception:
        pass
    try:
        with open(FAN_CURVE_FILE, "w") as f:
            json.dump(curve, f)
    except Exception:
        pass
    return redirect("/")


@app.route("/control", methods=["POST"])
def control():
    action = request.form.get("action", "")
    if shutil.which("systemctl"):
        if action == "start":
            subprocess.Popen(["systemctl", "start", "dynamic-fans.service"])  # async
        elif action == "stop":
            subprocess.Popen(["systemctl", "stop", "dynamic-fans.service"])
    return redirect("/")


_test_lock = threading.Lock()
_test_running = False


def _run_quick_test(percent: int, duration: int):
    global _test_running
    with _test_lock:
        if _test_running:
            return
        _test_running = True
    try:
        # Stop control loop so it doesn't fight the test
        if shutil.which("systemctl"):
            try:
                subprocess.check_output(["systemctl", "stop", "dynamic-fans.service"], text=True, timeout=10)
                # Wait briefly for the process to actually exit
                for _ in range(10):
                    try:
                        state = subprocess.check_output(["systemctl", "is-active", "dynamic-fans.service"], text=True, timeout=2).strip()
                        if state != "active":
                            break
                    except Exception:
                        break
                    time.sleep(0.2)
            except Exception:
                pass
        # Discover fans and optionally P-IDs
        fans = _discover_fans()
        # Apply requested percent
        if ILO_MODDED:
            pids = _compute_pids(fans)
            def pct_to_255(p: int) -> int:
                v = max(0, min(100, int(p)))
                return max(1, min(255, (v * 255 + 50) // 100))
            v255 = pct_to_255(percent)
            vmin = max(1, v255 - 8)
            # Send separate commands per PID with small pacing gap (matches working pattern)
            for pid in pids:
                try:
                    _ilo_run(f"fan p {pid} max {v255}", timeout=8)
                except Exception:
                    pass
                try:
                    time.sleep(max(0.0, ILO_CMD_GAP_MS/1000.0))
                except Exception:
                    pass
                try:
                    _ilo_run(f"fan p {pid} min {vmin}", timeout=8)
                except Exception:
                    pass
                try:
                    time.sleep(max(0.0, ILO_CMD_GAP_MS/1000.0))
                except Exception:
                    pass
        else:
            # Try detected prop/path; fallback across candidates
            prop, path = _detect_fan_prop()
            candidates = [prop] if prop else []
            for c in ["speed","pwm","duty","duty_cycle","fan_speed","percentage"]:
                if c not in candidates:
                    candidates.append(c)
            for fan in fans:
                ok = False
                for pr in candidates:
                    for prefix in ([path] if path else []) + [p for p in ["/system1","/system1/fans1"] if p != path]:
                        try:
                            _ilo_run(f"set {prefix}/{fan} {pr}={int(percent)}", timeout=4)
                            ok = True
                            break
                        except Exception:
                            continue
                    if ok: break
        # Reflect immediately in speed file for UI
        try:
            speeds = [max(0, min(100, int(percent))) for _ in fans]
            os.makedirs(os.path.dirname(FAN_SPEED_FILE), exist_ok=True)
            with open(FAN_SPEED_FILE, "w") as f:
                for s in speeds:
                    f.write(str(s) + "\n")
        except Exception:
            pass
        # Hold for duration
        time.sleep(max(1, min(300, duration)))
        # Resume daemon which will take over speed management
        if shutil.which("systemctl"):
            subprocess.Popen(["systemctl", "start", "dynamic-fans.service"])  # resume
    finally:
        with _test_lock:
            _test_running = False


@app.route("/fan_test", methods=["POST"])
def fan_test():
    try:
        percent = int(request.form.get("test_percent", "100"))
        duration = int(request.form.get("test_duration", "10"))
    except Exception:
        percent, duration = 100, 10
    t = threading.Thread(target=_run_quick_test, args=(percent, duration), daemon=True)
    t.start()
    return redirect("/")


@app.route("/test_ilo")
def test_ilo():
    method = "ssh"
    try:
        out = _ilo_try("echo ok", attempts=3, timeout=int(ILO_SSH_TIMEOUT * 1.5))
        text = (out or "").strip().lower()
        ok_flag = ("ok" in text) or ("connection to" in text and "closed" in text)
        return jsonify({"ok": ok_flag, "method": ("sshpass" if ILO_PASSWORD else ("ssh-key" if (ILO_SSH_KEY and os.path.exists(ILO_SSH_KEY)) else "ssh")), "output": (out or "").strip()})
    except Exception as e:
        return jsonify({"ok": False, "method": method, "error": str(e)}), 500


@app.route("/export_status")
def export_status():
    return status()


@app.route("/export_curve")
def export_curve():
    try:
        with open(FAN_CURVE_FILE) as f:
            data = json.load(f)
    except Exception:
        data = {}
    return jsonify(data)


@app.route("/ilo_recent")
def ilo_recent():
    # Return recent iLO command interactions from the server side
    arr = list(_ILO_RECENT)
    for item in arr:
        # Render timestamps as ISO-ish strings
        item["time"] = time.strftime("%H:%M:%S", time.localtime(item.pop("ts", time.time())))
    return jsonify(arr)


@app.route("/settings", methods=["POST"])
def update_settings():
    # Server-side overrides; persists to UI_CONFIG_FILE and applies immediately to server use
    cfg = _load_ui_config()
    fields = [
        "ILO_SSH_LEGACY", "ILO_SSH_TTY", "ILO_SSH_TIMEOUT", "ILO_SSH_PERSIST",
        "ILO_MODDED", "ILO_PID_OFFSET", "FAN_P_IDS_STR", "ILO_FAN_PROP",
        "ILO_CMD_GAP_MS", "ILO_BATCH_SIZE"
    ]
    for k in fields:
        v = request.form.get(k)
        if v is not None:
            cfg[k] = v
    try:
        os.makedirs(os.path.dirname(UI_CONFIG_FILE), exist_ok=True)
        with open(UI_CONFIG_FILE, "w") as f:
            json.dump(cfg, f)
    except Exception:
        pass
    _apply_ui_overrides(cfg)
    return redirect("/")


@app.route("/logs")
def logs():
    # Try to show last 80 lines of the daemon logs
    try:
        if shutil.which("journalctl"):
            out = subprocess.check_output(["journalctl", "-u", "dynamic-fans.service", "-n", "80", "--no-pager"], text=True, timeout=5)
            return jsonify({"ok": True, "text": out})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})
    return jsonify({"ok": False, "error": "journalctl not available"})


@app.route("/test_ilo_control", methods=["POST"])
def test_ilo_control():
    """One-shot control test: set percent on either one fan (normal) or one PID (modded)."""
    try:
        percent = int(request.form.get("percent", "50"))
    except Exception:
        percent = 50
    target = request.form.get("target", "0")  # index of fan or pid
    result = {"ok": False, "percent": percent}
    try:
        if ILO_MODDED:
            fans = _discover_fans()
            pids = _compute_pids(fans)
            idx = int(target) if str(target).isdigit() else 0
            if idx < 0 or idx >= len(pids):
                idx = 0
            pid = pids[idx]
            ok = ilo_set_speed_percent_modded(pid, percent)
            result.update({"mode": "modded", "pid": pid, "fan_index": idx, "ok": ok})
        else:
            fans = _discover_fans()
            idx = int(target) if str(target).isdigit() else 0
            if idx < 0 or idx >= len(fans):
                idx = 0
            fan = fans[idx]
            ok = ilo_set_speed_percent_normal(fan, percent)
            result.update({"mode": "normal", "fan": fan, "fan_index": idx, "ok": ok})
    except Exception as e:
        result.update({"error": str(e)})
        return jsonify(result), 500
    return jsonify(result)


@app.route("/debug_ilo_fans")
def debug_ilo_fans():
    info = {
        "ilo_ip": ILO_IP,
        "ilo_user": ILO_USER,
        "legacy": ILO_SSH_LEGACY,
        "modded": ILO_MODDED,
        "pid_offset": ILO_PID_OFFSET,
        "fan_paths": FAN_PATHS,
        "prop_candidates": PROP_CANDIDATES,
        "env_prop": ILO_FAN_PROP or "",
        "configured_fans": FAN_IDS,
    }
    # Connectivity
    try:
        out = _ilo_run("echo ok", timeout=3).strip()
        info["ssh_ok"] = (out.lower().find("ok") != -1)
    except Exception as e:
        info["ssh_ok"] = False
        info["ssh_error"] = str(e)
    # Discovery
    try:
        fans = _discover_fans()
        info["discovered_fans"] = fans
    except Exception as e:
        info["discovered_fans_error"] = str(e)
        fans = FAN_IDS
    try:
        prop, path = _detect_fan_prop()
        info["detected_prop"] = prop
        info["detected_path"] = path
    except Exception as e:
        info["detected_prop_error"] = str(e)
    # P-IDs mapping
    try:
        info["pids"] = _compute_pids(fans)
    except Exception as e:
        info["pids_error"] = str(e)
    # Sensors sample
    try:
        s = _ilo_run("show /system1/sensors", timeout=5)
        info["sensors_preview"] = "\n".join(s.splitlines()[:20])
    except Exception:
        pass
    # Last speeds
    try:
        info["last_speeds"] = get_fan_speeds()
    except Exception:
        pass
    return jsonify(info)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
