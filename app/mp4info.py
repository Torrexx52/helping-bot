"""Чтение размеров и длительности из заголовка MP4/MOV без ffprobe.

Telegram без width/height может показать вертикальное видео 9:16 квадратом
или горизонтально, поэтому размеры передаются явно.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"edts"}


@dataclass(frozen=True)
class VideoInfo:
    width: int
    height: int
    duration: int  # секунды, округлено


def _boxes(f: BinaryIO, start: int, end: int):
    pos = start
    while pos + 8 <= end:
        f.seek(pos)
        head = f.read(8)
        if len(head) < 8:
            return
        size, kind = struct.unpack(">I4s", head)
        hdr = 8
        if size == 1:
            size = struct.unpack(">Q", f.read(8))[0]
            hdr = 16
        elif size == 0:
            size = end - pos
        if size < hdr:
            return
        yield kind, pos + hdr, pos + size
        pos += size


def _tkhd(data: bytes) -> tuple[int, int] | None:
    version = data[0]
    off = 4 + (32 if version == 1 else 20)  # version/flags + времена, track_id, duration
    off += 8 + 8  # reserved, layer, alternate_group, volume, reserved
    a, b, _u, c, d = struct.unpack(">iii ii", data[off : off + 20])
    w, h = struct.unpack(">II", data[off + 36 : off + 44])
    w, h = w >> 16, h >> 16
    if not w or not h:
        return None
    if a == 0 and d == 0 and abs(b) == 0x10000 and abs(c) == 0x10000:  # повёрнуто на 90°
        w, h = h, w
    return w, h


def _mvhd(data: bytes) -> int:
    if data[0] == 1:
        timescale, duration = struct.unpack(">IQ", data[20:32])
    else:
        timescale, duration = struct.unpack(">II", data[12:20])
    return round(duration / timescale) if timescale else 0


def read_video_info(path: Path) -> VideoInfo | None:
    """Размеры видеодорожки и длительность; None, если файл не удалось разобрать."""
    try:
        with path.open("rb") as f:
            f.seek(0, 2)
            end = f.tell()
            size: tuple[int, int] | None = None
            duration = 0

            def walk(start: int, stop: int) -> None:
                nonlocal size, duration
                for kind, s, e in _boxes(f, start, stop):
                    if kind == b"mvhd":
                        f.seek(s)
                        duration = _mvhd(f.read(min(e - s, 64)))
                    elif kind == b"tkhd" and size is None:
                        f.seek(s)
                        size = _tkhd(f.read(min(e - s, 128)))
                    elif kind in CONTAINERS:
                        walk(s, e)

            for kind, s, e in _boxes(f, 0, end):
                if kind == b"moov":
                    walk(s, e)
                    break
    except (OSError, struct.error, IndexError):
        return None
    if size is None:
        return None
    return VideoInfo(size[0], size[1], duration)
