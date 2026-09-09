#!/usr/bin/env python3
"""本地人像抠图服务：POST /v1/masks → 灰度 PNG Mask，默认 BiRefNet Portrait。"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import os
from urllib.parse import parse_qs, urlparse

from PIL import Image
from rembg import new_session, remove


HOST = os.getenv("CUTOUT_HOST", "127.0.0.1")
PORT = int(os.getenv("CUTOUT_PORT", "7100"))
DEFAULT_MODEL = os.getenv("CUTOUT_MODEL", "birefnet-portrait")
_sessions = {}


def session_for(model):
    if model not in _sessions:
        _sessions[model] = new_session(model)
    return _sessions[model]


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self._send(200, b'{"ok":true}', "application/json")
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != "/v1/masks":
            self._send(404, b"not found", "text/plain")
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if size <= 0 or size > 40 * 1024 * 1024:
                raise ValueError("image size must be 1 byte to 40MB")
            source = self.rfile.read(size)
            model = parse_qs(parsed.query).get("model", [DEFAULT_MODEL])[0]
            rgba = remove(source, session=session_for(model), alpha_matting=False)
            with Image.open(BytesIO(rgba)) as image:
                mask = image.getchannel("A")
                output = BytesIO()
                mask.save(output, format="PNG")
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("X-Cutout-Model", model)
            self.send_header("Content-Length", str(output.tell()))
            self.end_headers()
            self.wfile.write(output.getvalue())
        except Exception as error:  # HTTP 边界必须转为明确错误
            self._send(500, str(error).encode("utf-8"), "text/plain; charset=utf-8")

    def _send(self, status, body, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        print("cutout:", fmt % args)


if __name__ == "__main__":
    print(f"cutout service: http://{HOST}:{PORT} model={DEFAULT_MODEL}")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
