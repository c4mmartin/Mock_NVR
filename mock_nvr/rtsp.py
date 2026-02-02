from __future__ import annotations

import asyncio
import subprocess
import time
from typing import Dict, Optional

from .logging import get_logger

from .frames import FrameSource
from .models import AppState


class RtspStreamer:
    def __init__(
        self,
        *,
        name: str,
        url: str,
        width: int,
        height: int,
        fps: int,
        codec: str,
        bitrate_kbps: int = 0,
        gop_seconds: float = 2.0,
        h264_profile: str = "baseline",
        h264_level: str | None = None,
    ):
        self.name = name
        self.url = url
        self.width = width
        self.height = height
        self.fps = fps
        self.codec = codec
        self.bitrate_kbps = bitrate_kbps
        self.gop_seconds = gop_seconds
        self.h264_profile = h264_profile
        self.h264_level = h264_level
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

        # GOP/keyframe interval. Keep it bounded for decoder recovery.
        gop_frames = max(1, int(round(max(0.1, self.gop_seconds) * max(1, self.fps))))

        # Optional bitrate cap to reduce network load.
        bitrate_args: list[str] = []
        if self.bitrate_kbps and self.bitrate_kbps > 0:
            b = int(self.bitrate_kbps)
            bitrate_args = [
                "-b:v",
                f"{b}k",
                "-maxrate",
                f"{b}k",
                "-bufsize",
                f"{max(2 * b, 1)}k",
            ]

        # Some decoders (notably embedded/smart TV stacks) behave better when SPS/PPS
        # are repeated with keyframes.
        codec_params: list[str] = []
        if vcodec == "libx264":
            codec_params = [
                "-profile:v",
                self.h264_profile,
                *( ["-level:v", str(self.h264_level)] if self.h264_level else [] ),
                "-x264-params",
                f"repeat-headers=1:keyint={gop_frames}:min-keyint={gop_frames}:scenecut=0",
            ]
        elif vcodec == "libx265":
            codec_params = [
                "-x265-params",
                f"repeat-headers=1:keyint={gop_frames}:min-keyint={gop_frames}:scenecut=0",
            ]

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
            *bitrate_args,
            *codec_params,
            "-pix_fmt",
            "yuv420p",
            "-g",
            str(gop_frames),
            "-keyint_min",
            str(gop_frames),
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
