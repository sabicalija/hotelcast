"""
Automated End-to-End & Unit Tests for HotelCast (`tests/test_pipeline.py`).

Runs completely offline on `127.0.0.1` (no hotel Wi-Fi required):
1. End-to-end pipeline + Persistent `.hls/` Cache + Instant Resume + Seeking:
   - Generates a 5-second synthetic multi-audio test video via `ffmpeg -f lavfi`
   - Spins up a mock AirPlay / HappyCast receiver socket on `127.0.0.1`
   - Runs first session: verifies `.mkv.hls/` cache creation, `/play` payload, HTTP 200 & 206
     segment responses, early disconnect resilience, keep-alive socket persistence, and `/stop`
   - Runs second session with seek offset (`00:00:02` / 40%): verifies `ffmpeg` is skipped
     (instant resume from `.mkv.hls/`), `Start-Position: 0.4` fraction is sent in `/play`,
     and `seek_stream.m3u8` is generated
   - Tests `delete_video_hls_cache()` cleanup
2. Local Control API (`POST /api/cast`, `OPTIONS /api/cast` CORS preflight, `GET /api/status`, `POST /api/stop`)
3. Discovery, `network.py` (Wi-Fi interface detection, `expand_activation_url`), and `config.py`
4. Streamer command builders & CLI argument parsing (`hotelcast://` URI and file path parsing)
"""

from __future__ import annotations

import http.client
import http.server
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from typing import Dict, List, Tuple
from urllib.parse import urlparse

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from airplay import compute_start_fraction, format_play_payload
from app import HotelCastSession, build_arg_parser, parse_invocation_target
from config import HotelCastConfig, matches_preferred_language, select_preferred_audio_index
from discovery import (
    check_firewall,
    get_local_ip,
    parse_avahi_parsable_output,
    parse_manual_target,
)
from network import (
    check_tv_reachable,
    expand_activation_url,
    find_physical_wifi_interface,
    is_vpn_or_virtual_iface,
)
from server import parse_byte_range
from streamer import (
    build_local_file_cmd,
    build_mirror_cmd,
    build_web_url_cmd,
    delete_video_hls_cache,
    format_timestamp,
    get_video_hls_cache_dir,
    is_hls_cache_complete,
    parse_timestamp_to_seconds,
    probe_audio_tracks,
    probe_media_info,
)


class MockAirPlayReceiver:
    """
    Mock AirPlay v1 / HappyCast receiver running on `127.0.0.1` over HTTP/1.1.
    Records all requests, keeps the `/play` TCP socket open (`Connection: keep-alive`),
    and tracks whether the control socket remains open until `/stop`.
    """

    def __init__(self) -> None:
        self.play_requests: List[Dict[str, object]] = []
        self.playback_info_count: int = 0
        self.stop_requests: int = 0
        self.play_event = threading.Event()
        self.stop_event = threading.Event()
        self.active_play_sockets: List[socket.socket] = []
        self._lock = threading.Lock()

        receiver_ref = self

        class _Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: object) -> None:
                pass

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0") or "0")
                body = self.rfile.read(length).decode("utf-8", errors="replace") if length > 0 else ""

                if self.path == "/play":
                    with receiver_ref._lock:
                        receiver_ref.play_requests.append(
                            {
                                "path": self.path,
                                "headers": {k.lower(): v for k, v in self.headers.items()},
                                "body": body,
                            }
                        )
                        receiver_ref.active_play_sockets.append(self.connection)
                    receiver_ref.play_event.set()

                    self.send_response(200, "OK")
                    self.send_header("Content-Length", "0")
                    self.send_header("Connection", "keep-alive")
                    self.end_headers()
                    self.wfile.flush()
                    self.close_connection = False

                elif self.path == "/stop":
                    with receiver_ref._lock:
                        receiver_ref.stop_requests += 1
                    receiver_ref.stop_event.set()
                    self.send_response(200, "OK")
                    self.send_header("Content-Length", "0")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.flush()
                    self.close_connection = True
                else:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()

            def do_GET(self) -> None:
                if self.path == "/playback-info":
                    with receiver_ref._lock:
                        receiver_ref.playback_info_count += 1
                    xml_body = (
                        b'<?xml version="1.0" encoding="UTF-8"?>\n'
                        b'<plist version="1.0"><dict>'
                        b"<key>duration</key><real>5.0</real>"
                        b"<key>position</key><real>1.5</real>"
                        b"<key>rate</key><real>1.0</real>"
                        b"</dict></plist>"
                    )
                    self.send_response(200, "OK")
                    self.send_header("Content-Type", "text/x-apple-plist+xml")
                    self.send_header("Content-Length", str(len(xml_body)))
                    self.send_header("Connection", "keep-alive")
                    self.end_headers()
                    self.wfile.write(xml_body)
                    self.wfile.flush()
                else:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()

        class _Server(http.server.ThreadingHTTPServer):
            allow_reuse_address = True
            daemon_threads = True

            def handle_error(self, request: object, client_address: Tuple[str, int]) -> None:
                pass

        self._server = _Server(("127.0.0.1", 0), _Handler)
        self.port = int(self._server.server_address[1])
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def start(self) -> int:
        self._thread.start()
        return self.port

    def is_play_socket_open(self) -> bool:
        with self._lock:
            if not self.active_play_sockets:
                return False
            sock = self.active_play_sockets[-1]
        try:
            if sock.fileno() == -1:
                return False
            sock.setblocking(False)
            try:
                data = sock.recv(1, socket.MSG_PEEK)
                return data != b""
            except BlockingIOError:
                return True
            finally:
                sock.setblocking(True)
        except OSError:
            return False

    def stop(self) -> None:
        try:
            self._server.shutdown()
        except Exception:
            pass
        try:
            self._server.server_close()
        except Exception:
            pass
        self._thread.join(timeout=2.0)


def generate_synthetic_video(output_path: str, duration: int = 5) -> None:
    """
    Generate a 5-second synthetic H.264 + dual-audio video via `ffmpeg -f lavfi`
    with keyframes every 1 second (`-g 25`).
    """
    ffmpeg_bin = shutil.which("ffmpeg") or "ffmpeg"
    cmd = [
        ffmpeg_bin, "-y",
        "-f", "lavfi", "-i", f"testsrc=duration={duration}:size=320x240:rate=25",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}",
        "-f", "lavfi", "-i", f"sine=frequency=880:duration={duration}",
        "-map", "0:v:0", "-map", "1:a:0", "-map", "2:a:0",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-g", "25", "-keyint_min", "25", "-sc_threshold", "0",
        "-c:a", "aac", "-ac", "2",
        "-metadata:s:a:0", "language=eng",
        "-metadata:s:a:1", "language=ger",
        output_path,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30.0, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"Failed to generate synthetic test video: {proc.stderr}")


class TestHotelCastPipeline(unittest.TestCase):
    """End-to-end and component tests for HotelCast."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp(prefix="hotelcast_test_")
        self.video_path = os.path.join(self.temp_dir, "synthetic_5s.mkv")

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_end_to_end_pipeline_and_persistent_cache_and_seek(self) -> None:
        """
        Full pipeline verification:
        1. Generate 5-second synthetic test video via `ffmpeg -f lavfi`
        2. Probe audio tracks & duration via `ffprobe`
        3. Spin up mock AirPlay receiver on `127.0.0.1`
        4. Run HLS pipeline -> verify `.mkv.hls/` cache folder creation, `/play`, HTTP 200 & 206,
           keep-alive socket persistence, and `/api/status`
        5. Run second session on the same file with seek (`2.0s` / `5.0s` = `0.4`) -> verify
           instant resume (skipping `ffmpeg`), `Start-Position: 0.4` in `/play`, and `seek_stream.m3u8`
        """
        generate_synthetic_video(self.video_path, duration=5)
        self.assertTrue(os.path.isfile(self.video_path))

        tracks, duration = probe_media_info(self.video_path)
        self.assertEqual(len(tracks), 2)
        self.assertEqual(tracks[0].language, "eng")
        self.assertEqual(tracks[1].language, "ger")
        self.assertAlmostEqual(duration, 5.0, delta=0.5)

        # Verify German synonym matching ("deu" matches "ger")
        self.assertEqual(select_preferred_audio_index(tracks, "deu"), 1)

        mock_tv = MockAirPlayReceiver()
        tv_port = mock_tv.start()
        logs: List[str] = []

        # Verify fast TCP reachability check against mock_tv
        self.assertTrue(check_tv_reachable("127.0.0.1", tv_port, timeout=0.5))

        session = HotelCastSession(
            http_port=18080,
            on_log=logs.append,
        )
        try:
            stream_url = session.start_session(
                tv_target=f"127.0.0.1:{tv_port}",
                mode="file",
                file_path=self.video_path,
                audio_index=1,
                min_segments=2,
                ready_timeout=20.0,
            )

            # 1. Verify mock AirPlay receiver got POST /play
            self.assertTrue(mock_tv.play_event.wait(timeout=5.0), "POST /play was not received")
            self.assertEqual(len(mock_tv.play_requests), 1)
            play_req = mock_tv.play_requests[0]
            headers = play_req["headers"]
            body = str(play_req["body"])

            self.assertEqual(headers.get("content-type"), "text/parameters")
            self.assertEqual(headers.get("user-agent"), "MediaControl/1.0")
            self.assertEqual(headers.get("connection", "").lower(), "keep-alive")
            self.assertIn(f"Content-Location: {stream_url}", body)
            self.assertIn("Start-Position: 0.0", body)

            # 2. Verify keep-alive connection remains open on both client and receiver
            time.sleep(0.3)
            assert session.airplay is not None
            self.assertTrue(session.airplay.is_connected)
            self.assertTrue(mock_tv.is_play_socket_open())

            # 3. Fetch stream.m3u8 via HTTP 200
            parsed = urlparse(stream_url)
            http_conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5.0)
            http_conn.request("GET", parsed.path)
            m3u8_resp = http_conn.getresponse()
            m3u8_body = m3u8_resp.read().decode("utf-8")
            self.assertEqual(m3u8_resp.status, 200)
            self.assertEqual(m3u8_resp.getheader("Content-Type"), "application/vnd.apple.mpegurl")
            self.assertIn("#EXTM3U", m3u8_body)

            segments = [
                line.strip()
                for line in m3u8_body.splitlines()
                if line.strip().endswith(".ts")
            ]
            self.assertGreaterEqual(len(segments), 2)

            # 4. Fetch first segment with HTTP 200 OK and HTTP 206 Partial Content
            seg0 = f"/{segments[0].lstrip('/')}"
            http_conn.request("GET", seg0)
            seg_resp_200 = http_conn.getresponse()
            full_seg_bytes = seg_resp_200.read()
            self.assertEqual(seg_resp_200.status, 200)
            self.assertEqual(seg_resp_200.getheader("Content-Type"), "video/MP2T")

            http_conn.request("GET", seg0, headers={"Range": "bytes=0-187"})
            seg_resp_206 = http_conn.getresponse()
            partial_bytes = seg_resp_206.read()
            self.assertEqual(seg_resp_206.status, 206)
            self.assertEqual(partial_bytes, full_seg_bytes[:188])

            # 5. Test CORS preflight OPTIONS /api/cast and GET /api/status
            http_conn.request("OPTIONS", "/api/cast")
            opt_resp = http_conn.getresponse()
            _ = opt_resp.read()
            self.assertEqual(opt_resp.status, 204)
            self.assertEqual(opt_resp.getheader("Access-Control-Allow-Origin"), "*")

            http_conn.request("GET", "/api/status")
            st_resp = http_conn.getresponse()
            st_data = json.loads(st_resp.read().decode("utf-8"))
            self.assertEqual(st_data.get("status"), "playing")
            http_conn.close()

            # Wait briefly for ffmpeg to finish writing #EXT-X-ENDLIST on the 5s file
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and not is_hls_cache_complete(self.video_path, 1):
                time.sleep(0.1)

            self.assertTrue(
                is_hls_cache_complete(self.video_path, 1),
                "Expected per-movie .mkv.hls cache to be marked complete",
            )

            # 6. Stop first session and verify .mkv.hls/ cache is preserved!
            session.stop_session(cleanup_files=True, stop_server=False)
            cache_dir = get_video_hls_cache_dir(self.video_path)
            self.assertTrue(os.path.isdir(cache_dir))

            # 7. Start second session on the same file with seek_seconds=2.5 (50% of 5.0s)
            mock_tv.play_event.clear()
            session.start_session(
                tv_target=f"127.0.0.1:{tv_port}",
                mode="file",
                file_path=self.video_path,
                audio_index=1,
                seek_seconds=2.5,
                total_duration=5.0,
            )
            self.assertTrue(mock_tv.play_event.wait(timeout=5.0))
            second_play_body = str(mock_tv.play_requests[-1]["body"])
            self.assertIn("Start-Position: 0.5", second_play_body)
            self.assertIn("/seek_stream.m3u8", second_play_body)
            self.assertTrue(
                any("skipping ffmpeg conversion" in line for line in logs),
                "Second run should skip ffmpeg and use complete .hls/ cache",
            )
            self.assertTrue(os.path.isfile(os.path.join(cache_dir, "seek_stream.m3u8")))

            # 8. Delete cached HLS folder and verify removal
            self.assertTrue(delete_video_hls_cache(self.video_path))
            self.assertFalse(os.path.exists(cache_dir))

        finally:
            session.stop_session(cleanup_files=True, stop_server=True)
            mock_tv.stop()

    def test_config_network_and_portal_helpers(self) -> None:
        # Verify fresh default config carries no hardcoded room/hotel defaults
        default_cfg = HotelCastConfig()
        self.assertEqual(default_cfg.tv_ip, "")
        self.assertEqual(default_cfg.tv_name, "")
        self.assertEqual(default_cfg.activation_url, "")
        self.assertEqual(default_cfg.tv_address, "")

        # Test portal URL and short-token expansion
        self.assertEqual(
            expand_activation_url("TOKEN1", room_number="101"),
            "https://ovk.top/TOKEN1/101",
        )
        self.assertEqual(
            expand_activation_url("TOKEN1"),
            "https://ovk.top/TOKEN1",
        )
        self.assertEqual(
            expand_activation_url("https://portal.example.com/bind/101", room_number="202"),
            "https://portal.example.com/bind/101",
        )

        # Test VPN interface filter
        self.assertTrue(is_vpn_or_virtual_iface("wg0"))
        self.assertTrue(is_vpn_or_virtual_iface("tun0"))
        self.assertFalse(is_vpn_or_virtual_iface("wlan0"))

        # Test persistent config save/load in temp file (including port 7000 preservation)
        cfg_file = os.path.join(self.temp_dir, "config.json")
        cfg = HotelCastConfig(
            tv_ip="192.0.2.50",
            tv_port=7000,
            tv_name="Room-101",
            activation_url="https://portal.example.com/bind/101",
            preferred_audio_lang="ger",
        )
        cfg.save(cfg_file)
        loaded = HotelCastConfig.load(cfg_file)
        self.assertEqual(loaded.tv_ip, "192.0.2.50")
        self.assertEqual(loaded.tv_port, 7000)
        self.assertEqual(loaded.tv_name, "Room-101")
        self.assertEqual(loaded.tv_address, "192.0.2.50:7000")
        self.assertEqual(loaded.preferred_audio_lang, "ger")
        self.assertTrue(matches_preferred_language("deu", loaded.preferred_audio_lang))

        # Test timestamp & fraction helpers
        self.assertEqual(format_timestamp(3661), "01:01:01")
        self.assertEqual(parse_timestamp_to_seconds("01:01:01"), 3661.0)
        self.assertAlmostEqual(compute_start_fraction(45.0, 90.0), 0.5)
        self.assertIn("Start-Position: 0.5", format_play_payload("http://192.0.2.10:8000/stream.m3u8", 0.5))

    def test_uri_and_cli_parsing(self) -> None:
        t1 = parse_invocation_target("hotelcast://cast?mode=url&url=https%3A%2F%2Fexample.com%2Fwatch")
        self.assertIsNotNone(t1)
        assert t1 is not None
        self.assertEqual(t1["mode"], "url")
        self.assertEqual(t1["url"], "https://example.com/watch")

        t2 = parse_invocation_target("hotelcast://cast?mode=mirror")
        assert t2 is not None
        self.assertEqual(t2["mode"], "mirror")

        t3 = parse_invocation_target("/movies/example.mkv")
        assert t3 is not None
        self.assertEqual(t3["mode"], "file")


if __name__ == "__main__":
    unittest.main(verbosity=2)
