"""Paste-image upload handler. Extracted from annotate/server.py:613-653.

The endpoint is unchanged: POST <session>/api/upload with a raw image body
(Content-Type one of png/jpeg/gif/webp). Saves under dirs["state_dir"]/images/.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from http.server import BaseHTTPRequestHandler


UPLOAD_EXT = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
}
UPLOAD_MAX_BYTES = 10 * 1024 * 1024


def handle(handler: BaseHTTPRequestHandler, dirs: dict) -> None:
    ctype = (handler.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
    ext = UPLOAD_EXT.get(ctype)
    if ext is None:
        _send_text(handler, 415, "unsupported media type")
        return
    length_hdr = handler.headers.get("Content-Length")
    if not length_hdr:
        _send_text(handler, 411, "length required")
        return
    try:
        length = int(length_hdr)
    except ValueError:
        _send_text(handler, 400, "invalid content-length")
        return
    if length <= 0 or length > UPLOAD_MAX_BYTES:
        _send_text(handler, 413, "payload too large")
        return
    body = handler.rfile.read(length)
    images_dir = Path(dirs["state_dir"]) / "images"
    images_dir.mkdir(parents=True, exist_ok=True)
    path = images_dir / f"{uuid.uuid4().hex}.{ext}"
    path.write_bytes(body)
    body_json = json.dumps({"path": str(path), "size": len(body)})
    data = body_json.encode("utf-8")
    handler.send_response(200)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def images_ok(images, state_dir) -> bool:
    """Every submitted image must point at a file under <state_dir>/images/.

    Paths are minted server-side by handle() above (uuid filename under that
    dir) and echoed back by the client. Validating containment stops a hostile
    or buggy client from naming an arbitrary path (e.g. /etc/passwd) that
    Claude would then be told to read as a 'pasted image'. Empty list is fine.
    """
    if not isinstance(images, list):
        return False
    images_root = (Path(state_dir) / "images").resolve()
    for img in images:
        if not isinstance(img, dict):
            return False
        p = img.get("path")
        if not isinstance(p, str) or not p:
            return False
        try:
            resolved = Path(p).resolve()
        except (OSError, ValueError):
            return False
        if not resolved.is_relative_to(images_root) or not resolved.is_file():
            return False
    return True


def _send_text(handler: BaseHTTPRequestHandler, status: int, body: str) -> None:
    data = body.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "text/plain; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)
