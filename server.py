"""
Resilient Local HTTP Server & Local Control API for HotelCast (`server.py`).

Provides:
- Multi-threaded `ThreadingHTTPServer` bound to `0.0.0.0` (default port 8000, auto-incrementing if busy)
- Dynamic serving directory switching (`set_hls_dir`) so per-movie `.hls/` cache folders
  (`/path/to/MovieName.mkv.hls/`) and `/tmp/hotelcast_hls/` can be served seamlessly
- Local JSON Control API:
  - `OPTIONS /api/*`: CORS preflight (`Access-Control-Allow-Origin: *`, `Access-Control-Allow-Methods: POST, GET, OPTIONS`, `Access-Control-Allow-Headers: Content-Type`)
  - `POST /api/cast`: Accepts `{"mode": "url" | "mirror", "url": "<page_or_video_url>", "title": "<tab_title>"}`,
    switches/starts the stream, pushes `/play` to the TV, and returns `200 OK` with
    `{"status": "playing", "target": "<tv_ip>:<tv_port>"}`.
  - `POST /api/stop`: Stops active streaming session and returns `{"status": "stopped"}`.
  - `GET /api/status`: Returns current streaming status JSON.
- Full HTTP Range (`206 Partial Content`) support for `.ts`, `.mp4`, and `.m3u8` files
- Proper HLS MIME types (`application/vnd.apple.mpegurl` for `.m3u8`, `video/MP2T` for `.ts`)
- Silent suppression of `BrokenPipeError` and `ConnectionResetError` disconnects
- Callback hook for logging live `GET /seg_xxxxx.ts` requests from the TV
"""

from __future__ import annotations

import http.server
import json
import os
import posixpath
import re
import socket
import threading
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import unquote


DEFAULT_HTTP_PORT = 8000
DEFAULT_HLS_DIR = "/tmp/hotelcast_hls"
CHUNK_SIZE = 64 * 1024

MIME_OVERRIDES = {
    ".m3u8": "application/vnd.apple.mpegurl",
    ".ts": "video/MP2T",
    ".mp4": "video/mp4",
    ".m4s": "video/iso.segment",
    ".aac": "audio/aac",
}


def parse_byte_range(range_header: str, file_size: int) -> Optional[Tuple[int, int]]:
    """
    Parse an HTTP `Range: bytes=<start>-<end>` header against `file_size`.
    Returns `(start, end)` inclusive if valid, or `None` if unsatisfiable/invalid.
    """
    if file_size <= 0 or not range_header:
        return None

    m = re.match(r"^\s*bytes\s*=\s*(\d*)\s*-\s*(\d*)", range_header.strip(), re.IGNORECASE)
    if not m:
        return None

    start_str, end_str = m.groups()
    if not start_str and not end_str:
        return None

    try:
        if not start_str:
            suffix_len = int(end_str)
            if suffix_len <= 0:
                return None
            start = max(0, file_size - suffix_len)
            end = file_size - 1
        else:
            start = int(start_str)
            end = int(end_str) if end_str else (file_size - 1)
    except ValueError:
        return None

    if start < 0 or start >= file_size:
        return None

    end = min(end, file_size - 1)
    if start > end:
        return None

    return (start, end)


class QuietThreadingHTTPServer(http.server.ThreadingHTTPServer):
    """
    ThreadingHTTPServer that suppresses `BrokenPipeError` and `ConnectionResetError`
    tracebacks and carries server-level configuration and control callbacks.
    """

    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        server_address: Tuple[str, int],
        RequestHandlerClass: type,
        hls_dir: str = DEFAULT_HLS_DIR,
        on_request: Optional[Callable[[str, str, int, Optional[str]], None]] = None,
        on_log: Optional[Callable[[str], None]] = None,
        on_api_cast: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
        on_api_stop: Optional[Callable[[], Dict[str, Any]]] = None,
        on_api_status: Optional[Callable[[], Dict[str, Any]]] = None,
    ) -> None:
        self.hls_dir = os.path.abspath(hls_dir)
        self.on_request = on_request
        self.on_log = on_log
        self.on_api_cast = on_api_cast
        self.on_api_stop = on_api_stop
        self.on_api_status = on_api_status
        super().__init__(server_address, RequestHandlerClass)

    def handle_error(self, request: object, client_address: Tuple[str, int]) -> None:
        """Silently ignore early client disconnects (BrokenPipeError / ConnectionResetError)."""
        import sys
        _, exc, _ = sys.exc_info()
        if isinstance(exc, (BrokenPipeError, ConnectionResetError, TimeoutError, socket.error)):
            return
        super().handle_error(request, client_address)


class HLSRequestHandler(http.server.SimpleHTTPRequestHandler):
    """
    HTTP Request Handler serving the active HLS directory with HTTP Range (`206 Partial Content`)
    support, `/api/cast` JSON control endpoints with CORS headers, and disconnect suppression.
    """

    protocol_version = "HTTP/1.1"

    def _send_cors_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, HEAD, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Range")

    def _send_json(self, status_code: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self._send_cors_headers()
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

    def translate_path(self, path: str) -> str:
        """Map URL path securely inside `server.hls_dir`."""
        hls_root = getattr(self.server, "hls_dir", DEFAULT_HLS_DIR)
        clean_path = path.split("?", 1)[0].split("#", 1)[0]
        clean_path = posixpath.normpath(unquote(clean_path))
        words = [w for w in clean_path.split("/") if w and w not in (".", "..")]
        resolved = hls_root
        for word in words:
            resolved = os.path.join(resolved, word)
        return resolved

    def guess_type(self, path: str) -> str:
        _, ext = os.path.splitext(path)
        ext_lower = ext.lower()
        if ext_lower in MIME_OVERRIDES:
            return MIME_OVERRIDES[ext_lower]
        return super().guess_type(path)

    def log_message(self, format: str, *args: object) -> None:
        """Suppress default stderr logging; delegate to callback instead."""
        pass

    def handle(self) -> None:
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError, TimeoutError, socket.error):
            pass

    def handle_one_request(self) -> None:
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError, TimeoutError, socket.error):
            self.close_connection = True

    def do_OPTIONS(self) -> None:
        """Handle CORS preflight requests from Chrome extension."""
        try:
            self.send_response(204, "No Content")
            self._send_cors_headers()
            self.send_header("Content-Length", "0")
            self.end_headers()
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, TimeoutError, socket.error):
            self.close_connection = True

    def do_POST(self) -> None:
        """Handle `POST /api/cast` and `POST /api/stop` control requests."""
        try:
            clean_path = self.path.split("?", 1)[0].rstrip("/")
            content_len = int(self.headers.get("Content-Length", "0") or "0")
            raw_body = self.rfile.read(content_len) if content_len > 0 else b"{}"

            if clean_path == "/api/cast":
                try:
                    data = json.loads(raw_body.decode("utf-8", errors="replace") or "{}")
                except json.JSONDecodeError as exc:
                    self._send_json(400, {"status": "error", "error": f"Invalid JSON: {exc}"})
                    return

                mode = str(data.get("mode", "")).strip().lower()
                if mode not in ("url", "mirror", "file"):
                    self._send_json(
                        400,
                        {"status": "error", "error": "Field 'mode' must be 'url' or 'mirror'."},
                    )
                    return

                on_api_cast = getattr(self.server, "on_api_cast", None)
                if not callable(on_api_cast):
                    self._send_json(
                        503,
                        {"status": "error", "error": "Cast controller callback is not registered."},
                    )
                    return

                try:
                    result = on_api_cast(data)
                    self._send_json(200, result)
                except Exception as exc:
                    self._send_json(500, {"status": "error", "error": str(exc)})
                return

            if clean_path == "/api/stop":
                on_api_stop = getattr(self.server, "on_api_stop", None)
                if callable(on_api_stop):
                    try:
                        res = on_api_stop()
                        self._send_json(200, res)
                    except Exception as exc:
                        self._send_json(500, {"status": "error", "error": str(exc)})
                else:
                    self._send_json(200, {"status": "stopped"})
                return

            self._send_json(404, {"status": "error", "error": f"Unknown endpoint: {clean_path}"})
        except (BrokenPipeError, ConnectionResetError, TimeoutError, socket.error):
            self.close_connection = True

    def do_HEAD(self) -> None:
        try:
            self._serve_file(send_body=False)
        except (BrokenPipeError, ConnectionResetError, TimeoutError, socket.error):
            self.close_connection = True

    def do_GET(self) -> None:
        try:
            clean_path = self.path.split("?", 1)[0].rstrip("/")
            if clean_path == "/api/status":
                on_api_status = getattr(self.server, "on_api_status", None)
                if callable(on_api_status):
                    self._send_json(200, on_api_status())
                else:
                    self._send_json(200, {"status": "idle"})
                return
            self._serve_file(send_body=True)
        except (BrokenPipeError, ConnectionResetError, TimeoutError, socket.error):
            self.close_connection = True

    def _serve_file(self, send_body: bool = True) -> None:
        fs_path = self.translate_path(self.path)
        if not os.path.isfile(fs_path):
            self.send_error(404, "File not found")
            self._notify_request(404, None)
            return

        try:
            file_size = os.path.getsize(fs_path)
        except OSError:
            self.send_error(404, "File not found")
            self._notify_request(404, None)
            return

        content_type = self.guess_type(fs_path)
        range_header = self.headers.get("Range")

        if range_header:
            byte_range = parse_byte_range(range_header, file_size)
            if byte_range is None:
                self.send_response(416, "Requested Range Not Satisfiable")
                self.send_header("Content-Range", f"bytes */{file_size}")
                self.send_header("Content-Length", "0")
                self.send_header("Accept-Ranges", "bytes")
                self._send_cors_headers()
                self.end_headers()
                self._notify_request(416, range_header)
                return

            start, end = byte_range
            length = end - start + 1
            self.send_response(206, "Partial Content")
            self.send_header("Content-Type", content_type)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self._send_cors_headers()
            self.end_headers()
            self._notify_request(206, f"bytes={start}-{end}")

            if send_body:
                self._stream_file_range(fs_path, start, length)
            return

        self.send_response(200, "OK")
        self.send_header("Content-Type", content_type)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(file_size))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self._send_cors_headers()
        self.end_headers()
        self._notify_request(200, None)

        if send_body:
            self._stream_file_range(fs_path, 0, file_size)

    def _stream_file_range(self, fs_path: str, start: int, length: int) -> None:
        remaining = length
        try:
            with open(fs_path, "rb") as f:
                if start > 0:
                    f.seek(start)
                while remaining > 0:
                    chunk = f.read(min(CHUNK_SIZE, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, TimeoutError, socket.error):
            self.close_connection = True

    def _notify_request(self, status_code: int, range_info: Optional[str]) -> None:
        client_ip = self.client_address[0] if self.client_address else "unknown"
        req_path = self.path.split("?", 1)[0]

        on_req = getattr(self.server, "on_request", None)
        if callable(on_req):
            try:
                on_req(client_ip, req_path, status_code, range_info)
            except Exception:
                pass

        on_log = getattr(self.server, "on_log", None)
        if callable(on_log):
            range_suffix = f" ({range_info})" if range_info else ""
            try:
                on_log(f"[HTTP {status_code}] {client_ip} -> GET {req_path}{range_suffix}")
            except Exception:
                pass


class HLSServer:
    """
    Lifecycle wrapper around `QuietThreadingHTTPServer` that binds to `0.0.0.0`,
    auto-increments the port if busy, and dynamically switches its served `hls_dir`
    between per-movie `.hls/` cache directories and `/tmp/hotelcast_hls/`.
    """

    def __init__(
        self,
        hls_dir: str = DEFAULT_HLS_DIR,
        host: str = "0.0.0.0",
        port: int = DEFAULT_HTTP_PORT,
        on_request: Optional[Callable[[str, str, int, Optional[str]], None]] = None,
        on_log: Optional[Callable[[str], None]] = None,
        on_api_cast: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
        on_api_stop: Optional[Callable[[], Dict[str, Any]]] = None,
        on_api_status: Optional[Callable[[], Dict[str, Any]]] = None,
    ) -> None:
        self.hls_dir = os.path.abspath(hls_dir)
        self.host = host
        self.requested_port = port
        self.port = port
        self.on_request = on_request
        self.on_log = on_log
        self.on_api_cast = on_api_cast
        self.on_api_stop = on_api_stop
        self.on_api_status = on_api_status
        self._httpd: Optional[QuietThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def set_hls_dir(self, new_dir: str) -> None:
        """Switch the active directory served by the HTTP server without restarting it."""
        abs_dir = os.path.abspath(new_dir)
        os.makedirs(abs_dir, exist_ok=True)
        self.hls_dir = abs_dir
        if self._httpd is not None:
            self._httpd.hls_dir = abs_dir

    def start(self, max_port_tries: int = 50) -> int:
        """
        Bind and start the multi-threaded HTTP server in a daemon thread.
        If already running, returns the current bound port.
        """
        if self.is_running():
            return self.port

        self.stop()
        os.makedirs(self.hls_dir, exist_ok=True)

        candidate_port = self.requested_port
        last_err: Optional[Exception] = None
        for _ in range(max_port_tries):
            try:
                httpd = QuietThreadingHTTPServer(
                    (self.host, candidate_port),
                    HLSRequestHandler,
                    hls_dir=self.hls_dir,
                    on_request=self.on_request,
                    on_log=self.on_log,
                    on_api_cast=self.on_api_cast,
                    on_api_stop=self.on_api_stop,
                    on_api_status=self.on_api_status,
                )
                self._httpd = httpd
                self.port = int(httpd.server_address[1])
                break
            except OSError as exc:
                last_err = exc
                candidate_port += 1
        else:
            raise RuntimeError(
                f"Could not bind HTTP server starting at port {self.requested_port}: {last_err}"
            )

        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            name="hotelcast-http-server",
            daemon=True,
        )
        self._thread.start()
        if self.on_log:
            self.on_log(
                f"Local HLS & Control HTTP server listening on {self.host}:{self.port} (serving {self.hls_dir})"
            )
        return self.port

    def stream_url(self, local_ip: str, playlist_name: str = "stream.m3u8") -> str:
        """Return `http://<local_ip>:<port>/<playlist_name>`."""
        return f"http://{local_ip}:{self.port}/{playlist_name.lstrip('/')}"

    def is_running(self) -> bool:
        return self._httpd is not None and self._thread is not None and self._thread.is_alive()

    def stop(self) -> None:
        """Shut down the HTTP server and close its listening socket."""
        httpd = self._httpd
        self._httpd = None
        if httpd is not None:
            try:
                httpd.shutdown()
            except Exception:
                pass
            try:
                httpd.server_close()
            except Exception:
                pass
        if (
            self._thread is not None
            and self._thread is not threading.current_thread()
        ):
            self._thread.join(timeout=2.0)
            self._thread = None
