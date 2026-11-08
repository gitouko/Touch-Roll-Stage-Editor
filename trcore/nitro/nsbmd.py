"""Read-only NSBMD (BMD0 / MDL0) reader: model structure and triangle geometry.

Only what is needed to draw a stage's own model is decoded: the resource
dictionaries, node transforms, the structure byte code (SBC) that places
shapes, and the shapes' GX display lists (vertex commands). Materials and
textures are listed by name only.

The format is the public NITRO-System G3D resource layout (documented by the
DS homebrew / modding community). Everything here is a *reader*: nothing is
ever written back, and malformed input raises NitroError instead of crashing.

Conventions (as on the DS): matrices are 4x3 row-vector matrices,
v_out = v_in . R + T. Vertex positions in display lists are multiplied by the
model's posScale before the node transform.
"""
from __future__ import annotations

import math
import struct
from typing import Dict, List, Optional, Tuple

from ..binreader import BinReader, ReadOutOfBounds

FX = 4096.0
MAX_DL_WORDS = 1 << 20
MAX_SBC_STEPS = 1 << 16
STACK_SIZE = 32

Vec3 = Tuple[float, float, float]


class NitroError(Exception):
    pass


# ----------------------------------------------------------------------------- matrices
# A matrix is (R, T): R = 3 rows of 3 floats, T = 3 floats.  v' = v.R + T

IDENTITY = (((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)), (0.0, 0.0, 0.0))


def mat_apply(m, v: Vec3) -> Vec3:
    (r0, r1, r2), t = m
    x, y, z = v
    return (x * r0[0] + y * r1[0] + z * r2[0] + t[0],
            x * r0[1] + y * r1[1] + z * r2[1] + t[1],
            x * r0[2] + y * r1[2] + z * r2[2] + t[2])


def mat_then(a, b):
    """Matrix that applies a first, then b (row vectors: a . b)."""
    ra, ta = a
    rb, tb = b
    r = tuple(tuple(sum(ra[i][k] * rb[k][j] for k in range(3)) for j in range(3)) for i in range(3))
    t = mat_apply(b, ta)
    return (r, t)


def mat_scale(s: Vec3):
    return (((s[0], 0.0, 0.0), (0.0, s[1], 0.0), (0.0, 0.0, s[2])), (0.0, 0.0, 0.0))


def pivot_matrix(select: int, neg: int, a: float, b: float):
    """Compressed rotation: one element is +-1, its row and column are zero, the rest is a, b, c, d
    in row-major order with c = +-b and d = +-a."""
    one = -1.0 if neg & 1 else 1.0
    c = -b if neg & 2 else b
    d = -a if neg & 4 else a
    if not 0 <= select <= 8:
        raise NitroError(f"pivot index {select} out of range")
    pr, pc = divmod(select, 3)
    rows = [[0.0] * 3 for _ in range(3)]
    rows[pr][pc] = one
    rest = iter((a, b, c, d))
    for i in range(3):
        for j in range(3):
            if i != pr and j != pc:
                rows[i][j] = next(rest)
    return tuple(tuple(r) for r in rows)


# ----------------------------------------------------------------------------- dictionaries

def read_dict(r: BinReader, off: int) -> List[Tuple[str, bytes]]:
    """NNS resource dictionary at off -> [(name, entry_bytes)]."""
    count = r.u8(off + 1, "dict count")
    ofs_entry = r.u16(off + 6, "dict entry offset")
    eh = off + ofs_entry
    unit = r.u16(eh, "dict entry size")
    ofs_name = r.u16(eh + 2, "dict name offset")
    out = []
    for i in range(count):
        data = r.slice(eh + 4 + unit * i, unit, "dict entry")
        raw = r.slice(eh + ofs_name + 16 * i, 16, "dict name")
        name = raw.split(b"\x00", 1)[0].decode("latin-1")
        out.append((name, data))
    return out


def dict_offsets(r: BinReader, off: int) -> List[Tuple[str, int]]:
    """Dictionary whose entries are u32 offsets."""
    out = []
    for name, data in read_dict(r, off):
        if len(data) < 4:
            raise NitroError(f"dictionary at 0x{off:X}: entry size {len(data)}, expected 4")
        out.append((name, struct.unpack_from("<I", data)[0]))
    return out


# ----------------------------------------------------------------------------- model

class Model:
    def __init__(self, r: BinReader, base: int, name: str):
        self.name = name
        self.base = base
        self.size = r.u32(base, "model size")
        self.ofs_sbc = r.u32(base + 0x04)
        self.ofs_mat = r.u32(base + 0x08)
        self.ofs_shp = r.u32(base + 0x0C)
        self.ofs_evp = r.u32(base + 0x10)
        info = base + 0x14
        self.sbc_type, self.scaling_rule, self.tex_mtx_mode, self.num_node = (r.u8(info + i) for i in range(4))
        self.num_mat, self.num_shp, self.first_unused_mtx = r.u8(info + 4), r.u8(info + 5), r.u8(info + 6)
        self.pos_scale = r.s32(info + 0x08) / FX
        self.inv_pos_scale = r.s32(info + 0x0C) / FX
        self.num_vertex, self.num_polygon, self.num_triangle, self.num_quad = (
            r.u16(info + 0x10 + 2 * i) for i in range(4))
        box = [struct.unpack_from("<h", r.data, info + 0x18 + 2 * i)[0] / FX for i in range(6)] \
            if r.fits(info + 0x18, 12) else [0.0] * 6
        self.box_pos_scale = r.s32(info + 0x24) / FX
        self.box = {"x": box[0], "y": box[1], "z": box[2], "w": box[3], "h": box[4], "d": box[5]}
        node_info = base + 0x40
        self.nodes = []
        for nname, rel in dict_offsets(r, node_info):
            self.nodes.append(self._read_node(r, node_info + rel, nname))
        if len(self.nodes) != self.num_node:
            raise NitroError(f"model {name}: {len(self.nodes)} nodes in dictionary, header says {self.num_node}")
        mat_base = base + self.ofs_mat
        # material block: u16 ofsDictTexToMatList, u16 ofsDictPlttToMatList, then the material dict
        self.materials = [n for n, _ in read_dict(r, mat_base + 4)]
        shp_base = base + self.ofs_shp
        self.shapes = []
        for sname, rel in dict_offsets(r, shp_base):
            so = shp_base + rel
            self.shapes.append({"name": sname, "offset": so, "flag": r.u32(so + 4),
                                "dl_offset": so + r.u32(so + 8), "dl_size": r.u32(so + 12)})
        sbc_end = base + self.ofs_mat
        self.sbc = r.slice(base + self.ofs_sbc, max(0, sbc_end - (base + self.ofs_sbc)), "sbc")

    @staticmethod
    def _read_node(r: BinReader, o: int, name: str) -> dict:
        flag = r.u16(o)
        m00 = struct.unpack_from("<h", r.data, o + 2)[0] / FX if r.fits(o + 2, 2) else 0.0
        p = o + 4
        t = (0.0, 0.0, 0.0)
        rot = IDENTITY[0]
        s = (1.0, 1.0, 1.0)
        if not flag & 1:
            t = (r.s32(p) / FX, r.s32(p + 4) / FX, r.s32(p + 8) / FX)
            p += 12
        if not flag & 2:
            if flag & 8:
                a = struct.unpack_from("<h", r.slice(p, 2))[0] / FX
                b = struct.unpack_from("<h", r.slice(p + 2, 2))[0] / FX
                p += 4
                rot = pivot_matrix((flag >> 4) & 0xF, (flag >> 8) & 7, a, b)
            else:
                rest = struct.unpack("<8h", r.slice(p, 16))
                p += 16
                v = [m00] + [x / FX for x in rest]
                rot = ((v[0], v[1], v[2]), (v[3], v[4], v[5]), (v[6], v[7], v[8]))
        if not flag & 4:
            s = (r.s32(p) / FX, r.s32(p + 4) / FX, r.s32(p + 8) / FX)
        return {"name": name, "flag": flag, "translation": t, "rotation": rot, "scale": s}

    def node_matrix(self, i: int):
        n = self.nodes[i]
        return mat_then(mat_scale(n["scale"]), (n["rotation"], n["translation"]))


class Nsbmd:
    def __init__(self, data: bytes, name: str = ""):
        self.name = name
        r = BinReader(data, name)
        self.r = r
        try:
            if r.slice(0, 4) != b"BMD0":
                raise NitroError("not a BMD0 file")
            nblocks = r.u16(0x0E)
            self.models: List[Model] = []
            self.has_textures = False
            for i in range(nblocks):
                bo = r.u32(0x10 + 4 * i)
                magic = r.slice(bo, 4)
                if magic == b"MDL0":
                    for mname, rel in dict_offsets(r, bo + 8):
                        self.models.append(Model(r, bo + rel, mname))
                elif magic == b"TEX0":
                    self.has_textures = True
        except (ReadOutOfBounds, struct.error) as exc:
            raise NitroError(f"{name}: {exc}") from None


# ----------------------------------------------------------------------------- geometry

GX_PARAMS = {
    0x00: 0, 0x10: 1, 0x11: 0, 0x12: 1, 0x13: 1, 0x14: 1, 0x15: 0, 0x16: 16, 0x17: 12, 0x18: 16,
    0x19: 12, 0x1A: 9, 0x1B: 3, 0x1C: 3, 0x20: 1, 0x21: 1, 0x22: 1, 0x23: 2, 0x24: 1, 0x25: 1,
    0x26: 1, 0x27: 1, 0x28: 1, 0x29: 1, 0x2A: 1, 0x2B: 1, 0x30: 1, 0x31: 1, 0x32: 1, 0x33: 1,
    0x34: 32, 0x40: 1, 0x41: 0, 0x50: 1, 0x60: 1, 0x70: 3, 0x71: 2, 0x72: 1,
}


def _s16(v: int) -> int:
    return v - 0x10000 if v & 0x8000 else v


def _s10(v: int) -> int:
    v &= 0x3FF
    return v - 0x400 if v & 0x200 else v


def _fx_mat(words, rows, cols):
    vals = [struct.unpack("<i", struct.pack("<I", w))[0] / FX for w in words]
    return [vals[i * cols:(i + 1) * cols] for i in range(rows)]


class _DLRunner:
    """Runs one GX display list and collects triangles (in model space, before posScale/matrix)."""

    def __init__(self, cur, stack, pos_scale: float):
        self.cur = cur
        self.stack = stack
        self.pos_scale = pos_scale
        self.prim = None
        self.strip: List[Vec3] = []
        self.raw = (0.0, 0.0, 0.0)
        self.tris: List[Tuple[Vec3, Vec3, Vec3]] = []
        self.polys: List[Tuple[Vec3, ...]] = []         # as drawn: triangles and quads (strips unrolled)
        self.unknown_cmds = set()

    def _emit(self, x, y, z):
        self.raw = (x, y, z)
        s = self.pos_scale
        self.strip.append(mat_apply(self.cur, (x * s, y * s, z * s)))
        v, p = self.strip, self.prim
        if p == 0 and len(v) == 3:
            self.tris.append((v[0], v[1], v[2]))
            self.polys.append((v[0], v[1], v[2]))
            self.strip = []
        elif p == 1 and len(v) == 4:
            self.tris += [(v[0], v[1], v[2]), (v[0], v[2], v[3])]
            self.polys.append((v[0], v[1], v[2], v[3]))
            self.strip = []
        elif p == 2 and len(v) >= 3:
            n = len(v)
            self.tris.append((v[n - 3], v[n - 2], v[n - 1]) if n % 2 else (v[n - 2], v[n - 3], v[n - 1]))
            self.polys.append(self.tris[-1])
        elif p == 3 and len(v) >= 4 and len(v) % 2 == 0:
            n = len(v)
            a, b, c, d = v[n - 4], v[n - 3], v[n - 2], v[n - 1]
            self.tris += [(a, b, d), (a, d, c)]
            self.polys.append((a, b, d, c))

    def run(self, r: BinReader, start: int, size: int):
        p, end = start, start + size
        if size // 4 > MAX_DL_WORDS:
            raise NitroError("display list too large")
        while p + 4 <= end:
            packed = r.u32(p)
            p += 4
            for k in range(4):
                op = (packed >> (8 * k)) & 0xFF
                n = GX_PARAMS.get(op)
                if n is None:
                    raise NitroError(f"unknown GX command 0x{op:02X} at 0x{p - 4:X}")
                if p + 4 * n > end:
                    if op == 0:
                        continue
                    raise NitroError(f"GX command 0x{op:02X} runs past the display list")
                args = [r.u32(p + 4 * i) for i in range(n)]
                p += 4 * n
                self._cmd(op, args)

    def _cmd(self, op, a):
        x, y, z = self.raw
        if op == 0x40:
            self.prim = a[0] & 3
            self.strip = []
        elif op == 0x41:
            self.strip = []
        elif op == 0x23:
            self._emit(_s16(a[0] & 0xFFFF) / FX, _s16(a[0] >> 16) / FX, _s16(a[1] & 0xFFFF) / FX)
        elif op == 0x24:
            self._emit(_s10(a[0]) / 64.0, _s10(a[0] >> 10) / 64.0, _s10(a[0] >> 20) / 64.0)
        elif op == 0x25:
            self._emit(_s16(a[0] & 0xFFFF) / FX, _s16(a[0] >> 16) / FX, z)
        elif op == 0x26:
            self._emit(_s16(a[0] & 0xFFFF) / FX, y, _s16(a[0] >> 16) / FX)
        elif op == 0x27:
            self._emit(x, _s16(a[0] & 0xFFFF) / FX, _s16(a[0] >> 16) / FX)
        elif op == 0x28:
            self._emit(x + _s10(a[0]) / FX, y + _s10(a[0] >> 10) / FX, z + _s10(a[0] >> 20) / FX)
        elif op == 0x14:
            m = self.stack[a[0] & 0x1F]
            if m is None:
                raise NitroError(f"display list restores empty matrix stack slot {a[0] & 0x1F}")
            self.cur = m
        elif op == 0x15:
            self.cur = IDENTITY
        elif op == 0x13:
            self.stack[a[0] & 0x1F] = self.cur
        elif op in (0x16, 0x17, 0x18, 0x19, 0x1A, 0x1B, 0x1C):
            if op in (0x16, 0x18):
                m = _fx_mat(a, 4, 4)
                mm = (tuple(tuple(m[i][:3]) for i in range(3)), tuple(m[3][:3]))
            elif op in (0x17, 0x19):
                m = _fx_mat(a, 4, 3)
                mm = (tuple(tuple(m[i]) for i in range(3)), tuple(m[3]))
            elif op == 0x1A:
                m = _fx_mat(a, 3, 3)
                mm = (tuple(tuple(m[i]) for i in range(3)), (0.0, 0.0, 0.0))
            elif op == 0x1B:
                s = _fx_mat(a, 1, 3)[0]
                mm = mat_scale(tuple(s))
            else:
                t = _fx_mat(a, 1, 3)[0]
                mm = (IDENTITY[0], tuple(t))
            self.cur = mm if op in (0x16, 0x17) else mat_then(mm, self.cur)
        # everything else (colour, normal, texcoord, attributes, push/pop of other matrix modes) is ignored


def model_geometry(nsbmd: Nsbmd, model_index: int = 0) -> dict:
    """Run the model's SBC and return {"triangles": [(v0, v1, v2, shape, node)], "notes": [...]}."""
    try:
        return _model_geometry(nsbmd, model_index)
    except (ReadOutOfBounds, struct.error) as exc:
        raise NitroError(f"{nsbmd.name}: {exc}") from None


def _slot(i: int) -> int:
    if not 0 <= i < STACK_SIZE:
        raise NitroError(f"matrix stack slot {i} out of range")
    return i


def _model_geometry(nsbmd: Nsbmd, model_index: int) -> dict:
    if not 0 <= model_index < len(nsbmd.models):
        raise NitroError(f"model {model_index} does not exist")
    m = nsbmd.models[model_index]
    r = nsbmd.r
    sbc = m.sbc
    stack: List[Optional[tuple]] = [None] * STACK_SIZE
    cur = IDENTITY
    notes = []
    tris = []
    polys = []
    last_node = None
    visible = {}
    shape_matrix = {}
    shape_material = {}
    cur_mat = None
    i = 0
    steps = 0
    pos_scale_in_sbc = any(b & 0x1F == 0x0B for b in sbc)   # rough: only used for the note below
    while i < len(sbc):
        steps += 1
        if steps > MAX_SBC_STEPS:
            raise NitroError("SBC does not terminate")
        op = sbc[i]
        cmd, var = op & 0x1F, op >> 5

        def arg(k):
            if i + k >= len(sbc):
                raise NitroError(f"SBC command 0x{op:02X} truncated")
            return sbc[i + k]

        if cmd == 0x00:
            i += 1
        elif cmd == 0x01:
            break
        elif cmd == 0x02:
            visible[arg(1)] = bool(arg(2) & 1)
            i += 3
        elif cmd == 0x03:
            m_ = stack[_slot(arg(1))]
            if m_ is None:
                raise NitroError(f"SBC MTX restores empty slot {arg(1)}")
            cur = m_
            i += 2
        elif cmd == 0x04:
            cur_mat = arg(1)
            i += 2
        elif cmd == 0x05:
            si = arg(1)
            if si >= len(m.shapes):
                raise NitroError(f"SBC draws shape {si} of {len(m.shapes)}")
            shape_matrix.setdefault(si, (cur, last_node))
            shape_material.setdefault(si, cur_mat)
            if visible.get(last_node, True):
                sh = m.shapes[si]
                runner = _DLRunner(cur, stack, m.pos_scale)
                runner.run(r, sh["dl_offset"], sh["dl_size"])
                tris += [(a, b, c, si, last_node) for a, b, c in runner.tris]
                polys += [(pv, si, last_node) for pv in runner.polys]
            i += 2
        elif cmd == 0x06:
            node, _parent, _flags = arg(1), arg(2), arg(3)
            n = 4
            dest = src = None
            if var == 1:
                dest = _slot(arg(4)); n = 5
            elif var == 2:
                src = _slot(arg(4)); n = 5
            elif var == 3:
                dest, src = _slot(arg(4)), _slot(arg(5)); n = 6
            if src is not None:
                if stack[src] is None:
                    raise NitroError(f"SBC NODEDESC restores empty slot {src}")
                cur = stack[src]
            if node >= len(m.nodes):
                raise NitroError(f"SBC references node {node} of {len(m.nodes)}")
            cur = mat_then(m.node_matrix(node), cur)
            last_node = node
            if dest is not None:
                stack[dest] = cur
            i += n
        elif cmd in (0x07, 0x08):
            notes.append("billboard node (drawn without billboarding)")
            i += 2 + (1 if var in (1, 2) else 2 if var == 3 else 0)
        elif cmd == 0x09:
            dest, count = _slot(arg(1)), arg(2)
            notes.append("NODEMIX (skinning) approximated by its first matrix")
            first = stack[_slot(arg(3))] if count else None
            if first is not None:
                stack[dest] = first
            i += 3 + 3 * count
        elif cmd == 0x0A:
            notes.append("CALLDL not supported (skipped)")
            i += 9
        elif cmd == 0x0B:
            i += 1          # posScale is applied to every vertex already
        elif cmd in (0x0C, 0x0D):
            i += 3
        else:
            raise NitroError(f"unknown SBC command 0x{op:02X} at {i}")
    return {"triangles": tris, "polygons": polys, "notes": sorted(set(notes)), "pos_scale": m.pos_scale,
            "pos_scale_cmd_in_sbc": pos_scale_in_sbc, "shape_matrix": shape_matrix,
            "shape_material": shape_material}


def mat_inverse(m):
    """Inverse of an affine row-vector matrix ((r0, r1, r2), t); None if singular."""
    (a, b, c), (d, e, f), (g, h, k) = m[0]
    det = a * (e * k - f * h) - b * (d * k - f * g) + c * (d * h - e * g)
    if abs(det) < 1e-12:
        return None
    inv = ((((e * k - f * h) / det), ((c * h - b * k) / det), ((b * f - c * e) / det)),
           (((f * g - d * k) / det), ((a * k - c * g) / det), ((c * d - a * f) / det)),
           (((d * h - e * g) / det), ((b * g - a * h) / det), ((a * e - b * d) / det)))
    t = m[1]
    ti = tuple(-(t[0] * inv[0][j] + t[1] * inv[1][j] + t[2] * inv[2][j]) for j in range(3))
    return (inv, ti)


def bounds(tris) -> Optional[dict]:
    if not tris:
        return None
    xs = [v[0] for t in tris for v in t[:3]]
    ys = [v[1] for t in tris for v in t[:3]]
    zs = [v[2] for t in tris for v in t[:3]]
    return {"min": [min(xs), min(ys), min(zs)], "max": [max(xs), max(ys), max(zs)]}


def triangle_normal(t) -> Vec3:
    a, b, c = t[0], t[1], t[2]
    u = (b[0] - a[0], b[1] - a[1], b[2] - a[2])
    v = (c[0] - a[0], c[1] - a[1], c[2] - a[2])
    n = (u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0])
    ln = math.sqrt(n[0] ** 2 + n[1] ** 2 + n[2] ** 2)
    return (0.0, 0.0, 0.0) if ln == 0 else (n[0] / ln, n[1] / ln, n[2] / ln)
