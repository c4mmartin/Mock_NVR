from __future__ import annotations

import asyncio

from aiohttp import web

from .models import AppState


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
    browser_host = request.host.split(":")[0]
    advertised = getattr(state, "advertise_host", "IPADDR") or "IPADDR"
    http_port = getattr(state, "http_port", None)
    if not http_port:
        http_port = request.url.port or 80

    rows = []
    for cam_id in sorted(state.cameras.keys()):
        snap_rel = f"/cam/{cam_id}/snapshot.jpg"
        mjpeg_rel = f"/cam/{cam_id}/mjpeg"
        rows.append(
            f"<tr>"
            f"<td>{cam_id}</td>"
            f"<td><a href='{snap_rel}'>snapshot.jpg</a></td>"
            f"<td><a href='{mjpeg_rel}'>mjpeg</a></td>"
            f"<td><code>http://{advertised}:{http_port}{snap_rel}</code></td>"
            f"<td><code>http://{advertised}:{http_port}{mjpeg_rel}</code></td>"
            f"<td><code>rtsp://{advertised}:{state.rtsp_port}/cam{cam_id}_h264</code></td>"
            f"<td><code>rtsp://{advertised}:{state.rtsp_port}/cam{cam_id}_h265</code></td>"
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
    <p><b>Tip:</b> URLs below use <code>{advertised}</code>. Replace it with your LAN IP. Browser host is <code>{browser_host}</code>.</p>
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
