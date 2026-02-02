from __future__ import annotations

import asyncio

from aiohttp import web

from .models import AppState


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

    if cam_id not in state.cameras:
        raise web.HTTPNotFound(text="Unknown camera")

    boundary = "frame"
    resp = web.StreamResponse(
        status=200,
        reason="OK",
        headers={
            "Content-Type": f"multipart/x-mixed-replace; boundary={boundary}",
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )
    await resp.prepare(request)

    frame_interval = 1.0 / max(1, state.cameras[cam_id].cfg.fps)

    try:
        while True:
            jpg = await state.cameras[cam_id].get_jpeg()
            await resp.write(
                (
                    f"--{boundary}\r\n"
                    "Content-Type: image/jpeg\r\n"
                    f"Content-Length: {len(jpg)}\r\n\r\n"
                ).encode("ascii")
            )
            await resp.write(jpg)
            await resp.write(b"\r\n")
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

    if cam_id not in state.cameras:
        raise web.HTTPNotFound(text="Unknown camera")

    jpg = await state.cameras[cam_id].get_jpeg()
    return web.Response(body=jpg, content_type="image/jpeg")


async def index(request: web.Request) -> web.Response:
    state: AppState = request.app["state"]
    browser_host, _ = _split_host_port(request.host)
    advertised = getattr(state, "advertise_host", "IPADDR") or "IPADDR"

    # For display: if advertise_host is explicitly set, use it for both HTTP and RTSP
    # URLs (common when the host has multiple NICs and you want a specific IP).
    display_host = advertised if advertised and advertised != "IPADDR" else (browser_host or "IPADDR")
    http_base = f"{request.scheme}://{display_host}:{state.http_port}"
    rtsp_host = display_host

    rows = []
    for cam_id in sorted(state.cameras.keys()):
        snap_rel = f"/cam/{cam_id}/snapshot.jpg"
        mjpeg_rel = f"/cam/{cam_id}/mjpeg"
        rows.append(
            f"<tr>"
            f"<td>{cam_id}</td>"
            f"<td><a href='{snap_rel}'>snapshot.jpg</a></td>"
            f"<td><a href='{mjpeg_rel}'>mjpeg</a></td>"
            f"<td><code>{http_base}{snap_rel}</code></td>"
            f"<td><code>{http_base}{mjpeg_rel}</code></td>"
            f"<td><code>rtsp://{rtsp_host}:{state.rtsp_port}/cam{cam_id}_h264</code></td>"
            f"<td><code>rtsp://{rtsp_host}:{state.rtsp_port}/cam{cam_id}_h265</code></td>"
            f"</tr>"
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
        <p><b>Tip:</b> HTTP URLs below use the host you connected with (<code>{request.host}</code>).</p>
        <p><b>Tip:</b> RTSP URLs use <code>{rtsp_host}</code> (set <code>--advertise-host</code> to override).</p>
  <table>
    <thead>
            <tr><th>Cam</th><th>Snapshot</th><th>MJPEG</th><th>Snapshot URL</th><th>MJPEG URL</th><th>RTSP H.264</th><th>RTSP H.265</th></tr>
    </thead>
    <tbody>
      {''.join(rows)}
    </tbody>
  </table>
</body>
</html>"""

    return web.Response(text=html, content_type="text/html")


def build_app(state: AppState) -> web.Application:
    app = web.Application()
    app["state"] = state

    app.router.add_get("/healthz", healthz)
    app.router.add_get("/stats", stats)
    app.router.add_get("/", index)
    app.router.add_get("/cam/{cam_id:\\d+}/snapshot.jpg", snapshot)
    app.router.add_get("/cam/{cam_id:\\d+}/mjpeg", mjpeg_stream)

    return app
