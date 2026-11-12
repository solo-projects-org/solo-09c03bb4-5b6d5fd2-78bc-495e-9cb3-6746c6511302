#!/usr/bin/env python3
"""Generate ``sample_cards/rain-card-demo.png`` with the standard library only.

The archive app accepts real scan formats (PNG/JPEG/GIF/WebP) but deliberately
rejects scriptable SVG uploads. This script renders a paper-like daily rainfall
card as a true PNG so reviewers can try the upload-and-resend flow without
preparing an image. Run from anywhere; output goes next to the SVG samples:

    python3 tools/make_demo_scan.py
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

WIDTH, HEIGHT = 900, 600

# A compact 5x7 bitmap font. ASCII digits, uppercase letters and the few
# punctuation marks the card needs. Anything missing renders as a blank glyph.
FONT: dict[str, list[str]] = {
    "0": ["01110", "10001", "10011", "10101", "11001", "10001", "01110"],
    "1": ["00100", "01100", "00100", "00100", "00100", "00100", "01110"],
    "2": ["01110", "10001", "00001", "00010", "00100", "01000", "11111"],
    "3": ["11110", "00001", "00001", "01110", "00001", "00001", "11110"],
    "4": ["00010", "00110", "01010", "10010", "11111", "00010", "00010"],
    "5": ["11111", "10000", "11110", "00001", "00001", "10001", "01110"],
    "6": ["00110", "01000", "10000", "11110", "10001", "10001", "01110"],
    "7": ["11111", "00001", "00010", "00100", "01000", "01000", "01000"],
    "8": ["01110", "10001", "10001", "01110", "10001", "10001", "01110"],
    "9": ["01110", "10001", "10001", "01111", "00001", "00010", "01100"],
    "A": ["01110", "10001", "10001", "11111", "10001", "10001", "10001"],
    "B": ["11110", "10001", "10001", "11110", "10001", "10001", "11110"],
    "C": ["01111", "10000", "10000", "10000", "10000", "10000", "01111"],
    "D": ["11110", "10001", "10001", "10001", "10001", "10001", "11110"],
    "E": ["11111", "10000", "10000", "11110", "10000", "10000", "11111"],
    "F": ["11111", "10000", "10000", "11110", "10000", "10000", "10000"],
    "G": ["01111", "10000", "10000", "10111", "10001", "10001", "01111"],
    "H": ["10001", "10001", "10001", "11111", "10001", "10001", "10001"],
    "I": ["11111", "00100", "00100", "00100", "00100", "00100", "11111"],
    "L": ["10000", "10000", "10000", "10000", "10000", "10000", "11111"],
    "M": ["10001", "11011", "10101", "10101", "10001", "10001", "10001"],
    "N": ["10001", "11001", "10101", "10011", "10001", "10001", "10001"],
    "O": ["01110", "10001", "10001", "10001", "10001", "10001", "01110"],
    "P": ["11110", "10001", "10001", "11110", "10000", "10000", "10000"],
    "R": ["11110", "10001", "10001", "11110", "10100", "10010", "10001"],
    "S": ["01111", "10000", "10000", "01110", "00001", "00001", "11110"],
    "T": ["11111", "00100", "00100", "00100", "00100", "00100", "00100"],
    "U": ["10001", "10001", "10001", "10001", "10001", "10001", "01110"],
    "V": ["10001", "10001", "10001", "10001", "10001", "01010", "00100"],
    "W": ["10001", "10001", "10001", "10101", "10101", "11011", "10001"],
    "Y": ["10001", "10001", "01010", "00100", "00100", "00100", "00100"],
    "-": ["00000", "00000", "00000", "11111", "00000", "00000", "00000"],
    ".": ["00000", "00000", "00000", "00000", "00000", "00110", "00110"],
    "/": ["00001", "00010", "00010", "00100", "01000", "01000", "10000"],
    ":": ["00000", "00110", "00110", "00000", "00110", "00110", "00000"],
    "(": ["00010", "00100", "01000", "01000", "01000", "00100", "00010"],
    ")": ["01000", "00100", "00010", "00010", "00010", "00100", "01000"],
    " ": ["00000", "00000", "00000", "00000", "00000", "00000", "00000"],
}

PAPER = (231, 224, 207)
PANEL = (248, 243, 229)
INK = (37, 59, 51)
MUTED = (123, 113, 95)
RULE = (184, 174, 152)
RED = (173, 58, 46)


class Canvas:
    def __init__(self, width: int, height: int, fill: tuple[int, int, int]):
        self.width = width
        self.height = height
        self.pixels = bytearray(fill * width * height)

    def _set(self, x: int, y: int, color: tuple[int, int, int]) -> None:
        if 0 <= x < self.width and 0 <= y < self.height:
            offset = (y * self.width + x) * 3
            self.pixels[offset : offset + 3] = bytes(color)

    def rect(
        self,
        x: int,
        y: int,
        w: int,
        h: int,
        color: tuple[int, int, int],
        outline: tuple[int, int, int] | None = None,
    ) -> None:
        for py in range(y, min(y + h, self.height)):
            for px in range(x, min(x + w, self.width)):
                self._set(px, py, color)
        if outline is not None:
            self.hline(x, x + w - 1, y, outline)
            self.hline(x, x + w - 1, y + h - 1, outline)
            self.vline(x, y, y + h - 1, outline)
            self.vline(x + w - 1, y, y + h - 1, outline)

    def hline(self, x0: int, x1: int, y: int, color: tuple[int, int, int]) -> None:
        for px in range(max(0, x0), min(self.width, x1 + 1)):
            self._set(px, y, color)

    def vline(self, x: int, y0: int, y1: int, color: tuple[int, int, int]) -> None:
        for py in range(max(0, y0), min(self.height, y1 + 1)):
            self._set(x, py, color)

    def line(
        self, x0: int, y0: int, x1: int, y1: int, color: tuple[int, int, int]
    ) -> None:
        steps = max(abs(x1 - x0), abs(y1 - y0), 1)
        for i in range(steps + 1):
            x = round(x0 + (x1 - x0) * i / steps)
            y = round(y0 + (y1 - y0) * i / steps)
            self._set(x, y, color)

    def circle(
        self, cx: int, cy: int, radius: int, color: tuple[int, int, int], thickness: int = 1
    ) -> None:
        for py in range(cy - radius, cy + radius + 1):
            for px in range(cx - radius, cx + radius + 1):
                dist = ((px - cx) ** 2 + (py - cy) ** 2) ** 0.5
                if radius - thickness < dist <= radius:
                    self._set(px, py, color)

    def text(
        self,
        x: int,
        y: int,
        text: str,
        color: tuple[int, int, int],
        scale: int = 2,
    ) -> None:
        cursor = x
        for ch in text.upper():
            glyph = FONT.get(ch, FONT[" "])
            for row, bits in enumerate(glyph):
                for col, bit in enumerate(bits):
                    if bit == "1":
                        self.rect(
                            cursor + col * scale,
                            y + row * scale,
                            scale,
                            scale,
                            color,
                        )
            cursor += 6 * scale

    def to_png(self) -> bytes:
        raw = bytearray()
        stride = self.width * 3
        for y in range(self.height):
            raw.append(0)  # filter type 0 (None) for each scanline
            raw.extend(self.pixels[y * stride : (y + 1) * stride])

        def chunk(tag: bytes, payload: bytes) -> bytes:
            return (
                struct.pack(">I", len(payload))
                + tag
                + payload
                + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
            )

        ihdr = struct.pack(">IIBBBBB", self.width, self.height, 8, 2, 0, 0, 0)
        return b"".join(
            [
                b"\x89PNG\r\n\x1a\n",
                chunk(b"IHDR", ihdr),
                chunk(b"IDAT", zlib.compress(bytes(raw), 9)),
                chunk(b"IEND", b""),
            ]
        )


def build() -> bytes:
    c = Canvas(WIDTH, HEIGHT, PAPER)
    c.rect(24, 22, WIDTH - 48, HEIGHT - 44, PANEL, outline=RULE)

    # Header.
    c.text(56, 50, "DAILY RAINFALL CARD", INK, scale=3)
    c.text(56, 86, "STATION ARCHIVE - DEMO SCAN", MUTED, scale=1)
    c.hline(56, WIDTH - 56, 112, INK)
    c.hline(56, WIDTH - 56, 115, INK)

    # Red archive stamp.
    c.circle(WIDTH - 110, 170, 44, RED, thickness=2)
    c.circle(WIDTH - 110, 170, 38, RED, thickness=1)
    c.text(WIDTH - 146, 158, "ARCHIVE", RED, scale=1)
    c.text(WIDTH - 134, 176, "DEMO", RED, scale=1)

    # Main fields.
    c.text(56, 150, "STATION NO", MUTED, scale=1)
    c.text(56, 170, "30501240", INK, scale=3)
    c.hline(56, 330, 210, RULE)

    c.text(380, 150, "OBS DATE", MUTED, scale=1)
    c.text(380, 170, "1986-07-18", INK, scale=3)
    c.hline(380, 680, 210, RULE)

    c.text(56, 250, "RAINFALL / MM", MUTED, scale=1)
    c.text(60, 280, "17.6", INK, scale=8)
    c.line(60, 360, 270, 356, INK)
    c.text(300, 300, "MM", MUTED, scale=3)

    # Notes panel.
    c.hline(56, WIDTH - 56, 400, RULE)
    c.text(56, 424, "METHOD", MUTED, scale=1)
    c.text(56, 444, "RAIN GAUGE 20CM, 08H-08H", INK, scale=2)
    c.text(56, 486, "NOTE", MUTED, scale=1)
    c.text(56, 506, "SHOWERS IN AFTERNOON", INK, scale=2)

    # Footer.
    c.hline(56, WIDTH - 56, 548, RULE)
    c.text(56, 562, "CARD NO YC-1986-0718 - SAMPLE REPLICA - NOT A REAL OBSERVATION", MUTED, scale=1)
    return c.to_png()


def main() -> None:
    output = Path(__file__).resolve().parent.parent / "sample_cards" / "rain-card-demo.png"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(build())
    print(f"wrote {output} ({output.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
