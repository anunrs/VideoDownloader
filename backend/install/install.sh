#!/usr/bin/env bash
# install.sh — macOS / Linux companion app installer
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
HOST_BAT="$BACKEND_DIR/host.py"
MANIFEST_FILE="$SCRIPT_DIR/com.videodownloader.host.json"

echo "============================================================"
echo " Video Downloader Companion App Installer (macOS / Linux)"
echo "============================================================"
echo

# ── 1. Check Python ──────────────────────────────────────────────────────────
if ! command -v python3 &>/dev/null; then
    echo "[ERROR] python3 not found. Install it via your package manager."
    exit 1
fi
echo "[OK] python3 found."

# ── 2. Install / upgrade yt-dlp ──────────────────────────────────────────────
echo
echo "[INFO] Installing / upgrading yt-dlp..."
python3 -m pip install --upgrade yt-dlp
echo "[OK] yt-dlp installed."

# ── 3. Check ffmpeg ──────────────────────────────────────────────────────────
echo
if command -v ffmpeg &>/dev/null; then
    echo "[OK] ffmpeg found."
else
    echo "[WARN] ffmpeg not found. HLS/DASH stream merging will be unavailable."
    echo "       macOS:  brew install ffmpeg"
    echo "       Ubuntu: sudo apt install ffmpeg"
fi

# ── 4. Make host.py executable ───────────────────────────────────────────────
chmod +x "$HOST_BAT"

# ── 5. Write native messaging manifest ───────────────────────────────────────
echo
PYTHON_BIN="$(command -v python3)"

cat > "$MANIFEST_FILE" <<EOF
{
  "name": "com.videodownloader.host",
  "description": "Video Downloader Native Messaging Host",
  "path": "$HOST_BAT",
  "type": "stdio",
  "allowed_origins": [
    "chrome-extension://REPLACE_WITH_YOUR_EXTENSION_ID/"
  ]
}
EOF

echo "[OK] Manifest written to: $MANIFEST_FILE"

# ── 6. Install manifest for Chrome ───────────────────────────────────────────
echo
if [[ "$OSTYPE" == "darwin"* ]]; then
    # macOS — current user
    CHROME_NMH_DIR="$HOME/Library/Application Support/Google/Chrome/NativeMessagingHosts"
else
    # Linux — current user
    CHROME_NMH_DIR="$HOME/.config/google-chrome/NativeMessagingHosts"
fi

mkdir -p "$CHROME_NMH_DIR"
cp "$MANIFEST_FILE" "$CHROME_NMH_DIR/com.videodownloader.host.json"
echo "[OK] Manifest installed to: $CHROME_NMH_DIR"

# ── 7. Remind about extension ID ─────────────────────────────────────────────
echo
echo "============================================================"
echo " NEXT STEP — required before downloading HLS/DASH streams:"
echo
echo " 1. Load the extension in Chrome:"
echo "      chrome://extensions  >  Load unpacked  >  select frontend/"
echo
echo " 2. Copy the Extension ID shown on that page."
echo
echo " 3. Replace REPLACE_WITH_YOUR_EXTENSION_ID in:"
echo "      $CHROME_NMH_DIR/com.videodownloader.host.json"
echo
echo " 4. Restart Chrome."
echo "============================================================"
echo
echo "Installation complete!"
