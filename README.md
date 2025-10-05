# ML350p-G8-Fan-controller

Flask UI + Bash daemon to control HP ML350p Gen8 fans (iLO 4) based on CPU/GPU temperatures.

## Preset configuration (zero-tweak)
- Raw PWM (bits) mode by default; UI converts for display
- iLO modded control enabled; mapping: fan2→PID1, fan3→PID2, fan4→PID3
- Fixed pacing: 1 second between iLO commands
- Control loop interval: 1 second
- Min window: min = max − 4 (bits)
- Files:
	- fan curve: `/opt/dynamic-fan-ui/fan_curve.json` (auto-created with checkInterval=1, maxStep=20)
	- last speeds: `/opt/dynamic-fan-ui/fan_speeds.txt`
	- last speeds (bits): `/opt/dynamic-fan-ui/fan_speeds_bits.txt`
	- predictive defaults: horizon 45s, history 240 samples, blend 0.7 (GPU 0.75), lead 20s (GPU 25s), slopeGain 1.2 (GPU 1.35), maxOffset 12°C (GPU 16°C), deadband 3% (GPU 4%)

## Components
- `dynamic_fans.sh` (daemon): Reads temps (CPU via iLO SSH or IPMI, GPU via nvidia-smi), computes target from a linear fan curve, applies speeds via modded iLO, persists last speeds.
- `app.py` (Flask UI): Shows temps and fan speeds, lets you edit the fan curve, start/stop the systemd service, and run quick tests.
- `dynamic-fans.service.example`: Example systemd unit wired with presets.

## Quick start
1) Optional: run the daemon as a service using the example unit

```bash
sudo cp dynamic-fans.service.example /etc/systemd/system/dynamic-fans.service
sudo systemctl daemon-reload
sudo systemctl enable --now dynamic-fans.service
```

2) Run the web UI for development

```bash
python3 app.py
```

Browse to http://0.0.0.0:5000

## Tuning (optional)
You can still tweak via env or curve JSON:
- CHECK_INTERVAL (seconds), ILO_CMD_GAP_MS (ms)
- Curve: min/max temps & speeds; `maxStep`, `minChange`, optional GPU curve
- Predictive feed-forward (`fan_curve.json` → `predict`):
	- `horizon`, `history`, `minPoints` – how much telemetry to look at/in to consider
	- `blend` / `gpuBlend` – weighting between current temp and forecast (0=current, 1=forecast)
	- `lead` / `gpuLead` + `slopeGain` / `gpuSlopeGain` – anticipatory offset based on forecast slope
	- `maxOffset` / `gpuMaxOffset` – clamp on anticipatory °C bump
	- `deadband` / `gpuDeadband` – ignore tiny target changes to keep fans steady while idle
Defaults are tuned for systems that idle long, then spike into heavy jobs; trim deadbands for quicker corrections or lower leads/gains if fans ramp too early.

## Notes
- Ensure iLO SSH key path/permissions are correct for the service user.
- Use `ILO_SSH_LEGACY=1` for older iLO cipher support.
- GPU telemetry requires `nvidia-smi`.
- Self-update (pull latest Git) is now enabled by default; set `SELF_UPDATE_ENABLED=0` before launching the UI to hide it if you want to manage updates manually.
