from __future__ import annotations

import asyncio
import subprocess
from dataclasses import dataclass
from typing import Dict, Optional


@dataclass(frozen=True)
class CameraConfig:
    camera_id: int
    width: int
    height: int
    fps: int
    bg_change_seconds: float
    move_step_pixels: int


@dataclass
class AppState:
    cameras: Dict[int, "FrameSource"]
    rtsp_port: int
    rtsp_streamers: Dict[str, "RtspStreamer"]
    stop_event: asyncio.Event
    rtsp_backend_proc: Optional[subprocess.Popen] = None
    http_port: int = 0
    advertise_host: str = "IPADDR"


# Only used for typing; avoids circular imports at runtime.
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from .frames import FrameSource
    from .rtsp import RtspStreamer
