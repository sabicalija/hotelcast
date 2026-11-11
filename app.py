#!/usr/bin/env python3
"""
HotelCast — Automated Linux-to-AirPlay/HappyCast Streamer (`app.py`).

Features:
- Persistent settings in `~/.config/hotelcast/config.json` (`config.py`) with fast 0.5s
  startup TCP reachability verification and `[Rescan Network]` override.
- Physical Wi-Fi interface pinning & VPN bypass routing (`network.py`),
  dynamically re-detecting the Wi-Fi IPv4 before every `/play` request.
- Optional Hotel Portal Activation bar with `[Activate TV Bridge]` button,
  short-code expansion, and automatic activation on TV connection timeout.
- Persistent per-movie `.hls/` cache next to local video files (`/path/to/MovieName.mkv.hls/`)
  with instant zero-reconversion resume, `[Force Re-convert]` checkbox, and `[Delete Cached HLS]` button.
- Dual Seeking Support: `0%`–`100%` Seek Slider synchronized with `HH:MM:SS` timestamp,
  fractional `Start-Position` (`0.0`–`0.99`), and dynamic `seek_stream.m3u8` slicing.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import queue
import signal
import sys
import threading
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import parse_qs, unquote, urlparse

__version__ = "0.1.0"

from airplay import AirPlayController, compute_start_fraction, wait_for_hls_ready
from config import HotelCastConfig, select_preferred_audio_index
from discovery import (
    DEFAULT_AIRPLAY_PORT,
    DiscoveredDevice,
    check_firewall,
    discover_devices,
    get_local_ip,
    parse_manual_target,
)
from network import (
    activate_hotel_tv_bridge,
    check_tv_reachable,
    expand_activation_url,
    find_physical_wifi_interface,
)
from server import DEFAULT_HTTP_PORT, HLSServer
from streamer import (
    DEFAULT_HLS_DIR,
    AudioTrack,
    HLSStreamer,
    WindowInfo,
    cleanup_hls_dir,
    delete_video_hls_cache,
    format_timestamp,
    generate_seek_playlist,
    get_hls_cache_size_mb,
    get_video_hls_cache_dir,
    is_hls_cache_complete,
    list_chrome_windows,
    parse_timestamp_to_seconds,
    probe_media_info,
)


def parse_invocation_target(raw_arg: str) -> Optional[Dict[str, str]]:
    """
    Parse a positional CLI / desktop argument:
    - `hotelcast://cast?mode=url&url=<urlencoded_url>&title=<title>`
    - `hotelcast://cast?mode=mirror`
    - `file:///path/to/video.mkv`
    - `/path/to/video.mkv`
    """
    if not raw_arg or not raw_arg.strip():
        return None
    val = raw_arg.strip()

    if val.lower().startswith("hotelcast://"):
        parsed = urlparse(val)
        params = parse_qs(parsed.query)
        mode = (params.get("mode", ["url"])[0] or "url").strip().lower()
        result: Dict[str, str] = {"mode": mode}
        if "url" in params and params["url"]:
            result["url"] = unquote(params["url"][0])
        if "title" in params and params["title"]:
            result["title"] = unquote(params["title"][0])
        if "file" in params and params["file"]:
            result["file"] = unquote(params["file"][0])
        if "tv" in params and params["tv"]:
            result["tv"] = unquote(params["tv"][0])
        return result

    if val.lower().startswith("file://"):
        parsed = urlparse(val)
        file_path = unquote(parsed.path)
        return {"mode": "file", "file": file_path}

    expanded = os.path.abspath(os.path.expanduser(val))
    if os.path.isfile(expanded) or any(
        val.lower().endswith(ext)
        for ext in (".mkv", ".mp4", ".avi", ".mov", ".webm", ".ts", ".m4v", ".flv")
    ):
        return {"mode": "file", "file": expanded}

    if val.lower().startswith(("http://", "https://")):
        return {"mode": "url", "url": val}

    return None


def try_forward_to_running_instance(
    payload: Dict[str, str],
    port: int = DEFAULT_HTTP_PORT,
    timeout: float = 1.5,
) -> Optional[Dict[str, Any]]:
    """
    If a HotelCast server is already running on `127.0.0.1:<port..port+4>`, forward the
    `hotelcast://` or file-manager cast request to `POST /api/cast` so no duplicate
    window is spawned.
    """
    if payload.get("mode") not in ("file", "url", "mirror"):
        return None

    body = json.dumps(payload).encode("utf-8")
    for candidate_port in range(port, port + 5):
        conn: Optional[http.client.HTTPConnection] = None
        try:
            conn = http.client.HTTPConnection("127.0.0.1", candidate_port, timeout=timeout)
            conn.request(
                "POST",
                "/api/cast",
                body=body,
                headers={"Content-Type": "application/json", "Content-Length": str(len(body))},
            )
            resp = conn.getresponse()
            raw = resp.read().decode("utf-8", errors="replace")
            if resp.status == 200:
                parsed = json.loads(raw)
                if isinstance(parsed, dict) and "status" in parsed:
                    return parsed
        except Exception:
            pass
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
    return None


class HotelCastSession:
    """
    Coordinates the HTTP server, FFmpeg HLS pipeline, per-movie `.hls/` cache,
    portal activation, AirPlay keep-alive controller, and `/api/cast` JSON control endpoint.
    """

    def __init__(
        self,
        hls_dir: str = DEFAULT_HLS_DIR,
        http_port: int = DEFAULT_HTTP_PORT,
        config: Optional[HotelCastConfig] = None,
        on_log: Optional[Callable[[str], None]] = None,
        on_state_change: Optional[Callable[[str], None]] = None,
        on_gui_sync: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_cache_complete: Optional[Callable[[str], None]] = None,
        on_cache_progress: Optional[Callable[[float, float, str], None]] = None,
        tv_resolver: Optional[Callable[[], str | DiscoveredDevice]] = None,
        use_custom_hls_dir_for_files: bool = False,
    ) -> None:
        self.default_hls_dir = hls_dir
        self.use_custom_hls_dir_for_files = use_custom_hls_dir_for_files
        self.requested_http_port = http_port
        self.config = config or HotelCastConfig.load()
        self.on_log = on_log
        self.on_state_change = on_state_change
        self.on_gui_sync = on_gui_sync
        self.on_cache_complete = on_cache_complete
        self.on_cache_progress = on_cache_progress
        self.tv_resolver = tv_resolver

        self.server = HLSServer(
            hls_dir=hls_dir,
            port=http_port,
            on_log=self._log,
            on_api_cast=self.handle_api_cast,
            on_api_stop=self.handle_api_stop,
            on_api_status=self.handle_api_status,
        )
        self.streamer = HLSStreamer(
            hls_dir=hls_dir,
            on_log=self._log,
            on_cache_complete=self._handle_cache_complete,
            on_progress=self._handle_cache_progress,
        )
        self.airplay: Optional[AirPlayController] = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self.active = False
        self.current_target: str = self.config.tv_address
        self.current_mode: str = "idle"

    def _log(self, msg: str) -> None:
        if self.on_log:
            self.on_log(msg)

    def _set_state(self, state: str) -> None:
        if self.on_state_change:
            self.on_state_change(state)

    def _handle_cache_complete(self, cache_dir: str) -> None:
        if callable(self.on_cache_complete):
            self.on_cache_complete(cache_dir)

    def _handle_cache_progress(self, pos_sec: float, total_dur: float, speed_str: str) -> None:
        if callable(self.on_cache_progress):
            self.on_cache_progress(pos_sec, total_dur, speed_str)

    def ensure_server_running(self) -> int:
        """Ensure the local HTTP + `/api/cast` server is listening."""
        return self.server.start()

    def resolve_target_device(
        self,
        explicit_target: Optional[str | DiscoveredDevice] = None,
    ) -> DiscoveredDevice:
        """Resolve the target TV via explicit argument, GUI callback, saved config, or mDNS."""
        if isinstance(explicit_target, DiscoveredDevice):
            return explicit_target
        if isinstance(explicit_target, str) and explicit_target.strip():
            return parse_manual_target(explicit_target.strip(), name=self.config.tv_name or None)

        if callable(self.tv_resolver):
            try:
                resolved = self.tv_resolver()
                if isinstance(resolved, DiscoveredDevice):
                    return resolved
                if isinstance(resolved, str) and resolved.strip():
                    return parse_manual_target(resolved.strip(), name=self.config.tv_name or None)
            except Exception:
                pass

        if self.config.tv_ip:
            return DiscoveredDevice(
                name=self.config.tv_name or f"Saved TV ({self.config.tv_ip})",
                ip=self.config.tv_ip,
                port=self.config.tv_port,
                service_type="saved",
            )

        devices = discover_devices(timeout=2.5, on_log=self._log)
        if devices:
            return devices[0]

        if self.current_target:
            return parse_manual_target(self.current_target, name=self.config.tv_name or None)

        raise ValueError("No target TV configured or discovered. Scan the network or specify TV IP:Port.")

    def handle_api_cast(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Handle `POST /api/cast` from Chrome Extension or Ubuntu file manager:
        Accepts `{"mode": "file" | "url" | "mirror", "url": "<page_or_video_url>", "file": "<path>", "title": "<tab_title>"}`.
        """
        mode = str(payload.get("mode", "url")).strip().lower()
        url = str(payload.get("url", "") or "").strip()
        title = str(payload.get("title", "") or "").strip()
        file_path = str(payload.get("file", "") or "").strip() or None
        tv_override = payload.get("tv")

        self._log(
            f"[API /api/cast] Received request: mode={mode}"
            + (f", title='{title}'" if title else "")
            + (f", url={url}" if url else "")
            + (f", file={file_path}" if file_path else "")
        )

        if callable(self.on_gui_sync):
            try:
                self.on_gui_sync({"mode": mode, "url": url, "title": title, "file": file_path})
            except Exception:
                pass

        device = self.resolve_target_device(tv_override)
        target_str = f"{device.ip}:{device.port}"

        audio_idx = 0
        total_dur = 0.0
        if mode == "file" and file_path and os.path.isfile(file_path):
            tracks, total_dur = probe_media_info(file_path)
            audio_idx = select_preferred_audio_index(tracks, self.config.preferred_audio_lang)

        window_id: Optional[str] = None
        if mode == "mirror" and title:
            for win in list_chrome_windows():
                if win.window_id and (
                    title.lower() in win.title.lower() or "chrome" in win.title.lower()
                ):
                    window_id = win.window_id
                    break

        self.start_session(
            tv_target=device,
            mode=mode,
            file_path=file_path,
            audio_index=audio_idx,
            total_duration=total_dur,
            web_url=url if mode == "url" else None,
            window_id=window_id,
        )
        return {"status": "playing", "target": target_str}

    def handle_api_stop(self) -> Dict[str, Any]:
        """Handle `POST /api/stop` from Chrome Extension."""
        self._log("[API /api/stop] Stopping active cast session...")
        self.stop_session(cleanup_files=True, stop_server=False)
        return {"status": "stopped"}

    def handle_api_status(self) -> Dict[str, Any]:
        """Handle `GET /api/status`."""
        return {
            "status": "playing" if self.active else "idle",
            "mode": self.current_mode if self.active else "idle",
            "target": self.current_target,
        }

    def start_session(
        self,
        tv_target: str | DiscoveredDevice,
        mode: str,
        file_path: Optional[str] = None,
        audio_index: int = 0,
        start_time: Optional[str] = None,
        seek_seconds: Optional[float] = None,
        total_duration: float = 0.0,
        force_reconvert: bool = False,
        web_url: Optional[str] = None,
        window_id: Optional[str] = None,
        min_segments: int = 2,
        ready_timeout: float = 30.0,
    ) -> str:
        """
        Start the full end-to-end streaming pipeline and trigger AirPlay `/play`.
        Always dynamically re-detects the laptop's current physical Wi-Fi IP
        before every `/play` request.
        """
        effective_seek_sec = (
            float(seek_seconds)
            if seek_seconds is not None
            else parse_timestamp_to_seconds(start_time)
        )
        custom_dir = (
            self.default_hls_dir
            if (self.use_custom_hls_dir_for_files or self.default_hls_dir != DEFAULT_HLS_DIR)
            else None
        )
        keep_running_ffmpeg = (
            mode == "file"
            and bool(file_path)
            and not force_reconvert
            and self.streamer.can_reuse_running_conversion(
                file_path,
                audio_index=audio_index,
                seek_seconds=effective_seek_sec,
                custom_cache_dir=custom_dir,
            )
        )

        self.stop_session(
            cleanup_files=not keep_running_ffmpeg,
            stop_server=False,
            stop_streamer=not keep_running_ffmpeg,
        )
        self._stop_event.clear()
        self.active = True
        self.current_mode = mode
        self._set_state("Preparing...")

        if isinstance(tv_target, DiscoveredDevice):
            device = tv_target
        else:
            device = parse_manual_target(tv_target, name=self.config.tv_name)

        self.current_target = f"{device.ip}:{device.port}"

        # Always dynamically re-detect laptop's current physical Wi-Fi IP before every /play
        local_ip = get_local_ip(device.ip, device.port, on_log=self._log)
        wifi_info = find_physical_wifi_interface()
        bound_port = self.server.start()
        check_firewall(http_port=bound_port, on_log=self._log)

        start_fraction = 0.0
        playlist_name = "stream.m3u8"

        if mode == "file":
            if not file_path:
                raise ValueError("No local video file selected.")

            if total_duration <= 0.0:
                _, total_duration = probe_media_info(file_path)

            cache_already_complete = (
                not force_reconvert
                and is_hls_cache_complete(file_path, audio_index, cache_dir=custom_dir)
            )

            # When cache is already complete or an active full-movie conversion is being reused,
            # do not pass -ss to ffmpeg; instead use dual seeking: Start-Position fraction + seek_stream.m3u8
            ffmpeg_start_arg = (
                None
                if (cache_already_complete or keep_running_ffmpeg)
                else (format_timestamp(effective_seek_sec) if effective_seek_sec > 0 else None)
            )

            active_dir, used_cache = self.streamer.start_local_file(
                input_file=file_path,
                audio_index=audio_index,
                start_time=ffmpeg_start_arg,
                force_reconvert=force_reconvert,
                custom_cache_dir=custom_dir,
            )
            self.server.set_hls_dir(active_dir)

            if (used_cache or keep_running_ffmpeg) and effective_seek_sec > 0.0:
                start_fraction = compute_start_fraction(effective_seek_sec, total_duration)
                # Also generate seek_stream.m3u8 in the .hls/ folder starting at effective_seek_sec
                seek_pl, seg_offset = generate_seek_playlist(active_dir, effective_seek_sec)
                playlist_name = seek_pl
                self._log(
                    f"Prepared seek target: {format_timestamp(effective_seek_sec)} "
                    f"(Start-Position={start_fraction:.4f}, serving sliced playlist={seek_pl} @ {seg_offset:.1f}s)"
                )

        elif mode == "url":
            if not web_url:
                raise ValueError("No web URL specified.")
            self.server.set_hls_dir(self.default_hls_dir)
            self.streamer.start_web_url(web_url)
        elif mode == "mirror":
            self.server.set_hls_dir(self.default_hls_dir)
            self.streamer.start_mirror(window_id=window_id)
        else:
            raise ValueError(f"Unsupported streaming mode: {mode}")

        stream_url = self.server.stream_url(local_ip, playlist_name=playlist_name)
        iface_tag = f" ({wifi_info.ifname})" if wifi_info and not device.ip.startswith("127.") else ""
        self._log(
            f"Target TV: {device.ip}:{device.port} | Wi-Fi IP{iface_tag}: {local_ip} | Stream URL: {stream_url}"
        )

        self._set_state("Buffering HLS segments...")
        ready = wait_for_hls_ready(
            hls_dir=self.server.hls_dir,
            min_segments=min_segments,
            timeout=ready_timeout,
            stop_event=self._stop_event,
            on_log=self._log,
            playlist_name=playlist_name,
        )
        if not ready:
            self.stop_session(cleanup_files=True, stop_server=False)
            raise RuntimeError("HLS stream failed to produce initial segments before timeout.")

        self._set_state(f"Connecting to TV ({device.ip}:{device.port})...")
        controller = AirPlayController(
            tv_ip=device.ip,
            tv_port=device.port,
            on_log=self._log,
        )
        with self._lock:
            self.airplay = controller

        try:
            status = controller.play(content_location=stream_url, start_position=start_fraction)
        except (TimeoutError, OSError) as exc:
            # Automatically attempt Hotel Portal Activation if connecting to <tv_ip>:<tv_port> times out
            if (
                not device.ip.startswith("127.")
                and self.config.activation_url
                and not self._stop_event.is_set()
            ):
                self._log(
                    f"Connection to {device.ip}:{device.port} timed out ({exc}); "
                    f"auto-triggering Hotel Portal Activation..."
                )
                ok, _ = activate_hotel_tv_bridge(
                    self.config.activation_url,
                    room_number=self.config.tv_name,
                    on_log=self._log,
                )
                if ok:
                    time.sleep(0.5)
                    status = controller.play(
                        content_location=stream_url,
                        start_position=start_fraction,
                    )
                else:
                    raise
            else:
                raise

        self._set_state(f"Streaming to {device.ip}:{device.port} (HTTP {status})")
        return stream_url

    def stop_session(
        self,
        cleanup_files: bool = True,
        stop_server: bool = True,
        stop_streamer: bool = True,
    ) -> None:
        """
        Clean teardown:
        1. Send `POST /stop` to the TV and close keep-alive socket
        2. Terminate `ffmpeg` subprocess (if `stop_streamer=True`)
        3. Shut down local HTTP server (if `stop_server=True`)
        4. Clean up temporary `/tmp/hotelcast_hls/` files (preserving `.mkv.hls/` caches!)
        """
        self._stop_event.set()
        self.active = False
        self.current_mode = "idle"

        with self._lock:
            controller = self.airplay
            self.airplay = None

        if controller is not None:
            try:
                controller.stop(send_stop_request=True)
            except Exception:
                pass

        if stop_streamer:
            try:
                self.streamer.stop(cleanup_temp=cleanup_files)
            except Exception:
                pass

        if stop_server:
            try:
                self.server.stop()
            except Exception:
                pass

        if cleanup_files and self.default_hls_dir == DEFAULT_HLS_DIR:
            cleanup_hls_dir(self.default_hls_dir)

        self._set_state("Stopped")


class HotelCastGUI:
    """
    Single-window desktop GUI for HotelCast built with Tkinter and `ttk` styling.
    """

    def __init__(
        self,
        initial_port: int = DEFAULT_HTTP_PORT,
        startup_task: Optional[Dict[str, str]] = None,
    ) -> None:
        import tkinter as tk
        from tkinter import filedialog, messagebox, scrolledtext, ttk

        self.tk = tk
        self.ttk = ttk
        self.filedialog = filedialog
        self.messagebox = messagebox
        self.scrolledtext = scrolledtext
        self.startup_task = startup_task

        self.config = HotelCastConfig.load()

        self.root = tk.Tk()
        self.root.title("HotelCast — Linux to AirPlay / HappyCast Streamer")
        self.root.geometry("880x720")
        self.root.minsize(780, 620)

        self._log_queue: queue.Queue[str] = queue.Queue()
        self._devices: List[DiscoveredDevice] = []
        self._audio_tracks: List[AudioTrack] = []
        self._windows: List[WindowInfo] = []
        self._current_file_duration: float = 0.0
        self._updating_seek_widgets: bool = False

        self.session = HotelCastSession(
            http_port=initial_port,
            config=self.config,
            on_log=self.enqueue_log,
            on_state_change=self._on_state_change_threadsafe,
            on_gui_sync=self._on_api_gui_sync_threadsafe,
            on_cache_complete=self._on_cache_complete_threadsafe,
            on_cache_progress=self._on_cache_progress_threadsafe,
            tv_resolver=self._get_current_tv_target,
        )

        self._configure_styles()
        self._build_widgets()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(100, self._drain_log_queue)
        self.root.after(150, self._start_control_server)
        self.root.after(200, self._startup_fast_tv_check)
        self.root.after(300, self._refresh_windows)
        if self.startup_task:
            self.root.after(600, lambda: self._execute_startup_task(self.startup_task))

    def _start_control_server(self) -> None:
        try:
            self.session.ensure_server_running()
        except Exception as exc:
            self.enqueue_log(f"Control API server warning: {exc}")

    def _startup_fast_tv_check(self) -> None:
        """
        On launch, if a saved `tv_ip:tv_port` exists, verify reachability via a fast
        `0.5s` TCP connect check; otherwise run an initial mDNS network scan.
        """
        saved_ip = self.config.tv_ip.strip()
        saved_port = self.config.tv_port
        saved_name = self.config.tv_name or (f"Saved TV ({saved_ip})" if saved_ip else "")
        wifi = find_physical_wifi_interface()
        if wifi:
            self.enqueue_log(f"Pinned physical Wi-Fi interface: {wifi.ifname} (IPv4: {wifi.ipv4})")

        if not saved_ip:
            self._trigger_scan()
            return

        def _check_worker() -> None:
            reachable = check_tv_reachable(saved_ip, saved_port, timeout=0.5, wifi_info=wifi)
            dev = DiscoveredDevice(
                name=saved_name,
                ip=saved_ip,
                port=saved_port,
                service_type="saved",
            )
            if reachable:
                self.enqueue_log(
                    f"Fast startup check: Saved TV '{saved_name}' at {saved_ip}:{saved_port} is REACHABLE (0.5s TCP OK)."
                )
            else:
                self.enqueue_log(
                    f"Saved TV {saved_ip}:{saved_port} did not respond to 0.5s TCP probe. "
                    f"Click [Activate TV Bridge] or [Rescan Network] if needed."
                )
            self.root.after(0, lambda: self._populate_devices([dev]))

        threading.Thread(target=_check_worker, daemon=True).start()

    def _save_current_config(self) -> None:
        room = self.room_name_var.get().strip()
        self.config.tv_name = room

        raw_target = self.manual_ip_var.get().strip()
        if raw_target:
            try:
                dev = parse_manual_target(raw_target, name=room or None)
                self.config.tv_ip = dev.ip
                self.config.tv_port = dev.port
            except ValueError:
                pass
        else:
            self.config.tv_ip = ""

        act_raw = self.activation_url_var.get().strip()
        if act_raw:
            try:
                self.config.activation_url = expand_activation_url(act_raw, room_number=room)
            except ValueError:
                self.config.activation_url = act_raw
        else:
            self.config.activation_url = ""

        self.config.save()
        if self.config.tv_address and self.manual_ip_var.get().strip() != self.config.tv_address:
            self.manual_ip_var.set(self.config.tv_address)

    def _get_current_tv_target(self) -> str | DiscoveredDevice:
        self._save_current_config()
        if self.config.tv_address:
            return parse_manual_target(self.config.tv_address, name=self.config.tv_name or None)
        return ""

    def _on_api_gui_sync_threadsafe(self, info: Dict[str, Any]) -> None:
        try:
            self.root.after(0, lambda: self._apply_gui_sync(info))
        except Exception:
            pass

    def _on_cache_complete_threadsafe(self, _cache_dir: str) -> None:
        try:
            self.root.after(0, self._update_cache_status_label)
        except Exception:
            pass

    def _on_cache_progress_threadsafe(self, pos_sec: float, total_dur: float, speed_str: str) -> None:
        try:
            if total_dur > 0:
                pct = min(99, max(1, int(round((pos_sec / total_dur) * 100.0))))
                msg = (
                    f"Caching Full Movie: {pct}% ({format_timestamp(pos_sec)} / "
                    f"{format_timestamp(total_dur)} @ {speed_str})"
                )
            else:
                msg = f"Caching Movie: {format_timestamp(pos_sec)} (@ {speed_str})"
            self.root.after(0, lambda: self.cache_status_var.set(msg))
        except Exception:
            pass

    def _apply_gui_sync(self, info: Dict[str, Any]) -> None:
        mode = info.get("mode", "url")
        url = info.get("url", "")
        file_path = info.get("file", "")
        tv_val = info.get("tv", "")
        if tv_val:
            self.manual_ip_var.set(tv_val)
            self._save_current_config()
        if mode == "file" and file_path:
            self.notebook.select(0)
            self.file_path_var.set(file_path)
            self._probe_selected_file(file_path)
        elif mode == "url":
            self.notebook.select(1)
            if url:
                self.web_url_var.set(url)
        elif mode == "mirror":
            self.notebook.select(2)

        self.start_btn.configure(state=self.tk.DISABLED)
        self.stop_btn.configure(state=self.tk.NORMAL)

    def _execute_startup_task(self, task: Optional[Dict[str, str]]) -> None:
        if not task:
            return
        mode = task.get("mode", "file")
        if task.get("tv"):
            self.manual_ip_var.set(task["tv"])

        if mode == "file" and task.get("file"):
            path = task["file"]
            self.notebook.select(0)
            self.file_path_var.set(path)
            if os.path.isfile(path):
                self._probe_selected_file(path)
            self._on_start_clicked()
        elif mode == "url" and task.get("url"):
            self.notebook.select(1)
            self.web_url_var.set(task["url"])
            self._on_start_clicked()
        elif mode == "mirror":
            self.notebook.select(2)
            self._on_start_clicked()

    def _configure_styles(self) -> None:
        style = self.ttk.Style(self.root)
        available = style.theme_names()
        for preferred in ("clam", "alt", "default"):
            if preferred in available:
                style.theme_use(preferred)
                break

        style.configure("TFrame", padding=4)
        style.configure("TLabelframe", padding=8)
        style.configure("TLabelframe.Label", font=("Sans", 10, "bold"))
        style.configure("TButton", padding=(10, 5), font=("Sans", 10))
        style.configure("Accent.TButton", padding=(14, 6), font=("Sans", 10, "bold"))
        style.configure("Status.TLabel", font=("Sans", 10, "bold"))

    def _build_widgets(self) -> None:
        tk = self.tk
        ttk = self.ttk

        main_frame = ttk.Frame(self.root, padding=10)
        main_frame.pack(fill=tk.BOTH, expand=True)

        # 0. Hotel Portal Activation Bar
        portal_frame = ttk.LabelFrame(
            main_frame, text="Hotel Portal / TV Bridge Activation (Optional)"
        )
        portal_frame.pack(fill=tk.X, pady=(0, 8))

        ttk.Label(portal_frame, text="Portal URL / Short Code:").grid(
            row=0, column=0, sticky=tk.W, padx=(4, 6), pady=4
        )
        self.activation_url_var = tk.StringVar(value=self.config.activation_url)
        self.activation_entry = ttk.Entry(
            portal_frame, textvariable=self.activation_url_var, width=38
        )
        self.activation_entry.grid(row=0, column=1, sticky=tk.EW, padx=4, pady=4)

        ttk.Label(portal_frame, text="Room #:").grid(
            row=0, column=2, sticky=tk.W, padx=(10, 4), pady=4
        )
        self.room_name_var = tk.StringVar(value=self.config.tv_name)
        self.room_entry = ttk.Entry(portal_frame, textvariable=self.room_name_var, width=8)
        self.room_entry.grid(row=0, column=3, sticky=tk.W, padx=4, pady=4)

        self.activate_btn = ttk.Button(
            portal_frame,
            text="Activate TV Bridge",
            command=self._on_activate_portal_clicked,
        )
        self.activate_btn.grid(row=0, column=4, padx=(8, 4), pady=4)
        portal_frame.columnconfigure(1, weight=1)

        # 1. Target TV Bar
        tv_frame = ttk.LabelFrame(main_frame, text="1. Target Hotel TV (AirPlay / HappyCast)")
        tv_frame.pack(fill=tk.X, pady=(0, 8))

        ttk.Label(tv_frame, text="Discovered / Saved TV:").grid(
            row=0, column=0, sticky=tk.W, padx=(4, 6), pady=4
        )
        self.tv_combo_var = tk.StringVar(value="")
        self.tv_combo = ttk.Combobox(
            tv_frame,
            textvariable=self.tv_combo_var,
            state="readonly",
            width=32,
        )
        self.tv_combo.grid(row=0, column=1, sticky=tk.EW, padx=4, pady=4)
        self.tv_combo.bind("<<ComboboxSelected>>", self._on_tv_selected)

        self.scan_btn = ttk.Button(tv_frame, text="Rescan Network", command=self._trigger_scan)
        self.scan_btn.grid(row=0, column=2, padx=4, pady=4)

        ttk.Label(tv_frame, text="TV IP:Port:").grid(
            row=0, column=3, sticky=tk.W, padx=(10, 6), pady=4
        )
        self.manual_ip_var = tk.StringVar(value=self.config.tv_address)
        self.manual_ip_entry = ttk.Entry(tv_frame, textvariable=self.manual_ip_var, width=20)
        self.manual_ip_entry.grid(row=0, column=4, sticky=tk.EW, padx=4, pady=4)

        tv_frame.columnconfigure(1, weight=2)
        tv_frame.columnconfigure(4, weight=1)

        # 2. Source Tabs
        source_frame = ttk.LabelFrame(main_frame, text="2. Media Source & HLS Cache")
        source_frame.pack(fill=tk.X, pady=(0, 8))

        self.notebook = ttk.Notebook(source_frame)
        self.notebook.pack(fill=tk.X, expand=True, padx=2, pady=2)

        # Tab A: Local File
        self.tab_file = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(self.tab_file, text="  Local Video File  ")

        ttk.Label(self.tab_file, text="Video File:").grid(
            row=0, column=0, sticky=tk.W, padx=4, pady=5
        )
        self.file_path_var = tk.StringVar(value="")
        self.file_entry = ttk.Entry(self.tab_file, textvariable=self.file_path_var)
        self.file_entry.grid(row=0, column=1, columnspan=2, sticky=tk.EW, padx=4, pady=5)
        self.browse_btn = ttk.Button(
            self.tab_file, text="Browse...", command=self._on_browse_file
        )
        self.browse_btn.grid(row=0, column=3, padx=4, pady=5)

        ttk.Label(self.tab_file, text="Audio Track:").grid(
            row=1, column=0, sticky=tk.W, padx=4, pady=5
        )
        self.audio_track_var = tk.StringVar(value="Track 0 (Default)")
        self.audio_combo = ttk.Combobox(
            self.tab_file,
            textvariable=self.audio_track_var,
            state="readonly",
        )
        self.audio_combo.grid(row=1, column=1, columnspan=2, sticky=tk.EW, padx=4, pady=5)
        self.audio_combo.bind("<<ComboboxSelected>>", self._on_audio_track_selected)

        # Persistent HLS Cache Controls row
        ttk.Label(self.tab_file, text="HLS Cache:").grid(
            row=2, column=0, sticky=tk.W, padx=4, pady=5
        )
        self.cache_status_var = tk.StringVar(value="No file selected")
        self.cache_status_label = ttk.Label(self.tab_file, textvariable=self.cache_status_var)
        self.cache_status_label.grid(row=2, column=1, sticky=tk.W, padx=4, pady=5)

        self.force_reconvert_var = tk.BooleanVar(value=False)
        self.force_reconvert_chk = ttk.Checkbutton(
            self.tab_file,
            text="Force Re-convert",
            variable=self.force_reconvert_var,
        )
        self.force_reconvert_chk.grid(row=2, column=2, sticky=tk.E, padx=4, pady=5)

        self.delete_cache_btn = ttk.Button(
            self.tab_file,
            text="Delete Cached HLS",
            command=self._on_delete_cache_clicked,
        )
        self.delete_cache_btn.grid(row=2, column=3, padx=4, pady=5)

        # Seek Slider (0%-100%) + Timestamp (HH:MM:SS) row
        ttk.Label(self.tab_file, text="Seek / Start:").grid(
            row=3, column=0, sticky=tk.W, padx=4, pady=6
        )
        seek_subframe = ttk.Frame(self.tab_file)
        seek_subframe.grid(row=3, column=1, columnspan=3, sticky=tk.EW, padx=2, pady=4)

        self.seek_pct_var = tk.DoubleVar(value=0.0)
        self.seek_slider = ttk.Scale(
            seek_subframe,
            from_=0.0,
            to=100.0,
            orient=tk.HORIZONTAL,
            variable=self.seek_pct_var,
            command=self._on_seek_slider_moved,
        )
        self.seek_slider.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))
        self.seek_slider.bind("<ButtonRelease-1>", self._on_seek_committed)

        self.seek_pct_label_var = tk.StringVar(value="0%")
        ttk.Label(seek_subframe, textvariable=self.seek_pct_label_var, width=5).pack(
            side=tk.LEFT, padx=(0, 6)
        )

        self.start_time_var = tk.StringVar(value="00:00:00")
        self.start_time_entry = ttk.Entry(
            seek_subframe, textvariable=self.start_time_var, width=10
        )
        self.start_time_entry.pack(side=tk.LEFT, padx=2)
        self.start_time_entry.bind("<Return>", self._on_seek_committed)
        self.start_time_entry.bind("<FocusOut>", self._on_timestamp_edited)

        self.duration_label_var = tk.StringVar(value="/ 00:00:00")
        ttk.Label(seek_subframe, textvariable=self.duration_label_var).pack(
            side=tk.LEFT, padx=(4, 6)
        )

        self.seek_jump_btn = ttk.Button(
            seek_subframe,
            text="Jump / Seek",
            command=self._on_jump_seek_clicked,
        )
        self.seek_jump_btn.pack(side=tk.LEFT, padx=(2, 0))

        self.tab_file.columnconfigure(1, weight=1)

        # Tab B: Web URL
        self.tab_url = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(self.tab_url, text="  Chrome Web URL  ")

        ttk.Label(self.tab_url, text="Video / Page URL:").grid(
            row=0, column=0, sticky=tk.W, padx=4, pady=10
        )
        self.web_url_var = tk.StringVar(value="")
        self.web_url_entry = ttk.Entry(self.tab_url, textvariable=self.web_url_var)
        self.web_url_entry.grid(row=0, column=1, sticky=tk.EW, padx=4, pady=10)
        ttk.Label(
            self.tab_url,
            text="Supports direct .m3u8/.mp4 links, yt-dlp pages, or 1-click cast from the Chrome Extension.",
        ).grid(row=1, column=1, sticky=tk.W, padx=4, pady=2)
        self.tab_url.columnconfigure(1, weight=1)

        # Tab C: Mirror Chrome / Screen
        self.tab_mirror = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(self.tab_mirror, text="  Mirror Chrome / Screen  ")

        ttk.Label(self.tab_mirror, text="Capture Target:").grid(
            row=0, column=0, sticky=tk.W, padx=4, pady=10
        )
        self.mirror_target_var = tk.StringVar(value="Full Desktop Screen")
        self.mirror_combo = ttk.Combobox(
            self.tab_mirror,
            textvariable=self.mirror_target_var,
            state="readonly",
        )
        self.mirror_combo.grid(row=0, column=1, sticky=tk.EW, padx=4, pady=10)
        self.refresh_win_btn = ttk.Button(
            self.tab_mirror,
            text="Refresh Windows",
            command=self._refresh_windows,
        )
        self.refresh_win_btn.grid(row=0, column=2, padx=4, pady=10)
        self.tab_mirror.columnconfigure(1, weight=1)

        # 3. Controls Bar
        ctrl_frame = ttk.Frame(main_frame)
        ctrl_frame.pack(fill=tk.X, pady=(0, 8))

        self.start_btn = ttk.Button(
            ctrl_frame,
            text="▶ Start Streaming",
            style="Accent.TButton",
            command=self._on_start_clicked,
        )
        self.start_btn.pack(side=tk.LEFT, padx=(0, 8))

        self.stop_btn = ttk.Button(
            ctrl_frame,
            text="■ Stop",
            command=self._on_stop_clicked,
            state=tk.DISABLED,
        )
        self.stop_btn.pack(side=tk.LEFT, padx=(0, 14))

        self.state_var = tk.StringVar(value="Status: Ready")
        self.state_label = ttk.Label(
            ctrl_frame, textvariable=self.state_var, style="Status.TLabel"
        )
        self.state_label.pack(side=tk.LEFT, padx=4)

        # 4. Live Status Log
        log_frame = ttk.LabelFrame(main_frame, text="Live Status & TV Playback Log")
        log_frame.pack(fill=tk.BOTH, expand=True)

        self.log_text = self.scrolledtext.ScrolledText(
            log_frame,
            height=13,
            wrap=tk.WORD,
            font=("Monospace", 9),
            state=tk.DISABLED,
        )
        self.log_text.pack(fill=tk.BOTH, expand=True, padx=2, pady=2)

    def enqueue_log(self, msg: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        self._log_queue.put(f"[{ts}] {msg}")

    def _drain_log_queue(self) -> None:
        tk = self.tk
        try:
            while True:
                line = self._log_queue.get_nowait()
                self.log_text.configure(state=tk.NORMAL)
                self.log_text.insert(tk.END, line + "\n")
                self.log_text.see(tk.END)
                self.log_text.configure(state=tk.DISABLED)
        except queue.Empty:
            pass
        self.root.after(100, self._drain_log_queue)

    def _on_state_change_threadsafe(self, state: str) -> None:
        try:
            self.root.after(0, lambda: self._update_state_ui(state))
        except Exception:
            pass

    def _update_state_ui(self, state: str) -> None:
        self.state_var.set(f"Status: {state}")
        if state == "Stopped":
            self._reset_buttons()
        elif state.startswith("Streaming"):
            self.start_btn.configure(state=self.tk.DISABLED)
            self.stop_btn.configure(state=self.tk.NORMAL)

    def _on_activate_portal_clicked(self) -> None:
        raw_input = self.activation_url_var.get().strip()
        room = self.room_name_var.get().strip() or self.config.tv_name
        if not raw_input:
            self.messagebox.showerror(
                "Missing Activation URL / Code",
                "Please enter the Hotel Portal URL or short QR activation code.",
            )
            return

        full_url = expand_activation_url(raw_input, room_number=room)
        self.activation_url_var.set(full_url)
        self._save_current_config()
        self.activate_btn.configure(state=self.tk.DISABLED)

        def _worker() -> None:
            try:
                activate_hotel_tv_bridge(
                    full_url,
                    room_number=room,
                    on_log=self.enqueue_log,
                )
            finally:
                self.root.after(0, lambda: self.activate_btn.configure(state=self.tk.NORMAL))

        threading.Thread(target=_worker, daemon=True).start()

    def _trigger_scan(self) -> None:
        self.scan_btn.configure(state=self.tk.DISABLED)

        def _worker() -> None:
            devices = discover_devices(
                timeout=3.0,
                on_log=self.enqueue_log,
                saved_ip=self.config.tv_ip,
                saved_port=self.config.tv_port,
                saved_name=self.config.tv_name,
            )
            self.root.after(0, lambda: self._populate_devices(devices))

        threading.Thread(target=_worker, daemon=True).start()

    def _populate_devices(self, devices: List[DiscoveredDevice]) -> None:
        self.scan_btn.configure(state=self.tk.NORMAL)
        if devices:
            self._devices = devices
            labels = [d.display_name for d in devices]
            self.tv_combo["values"] = labels
            self.tv_combo.current(0)
            self.manual_ip_var.set(devices[0].address)
            if devices[0].name and not devices[0].name.startswith(("Room TV", "Saved TV")):
                self.room_name_var.set(devices[0].name)
            self._save_current_config()
        elif self.config.tv_address:
            label_name = self.config.tv_name or "TV"
            self.tv_combo["values"] = [
                f"Saved: {label_name} ({self.config.tv_address})"
            ]
            self.tv_combo.current(0)
        else:
            self.tv_combo["values"] = ["No TVs discovered — enter TV IP:Port"]
            self.tv_combo.current(0)

    def _on_tv_selected(self, _event: object = None) -> None:
        idx = self.tv_combo.current()
        if 0 <= idx < len(self._devices):
            dev = self._devices[idx]
            self.manual_ip_var.set(dev.address)
            if dev.name and not dev.name.startswith(("Room TV", "Saved TV")):
                self.room_name_var.set(dev.name)
            self._save_current_config()

    def _on_browse_file(self) -> None:
        initial_dir = (
            self.config.last_directory
            if self.config.last_directory and os.path.isdir(self.config.last_directory)
            else os.path.expanduser("~")
        )
        path = self.filedialog.askopenfilename(
            title="Select Video File",
            initialdir=initial_dir,
            filetypes=[
                ("Video Files", "*.mkv *.mp4 *.avi *.mov *.webm *.ts *.m4v *.flv"),
                ("All Files", "*.*"),
            ],
        )
        if not path:
            return
        self.file_path_var.set(path)
        self.config.last_directory = os.path.dirname(path)
        self.config.save()
        self._probe_selected_file(path)

    def _probe_selected_file(self, path: str) -> None:
        try:
            tracks, duration = probe_media_info(path)
            self._audio_tracks = tracks
            self._current_file_duration = duration
            self.duration_label_var.set(f"/ {format_timestamp(duration)}")
            self.seek_pct_var.set(0.0)
            self.seek_pct_label_var.set("0%")
            self.start_time_var.set("00:00:00")

            if tracks:
                labels = [t.display_label for t in tracks]
                self.audio_combo["values"] = labels
                pref_idx = select_preferred_audio_index(
                    tracks, self.config.preferred_audio_lang
                )
                self.audio_combo.current(pref_idx)
                self.enqueue_log(
                    f"Probed {len(tracks)} audio track(s) in {os.path.basename(path)} "
                    f"(duration {format_timestamp(duration)}, selected track #{pref_idx})."
                )
            else:
                self.audio_combo["values"] = ["No audio streams detected (video-only)"]
                self.audio_combo.current(0)
                self.enqueue_log(
                    f"Probed {os.path.basename(path)} (duration {format_timestamp(duration)}, video-only)."
                )
            self._update_cache_status_label()
        except Exception as exc:
            self.enqueue_log(f"Media probe error: {exc}")

    def _on_audio_track_selected(self, _event: object = None) -> None:
        idx = self.audio_combo.current()
        if 0 <= idx < len(self._audio_tracks):
            lang = self._audio_tracks[idx].language
            if lang and lang != "und":
                self.config.preferred_audio_lang = lang.lower()
                self.config.save()
        self._update_cache_status_label()

    def _update_cache_status_label(self) -> None:
        path = self.file_path_var.get().strip()
        if not path or not os.path.isfile(path):
            self.cache_status_var.set("No file selected")
            return

        audio_idx = max(0, self.audio_combo.current())
        cache_dir = get_video_hls_cache_dir(path)
        if is_hls_cache_complete(path, audio_idx, cache_dir=cache_dir):
            size_mb = get_hls_cache_size_mb(path)
            self.cache_status_var.set(
                f"Ready ({os.path.basename(cache_dir)}, {size_mb:.1f} MB — Instant Start)"
            )
        elif os.path.isdir(cache_dir):
            size_mb = get_hls_cache_size_mb(path)
            self.cache_status_var.set(f"Partial/Other Track ({size_mb:.1f} MB)")
        else:
            self.cache_status_var.set("Not cached yet (will create .hls/ folder)")

    def _on_delete_cache_clicked(self) -> None:
        path = self.file_path_var.get().strip()
        if not path:
            return
        cache_dir = get_video_hls_cache_dir(path)
        if delete_video_hls_cache(path):
            self.enqueue_log(f"Deleted cached HLS directory: {cache_dir}")
        else:
            self.enqueue_log(f"No cached HLS directory found at {cache_dir}")
        self._update_cache_status_label()

    def _on_seek_slider_moved(self, _val: str) -> None:
        if self._updating_seek_widgets:
            return
        self._updating_seek_widgets = True
        try:
            pct = max(0.0, min(100.0, float(self.seek_pct_var.get())))
            self.seek_pct_label_var.set(f"{int(round(pct))}%")
            if self._current_file_duration > 0.0:
                target_sec = (pct / 100.0) * self._current_file_duration
                self.start_time_var.set(format_timestamp(target_sec))
        finally:
            self._updating_seek_widgets = False

    def _on_timestamp_edited(self, _event: object = None) -> None:
        if self._updating_seek_widgets:
            return
        self._updating_seek_widgets = True
        try:
            sec = parse_timestamp_to_seconds(self.start_time_var.get())
            if self._current_file_duration > 0.0:
                pct = min(100.0, max(0.0, (sec / self._current_file_duration) * 100.0))
                self.seek_pct_var.set(pct)
                self.seek_pct_label_var.set(f"{int(round(pct))}%")
        finally:
            self._updating_seek_widgets = False

    def _on_seek_committed(self, _event: object = None) -> None:
        """
        Called when the user releases the Seek Slider or presses Enter in the timestamp entry.
        If a local file session is currently active, immediately jump the TV to the new timestamp.
        """
        self._on_timestamp_edited()
        if self.session.active and self.session.current_mode == "file":
            self._on_start_clicked()

    def _on_jump_seek_clicked(self) -> None:
        """Called when the user clicks [Jump / Seek]. Starts or seeks the local file at the target timestamp."""
        self._on_timestamp_edited()
        self.notebook.select(0)
        self._on_start_clicked()

    def _refresh_windows(self) -> None:
        self._windows = list_chrome_windows()
        labels = [w.display_label for w in self._windows]
        self.mirror_combo["values"] = labels
        if labels:
            self.mirror_combo.current(0)

    def _on_start_clicked(self) -> None:
        self._save_current_config()
        target_str = self.manual_ip_var.get().strip() or self.config.tv_address
        if not target_str:
            self.messagebox.showerror(
                "Missing Target TV",
                "Please select a discovered TV or enter the target TV IP:Port.",
            )
            return

        tab_idx = self.notebook.index(self.notebook.select())
        if tab_idx == 0:
            mode = "file"
            file_path = self.file_path_var.get().strip()
            if not file_path or not os.path.isfile(file_path):
                self.messagebox.showerror("Invalid File", "Please choose an existing local video file.")
                return
            audio_idx = max(0, self.audio_combo.current())
            start_time = self.start_time_var.get().strip()
            seek_seconds = parse_timestamp_to_seconds(start_time)
            force_reconvert = bool(self.force_reconvert_var.get())
            web_url = None
            window_id = None
        elif tab_idx == 1:
            mode = "url"
            web_url = self.web_url_var.get().strip()
            if not web_url:
                self.messagebox.showerror("Missing URL", "Please enter a Chrome video/page URL.")
                return
            file_path = None
            audio_idx = 0
            start_time = None
            seek_seconds = 0.0
            force_reconvert = False
            window_id = None
        else:
            mode = "mirror"
            w_idx = self.mirror_combo.current()
            window_id = (
                self._windows[w_idx].window_id
                if 0 <= w_idx < len(self._windows)
                else None
            )
            file_path = None
            audio_idx = 0
            start_time = None
            seek_seconds = 0.0
            force_reconvert = False
            web_url = None

        self.start_btn.configure(state=self.tk.DISABLED)
        self.stop_btn.configure(state=self.tk.NORMAL)

        def _session_worker() -> None:
            try:
                self.session.start_session(
                    tv_target=target_str,
                    mode=mode,
                    file_path=file_path,
                    audio_index=audio_idx,
                    start_time=start_time,
                    seek_seconds=seek_seconds,
                    total_duration=self._current_file_duration,
                    force_reconvert=force_reconvert,
                    web_url=web_url,
                    window_id=window_id,
                )
                self.root.after(0, self._update_cache_status_label)
            except Exception as exc:
                self.enqueue_log(f"ERROR starting stream: {exc}")
                self.root.after(0, self._reset_buttons)

        threading.Thread(target=_session_worker, daemon=True).start()

    def _on_stop_clicked(self) -> None:
        self.enqueue_log("Stopping streaming session...")
        threading.Thread(target=self._stop_worker, daemon=True).start()

    def _stop_worker(self) -> None:
        self.session.stop_session(cleanup_files=True, stop_server=False)
        self.root.after(0, self._reset_buttons)
        self.root.after(0, self._update_cache_status_label)

    def _reset_buttons(self) -> None:
        self.start_btn.configure(state=self.tk.NORMAL)
        self.stop_btn.configure(state=self.tk.DISABLED)
        if self.force_reconvert_var.get():
            self.force_reconvert_var.set(False)

    def _on_close(self) -> None:
        self._save_current_config()
        self.session.stop_session(cleanup_files=True, stop_server=True)
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hotelcast",
        description="HotelCast — Automated Linux-to-AirPlay/HappyCast Streamer",
    )
    parser.add_argument(
        "--version", "-V",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "target_arg",
        nargs="?",
        default=None,
        help="Optional hotelcast:// URI or local video file path (from Chrome or Ubuntu file manager)",
    )
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--file", "-f",
        metavar="PATH",
        help="Stream a local video file (.mkv, .mp4, .avi, etc.) via per-movie .hls/ cache",
    )
    mode_group.add_argument(
        "--url", "-u",
        metavar="URL",
        help="Stream a web video URL from Chrome (direct link or via yt-dlp)",
    )
    mode_group.add_argument(
        "--mirror", "-m",
        action="store_true",
        help="Mirror live desktop screen or Chrome window + system audio",
    )
    mode_group.add_argument(
        "--activate",
        metavar="URL_OR_TOKEN",
        help="Activate Hotel TV Bridge via portal URL or short token and exit",
    )
    mode_group.add_argument(
        "--scan",
        action="store_true",
        help="Scan LAN for AirPlay / HappyCast TVs and exit",
    )
    mode_group.add_argument(
        "--list-tracks",
        metavar="PATH",
        help="Probe and list audio tracks in a local video file and exit",
    )

    parser.add_argument(
        "--tv", "-t",
        metavar="IP[:PORT]",
        help="Target TV IP and port (defaults to saved ~/.config/hotelcast/config.json)",
    )
    parser.add_argument(
        "--audio-track", "-a",
        type=int,
        default=None,
        help="0-based audio track index for --file (auto-selects preferred_audio_lang if omitted)",
    )
    parser.add_argument(
        "--start-time", "-s",
        default="00:00:00",
        help="Start seek timestamp for --file in HH:MM:SS (default: 00:00:00)",
    )
    parser.add_argument(
        "--force-reconvert",
        action="store_true",
        help="Force overwriting an existing .hls/ cache for --file",
    )
    parser.add_argument(
        "--delete-cache",
        metavar="PATH",
        help="Delete the cached .hls/ directory for a video file and exit",
    )
    parser.add_argument(
        "--window-id", "-w",
        default=None,
        help="Specific X11 window ID to capture in --mirror mode",
    )
    parser.add_argument(
        "--port", "-p",
        type=int,
        default=DEFAULT_HTTP_PORT,
        help=f"Local HTTP server port (default: {DEFAULT_HTTP_PORT})",
    )
    return parser


def run_cli(args: argparse.Namespace) -> int:
    cfg = HotelCastConfig.load()

    def _cli_log(msg: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"[{ts}] {msg}", flush=True)

    if args.delete_cache:
        if delete_video_hls_cache(args.delete_cache):
            _cli_log(f"Deleted HLS cache: {get_video_hls_cache_dir(args.delete_cache)}")
        else:
            _cli_log("No HLS cache found.")
        return 0

    if args.activate:
        ok, _ = activate_hotel_tv_bridge(
            args.activate,
            room_number=cfg.tv_name,
            on_log=_cli_log,
        )
        if ok:
            cfg.activation_url = expand_activation_url(args.activate, room_number=cfg.tv_name)
            cfg.save()
        return 0 if ok else 1

    if args.scan:
        devices = discover_devices(
            timeout=4.0,
            on_log=_cli_log,
            saved_ip=cfg.tv_ip,
            saved_port=cfg.tv_port,
            saved_name=cfg.tv_name,
        )
        if not devices:
            return 1
        for d in devices:
            print(f"  {d.display_name}")
        return 0

    if args.list_tracks:
        tracks, dur = probe_media_info(args.list_tracks)
        print(f"Duration: {format_timestamp(dur)} ({dur:.1f}s)")
        if not tracks:
            print("No audio tracks found.")
            return 0
        for t in tracks:
            print(f"  {t.display_label}")
        return 0

    if args.tv:
        target = parse_manual_target(args.tv, name=cfg.tv_name or None)
        cfg.tv_ip = target.ip
        cfg.tv_port = target.port
        cfg.save()
    elif cfg.tv_ip:
        target = DiscoveredDevice(
            name=cfg.tv_name or f"Saved TV ({cfg.tv_ip})",
            ip=cfg.tv_ip,
            port=cfg.tv_port,
            service_type="saved",
        )
    else:
        devices = discover_devices(timeout=4.0, on_log=_cli_log)
        if not devices:
            _cli_log("ERROR: No TV discovered via mDNS. Please pass --tv <IP:Port>.")
            return 1
        target = devices[0]

    session = HotelCastSession(http_port=args.port, config=cfg, on_log=_cli_log)

    stop_requested = threading.Event()

    def _sig_handler(_signum: int, _frame: object) -> None:
        _cli_log("Interrupt received; stopping HotelCast session...")
        stop_requested.set()

    signal.signal(signal.SIGINT, _sig_handler)
    signal.signal(signal.SIGTERM, _sig_handler)

    try:
        if args.file:
            tracks, dur = probe_media_info(args.file)
            audio_idx = (
                args.audio_track
                if args.audio_track is not None
                else select_preferred_audio_index(tracks, cfg.preferred_audio_lang)
            )
            session.start_session(
                tv_target=target,
                mode="file",
                file_path=args.file,
                audio_index=audio_idx,
                start_time=args.start_time,
                total_duration=dur,
                force_reconvert=args.force_reconvert,
            )
        elif args.url:
            session.start_session(
                tv_target=target,
                mode="url",
                web_url=args.url,
            )
        elif args.mirror:
            session.start_session(
                tv_target=target,
                mode="mirror",
                window_id=args.window_id,
            )

        _cli_log("Streaming active. Press Ctrl+C to stop.")
        while not stop_requested.is_set():
            if session.airplay and not session.airplay.is_connected:
                _cli_log("TV disconnected from AirPlay control channel.")
                break
            time.sleep(0.5)
    finally:
        session.stop_session(cleanup_files=True, stop_server=True)

    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if args.target_arg:
        parsed_task = parse_invocation_target(args.target_arg)
        if parsed_task:
            if args.tv:
                parsed_task["tv"] = args.tv
            forwarded = try_forward_to_running_instance(parsed_task, port=args.port)
            if forwarded is not None:
                print(f"Forwarded to running HotelCast instance: {forwarded}")
                return 0

            if os.environ.get("DISPLAY"):
                gui = HotelCastGUI(initial_port=args.port, startup_task=parsed_task)
                gui.run()
                return 0

            mode = parsed_task.get("mode")
            if mode == "file":
                args.file = parsed_task.get("file")
            elif mode == "url":
                args.url = parsed_task.get("url")
            elif mode == "mirror":
                args.mirror = True
            return run_cli(args)

    if any(
        [
            args.file,
            args.url,
            args.mirror,
            args.activate,
            args.delete_cache,
            args.scan,
            args.list_tracks,
        ]
    ):
        return run_cli(args)

    gui = HotelCastGUI(initial_port=args.port)
    gui.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
