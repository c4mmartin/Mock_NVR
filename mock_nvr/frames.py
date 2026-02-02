from __future__ import annotations

import asyncio
import io
import random
import threading
import time
from typing import Optional, Tuple

from PIL import Image, ImageDraw, ImageFont

from .models import CameraConfig


class FrameSource:
    def __init__(self, cfg: CameraConfig):
        self.cfg = cfg
        self._lock = threading.Lock()
        self._latest_rgb: Optional[bytes] = None
        self._latest_jpeg: Optional[bytes] = None
        self._latest_frame_monotonic: float = 0.0

        self._render_task: Optional[asyncio.Task] = None

        # lightweight stats
        self._frames_rendered: int = 0
        self._jpeg_encodes: int = 0
        self._render_overruns: int = 0
        self._last_render_ms: float = 0.0
        self._avg_render_ms: float = 0.0

        self._bg = self._random_bg()
        self._x = random.randint(0, max(0, cfg.width - 240))
        self._y = random.randint(0, max(0, cfg.height - 80))
        self._vx = cfg.move_step_pixels
        self._vy = cfg.move_step_pixels
        self._next_bg_change = time.monotonic() + cfg.bg_change_seconds

        self._font = self._load_font()

        # Default JPEG quality used by HTTP snapshot/MJPEG.
        self._jpeg_quality_default = 80

    def _load_font(self):
        # Degrade gracefully on macOS/Linux; PIL default font is fine.
        try:
            return ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 36)
        except Exception:
            return ImageFont.load_default()

    def _random_bg(self) -> Tuple[int, int, int]:
        # Avoid too-dark and too-light backgrounds.
        return (
            random.randint(30, 220),
            random.randint(30, 220),
            random.randint(30, 220),
        )

    def _step_state(self):
        now = time.monotonic()
        if now >= self._next_bg_change:
            self._bg = self._random_bg()
            self._next_bg_change = now + self.cfg.bg_change_seconds

        pad = 12
        box_w = 260
        box_h = 90

        self._x += self._vx
        self._y += self._vy

        if self._x < pad:
            self._x = pad
            self._vx = abs(self._vx)
        if self._y < pad:
            self._y = pad
            self._vy = abs(self._vy)

        if self._x + box_w > self.cfg.width - pad:
            self._x = max(pad, self.cfg.width - pad - box_w)
            self._vx = -abs(self._vx)
        if self._y + box_h > self.cfg.height - pad:
            self._y = max(pad, self.cfg.height - pad - box_h)
            self._vy = -abs(self._vy)

    def _render_rgb(self) -> bytes:
        self._step_state()

        img = Image.new("RGB", (self.cfg.width, self.cfg.height), self._bg)
        draw = ImageDraw.Draw(img)

        # simple vignette-ish border
        draw.rectangle(
            [0, 0, self.cfg.width - 1, self.cfg.height - 1],
            outline=(0, 0, 0),
            width=2,
        )

        label = f"CAM {self.cfg.camera_id}"
        ts = time.strftime("%Y-%m-%d %H:%M:%S")

        x0, y0 = self._x, self._y
        x1, y1 = x0 + 260, y0 + 90

        draw.rounded_rectangle(
            [x0, y0, x1, y1],
            radius=12,
            fill=(0, 0, 0),
            outline=(255, 255, 255),
            width=2,
        )
        draw.text((x0 + 16, y0 + 10), label, fill=(255, 255, 255), font=self._font)
        draw.text(
            (x0 + 16, y0 + 52),
            ts,
            fill=(200, 200, 200),
            font=ImageFont.load_default(),
        )

        return img.tobytes()

    def _render_rgb_sync(self) -> bytes:
        # Render (and update animation state) under the lock.
        with self._lock:
            rgb = self._render_rgb()

        # Encode JPEG outside the lock (can be CPU-heavy).
        jpg = self._encode_jpeg_sync(rgb, self._jpeg_quality_default)

        # Publish the new frame atomically.
        with self._lock:
            self._latest_rgb = rgb
            self._latest_jpeg = jpg
            self._latest_frame_monotonic = time.monotonic()
            self._frames_rendered += 1
            self._jpeg_encodes += 1
            return rgb

    def _encode_jpeg_sync(self, rgb: bytes, quality: int) -> bytes:
        img = Image.frombytes("RGB", (self.cfg.width, self.cfg.height), rgb)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality)
        return buf.getvalue()

    def start(self, stop_event: asyncio.Event) -> asyncio.Task:
        if self._render_task is not None:
            return self._render_task
        self._render_task = asyncio.create_task(self._render_loop(stop_event))
        return self._render_task

    async def _render_loop(self, stop_event: asyncio.Event):
        interval = 1.0 / max(1, self.cfg.fps)
        next_tick = time.monotonic()
        while not stop_event.is_set():
            t0 = time.monotonic()
            await asyncio.to_thread(self._render_rgb_sync)
            t1 = time.monotonic()

            render_ms = (t1 - t0) * 1000.0
            with self._lock:
                self._last_render_ms = render_ms
                # simple EMA
                if self._avg_render_ms <= 0.0:
                    self._avg_render_ms = render_ms
                else:
                    self._avg_render_ms = (0.9 * self._avg_render_ms) + (0.1 * render_ms)

            next_tick += interval
            sleep_for = next_tick - time.monotonic()
            if sleep_for <= 0:
                # We fell behind; reset schedule and count an overrun.
                with self._lock:
                    self._render_overruns += 1
                next_tick = time.monotonic()
                continue
            await asyncio.sleep(sleep_for)

    def get_stats(self) -> dict:
        now = time.monotonic()
        with self._lock:
            age_s = (now - self._latest_frame_monotonic) if self._latest_frame_monotonic else None
            return {
                "camera_id": self.cfg.camera_id,
                "width": self.cfg.width,
                "height": self.cfg.height,
                "fps": self.cfg.fps,
                "frames_rendered": self._frames_rendered,
                "jpeg_encodes": self._jpeg_encodes,
                "render_overruns": self._render_overruns,
                "last_render_ms": round(self._last_render_ms, 2),
                "avg_render_ms": round(self._avg_render_ms, 2),
                "last_frame_age_s": (round(age_s, 3) if age_s is not None else None),
            }

    async def get_rgb_frame(self) -> bytes:
        with self._lock:
            rgb = self._latest_rgb

        # If the background loop hasn't produced a frame yet, render one.
        if rgb is None:
            rgb = await asyncio.to_thread(self._render_rgb_sync)
        return rgb

    async def get_jpeg(self, quality: int = 80) -> bytes:
        with self._lock:
            cached = self._latest_jpeg
            rgb = self._latest_rgb

        if cached is not None and quality == self._jpeg_quality_default:
            return cached
        if rgb is None:
            rgb = await self.get_rgb_frame()

        # Non-default quality: encode on demand.
        return await asyncio.to_thread(self._encode_jpeg_sync, rgb, quality)
