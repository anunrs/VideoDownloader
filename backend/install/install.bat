@echo off
setlocal enabledelayedexpansion
title Video Downloader — Companion App Installer

echo ============================================================
echo  Video Downloader Companion App Installer (Windows)
echo ============================================================
echo.

:: ── 1. Check Python ──────────────────────────────────────────────────────────
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python is not installed or not on PATH.
    echo         Download it from https://python.org ^(check "Add to PATH"^).
    pause & exit /b 1
)
echo [OK] Python found.

:: ── 2. Install / upgrade yt-dlp ──────────────────────────────────────────────
echo.
echo [INFO] Installing / upgrading yt-dlp...
pip install --upgrade yt-dlp
if errorlevel 1 (
    echo [ERROR] pip install failed. Try running this script as Administrator.
    pause & exit /b 1
)
echo [OK] yt-dlp installed.

:: ── 3. Check ffmpeg ──────────────────────────────────────────────────────────
echo.
ffmpeg -version >nul 2>&1
if errorlevel 1 (
    echo [WARN] ffmpeg not found on PATH.
    echo        HLS/DASH stream merging will be unavailable.
    echo        Install ffmpeg: https://ffmpeg.org/download.html
    echo        Then add its bin/ folder to your PATH environment variable.
) else (
    echo [OK] ffmpeg found.
)

:: ── 4. Build native messaging manifest ───────────────────────────────────────
echo.

:: Resolve the backend directory (one level up from install\)
set "BACKEND_DIR=%~dp0.."
:: Canonicalise (remove trailing backslash, resolve ..)
for %%I in ("%BACKEND_DIR%") do set "BACKEND_DIR=%%~fI"

set "HOST_BAT=%BACKEND_DIR%\host.bat"
set "MANIFEST_PATH=%BACKEND_DIR%\install\com.videodownloader.host.json"

:: Escape backslashes for JSON
set "HOST_BAT_JSON=%HOST_BAT:\=\\%"

(
echo {
echo   "name": "com.videodownloader.host",
echo   "description": "Video Downloader Native Messaging Host",
echo   "path": "%HOST_BAT_JSON%",
echo   "type": "stdio",
echo   "allowed_origins": [
echo     "chrome-extension://cpejpofmeghicfogjehflembmdolohbp/"
echo   ]
echo }
) > "%MANIFEST_PATH%"

echo [OK] Manifest written to:
echo      %MANIFEST_PATH%

:: ── 5. Register with Chrome (current user) ───────────────────────────────────
echo.
set "REG_KEY=HKCU\Software\Google\Chrome\NativeMessagingHosts\com.videodownloader.host"
reg add "%REG_KEY%" /ve /t REG_SZ /d "%MANIFEST_PATH%" /f >nul
if errorlevel 1 (
    echo [ERROR] Could not write to registry. Try running as Administrator.
    pause & exit /b 1
)
echo [OK] Registry key created:
echo      %REG_KEY%

:: ── 6. Remind user to paste extension ID ─────────────────────────────────────
echo.
echo ============================================================
echo  NEXT STEP — required before downloading HLS/DASH streams:
echo.
echo  1. Load the extension in Chrome:
echo       chrome://extensions  ^>  Load unpacked  ^>  select frontend/
echo.
echo  2. Copy the Extension ID shown on that page.
echo.
echo  3. Open this file and replace REPLACE_WITH_YOUR_EXTENSION_ID:
echo       %MANIFEST_PATH%
echo.
echo  4. Restart Chrome.
echo ============================================================
echo.
echo Installation complete!
pause
