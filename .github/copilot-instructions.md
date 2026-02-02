# Copilot instructions for mock_nvr

## Quick workflows (copy/paste)
- One-command run (auto-creates `.venv` + installs deps): `python scripts/mock-nvr.py --cameras 4 --http-port 8080 --rtsp-port 8554`
- Bootstrap local venv + deps (explicit): `python scripts/bootstrap.py`
- Environment sanity check: `python scripts/mock-nvr.py doctor`
- Run single-process (default): `python -m mock_nvr --cameras 4 --http-port 8080 --rtsp-port 8554` (assumes deps already installed)
- Local-only: `python -m mock_nvr run --bind-host 127.0.0.1 --advertise-host 127.0.0.1 ...`
- Supervisor + workers: `python -m mock_nvr supervise --cameras 8 --workers 2 --supervisor-http-port 8090 --worker-http-port-base 8100 ...`

## Big picture architecture
- CLI entrypoint: `mock_nvr/cli.py` (also `python -m mock_nvr` via `mock_nvr/__main__.py`). If no subcommand is provided, it behaves like `run`.
- Frame generation: `mock_nvr/frames.py::FrameSource` runs an async render loop that caches the latest RGB frame + JPEG per camera (HTTP/RTSP read from this cache).
- HTTP server: `mock_nvr/web.py` (aiohttp). Routes: `/` index, `/stats`, `/healthz`, and per-camera `/cam/{id}/snapshot.jpg` + `/cam/{id}/mjpeg`.
- RTSP publishing: `mock_nvr/rtsp.py` spawns per-stream `ffmpeg` publishers (`RtspStreamer`) that ingest raw RGB frames on stdin and publish to an RTSP server. `rtsp_pump()` writes frames on a cadence without blocking the event loop.
- RTSP backend: `mock_nvr/mediamtx.py` can auto-start MediaMTX and writes an auto-generated config at `logs/mediamtx.yml`.
- Supervisor mode: `mock_nvr/supervisor.py` spawns worker processes (`sys.executable -m mock_nvr ... --no-start-mediamtx`) and serves a supervisor UI + aggregated `/stats`.

## Project-specific conventions / gotchas
- Avoid blocking the asyncio loop: heavy CPU work uses `asyncio.to_thread(...)` (frame rendering, JPEG encoding, ffmpeg stdin writes). Keep that pattern.
- `--bind-host` controls what interfaces servers listen on; `--advertise-host` only affects printed/HTML URLs (it can be an `IPADDR` placeholder).
- RTSP “publish host” is for ffmpeg -> RTSP server connectivity (`--rtsp-publish-host`, default `127.0.0.1`). When running a single shared MediaMTX, workers typically use `--no-start-mediamtx`.
- If you add/change CLI flags used by workers, update both `mock_nvr/cli.py` and `Supervisor._spawn_worker()` so supervisor-spawned workers keep working.
- Logging is structured and rotated: `mock_nvr/logging.py::configure_logging()` writes to `logs/<process>_<pid>.log` and optionally deletes old logs via `--log-retention-days`.

## Where to look when changing behavior
- Add/adjust HTTP endpoints or HTML: `mock_nvr/web.py`.
- Change video content/perf: `mock_nvr/frames.py` (thread-safe cache + render cadence).
- RTSP/ffmpeg flags or restart behavior: `mock_nvr/rtsp.py`.
- MediaMTX lifecycle/config: `mock_nvr/mediamtx.py`.
- Multi-process scaling: `mock_nvr/supervisor.py`.
