from __future__ import annotations

import asyncio

from aiohttp import web

from .models import AppState
from .logging import get_logger


def _split_host_port(authority: str) -> tuple[str, str | None]:
    """Split an HTTP Host header / authority into host and port.

    Handles:
    - "example.com"
    - "example.com:8100"
    - "[::1]:8100"
    """

    if not authority:
        return "", None

    # IPv6 in brackets.
    if authority.startswith("["):
        end = authority.find("]")
        if end != -1:
            host = authority[1:end]
            rest = authority[end + 1 :]
            if rest.startswith(":"):
                return host, rest[1:] or None
            return host, None

    if ":" in authority:
        host, port = authority.rsplit(":", 1)
        if host and port.isdigit():
            return host, port

    return authority, None


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _parse_float(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except Exception:
        return None


def _parse_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except Exception:
        return None


async def healthz(_: web.Request) -> web.Response:
    return web.json_response({"ok": True})


async def stats(request: web.Request) -> web.Response:
    state: AppState = request.app["state"]
    cams = [state.cameras[cid].get_stats() for cid in sorted(state.cameras.keys())]
    return web.json_response(
        {
            "cameras": cams,
            "rtsp_port": state.rtsp_port,
            "http_port": state.http_port,
            "advertise_host": state.advertise_host,
            "rtsp_enabled": bool(state.rtsp_streamers),
        }
    )


async def mjpeg_stream(request: web.Request) -> web.StreamResponse:
    cam_id = int(request.match_info["cam_id"])
    state: AppState = request.app["state"]

    get_logger().info(
        "http_mjpeg_open",
        cam_id=cam_id,
        remote=getattr(request, "remote", None),
        host=request.host,
        path=request.path,
        ua=request.headers.get("User-Agent"),
    )

    if cam_id not in state.cameras:
        raise web.HTTPNotFound(text="Unknown camera")

    # Per-request tuning knobs (handy for smart TVs / constrained clients).
    # - fps: frames/sec
    # - interval_ms: explicit cadence override
    # - quality: JPEG quality (1..95)
    q = request.rel_url.query
    quality = _parse_int(q.get("quality"))
    if quality is None:
        quality = 80
    quality = int(_clamp(float(quality), 1.0, 95.0))

    interval_ms = _parse_int(q.get("interval_ms"))
    fps_override = _parse_float(q.get("fps"))

    boundary = "frame"
    resp = web.StreamResponse(
        status=200,
        reason="OK",
        headers={
            "Content-Type": f"multipart/x-mixed-replace; boundary={boundary}",
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
            # Helps when running behind reverse proxies that buffer responses.
            "X-Accel-Buffering": "no",
        },
    )
    await resp.prepare(request)

    if interval_ms is not None and interval_ms > 0:
        frame_interval = _clamp(interval_ms / 1000.0, 0.05, 60.0)
    else:
        default_fps = getattr(state, "mjpeg_fps", 0.0) or 0.0
        if default_fps <= 0:
            default_fps = float(state.cameras[cam_id].cfg.fps)

        fps = fps_override if (fps_override is not None and fps_override > 0) else default_fps
        fps = _clamp(float(fps), 0.1, 60.0)
        frame_interval = 1.0 / fps
    frames_sent = 0

    try:
        while True:
            jpg = await state.cameras[cam_id].get_jpeg(quality=quality)
            frames_sent += 1
            await resp.write(
                (
                    f"--{boundary}\r\n"
                    "Content-Type: image/jpeg\r\n"
                    f"Content-Length: {len(jpg)}\r\n\r\n"
                ).encode("ascii")
            )
            await resp.write(jpg)
            await resp.write(b"\r\n")

            if frames_sent == 1 or frames_sent % 30 == 0:
                get_logger().info(
                    "http_mjpeg_frame",
                    cam_id=cam_id,
                    frames_sent=frames_sent,
                    jpeg_bytes=len(jpg),
                )

            await asyncio.sleep(frame_interval)
    except asyncio.CancelledError:
        raise
    except (ConnectionResetError, BrokenPipeError):
        # Normal when clients disconnect; don't spam stack traces.
        pass
    finally:
        try:
            await resp.write_eof()
        except Exception:
            pass

    return resp


async def snapshot(request: web.Request) -> web.Response:
    cam_id = int(request.match_info["cam_id"])
    state: AppState = request.app["state"]

    get_logger().info(
        "http_snapshot",
        cam_id=cam_id,
        remote=getattr(request, "remote", None),
        host=request.host,
        path=request.path,
        ua=request.headers.get("User-Agent"),
    )

    if cam_id not in state.cameras:
        raise web.HTTPNotFound(text="Unknown camera")

    q = request.rel_url.query
    quality = _parse_int(q.get("quality"))
    if quality is None:
        quality = 80
    quality = int(_clamp(float(quality), 1.0, 95.0))

    jpg = await state.cameras[cam_id].get_jpeg(quality=quality)
    return web.Response(body=jpg, content_type="image/jpeg")


async def index(request: web.Request) -> web.Response:
    state: AppState = request.app["state"]
    browser_host, _ = _split_host_port(request.host)
    advertised = getattr(state, "advertise_host", "IPADDR") or "IPADDR"

    # What the client actually used to reach this server (best for copy/paste).
    # request.host already includes the port when present.
    http_base_connected = f"{request.scheme}://{request.host}"

    # Also compute an advertised HTTP base for environments where you want to
    # publish a specific IP/hostname.
    http_base_advertised = (
        f"{request.scheme}://{advertised}:{state.http_port}"
        if advertised and advertised != "IPADDR"
        else None
    )

    # For RTSP, allow explicit override (clients may differ from HTTP browser).
    rtsp_host = advertised if advertised and advertised != "IPADDR" else (browser_host or "IPADDR")

    rows = []
    for cam_id in sorted(state.cameras.keys()):
        snap_rel = f"/cam/{cam_id}/snapshot.jpg"
        mjpeg_rel = f"/cam/{cam_id}/mjpeg"
        view_rel = f"/cam/{cam_id}"
        have_h264 = bool(state.rtsp_streamers) and (f"{cam_id}:h264" in state.rtsp_streamers)
        have_h265 = bool(state.rtsp_streamers) and (f"{cam_id}:h265" in state.rtsp_streamers)

        rtsp_h264_td = (
            f"<td><code>rtsp://{rtsp_host}:{state.rtsp_port}/cam{cam_id}_h264</code></td>"
            if have_h264
            else "<td><em>disabled</em></td>"
        )
        rtsp_h265_td = (
            f"<td><code>rtsp://{rtsp_host}:{state.rtsp_port}/cam{cam_id}_h265</code></td>"
            if have_h265
            else "<td><em>disabled</em></td>"
        )

        rows.append(
            f"<tr>"
            f"<td>{cam_id}</td>"
            f"<td><a href='{view_rel}'>view</a></td>"
            f"<td><a href='{snap_rel}'>snapshot.jpg</a></td>"
            f"<td><a href='{mjpeg_rel}'>mjpeg</a></td>"
            f"<td><code>{http_base_connected}{snap_rel}</code>"
            + (f"<br/><small>adv: <code>{http_base_advertised}{snap_rel}</code></small>" if http_base_advertised else "")
            + "</td>"
            f"<td><code>{http_base_connected}{mjpeg_rel}</code>"
            + (f"<br/><small>adv: <code>{http_base_advertised}{mjpeg_rel}</code></small>" if http_base_advertised else "")
            + "</td>"
            + rtsp_h264_td
            + rtsp_h265_td
            + "</tr>"
        )

    html = f"""<!doctype html>
<html>
<head>
  <meta charset='utf-8'>
  <meta name='viewport' content='width=device-width, initial-scale=1'>
  <title>mock_nvr</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif; padding: 24px; }}
    table {{ border-collapse: collapse; width: 100%; }}
    th, td {{ border: 1px solid #ddd; padding: 10px; text-align: left; }}
    th {{ background: #f5f5f5; }}
    code {{ background: #f0f0f0; padding: 2px 6px; border-radius: 6px; }}
  </style>
</head>
<body>
  <h1>mock_nvr</h1>
    <p>HTTP: snapshots + MJPEG. RTSP: MediamTX + ffmpeg publishers (H.264/H.265).</p>
        <p><b>Tip:</b> The primary HTTP URLs below use the exact host you connected with (<code>{request.host}</code>).</p>
        <p><b>Tip:</b> RTSP URLs use <code>{rtsp_host}</code> (set <code>--advertise-host</code> if you want a specific IP/hostname printed).</p>
  <table>
    <thead>
            <tr><th>Cam</th><th>View</th><th>Snapshot</th><th>MJPEG</th><th>Snapshot URL</th><th>MJPEG URL</th><th>RTSP H.264</th><th>RTSP H.265</th></tr>
    </thead>
    <tbody>
      {''.join(rows)}
    </tbody>
  </table>
</body>
</html>"""

    return web.Response(text=html, content_type="text/html")


async def cam_view(request: web.Request) -> web.Response:
        cam_id = int(request.match_info["cam_id"])
        state: AppState = request.app["state"]
        if cam_id not in state.cameras:
                raise web.HTTPNotFound(text="Unknown camera")

        mjpeg_rel = f"/cam/{cam_id}/mjpeg"
        snap_rel = f"/cam/{cam_id}/snapshot.jpg"

        q = request.rel_url.query
        fps = q.get("fps") or ""
        interval_ms = q.get("interval_ms") or ""
        quality = q.get("quality") or "80"

        params = []
        if fps:
            params.append(f"fps={fps}")
        if interval_ms:
            params.append(f"interval_ms={interval_ms}")
        if quality:
            params.append(f"quality={quality}")
        qs = ("?" + "&".join(params)) if params else ""

        html = f"""<!doctype html>
<html>
<head>
    <meta charset='utf-8'>
    <meta name='viewport' content='width=device-width, initial-scale=1'>
    <title>CAM {cam_id} - mock_nvr</title>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif; padding: 24px; }}
        img {{ max-width: 100%; height: auto; border: 1px solid #333; }}
        code {{ background: #f0f0f0; padding: 2px 6px; border-radius: 6px; }}
        label {{ display: inline-block; margin-right: 12px; }}
        input {{ width: 110px; }}
    </style>
</head>
<body>
    <h1>CAM {cam_id}</h1>
    <p>MJPEG: <code>{mjpeg_rel}</code> (supports <code>?fps=</code>, <code>?interval_ms=</code>, <code>?quality=</code>)</p>
    <p>Snapshot: <code>{snap_rel}</code> (supports <code>?quality=</code>)</p>
    <form method='get' action=''>
        <label>fps <input name='fps' value='{fps}' placeholder='e.g. 1 or 0.5' /></label>
        <label>interval_ms <input name='interval_ms' value='{interval_ms}' placeholder='e.g. 1000' /></label>
        <label>quality <input name='quality' value='{quality}' /></label>
        <button type='submit'>Apply</button>
    </form>
    <p><a href='/'>Back</a></p>
    <img src='{mjpeg_rel}{qs}' alt='mjpeg cam {cam_id}' />
</body>
</html>"""

        return web.Response(text=html, content_type="text/html")


def build_app(state: AppState) -> web.Application:
    app = web.Application()
    app["state"] = state

    app.router.add_get("/healthz", healthz)
    app.router.add_get("/stats", stats)
    app.router.add_get("/", index)
    app.router.add_get("/cam/{cam_id:\\d+}", cam_view)
    app.router.add_get("/cam/{cam_id:\\d+}/snapshot.jpg", snapshot)
    app.router.add_get("/cam/{cam_id:\\d+}/mjpeg", mjpeg_stream)

    return app
