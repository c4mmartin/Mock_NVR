from __future__ import annotations

import asyncio
import subprocess
import time
from typing import Dict, Optional

from .logging import get_logger

from .frames import FrameSource
from .models import AppState


class RtspStreamer:
    def __init__(self, *, name: str, url: str, width: int, height: int, fps: int, codec: str):
        self.name = name
        self.url = url
        self.width = width
        self.height = height
        self.fps = fps
        self.codec = codec
        self.proc: Optional[subprocess.Popen] = None
        self._log_task: Optional[asyncio.Task] = None

    def start(self):
        if self.proc is not None:
            return

        log = get_logger().bind(stream=self.name, codec=self.codec)

        # ffmpeg RTSP publisher mode (publishes to an RTSP server like MediamTX)
        # - input: raw RGB frames via stdin
        # - output: rtsp://127.0.0.1:<port>/...
        vcodec = {"h264": "libx264", "h265": "libx265", "hevc": "libx265"}.get(
            self.codec, self.codec
        )

        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "info",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-s",
            f"{self.width}x{self.height}",
            "-r",
            str(self.fps),
            "-i",
            "-",
            "-an",
            "-c:v",
            vcodec,
            "-preset",
            "ultrafast",
            "-tune",
            "zerolatency",
            "-pix_fmt",
            "yuv420p",
            "-g",
            str(self.fps * 2),
            "-keyint_min",
            str(self.fps * 2),
            "-f",
            "rtsp",
            "-rtsp_transport",
            "tcp",
            self.url,
        ]

        # Pipe output so we can log it with rotation.
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        log.info("ffmpeg_started", pid=self.proc.pid, url=self.url)

        # If ffmpeg exits immediately (bad flags/port busy/etc), leave a breadcrumb in logs.
        if self.proc.poll() is not None:
            log.warning("ffmpeg_exited_immediately")
            self.stop()
            return

        # Start background reader (once we have an event loop).
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop is not None and self.proc.stdout is not None:
            self._log_task = asyncio.create_task(self._read_ffmpeg_output(self.proc.stdout))

    def write_frame(self, rgb_bytes: bytes):
        if self.proc is None or self.proc.stdin is None:
            return
        try:
            self.proc.stdin.write(rgb_bytes)
            # Ensure frames reach ffmpeg promptly (stdin is a buffered file object).
            self.proc.stdin.flush()
        except BrokenPipeError:
            # Client disconnects / server reset -> allow restart
            self.stop()

    def stop(self):
        if self.proc is None:
            return
        log = get_logger().bind(stream=self.name, codec=self.codec)
        try:
            if self.proc.stdin:
                try:
                    self.proc.stdin.close()
                except Exception:
                    pass
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        finally:
            self.proc = None
            if self._log_task is not None:
                self._log_task.cancel()
                self._log_task = None
        log.info("ffmpeg_stopped")

    async def _read_ffmpeg_output(self, stdout):
        log = get_logger().bind(stream=self.name, codec=self.codec, source="ffmpeg")

        def _reader():
            for raw in iter(stdout.readline, b""):
                try:
                    line = raw.decode("utf-8", errors="replace").rstrip()
                except Exception:
                    line = repr(raw)
                if line:
                    log.info("ffmpeg", line=line)

        try:
            await asyncio.to_thread(_reader)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("ffmpeg_log_reader_failed", error=str(e))


async def rtsp_pump(state: AppState):
    if not state.rtsp_streamers:
        await state.stop_event.wait()
        return

    # One shared cadence per camera; write the same frame to both codecs for that camera.
    per_cam_targets: Dict[int, list[RtspStreamer]] = {}
    for key, streamer in state.rtsp_streamers.items():
        cam_id = int(key.split(":", 1)[0])
        per_cam_targets.setdefault(cam_id, []).append(streamer)

    async def publish_camera(cam_id: int, frame_source: FrameSource, targets: list[RtspStreamer]):
        for streamer in targets:
            streamer.start()

        interval = 1.0 / max(1, frame_source.cfg.fps)
        next_tick = time.monotonic()
        while not state.stop_event.is_set():
            rgb = await frame_source.get_rgb_frame()

            def _write_all():
                for streamer in targets:
                    if streamer.proc is None:
                        streamer.start()
                    streamer.write_frame(rgb)

            await asyncio.to_thread(_write_all)

            next_tick += interval
            sleep_for = next_tick - time.monotonic()
            if sleep_for <= 0:
                next_tick = time.monotonic()
                continue
            await asyncio.sleep(sleep_for)

    tasks: list[asyncio.Task] = []
    for cam_id, frame_source in state.cameras.items():
        targets = per_cam_targets.get(cam_id, [])
        if not targets:
            continue
        tasks.append(asyncio.create_task(publish_camera(cam_id, frame_source, targets)))

    try:
        await state.stop_event.wait()
    finally:
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except asyncio.CancelledError:
                pass
