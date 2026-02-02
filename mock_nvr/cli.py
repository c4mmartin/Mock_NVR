from __future__ import annotations

import argparse
import asyncio
import os
import signal
import socket
import sys
from pathlib import Path
from shutil import which
from typing import Dict

from aiohttp import web

from .firewall import (
    FirewallRule,
    apply_commands,
    build_close_commands,
    build_open_commands,
    detect_backend,
    format_commands,
)
from .frames import FrameSource
from .mediamtx import mediamtx_path, start_mediamtx
from .models import AppState, CameraConfig
from .rtsp import RtspStreamer, rtsp_pump
from .supervisor import Supervisor
from .web import build_app
from .logging import configure_logging, get_logger


def _ffmpeg_exists() -> bool:
    return which("ffmpeg") is not None


def _add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--cameras",
        type=int,
        default=4,
        help="Number of cameras to run (starting at --camera-start)",
    )
    parser.add_argument(
        "--camera-start",
        type=int,
        default=1,
        help="First camera ID (useful for splitting across multiple processes)",
    )
    parser.add_argument("--http-port", type=int, default=8080)
    parser.add_argument("--rtsp-port", type=int, default=8554)
    parser.add_argument(
        "--bind-host",
        type=str,
        default="0.0.0.0",
        help="Bind address for HTTP servers (and auto-started MediamTX). Use 127.0.0.1 for local-only.",
    )
    parser.add_argument(
        "--rtsp-publish-host",
        type=str,
        default="127.0.0.1",
        help="Host/IP where the RTSP server is reachable for publishing (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--no-start-mediamtx",
        action="store_true",
        help="Do not auto-start MediamTX (run it separately if you want RTSP)",
    )
    parser.add_argument(
        "--rtsp-tcp-only",
        action="store_true",
        help="When auto-starting MediamTX, only allow RTSP-over-TCP for clients (often more stable on Wi-Fi/smart TVs)",
    )
    parser.add_argument(
        "--advertise-host",
        type=str,
        default="IPADDR",
        help="Host/IP to print in URLs (default: IPADDR placeholder)",
    )
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--fps", type=int, default=10)
    parser.add_argument(
        "--mjpeg-fps",
        type=float,
        default=0.0,
        help="MJPEG stream send rate (frames/sec). 0 = follow --fps",
    )

    # RTSP encoder knobs (primarily for compatibility + bandwidth).
    parser.add_argument(
        "--rtsp-bitrate-kbps",
        type=int,
        default=0,
        help="If >0, cap video bitrate for RTSP streams (kbps). Helps reduce network load.",
    )
    parser.add_argument(
        "--rtsp-codecs",
        type=str,
        default="h264,h265",
        help="Comma-separated RTSP codecs to publish: h264, h265 (default: h264,h265). Use 'h264' to cut bandwidth.",
    )
    parser.add_argument(
        "--rtsp-gop-seconds",
        type=float,
        default=2.0,
        help="Keyframe interval in seconds for RTSP streams (default: 2.0)",
    )
    parser.add_argument(
        "--rtsp-h264-profile",
        type=str,
        default="baseline",
        choices=("baseline", "main", "high"),
        help="H.264 profile used for RTSP (default: baseline for broad compatibility)",
    )
    parser.add_argument(
        "--rtsp-h264-level",
        type=str,
        default="",
        help="Optional H.264 level (e.g. 3.1, 4.0). Some embedded decoders require this.",
    )
    parser.add_argument("--bg-change-seconds", type=float, default=3.0)
    parser.add_argument("--move-step", type=int, default=18)

    parser.add_argument("--log-level", type=str, default="INFO")
    parser.add_argument("--log-max-bytes", type=int, default=10 * 1024 * 1024)
    parser.add_argument("--log-backups", type=int, default=5)
    parser.add_argument(
        "--log-retention-days",
        type=int,
        default=0,
        help="Delete log files older than N days on startup (0 disables)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Quick and dirty mock NVR")
    sub = parser.add_subparsers(dest="cmd")

    run_p = sub.add_parser("run", help="Run cameras in a single process")
    _add_common_args(run_p)

    sup_p = sub.add_parser("supervise", help="Run multiple worker processes and aggregate stats")
    _add_common_args(sup_p)
    sup_p.add_argument(
        "--workers",
        type=int,
        default=max(1, (os_cpu_count() or 2) // 2),
        help="Number of worker processes to spawn",
    )
    sup_p.add_argument(
        "--worker-http-port-base",
        type=int,
        default=8100,
        help="Base port for worker HTTP servers (worker i uses base+i)",
    )
    sup_p.add_argument(
        "--supervisor-http-port",
        type=int,
        default=8090,
        help="HTTP port for supervisor dashboard",
    )
    sup_p.add_argument(
        "--start-mediamtx",
        action="store_true",
        help="Start a MediamTX instance in the supervisor process",
    )

    doc_p = sub.add_parser("doctor", help="Check environment and dependencies")
    doc_p.add_argument("--http-port", type=int, default=8080)
    doc_p.add_argument("--rtsp-port", type=int, default=8554)
    doc_p.add_argument("--supervisor-http-port", type=int, default=8090)

    fw_p = sub.add_parser("firewall", help="Print/apply firewall rules for ports")
    fw_p.add_argument("action", choices=("open", "close"), help="Open or close firewall rules")
    fw_p.add_argument("--http-port", type=int, default=8080)
    fw_p.add_argument("--rtsp-port", type=int, default=8554)
    fw_p.add_argument("--supervisor-http-port", type=int, default=8090)
    fw_p.add_argument(
        "--workers",
        type=int,
        default=0,
        help="If set (>0), also include worker HTTP ports (worker-http-port-base .. base+workers-1)",
    )
    fw_p.add_argument(
        "--worker-http-port-base",
        type=int,
        default=8100,
        help="Base port for worker HTTP servers when including workers",
    )
    fw_p.add_argument(
        "--apply",
        action="store_true",
        help="Actually run the firewall commands (requires admin/root). Default is print-only.",
    )
    fw_p.add_argument(
        "--yes",
        action="store_true",
        help="Required with --apply to confirm you want to modify firewall rules",
    )

    return parser


def _is_port_available(host: str, port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((host, port))
        return True
    except OSError:
        return False


def _doctor_print_install_hints() -> None:
    if os.name == "nt":
        print("Install hints (Windows):")
        print("  - Python: https://www.python.org/downloads/ (or Microsoft Store)")
        print("  - ffmpeg: winget install Gyan.FFmpeg")
        print("  - mediamtx: download MediaMTX release or `winget search mediamtx`")
    else:
        print("Install hints (macOS/Linux):")
        print("  - ffmpeg (macOS): brew install ffmpeg")
        print("  - mediamtx (macOS): brew install mediamtx")
        print("  - On Linux, use your distro package manager for ffmpeg; MediaMTX from releases if missing")


def _find_project_root() -> Path:
    """Best-effort project root discovery.

    This avoids assuming the current working directory is the repo root.
    """

    markers = (
        ".venv",
        "pyproject.toml",
        "requirements.txt",
        ".git",
    )

    def score(p: Path) -> int:
        # Prefer paths that actually contain a venv.
        return 10 if (p / ".venv").exists() else 0

    starts: list[Path] = []
    try:
        starts.append(Path.cwd().resolve())
    except Exception:
        starts.append(Path.cwd())

    try:
        starts.append(Path(__file__).resolve())
    except Exception:
        starts.append(Path(__file__))

    candidates: list[Path] = []
    for start in starts:
        base = start if start.is_dir() else start.parent
        for p in (base, *base.parents):
            if any((p / m).exists() for m in markers):
                candidates.append(p)

    if candidates:
        return sorted(candidates, key=score, reverse=True)[0]

    return starts[0] if starts else Path.cwd()


def doctor(args: argparse.Namespace) -> int:
    ok = True

    print("mock_nvr doctor")
    print(f"- python: {sys.version.split()[0]}")
    print(f"- executable: {sys.executable}")

    if sys.version_info < (3, 10):
        ok = False
        print("- ERROR: Python 3.10+ required")

    ff = which("ffmpeg")
    print(f"- ffmpeg: {ff if ff else 'NOT FOUND'}")
    if ff is None:
        ok = False

    mt = which("mediamtx")
    print(f"- mediamtx: {mt if mt else 'NOT FOUND'}")
    # mediamtx is optional (RTSP can be disabled / external), so don't fail hard

    root = _find_project_root()
    print(f"- project_root: {root}")

    venv_py = root / ".venv" / ("Scripts" if os.name == "nt" else "bin") / (
        "python" + (".exe" if os.name == "nt" else "")
    )
    if venv_py.exists():
        in_venv = os.path.abspath(sys.executable) == os.path.abspath(venv_py)
        print(f"- venv: found (.venv){' (active)' if in_venv else ' (not active)'}")
        if not in_venv:
            print("  - Tip: run via `python scripts/mock-nvr.py ...` to auto-use the venv")
    else:
        print("- venv: NOT FOUND (.venv)")
        print("  - Tip: run `python scripts/bootstrap.py` to create it")

    print("- ports:")
    for label, port in (
        ("http", int(args.http_port)),
        ("rtsp", int(args.rtsp_port)),
        ("supervisor_http", int(args.supervisor_http_port)),
    ):
        available = _is_port_available("127.0.0.1", port)
        print(f"  - {label}: {port} {'OK' if available else 'IN USE'}")
        if not available:
            ok = False

    if not ok:
        print("\nResult: NOT OK")
        _doctor_print_install_hints()
        return 2

    print("\nResult: OK")
    return 0


def firewall(args: argparse.Namespace) -> int:
    rules: list[FirewallRule] = [
        FirewallRule(name="http", port=int(args.http_port)),
        FirewallRule(name="rtsp", port=int(args.rtsp_port)),
        FirewallRule(name="supervisor_http", port=int(args.supervisor_http_port)),
    ]

    workers = int(getattr(args, "workers", 0) or 0)
    if workers > 0:
        base = int(getattr(args, "worker_http_port_base", 8100))
        for i in range(workers):
            rules.append(FirewallRule(name=f"worker_http_{i}", port=base + i))

    backend = detect_backend()
    print(f"mock_nvr firewall ({backend})")
    print(f"- action: {args.action}")
    print(f"- ports: {', '.join(str(r.port) for r in rules)}")

    if args.action == "open":
        cmds = build_open_commands(rules=rules, python_executable=sys.executable)
    else:
        cmds = build_close_commands(rules=rules, python_executable=sys.executable)

    if not cmds:
        print("- ERROR: No supported firewall backend detected for this OS.")
        print("  - Tip: open ports manually in your system firewall (allow inbound TCP to the ports above)")
        return 2

    print("- commands:")
    print(format_commands(cmds))

    if not args.apply:
        print("\nResult: PRINT ONLY (re-run with --apply --yes to execute)")
        return 0

    if not args.yes:
        print("\nResult: NOT OK (refusing to modify firewall without --yes)")
        return 2

    ok, messages = apply_commands(cmds, assume_yes=True)
    for m in messages:
        print(m)

    if not ok:
        print("\nResult: NOT OK")
        return 2

    print("\nResult: OK")
    return 0


def os_cpu_count() -> int | None:
    try:
        import os

        return os.cpu_count()
    except Exception:
        return None


async def run(args: argparse.Namespace) -> int:
    configure_logging(
        name="mock_nvr",
        level=args.log_level,
        max_bytes=args.log_max_bytes,
        backups=args.log_backups,
        retention_days=(args.log_retention_days or None),
    )
    log = get_logger()

    if not _ffmpeg_exists():
        raise SystemExit("ffmpeg not found on PATH. Install with: brew install ffmpeg")

    mt_exe = mediamtx_path()
    if args.no_start_mediamtx:
        mt_exe = None
    elif mt_exe is None:
        log.warning("mediamtx_not_found_rtsp_disabled", hint="brew install mediamtx")

    cameras: Dict[int, FrameSource] = {}
    if args.cameras <= 0:
        raise SystemExit("--cameras must be > 0")
    if args.camera_start <= 0:
        raise SystemExit("--camera-start must be > 0")

    for cam_id in range(args.camera_start, args.camera_start + args.cameras):
        cfg = CameraConfig(
            camera_id=cam_id,
            width=args.width,
            height=args.height,
            fps=args.fps,
            bg_change_seconds=args.bg_change_seconds,
            move_step_pixels=args.move_step,
        )
        cameras[cam_id] = FrameSource(cfg)

    rtsp_streamers: Dict[str, RtspStreamer] = {}
    # If MediamTX isn't available in this process, we can still publish to an external RTSP server.
    # We'll only enable RTSP publishers when either:
    # - we can start MediamTX ourselves, or
    # - the user explicitly points us at a publish host.
    enable_rtsp_publishers = (not args.no_start_mediamtx and mt_exe is not None) or bool(
        args.rtsp_publish_host
    )

    if enable_rtsp_publishers:
        codecs_raw = (args.rtsp_codecs or "").strip()
        selected = [c.strip().lower() for c in codecs_raw.split(",") if c.strip()]
        allowed = {"h264", "h265"}
        selected_codecs = [c for c in selected if c in allowed]
        if not selected_codecs:
            raise SystemExit("--rtsp-codecs must include at least one of: h264,h265")

        # Publish to localhost RTSP server; clients connect to the external address/port.
        for cam_id in cameras.keys():
            for codec in selected_codecs:
                path = f"cam{cam_id}_{codec}"
                url = f"rtsp://{args.rtsp_publish_host}:{args.rtsp_port}/{path}"
                key = f"{cam_id}:{codec}"
                rtsp_streamers[key] = RtspStreamer(
                    name=path,
                    url=url,
                    width=args.width,
                    height=args.height,
                    fps=args.fps,
                    codec=codec,
                    bitrate_kbps=args.rtsp_bitrate_kbps,
                    gop_seconds=args.rtsp_gop_seconds,
                    h264_profile=args.rtsp_h264_profile,
                    h264_level=(args.rtsp_h264_level or "").strip() or None,
                )

    state = AppState(
        cameras=cameras,
        rtsp_port=args.rtsp_port,
        rtsp_streamers=rtsp_streamers,
        stop_event=asyncio.Event(),
        http_port=args.http_port,
        advertise_host=args.advertise_host,
        mjpeg_fps=args.mjpeg_fps,
    )

    # Start background frame render loops so HTTP/RTSP are reading cached frames.
    render_tasks = [cam.start(state.stop_event) for cam in state.cameras.values()]

    app = build_app(state)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, args.bind_host, args.http_port)
    await site.start()

    if mt_exe is not None and not args.no_start_mediamtx:
        try:
            transports = ["tcp"] if args.rtsp_tcp_only else ["tcp", "udp"]
            state.rtsp_backend_proc, _ = start_mediamtx(
                args.rtsp_port,
                bind_host=args.bind_host,
                rtsp_transports=transports,
            )
        except Exception as e:
            log.warning("mediamtx_start_failed", error=str(e))
            state.rtsp_backend_proc = None
            state.rtsp_streamers = {}

    pump_task = asyncio.create_task(rtsp_pump(state))

    # Handle Ctrl+C
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, state.stop_event.set)
        except NotImplementedError:
            pass

    log.info("server_started", http_url=f"http://0.0.0.0:{args.http_port}/")
    log.info("url_advertise_host", advertise_host=state.advertise_host)
    log.info("streams")
    for cam_id in sorted(cameras.keys()):
        log.info(
            "camera_http",
            camera_id=cam_id,
            snapshot=f"http://{state.advertise_host}:{args.http_port}/cam/{cam_id}/snapshot.jpg",
            mjpeg=f"http://{state.advertise_host}:{args.http_port}/cam/{cam_id}/mjpeg",
        )
        if state.rtsp_streamers:
            log.info(
                "camera_rtsp",
                camera_id=cam_id,
                h264=f"rtsp://{state.advertise_host}:{args.rtsp_port}/cam{cam_id}_h264",
                h265=f"rtsp://{state.advertise_host}:{args.rtsp_port}/cam{cam_id}_h265",
            )

    try:
        await state.stop_event.wait()
    finally:
        for t in render_tasks:
            t.cancel()
        for t in render_tasks:
            try:
                await t
            except asyncio.CancelledError:
                pass

        pump_task.cancel()
        try:
            await pump_task
        except asyncio.CancelledError:
            pass

        for streamer in state.rtsp_streamers.values():
            streamer.stop()

        if state.rtsp_backend_proc is not None:
            try:
                state.rtsp_backend_proc.terminate()
                state.rtsp_backend_proc.wait(timeout=2)
            except Exception:
                pass
            state.rtsp_backend_proc = None

        await runner.cleanup()

    return 0


def main(argv: list[str] | None = None) -> int:
    # Backwards compatible: if no subcommand is provided, treat it as `run`.
    raw = list(argv) if argv is not None else sys.argv[1:]
    if not raw or raw[0].startswith("-"):
        raw = ["run", *raw]

    parser = build_parser()
    args = parser.parse_args(raw)

    if args.cmd == "doctor":
        return doctor(args)

    if args.cmd == "firewall":
        return firewall(args)

    if args.cmd == "supervise":
        configure_logging(
            name="supervisor",
            level=args.log_level,
            max_bytes=args.log_max_bytes,
            backups=args.log_backups,
            retention_days=(args.log_retention_days or None),
        )
        supervisor = Supervisor(
            total_cameras=args.cameras,
            camera_start=args.camera_start,
            workers=args.workers,
            worker_http_port_base=args.worker_http_port_base,
            supervisor_http_port=args.supervisor_http_port,
            rtsp_port=args.rtsp_port,
            rtsp_publish_host=args.rtsp_publish_host,
            advertise_host=args.advertise_host,
            bind_host=args.bind_host,
            width=args.width,
            height=args.height,
            fps=args.fps,
            mjpeg_fps=args.mjpeg_fps,
            rtsp_tcp_only=bool(args.rtsp_tcp_only),
            rtsp_bitrate_kbps=int(args.rtsp_bitrate_kbps),
            rtsp_codecs=str(args.rtsp_codecs),
            rtsp_gop_seconds=float(args.rtsp_gop_seconds),
            rtsp_h264_profile=str(args.rtsp_h264_profile),
            rtsp_h264_level=(args.rtsp_h264_level or "").strip() or None,
            bg_change_seconds=args.bg_change_seconds,
            move_step=args.move_step,
            start_mediamtx_enabled=bool(args.start_mediamtx) and (not args.no_start_mediamtx),
            log_level=args.log_level,
            log_max_bytes=args.log_max_bytes,
            log_backups=args.log_backups,
            log_retention_days=(args.log_retention_days or None),
        )
        return asyncio.run(supervisor.start())

    return asyncio.run(run(args))
