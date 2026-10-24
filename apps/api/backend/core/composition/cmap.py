"""Read cmap format 4 and 12. Glyph id 0 is .notdef and does not count as coverage."""

from __future__ import annotations

import struct
from pathlib import Path


def covered_codepoints(path: Path) -> set[int]:
    data = path.read_bytes()
    tables = _tables(data)
    cmap = _slice(data, tables, b"cmap")
    _version, count = struct.unpack_from(">HH", cmap, 0)
    best = None
    for index in range(count):
        platform_id, encoding_id, offset = struct.unpack_from(">HHI", cmap, 4 + index * 8)
        fmt = struct.unpack_from(">H", cmap, offset)[0]
        rank = 3 if fmt == 12 else 2 if fmt == 4 else 0
        if platform_id in {0, 3} and rank and (best is None or rank > best[0]):
            best = (rank, offset, fmt)
    if best is None:
        return set()
    _rank, offset, fmt = best
    if fmt == 12:
        return _format12(cmap, offset)
    return _format4(cmap, offset)


def _tables(data: bytes) -> dict[bytes, tuple[int, int]]:
    scaler = struct.unpack_from(">I", data, 0)[0]
    if scaler not in {0x00010000, 0x4F54544F}:
        raise ValueError("unsupported font outline")
    count = struct.unpack_from(">H", data, 4)[0]
    tables = {}
    cursor = 12
    for _ in range(count):
        tag, _checksum, offset, length = struct.unpack_from(">4sIII", data, cursor)
        tables[tag] = (offset, length)
        cursor += 16
    return tables


def _slice(data: bytes, tables: dict[bytes, tuple[int, int]], tag: bytes) -> bytes:
    offset, length = tables[tag]
    return data[offset:offset + length]


def _format12(cmap: bytes, offset: int) -> set[int]:
    _fmt, _reserved, _length, _language, groups = struct.unpack_from(">HHIII", cmap, offset)
    covered: set[int] = set()
    cursor = offset + 16
    for _ in range(groups):
        start, end, glyph = struct.unpack_from(">III", cmap, cursor)
        cursor += 12
        # Sequential glyph ids. Skip .notdef (0) only.
        for codepoint in range(start, end + 1):
            if glyph + (codepoint - start) != 0:
                covered.add(codepoint)
    return covered


def _format4(cmap: bytes, offset: int) -> set[int]:
    _fmt, _length, _language, seg_x2 = struct.unpack_from(">HHHH", cmap, offset)
    segments = seg_x2 // 2
    end_at = offset + 14
    ends = struct.unpack_from(f">{segments}H", cmap, end_at)
    start_at = end_at + seg_x2 + 2
    starts = struct.unpack_from(f">{segments}H", cmap, start_at)
    delta_at = start_at + seg_x2
    deltas = struct.unpack_from(f">{segments}h", cmap, delta_at)
    range_at = delta_at + seg_x2
    ranges = struct.unpack_from(f">{segments}H", cmap, range_at)
    covered: set[int] = set()
    for index, (start, end) in enumerate(zip(starts, ends, strict=True)):
        if start == 0xFFFF:
            continue
        for codepoint in range(start, end + 1):
            if ranges[index] == 0:
                glyph = (codepoint + deltas[index]) & 0xFFFF
            else:
                pos = range_at + index * 2 + ranges[index] + (codepoint - start) * 2
                glyph = struct.unpack_from(">H", cmap, pos)[0]
                if glyph:
                    glyph = (glyph + deltas[index]) & 0xFFFF
            if glyph:
                covered.add(codepoint)
    return covered
