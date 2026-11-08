"""Edit NITRO joint animations (.nsbca): scale translations, mirror rotations, retime (spec §18).

The animation is parsed into raw tracks (the stored integers and rotation indices, not floats), changed,
and written back in the layout the game's files use: header, per-node track records, pivot-rotation table,
compressed-rotation table, then the value arrays. Only stage files with one animation that ends the file
are handled; the BCA0/JNT0 headers and the dictionary stay, their sizes are updated. Every result is read
back with trcore.nitro.nsbca and compared with what was meant.

Operations ({"op": ...}):
  scale_translation  {"nodes": [i, ...] | "animated", "axis": "x"|"y"|"z", "factor": f}
                     every stored value of that translation axis (animated tracks only) times f
  mirror_rotation    {"node": i, "axis": "x"|"y"|"z"}
                     the node's rotations mirrored across the plane normal to `axis` (M -> F M F):
                     a swing to one side becomes a swing to the other side
  retime             {"factor": k}   (k = 2: half speed) frames = (frames - 1) * k + 1; every animated
                     track is resampled at one value per frame (rotations take the nearest stored one)

Keyframe operations (the editor's keyframe editor; trcore/keyframes.py; not mixed with the three above):
  set_length         {"frames": n}   first op only; every moving track is stretched to n frames (one value
                     per frame; rotations interpolated between stored samples and re-encoded)
  set_keys           {"node": i, "channel": "tx"|"ty"|"tz"|"rx"|"ry"|"rz", "keys": [...]}
                     the channel becomes the keys baked to one value per frame. A fixed axis becomes a
                     moving one; any rotation key rebuilds the node's whole rotation track from the three
                     turns (unkeyed turns keep the current motion). New rotations go into the pivot table
                     when they turn about one axis, otherwise into the compressed table.
"""
from __future__ import annotations

import hashlib
import struct
from typing import Dict, List, Optional, Tuple

from .nsbca import Nsbca, Track, rot3_matrix, rot5_matrix, rot5_words
from .nsbmd import NitroError, dict_offsets
from ..binreader import BinReader
from ..keyframes import CHANNELS, MAX_FRAMES, KeyframeError, bake, euler_matrix, euler_sequence

FX = 4096


class AnimEditRefused(Exception):
    pass


class RawTrack:
    __slots__ = ("info", "values")       # values: ints (t), (scale, inv) pairs (s) or u16 indices (r)

    def __init__(self, info: int, values: list):
        self.info, self.values = info, values

    @property
    def step(self):
        return self.info >> 30

    @property
    def last(self):
        return (self.info >> 16) & 0x1FFF

    @property
    def fx16(self):
        return bool(self.info >> 29 & 1)


class RawNode:
    def __init__(self, flags: int):
        self.flags = flags
        self.t: Optional[list] = None       # [("const", v) | RawTrack] x3, or None (identity / rest by flags)
        self.r = None                       # ("const", index) | RawTrack | None
        self.s: Optional[list] = None       # [("const", s, inv) | RawTrack] x3


class RawAnim:
    def __init__(self, data: bytes, base: int, end: int):
        r = BinReader(data)
        self.base = base
        self.tag = data[base:base + 4]
        self.frames, nnodes = r.u16(base + 4), r.u16(base + 6)
        self.flags, o3, o5 = r.u32(base + 8), r.u32(base + 0x0C), r.u32(base + 0x10)
        self.nodes: List[RawNode] = []
        max_r3 = max_r5 = -1
        n_frames = self.frames

        def count(info):
            step, last = info >> 30, (info >> 16) & 0x1FFF
            return n_frames if step == 0 else (last >> step) + 1 + max(0, n_frames - 1 - last)

        for i in range(nnodes):
            q = base + r.u16(base + 0x14 + 2 * i)
            f = r.u32(q)
            nd = RawNode(f)
            q += 4
            if not f & 1:
                if not f & 6:
                    nd.t = []
                    for ax in range(3):
                        if f & (8 << ax):
                            nd.t.append(("const", r.s32(q)))
                            q += 4
                        else:
                            info, off = r.u32(q), r.u32(q + 4)
                            q += 8
                            n = count(info)
                            vals = [struct.unpack_from("<h", data, base + off + 2 * k)[0] for k in range(n)] \
                                if info >> 29 & 1 else [r.s32(base + off + 4 * k) for k in range(n)]
                            nd.t.append(RawTrack(info, vals))
                if not f & 0xC0:
                    if f & 0x100:
                        idx = r.u32(q)
                        nd.r = ("const", idx)
                        q += 4
                        idxs = [idx & 0xFFFF]
                    else:
                        info, off = r.u32(q), r.u32(q + 4)
                        q += 8
                        idxs = [r.u16(base + off + 2 * k) for k in range(count(info))]
                        nd.r = RawTrack(info, idxs)
                    for ix in idxs:
                        if ix & 0x8000:
                            max_r3 = max(max_r3, ix & 0x7FFF)
                        else:
                            max_r5 = max(max_r5, ix)
                if not f & 0x600:
                    nd.s = []
                    for ax in range(3):
                        if f & (0x800 << ax):
                            nd.s.append(("const", r.s32(q), r.s32(q + 4)))
                            q += 8
                        else:
                            info, off = r.u32(q), r.u32(q + 4)
                            q += 8
                            n = count(info)
                            if info >> 29 & 1:
                                vals = [struct.unpack_from("<hh", data, base + off + 4 * k) for k in range(n)]
                            else:
                                vals = [struct.unpack_from("<ii", data, base + off + 8 * k) for k in range(n)]
                            nd.s.append(RawTrack(info, vals))
            self.nodes.append(nd)
        # keep whole tables (files carry a few unused entries): rot3 up to rot5, rot5 up to the first array
        n3 = max(max_r3 + 1, (o5 - o3) // 6 if o5 > o3 else 0)
        first_array = min(self._array_offsets(data, base) or [end - base])
        n5 = max(max_r5 + 1, (first_array - o5) // 10 if first_array > o5 else 0)
        self.rot3 = [struct.unpack_from("<Hhh", data, base + o3 + 6 * k) for k in range(n3)]
        self.rot5 = [struct.unpack_from("<5H", data, base + o5 + 10 * k) for k in range(n5)]

    @staticmethod
    def _array_offsets(data: bytes, base: int) -> List[int]:
        offs = []
        n = struct.unpack_from("<H", data, base + 6)[0]
        for i in range(n):
            q = base + struct.unpack_from("<H", data, base + 0x14 + 2 * i)[0]
            f = struct.unpack_from("<I", data, q)[0]
            q += 4
            if f & 1:
                continue
            if not f & 6:
                for ax in range(3):
                    if f & (8 << ax):
                        q += 4
                    else:
                        offs.append(struct.unpack_from("<I", data, q + 4)[0])
                        q += 8
            if not f & 0xC0:
                if f & 0x100:
                    q += 4
                else:
                    offs.append(struct.unpack_from("<I", data, q + 4)[0])
                    q += 8
            if not f & 0x600:
                for ax in range(3):
                    if not f & (0x800 << ax):
                        offs.append(struct.unpack_from("<I", data, q + 4)[0])
                    q += 8
        return offs

    # ---- writing
    def build(self) -> bytes:
        n = len(self.nodes)
        head = 0x14 + 2 * n
        head += (-head) % 4
        recs = []
        for nd in self.nodes:
            size = 4
            if not nd.flags & 1:
                if nd.t is not None:
                    size += sum(4 if isinstance(a, tuple) else 8 for a in nd.t)
                if nd.r is not None:
                    size += 4 if isinstance(nd.r, tuple) else 8
                if nd.s is not None:
                    size += 8 * 3
            recs.append(size)
        pos = head
        rec_off = []
        for size in recs:
            rec_off.append(pos)
            pos += size
        o3 = pos
        rot3 = b"".join(struct.pack("<Hhh", *e) for e in self.rot3)
        pos += len(rot3)
        pos += (-pos) % 4                  # the game's files align the compressed-rotation table to 4
        o5 = pos
        rot5 = b"".join(struct.pack("<5H", *e) for e in self.rot5)
        pos += len(rot5)
        arrays = bytearray()
        arr_base = pos + ((-pos) % 4)
        shared: Dict[bytes, int] = {}

        def add_array(blob: bytes) -> int:
            if blob in shared:                 # identical tracks share one array, as in the game's files
                return shared[blob]
            arrays.extend(bytes((-len(arrays)) % 4))
            off = arr_base + len(arrays)
            arrays.extend(blob)
            shared[blob] = off
            return off

        # value arrays: rotation arrays first, then translations, then scales (the order of the game's files)
        where: Dict[Tuple[int, str, int], int] = {}
        for i, nd in enumerate(self.nodes):
            if not nd.flags & 1 and isinstance(nd.r, RawTrack):
                where[(i, "r", 0)] = add_array(b"".join(struct.pack("<H", v) for v in nd.r.values))
        for key, fmt_pair in (("t", False), ("s", True)):
            for i, nd in enumerate(self.nodes):
                group = nd.t if key == "t" else nd.s
                if nd.flags & 1 or group is None:
                    continue
                for ax, a in enumerate(group):
                    if isinstance(a, RawTrack):
                        if key == "t":
                            fmt = "<h" if a.fx16 else "<i"
                            blob = b"".join(struct.pack(fmt, v) for v in a.values)
                        else:
                            fmt = "<hh" if a.fx16 else "<ii"
                            blob = b"".join(struct.pack(fmt, *v) for v in a.values)
                        where[(i, key, ax)] = add_array(blob)
        body = bytearray()
        for i, nd in enumerate(self.nodes):
            rec = bytearray(struct.pack("<I", nd.flags))
            if not nd.flags & 1:
                if nd.t is not None:
                    for ax, a in enumerate(nd.t):
                        rec += struct.pack("<i", a[1]) if isinstance(a, tuple) else \
                            struct.pack("<II", a.info, where[(i, "t", ax)])
                if nd.r is not None:
                    rec += struct.pack("<I", nd.r[1]) if isinstance(nd.r, tuple) else \
                        struct.pack("<II", nd.r.info, where[(i, "r", 0)])
                if nd.s is not None:
                    for ax, a in enumerate(nd.s):
                        rec += struct.pack("<ii", a[1], a[2]) if isinstance(a, tuple) else \
                            struct.pack("<II", a.info, where[(i, "s", ax)])
            body += rec
        out = bytearray(self.tag + struct.pack("<HHIII", self.frames, n, self.flags, o3, o5))
        out += b"".join(struct.pack("<H", o) for o in rec_off)
        out += bytes(head - len(out))
        out += body
        out += rot3
        out += bytes(o5 - len(out))
        out += rot5
        out += bytes(arr_base - len(out))
        out += arrays
        out += bytes((-len(out)) % 4)
        return bytes(out)


def _the_anim(data: bytes, name: str) -> Tuple[int, int, int]:
    """(JNT0 block offset, animation offset, end) for a file with one animation ending the file."""
    if data[:4] != b"BCA0":
        raise AnimEditRefused(f"{name}: not a BCA0 file")
    if struct.unpack_from("<H", data, 0x0E)[0] != 1:
        raise AnimEditRefused(f"{name}: expected one block")
    jnt = struct.unpack_from("<I", data, 0x10)[0]
    if data[jnt:jnt + 4] != b"JNT0" or jnt + struct.unpack_from("<I", data, jnt + 4)[0] != len(data):
        raise AnimEditRefused(f"{name}: the JNT0 block must end the file")
    entries = dict_offsets(BinReader(data), jnt + 8)
    if len(entries) != 1:
        raise AnimEditRefused(f"{name}: expected one animation, found {len(entries)}")
    return jnt, jnt + entries[0][1], len(data)


def _mirror(m, axis: int):
    f = [1.0, 1.0, 1.0]
    f[axis] = -1.0
    return tuple(tuple(f[i] * m[i][j] * f[j] for j in range(3)) for i in range(3))


def _close(a, b, tol=1e-6):
    return all(abs(a[i][j] - b[i][j]) <= tol for i in range(3) for j in range(3))


def edit_animation(data: bytes, ops: List[dict], name: str = "") -> Tuple[bytes, dict]:
    jnt, abase, end = _the_anim(data, name)
    try:
        before = Nsbca(data, name).anims[0]
    except NitroError as exc:
        raise AnimEditRefused(f"{name}: {exc}") from None
    raw = RawAnim(data, abase, end)
    kinds = {op.get("op") for op in ops}
    if kinds & set(KEY_OPS):
        if kinds - set(KEY_OPS):
            raise AnimEditRefused("set_length / set_keys cannot be combined with scale, mirror or retime")
        return _edit_keys(data, jnt, abase, raw, before, ops, name)
    report = {"ops": [], "frames_before": raw.frames}
    expect = {"scale": {}, "mirror": {}, "retime": 1}
    for op in ops:
        kind = op.get("op")
        if kind == "scale_translation":
            axis = "xyz".index(op.get("axis", "y"))
            factor = float(op["factor"])
            nodes = op.get("nodes", "animated")
            if nodes == "animated":
                nodes = [i for i, nd in enumerate(raw.nodes) if nd.t and isinstance(nd.t[axis], RawTrack)]
            changed = 0
            for i in nodes:
                nd = raw.nodes[i]
                if not nd.t or not isinstance(nd.t[axis], RawTrack):
                    raise AnimEditRefused(f"node {i} has no animated {'xyz'[axis]} translation")
                tr = nd.t[axis]
                lim = 0x7FFF if tr.fx16 else 0x7FFFFFFF
                new = [int(round(v * factor)) for v in tr.values]
                if any(abs(v) > lim for v in new):
                    raise AnimEditRefused(f"node {i}: scaled values do not fit")
                tr.values = new
                expect["scale"][(i, axis)] = expect["scale"].get((i, axis), 1.0) * factor
                changed += 1
            report["ops"].append({"op": kind, "axis": "xyz"[axis], "factor": factor, "nodes": nodes,
                                  "label": op.get("label")})
        elif kind == "mirror_rotation":
            i = int(op["node"])
            axis = "xyz".index(op.get("axis", "x"))
            nd = raw.nodes[i]
            if nd.r is None:
                raise AnimEditRefused(f"node {i} has no stored rotation")
            mine = [nd.r[1] & 0xFFFF] if isinstance(nd.r, tuple) else list(nd.r.values)
            others = set()
            for j, o in enumerate(raw.nodes):
                if j != i and o.r is not None:
                    others.update([o.r[1] & 0xFFFF] if isinstance(o.r, tuple) else o.r.values)
            if set(mine) & others:
                raise AnimEditRefused(f"node {i}'s rotations are shared with other nodes")
            n3 = n5 = 0
            for ix in sorted(set(mine)):
                if ix & 0x8000:
                    info, a, b = raw.rot3[ix & 0x7FFF]
                    want = _mirror(rot3_matrix(info, a, b), axis)
                    for cand in [(inf, aa, bb) for inf in range(256) if (inf & 0xF) <= 8
                                 for aa in (a, -a) for bb in (b, -b)]:
                        if _close(rot3_matrix(*cand), want, 1e-4):
                            raw.rot3[ix & 0x7FFF] = cand
                            n3 += 1
                            break
                    else:
                        raise AnimEditRefused("a pivot rotation has no mirrored pivot form")
                else:
                    raw.rot5[ix] = rot5_words(_mirror(rot5_matrix(raw.rot5[ix]), axis))
                    n5 += 1
            expect["mirror"][i] = axis
            report["ops"].append({"op": kind, "node": i, "axis": "xyz"[axis], "pivot_rotations": n3,
                                  "compressed_rotations": n5, "label": op.get("label")})
        elif kind == "retime":
            k = int(op["factor"])
            if k < 2 or k > 8:
                raise AnimEditRefused("retime factor must be 2..8")
            old_frames = raw.frames
            new_frames = (old_frames - 1) * k + 1
            if new_frames > 0x1FFF:
                raise AnimEditRefused("too many frames")
            for i, nd in enumerate(raw.nodes):
                for group in (nd.t, nd.s):
                    if not group:
                        continue
                    for ax, tr in enumerate(group):
                        if not isinstance(tr, RawTrack):
                            continue
                        pair = isinstance(tr.values[0], tuple)
                        vals = []
                        for g in range(new_frames):
                            f = g / k
                            sample = _sample_raw(tr, f, old_frames)
                            vals.append(tuple(int(round(v)) for v in sample) if pair else int(round(sample)))
                        group[ax] = RawTrack((tr.info & 0x20000000) | (new_frames << 16), vals)
                if isinstance(nd.r, RawTrack):
                    tr = nd.r
                    vals = []
                    for g in range(new_frames):
                        f = g / k
                        vals.append(tr.values[_nearest_index(tr, f, old_frames)])
                    nd.r = RawTrack(new_frames << 16, vals)
            raw.frames = new_frames
            expect["retime"] *= k
            report["ops"].append({"op": kind, "factor": k, "frames": new_frames, "label": op.get("label")})
        else:
            raise AnimEditRefused(f"unknown animation op {kind!r}")
    body = raw.build()
    out = bytearray(data[:abase]) + body
    struct.pack_into("<I", out, jnt + 4, len(out) - jnt)
    struct.pack_into("<I", out, 0x08, len(out))
    new = bytes(out)
    _verify(before, new, expect, name)
    report.update({"frames_after": raw.frames, "size_before": len(data), "size_after": len(new),
                   "sha256_before": hashlib.sha256(data).hexdigest(), "sha256_after": hashlib.sha256(new).hexdigest()})
    return new, report


def _sample_raw(tr: RawTrack, f: float, frames: int):
    vals = tr.values
    n = len(vals)
    s = tr.step
    if s == 0:
        i = max(0, min(n - 1, int(f)))
        j = min(n - 1, i + 1)
        t = f - int(f)
    elif f >= tr.last:
        i = max(0, min(n - 1, (tr.last >> s) + int(f) - tr.last))
        j = min(n - 1, i + 1)
        t = f - int(f)
    else:
        pos = f / (1 << s)
        i = max(0, min(n - 1, int(pos)))
        j = min(n - 1, i + 1)
        t = pos - int(pos)
    a, b = vals[i], vals[j]
    if isinstance(a, tuple):
        return tuple(x + (y - x) * t for x, y in zip(a, b))
    return a + (b - a) * t


def _nearest_index(tr: RawTrack, f: float, frames: int) -> int:
    n = len(tr.values)
    s = tr.step
    if s == 0:
        return max(0, min(n - 1, int(round(f))))
    if f >= tr.last:
        return max(0, min(n - 1, (tr.last >> s) + int(round(f)) - tr.last))
    return max(0, min(n - 1, int(round(f / (1 << s)))))


def _verify(before, new: bytes, expect: dict, name: str):
    try:
        after = Nsbca(new, name).anims[0]
    except NitroError as exc:
        raise AnimEditRefused(f"edited animation does not read back: {exc}") from None
    k = expect["retime"]
    if after.frames != (before.frames - 1) * k + 1 or len(after.nodes) != len(before.nodes):
        raise AnimEditRefused("internal: frame or node count wrong after the edit")
    worst = 0.0
    for i, (nb, na) in enumerate(zip(before.nodes, after.nodes)):
        if nb.flags != na.flags and (nb.flags & ~0) != na.flags:
            raise AnimEditRefused(f"internal: node {i} flags changed")
        for g in range(0, after.frames, max(1, after.frames // 50)):
            f = g / k
            tb, rb, sb = nb.pose(f)
            ta, ra, sa = na.pose(g)
            if isinstance(tb, tuple):
                want = list(tb)
                for (ni, ax), fac in expect["scale"].items():
                    if ni == i and isinstance(nb.t[ax], Track):
                        want[ax] *= fac
                for x, y in zip(want, ta):
                    worst = max(worst, abs(x - y))
                    if abs(x - y) > 2.0 / FX + 1e-6 + (0.002 if k > 1 else 0):
                        raise AnimEditRefused(f"node {i}: translation reads back wrong at frame {g}")
            elif tb != ta:
                raise AnimEditRefused(f"internal: node {i} translation kind changed")
            if isinstance(rb, tuple) and not isinstance(rb, str):
                want = _mirror(rb, expect["mirror"][i]) if i in expect["mirror"] else rb
                tol = 0.01 if k == 1 else 0.2
                if not _close(want, ra, tol):
                    raise AnimEditRefused(f"node {i}: rotation reads back wrong at frame {g}")


# ---------------------------------------------------------------------------- keyframes
KEY_OPS = ("set_length", "set_keys")
IDENT3 = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))


def encode_rotation(m):
    """("rot3", (info, a, b)) when m turns about one axis (the compact pivot form the game mostly uses),
    else ("rot5", five words). The encoding is decoded again and compared with m."""
    for i in range(3):
        for j in range(3):
            v = m[i][j]
            if abs(abs(v) - 1.0) > 1e-5:
                continue
            if any(abs(m[i][k]) > 1e-5 for k in range(3) if k != j) or \
                    any(abs(m[k][j]) > 1e-5 for k in range(3) if k != i):
                continue
            a, b, c, d = (m[r][q] for r in range(3) for q in range(3) if r != i and q != j)
            neg = (1 if v < 0 else 0) | (2 if abs(c + b) < abs(c - b) else 0) | (4 if abs(d + a) < abs(d - a) else 0)
            qa, qb = int(round(a * FX)), int(round(b * FX))
            info = (i * 3 + j) | (neg << 4)
            if _close(rot3_matrix(info, qa, qb), m, 1.5 / FX):
                return "rot3", (info, qa, qb)
    w = rot5_words(m)
    if not _close(rot5_matrix(w), m, 2.5 / FX):
        raise AnimEditRefused("a rotation cannot be stored (not a rotation matrix?)")
    return "rot5", tuple(w)


def _rows(r):
    return r if isinstance(r, tuple) and len(r) == 3 and isinstance(r[0], tuple) else None


def _t_track(vals: List[float], prefer16: bool, frames: int, what: str) -> RawTrack:
    q = [int(round(v * FX)) for v in vals]
    if any(abs(x) > 0x7FFFFFFF for x in q):
        raise AnimEditRefused(f"{what}: values out of range")
    fx16 = prefer16 and all(-0x8000 <= x <= 0x7FFF for x in q)
    return RawTrack((0x20000000 if fx16 else 0) | (frames << 16), q)


def _edit_keys(data: bytes, jnt: int, abase: int, raw: RawAnim, before, ops: List[dict], name: str):
    old = raw.frames
    new = old
    rest_ops = list(ops)
    report = {"ops": [], "frames_before": old}
    if rest_ops and rest_ops[0].get("op") == "set_length":
        try:
            new = int(rest_ops[0]["frames"])
        except (KeyError, TypeError, ValueError):
            raise AnimEditRefused("set_length needs 'frames'") from None
        if not 2 <= new <= MAX_FRAMES:
            raise AnimEditRefused(f"an animation has 2 to {MAX_FRAMES} frames")
        report["ops"].append({"op": "set_length", "frames": new, "label": rest_ops[0].get("label")})
        rest_ops = rest_ops[1:]
    keyed: Dict[Tuple[int, str], List[float]] = {}
    for op in rest_ops:
        if op.get("op") != "set_keys":
            raise AnimEditRefused("set_length must be the first op and may appear once")
        try:
            i, ch = int(op["node"]), op["channel"]
        except (KeyError, TypeError, ValueError):
            raise AnimEditRefused("set_keys needs 'node', 'channel' and 'keys'") from None
        if not 0 <= i < len(raw.nodes):
            raise AnimEditRefused(f"no node {i}")
        if ch not in CHANNELS:
            raise AnimEditRefused(f"unknown channel {ch!r}")
        if raw.nodes[i].flags & 1:
            raise AnimEditRefused(f"node {i} stores no animation (identity node)")
        if (i, ch) in keyed:
            raise AnimEditRefused(f"node {i} {ch}: keys given twice")
        try:
            keyed[(i, ch)] = bake(op.get("keys"), new, f"node {i} {ch}")
        except KeyframeError as exc:
            raise AnimEditRefused(str(exc)) from None
        report["ops"].append({"op": "set_keys", "node": i, "channel": ch, "keys": len(op["keys"]),
                              "label": op.get("label")})
    resample = new != old

    def src(g: int) -> float:
        return g * (old - 1) / (new - 1) if resample else float(g)

    r3_at, r5_at = {}, {}
    for k, e in enumerate(raw.rot3):
        r3_at.setdefault(tuple(e), k)
    for k, e in enumerate(raw.rot5):
        r5_at.setdefault(tuple(e), k)
    added = {"rot3": 0, "rot5": 0}

    def rot_index(m) -> int:
        kind, e = encode_rotation(m)
        table, at = (raw.rot3, r3_at) if kind == "rot3" else (raw.rot5, r5_at)
        k = at.get(e)
        if k is None:
            k = len(table)
            if k >= 0x8000:
                raise AnimEditRefused("too many different rotations")
            table.append(e)
            at[e] = k
            added[kind] += 1
        return (0x8000 | k) if kind == "rot3" else k

    expect_t: Dict[Tuple[int, int], List[float]] = {}
    expect_r: Dict[int, list] = {}
    for i, nd in enumerate(raw.nodes):
        if nd.flags & 1:
            continue
        bn = before.nodes[i]
        tkeys = [ax for ax in range(3) if (i, "t" + "xyz"[ax]) in keyed]
        moving_t = bool(nd.t) and any(isinstance(a, RawTrack) for a in nd.t)
        if tkeys or (resample and moving_t):
            if nd.t is None:
                if nd.flags & 4:
                    raise AnimEditRefused(f"node {i}: translation at the model's rest value is not supported")
                nd.t = [("const", 0), ("const", 0), ("const", 0)]
                nd.flags &= ~0x2
            for ax in range(3):
                cur = nd.t[ax]
                key = (i, "t" + "xyz"[ax])
                if key in keyed:
                    vals = keyed[key]
                    nd.t[ax] = _t_track(vals, cur.fx16 if isinstance(cur, RawTrack) else True, new,
                                        f"node {i} {key[1]}")
                    expect_t[(i, ax)] = vals
                elif isinstance(cur, RawTrack) and resample:
                    vals = [bn.pose(src(g))[0][ax] for g in range(new)]
                    nd.t[ax] = _t_track(vals, cur.fx16, new, f"node {i} t{'xyz'[ax]}")
                    expect_t[(i, ax)] = vals
            for ax in range(3):
                bit = 8 << ax
                nd.flags = (nd.flags | bit) if isinstance(nd.t[ax], tuple) else (nd.flags & ~bit)
        rkeys = [ch for ch in ("rx", "ry", "rz") if (i, ch) in keyed]
        if rkeys or (resample and isinstance(nd.r, RawTrack)):
            if nd.r is None and nd.flags & 0x80 and not nd.flags & 0x40:
                raise AnimEditRefused(f"node {i}: rotation at the model's rest value is not supported")
            mats = [_rows(bn.pose(src(g))[1]) or IDENT3 for g in range(new)]
            if rkeys:
                eul = euler_sequence(mats)
                cols = [[e[k] for e in eul] for k in range(3)]
                for k, ch in enumerate(("rx", "ry", "rz")):
                    if (i, ch) in keyed:
                        cols[k] = keyed[(i, ch)]
                mats = [euler_matrix(cols[0][g], cols[1][g], cols[2][g]) for g in range(new)]
            nd.r = RawTrack(new << 16, [rot_index(m) for m in mats])
            nd.flags &= ~0x1C0
            expect_r[i] = mats
    raw.frames = new
    body = raw.build()
    out = bytearray(data[:abase]) + body
    struct.pack_into("<I", out, jnt + 4, len(out) - jnt)
    struct.pack_into("<I", out, 0x08, len(out))
    newb = bytes(out)
    _verify_keys(before, newb, new, src, expect_t, expect_r, name)
    report.update({"frames_after": new, "rotations_added": added, "size_before": len(data),
                   "size_after": len(newb), "sha256_before": hashlib.sha256(data).hexdigest(),
                   "sha256_after": hashlib.sha256(newb).hexdigest()})
    return newb, report


def _verify_keys(before, newb: bytes, frames: int, src, expect_t, expect_r, name: str):
    try:
        after = Nsbca(newb, name).anims[0]
    except NitroError as exc:
        raise AnimEditRefused(f"edited animation does not read back: {exc}") from None
    if after.frames != frames or len(after.nodes) != len(before.nodes):
        raise AnimEditRefused("internal: frame or node count wrong after the edit")
    allowed = 0x1FA                          # translation / rotation kind bits may change, nothing else
    for i, (nb, na) in enumerate(zip(before.nodes, after.nodes)):
        if (nb.flags ^ na.flags) & ~allowed:
            raise AnimEditRefused(f"internal: node {i} flags changed beyond the edited channels")
        if na.flags & 1:
            continue
        for g in range(frames):
            ta, ra, sa = na.pose(g)
            tb, rb, sb = nb.pose(src(g))
            for ax in range(3):
                want = expect_t.get((i, ax))
                got = ta[ax] if isinstance(ta, tuple) else 0.0
                if want is not None:
                    if abs(got - int(round(want[g] * FX)) / FX) > 1e-9:
                        raise AnimEditRefused(f"node {i}: translation reads back wrong at frame {g}")
                else:
                    ref = tb[ax] if isinstance(tb, tuple) else 0.0
                    if abs(got - ref) > 1e-9:
                        raise AnimEditRefused(f"internal: node {i} translation changed at frame {g}")
            want_r = expect_r.get(i)
            if want_r is not None:
                if not _close(_rows(ra) or IDENT3, want_r[g], 2.5 / FX):
                    raise AnimEditRefused(f"node {i}: rotation reads back wrong at frame {g}")
            elif not _close(_rows(ra) or IDENT3, _rows(rb) or IDENT3, 1e-9):
                raise AnimEditRefused(f"internal: node {i} rotation changed at frame {g}")
            if sa != sb:
                raise AnimEditRefused(f"internal: node {i} scale changed")
