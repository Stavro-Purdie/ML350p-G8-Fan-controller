from flask import Flask, render_template, request, redirect, jsonify
import json, os

app = Flask(__name__)

# Default to server install paths; can be overridden by env
FAN_CURVE_FILE = os.getenv("FAN_CURVE_FILE", "/opt/dynamic-fan-ui/fan_curve.json")
FAN_SPEED_FILE = os.getenv("FAN_SPEED_FILE", "/opt/dynamic-fan-ui/fan_speeds.txt")
FAN_IDS = ["fan1", "fan2", "fan3", "fan4", "fan5"]


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
    return jsonify({
        "cpu": "",
        "gpu": "",
        "fans": get_fan_speeds(),
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
    try:
        with open(FAN_CURVE_FILE, "w") as f:
            json.dump(curve, f)
    except Exception:
        pass
    return redirect("/")


@app.route("/control", methods=["POST"])
def control():
    # Placeholder for future start/stop logic
    return redirect("/")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
