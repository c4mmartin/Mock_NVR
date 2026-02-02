from __future__ import annotations

import asyncio
import signal
import subprocess
import sys
from dataclasses import dataclass
from typing import Any, Optional

from aiohttp import ClientSession, web

from .mediamtx import mediamtx_path, start_mediamtx
from .logging import get_logger


@dataclass(frozen=True)
class WorkerSpec:
    index: int
    camera_start: int
    cameras: int
    http_port: int


class Supervisor:
    def __init__(
        self,
        *,
        total_cameras: int,
        camera_start: int,
        workers: int,
        worker_http_port_base: int,
        supervisor_http_port: int,
        rtsp_port: int,
        rtsp_publish_host: str,
        advertise_host: str,
        bind_host: str,
        width: int,
        height: int,
        fps: int,
        bg_change_seconds: float,
        move_step: int,
        start_mediamtx_enabled: bool,
        log_level: str,
        log_max_bytes: int,
        log_backups: int,
        log_retention_days: int | None,
    ):
        self.total_cameras = total_cameras
        self.camera_start = camera_start
        self.workers = workers
        self.worker_http_port_base = worker_http_port_base
        self.supervisor_http_port = supervisor_http_port
        self.rtsp_port = rtsp_port
        self.rtsp_publish_host = rtsp_publish_host
        self.advertise_host = advertise_host
        self.bind_host = bind_host
        self.width = width
        self.height = height
        self.fps = fps
        self.bg_change_seconds = bg_change_seconds
        self.move_step = move_step
        self.start_mediamtx_enabled = start_mediamtx_enabled
        self.log_level = log_level
        self.log_max_bytes = log_max_bytes
        self.log_backups = log_backups
        self.log_retention_days = log_retention_days

        self.stop_event = asyncio.Event()
        self.worker_specs: list[WorkerSpec] = []
        self.worker_procs: dict[int, subprocess.Popen] = {}
        self.mediamtx_proc: Optional[subprocess.Popen] = None

    def _build_worker_specs(self) -> list[WorkerSpec]:
        if self.total_cameras <= 0:
            raise ValueError("total_cameras must be > 0")
        if self.workers <= 0:
            raise ValueError("workers must be > 0")

        # Distribute cameras as evenly as possible.
        base = self.total_cameras // self.workers
        rem = self.total_cameras % self.workers

        specs: list[WorkerSpec] = []
        next_cam = self.camera_start
        for i in range(self.workers):
            count = base + (1 if i < rem else 0)
            if count == 0:
                continue
            specs.append(
                WorkerSpec(
                    index=i,
                    camera_start=next_cam,
                    cameras=count,
                    http_port=self.worker_http_port_base + i,
                )
            )
            next_cam += count
        return specs

    def _spawn_worker(self, spec: WorkerSpec) -> subprocess.Popen:
        # Use the current interpreter so it works in venv.
        cmd = [
            sys.executable,
            "-m",
            "mock_nvr",
            "--camera-start",
            str(spec.camera_start),
            "--cameras",
            str(spec.cameras),
            "--http-port",
            str(spec.http_port),
            "--rtsp-port",
            str(self.rtsp_port),
            "--bind-host",
            str(self.bind_host),
            "--rtsp-publish-host",
            str(self.rtsp_publish_host),
            "--advertise-host",
            str(self.advertise_host),
            "--width",
            str(self.width),
            "--height",
            str(self.height),
            "--fps",
            str(self.fps),
            "--bg-change-seconds",
            str(self.bg_change_seconds),
            "--move-step",
            str(self.move_step),
            "--log-level",
            str(self.log_level),
            "--log-max-bytes",
            str(self.log_max_bytes),
            "--log-backups",
            str(self.log_backups),
            "--log-retention-days",
            str(self.log_retention_days or 0),
            "--no-start-mediamtx",
        ]

        return subprocess.Popen(cmd)

    async def start(self) -> int:
        log = get_logger().bind(component="supervisor")
        self.worker_specs = self._build_worker_specs()

        if self.start_mediamtx_enabled:
            exe = mediamtx_path()
            if exe is None:
                log.warning("mediamtx_not_found_rtsp_disabled", hint="brew install mediamtx")
            else:
                try:
                    self.mediamtx_proc, _ = start_mediamtx(self.rtsp_port, bind_host=self.bind_host)
                except Exception as e:
                    log.warning("mediamtx_start_failed", error=str(e))
                    self.mediamtx_proc = None

        for spec in self.worker_specs:
            self.worker_procs[spec.index] = self._spawn_worker(spec)

        runner = web.AppRunner(self._build_app())
        await runner.setup()
        site = web.TCPSite(runner, self.bind_host, self.supervisor_http_port)
        await site.start()

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self.stop_event.set)
            except NotImplementedError:
                pass

        log.info("supervisor_started", http_url=f"http://0.0.0.0:{self.supervisor_http_port}/")
        for spec in self.worker_specs:
            rng = f"{spec.camera_start}..{spec.camera_start + spec.cameras - 1}"
            log.info(
                "worker_started",
                worker=spec.index,
                cameras=rng,
                http_url=f"http://127.0.0.1:{spec.http_port}/",
            )

        try:
            await self.stop_event.wait()
        finally:
            await runner.cleanup()
            self.stop()

        return 0

    def stop(self):
        for proc in self.worker_procs.values():
            try:
                proc.terminate()
            except Exception:
                pass
        for proc in self.worker_procs.values():
            try:
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

        self.worker_procs.clear()

        if self.mediamtx_proc is not None:
            try:
                self.mediamtx_proc.terminate()
                self.mediamtx_proc.wait(timeout=2)
            except Exception:
                pass
            self.mediamtx_proc = None

    async def _fetch_json(self, session: ClientSession, url: str) -> Any:
        async with session.get(url, timeout=2) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def _aggregate_stats(self) -> dict[str, Any]:
        async with ClientSession() as session:
            worker_stats = []
            for spec in self.worker_specs:
                base = f"http://127.0.0.1:{spec.http_port}"
                try:
                    data = await self._fetch_json(session, f"{base}/stats")
                except Exception as e:
                    data = {"error": str(e)}
                worker_stats.append(
                    {
                        "worker": spec.index,
                        "http_port": spec.http_port,
                        "camera_start": spec.camera_start,
                        "cameras": spec.cameras,
                        "stats": data,
                    }
                )

        return {
            "supervisor": {
                "total_cameras": self.total_cameras,
                "camera_start": self.camera_start,
                "workers": self.workers,
                "supervisor_http_port": self.supervisor_http_port,
                "worker_http_port_base": self.worker_http_port_base,
                "rtsp_port": self.rtsp_port,
                "rtsp_publish_host": self.rtsp_publish_host,
                "advertise_host": self.advertise_host,
            },
            "workers": worker_stats,
        }

    async def handle_healthz(self, _: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    async def handle_stats(self, _: web.Request) -> web.Response:
        return web.json_response(await self._aggregate_stats())

    async def handle_index(self, request: web.Request) -> web.Response:
        scheme = request.scheme

        def _split_host_port(authority: str) -> tuple[str, str | None]:
            if not authority:
                return "", None
            if authority.startswith("["):
                end = authority.find("]")
                if end != -1:
                    h = authority[1:end]
                    rest = authority[end + 1 :]
                    if rest.startswith(":"):
                        return h, rest[1:] or None
                    return h, None
            if ":" in authority:
                h, p = authority.rsplit(":", 1)
                if h and p.isdigit():
                    return h, p
            return authority, None

        req_host, _ = _split_host_port(request.host)

        # If the user explicitly set an advertise host (often an IP), prefer that
        # so the dashboard consistently points at the intended interface.
        host = self.advertise_host if self.advertise_host and self.advertise_host != "IPADDR" else req_host

        def _host_for_url(h: str) -> str:
            # Basic IPv6 bracket handling.
            if ":" in h and not h.startswith("["):
                return f"[{h}]"
            return h

        host_url = _host_for_url(host)

        rows = []
        for spec in self.worker_specs:
            base = f"{scheme}://{host_url}:{spec.http_port}"
            rng = f"{spec.camera_start}..{spec.camera_start + spec.cameras - 1}"
            rows.append(
                "<tr>"
                f"<td>{spec.index}</td>"
                f"<td>{rng}</td>"
                f"<td><a href='{base}/'>worker ui</a></td>"
                f"<td><a href='{base}/stats'>worker stats</a></td>"
                "</tr>"
            )

        html = f"""<!doctype html>
<html>
<head>
  <meta charset='utf-8'>
  <meta name='viewport' content='width=device-width, initial-scale=1'>
  <title>mock_nvr supervisor</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif; padding: 24px; }}
    table {{ border-collapse: collapse; width: 100%; }}
    th, td {{ border: 1px solid #ddd; padding: 10px; text-align: left; }}
    th {{ background: #f5f5f5; }}
    code {{ background: #f0f0f0; padding: 2px 6px; border-radius: 6px; }}
  </style>
</head>
<body>
  <h1>mock_nvr supervisor</h1>
  <p>Aggregated stats: <a href='/stats'>/stats</a></p>
  <table>
    <thead>
      <tr><th>Worker</th><th>Cameras</th><th>UI</th><th>Stats</th></tr>
    </thead>
    <tbody>
      {''.join(rows)}
    </tbody>
  </table>
</body>
</html>"""

        return web.Response(text=html, content_type="text/html")

    def _build_app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/", self.handle_index)
        app.router.add_get("/healthz", self.handle_healthz)
        app.router.add_get("/stats", self.handle_stats)
        return app
