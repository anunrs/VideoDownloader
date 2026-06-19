"""
downloader.py — yt-dlp + ffmpeg wrapper for Video Downloader.

All output is written to:
  ~/Downloads/VideoDownloader/logs/downloader.log
Open that file to see the full yt-dlp output when a download fails.
"""

import glob
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path


# ── Logging setup ─────────────────────────────────────────────────────────────

_LOG_DIR  = Path.home() / 'Downloads' / 'VideoDownloader' / 'logs'
_LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE  = str(_LOG_DIR / 'downloader.log')

logging.basicConfig(
    filename = LOG_FILE,
    level    = logging.DEBUG,
    format   = '%(asctime)s  %(levelname)-8s  %(message)s',
    datefmt  = '%Y-%m-%d %H:%M:%S',
)
log = logging.getLogger(__name__)


class Downloader:

    DEFAULT_OUTPUT_DIR = str(Path.home() / 'Downloads' / 'VideoDownloader')

    def __init__(self, output_dir: str | None = None):
        self.output_dir = output_dir or self.DEFAULT_OUTPUT_DIR
        os.makedirs(self.output_dir, exist_ok=True)

        # Prefer the yt-dlp binary on PATH; fall back to the Python module so
        # Chrome's restricted launch environment (which strips Scripts/ from PATH)
        # doesn't break the lookup.
        _ytdlp_bin = shutil.which('yt-dlp')
        if _ytdlp_bin:
            self.ytdlp = [_ytdlp_bin]
        else:
            try:
                import yt_dlp as _  # noqa: F401
                self.ytdlp = [sys.executable, '-u', '-m', 'yt_dlp']
            except ImportError:
                self.ytdlp = None

        self.ffmpeg = shutil.which('ffmpeg')

        log.debug('Downloader init — output_dir=%s  ytdlp=%s  ffmpeg=%s',
                  self.output_dir, self.ytdlp, self.ffmpeg)

    # ── Public entry point ────────────────────────────────────────────────────

    def download(
        self,
        url:              str,
        stream_type:      str,
        progress_cb       = None,
        page_url:         str | None  = None,
        cookies:          list | None = None,
        captured_headers: list | None = None,  # [{name,value}] from browser's CDN request
        listener_fired:   bool        = False,
        all_header_names: list | None = None,
    ) -> dict:
        """
        Download *url* and return a result dict.
        If *page_url* is given (the browser tab URL), yt-dlp tries that first
        so site-specific extractors (YouTube, Vimeo, …) can be used.
        *cookies* is a list of dicts with keys domain/path/secure/expires/name/value,
        already decrypted by the Chrome extension (bypasses the locked SQLite DB).
        Falls back to the direct stream URL automatically.
        """
        log.info('=== Download request ===  type=%s  url=%s  page_url=%s  cookies=%d',
                 stream_type, url, page_url, len(cookies) if cookies else 0)

        def notify(status: str, message: str, percent: float | None = None):
            log.debug('[%s] %s', status, message)
            if progress_cb:
                payload = {'status': status, 'message': message}
                if percent is not None:
                    payload['percent'] = percent
                progress_cb(payload)

        if not self.ytdlp:
            msg = ('yt-dlp not found. '
                   'Run backend/install/install.bat to install it.')
            log.error(msg)
            return {'success': False, 'error': msg, 'logFile': LOG_FILE}

        # Log what the browser's onBeforeSendHeaders listener captured.
        log.debug('Browser header capture: listener_fired=%s  all_headers=%s  auth_headers=%s',
                  listener_fired,
                  all_header_names or [],
                  [(h.get('name'), h.get('value', '')[:40]) for h in (captured_headers or [])])

        # Build cookie file for yt-dlp (used if no captured Cookie header below).
        # Also look for a Cookie header in the captured set.
        captured_cookie_value = None
        for h in (captured_headers or []):
            if h.get('name', '').lower() == 'cookie':
                captured_cookie_value = h['value']
                break

        if captured_cookie_value:
            log.debug('Using captured Cookie header (%d chars)', len(captured_cookie_value))
            cookie_file = None   # passed directly via --add-header
        elif cookies:
            cookie_file = _write_cookie_file(cookies)
            domains = sorted({c.get('domain', '') for c in cookies})
            log.debug('Using %d extension-supplied cookies for domains: %s',
                      len(cookies), domains)
        else:
            cookie_file = _export_chrome_cookies()

        # Try the page URL only when the stream URL is NOT already a direct
        # manifest — for .m3u8 / .mpd we already have the stream, so the page
        # attempt just hangs while yt-dlp tries generic extractors on a web page.
        stream_path = url.lower().split('?')[0]
        is_manifest = stream_path.endswith('.m3u8') or stream_path.endswith('.mpd')
        if page_url and page_url.startswith('http') and page_url != url and not is_manifest:
            notify('starting', f'Trying page URL with yt-dlp: {page_url}')
            result = self._ytdlp_download(page_url, progress_cb, cookie_file,
                                          referer=page_url,
                                          captured_headers=captured_headers,
                                          captured_cookie=captured_cookie_value)
            if result['success']:
                _cleanup(cookie_file)
                return result
            log.info('Page URL failed (%s), falling back to stream URL.',
                     result.get('error', '')[:120])

        notify('starting', f'Downloading stream ({stream_type}): {url}')
        result = self._ytdlp_download(url, progress_cb, cookie_file,
                                      referer=page_url,
                                      captured_headers=captured_headers,
                                      captured_cookie=captured_cookie_value)

        # ffmpeg fallback for raw HLS/DASH if yt-dlp fails
        if not result['success'] and stream_type in ('HLS', 'DASH', 'TS') and self.ffmpeg:
            notify('starting', 'yt-dlp failed — trying ffmpeg direct mux…')
            result = self._ffmpeg_download(url, stream_type, progress_cb)

        _cleanup(cookie_file)
        result['logFile'] = LOG_FILE
        log.info('Result: success=%s  error=%s', result.get('success'),
                 result.get('error', '')[:200])
        return result

    # ── yt-dlp ────────────────────────────────────────────────────────────────

    def _ytdlp_download(
        self,
        url:               str,
        progress_cb        = None,
        cookie_file:       str | None = None,
        referer:           str | None = None,
        captured_headers:  list | None = None,  # [{name,value}] all auth headers from browser
        captured_cookie:   str | None = None,   # value of Cookie header if present
    ) -> dict:
        output_template = os.path.join(self.output_dir, '%(title)s.%(ext)s')

        cmd = [
            *self.ytdlp,
            '--no-playlist',
            '--output',              output_template,
            '--format',              'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
            '--merge-output-format', 'mp4',
            '--newline',
            '--socket-timeout',      '30',
            '--retries',             '3',
        ]

        if self.ffmpeg:
            cmd += ['--ffmpeg-location', self.ffmpeg]

        # Classify the URL so we can apply the right auth strategy per site.
        _url_lower = url.lower()
        is_youtube   = 'youtube.com/watch' in _url_lower or 'youtu.be/' in _url_lower
        is_instagram = 'instagram.com/' in _url_lower

        if is_youtube:
            # YouTube has its own extractor inside yt-dlp — adding --impersonate or
            # custom headers breaks its internal API calls and n-challenge solving.
            # android client bypasses both n-challenge and GVS PO Token requirements.
            # mweb is kept as automatic fallback.
            cmd += ['--extractor-args', 'youtube:player_client=android,mweb']
            log.debug('YouTube detected: using android+mweb clients, skipping cookies')
        elif is_instagram:
            # Instagram requires browser cookies for most content (login-gated).
            # Use the cookie file; --impersonate is added below with the other sites.
            # Do NOT forward CDN request headers — the page URL is passed to yt-dlp's
            # Instagram extractor which manages its own API headers.
            # --ignore-errors lets yt-dlp skip image items in carousel posts (which
            # have no video formats) without treating the whole download as failed.
            cmd += ['--ignore-errors']
            if cookie_file:
                cmd += ['--cookies', cookie_file]
                log.debug('Instagram detected: passing cookie file to yt-dlp extractor')
            else:
                log.warning('Instagram: no cookies available — private content will fail.')
        elif captured_cookie:
            # Cookie header intercepted from the browser's own CDN request.
            cmd += ['--add-header', f'Cookie: {captured_cookie}']
        elif cookie_file:
            cmd += ['--cookies', cookie_file]
        else:
            log.warning('No cookies available — download may fail for auth-protected streams.')

        # Forward all non-cookie auth headers the browser sent (Origin, Referer, tokens…).
        # Skip for YouTube (extractor manages its own) and Instagram (page-URL extractor,
        # no CDN headers to replay).
        already_set: set[str] = set()
        if not is_youtube and not is_instagram:
            for h in (captured_headers or []):
                name  = h.get('name', '')
                value = h.get('value', '')
                if not name or not value or name.lower() == 'cookie':
                    continue
                cmd += ['--add-header', f'{name}: {value}']
                already_set.add(name.lower())
                log.debug('Forwarding captured header: %s: %s…', name, value[:60])

            # Impersonate Chrome's TLS + HTTP/2 fingerprint (requires curl_cffi).
            # Not used for YouTube/Instagram — their extractors manage their own sessions.
            cmd += ['--impersonate', 'chrome']

            # Fallback User-Agent (curl_cffi sets its own, but this acts as a safety net).
            cmd += ['--user-agent',
                    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                    'AppleWebKit/537.36 (KHTML, like Gecko) '
                    'Chrome/137.0.0.0 Safari/537.36']

            # Add Referer / Origin from the page URL ONLY if the browser capture did not
            # already provide them.  Sending two conflicting Origin headers causes 403.
            if referer and referer.startswith('http'):
                if 'referer' not in already_set:
                    cmd += ['--add-header', f'Referer: {referer}']
                if 'origin' not in already_set:
                    try:
                        from urllib.parse import urlparse as _up
                        _p = _up(referer)
                        cmd += ['--add-header', f'Origin: {_p.scheme}://{_p.netloc}']
                    except Exception:
                        pass

        cmd.append(url)
        log.debug('yt-dlp command: %s', ' '.join(cmd))

        # Preflight: probe the URL with the same auth we'll pass to yt-dlp.
        _preflight_url(url, cookie_file, referer, cookie_header=captured_cookie)

        return self._run(cmd, progress_cb, tool='yt-dlp')

    # ── ffmpeg direct mux (raw HLS/DASH fallback) ─────────────────────────────

    def _ffmpeg_download(self, url: str, stream_type: str, progress_cb=None) -> dict:
        out_file = os.path.join(self.output_dir, f'stream_{int(time.time())}.mp4')
        cmd = [
            self.ffmpeg, '-y',
            '-i', url,
            '-c', 'copy',
            out_file,
        ]
        log.debug('ffmpeg command: %s', ' '.join(cmd))
        result = self._run(cmd, progress_cb, tool='ffmpeg')
        if result['success']:
            result['filename'] = out_file
        return result

    # ── Subprocess runner ─────────────────────────────────────────────────────

    def _run(self, cmd: list, progress_cb=None, tool: str = '') -> dict:
        try:
            process = subprocess.Popen(
                cmd,
                stdin    = subprocess.DEVNULL,   # prevent inheriting Chrome's NM stdin;
                                                 # binary NM data on that pipe can cause
                                                 # yt-dlp's Python runtime to hang on startup
                stdout   = subprocess.PIPE,
                stderr   = subprocess.STDOUT,
            )
        except FileNotFoundError as exc:
            log.error('%s not found: %s', tool, exc)
            return {'success': False, 'error': str(exc)}

        all_lines: list[str] = []
        filename: str | None = None

        # readline() flushes on each line on Windows; the file-iterator does not.
        while True:
            raw_bytes = process.stdout.readline()
            if not raw_bytes:
                break
            raw = raw_bytes.decode('utf-8', errors='replace')
            line = raw.strip()
            if not line:
                continue

            log.debug('%s | %s', tool, line)
            all_lines.append(line)

            if progress_cb:
                if '[download]' in line:
                    progress_cb({
                        'status':  'downloading',
                        'message': line,
                        'percent': _parse_percent(line),
                    })
                elif any(k in line for k in ('[Merger]', '[ffmpeg]', 'Merging')):
                    progress_cb({'status': 'merging', 'message': line})
                else:
                    progress_cb({'status': 'info', 'message': line})

            m = re.search(r'Destination:\s+(.+)$', line)
            if m:
                filename = m.group(1).strip()

        process.wait()

        if process.returncode == 0:
            return {
                'success':   True,
                'message':   'Download complete.',
                'filename':  filename,
                'outputDir': self.output_dir,
            }

        # Return the last 20 lines so the popup can show a meaningful error
        tail = '\n'.join(all_lines[-20:]) if all_lines else f'{tool} exited with code {process.returncode}'
        log.error('%s failed (rc=%d):\n%s', tool, process.returncode, tail)
        return {
            'success': False,
            'error':   tail,
        }


# ── Helpers ───────────────────────────────────────────────────────────────────

def _cleanup(path: str | None) -> None:
    """Delete a temp file, ignoring errors."""
    if path:
        try:
            os.unlink(path)
        except OSError:
            pass


def _write_cookie_file(cookies: list) -> str | None:
    """
    Write a list of cookie dicts (from chrome.cookies API) to a Netscape
    cookie file for yt-dlp's --cookies flag.
    Each dict must have: domain, path, secure, expires, name, value.
    Returns the temp file path, or None on failure.
    """
    if not cookies:
        return None
    fd, path = tempfile.mkstemp(suffix='.txt', prefix='vd_cookies_')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write('# Netscape HTTP Cookie File\n')
            for c in cookies:
                host      = c.get('domain', '')
                subdomain = 'TRUE' if host.startswith('.') else 'FALSE'
                cpath     = c.get('path', '/')
                secure    = 'TRUE' if c.get('secure') else 'FALSE'
                expires   = int(c.get('expires', 0) or 0)
                name      = c.get('name', '')
                value     = c.get('value', '')
                f.write(f'{host}\t{subdomain}\t{cpath}\t{secure}\t{expires}\t{name}\t{value}\n')
        log.debug('Wrote %d extension cookies to %s', len(cookies), path)
        return path
    except Exception as exc:
        log.warning('Could not write cookie file: %s', exc)
        _cleanup(path)
        return None


def _dpapi_decrypt(ciphertext: bytes) -> bytes:
    """Decrypt bytes using Windows DPAPI (no extra dependencies)."""
    import ctypes
    import ctypes.wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [('cbData', ctypes.wintypes.DWORD),
                    ('pbData', ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(ciphertext, len(ciphertext))
    blob_in  = DATA_BLOB(len(ciphertext), buf)
    blob_out = DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None, 0,
        ctypes.byref(blob_out),
    )
    if not ok:
        raise OSError(f'CryptUnprotectData failed: {ctypes.GetLastError()}')
    result = ctypes.string_at(blob_out.pbData, blob_out.cbData)
    ctypes.windll.kernel32.LocalFree(blob_out.pbData)
    return result


def _get_chrome_aes_key() -> bytes | None:
    """Read and decrypt Chrome's AES-256 cookie encryption key."""
    import base64, json
    local_state_path = os.path.join(
        os.environ.get('LOCALAPPDATA', ''),
        'Google', 'Chrome', 'User Data', 'Local State',
    )
    try:
        with open(local_state_path, encoding='utf-8') as f:
            ls = json.load(f)
        enc_key_b64 = ls['os_crypt']['encrypted_key']
        enc_key = base64.b64decode(enc_key_b64)[5:]  # strip 'DPAPI' prefix
        return _dpapi_decrypt(enc_key)               # 32-byte AES key
    except Exception as exc:
        log.warning('Could not read Chrome AES key: %s', exc)
        return None


def _decrypt_chrome_value(encrypted_value: bytes, aes_key: bytes) -> str:
    """Decrypt a Chrome cookie encrypted_value field (v10/v11 AES-GCM)."""
    if not encrypted_value:
        return ''
    if encrypted_value[:3] in (b'v10', b'v11'):
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
            nonce      = encrypted_value[3:15]
            ciphertext = encrypted_value[15:]
            return AESGCM(aes_key).decrypt(nonce, ciphertext, None).decode('utf-8', errors='replace')
        except Exception:
            return ''
    # Older DPAPI-per-value fallback
    try:
        return _dpapi_decrypt(encrypted_value).decode('utf-8', errors='replace')
    except Exception:
        return ''


def _export_chrome_cookies() -> str | None:
    """
    Export Chrome's cookies to a Netscape cookie file for yt-dlp (--cookies).

    Chrome locks its SQLite WAL file while running, but SQLite allows read-only
    URI connections in WAL mode.  Cookie values are AES-256-GCM encrypted with
    a key stored in 'Local State' (itself DPAPI-wrapped), so we decrypt them
    here instead of relying on yt-dlp's --cookies-from-browser (which tries to
    copy the locked file).

    Returns the temp file path, or None on any failure.
    """
    appdata = os.environ.get('LOCALAPPDATA', '')
    candidates = [
        os.path.join(appdata, 'Google', 'Chrome', 'User Data', 'Default', 'Network', 'Cookies'),
        os.path.join(appdata, 'Google', 'Chrome', 'User Data', 'Default', 'Cookies'),
    ]
    cookie_db = next((p for p in candidates if os.path.exists(p)), None)
    if not cookie_db:
        log.warning('Chrome Cookies DB not found.')
        return None

    aes_key = _get_chrome_aes_key()
    if not aes_key:
        return None

    # Copy DB to a temp file — avoids SQLite URI encoding issues on Windows
    # (spaces in "User Data" path) and works with Chrome's WAL lock.
    fd_tmp, tmp_db = tempfile.mkstemp(suffix='.sqlite', prefix='vd_ckdb_')
    os.close(fd_tmp)
    rows = None
    try:
        shutil.copy2(cookie_db, tmp_db)
        con = sqlite3.connect(tmp_db)
        cur = con.cursor()
        cur.execute(
            'SELECT host_key, path, is_secure, expires_utc, name, encrypted_value '
            'FROM cookies'
        )
        rows = cur.fetchall()
        con.close()
    except Exception as exc:
        log.warning('Could not query Chrome cookies DB: %s', exc)
    finally:
        try:
            os.unlink(tmp_db)
        except OSError:
            pass

    if not rows:
        return None

    fd, path = tempfile.mkstemp(suffix='.txt', prefix='vd_cookies_')
    written = 0
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write('# Netscape HTTP Cookie File\n')
            for host_key, cookie_path, is_secure, expires_utc, name, enc_val in rows:
                value = _decrypt_chrome_value(enc_val, aes_key)
                # Chrome epoch: microseconds since 1601-01-01 → Unix seconds
                epoch = max(0, (expires_utc - 11_644_473_600_000_000) // 1_000_000) if expires_utc else 0
                secure   = 'TRUE' if is_secure else 'FALSE'
                subdomain = 'TRUE' if host_key.startswith('.') else 'FALSE'
                f.write(f'{host_key}\t{subdomain}\t{cookie_path}\t{secure}\t{epoch}\t{name}\t{value}\n')
                written += 1
        log.debug('Exported %d Chrome cookies to %s', written, path)
        return path
    except Exception as exc:
        log.warning('Could not write cookie file: %s', exc)
        try:
            os.unlink(path)
        except OSError:
            pass
        return None


def _preflight_url(
    url:           str,
    cookie_file:   str | None,
    referer:       str | None,
    cookie_header: str | None = None,
) -> None:
    """
    Diagnostic: make a small ranged GET to *url* using the same auth that
    yt-dlp will use and log the HTTP response code.  Never raises.
    """
    try:
        import http.cookiejar
        import urllib.error
        import urllib.request
        from urllib.parse import urlparse

        hdrs = [
            ('User-Agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                           'AppleWebKit/537.36 (KHTML, like Gecko) '
                           'Chrome/137.0.0.0 Safari/537.36'),
            ('Range', 'bytes=0-1023'),
        ]
        if referer:
            hdrs.append(('Referer', referer))
            try:
                p = urlparse(referer)
                hdrs.append(('Origin', f'{p.scheme}://{p.netloc}'))
            except Exception:
                pass

        if cookie_header:
            # Use the raw browser-captured Cookie header directly.
            hdrs.append(('Cookie', cookie_header))
            log.debug('preflight: using captured Cookie header (%d chars)', len(cookie_header))
            opener = urllib.request.build_opener()
            opener.addheaders = hdrs
        else:
            cj = http.cookiejar.MozillaCookieJar()
            if cookie_file:
                try:
                    cj.load(cookie_file, ignore_discard=True, ignore_expires=True)
                    log.debug('preflight: loaded %d cookies from file', len(list(cj)))
                except Exception as exc:
                    log.debug('preflight: cookie load error: %s', exc)
            opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
            opener.addheaders = hdrs

        resp = opener.open(urllib.request.Request(url), timeout=15)
        log.debug('preflight: %s → HTTP %d  content-type=%s',
                  url[:120], resp.status,
                  resp.headers.get('Content-Type', '?'))
        resp.close()
    except urllib.error.HTTPError as exc:
        log.debug('preflight: %s → HTTP %d %s', url[:120], exc.code, exc.reason)
    except Exception as exc:
        log.debug('preflight: %s → %s', url[:120], exc)


def _parse_percent(line: str) -> float | None:
    m = re.search(r'(\d+(?:\.\d+)?)%', line)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    return None
