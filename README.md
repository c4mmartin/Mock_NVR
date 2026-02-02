# mock_nvr

Quick-and-dirty Python mock NVR that generates fake multi-camera video.

## What it provides

Per camera:

- Snapshot (JPEG): `http://<host>:<http_port>/cam/<id>/snapshot.jpg`
- MJPEG stream: `http://<host>:<http_port>/cam/<id>/mjpeg`
- RTSP (H.264): `rtsp://<host>:<rtsp_port>/cam<id>_h264`
- RTSP (H.265): `rtsp://<host>:<rtsp_port>/cam<id>_h265`

The image changes every few seconds:

- background color randomly changes
- camera identifier bounces/moves around
- timestamp updates

## Requirements

- Python 3.10+
- `ffmpeg` available on PATH
  - macOS (Homebrew): `brew install ffmpeg`
- RTSP backend: `mediamtx` (recommended)
  - macOS (Homebrew): `brew install mediamtx`

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Cross-platform bootstrap (recommended: creates a local `.venv` and installs deps; no global installs needed):

```bash
python scripts/bootstrap.py
```

Windows convenience:

- PowerShell: `scripts\install.ps1`
- Cmd: `scripts\install.cmd`

If you prefer not to activate a shell, you can always run via the venv interpreter:

```bash
.venv/bin/python -m pip install -r requirements.txt
```

## Run

```bash
python -m mock_nvr --cameras 4 --http-port 8080 --rtsp-port 8554
```

Local-only (bind to localhost; typically no firewall changes needed):

```bash
python -m mock_nvr run --cameras 4 --http-port 8080 --rtsp-port 8554 --bind-host 127.0.0.1 --advertise-host 127.0.0.1
```

Without activating the venv:

```bash
.venv/bin/python -m mock_nvr --cameras 4 --http-port 8080 --rtsp-port 8554
```

Or use the wrapper script (no activation needed):

```bash
chmod +x scripts/mock-nvr
scripts/mock-nvr --cameras 4 --http-port 8080 --rtsp-port 8554
```

On Windows (no activation needed):

- PowerShell: `scripts\mock-nvr.ps1 --cameras 4 --http-port 8080 --rtsp-port 8554`
- Cmd: `scripts\mock-nvr.cmd --cameras 4 --http-port 8080 --rtsp-port 8554`

Cross-platform option (works anywhere you have Python):

```bash
python scripts/mock-nvr.py --cameras 4 --http-port 8080 --rtsp-port 8554
```

## Doctor / troubleshooting

Run a quick environment check (Python version, ffmpeg, ports):

```bash
python scripts/mock-nvr.py doctor
```

## Firewall / opening ports

If you want other machines to reach this host (LAN/WAN), you may need to allow inbound connections in the system firewall for the ports you’re using.

This repo includes a helper that prints OS-specific commands (and can optionally run them with `--apply --yes`):

```bash
python scripts/mock-nvr.py firewall open --http-port 8080 --rtsp-port 8554 --supervisor-http-port 8090
python scripts/mock-nvr.py firewall close --http-port 8080 --rtsp-port 8554 --supervisor-http-port 8090
```

If you’re using supervisor mode and want to expose worker HTTP ports too:

```bash
python scripts/mock-nvr.py firewall open --workers 2 --worker-http-port-base 8100
```

Notes:

- macOS uses the built-in Application Firewall (`socketfilterfw`), which is app-based (it allows the Python executable), not port-based.
- Windows uses Windows Defender Firewall rules via `netsh`.
- Linux supports `firewalld` or `ufw` when detected.

## Uninstall / cleanup

Remove the local venv (and optionally logs):

```bash
python scripts/uninstall.py --yes
python scripts/uninstall.py --yes --remove-logs
```

Windows convenience:

- PowerShell: `scripts\uninstall.ps1`
- Cmd: `scripts\uninstall.cmd`

By default the startup output and the index page show URLs with an `IPADDR` placeholder. Replace it with your LAN IP, or set it explicitly:

```bash
python -m mock_nvr --cameras 4 --http-port 8080 --rtsp-port 8554 --advertise-host 192.168.1.50
```

If you prefer a script-style entrypoint:

```bash
python run_mock_nvr.py --cameras 4 --http-port 8080 --rtsp-port 8554
```

Or install an executable:

```bash
pip install -e .
mock-nvr --cameras 4 --http-port 8080 --rtsp-port 8554
```

Open index page:

- `http://localhost:8080/`

Test snapshot:

- `open http://localhost:8080/cam/1/snapshot.jpg`

Test MJPEG:

- `open http://localhost:8080/cam/1/mjpeg`

Test RTSP:

- H.264: `ffplay -rtsp_transport tcp rtsp://localhost:8554/cam1_h264`
- H.265: `ffplay -rtsp_transport tcp rtsp://localhost:8554/cam1_h265`

## Scaling / avoiding dropouts

This project renders frames (Pillow) in background tasks and caches the latest frame per camera.
HTTP snapshot/MJPEG and RTSP publishing read from that cache, which avoids blocking the web server
when rendering gets expensive.

Practical tips:

- If you see stutter, reduce CPU load first: lower `--fps`, `--width`, `--height`, or number of cameras.
- Watch basic stats at `http://localhost:<http_port>/stats` (look at `avg_render_ms`, `render_overruns`, and `last_frame_age_s`).
- RTSP publishing uses `ffmpeg` processes; check logs in `logs/ffmpeg_*.log` if a stream won’t start.

### Split across multiple processes

If you want to spread load across CPU cores, run **one** MediamTX and then run multiple `mock_nvr` publisher processes, each generating a slice of camera IDs.

1) Start MediamTX yourself (once):

```bash
mediamtx logs/mediamtx.yml
```

1) Start multiple mock_nvr processes (different `--http-port`, different camera ranges), all publishing to the same RTSP server:

```bash
python -m mock_nvr --camera-start 1 --cameras 4 --http-port 8080 --rtsp-port 8554 --rtsp-publish-host 127.0.0.1 --no-start-mediamtx
python -m mock_nvr --camera-start 5 --cameras 4 --http-port 8081 --rtsp-port 8554 --rtsp-publish-host 127.0.0.1 --no-start-mediamtx
```

This keeps the Python rendering load distributed while still producing a single RTSP namespace on the MediamTX side (e.g. `rtsp://<host>:8554/cam7_h264`).

### Supervisor mode (auto-spawn workers)

There’s also a built-in supervisor that spawns workers for you and provides a small dashboard + aggregated stats.

```bash
python -m mock_nvr supervise --cameras 8 --workers 2 --supervisor-http-port 8090 --worker-http-port-base 8100 \
  --rtsp-port 8554 --rtsp-publish-host 127.0.0.1 --advertise-host 192.168.1.50 --start-mediamtx --log-level INFO
```

Local-only supervisor:

```bash
python -m mock_nvr supervise --cameras 8 --workers 2 --supervisor-http-port 8090 --worker-http-port-base 8100 \
  --rtsp-port 8554 --rtsp-publish-host 127.0.0.1 --bind-host 127.0.0.1 --advertise-host 127.0.0.1 --start-mediamtx
```

- Supervisor UI: `http://localhost:8090/`
- Aggregated stats: `http://localhost:8090/stats`

## Notes

- This is intentionally simple and “dumb”: it’s meant to trick NVR/camera drivers into thinking a live camera exists.
- RTSP is provided by `mediamtx` (RTSP server) + per-stream `ffmpeg` publishers.

## Logging

`mock_nvr` uses `structlog` and writes logs to `logs/` with automatic size-based rotation.

- Console logs are human-readable.
- File logs are JSON (one event per line), rotated by size.

- Each process writes `logs/<process>_<pid>.log` (plus rotated backups).
- `ffmpeg` and `mediamtx` output is captured into the same rotated logs (instead of growing forever).
- Tune with `--log-level`, `--log-max-bytes`, and `--log-backups`.
- Optionally delete old logs on startup with `--log-retention-days 14`.
