"""Stage header values that edits may set (spec §2, §17.3).

* `bounds` (0x1C-0x30, min xyz then max xyz, Q20.12): read only by 0x02059BE8, which turns them into a
  centre and radius for the stage's intro camera. Level 2 from code; edit test 5 sets them for new stages.
"""
from __future__ import annotations

import struct
from typing import Dict, Tuple

from .document import StageDocument

FX = 4096
BOUNDS_OFFSET = 0x1C
LIMIT = (1 << 31) - 1


class HeaderEditRefused(Exception):
    pass


def read_bounds(data: bytes) -> Tuple[Tuple[float, float, float], Tuple[float, float, float]]:
    v = struct.unpack_from("<6i", data, BOUNDS_OFFSET)
    return tuple(x / FX for x in v[:3]), tuple(x / FX for x in v[3:])


def apply_header(decoded: bytes, header: Dict, name: str = "") -> Tuple[bytes, dict]:
    """header = {"bounds": {"min": [x, y, z], "max": [x, y, z]}} -> (new bytes, report)."""
    if not isinstance(header, dict) or set(header) - {"bounds"} or not header:
        raise HeaderEditRefused("header edits support only 'bounds'")
    b = header["bounds"]
    try:
        lo, hi = [float(v) for v in b["min"]], [float(v) for v in b["max"]]
    except (KeyError, TypeError, ValueError):
        raise HeaderEditRefused("bounds needs 'min' and 'max' as [x, y, z]") from None
    if len(lo) != 3 or len(hi) != 3 or any(not a < c for a, c in zip(lo, hi)):
        raise HeaderEditRefused("bounds: every min must be below its max")
    raw = [int(round(v * FX)) for v in lo + hi]
    if any(abs(v) > LIMIT for v in raw):
        raise HeaderEditRefused("bounds out of range")
    before = read_bounds(decoded)
    out = bytearray(decoded)
    struct.pack_into("<6i", out, BOUNDS_OFFSET, *raw)
    new = bytes(out)
    if any(new[i] != decoded[i] for i in range(len(decoded)) if not BOUNDS_OFFSET <= i < BOUNDS_OFFSET + 24):
        raise HeaderEditRefused("internal: bytes outside the bounds changed")
    doc = StageDocument(new, name)
    if doc.has_errors():
        raise HeaderEditRefused("edited stage has error diagnostics")
    return new, {"bounds_before": {"min": list(before[0]), "max": list(before[1])},
                 "bounds_after": {"min": [v / FX for v in raw[:3]], "max": [v / FX for v in raw[3:]]}}
