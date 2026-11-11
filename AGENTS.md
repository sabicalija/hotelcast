# AGENTS.md — HotelCast Developer & AI Agent Guide

This document describes the current implementation state, architecture, protocol invariants, and operational rules of **HotelCast** for coding agents and contributors picking up this repository.

---

## 1. Purpose & Current State

**HotelCast** is a complete, production-ready Linux desktop + CLI + Chrome Extension streaming suite designed to cast media to **AirPlay v1**, **HappyCast (乐播投屏)**, and **LeLink** receivers (commonly found on smart/hotel TVs on TCP port `52266` or standard AirPlay port `7000`).

Unlike generic AirPlay scripts, HotelCast specifically handles hostile hotel network setups:
1. **Active VPN / Proxy Coexistence:** Works while WireGuard (`wg-quick`), OpenVPN, Tailscale, or Mihomo/Clash tunnels are active by detecting the physical wireless NIC, pinning sockets, and injecting `/24` LAN bypass routes.
2. **Cross-VLAN / Blocked mDNS Networks:** Supports fast `0.5s` unicast TCP reachability checks on saved TV targets when hotel routers drop multicast mDNS across guest and TV subnets.
3. **Hotel TV Room-Binding Portals:** Automates QR-code / web portal handshakes (such as `ovk.top` / HappyCast router bridges returning `绑定电视成功`) via proxy-bypassing, Wi-Fi-pinned HTTP/HTTPS requests.
4. **Three Input Pipelines:** Local video files (with persistent per-movie `.hls/` VOD caching and instant seeking), web video URLs (direct or via `yt-dlp`), and live X11/XWayland desktop or Chrome window + PulseAudio/PipeWire audio mirroring.

### Current Status
- All 7 Python modules, the Manifest V3 Chrome Extension, the installer (`install.sh`), and the offline end-to-end test suite (`tests/test_pipeline.py`) are implemented and passing.
- Core functionality relies strictly on the **Python 3 standard library** + standard Linux system binaries (`ffmpeg`, `ffprobe`, `ip`, `avahi-browse`, `pactl`, `xwininfo`/`xdotool`, `python3-tk`). `yt-dlp` and `zeroconf` in `requirements.txt` are optional enhancements.
- **No room-specific or private defaults are hardcoded.** Fresh installs start with empty `tv_ip`, `tv_name`, and `activation_url` fields in `~/.config/hotelcast/config.json` and trigger mDNS discovery automatically unless the user configures/saves a target.

---

## 2. Repository & Runtime Layout

```text
.
├── app.py               # Entry point: Tkinter ttk GUI, CLI parser, HotelCastSession orchestrator, IPC forwarder
├── config.py            # Atomic JSON persistence in ~/.config/hotelcast/config.json & ISO-639 audio track selection
├── network.py           # Physical Wi-Fi detection, VPN /24 route bypass, socket pinning, portal bridge activation
├── discovery.py         # Avahi / Zeroconf mDNS scanning, TXT port parsing, saved-TV unicast probe, UFW check
├── streamer.py          # ffprobe inspection, FFmpeg pipelines (File / URL / Mirror), per-movie .hls/ VOD cache & seek slicing
├── server.py            # ThreadingHTTPServer (HTTP 206 Range, dynamic dir switching, /api/cast, /api/stop, /api/status)
├── airplay.py           # AirPlay v1 / HappyCast HTTP/1.1 controller (persistent keep-alive /play socket, /playback-info, /stop)
├── chrome_extension/    # Manifest V3 Chrome extension (one-click Cast URL / Mirror Window + hotelcast:// URI fallback)
├── tests/
│   └── test_pipeline.py # Offline E2E + unit test suite (synthetic ffmpeg video + MockAirPlayReceiver on 127.0.0.1)
├── install.sh           # Installs/updates ~/hotelcast (.venv, ~/.local/bin/hotelcast, hotelcast.desktop)
└── requirements.txt     # Optional Python packages (zeroconf, yt-dlp)
```

### Runtime Directories Outside the Repo
- **`~/hotelcast/`**: Default runtime install target created by `./install.sh` (contains `.venv/`). Note: `install.sh` safely supports cloning the repo *directly* into `~/hotelcast` (`SRC_DIR == DEST_DIR`) or running from a separate workspace directory and syncing into `~/hotelcast`.
- **`~/.config/hotelcast/config.json`**: User-local persistent settings. **Never commit personal IPs, room numbers, tokens, or directory paths into `config.py` defaults.**
- **`/tmp/hotelcast_hls/`**: Ephemeral rolling HLS segment directory used *only* for Mode B (Web URL) and Mode C (Screen/Window Mirroring). Cleaned up automatically when sessions stop.
- **`/path/to/<VideoFile>.hls/`**: Persistent VOD cache created beside local video files in Mode A (`stream.m3u8`, `seg_00000.ts`..., `meta.json`, and optional `seek_stream.m3u8`). **Never auto-delete `<VideoFile>.hls/` directories during normal session teardown** — only delete when the user explicitly clicks `[Delete Cached HLS]` or passes `--delete-cache`.

---

## 3. Critical Protocol & Engineering Invariants (Read Before Modifying)

### A. AirPlay v1 / HappyCast Keep-Alive Socket (`airplay.py`)
1. **Primary `/play` socket MUST remain open:** HappyCast and AirPlay v1 receivers tie the playback lifecycle to the TCP connection that sent `POST /play`. If that socket closes, the TV immediately exits playback back to the home screen. `AirPlayController` keeps `self._conn` open with `Connection: keep-alive` and OS TCP keep-alive probes in a daemon thread (`_keepalive_loop`).
2. **Secondary sockets for polling & teardown:** `GET /playback-info` and `POST /stop` must open separate short-lived connections rather than reusing `self._conn` (only falling back to `self._conn` for `/stop` if opening a new socket fails).
3. **`Start-Position` is a `[0.0, 0.99]` fraction, NOT seconds:** In `Content-Type: text/parameters`, `Start-Position` represents `seek_seconds / total_duration` clamped to `[0.0, 0.99]` (`compute_start_fraction`). Passing raw seconds (> 1.0) causes receivers to jump to EOF or reject `/play`.
4. **Pre-play segment readiness:** Never send `POST /play` before `stream.m3u8` exists and at least 2 non-empty `.ts` segments are written (`wait_for_hls_ready`), or the TV HTTP client will 404/stall on initial buffer.

### B. FFmpeg HLS Remuxing, SIGTERM Quirk & Dual Seeking (`streamer.py`)
1. **Zero video re-encoding for local files:** Mode A uses `-c:v copy -c:a aac -ac 2 -b:a 192k` so 1080p/4K H.264/HEVC files remux at 20x–80x realtime with negligible CPU load while ensuring stereo AAC compatibility (many TVs fail on raw 5.1 EAC3/DTS/TrueHD in MPEG-TS).
2. **FFmpeg `SIGTERM` `#EXT-X-ENDLIST` trap:** When `ffmpeg` runs with `-hls_playlist_type event` and is stopped mid-movie via `SIGTERM`, `ffmpeg` writes `#EXT-X-ENDLIST` before exiting! Therefore, `#EXT-X-ENDLIST` alone is **not** proof that a movie finished converting. `is_hls_cache_complete()` checks *all* of:
   - `meta.json` has `completed == True`, matching `source_size`, and matching `audio_index`
   - `#EXT-X-ENDLIST` is present in `stream.m3u8`
   - Summed `#EXTINF` duration in `stream.m3u8` is `>= 95%` of `ffprobe` duration (for videos > 10s)
   When stopped early, `_strip_endlist_from_playlist()` removes `#EXT-X-ENDLIST` and marks `completed=False`. When 100% finished, `_finalize_vod_playlist()` upgrades `#EXT-X-PLAYLIST-TYPE:EVENT` to `#EXT-X-PLAYLIST-TYPE:VOD` so the TV displays the full timeline immediately.
3. **Dual Seeking (`Start-Position` + `seek_stream.m3u8`):** Firmware behavior varies across TVs — Apple AirPlay honors `Start-Position`, whereas several HappyCast/LeBo builds ignore `Start-Position` and always start at segment 0 of the `.m3u8`. When seeking into a complete or sufficiently buffered `.hls/` cache, `HotelCastSession.start_session` supplies *both* `Start-Position` and `generate_seek_playlist()` (`seek_stream.m3u8` sliced from the target segment onward).

### C. VPN Bypass & Physical Wi-Fi Pinning (`network.py`)
1. **Never rely solely on `socket.connect` dummy UDP routing to find the LAN IP:** When WireGuard (`wg-quick`) or a TUN VPN is active, a default UDP route lookup returns the tunnel interface IP (`10.x.x.x`), giving the TV an unreachable `Content-Location: http://10.x.x.x:8000/stream.m3u8`.
2. **Physical Wi-Fi discovery (`find_physical_wifi_interface`):** Inspects `/sys/class/net/<iface>/wireless` and `phy80211`, explicitly excluding `VPN_PREFIXES` (`wg*`, `tun*`, `tailscale*`, `mihomo*`, `clash*`, etc.) and `VIRTUAL_PREFIXES` (`lo`, `docker*`, `veth*`, `br-*`).
3. **WireGuard `suppress_prefixlength 0` bypass (`ensure_vpn_bypass_route`):** When a TV lives on a routed hotel VLAN (e.g., guest Wi-Fi on `192.168.10.0/24`, TV VLAN on `192.168.20.0/24`), WireGuard captures traffic to the TV's subnet because the Wi-Fi NIC only has a `default` (`/0`) route for it. `ensure_vpn_bypass_route` checks `has_explicit_wifi_route` and injects `ip route replace <target_subnet>/24 via <wifi_gw> dev <wifi_iface> src <wifi_ip>` (attempting non-interactive `sudo -n`, interactive `sudo`, or GUI `pkexec`, and logging the exact manual command if unprivileged).
4. **Loopback exemption:** Whenever `target_ip` is `127.x.x.x` or `localhost` (such as in `tests/test_pipeline.py`), bypass Wi-Fi socket pinning and route injection completely so offline local tests run without root or Wi-Fi hardware.

### D. HTTP Server, Control API & GUI Threading (`server.py`, `app.py`)
1. **HTTP `206 Partial Content` & Disconnect Suppression:** TV media players issue byte-range probes (`Range: bytes=0-...`) and abruptly reset TCP sockets while seeking. `HLSRequestHandler` must support `206` / `416` responses and silently swallow `BrokenPipeError`, `ConnectionResetError`, and `TimeoutError`.
2. **Single-Instance IPC:** `app.py` listens on `0.0.0.0:8000` (auto-incrementing if busy) and exposes `POST /api/cast`, `POST /api/stop`, and `GET /api/status` (with permissive CORS headers for the Chrome Extension). Launching `hotelcast <file_or_uri>` checks `127.0.0.1:8000..8004` via `try_forward_to_running_instance` before creating a new GUI window.
3. **Tkinter Thread Safety:** Never update `tk`/`ttk` widgets directly from HTTP handler, `ffmpeg` stderr monitor, mDNS discovery, or AirPlay keep-alive threads. Always schedule UI updates via `self.root.after(0, ...)` and log via `self.enqueue_log(...)`.

---

## 4. Development & Verification Workflow

### Running the Test Suite
Always run the offline end-to-end test suite after modifying any Python module:

```bash
python3 -m unittest discover -v -s tests
```

What `tests/test_pipeline.py` verifies (completely offline on `127.0.0.1`, ~3 seconds):
- Synthetic dual-audio (`eng` + `ger`) H.264 video generation via `ffmpeg -f lavfi`
- `ffprobe` track/duration extraction & ISO-639 language synonym matching (`deu` -> `ger`)
- Full `HotelCastSession` local-file remux into `<video>.hls/`, `MockAirPlayReceiver` `POST /play` handshake, persistent keep-alive socket verification, `stream.m3u8` + `.ts` `HTTP 200` and `HTTP 206 Range` responses, CORS `OPTIONS /api/cast`, and `GET /api/status`
- VOD cache completion (`is_hls_cache_complete`), preservation on session stop, instant zero-ffmpeg second-run resume with `seek_seconds=2.5` (`Start-Position: 0.5` + `/seek_stream.m3u8`), and `delete_video_hls_cache`
- Clean empty defaults in `HotelCastConfig`, URL/token expansion, VPN interface filtering, and `hotelcast://` URI / CLI parsing

### Installing / Syncing Local Changes
If testing the installed desktop launcher (`~/.local/bin/hotelcast` or `hotelcast.desktop`), sync changes into `~/hotelcast` by running:

```bash
bash ./install.sh
```
