"""
Persistent AirPlay / HappyCast Controller for HotelCast (`airplay.py`).

Provides:
- HLS readiness check (`stream.m3u8` + at least 2 `.ts` segments on disk, or completed `#EXT-X-ENDLIST`)
- Wi-Fi interface-pinned HTTP/1.1 connection (`WiFiPinnedHTTPConnection`) to `<tv_ip>:<tv_port>`
- `POST /play` with `Content-Type: text/parameters`, `User-Agent: MediaControl/1.0`, `Connection: keep-alive`
- Fractional `Start-Position` (`0.0` to `0.99`) calculation from `seek_seconds / total_duration`
- Background keep-alive loop maintaining the primary socket and polling `GET /playback-info`
- Clean `POST /stop` teardown when streaming stops
"""

from __future__ import annotations

import glob
import http.client
import os
import select
import socket
import threading
import time
from typing import Callable, Optional

from network import (
    WiFiPinnedHTTPConnection,
    ensure_vpn_bypass_route,
    find_physical_wifi_interface,
)


DEFAULT_HLS_DIR = "/tmp/hotelcast_hls"
USER_AGENT = "MediaControl/1.0"


def count_ready_segments(hls_dir: str = DEFAULT_HLS_DIR) -> int:
    """Return the count of non-empty `.ts` segment files in `hls_dir`."""
    if not os.path.isdir(hls_dir):
        return 0
    count = 0
    for ts_path in glob.glob(os.path.join(hls_dir, "*.ts")):
        try:
            if os.path.getsize(ts_path) > 0:
                count += 1
        except OSError:
            pass
    return count


def wait_for_hls_ready(
    hls_dir: str = DEFAULT_HLS_DIR,
    min_segments: int = 2,
    timeout: float = 30.0,
    stop_event: Optional[threading.Event] = None,
    on_log: Optional[Callable[[str], None]] = None,
    playlist_name: str = "stream.m3u8",
) -> bool:
    """
    Wait until `<hls_dir>/<playlist_name>` and at least `min_segments` `.ts` segments
    exist on disk (or `#EXT-X-ENDLIST` is written with at least 1 segment).
    """
    playlist_path = os.path.join(hls_dir, playlist_name)
    deadline = time.monotonic() + timeout

    if on_log:
        on_log(
            f"Waiting for {playlist_path} and at least {min_segments} .ts segments before triggering AirPlay..."
        )

    while time.monotonic() < deadline:
        if stop_event is not None and stop_event.is_set():
            return False

        if os.path.isfile(playlist_path):
            try:
                with open(playlist_path, "r", encoding="utf-8", errors="ignore") as f:
                    playlist_text = f.read()
            except OSError:
                playlist_text = ""

            seg_count = count_ready_segments(hls_dir)
            if playlist_text.strip() and (
                seg_count >= min_segments
                or ("#EXT-X-ENDLIST" in playlist_text and seg_count >= 1)
            ):
                if on_log:
                    on_log(f"HLS stream ready ({seg_count} segments on disk in {hls_dir}).")
                return True

        time.sleep(0.15)

    if on_log:
        on_log(f"Timed out after {timeout:.1f}s waiting for HLS segments in {hls_dir}.")
    return False


def compute_start_fraction(seek_seconds: float, total_duration: float) -> float:
    """
    Calculate `start_fraction = seek_seconds / total_duration` clamped to `[0.0, 0.99]`.
    HappyCast / AirPlay receivers expect `Start-Position` in `/play` to be a float
    between `0.0` and `1.0` (fraction of total duration), NOT seconds.
    """
    if seek_seconds <= 0.0 or total_duration <= 0.0:
        return 0.0
    return min(0.99, max(0.0, float(seek_seconds) / float(total_duration)))


def format_play_payload(content_location: str, start_position: float = 0.0) -> str:
    """
    Format the AirPlay v1 / HappyCast `text/parameters` `/play` request body.
    `start_position` is clamped to `[0.0, 0.99]`.
    """
    clamped = min(0.99, max(0.0, float(start_position)))
    pos_str = "0.0" if clamped == 0.0 else f"{clamped:.4f}".rstrip("0").rstrip(".")
    if "." not in pos_str:
        pos_str += ".0"
    return (
        f"Content-Location: {content_location}\n"
        f"Start-Position: {pos_str}\n"
    )


def _enable_tcp_keepalive(sock: Optional[socket.socket]) -> None:
    """Enable OS-level TCP keep-alive probes on the control socket."""
    if sock is None:
        return
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        if hasattr(socket, "TCP_KEEPIDLE"):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 15)
        if hasattr(socket, "TCP_KEEPINTVL"):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 5)
        if hasattr(socket, "TCP_KEEPCNT"):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 4)
    except OSError:
        pass


class AirPlayController:
    """
    Persistent AirPlay v1 / HappyCast / LeLink HTTP/1.1 controller.

    Pins outgoing sockets to the physical Wi-Fi interface via `SO_BINDTODEVICE`
    / source-address binding and maintains the `/play` control socket open in a background
    thread for the entire playback session.
    """

    def __init__(
        self,
        tv_ip: str,
        tv_port: int = 52266,
        poll_interval: float = 3.0,
        on_log: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.tv_ip = tv_ip
        self.tv_port = int(tv_port)
        self.poll_interval = poll_interval
        self.on_log = on_log

        self._conn: Optional[http.client.HTTPConnection] = None
        self._wifi_iface: Optional[str] = None
        self._wifi_ip: Optional[str] = None
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._keepalive_thread: Optional[threading.Thread] = None
        self.last_play_status: Optional[int] = None
        self.last_playback_info: str = ""

    def _log(self, msg: str) -> None:
        if self.on_log:
            self.on_log(msg)

    def _open_pinned_connection(self, timeout: float = 10.0) -> WiFiPinnedHTTPConnection:
        is_loopback = self.tv_ip.startswith("127.") or self.tv_ip.lower() == "localhost"
        if not is_loopback:
            wifi = find_physical_wifi_interface()
            if wifi:
                self._wifi_iface = wifi.ifname
                self._wifi_ip = wifi.ipv4
                ensure_vpn_bypass_route(self.tv_ip, wifi_info=wifi)
        return WiFiPinnedHTTPConnection(
            self.tv_ip,
            port=self.tv_port,
            timeout=timeout,
            wifi_iface=self._wifi_iface,
            wifi_ip=self._wifi_ip,
        )

    @property
    def is_connected(self) -> bool:
        """Return True if the persistent primary control socket is currently open."""
        with self._lock:
            if self._conn is None or self._conn.sock is None:
                return False
            try:
                return self._conn.sock.fileno() != -1
            except OSError:
                return False

    def play(
        self,
        content_location: str,
        start_position: float = 0.0,
        timeout: float = 10.0,
    ) -> int:
        """
        Open a persistent Wi-Fi-pinned HTTP/1.1 connection to `<tv_ip>:<tv_port>`,
        send `POST /play`, and start the background keep-alive thread.
        """
        self.stop(send_stop_request=False)
        self._stop_event.clear()

        payload = format_play_payload(content_location, start_position)
        body_bytes = payload.encode("utf-8")
        headers = {
            "Content-Type": "text/parameters",
            "User-Agent": USER_AGENT,
            "Connection": "keep-alive",
            "Content-Length": str(len(body_bytes)),
        }

        conn = self._open_pinned_connection(timeout=timeout)
        iface_note = f" via {self._wifi_iface} ({self._wifi_ip})" if self._wifi_iface else ""
        self._log(
            f"Connecting to AirPlay/HappyCast receiver at {self.tv_ip}:{self.tv_port}{iface_note}..."
        )
        conn.connect()
        _enable_tcp_keepalive(conn.sock)

        self._log(
            f"Sending POST /play -> Content-Location: {content_location} (Start-Position: {start_position:.4f})"
        )
        conn.request("POST", "/play", body=body_bytes, headers=headers)
        resp = conn.getresponse()
        _ = resp.read()
        status = resp.status
        self.last_play_status = status
        self._log(f"AirPlay /play response: HTTP {status} {resp.reason}")

        with self._lock:
            self._conn = conn

        self._keepalive_thread = threading.Thread(
            target=self._keepalive_loop,
            name="hotelcast-airplay-keepalive",
            daemon=True,
        )
        self._keepalive_thread.start()
        return status

    def _keepalive_loop(self) -> None:
        """
        Background loop that keeps the primary `/play` HTTP/1.1 connection open
        and periodically polls `GET /playback-info`.
        """
        next_poll = time.monotonic() + self.poll_interval
        while not self._stop_event.is_set():
            with self._lock:
                sock = self._conn.sock if self._conn else None

            if sock is not None:
                try:
                    readable, _, _ = select.select([sock], [], [], 0.25)
                    if readable:
                        data = sock.recv(1024, socket.MSG_PEEK)
                        if data == b"":
                            self._log("AirPlay receiver closed the control connection.")
                            break
                        _ = sock.recv(len(data))
                except (OSError, ValueError):
                    break
            else:
                break

            now = time.monotonic()
            if self.poll_interval > 0 and now >= next_poll:
                next_poll = now + self.poll_interval
                self.poll_playback_info()

    def poll_playback_info(self, timeout: float = 2.5) -> Optional[str]:
        """Poll `GET /playback-info` on a secondary Wi-Fi-pinned connection."""
        if self._stop_event.is_set():
            return None

        sec_conn: Optional[http.client.HTTPConnection] = None
        try:
            sec_conn = self._open_pinned_connection(timeout=timeout)
            sec_conn.request(
                "GET",
                "/playback-info",
                headers={
                    "User-Agent": USER_AGENT,
                    "Connection": "keep-alive",
                },
            )
            resp = sec_conn.getresponse()
            body = resp.read().decode("utf-8", errors="ignore")
            if resp.status == 200 and body:
                self.last_playback_info = body
                return body
        except Exception:
            pass
        finally:
            if sec_conn is not None:
                try:
                    sec_conn.close()
                except Exception:
                    pass
        return None

    def send_stop(self, timeout: float = 3.0) -> bool:
        """Send `POST /stop` to the AirPlay receiver."""
        headers = {
            "User-Agent": USER_AGENT,
            "Content-Length": "0",
            "Connection": "close",
        }
        stop_conn: Optional[http.client.HTTPConnection] = None
        try:
            stop_conn = self._open_pinned_connection(timeout=timeout)
            stop_conn.request("POST", "/stop", body=b"", headers=headers)
            resp = stop_conn.getresponse()
            _ = resp.read()
            self._log(f"Sent POST /stop to TV (HTTP {resp.status}).")
            return resp.status in (200, 202, 204)
        except Exception:
            with self._lock:
                conn = self._conn
            if conn is not None:
                try:
                    conn.request("POST", "/stop", body=b"", headers=headers)
                    resp = conn.getresponse()
                    _ = resp.read()
                    return True
                except Exception:
                    pass
        finally:
            if stop_conn is not None:
                try:
                    stop_conn.close()
                except Exception:
                    pass
        return False

    def stop(self, send_stop_request: bool = True) -> None:
        """Stop the keep-alive loop, send `POST /stop` to the TV, and close the control socket."""
        self._stop_event.set()

        if send_stop_request and self.is_connected:
            self.send_stop()

        with self._lock:
            conn = self._conn
            self._conn = None

        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

        if (
            self._keepalive_thread is not None
            and self._keepalive_thread is not threading.current_thread()
        ):
            self._keepalive_thread.join(timeout=2.0)
            self._keepalive_thread = None
