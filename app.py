from flask import Flask, render_template, request, redirect, jsonify
import subprocess, json, os, re, threading, time, shutil
from typing import Any, Dict, List

app = Flask(__name__)

# Configurable via environment variables with sensible defaults
FAN_SCRIPT = os.getenv("FAN_SCRIPT", "/usr/local/bin/dynamic_fans.sh")
FAN_CURVE_FILE = os.getenv("FAN_CURVE_FILE", "/opt/dynamic-fan-ui/fan_curve.json")
FAN_SPEED_FILE = os.getenv("FAN_SPEED_FILE", "/opt/dynamic-fan-ui/fan_speeds.txt")
ILO_SSH_KEY = os.getenv("ILO_SSH_KEY", "/root/.ssh/ilo_key")
ILO_USER = os.getenv("ILO_USER", "admin")
ILO_IP = os.getenv("ILO_IP", "192.168.1.100")
ILO_PASSWORD = os.getenv("ILO_PASSWORD", "")
USE_IPMI_TEMPS = os.getenv("USE_IPMI_TEMPS", "0") == "1"
ILO_SSH_LEGACY = os.getenv("ILO_SSH_LEGACY", "0") == "1"
ILO_MODDED = os.getenv("ILO_MODDED", "0") == "1"
FAN_P_IDS = [p for p in os.getenv("FAN_P_IDS_STR", "").split() if p]
ILO_PID_OFFSET = int(os.getenv("ILO_PID_OFFSET", "-1"))

FAN_IDS_ENV = os.getenv("FAN_IDS_STR", "").strip()
if FAN_IDS_ENV:
    FAN_IDS = FAN_IDS_ENV.split()
else:
    FAN_IDS = os.getenv("FAN_IDS", "fan1,fan2,fan3,fan4,fan5").split(",")

def get_temps():
    # Allow optional key; if missing, rely on ssh config or password auth (external)
    cpu_temp = ""
    if USE_IPMI_TEMPS:
        # Try ipmitool first
        ipmi_cmd = [
            "bash", "-lc",
            f"ipmitool -I lanplus -H {ILO_IP} -U {ILO_USER} {'-P ' + ILO_PASSWORD if ILO_PASSWORD else ''} sdr type Temperature | awk -F'|' '/CPU/ {{ if (match($2, /[0-9]+/, m)) print m[0]; }}' | sort -nr | head -1"
        ]
        try:
            cpu_temp = subprocess.check_output(ipmi_cmd, text=True, timeout=2).strip()
        except Exception:
            cpu_temp = ""
    if not cpu_temp:
        base_ssh = ["ssh", "-o", "StrictHostKeyChecking=no"]
        if ILO_SSH_LEGACY:
            base_ssh += [
                "-o", "KexAlgorithms=+diffie-hellman-group14-sha1,diffie-hellman-group1-sha1",
                "-o", "HostKeyAlgorithms=+ssh-rsa",
                "-o", "PubkeyAcceptedAlgorithms=+ssh-rsa",
                "-o", "PubkeyAcceptedKeyTypes=+ssh-rsa",
                "-o", "Ciphers=+aes128-cbc,3des-cbc",
                "-o", "MACs=+hmac-sha1",
            ]
        if ILO_SSH_KEY and os.path.exists(ILO_SSH_KEY):
            base_ssh += ["-i", ILO_SSH_KEY]
        ssh_cmd = base_ssh + [f"{ILO_USER}@{ILO_IP}", "show /system1/sensors"]
        if ILO_PASSWORD:
            # Prefer sshpass if password provided
            ssh_cmd = ["sshpass", "-p", ILO_PASSWORD] + ssh_cmd
        try:
            sensors = subprocess.check_output(ssh_cmd, text=True, timeout=2)
            # Look for lines containing CPU/Proc/Processor and extract the highest integer
            cmd = r"awk 'tolower($0) ~ /cpu|proc|processor/ { for (i=1;i<=NF;i++) if ($i ~ /^[0-9]+$/) print $i }' | sort -nr | head -1"
            cpu_temp = subprocess.check_output(cmd, input=sensors, shell=True, text=True).strip()
        except Exception:
            cpu_temp = ""
    try:
        gpu_q = subprocess.check_output(
            "nvidia-smi --query-gpu=temperature.gpu,name,power.draw --format=csv,noheader,nounits",
            shell=True, text=True, timeout=2
        ).strip()
        temps = []
        names = []
        powers = []
        for line in gpu_q.splitlines():
            parts = [p.strip() for p in line.split(',')]
            if len(parts) >= 1 and parts[0].isdigit():
                temps.append(int(parts[0]))
                names.append(parts[1] if len(parts) > 1 else "")
                powers.append(parts[2] if len(parts) > 2 else "")
        gpu_max = str(max(temps)) if temps else ""
        gpu_name = names[temps.index(max(temps))] if temps else ""
        gpu_power = powers[temps.index(max(temps))] if temps else ""
    except Exception:
        gpu_max, gpu_name, gpu_power = "", "", ""
    return cpu_temp, gpu_max, gpu_name, gpu_power

@app.route("/test_ilo")
def test_ilo():
    # Attempts to run a trivial command on iLO and reports the method used; also checks IPMI if enabled
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
        out = subprocess.check_output(cmd, text=True, timeout=2).strip()
        ipmi_ok = False
        ipmi_error = ""
        if USE_IPMI_TEMPS:
            try:
                ipmi_out = subprocess.check_output(
                    ["bash", "-lc", f"ipmitool -I lanplus -H {ILO_IP} -U {ILO_USER} {'-P ' + ILO_PASSWORD if ILO_PASSWORD else ''} sdr type Temperature | head -1"],
                    text=True,
                    timeout=2,
                )
                ipmi_ok = len(ipmi_out.strip()) > 0
            except Exception as e:
                ipmi_error = str(e)
        ok_flag = (out.lower().find("ok") != -1)
        return jsonify({"ok": ok_flag, "method": method, "output": out, "ipmi": {"enabled": USE_IPMI_TEMPS, "ok": ipmi_ok, "error": ipmi_error}})
    except Exception as e:
        return jsonify({"ok": False, "method": method, "error": str(e), "ipmi": {"enabled": USE_IPMI_TEMPS}}), 500

def get_fan_speeds():
    if os.path.exists(FAN_SPEED_FILE):
        with open(FAN_SPEED_FILE) as f:
            return [int(x.strip()) for x in f.readlines()]
    return [0]*len(FAN_IDS)

# Build SSH base for iLO commands
def build_ssh_base() -> list[str]:
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
    return base

_test_lock = threading.Lock()
_test_running = False

def _run_fan_test(percent: int, duration: int):
    global _test_running
    with _test_lock:
        if _test_running:
            return
        _test_running = True
    try:
        # Determine if control loop is active
        was_active = False
        if shutil.which("systemctl") is not None:
            try:
                out = subprocess.check_output(["systemctl", "is-active", "dynamic-fans.service"], text=True, timeout=2).strip()
                was_active = (out == "active")
            except Exception:
                was_active = False
        if was_active:
            subprocess.Popen(["systemctl", "stop", "dynamic-fans.service"])  # stop asynchronously
            time.sleep(1)

        base = build_ssh_base()
        ssh_cmd = base + [f"{ILO_USER}@{ILO_IP}"]
        if ILO_PASSWORD:
            ssh_cmd = ["sshpass", "-p", ILO_PASSWORD] + ssh_cmd
        # Apply test speed
        if ILO_MODDED:
            # Map percent to 1..255 with min=max for exact control
            v255 = max(1, min(255, int(round(percent * 255 / 100.0))))
            if FAN_P_IDS:
                pids = FAN_P_IDS
            else:
                pids = []
                for f in FAN_IDS:
                    try:
                        num = int(f.replace("fan", ""))
                        pids.append(str(max(0, num + ILO_PID_OFFSET)))
                    except Exception:
                        pids.append("0")
            for pid in pids:
                try:
                    subprocess.run(ssh_cmd + [f"fan p {pid} min {v255}"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
                    subprocess.run(ssh_cmd + [f"fan p {pid} max {v255}"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
                except Exception:
                    pass
        else:
            for fan in FAN_IDS:
                try:
                    subprocess.run(ssh_cmd + [f"set /system1/{fan} speed={percent}"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
                except Exception:
                    pass
        time.sleep(duration)
        # Drop to a safe level before resuming control
        if ILO_MODDED:
            if FAN_P_IDS:
                pids = FAN_P_IDS
            else:
                pids = []
                for f in FAN_IDS:
                    try:
                        num = int(f.replace("fan", ""))
                        pids.append(str(max(0, num + ILO_PID_OFFSET)))
                    except Exception:
                        pids.append("0")
            for pid in pids:
                try:
                    subprocess.run(ssh_cmd + [f"fan p {pid} min 76"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)  # ~30%
                    subprocess.run(ssh_cmd + [f"fan p {pid} max 76"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
                except Exception:
                    pass
        else:
            for fan in FAN_IDS:
                try:
                    subprocess.run(ssh_cmd + ["set", f"/system1/{fan}", "speed=30"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
                except Exception:
                    pass
        if was_active:
            subprocess.Popen(["systemctl", "start", "dynamic-fans.service"])  # resume
    finally:
        with _test_lock:
            _test_running = False

@app.route("/fan_test", methods=["POST"])
def fan_test():
    # Run a brief ramp in background, then redirect immediately
    try:
        percent = int(request.form.get("test_percent", "100"))
        duration = int(request.form.get("test_duration", "10"))
    except Exception:
        percent, duration = 100, 10
    percent = max(0, min(100, percent))
    duration = max(1, min(60, duration))
    t = threading.Thread(target=_run_fan_test, args=(percent, duration), daemon=True)
    t.start()
    return redirect("/")

@app.route("/")
def index():
    cpu_temp, gpu_temp, gpu_name, gpu_power = get_temps()
    fan_speeds = get_fan_speeds()
    # Load fan curve; if missing, show sensible defaults
    fan_curve = {"minTemp": 30, "maxTemp": 80, "minSpeed": 20, "maxSpeed": 100}
    try:
        if os.path.exists(FAN_CURVE_FILE):
            with open(FAN_CURVE_FILE) as f:
                fan_curve = json.load(f)
    except Exception:
        pass
    return render_template("index.html", cpu_temp=cpu_temp, gpu_temp=gpu_temp,
                           fan_speeds=fan_speeds, fan_curve=fan_curve)

@app.route("/control", methods=["POST"])
def control():
    action = request.form.get("action")
    if action == "start":
        subprocess.Popen(["systemctl", "start", "dynamic-fans.service"])
    elif action == "stop":
        subprocess.Popen(["systemctl", "stop", "dynamic-fans.service"])
    return redirect("/")

@app.route("/update_curve", methods=["POST"])
def update_curve():
    data: Dict[str, Any] = {
        "minTemp": int(request.form["minTemp"]),
        "maxTemp": int(request.form["maxTemp"]),
        "minSpeed": int(request.form["minSpeed"]),
        "maxSpeed": int(request.form["maxSpeed"])
    }
    # Optional GPU-specific curve
    if (
        "gpu_minTemp" in request.form and
        "gpu_maxTemp" in request.form and
        "gpu_minSpeed" in request.form and
        "gpu_maxSpeed" in request.form
    ):
        try:
            data["gpu"] = {
                "minTemp": int(request.form["gpu_minTemp"]),
                "maxTemp": int(request.form["gpu_maxTemp"]),
                "minSpeed": int(request.form["gpu_minSpeed"]),
                "maxSpeed": int(request.form["gpu_maxSpeed"]),
            }
        except Exception:
            pass
    os.makedirs(os.path.dirname(FAN_CURVE_FILE), exist_ok=True)
    # Optional blending
    try:
        mode = request.form.get("blend_mode", "max")
        cpu_w = float(request.form.get("blend_cpuWeight", "0.5"))
        gpu_w = float(request.form.get("blend_gpuWeight", "0.5"))
        data["blend"] = {"mode": mode, "cpuWeight": cpu_w, "gpuWeight": gpu_w}
    except Exception:
        pass
    # Optional GPU boost
    try:
        t = request.form.get("gpuBoost_threshold")
        a = request.form.get("gpuBoost_add")
        boost: Dict[str, Any] = {}
        if t is not None and t != "":
            boost["threshold"] = int(t)
        if a is not None and a != "":
            boost["add"] = int(a)
        if boost:
            data["gpuBoost"] = boost
    except Exception:
        pass
    with open(FAN_CURVE_FILE, "w") as f:
        json.dump(data, f)
    return redirect("/")

@app.route("/status")
def status():
    cpu, gpu, gpu_name, gpu_power = get_temps()
    speeds = get_fan_speeds()
    sensors = []
    # Try to include a few more temp sensors for display
    try:
        if USE_IPMI_TEMPS:
            ipmi_cmd = [
                "bash", "-lc",
                f"ipmitool -I lanplus -H {ILO_IP} -U {ILO_USER} {'-P ' + ILO_PASSWORD if ILO_PASSWORD else ''} sdr type Temperature"
            ]
            out = subprocess.check_output(ipmi_cmd, text=True, timeout=2)
            for line in out.splitlines():
                parts = [p.strip() for p in line.split('|')]
                if len(parts) >= 2 and 'CPU' not in parts[0]:
                    m = re.search(r"(\d+)", parts[1])
                    if m:
                        sensors.append({"label": parts[0], "value": m.group(1)})
                if len(sensors) >= 5:
                    break
        else:
            base_ssh = ["ssh", "-o", "StrictHostKeyChecking=no"]
            if ILO_SSH_LEGACY:
                base_ssh += [
                    "-o", "KexAlgorithms=+diffie-hellman-group14-sha1,diffie-hellman-group1-sha1",
                    "-o", "HostKeyAlgorithms=+ssh-rsa",
                    "-o", "PubkeyAcceptedAlgorithms=+ssh-rsa",
                    "-o", "PubkeyAcceptedKeyTypes=+ssh-rsa",
                    "-o", "Ciphers=+aes128-cbc,3des-cbc",
                    "-o", "MACs=+hmac-sha1",
                ]
            if ILO_SSH_KEY and os.path.exists(ILO_SSH_KEY):
                base_ssh += ["-i", ILO_SSH_KEY]
            ssh_cmd = base_ssh + [f"{ILO_USER}@{ILO_IP}", "show /system1/sensors"]
            if ILO_PASSWORD:
                ssh_cmd = ["sshpass", "-p", ILO_PASSWORD] + ssh_cmd
            sensors_out = subprocess.check_output(ssh_cmd, text=True, timeout=2)
            for line in sensors_out.splitlines():
                if 'CPU' in line:
                    continue
                # Include common Sea of Sensors labels
                if any(k in line.lower() for k in [
                    'temp', 'ambient', 'inlet', 'pci', 'chipset', 'p/s', 'vr', 'dimm', 'io board', 'controller']):
                    m = re.search(r"\b(\d{1,3})\b", line)
                    if m:
                        parts = line.split()
                        label = " ".join(parts[:3]) if len(parts) >= 3 else (" ".join(parts) if parts else line)
                        sensors.append({"label": label, "value": m.group(1)})
                if len(sensors) >= 5:
                    break
    except Exception:
        sensors = []

    # Detailed GPU info (best-effort)
    def get_gpu_info() -> List[Dict[str, Any]]:
        info: List[Dict[str, Any]] = []
        try:
            fields = (
                "index,name,uuid,pci.bus_id,temperature.gpu,utilization.gpu,utilization.memory,"
                "utilization.encoder,utilization.decoder,memory.total,memory.used,power.draw,power.limit,"
                "pstate,fan.speed,clocks.gr,clocks.mem,clocks.video,pcie.link.gen.current,pcie.link.gen.max,"
                "pcie.link.width.current,pcie.link.width.max"
            )
            base = subprocess.check_output(
                f"nvidia-smi --query-gpu={fields} --format=csv,noheader,nounits",
                shell=True, text=True, timeout=2
            )
            for line in base.splitlines():
                parts = [p.strip() for p in line.split(',')]
                if len(parts) < 20:
                    continue
                d = {
                    "index": parts[0],
                    "name": parts[1],
                    "uuid": parts[2],
                    "bus_id": parts[3],
                    "temperature": parts[4],
                    "util_gpu": parts[5],
                    "util_mem": parts[6],
                    "util_enc": parts[7],
                    "util_dec": parts[8],
                    "mem_total": parts[9],
                    "mem_used": parts[10],
                    "power_draw": parts[11],
                    "power_limit": parts[12],
                    "pstate": parts[13],
                    "fan_speed": parts[14],
                    "clocks_gr": parts[15],
                    "clocks_mem": parts[16],
                    "clocks_video": parts[17],
                    "pcie_gen_cur": parts[18],
                    "pcie_gen_max": parts[19],
                    "pcie_width_cur": parts[20] if len(parts) > 20 else "",
                    "pcie_width_max": parts[21] if len(parts) > 21 else "",
                    "encoder_sessions": None,
                }
                info.append(d)
        except Exception:
            info = []

        # NVENC sessions (best-effort)
        def collect_sessions() -> Dict[str, List[Dict[str, Any]]]:
            sessions: Dict[str, List[Dict[str, Any]]] = {}
            text = ""
            try:
                text = subprocess.check_output(
                    "nvidia-smi encodersessions -q",
                    shell=True, text=True, timeout=2
                )
            except Exception:
                try:
                    text = subprocess.check_output(
                        "nvidia-smi encodersessions",
                        shell=True, text=True, timeout=2
                    )
                except Exception:
                    return sessions

            current_bus: str | None = None
            current: Dict[str, Any] | None = None
            for raw in text.splitlines():
                ln = raw.strip()
                if not ln:
                    continue
                # Detect GPU header with bus id
                m = re.match(r"GPU\s+([0-9A-Fa-f:.]+)", ln)
                if m:
                    current_bus = m.group(1)
                    key = current_bus or ""
                    sessions.setdefault(key, [])
                    current = None
                    continue
                # Start of a session block (heuristic)
                if ln.lower().startswith("session") or ln.lower().startswith("pid"):
                    if current_bus:
                        if current:
                            sessions[current_bus or ""].append(current)
                        current = {}
                    continue
                # Parse key: value style lines
                if ":" in ln and current_bus is not None:
                    key, val = [p.strip() for p in ln.split(":", 1)]
                    if current is not None:
                        # Normalize some keys
                        key_norm = key.lower().replace(" ", "_")
                        current[key_norm] = val
                    continue
                # Fallback: add raw line
                if current_bus is not None and current is not None:
                    current.setdefault("raw", []).append(ln)
            # Flush last session
            if current_bus and current:
                sessions[current_bus or ""].append(current)
            return sessions

        try:
            sessions_map = collect_sessions()
            if info and sessions_map:
                for d in info:
                    bus = d.get("bus_id") or ""
                    sess = sessions_map.get(bus)
                    if sess is not None:
                        d["encoder_sessions"] = len(sess)
                        d["sessions"] = sess
        except Exception:
            pass

        return info

    gpu_info = get_gpu_info()

    return jsonify({
        "cpu": cpu,
        "gpu": gpu,
        "gpu_name": gpu_name,
        "gpu_power": gpu_power,
        "fans": speeds,
        "sensors": sensors,
        "gpu_info": gpu_info,
        "ok": True,
    })

@app.route("/debug_ilo_fans")
def debug_ilo_fans():
    prefixes = ["/system1", "/system1/fans1"]
    base = build_ssh_base()
    ssh_cmd = base + [f"{ILO_USER}@{ILO_IP}"]
    if ILO_PASSWORD:
        ssh_cmd = ["sshpass", "-p", ILO_PASSWORD] + ssh_cmd
    result = []
    for pref in prefixes:
        try:
            out = subprocess.check_output(ssh_cmd + [f"show {pref}"], text=True, timeout=3)
        except Exception as e:
            result.append({"prefix": pref, "error": str(e), "fans": []})
            continue
        fans = []
        tokens = set()
        for line in out.split():
            if re.match(r"fan\d+", line, re.IGNORECASE):
                tokens.add(line.strip())
        for fan in sorted(tokens):
            try:
                attrs = subprocess.check_output(ssh_cmd + [f"show -a {pref}/{fan}"], text=True, timeout=3)
            except Exception:
                try:
                    attrs = subprocess.check_output(ssh_cmd + [f"show {pref}/{fan}"], text=True, timeout=3)
                except Exception as e2:
                    fans.append({"name": fan, "error": str(e2)})
                    continue
            fans.append({"name": fan, "attrs": attrs.splitlines()})
        result.append({"prefix": pref, "fans": fans})
    return jsonify(result)

@app.route("/export_status")
def export_status():
    # Return current status JSON (one-shot without charts)
    resp = status()
    return resp

@app.route("/export_curve")
def export_curve():
    try:
        with open(FAN_CURVE_FILE) as f:
            data = json.load(f)
    except Exception:
        data = {}
    return jsonify(data)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
