from flask import Flask, render_template, request, redirect, jsonify
import subprocess, json, os

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
        if ILO_SSH_KEY and os.path.exists(ILO_SSH_KEY):
            base_ssh += ["-i", ILO_SSH_KEY]
        ssh_cmd = base_ssh + [f"{ILO_USER}@{ILO_IP}", "show /system1/sensors"]
        if ILO_PASSWORD:
            # Prefer sshpass if password provided
            ssh_cmd = ["sshpass", "-p", ILO_PASSWORD] + ssh_cmd
        try:
            sensors = subprocess.check_output(ssh_cmd, text=True, timeout=2)
            cpu_temp = subprocess.check_output("grep 'CPU' | awk '{print $2}' | sort -nr | head -1", input=sensors, shell=True, text=True).strip()
        except Exception:
            cpu_temp = ""
    try:
        gpu_temp = subprocess.check_output(
            "nvidia-smi --query-gpu=temperature.gpu --format=csv,noheader,nounits | sort -nr | head -1",
            shell=True, text=True, timeout=2
        ).strip()
    except Exception:
        gpu_temp = ""
    return cpu_temp, gpu_temp

@app.route("/test_ilo")
def test_ilo():
    # Attempts to run a trivial command on iLO and reports the method used; also checks IPMI if enabled
    method = "ssh"
    cmd = ["ssh", "-o", "StrictHostKeyChecking=no"]
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
        return jsonify({"ok": out == "ok", "method": method, "output": out, "ipmi": {"enabled": USE_IPMI_TEMPS, "ok": ipmi_ok, "error": ipmi_error}})
    except Exception as e:
        return jsonify({"ok": False, "method": method, "error": str(e), "ipmi": {"enabled": USE_IPMI_TEMPS}}), 500

def get_fan_speeds():
    if os.path.exists(FAN_SPEED_FILE):
        with open(FAN_SPEED_FILE) as f:
            return [int(x.strip()) for x in f.readlines()]
    return [0]*len(FAN_IDS)

@app.route("/")
def index():
    cpu_temp, gpu_temp = get_temps()
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
    data = {
        "minTemp": int(request.form["minTemp"]),
        "maxTemp": int(request.form["maxTemp"]),
        "minSpeed": int(request.form["minSpeed"]),
        "maxSpeed": int(request.form["maxSpeed"])
    }
    os.makedirs(os.path.dirname(FAN_CURVE_FILE), exist_ok=True)
    with open(FAN_CURVE_FILE, "w") as f:
        json.dump(data, f)
    return redirect("/")

@app.route("/status")
def status():
    cpu, gpu = get_temps()
    speeds = get_fan_speeds()
    return jsonify({"cpu": cpu, "gpu": gpu, "fans": speeds})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
