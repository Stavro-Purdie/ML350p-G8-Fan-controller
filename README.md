# ML350p-G8-Fan-controller

Flask UI + Bash daemon to control HP ML350p Gen8 fans (iLO 4) based on CPU/GPU temperatures.

## Components
- `dynamic_fans.sh` (daemon): Reads temps (CPU via iLO SSH or IPMI, GPU via nvidia-smi), computes target from a linear fan curve, applies stepped changes to iLO fans, persists last speeds.
- `app.py` (Flask UI): Shows temps and fan speeds, lets you edit the fan curve, and start/stop the systemd service. Polls `/status` every 5s. `/test_ilo` checks connectivity.
- `systemd/dynamic-fans.service`: Sample systemd unit to run the daemon.

## Install
Interactive installer lets you choose auth and options.

```bash
./install.sh
```

What it does:
- Installs `dynamic_fans.sh` to `/usr/local/bin/dynamic_fans.sh`
- Creates `/opt/dynamic-fan-ui/{fan_curve.json,fan_speeds.txt}`
- Optionally installs and enables `dynamic-fans.service` and writes overrides at `/etc/systemd/system/dynamic-fans.service.d/override.conf`

Auth options:
- SSH key: provide path (e.g., `/root/.ssh/ilo_key`)
- Password: set during install (requires `sshpass` on host)

IPMI option:
- You can use IPMI for CPU temperatures (`USE_IPMI_TEMPS=1`) if `ipmitool` is installed. Fan control still uses iLO CLI.

## Run
- Start service: `sudo systemctl start dynamic-fans.service`
- Stop service: `sudo systemctl stop dynamic-fans.service`
- Dev UI: `python3 app.py` (http://0.0.0.0:5000)
- Connectivity check: `GET /test_ilo`

## Fan curve
JSON: `{ "minTemp": 30, "maxTemp": 80, "minSpeed": 20, "maxSpeed": 100 }`
- File: `/opt/dynamic-fan-ui/fan_curve.json`
- Update via UI form, saved as above

## Environment
- Paths: `FAN_CURVE_FILE`, `FAN_SPEED_FILE` (defaults under `/opt/dynamic-fan-ui/`)
- Auth: `ILO_IP`, `ILO_USER`, `ILO_SSH_KEY`, `ILO_PASSWORD`
- Loop: `CHECK_INTERVAL`, `MAX_STEP`, `USE_IPMI_TEMPS`

## Notes
- GPU temps require `nvidia-smi`.
- IPMI read only; fan control remains via iLO `set /system1/<fan> speed=<N>`.
- Ensure both the daemon and the Flask app can read/write `/opt/dynamic-fan-ui/*`.
