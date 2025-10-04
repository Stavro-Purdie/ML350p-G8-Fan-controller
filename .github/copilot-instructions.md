# Copilot Instructions for ML350p-G8-Fan-controller

Purpose: Help AI coding agents work productively in this repo by documenting the architecture, workflows, and conventions actually used here.

## Big picture
- This is a small Flask web UI that controls and monitors fan speeds on an HP ML350p Gen8 with iLO, plus an NVIDIA GPU.
- Backend: `app.py` (Flask) reads temperatures and exposes routes to view status, update the fan curve, and start/stop a systemd service.
- Fan control loop is NOT inside Flask. It's a separate Bash daemon (`dynamic_fans.sh`) run by systemd (`dynamic-fans.service`, not in repo) that:
  - Reads temps (CPU via iLO over SSH, GPU via `nvidia-smi`)
  - Computes a target fan speed from a simple linear fan curve (JSON)
  - Applies the speed to each iLO fan with small step changes and persists last speeds
- Frontend: Jinja template `templates/index.html` + `static/script.js` for polling `/status` every 5s and updating the page.

## Files and data flow
- `app.py` constants:
  - FAN_SCRIPT: `/usr/local/bin/dynamic_fans.sh` (expected install path for the daemon script)
  - FAN_CURVE_FILE: `/opt/dynamic-fan-ui/fan_curve.json` (authoritative fan curve config)
  - FAN_SPEED_FILE: `/opt/dynamic-fan-ui/fan_speeds.txt` (last applied speeds, one per fan)
  - iLO creds and IP: `ILO_SSH_KEY`, `ILO_USER`, `ILO_IP` (hard-coded for the host; override at deploy time)
- `dynamic_fans.sh` reads the same curve/speed files and manages the control loop. It ensures a default curve JSON exists if missing.
- `templates/index.html` renders current temps and speeds from Flask; `static/script.js` polls `/status` and replaces the fan list.

## Environment assumptions
- Host has: systemd, `ssh` access to iLO, `jq`, `nvidia-smi` available, and the Flask app runs as a separate web process.
- Files under `/opt/dynamic-fan-ui/` are writable by the service users.
- The systemd unit `dynamic-fans.service` starts the Bash loop; Flask only triggers `systemctl start/stop`.

## Typical workflows
- Run the web UI locally for dev: `python app.py` (serves on 0.0.0.0:5000).
- Update fan curve via UI form (POST `/update_curve`); values are written to `FAN_CURVE_FILE` JSON.
- Start/stop control loop via UI buttons (POST `/control` → `systemctl start|stop dynamic-fans.service`).
- Live status: Frontend polls `/status` to fetch `{ cpu, gpu, fans: [speeds...] }`.

## Conventions and patterns
- Fan IDs are fixed: `["fan1", "fan2", "fan3", "fan4", "fan5"]`. Keep lists aligned with persisted speeds (one integer per line in `fan_speeds.txt`).
- Fan curve shape is linear interpolation between `(minTemp→minSpeed)` and `(maxTemp→maxSpeed)`; outside bounds clamp to min/max.
- Step changes: The Bash loop limits per-iteration change by `MAX_STEP` to avoid sudden ramping.
- Security: iLO SSH key path and admin user/IP are hard-coded; prefer making these configurable via environment variables when extending.

## External integration points
- iLO CLI over SSH: `show /system1/sensors` to read CPU temps; `set /system1/<fan> speed=<N>` to apply speed.
- GPU temp via `nvidia-smi --query-gpu=temperature.gpu --format=csv,noheader,nounits`.
- systemd: `dynamic-fans.service` controls `dynamic_fans.sh` execution.

## Useful examples
- Adding another fan: update `FAN_IDS` in both `app.py` and `dynamic_fans.sh`; ensure `fan_speeds.txt` has matching line count.
- Changing control interval or ramp: edit `CHECK_INTERVAL` or `MAX_STEP` in `dynamic_fans.sh`.
- Exposing more status: extend `status()` in `app.py` and update `static/script.js` to render new fields.

## Gotchas
- JSON and text files must be readable/writable by both the Flask process and the systemd service user.
- Missing `fan_curve.json` is auto-created by the Bash script, but Flask expects it to exist when rendering `/`.
- Running Flask without `nvidia-smi` or iLO connectivity will result in empty/incorrect temps; handle this gracefully if improving error handling.

## Next improvements (non-breaking)
- Parameterize ILO and file paths via env vars in `app.py` and `dynamic_fans.sh`.
- Add a sample `dynamic-fans.service` unit to the repo for reference.
- Add a lightweight README with install and service setup steps.
