#!/usr/bin/env python3
"""Draw the PDV2 symbol (web/assets/pdv2-simbolo.svg) as a multi-size .ico.

Pure standard library, so the icon can be regenerated on any build machine:
    python installer/make_icon.py [destino.ico]
"""
import math
import struct
import sys
import zlib
from pathlib import Path

SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)
GREEN, WHITE, MINT = (0x14, 0x87, 0x65), (0xFF, 0xFF, 0xFF), (0xD9, 0xF4, 0xE6)
RADIUS, STROKE = 15.0, 3.4 * 1.36


def _place(points):
    return [(5.5 + 1.36 * x, 5.5 + 1.36 * y) for x, y in points]


def _cubic(p0, p1, p2, p3, steps=24):
    points = []
    for index in range(1, steps + 1):
        t = index / steps
        a, b, c, d = (1 - t) ** 3, 3 * t * (1 - t) ** 2, 3 * t * t * (1 - t), t ** 3
        points.append((a * p0[0] + b * p1[0] + c * p2[0] + d * p3[0],
                       a * p0[1] + b * p1[1] + c * p2[1] + d * p3[1]))
    return points


# "P": M8 28V8h11a7 7 0 0 1 0 14H8
LETTER_P = _place([(8, 28), (8, 8), (19, 8)] +
                  [(19 + 7 * math.cos(math.radians(a)), 15 + 7 * math.sin(math.radians(a)))
                   for a in range(-84, 91, 6)] + [(8, 22)])
# "2": M23 23c3-4 8-2 8 1 0 3-6 4-8 7h8
DIGIT_2 = _place([(23, 23)] + _cubic((23, 23), (26, 19), (31, 21), (31, 24)) +
                 _cubic((31, 24), (31, 27), (25, 28), (23, 31)) + [(31, 31)])


def _segment_distance(px, py, a, b):
    ax, ay = a
    dx, dy = b[0] - ax, b[1] - ay
    length = dx * dx + dy * dy
    t = 0.0 if length == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length))
    return math.hypot(px - ax - t * dx, py - ay - t * dy)


def _stroke(px, py, path, scale):
    distance = min(_segment_distance(px, py, path[i], path[i + 1]) for i in range(len(path) - 1))
    return max(0.0, min(1.0, 0.5 - (distance - STROKE / 2) * scale))


def _background(px, py, scale):
    qx, qy = abs(px - 32) - (32 - RADIUS), abs(py - 32) - (32 - RADIUS)
    distance = math.hypot(max(qx, 0), max(qy, 0)) + min(max(qx, qy), 0) - RADIUS
    return max(0.0, min(1.0, 0.5 - distance * scale))


def render(size):
    """Rows of (r, g, b, a) with distance-based antialiasing."""
    scale = size / 64
    rows = []
    for y in range(size):
        row = []
        for x in range(size):
            px, py = (x + 0.5) / scale, (y + 0.5) / scale
            alpha = _background(px, py, scale)
            color = GREEN
            for path, ink in ((LETTER_P, WHITE), (DIGIT_2, MINT)):
                cover = _stroke(px, py, path, scale)
                color = tuple(round(c * (1 - cover) + i * cover) for c, i in zip(color, ink))
            row.append((*color, round(alpha * 255)))
        rows.append(row)
    return rows


def png(rows):
    size = len(rows)
    raw = b"".join(b"\x00" + bytes(v for pixel in row for v in pixel) for row in rows)

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def _dib(rows):
    size = len(rows)
    header = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0, 0, 0, 0, 0, 0)
    pixels = b"".join(bytes(v for r, g, b, a in row for v in (b, g, r, a)) for row in reversed(rows))
    mask = bytes(((size + 31) // 32) * 4) * size
    return header + pixels + mask


def build_ico(sizes=SIZES):
    # Classic bitmaps below 256 px keep old resource compilers happy; 256 px must be PNG.
    images = [(size, png(render(size)) if size >= 256 else _dib(render(size))) for size in sizes]
    offset = 6 + 16 * len(images)
    directory = struct.pack("<HHH", 0, 1, len(images))
    for size, data in images:
        directory += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    return directory + b"".join(data for _, data in images)


def main():
    arguments = [value for value in sys.argv[1:] if not value.startswith("--")]
    target = Path(arguments[0]) if arguments else Path(__file__).with_name("pdv2.ico")
    target.write_bytes(build_ico())
    print(f"Ícone gerado: {target} ({target.stat().st_size} bytes)")
    if "--png" in sys.argv:
        target.with_suffix(".png").write_bytes(png(render(256)))


if __name__ == "__main__":
    main()
