"""
Media & Chrome Input Pipeline for HotelCast (`streamer.py`).

Provides:
- Persistent per-video HLS cache folder right next to local video files (`/path/to/MovieName.mkv.hls/`)
  with `meta.json` tracking file size, selected audio track, duration, and completion (`#EXT-X-ENDLIST`).
- Instant Resume / Zero Re-conversion when a local video's `.hls/` cache is already complete.
- One-click cache deletion (`delete_video_hls_cache`) and `[Force Re-convert]` support.
- Dynamic `seek_stream.m3u8` playlist generation starting at any target timestamp (`seek_seconds`),
  plus duration probing via `ffprobe`.
- Isolated `/tmp/hotelcast_hls/` workspace strictly for live Chrome URL streaming and screen mirroring.
"""

from __future__ import annotations

import glob
import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse


DEFAULT_HLS_DIR = "/tmp/hotelcast_hls"
META_FILENAME = "meta.json"


@dataclass
class AudioTrack:
    """Metadata for a single audio stream within a media container."""

    audio_index: int  # 0-based audio stream index for `-map 0:a:<audio_index>`
    stream_index: int  # Global container stream index
    codec: str
    language: str = "und"
    title: str = ""
    channels: int = 2
    channel_layout: str = ""

    @property
    def display_label(self) -> str:
        lang = (self.language or "und").upper()
        ch_str = f"{self.channels}ch" if self.channels else "stereo"
        base = f"Track {self.audio_index} [#{self.stream_index}]: {lang} ({self.codec}, {ch_str})"
        if self.title:
            base += f" — {self.title}"
        return base


@dataclass
class WindowInfo:
    """Represents a capturable X11 window (e.g., Chrome or full screen)."""

    window_id: Optional[str]
    title: str

    @property
    def display_label(self) -> str:
        if not self.window_id:
            return "Full Desktop Screen"
        return f"{self.title} [{self.window_id}]"


def get_video_hls_cache_dir(input_file: str) -> str:
    """Return the dedicated per-movie HLS cache directory `/path/to/MovieName.mkv.hls`."""
    return f"{os.path.abspath(os.path.expanduser(input_file))}.hls"


def prepare_hls_dir(hls_dir: str = DEFAULT_HLS_DIR) -> str:
    """Create `hls_dir` if missing and remove any old `.ts`, `.m3u8`, and `meta.json` files."""
    os.makedirs(hls_dir, exist_ok=True)
    cleanup_hls_dir(hls_dir)
    return hls_dir


def cleanup_hls_dir(hls_dir: str = DEFAULT_HLS_DIR) -> None:
    """Delete all `.ts`, `.m3u8`, `.tmp`, and `meta.json` files inside `hls_dir`."""
    if not os.path.isdir(hls_dir):
        return
    for pattern in ("*.ts", "*.m3u8", "*.tmp", "*.m4s", META_FILENAME):
        for filepath in glob.glob(os.path.join(hls_dir, pattern)):
            try:
                os.remove(filepath)
            except OSError:
                pass


def delete_video_hls_cache(input_file: str) -> bool:
    """
    Delete the persistent `/path/to/MovieName.mkv.hls/` cache directory to free disk space.
    Returns True if the cache folder existed and was removed.
    """
    cache_dir = get_video_hls_cache_dir(input_file)
    if os.path.isdir(cache_dir):
        shutil.rmtree(cache_dir, ignore_errors=True)
        return not os.path.exists(cache_dir)
    return False


def get_hls_cache_size_mb(input_file: str) -> float:
    """Return total disk space in MiB used by `/path/to/MovieName.mkv.hls/`."""
    cache_dir = get_video_hls_cache_dir(input_file)
    if not os.path.isdir(cache_dir):
        return 0.0
    total = 0
    try:
        for entry in os.scandir(cache_dir):
            if entry.is_file():
                total += entry.stat().st_size
    except OSError:
        pass
    return total / (1024.0 * 1024.0)


def read_hls_cache_meta(cache_dir: str) -> Optional[Dict[str, Any]]:
    """Read `meta.json` from `cache_dir` if present."""
    meta_path = os.path.join(cache_dir, META_FILENAME)
    if not os.path.isfile(meta_path):
        return None
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def write_hls_cache_meta(
    cache_dir: str,
    source_file: str,
    audio_index: int,
    duration: float = 0.0,
    completed: bool = False,
) -> None:
    """Write `meta.json` inside `cache_dir`."""
    try:
        os.makedirs(cache_dir, exist_ok=True)
        source_size = os.path.getsize(source_file) if os.path.isfile(source_file) else 0
        payload = {
            "source_file": os.path.abspath(source_file),
            "source_size": source_size,
            "audio_index": int(audio_index),
            "duration": float(duration),
            "completed": bool(completed),
            "updated_at": time.time(),
        }
        meta_path = os.path.join(cache_dir, META_FILENAME)
        tmp_path = f"{meta_path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp_path, meta_path)
    except OSError:
        pass


def get_playlist_duration(playlist_path: str) -> float:
    """Sum all `#EXTINF:<duration>` values in an HLS `.m3u8` playlist."""
    if not os.path.isfile(playlist_path):
        return 0.0
    try:
        with open(playlist_path, "r", encoding="utf-8", errors="ignore") as f:
            text = f.read()
        return sum(float(x) for x in re.findall(r"#EXTINF:([0-9.]+)", text))
    except (OSError, ValueError):
        return 0.0


def _strip_endlist_from_playlist(playlist_path: str) -> None:
    """
    Remove `#EXT-X-ENDLIST` from an incomplete `.m3u8` playlist (which `ffmpeg`
    writes when interrupted by `SIGTERM`) so a partial conversion is never
    mistaken for a completed movie cache.
    """
    if not os.path.isfile(playlist_path):
        return
    try:
        with open(playlist_path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
        filtered = [ln for ln in lines if ln.strip() != "#EXT-X-ENDLIST"]
        if len(filtered) != len(lines):
            tmp_path = f"{playlist_path}.tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.writelines(filtered)
            os.replace(tmp_path, playlist_path)
    except OSError:
        pass


def _finalize_vod_playlist(playlist_path: str) -> None:
    """
    Ensure a completed `.m3u8` cache declares `#EXT-X-PLAYLIST-TYPE:VOD` and `#EXT-X-ENDLIST`
    so Smart TVs / AirPlay receivers immediately display the full movie duration.
    """
    if not os.path.isfile(playlist_path):
        return
    try:
        with open(playlist_path, "r", encoding="utf-8", errors="ignore") as f:
            text = f.read()
        updated = text.replace("#EXT-X-PLAYLIST-TYPE:EVENT", "#EXT-X-PLAYLIST-TYPE:VOD")
        if "#EXT-X-ENDLIST" not in updated:
            if not updated.endswith("\n"):
                updated += "\n"
            updated += "#EXT-X-ENDLIST\n"
        if updated != text:
            tmp_path = f"{playlist_path}.tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.write(updated)
            os.replace(tmp_path, playlist_path)
    except OSError:
        pass


def is_hls_cache_complete(
    input_file: str,
    audio_index: int,
    cache_dir: Optional[str] = None,
) -> bool:
    """
    Return True if `<input_file>.hls/stream.m3u8` and `meta.json` exist with matching
    `source_size`, matching `audio_index`, `completed=True`, `#EXT-X-ENDLIST` present
    in `stream.m3u8`, at least one non-empty `.ts` segment on disk, and total playlist
    segment duration matching at least 95% of the source video's duration.
    """
    if not os.path.isfile(input_file):
        return False

    target_dir = cache_dir or get_video_hls_cache_dir(input_file)
    playlist_path = os.path.join(target_dir, "stream.m3u8")
    if not os.path.isfile(playlist_path):
        return False

    try:
        with open(playlist_path, "r", encoding="utf-8", errors="ignore") as f:
            playlist_text = f.read()
    except OSError:
        return False

    if "#EXT-X-ENDLIST" not in playlist_text:
        return False

    meta = read_hls_cache_meta(target_dir)
    if not meta:
        return False

    if not bool(meta.get("completed")):
        return False

    try:
        current_size = os.path.getsize(input_file)
    except OSError:
        return False

    if int(meta.get("source_size", -1)) != current_size:
        return False
    if int(meta.get("audio_index", -1)) != int(audio_index):
        return False

    ts_files = glob.glob(os.path.join(target_dir, "seg_*.ts"))
    if not ts_files:
        return False

    expected_duration = float(meta.get("duration", 0.0) or 0.0)
    if expected_duration > 10.0:
        actual_duration = get_playlist_duration(playlist_path)
        if actual_duration < expected_duration * 0.95:
            return False

    return True



def probe_media_info(file_path: str) -> Tuple[List[AudioTrack], float]:
    """
    Probe a video file using `ffprobe -v quiet -print_format json -show_format -show_streams`
    and return `(audio_tracks, total_duration_seconds)`.
    """
    if not os.path.isfile(file_path):
        raise FileNotFoundError(f"Media file not found: {file_path}")

    ffprobe_bin = shutil.which("ffprobe") or "ffprobe"
    cmd = [
        ffprobe_bin,
        "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        file_path,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15.0, check=False)
    if proc.returncode != 0 or not proc.stdout:
        return [], 0.0

    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return [], 0.0

    duration = 0.0
    fmt = data.get("format") or {}
    if fmt.get("duration"):
        try:
            duration = max(0.0, float(fmt["duration"]))
        except (ValueError, TypeError):
            pass

    tracks: List[AudioTrack] = []
    audio_idx = 0
    for stream in data.get("streams", []):
        if duration <= 0.0 and stream.get("duration"):
            try:
                duration = max(duration, float(stream["duration"]))
            except (ValueError, TypeError):
                pass

        if stream.get("codec_type") != "audio":
            continue
        tags = stream.get("tags") or {}
        language = (
            tags.get("language")
            or tags.get("LANGUAGE")
            or tags.get("lang")
            or "und"
        )
        title = tags.get("title") or tags.get("TITLE") or tags.get("handler_name") or ""
        tracks.append(
            AudioTrack(
                audio_index=audio_idx,
                stream_index=int(stream.get("index", audio_idx)),
                codec=str(stream.get("codec_name", "unknown")),
                language=str(language),
                title=str(title),
                channels=int(stream.get("channels", 2) or 2),
                channel_layout=str(stream.get("channel_layout", "")),
            )
        )
        audio_idx += 1

    return tracks, duration


def probe_audio_tracks(file_path: str) -> List[AudioTrack]:
    """Return available audio tracks in `file_path`."""
    tracks, _ = probe_media_info(file_path)
    return tracks


def probe_media_duration(file_path: str) -> float:
    """Return total duration of `file_path` in seconds."""
    _, duration = probe_media_info(file_path)
    return duration


def format_timestamp(seconds: float) -> str:
    """Format seconds (`float`) into `HH:MM:SS`."""
    total_sec = max(0, int(round(seconds)))
    hrs = total_sec // 3600
    mins = (total_sec % 3600) // 60
    secs = total_sec % 60
    return f"{hrs:02d}:{mins:02d}:{secs:02d}"


def parse_timestamp_to_seconds(ts: Optional[str]) -> float:
    """Parse `HH:MM:SS`, `MM:SS`, or `SS` string into seconds (`float`)."""
    if not ts or not ts.strip():
        return 0.0
    cleaned = ts.strip()
    parts = cleaned.split(":")
    try:
        if len(parts) == 3:
            return max(0.0, int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2]))
        if len(parts) == 2:
            return max(0.0, int(parts[0]) * 60 + float(parts[1]))
        return max(0.0, float(cleaned))
    except ValueError:
        return 0.0


def normalize_start_time(start_time: Optional[str]) -> Optional[str]:
    """Return normalized seek timestamp (`HH:MM:SS` or seconds) if non-zero, else None."""
    if not start_time:
        return None
    if parse_timestamp_to_seconds(start_time) <= 0.0:
        return None
    return start_time.strip()


def generate_seek_playlist(
    hls_dir: str,
    seek_seconds: float,
    source_playlist: str = "stream.m3u8",
    output_playlist: str = "seek_stream.m3u8",
) -> Tuple[str, float]:
    """
    Dynamically generate `seek_stream.m3u8` in `hls_dir` starting from the segment
    corresponding to `seek_seconds` so playback starts at that exact timestamp even
    on receivers that ignore `Start-Position`.
    Returns `(playlist_filename, actual_segment_start_seconds)`.
    """
    src_path = os.path.join(hls_dir, source_playlist)
    if seek_seconds <= 0.0 or not os.path.isfile(src_path):
        return source_playlist, 0.0

    try:
        with open(src_path, "r", encoding="utf-8", errors="ignore") as f:
            lines = [ln.strip() for ln in f.readlines() if ln.strip()]
    except OSError:
        return source_playlist, 0.0

    header_lines: List[str] = []
    segments: List[Tuple[float, str, str]] = []  # (duration, extinf_line, seg_uri)
    has_endlist = False

    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("#EXTINF:"):
            m = re.match(r"^#EXTINF:([0-9.]+)", line)
            seg_dur = float(m.group(1)) if m else 4.0
            seg_uri = lines[i + 1] if (i + 1) < len(lines) else ""
            segments.append((seg_dur, line, seg_uri))
            i += 2
            continue
        if line == "#EXT-X-ENDLIST":
            has_endlist = True
        elif not line.startswith("#EXT-X-MEDIA-SEQUENCE"):
            header_lines.append(line)
        i += 1

    if not segments:
        return source_playlist, 0.0

    # Locate the segment index containing seek_seconds
    accum = 0.0
    start_idx = 0
    for idx, (seg_dur, _, _) in enumerate(segments):
        if accum + seg_dur > seek_seconds or idx == len(segments) - 1:
            start_idx = idx
            break
        accum += seg_dur

    out_lines: List[str] = []
    for hl in header_lines:
        out_lines.append(hl)
    out_lines.append("#EXT-X-MEDIA-SEQUENCE:0")

    for _, extinf_line, seg_uri in segments[start_idx:]:
        out_lines.append(extinf_line)
        out_lines.append(seg_uri)

    if has_endlist:
        out_lines.append("#EXT-X-ENDLIST")

    dst_path = os.path.join(hls_dir, output_playlist)
    try:
        with open(dst_path, "w", encoding="utf-8") as f:
            f.write("\n".join(out_lines) + "\n")
        return output_playlist, accum
    except OSError:
        return source_playlist, 0.0


def build_local_file_cmd(
    input_file: str,
    audio_index: int = 0,
    start_time: Optional[str] = None,
    hls_dir: str = DEFAULT_HLS_DIR,
    has_audio: bool = True,
    hls_time: int = 4,
) -> List[str]:
    """Construct the Mode A `ffmpeg` command for instant-start local file remuxing."""
    ffmpeg_bin = shutil.which("ffmpeg") or "ffmpeg"
    cmd = [ffmpeg_bin, "-y"]

    seek = normalize_start_time(start_time)
    if seek:
        cmd.extend(["-ss", seek])

    cmd.extend(["-i", input_file])
    cmd.extend(["-map", "0:v:0"])
    if has_audio:
        cmd.extend(["-map", f"0:a:{max(0, int(audio_index))}"])

    cmd.extend(["-c:v", "copy"])
    if has_audio:
        cmd.extend(["-c:a", "aac", "-ac", "2", "-b:a", "192k"])

    seg_pattern = os.path.join(hls_dir, "seg_%05d.ts")
    playlist_path = os.path.join(hls_dir, "stream.m3u8")

    cmd.extend([
        "-f", "hls",
        "-hls_time", str(hls_time),
        "-hls_playlist_type", "event",
        "-hls_flags", "independent_segments",
        "-hls_segment_filename", seg_pattern,
        playlist_path,
    ])
    return cmd


def is_direct_media_url(url: str) -> bool:
    """Return True if the URL points directly to an `.m3u8`, `.mp4`, `.ts`, `.mkv`, or `.webm` resource."""
    try:
        parsed = urlparse(url.strip())
        path_lower = parsed.path.lower()
        return any(
            path_lower.endswith(ext)
            for ext in (".m3u8", ".mp4", ".m4v", ".mov", ".mkv", ".webm", ".ts", ".mpd")
        )
    except Exception:
        return False


def resolve_web_url_with_ytdlp(
    url: str,
    timeout: float = 20.0,
    on_log: Optional[Callable[[str], None]] = None,
) -> List[str]:
    """Resolve a web page URL into direct stream URL(s) via `yt-dlp -g`."""
    ytdlp_bin = shutil.which("yt-dlp")
    if not ytdlp_bin:
        if on_log:
            on_log("yt-dlp not found on PATH; attempting direct ffmpeg stream...")
        return []

    if on_log:
        on_log(f"Resolving stream URL via yt-dlp -g: {url}")

    try:
        proc = subprocess.run(
            [ytdlp_bin, "--no-playlist", "-f", "best[ext=mp4]/best", "-g", url],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
            if lines:
                return lines
    except (subprocess.TimeoutExpired, OSError) as exc:
        if on_log:
            on_log(f"yt-dlp -g resolution warning: {exc}")

    return []


def build_web_url_cmd(
    stream_inputs: List[str],
    hls_dir: str = DEFAULT_HLS_DIR,
    hls_time: int = 4,
    hls_list_size: int = 8,
) -> List[str]:
    """Construct the Mode B `ffmpeg` command for Chrome web video URL streaming."""
    ffmpeg_bin = shutil.which("ffmpeg") or "ffmpeg"
    cmd = [ffmpeg_bin, "-y"]

    for inp in stream_inputs:
        cmd.extend(["-i", inp])

    if len(stream_inputs) >= 2:
        cmd.extend(["-map", "0:v:0", "-map", "1:a:0"])

    cmd.extend([
        "-c:v", "copy",
        "-c:a", "aac", "-ac", "2", "-b:a", "192k",
        "-f", "hls",
        "-hls_time", str(hls_time),
        "-hls_list_size", str(hls_list_size),
        "-hls_flags", "delete_segments+append_list",
        "-hls_segment_filename", os.path.join(hls_dir, "seg_%05d.ts"),
        os.path.join(hls_dir, "stream.m3u8"),
    ])
    return cmd


def get_default_pulse_monitor() -> Optional[str]:
    """Detect the active PulseAudio / PipeWire monitor sink via `pactl get-default-sink`."""
    pactl_bin = shutil.which("pactl")
    if not pactl_bin:
        return None

    try:
        proc = subprocess.run(
            [pactl_bin, "get-default-sink"],
            capture_output=True,
            text=True,
            timeout=2.5,
            check=False,
        )
        sink = proc.stdout.strip()
        if proc.returncode == 0 and sink:
            return f"{sink}.monitor"
    except (subprocess.TimeoutExpired, OSError):
        pass

    try:
        proc = subprocess.run(
            [pactl_bin, "info"],
            capture_output=True,
            text=True,
            timeout=2.5,
            check=False,
        )
        for line in proc.stdout.splitlines():
            if line.lower().startswith("default sink:"):
                sink = line.split(":", 1)[1].strip()
                if sink:
                    return f"{sink}.monitor"
    except (subprocess.TimeoutExpired, OSError):
        pass

    return None


def get_display_env() -> Tuple[str, str]:
    """Return `(session_type, display)` from `$XDG_SESSION_TYPE` and `$DISPLAY`."""
    session_type = os.environ.get("XDG_SESSION_TYPE", "x11").strip().lower() or "x11"
    display = os.environ.get("DISPLAY", ":0").strip() or ":0"
    return session_type, display


def list_chrome_windows() -> List[WindowInfo]:
    """Return a list of capturable targets starting with Full Screen, followed by open Chrome windows."""
    windows: List[WindowInfo] = [WindowInfo(window_id=None, title="Full Desktop Screen")]
    seen_ids: set[str] = set()

    xdotool_bin = shutil.which("xdotool")
    if xdotool_bin:
        try:
            proc = subprocess.run(
                [xdotool_bin, "search", "--onlyvisible", "--name", "."],
                capture_output=True,
                text=True,
                timeout=2.5,
                check=False,
            )
            if proc.returncode == 0 and proc.stdout:
                for wid in proc.stdout.splitlines():
                    wid = wid.strip()
                    if not wid:
                        continue
                    tproc = subprocess.run(
                        [xdotool_bin, "getwindowname", wid],
                        capture_output=True,
                        text=True,
                        timeout=1.0,
                        check=False,
                    )
                    title = (tproc.stdout or "").strip()
                    if title and any(k in title.lower() for k in ("chrome", "chromium", "youtube", "video", "mozilla", "firefox")):
                        hex_id = hex(int(wid)) if wid.isdigit() else wid
                        if hex_id not in seen_ids:
                            seen_ids.add(hex_id)
                            windows.append(WindowInfo(window_id=hex_id, title=title))
        except (subprocess.TimeoutExpired, OSError, ValueError):
            pass

    xwininfo_bin = shutil.which("xwininfo")
    if xwininfo_bin:
        try:
            proc = subprocess.run(
                [xwininfo_bin, "-root", "-tree"],
                capture_output=True,
                text=True,
                timeout=2.5,
                check=False,
            )
            if proc.returncode == 0 and proc.stdout:
                line_re = re.compile(
                    r'^\s*(0x[0-9a-fA-F]+)\s+"([^"]+)":\s*\(([^)]*)\)\s+(\d+)x(\d+)'
                )
                other_windows: List[WindowInfo] = []
                for line in proc.stdout.splitlines():
                    m = line_re.match(line)
                    if not m:
                        continue
                    wid, title, wm_class, w_str, h_str = m.groups()
                    w, h = int(w_str), int(h_str)
                    if w < 200 or h < 150 or wid in seen_ids:
                        continue
                    combined = f"{title} {wm_class}".lower()
                    if any(k in combined for k in ("chrome", "chromium", "brave", "firefox")):
                        seen_ids.add(wid)
                        windows.append(WindowInfo(window_id=wid, title=title))
                    elif len(other_windows) < 8 and title not in ("Desktop", "gnome-shell"):
                        other_windows.append(WindowInfo(window_id=wid, title=title))

                for ow in other_windows:
                    if ow.window_id and ow.window_id not in seen_ids:
                        seen_ids.add(ow.window_id)
                        windows.append(ow)
        except (subprocess.TimeoutExpired, OSError):
            pass

    return windows


def build_mirror_cmd(
    hls_dir: str = DEFAULT_HLS_DIR,
    display: Optional[str] = None,
    pulse_monitor: Optional[str] = None,
    window_id: Optional[str] = None,
    framerate: int = 25,
) -> List[str]:
    """Construct the Mode C `ffmpeg` command for live Chrome / desktop screen + audio capture."""
    ffmpeg_bin = shutil.which("ffmpeg") or "ffmpeg"
    _, env_display = get_display_env()
    target_display = display or env_display
    monitor_sink = pulse_monitor if pulse_monitor is not None else get_default_pulse_monitor()

    cmd = [
        ffmpeg_bin, "-y",
        "-f", "x11grab",
        "-framerate", str(framerate),
    ]
    if window_id and window_id.strip():
        cmd.extend(["-window_id", window_id.strip()])

    cmd.extend(["-i", target_display])

    if monitor_sink:
        cmd.extend(["-f", "pulse", "-i", monitor_sink])
    else:
        cmd.extend(["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"])

    cmd.extend([
        "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-tune", "zerolatency",
        "-pix_fmt", "yuv420p",
        "-g", str(framerate * 2),
        "-c:a", "aac",
        "-ac", "2",
        "-f", "hls",
        "-hls_time", "2",
        "-hls_list_size", "5",
        "-hls_flags", "delete_segments",
        "-hls_segment_filename", os.path.join(hls_dir, "seg_%05d.ts"),
        os.path.join(hls_dir, "stream.m3u8"),
    ])
    return cmd


class HLSStreamer:
    """
    Manages the `ffmpeg` (and optional `yt-dlp`) subprocess lifecycle, per-movie `.hls/`
    caching, and live stderr parsing.
    """

    def __init__(
        self,
        hls_dir: str = DEFAULT_HLS_DIR,
        on_log: Optional[Callable[[str], None]] = None,
        on_cache_complete: Optional[Callable[[str], None]] = None,
        on_progress: Optional[Callable[[float, float, str], None]] = None,
    ) -> None:
        self.default_hls_dir = hls_dir
        self.active_hls_dir = hls_dir
        self.on_log = on_log
        self.on_cache_complete = on_cache_complete
        self.on_progress = on_progress
        self._ffmpeg_proc: Optional[subprocess.Popen] = None
        self._ytdlp_proc: Optional[subprocess.Popen] = None
        self._monitor_thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self.last_speed: str = ""
        self.last_cmd: List[str] = []
        self._current_source_file: Optional[str] = None
        self._current_audio_index: int = 0
        self._current_duration: float = 0.0
        self._stopped_by_user: bool = False
        self._started_from_zero: bool = True

    def _log(self, msg: str) -> None:
        if self.on_log:
            self.on_log(msg)

    def is_running(self) -> bool:
        with self._lock:
            return self._ffmpeg_proc is not None and self._ffmpeg_proc.poll() is None

    def returncode(self) -> Optional[int]:
        with self._lock:
            if self._ffmpeg_proc is None:
                return None
            return self._ffmpeg_proc.poll()

    def can_reuse_running_conversion(
        self,
        input_file: str,
        audio_index: int = 0,
        seek_seconds: float = 0.0,
        custom_cache_dir: Optional[str] = None,
    ) -> bool:
        """
        Return True if `ffmpeg` is currently running and converting the exact same
        `input_file` and `audio_index` from `00:00:00`, and `seek_seconds` is already
        covered by the segments written so far.
        """
        if not self.is_running() or not self._current_source_file or not self._started_from_zero:
            return False
        if os.path.abspath(input_file) != os.path.abspath(self._current_source_file):
            return False
        if int(audio_index) != int(self._current_audio_index):
            return False
        target_dir = custom_cache_dir or get_video_hls_cache_dir(input_file)
        if os.path.abspath(target_dir) != os.path.abspath(self.active_hls_dir):
            return False
        if seek_seconds <= 0.0:
            return True
        playlist_path = os.path.join(target_dir, "stream.m3u8")
        converted_dur = get_playlist_duration(playlist_path)
        return converted_dur >= (seek_seconds + 8.0)

    def start_local_file(
        self,
        input_file: str,
        audio_index: int = 0,
        start_time: Optional[str] = None,
        hls_time: int = 4,
        force_reconvert: bool = False,
        custom_cache_dir: Optional[str] = None,
    ) -> Tuple[str, bool]:
        """
        Start Mode A: Local video file instant-start HLS remux with per-movie `.hls/` caching.
        Returns `(active_hls_dir, used_existing_cache)`.
        """
        tracks, duration = probe_media_info(input_file)
        has_audio = len(tracks) > 0
        if has_audio and audio_index >= len(tracks):
            audio_index = 0

        cache_dir = custom_cache_dir or get_video_hls_cache_dir(input_file)
        seek_sec = parse_timestamp_to_seconds(start_time)

        if not force_reconvert and self.can_reuse_running_conversion(
            input_file, audio_index, seek_seconds=seek_sec, custom_cache_dir=cache_dir
        ):
            self._log(
                f"Continuing active background HLS conversion in {cache_dir} without interruption."
            )
            return cache_dir, False

        self.stop(cleanup_temp=False)

        self.active_hls_dir = cache_dir
        self._current_source_file = input_file
        self._current_audio_index = audio_index
        self._current_duration = duration
        self._stopped_by_user = False

        if not force_reconvert and is_hls_cache_complete(input_file, audio_index, cache_dir=cache_dir):
            self._log(
                f"Found complete HLS cache in {cache_dir} (audio track {audio_index}, "
                f"full duration {format_timestamp(duration)}) — skipping ffmpeg conversion!"
            )
            return cache_dir, True

        # Overwrite incomplete or forced cache directory
        prepare_hls_dir(cache_dir)
        write_hls_cache_meta(
            cache_dir,
            source_file=input_file,
            audio_index=audio_index,
            duration=duration,
            completed=False,
        )

        norm_start = normalize_start_time(start_time)
        self._started_from_zero = norm_start is None

        # Note: When building a reusable full-movie cache, remux from 00:00:00 unless
        # start_time is requested for immediate uncached jump
        cmd = build_local_file_cmd(
            input_file=input_file,
            audio_index=audio_index,
            start_time=norm_start,
            hls_dir=cache_dir,
            has_audio=has_audio,
            hls_time=hls_time,
        )
        self._log(f"Starting Local File HLS Remux into {cache_dir}: {' '.join(cmd)}")
        self._spawn_ffmpeg(cmd)
        return cache_dir, False

    def start_web_url(self, url: str) -> List[str]:
        """Start Mode B: Chrome Web Video URL to rolling live HLS in `/tmp/hotelcast_hls/`."""
        self.stop(cleanup_temp=True)
        self.active_hls_dir = self.default_hls_dir
        self._current_source_file = None
        self._stopped_by_user = False
        prepare_hls_dir(self.default_hls_dir)

        cleaned_url = url.strip()
        if not cleaned_url:
            raise ValueError("Web URL cannot be empty.")

        if is_direct_media_url(cleaned_url):
            cmd = build_web_url_cmd([cleaned_url], hls_dir=self.default_hls_dir)
            self._log(f"Starting Direct Web Stream HLS: {' '.join(cmd)}")
            self._spawn_ffmpeg(cmd)
            return cmd

        resolved = resolve_web_url_with_ytdlp(cleaned_url, on_log=self.on_log)
        if resolved:
            cmd = build_web_url_cmd(resolved, hls_dir=self.default_hls_dir)
            self._log(f"Starting yt-dlp Resolved Stream HLS: {' '.join(cmd)}")
            self._spawn_ffmpeg(cmd)
            return cmd

        ytdlp_bin = shutil.which("yt-dlp")
        if ytdlp_bin:
            ytdlp_cmd = [ytdlp_bin, "--no-playlist", "-o", "-", cleaned_url]
            ffmpeg_cmd = build_web_url_cmd(["pipe:0"], hls_dir=self.default_hls_dir)
            self._log(f"Starting piped yt-dlp -> ffmpeg HLS stream for {cleaned_url}")
            with self._lock:
                self._ytdlp_proc = subprocess.Popen(
                    ytdlp_cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                )
                self._ffmpeg_proc = subprocess.Popen(
                    ffmpeg_cmd,
                    stdin=self._ytdlp_proc.stdout,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                )
                if self._ytdlp_proc.stdout:
                    self._ytdlp_proc.stdout.close()
                self.last_cmd = ffmpeg_cmd
            self._start_stderr_monitor()
            return ffmpeg_cmd

        cmd = build_web_url_cmd([cleaned_url], hls_dir=self.default_hls_dir)
        self._log(f"Starting Web Stream HLS: {' '.join(cmd)}")
        self._spawn_ffmpeg(cmd)
        return cmd

    def start_mirror(
        self,
        window_id: Optional[str] = None,
        display: Optional[str] = None,
        pulse_monitor: Optional[str] = None,
        framerate: int = 25,
    ) -> List[str]:
        """Start Mode C: Live Chrome window or desktop screen + audio mirroring in `/tmp/hotelcast_hls/`."""
        self.stop(cleanup_temp=True)
        self.active_hls_dir = self.default_hls_dir
        self._current_source_file = None
        self._stopped_by_user = False
        prepare_hls_dir(self.default_hls_dir)

        session_type, env_display = get_display_env()
        if session_type == "wayland":
            self._log(
                f"Detected Wayland session ($XDG_SESSION_TYPE=wayland); capturing via XWayland on {display or env_display}"
            )

        cmd = build_mirror_cmd(
            hls_dir=self.default_hls_dir,
            display=display,
            pulse_monitor=pulse_monitor,
            window_id=window_id,
            framerate=framerate,
        )
        self._log(f"Starting Screen Mirror HLS: {' '.join(cmd)}")
        self._spawn_ffmpeg(cmd)
        return cmd

    def _spawn_ffmpeg(self, cmd: List[str]) -> None:
        with self._lock:
            self._stopped_by_user = False
            self.last_cmd = cmd
            self._ffmpeg_proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        self._start_stderr_monitor()

    def _start_stderr_monitor(self) -> None:
        self._monitor_thread = threading.Thread(
            target=self._read_ffmpeg_stderr,
            name="hotelcast-ffmpeg-monitor",
            daemon=True,
        )
        self._monitor_thread.start()

    def _read_ffmpeg_stderr(self) -> None:
        proc = self._ffmpeg_proc
        if not proc or not proc.stderr:
            return

        src_file = self._current_source_file
        cache_dir = self.active_hls_dir
        audio_idx = self._current_audio_index
        total_dur = self._current_duration
        started_zero = self._started_from_zero

        buf = bytearray()
        last_log_time = 0.0
        speed_re = re.compile(r"speed=\s*([0-9.]+x)")
        time_re = re.compile(r"time=\s*([0-9:.]+)")

        try:
            while True:
                chunk = proc.stderr.read(256)
                if not chunk:
                    break
                buf.extend(chunk)
                while True:
                    idx_r = buf.find(b"\r")
                    idx_n = buf.find(b"\n")
                    if idx_r == -1 and idx_n == -1:
                        break
                    if idx_r != -1 and (idx_n == -1 or idx_r < idx_n):
                        split_idx = idx_r
                    else:
                        split_idx = idx_n

                    raw_line = bytes(buf[:split_idx]).decode("utf-8", errors="replace").strip()
                    del buf[: split_idx + 1]

                    if not raw_line:
                        continue

                    if "speed=" in raw_line or "time=" in raw_line:
                        sm = speed_re.search(raw_line)
                        tm = time_re.search(raw_line)
                        if sm:
                            self.last_speed = sm.group(1)
                        now = time.monotonic()
                        if now - last_log_time >= 1.5:
                            last_log_time = now
                            t_val = tm.group(1) if tm else "?"
                            s_val = sm.group(1) if sm else "?"
                            self._log(f"[ffmpeg] position={t_val} speed={s_val}")
                            if callable(self.on_progress) and tm:
                                try:
                                    pos_sec = parse_timestamp_to_seconds(t_val)
                                    self.on_progress(pos_sec, total_dur, s_val)
                                except Exception:
                                    pass
                    elif any(err in raw_line.lower() for err in ("error", "failed", "invalid", "permission denied")):
                        self._log(f"[ffmpeg] {raw_line}")
        except Exception:
            pass
        finally:
            try:
                if proc.stderr:
                    proc.stderr.close()
            except Exception:
                pass
            try:
                proc.wait(timeout=2.0)
            except Exception:
                pass

            # Verify whether local file conversion truly completed the ENTIRE movie
            if src_file and cache_dir != self.default_hls_dir:
                playlist_path = os.path.join(cache_dir, "stream.m3u8")
                if os.path.isfile(playlist_path):
                    try:
                        with open(playlist_path, "r", encoding="utf-8", errors="ignore") as f:
                            has_endlist = "#EXT-X-ENDLIST" in f.read()
                        actual_dur = get_playlist_duration(playlist_path)
                        duration_ok = (total_dur <= 10.0) or (actual_dur >= total_dur * 0.95)
                        if (
                            not self._stopped_by_user
                            and proc.returncode == 0
                            and started_zero
                            and has_endlist
                            and duration_ok
                        ):
                            _finalize_vod_playlist(playlist_path)
                            write_hls_cache_meta(
                                cache_dir,
                                source_file=src_file,
                                audio_index=audio_idx,
                                duration=total_dur,
                                completed=True,
                            )
                            self._log(
                                f"Local HLS cache 100% completed ({format_timestamp(actual_dur)}) and saved in {cache_dir}"
                            )
                            if callable(self.on_cache_complete):
                                self.on_cache_complete(cache_dir)
                        else:
                            # Interrupted or partial conversion: strip #EXT-X-ENDLIST so it is never treated as complete
                            _strip_endlist_from_playlist(playlist_path)
                            write_hls_cache_meta(
                                cache_dir,
                                source_file=src_file,
                                audio_index=audio_idx,
                                duration=total_dur,
                                completed=False,
                            )
                    except OSError:
                        pass

    def stop(self, cleanup: bool = False, cleanup_temp: bool = True) -> None:
        """
        Terminate running `ffmpeg` and `yt-dlp` subprocesses.
        Only cleans up `/tmp/hotelcast_hls/` — never deletes persistent `.mkv.hls/` folders!
        """
        with self._lock:
            self._stopped_by_user = True
            procs = [p for p in (self._ffmpeg_proc, self._ytdlp_proc) if p is not None]
            self._ffmpeg_proc = None
            self._ytdlp_proc = None

        for proc in procs:
            if proc.poll() is None:
                try:
                    proc.send_signal(signal.SIGTERM)
                except OSError:
                    pass

        for proc in procs:
            if proc.poll() is None:
                try:
                    proc.wait(timeout=2.5)
                except subprocess.TimeoutExpired:
                    try:
                        proc.kill()
                        proc.wait(timeout=1.0)
                    except OSError:
                        pass
            for pipe in (proc.stdin, proc.stdout, proc.stderr):
                if pipe is not None:
                    try:
                        pipe.close()
                    except Exception:
                        pass

        # If a local file cache was still being built when stopped, ensure #EXT-X-ENDLIST is stripped
        if self._current_source_file and self.active_hls_dir != self.default_hls_dir:
            playlist_path = os.path.join(self.active_hls_dir, "stream.m3u8")
            if not is_hls_cache_complete(
                self._current_source_file, self._current_audio_index, cache_dir=self.active_hls_dir
            ):
                _strip_endlist_from_playlist(playlist_path)

        if cleanup or cleanup_temp:
            cleanup_hls_dir(self.default_hls_dir)

