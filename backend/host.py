"""
host.py — Chrome Native Messaging host for Video Downloader.

Chrome communicates with this process over stdin/stdout using the Native
Messaging protocol: each message is prefixed with a 4-byte little-endian
unsigned integer that gives the byte length of the JSON payload that follows.

This script should NOT be run directly — it is launched by Chrome when the
extension calls chrome.runtime.connectNative('com.videodownloader.host').
To test manually: run install/install.bat first, then load the extension.
"""

import importlib
import sys
import json
import struct
import threading
import traceback

import downloader as _downloader_mod   # kept as module ref so importlib.reload() works


# ─── Native Messaging I/O ─────────────────────────────────────────────────────

def read_message() -> dict | None:
    """Block until a complete message arrives on stdin, return parsed dict."""
    raw_length = sys.stdin.buffer.read(4)
    if len(raw_length) < 4:
        return None                                    # Chrome closed the pipe
    length = struct.unpack('<I', raw_length)[0]        # little-endian uint32
    payload = sys.stdin.buffer.read(length)
    return json.loads(payload.decode('utf-8'))


def send_message(obj: dict) -> None:
    """Serialise obj and write it to stdout in Native Messaging format."""
    payload = json.dumps(obj, ensure_ascii=False).encode('utf-8')
    sys.stdout.buffer.write(struct.pack('<I', len(payload)))
    sys.stdout.buffer.write(payload)
    sys.stdout.buffer.flush()


# ─── Request dispatch ─────────────────────────────────────────────────────────

def handle(message: dict) -> None:
    action = message.get('action', '')
    msg_id = message.get('id')           # forwarded from background.js

    def reply(obj: dict) -> None:
        if msg_id is not None:
            obj['id'] = msg_id
        send_message(obj)

    if action == 'ping':
        reply({'success': True, 'status': 'alive'})
        return

    if action == 'download':
        url              = message.get('url', '').strip()
        stream_type      = message.get('streamType', 'unknown')
        page_url         = message.get('pageUrl', '').strip()
        output_dir       = message.get('outputDir')
        cookies          = message.get('cookies') or []
        captured_headers = message.get('capturedHeaders') or []   # [{name,value}] from browser
        listener_fired   = message.get('listenerFired') or False
        all_header_names = message.get('allHeaderNames') or []

        if not url:
            reply({'success': False, 'error': 'No URL provided.'})
            return

        # Hot-reload downloader.py so edits take effect without restarting the host.
        importlib.reload(_downloader_mod)
        downloader = _downloader_mod.Downloader(output_dir)

        def progress_cb(data: dict) -> None:
            data['type'] = 'progress'
            if msg_id is not None:
                data['id'] = msg_id
            send_message(data)

        def run() -> None:
            try:
                result = downloader.download(url, stream_type, progress_cb,
                                             page_url=page_url or None,
                                             cookies=cookies or None,
                                             captured_headers=captured_headers,
                                             listener_fired=listener_fired,
                                             all_header_names=all_header_names)
            except Exception:
                result = {'success': False, 'error': traceback.format_exc()}
            reply(result)

        t = threading.Thread(target=run, daemon=True)
        t.start()
        return

    reply({'success': False, 'error': f'Unknown action: {action!r}'})


# ─── Main loop ────────────────────────────────────────────────────────────────

def main() -> None:
    while True:
        try:
            message = read_message()
        except Exception:
            break                     # stdin closed or malformed data
        if message is None:
            break
        try:
            handle(message)
        except Exception:
            send_message({'success': False, 'error': traceback.format_exc()})


if __name__ == '__main__':
    main()
