from flask import Flask, render_template, request, redirect, jsonify
import json, os, subprocess, re, time, threading, shutil

app = Flask(__name__)

# Default to server install paths; can be overridden by env
FAN_CURVE_FILE = os.getenv("FAN_CURVE_FILE", "/opt/dynamic-fan-ui/fan_curve.json")
FAN_SPEED_FILE = os.getenv("FAN_SPEED_FILE", "/opt/dynamic-fan-ui/fan_speeds.txt")
FAN_IDS = ["fan1", "fan2", "fan3", "fan4", "fan5"]

# iLO/IPMI/GPU environment toggles (optional)
ILO_SSH_KEY = os.getenv("ILO_SSH_KEY", "/root/.ssh/ilo_key")
ILO_USER = os.getenv("ILO_USER", "admin")
ILO_IP = os.getenv("ILO_IP", "192.168.1.100")
ILO_PASSWORD = os.getenv("ILO_PASSWORD", "")
USE_IPMI_TEMPS = os.getenv("USE_IPMI_TEMPS", "0") == "1"
ILO_SSH_LEGACY = os.getenv("ILO_SSH_LEGACY", "0") == "1"


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
    return base


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

    return jsonify({
        "cpu": cpu, "gpu": gpu, "gpu_name": gpu_name, "gpu_power": gpu_power,
        "fans": get_fan_speeds(), "sensors": sensors, "gpu_info": gpu_info,
        "ok": True,
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
        # For now just stop the service briefly and restart (placeholder without direct iLO set)
        if shutil.which("systemctl"):
            try:
                subprocess.check_output(["systemctl", "stop", "dynamic-fans.service"], text=True, timeout=4)
            except Exception:
                pass
        time.sleep(max(1, min(60, duration)))
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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
