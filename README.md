# Video Downloader

A Chrome extension + Python companion app that detects and downloads videos from any website — including HLS (`.m3u8`), DASH (`.mpd`), MP4, WebM, and MKV streams.

Works with CDN-protected streams by replaying the exact browser headers (Origin, Referer, cookies) that the site uses.

---

## How it works

- The extension watches network requests and shows a badge count when it detects video streams on the current tab
- Click the extension icon → click **Download** next to any stream
- Direct files (MP4, WebM) are downloaded by the browser
- Streaming manifests (HLS, DASH) are handed off to the Python companion app, which uses **yt-dlp** + **ffmpeg** to download and merge all segments into a single MP4

---

## Prerequisites

- **Windows 10/11** (Linux/Mac support coming — the install script is Windows-only for now)
- **Python 3.10+** — [python.org](https://www.python.org/downloads/) (check "Add to PATH" during install)
- **ffmpeg** — [ffmpeg.org](https://ffmpeg.org/download.html) (add the `bin/` folder to your PATH)
- **Google Chrome**

---

## Installation

### 1. Clone the repo

```bat
git clone https://github.com/anunrs/VideoDownloader.git
cd VideoDownloader
```

### 2. Install the Python companion app

Run the installer (as a regular user, no admin required):

```bat
backend\install\install.bat
```

This will:
- Install / upgrade **yt-dlp** via pip
- Check that ffmpeg is available
- Write the native messaging manifest with your local path
- Register it in the Windows registry so Chrome can find it

### 3. Load the Chrome extension

1. Open Chrome and go to `chrome://extensions`
2. Enable **Developer mode** (top-right toggle)
3. Click **Load unpacked**
4. Select the `frontend/` folder from this repo

### 4. Update the extension ID (one-time)

After loading the extension, Chrome assigns it an ID (shown on the extensions page).

Open `backend/install/com.videodownloader.host.json` and replace the ID in `allowed_origins`:

```json
"allowed_origins": [
  "chrome-extension://YOUR_EXTENSION_ID_HERE/"
]
```

Then re-run `backend\install\install.bat` to write the updated manifest, and restart Chrome.

> **Note:** If you clone this repo and use the same `frontend/manifest.json` key, your extension ID will be identical to the original — no change needed.

---

## Usage

1. Navigate to any page with a video
2. Look for the red badge count on the extension icon
3. Click the icon to open the popup
4. Click **Download** next to the stream you want
5. Files are saved to `~/Downloads/VideoDownloader/`

### Troubleshooting

- **"Companion app not installed"** — run `backend\install\install.bat` again
- **Download fails / 403 error** — make sure you're logged in to the site in Chrome before downloading; the extension passes your session cookies to yt-dlp
- **Check the log** at `~/Downloads/VideoDownloader/logs/downloader.log` for detailed error output

---

## Project structure

```
VideoDownloader/
├── frontend/               # Chrome extension (Manifest V3)
│   ├── manifest.json
│   ├── background.js       # Stream detection, native messaging
│   ├── content.js          # Finds <video> src tags
│   └── popup/              # Extension popup UI
│
├── backend/                # Python companion app
│   ├── host.py             # Native messaging host (launched by Chrome)
│   ├── downloader.py       # yt-dlp + ffmpeg wrapper
│   ├── host.bat            # Wrapper batch file Chrome uses to launch host.py
│   ├── requirements.txt
│   └── install/
│       └── install.bat     # One-click installer for Windows
```

---

## Dependencies

| Tool | Purpose |
|------|---------|
| [yt-dlp](https://github.com/yt-dlp/yt-dlp) | Downloads and merges HLS/DASH streams |
| [ffmpeg](https://ffmpeg.org/) | Muxes video + audio segments into MP4 |
| [curl_cffi](https://github.com/yifeikong/curl_cffi) | Chrome TLS fingerprint impersonation (`pip install curl_cffi`) |
