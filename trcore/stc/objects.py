"""Add, remove and freely move goals, bumpers and bananas in a decoded LCTS stage.

Rules this follows (spec §16; Level 2 from game code + corpus, edit test 3 pending):
  * each object pool stays ONE contiguous array in group order; every group's slice (count, offset)
    points at its run, and the header total equals the sum of the counts;
  * the ball only tests objects listed in the collision cell it is in (bit 4 goals, bit 5 bumpers,
    bit 6 bananas; u16 local indices, sorted, ending in 0xFFFF).

How (nothing that already exists is moved):
  * a changed pool is rebuilt and appended at the end of the file (old bytes stay, unreferenced);
  * for every group whose objects of that type changed, that type's lists are rebuilt: untouched
    objects keep exactly the cells they had, added or relisted objects get the footprint rule's cells
    plus one cell of margin, removed objects drop out and later indices shift down;
  * new lists and per-cell offset arrays are appended; only the 8-byte cell entries that change are
    rewritten in place.
Everything is checked before the bytes are returned (see _verify).

Operations (indices always refer to the stage AS LOADED, before any operation in the same call):
  {"op": "remove", "kind": "banana", "index": 4, "expect_position": [6, 0.5, 3.25]}
  {"op": "add", "kind": "banana", "group": 0, "position": [x, y, z], "bunch": false}
  {"op": "add", "kind": "bumper", "group": 0, "position": [x, y, z]}                  (identity matrix)
  {"op": "add", "kind": "goal", "group": 0, "position": [x, y, z], "rotation_like": 0, "red": false}
  {"op": "move", "kind": "banana", "index": 11, "from": [x, y, z], "to": [x, y, z]}   (with relisting)
"""
from __future__ import annotations

import hashlib
import struct
from typing import Dict, List, Optional, Set, Tuple

from .cells import FX, KIND_BIT, MARGIN, CellError, coverage_problem, goal_extent, read_grids, survey_stage
from .document import StageDocument

# kind -> (pool key, record size, position offset in record, descriptor slice offset, header total offset)
KINDS = {
    "goal": ("A", 0x34, 0x24, 0x94, 0x40),
    "bumper": ("B", 0x30, 0x24, 0x9C, 0x44),
    "banana": ("C", 0x10, 0x00, 0xA4, 0x48),
}
IDENTITY_RAW = (FX, 0, 0, 0, FX, 0, 0, 0, FX)
MAX_BANANA_VALUE = 255          # the game sums it into a byte (0x020ACB10)
Q12_LIMIT = 1 << 31


class ObjectEditRefused(Exception):
    pass


def _u32(b, o):
    return struct.unpack_from("<I", b, o)[0]


def _q(v) -> int:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ObjectEditRefused(f"coordinate must be a number, got {v!r}")
    i = int(round(v * FX))
    if not -Q12_LIMIT <= i < Q12_LIMIT:
        raise ObjectEditRefused(f"value {v} out of range")
    return i


def _vec(v, what) -> Tuple[int, int, int]:
    if not isinstance(v, (list, tuple)) or len(v) != 3:
        raise ObjectEditRefused(f"{what} needs [x, y, z]")
    return tuple(_q(x) for x in v)


class _Entry:
    __slots__ = ("group", "data", "orig", "relist", "added")

    def __init__(self, group, data, orig=None, relist=False, added=False):
        self.group, self.data, self.orig, self.relist, self.added = group, bytearray(data), orig, relist, added


def _new_record(kind: str, op: dict, entries: List[_Entry], data: bytes) -> bytes:
    pool, size, poff, _s, _t = KINDS[kind]
    x, y, z = _vec(op.get("position"), "position")
    rec = bytearray(size)
    if kind == "banana":
        struct.pack_into("<iiiI", rec, 0, x, y, z, 1 if op.get("bunch") else 0)
    else:
        mat = IDENTITY_RAW
        if kind == "goal" and op.get("rotation_like") is not None:
            src = [e for e in entries if e.orig == op["rotation_like"]]
            if not src:
                raise ObjectEditRefused(f"rotation_like: goal {op['rotation_like']} does not exist")
            mat = struct.unpack_from("<9i", src[0].data, 0)
        elif op.get("matrix") is not None:
            m = op["matrix"]
            if len(m) != 9:
                raise ObjectEditRefused("matrix needs 9 values (rows, as stored)")
            mat = tuple(_q(v) for v in m)
        struct.pack_into("<9i", rec, 0, *mat)
        struct.pack_into("<3i", rec, poff, x, y, z)
        if kind == "goal":
            struct.pack_into("<I", rec, 0x30, 1 if op.get("red") else 0)
    return bytes(rec)


def apply_object_ops(decoded: bytes, ops: List[dict], name: str = "") -> Tuple[bytes, dict]:
    """Return (new_bytes, report). Raises ObjectEditRefused with the reason if anything is off."""
    doc = StageDocument(decoded, name)
    if doc.container != "plain" or doc.has_errors():
        raise ObjectEditRefused("needs a decoded stage without structural errors")
    if doc.layout_variant != "common":
        raise ObjectEditRefused(f"layout variant {doc.layout_variant!r} is not supported for object edits")
    if not ops:
        raise ObjectEditRefused("no object operations given")
    try:
        grids = read_grids(decoded)
    except CellError as exc:
        raise ObjectEditRefused(f"collision cells unreadable: {exc}") from None
    ngroups = len(doc.groups)
    base = _u32(decoded, 0x18)

    # current entries per kind, in pool order
    entries: Dict[str, List[_Entry]] = {}
    for kind, (pool, size, _p, soff, _t) in KINDS.items():
        recs = doc.pools[pool]["records"]
        entries[kind] = [_Entry(r["group"], decoded[int(r["offset"], 16):int(r["offset"], 16) + size], r["index"])
                         for r in recs]
    changed_kinds: Set[str] = set()
    changed_groups: Dict[str, Set[int]] = {k: set() for k in KINDS}
    touched = set()
    for op in ops:
        kind = op.get("kind")
        if kind not in KINDS:
            raise ObjectEditRefused(f"kind must be one of {sorted(KINDS)}, got {kind!r}")
        pool, size, poff, _s, _t = KINDS[kind]
        es = entries[kind]
        what = op.get("op")
        if what in ("remove", "move"):
            idx = op.get("index")
            match = [e for e in es if e.orig == idx]
            if not isinstance(idx, int) or not match:
                raise ObjectEditRefused(f"{what}: {kind} {idx!r} does not exist")
            if (kind, idx) in touched:
                raise ObjectEditRefused(f"{kind} {idx} is used by two operations")
            touched.add((kind, idx))
            e = match[0]
            cur = tuple(struct.unpack_from("<3i", e.data, poff))
            check = op.get("expect_position") if what == "remove" else op.get("from")
            if check is None:
                raise ObjectEditRefused(f"{what} {kind} {idx}: state its current position "
                                        f"({'expect_position' if what == 'remove' else 'from'})")
            if _vec(check, "position") != cur:
                raise ObjectEditRefused(f"{what} {kind} {idx}: it is at {[v / FX for v in cur]}, plan says {check}")
            if what == "remove":
                es.remove(e)
            else:
                struct.pack_into("<3i", e.data, poff, *_vec(op.get("to"), "to"))
                e.relist = True
            changed_groups[kind].add(e.group)
        elif what == "add":
            g = op.get("group", 0)
            if not isinstance(g, int) or not 0 <= g < ngroups:
                raise ObjectEditRefused(f"add: group {g!r} does not exist ({ngroups} groups)")
            if not grids[g].present:
                raise ObjectEditRefused(f"add: group {g} has no collision grid")
            new = _Entry(g, _new_record(kind, op, es, decoded), None, relist=True, added=True)
            last = max([i for i, e in enumerate(es) if e.group <= g], default=-1)
            es.insert(last + 1, new)
            changed_groups[kind].add(g)
        else:
            raise ObjectEditRefused(f"op must be add, remove or move, got {what!r}")
        changed_kinds.add(kind)

    out = bytearray(decoded)

    def append(blob: bytes, align: int = 4) -> int:
        out.extend(bytes((-len(out)) % align))
        off = len(out)
        out.extend(blob)
        return off

    report = {"ops": ops, "pools": {}, "cells": {}}
    allowed: Set[int] = set()       # byte offsets inside the original length that may change
    new_slices: Dict[str, List[Tuple[int, int]]] = {}
    for kind in sorted(changed_kinds):
        pool, size, poff, soff, toff = KINDS[kind]
        es = entries[kind]
        if [e.group for e in es] != sorted(e.group for e in es):
            raise ObjectEditRefused("internal: pool not in group order")
        blob = b"".join(bytes(e.data) for e in es)
        start = append(blob, 4) if blob else None
        if start is None:                       # empty pool: point slices at the end of the file
            start = append(b"", 4)
        slices = []
        pos = start
        for g in range(ngroups):
            n = sum(1 for e in es if e.group == g)
            slices.append((n, pos))
            struct.pack_into("<II", out, base + 0xCC * g + soff, n, pos)
            allowed.update(range(base + 0xCC * g + soff, base + 0xCC * g + soff + 8))
            pos += n * size
        struct.pack_into("<I", out, toff, len(es))
        allowed.update(range(toff, toff + 4))
        new_slices[kind] = slices
        report["pools"][kind] = {"records": len(es), "offset": f"0x{start:X}", "size": len(blob)}

    # rebuild cell lists (per-cell state is shared by all kinds, so several kinds can change one cell)
    list_cache: Dict[Tuple[int, ...], int] = {}
    cell_state: Dict[Tuple[int, int], List] = {}          # (group, cell) -> [mask, {bit: list offset}]
    for kind in sorted(changed_kinds):
        pool, size, poff, soff, toff = KINDS[kind]
        bit = KIND_BIT[kind]
        for g in sorted(changed_groups[kind]):
            grid = grids[g]
            es = [e for e in entries[kind] if e.group == g]
            orig_local = {}             # original pool index -> original local index in group g
            for r in doc.pools[pool]["records"]:
                if r["group"] == g:
                    orig_local[r["index"]] = len(orig_local)
            cells_of: List[Set[int]] = []
            for e in es:
                if e.relist or e.orig is None:
                    x, _y, z = struct.unpack_from("<3i", e.data, poff)
                    rx = rz = MARGIN
                    if kind == "goal":
                        rx, rz = goal_extent(list(struct.unpack_from("<9i", e.data, 0)))
                    fp = grid.footprint(x, z, rx + grid.sx, rz + grid.sz)
                    if not fp:
                        raise ObjectEditRefused(f"{kind} at {[v / FX for v in (x, _y, z)]} is outside the collision "
                                                f"grid of the part it belongs to (group {g}); the ball could never "
                                                "touch it there")
                    cells_of.append(fp)
                else:
                    cells_of.append(grid.listed(bit, orig_local[e.orig]))
            changed_cells = 0
            for c, (mask, arr, lists) in enumerate(grid.cells):
                new_list = [li for li, cs in enumerate(cells_of) if c in cs]
                old = lists.get(bit, (0, None))[1] if mask >> bit & 1 else None
                if (old or []) == new_list and bool(mask >> bit & 1) == bool(new_list):
                    continue
                st = cell_state.setdefault((g, c), [mask, {b2: lists[b2][0] for b2 in lists}])
                if new_list:
                    key = tuple(new_list)
                    if key not in list_cache:
                        list_cache[key] = append(struct.pack(f"<{len(key) + 1}H", *key, 0xFFFF), 2)
                    st[0] |= 1 << bit
                    st[1][bit] = list_cache[key]
                else:
                    st[0] &= ~(1 << bit)
                    st[1].pop(bit, None)
                changed_cells += 1
            report["cells"][f"{kind} group {g}"] = {"objects": len(es), "cells_rewritten": changed_cells}
    for (g, c), (mask, ptrs) in sorted(cell_state.items()):
        offs = [ptrs[b2] for b2 in range(32) if mask >> b2 & 1]
        arr_off = append(struct.pack(f"<{len(offs)}I", *offs), 4) if offs else 0
        ent = grids[g].table + 8 * c
        struct.pack_into("<II", out, ent, mask, arr_off)
        allowed.update(range(ent, ent + 8))

    new = bytes(out)
    _verify(decoded, new, doc, entries, changed_kinds, changed_groups, allowed, name)
    report["decoded_size_before"] = len(decoded)
    report["decoded_size_after"] = len(new)
    report["decoded_sha256_before"] = hashlib.sha256(decoded).hexdigest()
    report["decoded_sha256_after"] = hashlib.sha256(new).hexdigest()
    report["banana_value"] = _banana_value(StageDocument(new, name))
    return new, report


def _banana_value(doc: StageDocument) -> int:
    return sum(5 if r["u32_0C"] == 1 else 1 if r["u32_0C"] == 0 else 0 for r in doc.pools["C"]["records"])


def _verify(old: bytes, new: bytes, doc_old, entries, changed_kinds, changed_groups, allowed, name):
    # 1. nothing inside the original length changed except the allowed fields
    diff = [i for i in range(len(old)) if old[i] != new[i]]
    stray = [i for i in diff if i not in allowed]
    if stray:
        raise ObjectEditRefused(f"internal: bytes outside the allowed fields changed (first at 0x{stray[0]:X})")
    # 2. the stage parses cleanly with the same structure
    doc = StageDocument(new, name)
    if doc.has_errors():
        raise ObjectEditRefused("edited stage has error diagnostics: " +
                                "; ".join(d["code"] for d in doc.diagnostics if d["severity"] == "error"))
    if doc.layout_variant != doc_old.layout_variant or len(doc.groups) != len(doc_old.groups):
        raise ObjectEditRefused("edited stage changed its layout")
    for kind, (pool, size, poff, soff, toff) in KINDS.items():
        p = doc.pools[pool]
        if not p.get("contiguous", True):
            raise ObjectEditRefused(f"{kind} pool is not contiguous after the edit")
        if doc.pool_totals.get(pool) != len(entries[kind]):
            raise ObjectEditRefused(f"{kind} total does not match the number of records")
        got = [bytes(new[int(r["offset"], 16):int(r["offset"], 16) + size]) for r in p["records"]]
        if got != [bytes(e.data) for e in entries[kind]]:
            raise ObjectEditRefused(f"{kind} records differ from what was built")
    # 3. collision lists: well formed everywhere, every object listed, every new/moved object covered
    stats = survey_stage(new, doc)
    for bad in ("cells without bits 0-3", "cells with bits above 6", "lists not sorted / with repeats",
                "list index out of range"):
        if stats.get(bad):
            raise ObjectEditRefused(f"collision lists: {bad}")
    grids = read_grids(new)
    old_grids = read_grids(old)
    for kind in changed_kinds:
        pool, size, poff, _s, _t = KINDS[kind]
        bit = KIND_BIT[kind]
        for g in changed_groups[kind]:
            es = [e for e in entries[kind] if e.group == g]
            for li, e in enumerate(es):
                cells = grids[g].listed(bit, li)
                if not cells:
                    raise ObjectEditRefused(f"{kind} {li} of group {g} is in no collision cell")
                if e.relist or e.orig is None:
                    x, y, z = (v / FX for v in struct.unpack_from("<3i", e.data, poff))
                    prob = coverage_problem(grids[g], kind, li, (x, y, z))
                    if prob:
                        raise ObjectEditRefused(f"{kind} at {[x, y, z]}: {prob}")
    for g, (og, ng) in enumerate(zip(old_grids, grids)):          # other types' lists untouched
        for (m1, _a1, l1), (m2, _a2, l2) in zip(og.cells, ng.cells):
            for b2 in range(7):
                if any(b2 == KIND_BIT[k] and g in changed_groups[k] for k in changed_kinds):
                    continue
                if (m1 >> b2 & 1) != (m2 >> b2 & 1) or l1.get(b2, (0, None))[1] != l2.get(b2, (0, None))[1]:
                    raise ObjectEditRefused(f"internal: group {g} bit {b2} lists changed")
    # 4. every offset the game relocates stays inside the file
    n = len(new)
    hdr = [_u32(new, 0x18), _u32(new, 0x3C)]
    base = hdr[0]
    for g in range(len(doc.groups)):
        d = base + 0xCC * g
        for o in (0x04, 0x08, 0x54, 0x78, 0x80, 0x88, 0x90, 0x98, 0xA0, 0xA8, 0xAC):
            hdr.append(_u32(new, d + o))
    for v in hdr:
        if v and not v < n:
            raise ObjectEditRefused(f"internal: relocated offset 0x{v:X} outside the file")
    for grid in grids:
        for mask, arr, lists in grid.cells:
            if arr and not arr + 4 * bin(mask).count("1") <= n:
                raise ObjectEditRefused("internal: cell array outside the file")
    # 5. banana value fits the game's byte counter
    value = _banana_value(doc)
    if value > MAX_BANANA_VALUE:
        raise ObjectEditRefused(f"banana value would be {value}; the game counts it in a byte (max 255)")
