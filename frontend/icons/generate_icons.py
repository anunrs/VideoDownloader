"""
generate_icons.py — creates the three PNG icons required by the extension.
Run this once from the icons/ directory:  python generate_icons.py

No external dependencies — uses only Python standard library.
"""

import struct
import zlib
import os

# Brand colour: #e94560  (R=233, G=69, B=96)
ICON_COLOR = (233, 69, 96, 255)   # RGBA
SIZES = [16, 48, 128]


def make_png(width: int, height: int, color: tuple[int, int, int, int]) -> bytes:
    """Return the bytes of a minimal solid-colour RGBA PNG."""

    def chunk(name: bytes, data: bytes) -> bytes:
        payload = name + data
        crc     = zlib.crc32(payload) & 0xFFFFFFFF
        return struct.pack('>I', len(data)) + payload + struct.pack('>I', crc)

    r, g, b, a = color

    signature = b'\x89PNG\r\n\x1a\n'

    # IHDR: width(4) height(4) bit-depth(1) color-type(1=RGBA=6) compress(1) filter(1) interlace(1)
    ihdr = chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 6, 0, 0, 0))

    # Raw image data: one filter-None byte per row, then width * 4 pixel bytes
    row  = b'\x00' + bytes([r, g, b, a]) * width
    raw  = row * height
    idat = chunk(b'IDAT', zlib.compress(raw, level=9))

    iend = chunk(b'IEND', b'')

    return signature + ihdr + idat + iend


def main():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    for size in SIZES:
        path = os.path.join(script_dir, f'icon{size}.png')
        with open(path, 'wb') as f:
            f.write(make_png(size, size, ICON_COLOR))
        print(f'Created {path}')
    print('Done.')


if __name__ == '__main__':
    main()
