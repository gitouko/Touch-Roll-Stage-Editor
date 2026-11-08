"""Per-group collision grid ("cell table") of an LCTS stage: reader and footprint rules.

What the game does (ARM9, USA rev 0 — see spec §16):
  * descriptor +0x40 s16 nx, +0x42 s16 nz, +0x44/+0x48 Q20.12 origin x/z, +0x4C/+0x50 Q20.12 cell
    size x/z, +0x54 offset of nx*nz cell entries (8 bytes: u32 type mask, u32 offset of an array of
    offsets, one per set mask bit, in bit order). The loader 0x02046CC4 relocates the array offset and
    every entry of the array.
  * each array entry points to a list of u16 indices ending in 0xFFFF (0 = empty for bits 0-3).
  * cell lookup 0x02044C68: bits 0-3 are always present; bit n > 3 is present only if set in the mask.
  * collision 0x020444F4 (ball vs stage) uses bit 0 -> table +0x90, bit 1 -> table +0x88 (0x34-byte
    records), bit 2 -> table +0x80 (0x20), bit 3 -> table +0x78 (0x10), bit 4 -> goals (pool A slice,
    0x34), bit 5 -> bumpers (pool B slice, 0x30); banana pickup 0x020AC0F4 uses bit 6 -> pool C slice.
    Indices are local to the group's slice. The ball only tests objects listed in the cell it is in.

Footprint (Level 2, reproduced from the stage files): a cell lists an object when the cell's index
range overlaps [p - R, p + R] in x and z (floor of (value - origin) / size, clamped to the grid), with
R = 3.5 for bananas and bumpers, and for goals the axis-aligned extent of a 3.5-unit cube turned
with the goal. Stored (rest-pose) coordinates are used for every group.
"""
from __future__ import annotations

import struct
from typing import Dict, List, Optional, Set, Tuple

FX = 4096
MARGIN = int(3.5 * FX)          # Q20.12 footprint half-size for bananas and bumpers
END = 0xFFFF
BIT_NAMES = {0: "table_8C", 1: "table_84", 2: "table_7C", 3: "table_74", 4: "goal", 5: "bumper", 6: "banana"}
KIND_BIT = {"goal": 4, "bumper": 5, "banana": 6}
KIND_POOL = {"goal": "A", "bumper": "B", "banana": "C"}
MAX_LIST = 1 << 16


class CellError(Exception):
    pass


def _u32(b, o):
    return struct.unpack_from("<I", b, o)[0]


def _i32(b, o):
    return struct.unpack_from("<i", b, o)[0]


def _read_list(b: bytes, off: int) -> List[int]:
    out = []
    while True:
        if off + 2 * len(out) + 2 > len(b):
            raise CellError(f"list at 0x{off:X} runs past the end of the file")
        v = struct.unpack_from("<H", b, off + 2 * len(out))[0]
        if v == END:
            return out
        out.append(v)
        if len(out) > MAX_LIST:
            raise CellError(f"list at 0x{off:X} has no end marker")


class Grid:
    """One group's grid, read straight from the decoded bytes (raw Q20.12 integers kept)."""

    def __init__(self, data: bytes, descriptor_offset: int, group: int):
        b = data
        d = descriptor_offset
        self.group = group
        self.nx, self.nz = struct.unpack_from("<hh", b, d + 0x40)
        self.ox, self.oz, self.sx, self.sz = (_i32(b, d + o) for o in (0x44, 0x48, 0x4C, 0x50))
        self.table = _u32(b, d + 0x54)
        self.cells: List[Tuple[int, int, Dict[int, Tuple[int, Optional[List[int]]]]]] = []
        if not self.table:
            return
        if self.nx <= 0 or self.nz <= 0 or self.sx <= 0 or self.sz <= 0:
            raise CellError(f"group {group}: bad grid {self.nx}x{self.nz} step {self.sx},{self.sz}")
        if self.table + 8 * self.nx * self.nz > len(b):
            raise CellError(f"group {group}: cell table runs past the end of the file")
        for c in range(self.nx * self.nz):
            mask, arr = _u32(b, self.table + 8 * c), _u32(b, self.table + 8 * c + 4)
            lists = {}
            bits = [i for i in range(32) if mask >> i & 1]
            for k, bit in enumerate(bits):
                lp = _u32(b, arr + 4 * k) if arr else 0
                lists[bit] = (lp, _read_list(b, lp) if lp else None)
            self.cells.append((mask, arr, lists))

    @property
    def present(self) -> bool:
        return bool(self.cells)

    def cell_rect(self, c: int) -> Tuple[float, float, float, float]:
        i, j = c % self.nx, c // self.nx
        return ((self.ox + i * self.sx) / FX, (self.ox + (i + 1) * self.sx) / FX,
                (self.oz + j * self.sz) / FX, (self.oz + (j + 1) * self.sz) / FX)

    def listed(self, bit: int, local_index: int) -> Set[int]:
        out = set()
        for c, (mask, _arr, lists) in enumerate(self.cells):
            if mask >> bit & 1:
                lst = lists.get(bit, (0, None))[1]
                if lst and local_index in lst:
                    out.add(c)
        return out

    def footprint(self, x_raw: int, z_raw: int, rx_raw: int = MARGIN, rz_raw: int = MARGIN) -> Set[int]:
        """Cells whose index range overlaps [x - rx, x + rx] x [z - rz, z + rz] (Q20.12 integers)."""
        if not self.present:
            return set()
        i0 = max(0, (x_raw - rx_raw - self.ox) // self.sx)
        i1 = min(self.nx - 1, (x_raw + rx_raw - self.ox) // self.sx)
        j0 = max(0, (z_raw - rz_raw - self.oz) // self.sz)
        j1 = min(self.nz - 1, (z_raw + rz_raw - self.oz) // self.sz)
        return {j * self.nx + i for i in range(i0, i1 + 1) for j in range(j0, j1 + 1)}

    def within(self, x: float, z: float, reach: float) -> Set[int]:
        """Cells whose rectangle comes within `reach` (box distance) of (x, z)."""
        out = set()
        for c in range(len(self.cells)):
            x0, x1, z0, z1 = self.cell_rect(c)
            if max(x0 - x, 0.0, x - x1) <= reach and max(z0 - z, 0.0, z - z1) <= reach:
                out.add(c)
        return out


def goal_extent(matrix_raw: List[int], margin: int = MARGIN) -> Tuple[int, int]:
    """Half-extents (x, z) of a 3.5-unit cube turned with a goal (stored 3x3 matrix, row-major Q20.12)."""
    m = matrix_raw
    return ((abs(m[0]) + abs(m[3]) + abs(m[6])) * margin // FX, (abs(m[2]) + abs(m[5]) + abs(m[8])) * margin // FX)


def read_grids(data: bytes) -> List[Grid]:
    n, base = _u32(data, 0x14), _u32(data, 0x18)
    return [Grid(data, base + 0xCC * g, g) for g in range(n)]


# Reach around an object inside which the ball must be able to find it: ball radius (about 0.5)
# plus the object's own size, rounded up generously.
CONTACT_REACH = {"banana": 1.5, "bumper": 2.0, "goal": 2.5}


def coverage_problem(grid: Grid, kind: str, local_index: int, position, original=None) -> Optional[str]:
    """None if the object's ORIGINAL listed cells still cover the contact area around `position`
    (at least as well as around `original`, its stored position), otherwise a short explanation.
    Used to warn about moves that the game's collision lists would miss."""
    if not grid.present:
        return "this part has no collision grid"
    listed = grid.listed(KIND_BIT[kind], local_index)
    need = grid.within(position[0], position[2], CONTACT_REACH[kind])
    if not need:
        return "the new position is outside this part's collision grid"
    missing = need - listed
    base = len(grid.within(original[0], original[2], CONTACT_REACH[kind]) - listed) if original is not None else 0
    if len(missing) > base:
        return f"{len(missing) - base} collision cell(s) near the new position do not list this {kind}"
    return None


def survey_stage(data: bytes, doc) -> dict:
    """Check one stage's lists against the rules above; returns counters."""
    from collections import Counter
    c = Counter()
    grids = read_grids(data)
    limits = {0: 0x8C, 1: 0x84, 2: 0x7C, 3: 0x74}
    base = _u32(data, 0x18)
    for g in grids:
        if not g.present:
            c["groups without a grid"] += 1
            continue
        d = base + 0xCC * g.group
        counts = {bit: _u32(data, d + off) for bit, off in limits.items()}
        slices = {4: _u32(data, d + 0x94), 5: _u32(data, d + 0x9C), 6: _u32(data, d + 0xA4)}
        counts.update(slices)
        for mask, _arr, lists in g.cells:
            c["cells"] += 1
            if mask & 0xF != 0xF:
                c["cells without bits 0-3"] += 1
            if mask >> 7:
                c["cells with bits above 6"] += 1
            for bit, (_lp, lst) in lists.items():
                if not lst:
                    continue
                if lst != sorted(set(lst)):
                    c["lists not sorted / with repeats"] += 1
                if bit in counts and max(lst) >= counts[bit]:
                    c["list index out of range"] += 1
        for kind, bit in KIND_BIT.items():
            pool = KIND_POOL[kind]
            recs = [r for r in doc.pools[pool]["records"] if r["group"] == g.group]
            for li, r in enumerate(recs):
                c[f"{kind}s"] += 1
                listed = g.listed(bit, li)
                if not listed:
                    c[f"{kind}s in no cell"] += 1
                    continue
                o = int(r["offset"], 16) + (0x24 if pool in "AB" else 0)
                x, z = _i32(data, o), _i32(data, o + 8)
                rx = rz = MARGIN
                if kind == "goal":
                    rx, rz = goal_extent([_i32(data, int(r["offset"], 16) + 4 * k) for k in range(9)])
                fp = g.footprint(x, z, rx, rz)
                c[f"{kind}s exactly reproduced"] += listed == fp
                c[f"{kind}s within the rule's cells"] += listed <= fp
    return dict(c)
