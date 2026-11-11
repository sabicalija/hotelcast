# HotelCast — Automated Linux-to-AirPlay / HappyCast Streamer

Stream local video files, Chrome web video URLs, or live Chrome window / desktop screen + audio directly from Linux to hotel and smart TVs running AirPlay v1 / HappyCast / LeLink receivers (e.g., Xiaomi hotel TVs on port `52266`) — even while connected to a VPN or behind a hotel room-binding portal.

---

## Key Features

- **VPN Bypass & Physical Wi-Fi Pinning ([`network.py`](network.py)):** Automatically detects the physical wireless NIC (`/sys/class/net/*/wireless`, e.g., `wlan0`), ignores VPN/tunnel/container interfaces (`wg*`, `tun*`, `mihomo`, `docker*`), pins sockets directly to Wi-Fi, and injects `/24` LAN bypass routes so TV streaming works without disconnecting your VPN.
- **Hotel Portal Bridge Activation ([`network.py`](network.py)):** Supports hotel QR-portal URLs or short activation codes to trigger router TV-to-client IP binding (bypassing active proxies/VPNs), both manually and automatically on TV connection timeout.
- **Persistent Per-Movie `.hls/` Cache & Dual Seeking ([`streamer.py`](streamer.py)):** Remuxes local videos (`-c:v copy` + stereo AAC) into a reusable `<Movie>.hls/` folder beside the video file (`meta.json` + VOD playlist finalization) for instant zero-reconversion resume, paired with dual seeking (`Start-Position` fraction + dynamically sliced `seek_stream.m3u8`).
- **3 Streaming Modes:**
  1. **Local Video Files:** Multi-audio track probing (`ffprobe`), ISO-639 preferred language auto-selection, instant start, and seek slider (`0–100%` + `HH:MM:SS`).
  2. **Chrome Web URLs:** Direct `.m3u8`/`.mp4` links or `yt-dlp` stream resolution/piping into rolling live HLS segments.
  3. **Live Screen / Chrome Window Mirroring:** `x11grab` desktop or specific window capture + PulseAudio/PipeWire default `.monitor` sink (`libx264 ultrafast zerolatency`).
- **One-Click Chrome Extension ([`chrome_extension/`](chrome_extension/)):** Manifest V3 extension that casts the active tab URL or mirrors the Chrome window via `http://127.0.0.1:8000/api/cast`, automatically launching the desktop app via `hotelcast://` if it isn't running yet.

---

## Architecture Overview

- **[`config.py`](config.py)** — Persistent configuration in `~/.config/hotelcast/config.json` (TV IP/port, room number, activation URL, last browsed folder, preferred audio language).
- **[`network.py`](network.py)** — Physical Wi-Fi interface detection, WireGuard/OpenVPN subnet route bypass, Wi-Fi-pinned HTTP/HTTPS connections, and hotel portal bridge activation.
- **[`discovery.py`](discovery.py)** — Direct TCP reachability check, mDNS discovery (`_airplay._tcp` & `_leboremote._tcp` via `avahi-browse` and `zeroconf` fallback), TXT port extraction (`airplay=` / `lelinkport=`), and `ufw` firewall inspection.
- **[`streamer.py`](streamer.py)** — `ffprobe` track/duration inspection, per-movie `.hls/` cache lifecycle, dynamic seek playlist generation, and `ffmpeg` / `yt-dlp` process management.
- **[`server.py`](server.py)** — Multi-threaded `ThreadingHTTPServer` with `206 Partial Content` HTTP `Range` support, dynamic HLS directory switching, disconnect suppression, and CORS-enabled `/api/cast`, `/api/stop`, and `/api/status` endpoints.
- **[`airplay.py`](airplay.py)** — Segment readiness wait, Wi-Fi-pinned HTTP/1.1 `POST /play` with persistent `Connection: keep-alive` control socket, background `GET /playback-info` polling, and clean `POST /stop` teardown.
- **[`app.py`](app.py)** — Single-window Tkinter `ttk` desktop GUI, CLI entry point, and `hotelcast://` single-instance IPC forwarder.

---

## System Prerequisites (Ubuntu / Debian)

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip python3-tk ffmpeg avahi-utils pulseaudio-utils x11-utils xdotool
```

---

## Installation

Clone the repository anywhere (including directly into `~/hotelcast`) and run `./install.sh`:

```bash
git clone https://github.com/sabicalija/hotelcast.git ~/hotelcast
cd ~/hotelcast
bash ./install.sh
```

`install.sh` sets up `~/hotelcast/.venv` (installing optional `yt-dlp` and `zeroconf` packages from [`requirements.txt`](requirements.txt)), creates `~/.local/bin/hotelcast`, and registers `~/.local/share/applications/hotelcast.desktop` for the `hotelcast://` URI scheme and video file associations.

### Installing the Chrome Extension (Optional)

1. Open `chrome://extensions/` in Chrome / Chromium / Brave.
2. Enable **Developer mode** (top right).
3. Click **Load unpacked** and select `~/hotelcast/chrome_extension`.

---

## Usage

### 1. Launch Desktop GUI
```bash
hotelcast
# or directly with Python:
python3 app.py
```

### 2. CLI Commands
```bash
# Scan LAN for AirPlay / HappyCast TVs
hotelcast --scan

# Activate Hotel TV Bridge (full portal URL or short QR code)
hotelcast --activate "https://portal.example/bind"

# List audio tracks and duration in a local video file
hotelcast --list-tracks movie.mkv

# Stream a local video file (with audio track 1 and start offset 00:10:00)
hotelcast --file movie.mkv --audio-track 1 --start-time 00:10:00 --tv 192.168.1.50:52266

# Delete the persistent .hls/ cache folder for a local video file
hotelcast --delete-cache movie.mkv

# Stream a Chrome web video URL
hotelcast --url "https://example.com/video" --tv 192.168.1.50:52266

# Mirror live desktop screen or a specific Chrome window
hotelcast --mirror --tv 192.168.1.50:52266
hotelcast --mirror --window-id 0x02a00003 --tv 192.168.1.50:52266
```

---

## Running Tests

The end-to-end test suite runs completely offline against `127.0.0.1` using a synthetic `ffmpeg` test video and a mock AirPlay HTTP/1.1 receiver:

```bash
python3 -m unittest discover -v -s tests
```
