#!/usr/bin/env bash
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
INSTALL_DIR="${HOME}/hotelcast"
BIN_DIR="${HOME}/.local/bin"
APP_DIR="${HOME}/.local/share/applications"
PYTHON_BIN="$(command -v python3 || echo /usr/bin/python3)"

mkdir -p "${INSTALL_DIR}" "${BIN_DIR}" "${APP_DIR}"
DEST_DIR="$(cd "${INSTALL_DIR}" && pwd -P)"

echo "[1/5] Checking Chrome Extension PNG icons (16x16, 48x48, 128x128)..."
ICON_DIR="${SRC_DIR}/chrome_extension/icons"
if [ -f "${ICON_DIR}/icon-16.png" ] && [ -f "${ICON_DIR}/icon-48.png" ] && [ -f "${ICON_DIR}/icon-128.png" ]; then
    echo "  Icons already present in ${ICON_DIR}."
else
    echo "  Generating missing PNG icons..."
    "${PYTHON_BIN}" - "${ICON_DIR}" << 'PYEOF' || echo "  (Warning: Pillow not installed; skipping icon generation.)"
import os
import sys
from PIL import Image, ImageDraw

out_dir = sys.argv[1]
os.makedirs(out_dir, exist_ok=True)

for size in (16, 48, 128):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    margin = max(1, size // 16)
    draw.rounded_rectangle(
        [margin, margin, size - margin - 1, size - margin - 1],
        radius=max(2, size // 4),
        fill=(37, 99, 235, 255),
    )
    tri_left = int(size * 0.36)
    tri_right = int(size * 0.70)
    tri_top = int(size * 0.28)
    tri_bot = int(size * 0.72)
    draw.polygon(
        [(tri_left, tri_top), (tri_left, tri_bot), (tri_right, size // 2)],
        fill=(255, 255, 255, 255),
    )
    img.save(os.path.join(out_dir, f"icon-{size}.png"))
PYEOF
fi

echo "[2/5] Installing HotelCast modules and Chrome extension to ${INSTALL_DIR}..."
if [ "${SRC_DIR}" = "${DEST_DIR}" ]; then
    echo "  Source is already located at ${INSTALL_DIR}; skipping file copy."
else
    mkdir -p "${INSTALL_DIR}/tests" "${INSTALL_DIR}/chrome_extension"
    cp -f "${SRC_DIR}/config.py" \
          "${SRC_DIR}/network.py" \
          "${SRC_DIR}/discovery.py" \
          "${SRC_DIR}/streamer.py" \
          "${SRC_DIR}/server.py" \
          "${SRC_DIR}/airplay.py" \
          "${SRC_DIR}/app.py" \
          "${SRC_DIR}/requirements.txt" \
          "${SRC_DIR}/README.md" \
          "${SRC_DIR}/AGENTS.md" \
          "${SRC_DIR}/install.sh" \
          "${INSTALL_DIR}/"
    cp -f "${SRC_DIR}/tests/test_pipeline.py" "${INSTALL_DIR}/tests/"
    cp -rf "${SRC_DIR}/chrome_extension/." "${INSTALL_DIR}/chrome_extension/"
fi
chmod +x "${INSTALL_DIR}/app.py" "${INSTALL_DIR}/install.sh"

echo "[3/5] Setting up Python virtual environment in ${INSTALL_DIR}/.venv..."
if [ ! -x "${INSTALL_DIR}/.venv/bin/python3" ]; then
    "${PYTHON_BIN}" -m venv --system-site-packages "${INSTALL_DIR}/.venv"
fi
if [ -x "${INSTALL_DIR}/.venv/bin/pip" ]; then
    "${INSTALL_DIR}/.venv/bin/pip" install -q -r "${INSTALL_DIR}/requirements.txt" || \
        echo "  (Note: Could not install optional pip packages; core stdlib streamer is unaffected.)"
fi

echo "[4/5] Creating CLI launcher at ${BIN_DIR}/hotelcast..."
cat > "${BIN_DIR}/hotelcast" << 'EOF'
#!/usr/bin/env bash
exec "${HOME}/hotelcast/.venv/bin/python3" "${HOME}/hotelcast/app.py" "$@"
EOF
chmod +x "${BIN_DIR}/hotelcast"

echo "[5/5] Registering hotelcast.desktop for hotelcast:// URI scheme and video MIME types..."
cat > "${APP_DIR}/hotelcast.desktop" << EOF
[Desktop Entry]
Version=1.0
Type=Application
Name=HotelCast
GenericName=AirPlay & HappyCast Video Streamer
Comment=Stream local videos, Chrome web URLs, and screen to AirPlay / HappyCast Hotel TVs
Exec=${BIN_DIR}/hotelcast %U
Icon=video-display
Terminal=false
Categories=AudioVideo;Video;Network;Player;
MimeType=x-scheme-handler/hotelcast;video/x-matroska;video/mp4;video/x-msvideo;video/quicktime;video/webm;video/mp2t;
StartupNotify=true
EOF

xdg-mime default hotelcast.desktop x-scheme-handler/hotelcast 2>/dev/null || true
update-desktop-database "${APP_DIR}/" 2>/dev/null || true

MISSING_PKGS=()
command -v ffmpeg >/dev/null 2>&1 || MISSING_PKGS+=("ffmpeg")
command -v avahi-browse >/dev/null 2>&1 || MISSING_PKGS+=("avahi-utils")
command -v pactl >/dev/null 2>&1 || MISSING_PKGS+=("pulseaudio-utils")
command -v xwininfo >/dev/null 2>&1 || MISSING_PKGS+=("x11-utils")
"${INSTALL_DIR}/.venv/bin/python3" -c "import tkinter" >/dev/null 2>&1 || MISSING_PKGS+=("python3-tk")

echo ""
echo "HotelCast installed successfully in ${INSTALL_DIR}!"
if [[ ":${PATH}:" != *":${BIN_DIR}:"* ]]; then
    echo "Note: Ensure ${BIN_DIR} is in your PATH (e.g., export PATH=\"\$HOME/.local/bin:\$PATH\")."
fi
if [ "${#MISSING_PKGS[@]}" -gt 0 ]; then
    echo "Recommended system packages missing — install on Ubuntu/Debian with:"
    echo "  sudo apt install -y ${MISSING_PKGS[*]}"
fi
