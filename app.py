from flask import Flask, render_template, request, redirect, jsonify
import json, os, subprocess, re, time, threading, shutil

app = Flask(__name__)

# Default to server install paths; can be overridden by env
FAN_CURVE_FILE = os.getenv("FAN_CURVE_FILE", "/opt/dynamic-fan-ui/fan_curve.json")
FAN_SPEED_FILE = os.getenv("FAN_SPEED_FILE", "/opt/dynamic-fan-ui/fan_speeds.txt")
FAN_IDS = ["fan1", "fan2", "fan3", "fan4", "fan5"]
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
ILO_MODDED = os.getenv("ILO_MODDED", "0") == "1"
try:
    ILO_PID_OFFSET = int(os.getenv("ILO_PID_OFFSET", "-1"))
except Exception:
    ILO_PID_OFFSET = -1
ILO_FAN_PROP = os.getenv("ILO_FAN_PROP", "")
ILO_SSH_TTY = os.getenv("ILO_SSH_TTY", "1") == "1"  # some iLO shells require a TTY

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
    vals: list[int] = []
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
    base = ["ssh", "-o", "StrictHostKeyChecking=no"]
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
_DISCOVERED_FANS: list[str] | None = None
_DETECTED_PROP: str | None = None
_DETECTED_PATH: str | None = None
_COMPUTED_PIDS: list[int] | None = None


def _ilo_run(cmd: str, timeout: int = 4) -> str:
    base = _build_ssh_base()
    ssh_cmd = base + [f"{ILO_USER}@{ILO_IP}", cmd]
    if ILO_PASSWORD:
        ssh_cmd = ["sshpass", "-p", ILO_PASSWORD] + ssh_cmd
    # Merge stderr for better diagnostics
    res = subprocess.run(ssh_cmd, text=True, timeout=timeout, capture_output=True)
    if res.returncode != 0:
        raise subprocess.CalledProcessError(res.returncode, ssh_cmd, output=res.stdout + res.stderr)
    return res.stdout


def _discover_fans() -> list[str]:
    global _DISCOVERED_FANS
    with _detect_lock:
        if _DISCOVERED_FANS is not None:
            return _DISCOVERED_FANS
        found: list[str] = []
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
        # fallback to configured list
        _DISCOVERED_FANS = FAN_IDS[:]
        return _DISCOVERED_FANS


def _detect_fan_prop() -> tuple[str | None, str | None]:
    """Return (prop, path) to use for normal iLO percent setting, or (None,None)."""
    global _DETECTED_PROP, _DETECTED_PATH
    with _detect_lock:
        if _DETECTED_PROP is not None and _DETECTED_PATH is not None:
            return _DETECTED_PROP, _DETECTED_PATH
        # If user provided override, probe to confirm works on first fan
        fans = _discover_fans()
        sample = fans[0] if fans else "fan1"
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


def _compute_pids(fans: list[str]) -> list[int]:
    global _COMPUTED_PIDS
    with _detect_lock:
        if _COMPUTED_PIDS is not None:
            return _COMPUTED_PIDS
        if _EXPLICIT_PIDS:
            _COMPUTED_PIDS = _EXPLICIT_PIDS
            return _COMPUTED_PIDS
        pids: list[int] = []
        for f in fans:
            m = re.search(r"(\d+)", f)
            if m:
                pid = int(m.group(1)) + (ILO_PID_OFFSET or 0)
            else:
                pid = 0
            if pid < 0:
                pid = 0
            pids.append(pid)
        _COMPUTED_PIDS = pids
        return _COMPUTED_PIDS


def ilo_set_speed_percent_normal(fan: str, percent: int) -> bool:
    """Set percent via property on fan object."""
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
                    global _DETECTED_PROP, _DETECTED_PATH
                    _DETECTED_PROP, _DETECTED_PATH = pr, prefix
                return True
            except Exception:
                continue
    return False


def ilo_set_speed_percent_modded(pid: int, percent: int) -> bool:
    """Set exact fan min=max in 1..255 domain for modded iLO."""
    v = max(0, min(100, int(percent)))
    v255 = max(1, min(255, (v * 255 + 50) // 100))
    ok = True
    try:
        _ilo_run(f"fan p {pid} max {v255}")
    except Exception:
        ok = False
    try:
        # small delay to help iLO apply sequentially
        time.sleep(0.1)
        _ilo_run(f"fan p {pid} min {v255}")
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
            base = _build_ssh_base()
            ssh_cmd = base + [f"{ILO_USER}@{ILO_IP}", "show /system1/sensors"]
            if ILO_PASSWORD:
                ssh_cmd = ["sshpass", "-p", ILO_PASSWORD] + ssh_cmd
            sensors = subprocess.check_output(ssh_cmd, text=True, timeout=3)
            # Extract biggest integer on lines mentioning CPU/Proc
            awk = r"awk 'tolower($0) ~ /cpu|proc|processor/ { for (i=1;i<=NF;i++) if ($i ~ /^[0-9]+$/) print $i }' | sort -nr | head -1"
            cpu_temp = subprocess.check_output(awk, input=sensors, shell=True, text=True).strip()
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
    return render_template(
        "index.html",
        fan_curve=load_curve(),
        fan_speeds=get_fan_speeds(),
        cpu_temp="",
        gpu_temp="",
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
            base = _build_ssh_base()
            ssh_cmd = base + [f"{ILO_USER}@{ILO_IP}", "show /system1/sensors"]
            if ILO_PASSWORD:
                ssh_cmd = ["sshpass", "-p", ILO_PASSWORD] + ssh_cmd
            stext = subprocess.check_output(ssh_cmd, text=True, timeout=3)
            for line in stext.splitlines():
                if 'CPU' in line:
                    continue
                if any(k in line.lower() for k in ['temp','ambient','inlet','pci','chipset','vr','dimm','io board','controller']):
                    m = re.search(r"\b(\d{1,3})\b", line)
                    if m:
                        sensors.append({"label": line.strip()[:30], "value": m.group(1)})
                if len(sensors) >= 5:
                    break
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
            # Batch commands
            cmds = "; ".join([f"fan p {pid} max {max(1, min(255, (max(0, min(100, int(percent))) * 255 + 50)//100))}; fan p {pid} min {max(1, min(255, (max(0, min(100, int(percent))) * 255 + 50)//100))}" for pid in pids])
            try:
                _ilo_run(cmds, timeout=8)
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
    cmd = ["ssh", "-o", "StrictHostKeyChecking=no"]
    if ILO_SSH_LEGACY:
        cmd += [
            "-o", "KexAlgorithms=+diffie-hellman-group14-sha1,diffie-hellman-group1-sha1",
            "-o", "HostKeyAlgorithms=+ssh-rsa",
            "-o", "PubkeyAcceptedAlgorithms=+ssh-rsa",
            "-o", "PubkeyAcceptedKeyTypes=+ssh-rsa",
            "-o", "Ciphers=+aes128-cbc,3des-cbc",
            "-o", "MACs=+hmac-sha1",
        ]
    if ILO_SSH_KEY and os.path.exists(ILO_SSH_KEY):
        cmd += ["-i", ILO_SSH_KEY]
        method = "ssh-key"
    if ILO_PASSWORD:
        cmd = ["sshpass", "-p", ILO_PASSWORD] + cmd
        method = "sshpass"
    cmd += [f"{ILO_USER}@{ILO_IP}", "echo ok"]
    try:
        out = subprocess.check_output(cmd, text=True, timeout=3).strip()
        ok_flag = (out.lower().find("ok") != -1)
        return jsonify({"ok": ok_flag, "method": method, "output": out})
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
