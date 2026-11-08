"""Bounds-checked little-endian reads over an immutable byte buffer."""
from __future__ import annotations

import struct

Q12_SCALE = 4096.0  # candidate signed fixed-point: value = s32 / 4096


class ReadOutOfBounds(Exception):
    def __init__(self, offset: int, width: int, length: int, what: str = ""):
        label = f" ({what})" if what else ""
        super().__init__(f"read of {width} byte(s) at 0x{offset:X}{label} outside buffer of 0x{length:X}")
        self.offset = offset
        self.width = width
        self.length = length


class BinReader:
    """Read-only view. Every read checks ``0 <= offset`` and ``offset + width <= len``."""

    __slots__ = ("_data", "name")

    def __init__(self, data: bytes, name: str = ""):
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError("BinReader needs a bytes-like object")
        self._data = bytes(data)
        self.name = name

    def __len__(self) -> int:
        return len(self._data)

    @property
    def data(self) -> bytes:
        return self._data

    def fits(self, offset: int, width: int) -> bool:
        return offset >= 0 and width >= 0 and offset + width <= len(self._data)

    def _check(self, offset: int, width: int, what: str) -> None:
        if not isinstance(offset, int) or not self.fits(offset, width):
            raise ReadOutOfBounds(offset if isinstance(offset, int) else -1, width, len(self._data), what)

    def u8(self, offset: int, what: str = "") -> int:
        self._check(offset, 1, what)
        return self._data[offset]

    def u16(self, offset: int, what: str = "") -> int:
        self._check(offset, 2, what)
        return struct.unpack_from("<H", self._data, offset)[0]

    def u32(self, offset: int, what: str = "") -> int:
        self._check(offset, 4, what)
        return struct.unpack_from("<I", self._data, offset)[0]

    def s16(self, offset: int, what: str = "") -> int:
        self._check(offset, 2, what)
        return struct.unpack_from("<h", self._data, offset)[0]

    def s32(self, offset: int, what: str = "") -> int:
        self._check(offset, 4, what)
        return struct.unpack_from("<i", self._data, offset)[0]

    def q12(self, offset: int, what: str = "") -> float:
        return self.s32(offset, what) / Q12_SCALE

    def slice(self, offset: int, length: int, what: str = "") -> bytes:
        self._check(offset, length, what)
        return self._data[offset:offset + length]

    def is_zero(self, offset: int, length: int) -> bool:
        self._check(offset, length, "zero-check")
        return not any(self._data[offset:offset + length])

    def cstring(self, offset: int, max_len: int = 256):
        """NUL-terminated printable-ASCII string, or None if absent/invalid."""
        if not self.fits(offset, 1):
            return None
        end = self._data.find(b"\x00", offset, min(len(self._data), offset + max_len))
        if end < 0:
            return None
        raw = self._data[offset:end]
        if not all(0x20 <= b < 0x7F for b in raw):
            return None
        return raw.decode("ascii")


def align_up(value: int, alignment: int) -> int:
    if alignment <= 0 or alignment & (alignment - 1):
        raise ValueError("alignment must be a positive power of two")
    return (value + alignment - 1) & ~(alignment - 1)
