"""Read-only reader for NITRO joint (node) animations (.nsbca, block JNT0): the moving parts of a stage.

Format, as decoded from the game's files (spec §18; checked over all 118 animations of the ROM):
  * JNT0 holds a dictionary of animations. Each animation: 'J\\0AC', u16 frame count, u16 node count,
    u32 flags, u32 offset of the pivot-rotation table ("rot3"), u32 offset of the compressed-rotation table
    ("rot5"), then u16 offsets (from the animation's start) of one track record per model node.
  * Track record: u32 flags, then data for translation, rotation, scale in that order:
      bit 0 identity (nothing stored), bits 24-31 node index;
      translation: bit 1 identity, bit 2 = the model's rest value, else per axis x/y/z
                   (bits 3/4/5 = constant: 4-byte fx32) or an animated track (8 bytes: info, offset);
      rotation:    bit 6 identity, bit 7 = rest value, bit 8 = constant (4 bytes: index) or animated
                   (info, offset -> u16 index per stored frame);
      scale:       bit 9 identity, bit 10 = rest value, else per axis (bits 11/12/13 constant: fx32 scale +
                   fx32 1/scale) or animated (info, offset -> pairs).
  * Track info: bits 0-15 first frame, bits 16-28 last interpolated frame, bit 29 values are fx16 (else
    fx32), bits 30-31 step (stored every 1, 2 or 4 frames; frames in between are interpolated). Values:
    step 0: one sample per frame; otherwise (last >> step) + 1 samples, then one per frame after `last`.
  * Rotation index: bit 15 set -> rot3 entry (6 bytes: u16 pivot info, fx16 a, fx16 b; pivot as in the
    model's nodes: low 4 bits = the +-1 element, next bits = signs); else rot5 entry (10 bytes, 5 x u16):
    the top 13 bits of each are rows r0 = (v0, v1, v2) and r1 = (v3, v4, ...), the sixth value is 13 bits
    made of the low 3 bits of words 4 (1 bit), 0, 1, 2, 3; the third row is r0 x r1. All 7,990 rot5 and
    80,580 rot3 rotations used in the ROM decode to orthonormal matrices; rows (not columns) are confirmed
    by continuity with neighbouring pivot rotations.
Matrices use the model's convention (row vectors, v' = v . S . R + T).
"""
from __future__ import annotations

import math
import struct
from typing import Dict, List, Optional, Sequence, Tuple

from ..binreader import BinReader, ReadOutOfBounds
from .nsbmd import NitroError, dict_offsets, pivot_matrix

FX = 4096.0
Vec = Tuple[float, float, float]
Mat3 = Tuple[Vec, Vec, Vec]
IDENTITY3: Mat3 = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))


def _s16(x: int) -> int:
    return x - 0x10000 if x & 0x8000 else x


def rot5_matrix(words: Sequence[int]) -> Mat3:
    v = [(_s16(w) >> 3) / FX for w in words[:5]]
    low = ((words[4] & 1) << 12) | ((words[0] & 7) << 9) | ((words[1] & 7) << 6) | ((words[2] & 7) << 3) | (words[3] & 7)
    if low & 0x1000:
        low -= 0x2000
    v.append(low / FX)
    a, b = (v[0], v[1], v[2]), (v[3], v[4], v[5])
    c = (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])
    return a, b, c


def rot5_words(m: Mat3) -> Tuple[int, int, int, int, int]:
    """Inverse of rot5_matrix: rows 0 and 1 of an orthonormal matrix -> five u16 words."""
    vals = []
    for v in (m[0][0], m[0][1], m[0][2], m[1][0], m[1][1], m[1][2]):
        q = int(round(v * FX))
        vals.append(max(-4096, min(4095, q)))
    six = vals[5] & 0x1FFF
    lows = [(six >> 9) & 7, (six >> 6) & 7, (six >> 3) & 7, six & 7, (six >> 12) & 1]
    return tuple(((vals[k] & 0x1FFF) << 3 | lows[k]) & 0xFFFF for k in range(5))


def rot3_matrix(info: int, a: int, b: int) -> Mat3:
    return pivot_matrix(info & 0xF, info >> 4, a / FX, b / FX)


class Track:
    """One animated value (a translation or scale axis, or the rotation)."""
    __slots__ = ("info", "values", "offset")

    def __init__(self, info: int, values: list, offset: int):
        self.info, self.values, self.offset = info, values, offset

    @property
    def step(self) -> int:
        return self.info >> 30

    @property
    def last(self) -> int:
        return (self.info >> 16) & 0x1FFF

    @property
    def fx16(self) -> bool:
        return bool(self.info >> 29 & 1)

    def sample(self, frame: float):
        """(value_a, value_b, t): the two stored samples around `frame` and the blend between them."""
        f = max(0.0, frame)
        s = self.step
        n = len(self.values)
        if s == 0:
            i = max(0, min(n - 1, int(f)))
            j = min(n - 1, i + 1)
            return self.values[i], self.values[j], (f - math.floor(f)) if j != i else 0.0
        if f >= self.last:
            i = (self.last >> s) + int(f) - self.last if f >= self.last else int(f)
            i = max(0, min(n - 1, i))
            j = min(n - 1, i + 1)
            frac = f - math.floor(f)
            return self.values[i], self.values[j], frac if j != i else 0.0
        pos = f / (1 << s)
        i = int(pos)
        i = max(0, min(n - 1, i))
        j = min(n - 1, i + 1)
        return self.values[i], self.values[j], pos - int(pos)


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def _orthonormal(m) -> Mat3:
    r0 = m[0]
    n0 = math.sqrt(sum(x * x for x in r0)) or 1.0
    r0 = tuple(x / n0 for x in r0)
    r1 = m[1]
    d = sum(x * y for x, y in zip(r0, r1))
    r1 = tuple(y - d * x for x, y in zip(r0, r1))
    n1 = math.sqrt(sum(x * x for x in r1)) or 1.0
    r1 = tuple(x / n1 for x in r1)
    r2 = (r0[1] * r1[2] - r0[2] * r1[1], r0[2] * r1[0] - r0[0] * r1[2], r0[0] * r1[1] - r0[1] * r1[0])
    return r0, r1, r2


class NodeAnim:
    """Tracks of one node. Each of t / r / s is None (identity), 'rest' (the model's value), a constant or a
    Track ([x, y, z] per axis for t and s)."""

    def __init__(self, index: int, flags: int):
        self.index = index
        self.flags = flags
        self.t = self.r = self.s = None
        self.offsets: Dict[str, int] = {}

    @property
    def animated(self) -> bool:
        parts = list(self.t) if isinstance(self.t, list) else [self.t]
        parts += list(self.s) if isinstance(self.s, list) else [self.s]
        parts.append(self.r)
        return any(isinstance(p, Track) for p in parts)

    def pose(self, frame: float):
        """(translation or None/'rest', rotation rows or None/'rest', scale or None/'rest') at `frame`."""
        def axis(v):
            if isinstance(v, Track):
                a, b, t = v.sample(frame)
                return _lerp(a, b, t)
            return v
        t = self.t if not isinstance(self.t, list) else tuple(axis(v) for v in self.t)
        s = self.s if not isinstance(self.s, list) else tuple(axis(v)[0] if isinstance(axis(v), tuple) else axis(v)
                                                              for v in self.s)
        r = self.r
        if isinstance(r, Track):
            a, b, tt = r.sample(frame)
            r = a if tt == 0 else _orthonormal(tuple(tuple(_lerp(a[i][j], b[i][j], tt) for j in range(3))
                                                     for i in range(3)))
        return t, r, s


class JointAnim:
    def __init__(self, name: str, frames: int, flags: int, nodes: List[NodeAnim], base: int):
        self.name, self.frames, self.flags, self.nodes, self.base = name, frames, flags, nodes, base

    def node(self, index: int) -> Optional[NodeAnim]:
        return self.nodes[index] if 0 <= index < len(self.nodes) else None

    def animated_nodes(self) -> List[int]:
        return [n.index for n in self.nodes if n.animated]


class Nsbca:
    def __init__(self, data: bytes, name: str = ""):
        self.data = data
        self.name = name
        r = BinReader(data, name)
        try:
            if data[:4] != b"BCA0":
                raise NitroError(f"{name}: not a BCA0 file")
            nblocks = r.u16(0x0E)
            self.anims: List[JointAnim] = []
            for bi in range(nblocks):
                b = r.u32(0x10 + 4 * bi)
                if data[b:b + 4] != b"JNT0":
                    continue
                for aname, off in dict_offsets(r, b + 8):
                    self.anims.append(self._anim(r, aname, b + off))
        except (ReadOutOfBounds, struct.error) as exc:
            raise NitroError(f"{name}: {exc}") from None

    @staticmethod
    def _anim(r: BinReader, name: str, a: int) -> JointAnim:
        d = r._data
        if d[a:a + 4] != b"J\x00AC":
            raise NitroError(f"animation {name}: bad tag")
        frames, nnodes = r.u16(a + 4), r.u16(a + 6)
        flags, o3, o5 = r.u32(a + 8), r.u32(a + 0x0C), r.u32(a + 0x10)

        def values(info, off, kind):
            step = info >> 30
            last = (info >> 16) & 0x1FFF
            n = frames if step == 0 else (last >> step) + 1 + max(0, frames - 1 - last)
            if kind == "rot":
                out = []
                for k in range(n):
                    idx = r.u16(a + off + 2 * k)
                    out.append(rot3_matrix(r.u16(a + o3 + 6 * (idx & 0x7FFF)), r.s16(a + o3 + 6 * (idx & 0x7FFF) + 2),
                                           r.s16(a + o3 + 6 * (idx & 0x7FFF) + 4))
                               if idx & 0x8000 else rot5_matrix([r.u16(a + o5 + 10 * idx + 2 * w) for w in range(5)]))
                return out
            fx16 = info >> 29 & 1
            if kind == "t":
                return [(r.s16(a + off + 2 * k) if fx16 else r.s32(a + off + 4 * k)) / FX for k in range(n)]
            return [((r.s16(a + off + 4 * k) if fx16 else r.s32(a + off + 8 * k)) / FX) for k in range(n)]   # scale

        nodes = []
        for i in range(nnodes):
            q = a + r.u16(a + 0x14 + 2 * i)
            f = r.u32(q)
            nd = NodeAnim(i, f)
            q += 4
            if f & 1:
                nodes.append(nd)
                continue
            if f & 2:
                nd.t = None
            elif f & 4:
                nd.t = "rest"
            else:
                axes = []
                for ax in range(3):
                    nd.offsets[f"t{'xyz'[ax]}"] = q
                    if f & (8 << ax):
                        axes.append(r.s32(q) / FX)
                        q += 4
                    else:
                        info, off = r.u32(q), r.u32(q + 4)
                        axes.append(Track(info, values(info, off, "t"), off))
                        q += 8
                nd.t = axes
            if f & 0x40:
                nd.r = None
            elif f & 0x80:
                nd.r = "rest"
            elif f & 0x100:
                nd.offsets["r"] = q
                idx = r.u32(q) & 0xFFFF
                nd.r = (rot3_matrix(r.u16(a + o3 + 6 * (idx & 0x7FFF)), r.s16(a + o3 + 6 * (idx & 0x7FFF) + 2),
                                    r.s16(a + o3 + 6 * (idx & 0x7FFF) + 4))
                        if idx & 0x8000 else rot5_matrix([r.u16(a + o5 + 10 * idx + 2 * w) for w in range(5)]))
                q += 4
            else:
                nd.offsets["r"] = q
                info, off = r.u32(q), r.u32(q + 4)
                nd.r = Track(info, values(info, off, "rot"), off)
                q += 8
            if f & 0x200:
                nd.s = None
            elif f & 0x400:
                nd.s = "rest"
            else:
                axes = []
                for ax in range(3):
                    nd.offsets[f"s{'xyz'[ax]}"] = q
                    if f & (0x800 << ax):
                        axes.append(r.s32(q) / FX)
                        q += 8
                    else:
                        info, off = r.u32(q), r.u32(q + 4)
                        axes.append(Track(info, values(info, off, "s"), off))
                        q += 8
                nd.s = axes
            nodes.append(nd)
        return JointAnim(name, frames, flags, nodes, a)


def node_local_matrix(model, node_index: int, anim: Optional[JointAnim], frame: float):
    """A model node's local matrix ((rows), T) at `frame` of `anim` (or at rest without one)."""
    rest = model.node_matrix(node_index)
    nd = anim.node(node_index) if anim else None
    if nd is None:
        return rest
    if nd.flags & 1:
        return (IDENTITY3, (0.0, 0.0, 0.0))
    t, r, s = nd.pose(frame)
    rest_r, rest_t = rest
    tt = rest_t if t == "rest" else (0.0, 0.0, 0.0) if t is None else t
    rr = rest_r if r == "rest" else IDENTITY3 if r is None else r
    if s not in (None, "rest"):
        rr = tuple(tuple(rr[i][j] * s[i] for j in range(3)) for i in range(3))
    elif s == "rest":
        rr = rest_r if r == "rest" else rr      # rest scale is folded into rest_r by the model reader
    return (tuple(tuple(row) for row in rr), tuple(tt))


def survey(data: bytes, name: str = "") -> dict:
    """Counters over one .nsbca: animations, frames, animated nodes, rotation checks."""
    import collections
    c = collections.Counter()
    nsb = Nsbca(data, name)
    for an in nsb.anims:
        c["animations"] += 1
        c["frames"] += an.frames
        for nd in an.nodes:
            c["node tracks"] += 1
            c["animated nodes"] += nd.animated
            rots = []
            if isinstance(nd.r, Track):
                rots = nd.r.values
            elif isinstance(nd.r, tuple):
                rots = [nd.r]
            for m in rots:
                ok = all(abs(math.sqrt(sum(x * x for x in row)) - 1) < 0.01 for row in m) and \
                    abs(sum(x * y for x, y in zip(m[0], m[1]))) < 0.01
                c["rotations"] += 1
                c["rotations not orthonormal"] += not ok
    return dict(c)
