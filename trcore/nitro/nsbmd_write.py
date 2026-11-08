"""Add polygons to a stage model (NSBMD) without moving anything that exists.

How (spec §15; checked by re-reading the result with trcore.nitro.nsbmd):
  * the polygons are drawn by an EXISTING shape, so they use that shape's material and the stage's own
    texture (the .nsbtx is not touched) and no dictionary or render command (SBC) changes;
  * the shape's display list is copied to the end of the model with the new GX commands after it
    (BEGIN_VTXS quads/triangles, and per polygon NORMAL, then TEXCOORD + VTX_16 per corner); the shape's
    display-list offset and size, the model size and the block/file sizes are updated, and the model's
    counters and bounding box are widened. The old display list stays in place, unreferenced;
  * corners are given in stage coordinates and stored divided by the model's posScale (4.12 fixed point);
    the shape must be drawn without a node transform (checked: the new polygons must read back at the
    coordinates asked for);
  * texture coordinates continue the shape's own texture mapping: the affine (x, z) -> (s, t) map of the
    nearest upward-facing polygon of the same shape, used along walls too (same texel density). Each
    polygon is shifted by a whole number of 1024-texel periods so the values stay small (textures are
    at most 1024 texels wide and repeat, so this does not change the picture).
Polygons are drawn from the side of (P1 - P0) x (P2 - P0) (front faces; the stage materials cull back
faces), with corners in order around the outline.

A polygon can also be given as {"corners": [...], "st": [[s, t], ...]}: its texture coordinates (in the
material's texel units) are then used as given (imported models with their own UVs). A shape with no
upward-facing polygon of its own (for example one cleared by Create New Stage) takes its mapping from the
same-named shape of `tex_source` (the original model) or, failing that and if `default_texels` is given,
that many texels per unit along x and z (32 is what the game's floors use).

A shape can also be CLEARED: its display list is replaced by one that draws nothing (a single END_VTXS,
which the hardware ignores). Used to empty a stage for "Create New Stage" while keeping the model file's
structure (nodes, materials, render commands) as it is, so animations and texture bindings stay valid.
Shapes drawn under a node transform are supported: corners are taken into the node's frame first.
"""
from __future__ import annotations

import hashlib
import math
import struct
from typing import Dict, List, Optional, Sequence, Tuple

from ..binreader import BinReader
from .nsbmd import FX, GX_PARAMS, NitroError, Nsbmd, mat_apply, mat_inverse, model_geometry

Vec = Tuple[float, float, float]
VTX16_LIMIT = 0x7FFF
TEX_PERIOD = 1024.0          # texels; a multiple of every DS texture size
POS_TOLERANCE_UNITS = 2      # in 1/4096 of model units (before posScale)


class ModelEditRefused(Exception):
    pass


def _s16(v):
    return v - 0x10000 if v & 0x8000 else v


def _s10(v):
    v &= 0x3FF
    return v - 0x400 if v & 0x200 else v


def _sub(a, b):
    return tuple(x - y for x, y in zip(a, b))


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _norm(a):
    ln = math.sqrt(sum(x * x for x in a))
    return tuple(x / ln for x in a) if ln > 1e-12 else None


def _newell(poly):
    n = [0.0, 0.0, 0.0]
    for i in range(len(poly)):
        c = _cross(poly[i], poly[(i + 1) % len(poly)])
        n = [n[k] + c[k] for k in range(3)]
    return tuple(n)


def shape_polygons_with_texcoords(data: bytes, model, shape_index: int) -> List[List[Tuple[Vec, Tuple[float, float]]]]:
    """Polygons of one shape as drawn, each corner with (position x posScale, texcoord in texels).
    Positions ignore node transforms (callers check that the shape has none)."""
    r = BinReader(data)
    sh = model.shapes[shape_index]
    p, end = sh["dl_offset"], sh["dl_offset"] + sh["dl_size"]
    ps = model.pos_scale
    tc = (0.0, 0.0)
    x = y = z = 0.0
    prim, strip, polys = None, [], []
    while p + 4 <= end:
        packed = r.u32(p)
        p += 4
        for k in range(4):
            op = (packed >> (8 * k)) & 0xFF
            n = GX_PARAMS.get(op)
            if n is None:
                raise ModelEditRefused(f"unknown GX command 0x{op:02X} in shape {sh['name']}")
            if p + 4 * n > end:
                if op == 0:
                    continue
                raise ModelEditRefused(f"GX command 0x{op:02X} runs past the display list")
            a = [r.u32(p + 4 * i) for i in range(n)]
            p += 4 * n
            if op == 0x40:
                prim, strip = a[0] & 3, []
                continue
            if op == 0x41:
                strip = []
                continue
            if op == 0x22:
                tc = (_s16(a[0] & 0xFFFF) / 16.0, _s16(a[0] >> 16) / 16.0)
                continue
            if op == 0x23:
                x, y, z = _s16(a[0] & 0xFFFF) / FX, _s16(a[0] >> 16) / FX, _s16(a[1] & 0xFFFF) / FX
            elif op == 0x24:
                x, y, z = _s10(a[0]) / 64.0, _s10(a[0] >> 10) / 64.0, _s10(a[0] >> 20) / 64.0
            elif op == 0x25:
                x, y = _s16(a[0] & 0xFFFF) / FX, _s16(a[0] >> 16) / FX
            elif op == 0x26:
                x, z = _s16(a[0] & 0xFFFF) / FX, _s16(a[0] >> 16) / FX
            elif op == 0x27:
                y, z = _s16(a[0] & 0xFFFF) / FX, _s16(a[0] >> 16) / FX
            elif op == 0x28:
                x += _s10(a[0]) / FX              # VTX_DIFF: 1.0.9 fixed scaled by 1/8 = raw 1/4096 units
                y += _s10(a[0] >> 10) / FX
                z += _s10(a[0] >> 20) / FX
            else:
                continue
            strip.append(((x * ps, y * ps, z * ps), tc))
            v = strip
            if prim == 0 and len(v) == 3:
                polys.append(list(v))
                strip = []
            elif prim == 1 and len(v) == 4:
                polys.append(list(v))
                strip = []
            elif prim == 2 and len(v) >= 3:
                m = len(v)
                polys.append([v[m - 3], v[m - 2], v[m - 1]] if m % 2 else [v[m - 2], v[m - 3], v[m - 1]])
            elif prim == 3 and len(v) >= 4 and len(v) % 2 == 0:
                m = len(v)
                polys.append([v[m - 4], v[m - 3], v[m - 1], v[m - 2]])
    return polys


def texture_map_near(polys, centre: Vec) -> Optional[Tuple[Tuple[float, float, float], Tuple[float, float, float]]]:
    """Affine map (x, z, 1) -> s and -> t from the nearest upward-facing polygon of the shape."""
    best = None
    for poly in polys:
        pos = [c[0] for c in poly]
        n = _norm(_newell(pos))
        if not n or n[1] < 0.95:
            continue
        cx = sum(p[0] for p in pos) / len(pos)
        cz = sum(p[2] for p in pos) / len(pos)
        d = (cx - centre[0]) ** 2 + (cz - centre[2]) ** 2
        if best is None or d < best[0]:
            best = (d, poly)
    if best is None:
        return None
    poly = best[1]
    (p0, t0), (p1, t1), (p2, t2) = poly[0], poly[1], poly[2]
    # solve [x z 1] * [a b c]^T = s for the three corners (and t)
    rows = [(p0[0], p0[2], 1.0), (p1[0], p1[2], 1.0), (p2[0], p2[2], 1.0)]
    det = (rows[0][0] * (rows[1][1] - rows[2][1]) - rows[0][1] * (rows[1][0] - rows[2][0])
           + (rows[1][0] * rows[2][1] - rows[2][0] * rows[1][1]))
    if abs(det) < 1e-9:
        return None

    def solve(vals):
        out = []
        for col in range(3):
            m = [list(r) for r in rows]
            for i in range(3):
                m[i][col] = vals[i]
            d = (m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1]) - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
                 + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0]))
            out.append(d / det)
        return tuple(out)

    return solve([t0[0], t1[0], t2[0]]), solve([t0[1], t1[1], t2[1]])


def texcoords_for(poly: Sequence[Vec], tmap) -> List[Tuple[float, float]]:
    (sa, sb, sc), (ta, tb, tc) = tmap
    n = _norm(_newell(poly)) or (0.0, 1.0, 0.0)
    if abs(n[1]) >= 0.7:
        flat = [(p[0], p[2]) for p in poly]
    else:   # wall: run the floor mapping along the wall (horizontal) and down its height
        h = _norm((-n[2], 0.0, n[0])) or (1.0, 0.0, 0.0)
        m = _norm((n[0], 0.0, n[2])) or (0.0, 0.0, 1.0)
        flat = []
        for p in poly:
            u = p[0] * h[0] + p[2] * h[2]
            flat.append((h[0] * u - m[0] * p[1], h[2] * u - m[2] * p[1]))
    st = [(sa * x + sb * z + sc, ta * x + tb * z + tc) for x, z in flat]
    ms = sum(s for s, _ in st) / len(st)
    mt = sum(t for _, t in st) / len(st)
    ks, kt = TEX_PERIOD * round(ms / TEX_PERIOD), TEX_PERIOD * round(mt / TEX_PERIOD)
    return [(s - ks, t - kt) for s, t in st]


def _has_up(polys) -> bool:
    for poly in polys:
        n = _norm(_newell([c[0] for c in poly]))
        if n and n[1] >= 0.95:
            return True
    return False


def _near_origin(st: Sequence[Tuple[float, float]]) -> List[Tuple[float, float]]:
    """Shift given texture coordinates by whole 1024-texel periods so they stay small (repeating
    textures look the same)."""
    ms = sum(s for s, _ in st) / len(st)
    mt = sum(t for _, t in st) / len(st)
    ks, kt = TEX_PERIOD * round(ms / TEX_PERIOD), TEX_PERIOD * round(mt / TEX_PERIOD)
    return [(s - ks, t - kt) for s, t in st]


def _pack(cmds: List[Tuple[int, List[int]]]) -> bytes:
    out = bytearray()
    for i in range(0, len(cmds), 4):
        chunk = cmds[i:i + 4]
        out += bytes([c[0] for c in chunk] + [0] * (4 - len(chunk)))
        for _op, params in chunk:
            for prm in params:
                out += struct.pack("<I", prm & 0xFFFFFFFF)
    return bytes(out)


def _normal10(n: Vec) -> int:
    def f(v):
        return max(-512, min(511, int(round(v * 512)))) & 0x3FF
    return f(n[0]) | (f(n[1]) << 10) | (f(n[2]) << 20)


def _texcoord(s: float, t: float) -> int:
    si, ti = int(round(s * 16)), int(round(t * 16))
    if not (-0x8000 <= si <= 0x7FFF and -0x8000 <= ti <= 0x7FFF):
        raise ModelEditRefused("texture coordinate out of range")
    return (si & 0xFFFF) | ((ti & 0xFFFF) << 16)


def _vtx16(p: Vec, pos_scale: float) -> Tuple[List[int], Vec]:
    raw = [int(round(c / pos_scale * FX)) for c in p]
    if any(abs(v) > VTX16_LIMIT for v in raw):
        raise ModelEditRefused(f"corner {list(p)} is too far out for this model (|coordinate| must stay "
                               f"below {8 * pos_scale:g})")
    return [(raw[0] & 0xFFFF) | ((raw[1] & 0xFFFF) << 16), raw[2] & 0xFFFF], tuple(v / FX * pos_scale for v in raw)


EMPTY_DL = bytes([0x41, 0, 0, 0])          # END_VTXS: draws nothing


def append_polygons(data: bytes, shape: str, polygons: Sequence[Sequence[Sequence[float]]],
                    name: str = "", model_index: int = 0) -> Tuple[bytes, dict]:
    """Add polygons to one shape (kept for callers of the first version)."""
    new, rep = edit_model(data, {shape: polygons}, (), name)
    out = dict(rep["shapes"][shape])
    out.update({k: v for k, v in rep.items() if k != "shapes"})
    return new, out


DEFAULT_TEXELS_PER_UNIT = 32.0


def edit_model(data: bytes, add: Dict[str, Sequence], clear: Sequence[str] = (), name: str = "",
               tex_source: Optional[bytes] = None, default_texels: Optional[float] = None) -> Tuple[bytes, dict]:
    """Add polygons to shapes ({shape name: [polygon, ...]}) and/or clear shapes (draw nothing).
    A shape that is cleared and added to ends up with only the new polygons.
    tex_source: the original model, for the texture mapping of shapes that have no floor of their own.
    default_texels: texels per unit when no mapping is found at all (None: refuse instead).
    Returns (new NSBMD bytes, report). Raises ModelEditRefused if anything is off."""
    try:
        nsb = Nsbmd(data, name)
    except NitroError as exc:
        raise ModelEditRefused(f"model unreadable: {exc}") from None
    if len(nsb.models) != 1 or nsb.has_textures:
        raise ModelEditRefused("only single-model files without embedded textures are supported")
    m = nsb.models[0]
    if m.base + m.size != len(data):
        raise ModelEditRefused("the model does not end the file; cannot append safely")
    names = [s["name"] for s in m.shapes]
    for nm in list(add) + list(clear):
        if nm not in names:
            raise ModelEditRefused(f"shape {nm!r} not in the model (shapes: {names})")
    if not add and not clear:
        raise ModelEditRefused("nothing to do")
    try:
        old_geo = model_geometry(nsb)
    except NitroError as exc:
        raise ModelEditRefused(f"model unreadable: {exc}") from None
    clear_set = set(clear)
    plans = {}
    for nm, polygons in add.items():
        si = names.index(nm)
        if not polygons:
            raise ModelEditRefused(f"no polygons given for shape {nm!r}")
        if si not in old_geo["shape_matrix"]:
            raise ModelEditRefused(f"shape {nm!r} is never drawn by the model")
        mtx, _node = old_geo["shape_matrix"][si]
        inv = mat_inverse(mtx)
        r = mtx[0]
        det = (r[0][0] * (r[1][1] * r[2][2] - r[1][2] * r[2][1]) - r[0][1] * (r[1][0] * r[2][2] - r[1][2] * r[2][0])
               + r[0][2] * (r[1][0] * r[2][1] - r[1][1] * r[2][0]))
        if inv is None or det <= 0:
            raise ModelEditRefused(f"shape {nm!r} is drawn with a mirrored or flat transform")
        existing = shape_polygons_with_texcoords(data, m, si)        # local (pre-transform) x posScale
        if tex_source is not None and not _has_up(existing):
            try:
                src = Nsbmd(tex_source, name).models[0]
                ssi = [s["name"] for s in src.shapes].index(nm)
                existing = shape_polygons_with_texcoords(tex_source, src, ssi)
            except (NitroError, ValueError, IndexError):
                pass
        polys = []
        for i, poly in enumerate(polygons):
            st_given = None
            if isinstance(poly, dict):
                st_given = poly.get("st")
                poly = poly.get("corners") or []
            pts = [tuple(float(c) for c in p) for p in poly]
            if len(pts) not in (3, 4):
                raise ModelEditRefused(f"polygon {i}: give 3 or 4 corners")
            if _norm(_newell(pts)) is None:
                raise ModelEditRefused(f"polygon {i} has no area")
            if st_given is not None:
                try:
                    st_given = [(float(a), float(b)) for a, b in st_given]
                except (TypeError, ValueError):
                    raise ModelEditRefused(f"polygon {i}: texture coordinates must be pairs of numbers") from None
                if len(st_given) != len(pts):
                    raise ModelEditRefused(f"polygon {i}: one texture coordinate per corner")
            local = [mat_apply(inv, p) for p in pts]
            polys.append((pts, local, _norm(_newell(local)), st_given))
        cmds: List[Tuple[int, List[int]]] = []
        intended = []
        counts = {"quads": 0, "triangles": 0, "vertices": 0}
        for want in (4, 3):
            group = [x for x in polys if len(x[0]) == want]
            if not group:
                continue
            cmds.append((0x40, [1 if want == 4 else 0]))
            for pts, local, n, st_given in group:
                if st_given is not None:
                    tcs = _near_origin(st_given)
                else:
                    centre = tuple(sum(p[k] for p in local) / len(local) for k in range(3))
                    tmap = texture_map_near(existing, centre)
                    if tmap is None:
                        if default_texels is None:
                            raise ModelEditRefused(f"shape {nm!r} has no upward-facing polygon to take a "
                                                   "texture mapping from")
                        d = float(default_texels)
                        tmap = ((d, 0.0, 0.0), (0.0, d, 0.0))
                    tcs = texcoords_for(local, tmap)
                cmds.append((0x21, [_normal10(n)]))
                stored = []
                for p, (s_, t_) in zip(local, tcs):
                    words, back = _vtx16(p, m.pos_scale)
                    cmds.append((0x22, [_texcoord(s_, t_)]))
                    cmds.append((0x23, words))
                    stored.append(mat_apply(mtx, back))
                intended.append(stored)
                counts["quads" if want == 4 else "triangles"] += 1
                counts["vertices"] += want
            cmds.append((0x41, []))
        plans[si] = {"cmds": cmds, "intended": intended, "counts": counts}
    out = bytearray(data)
    allowed = set()

    def put(fmt, off, *vals):
        struct.pack_into(fmt, out, off, *vals)
        allowed.update(range(off, off + struct.calcsize(fmt)))

    def append(blob):
        out.extend(bytes((-len(out)) % 4))
        at = len(out)
        out.extend(blob)
        return at

    empty_at = None
    shape_reports = {}
    for si, sh in enumerate(m.shapes):
        nm = sh["name"]
        if si not in plans and nm not in clear_set:
            continue
        old_dl = data[sh["dl_offset"]:sh["dl_offset"] + sh["dl_size"]]
        if si in plans:
            # an emptied shape (END_VTXS only) gets the new polygons as its whole display list, the layout
            # Create New Stage writes and the game has drawn (edit tests 5, 6 and 9)
            base = b"" if (nm in clear_set or old_dl == EMPTY_DL) else old_dl
            new_dl = base + _pack(plans[si]["cmds"])
            at = append(new_dl)
        else:
            if empty_at is None:
                empty_at = append(EMPTY_DL)
            at, new_dl = empty_at, EMPTY_DL
        put("<II", sh["offset"] + 8, at - sh["offset"], len(new_dl))
        shape_reports[nm] = {"shape": nm, "cleared": nm in clear_set,
                             "polygons_added": len(plans[si]["intended"]) if si in plans else 0,
                             **(plans[si]["counts"] if si in plans else {}),
                             "display_list_offset": f"0x{at:X}", "display_list_size_before": len(old_dl),
                             "display_list_size_after": len(new_dl)}
    delta = len(out) - len(data)
    old_size = m.size
    put("<I", m.base, old_size + delta)
    if m.ofs_evp == old_size:
        put("<I", m.base + 0x10, old_size + delta)
    mdl0 = struct.unpack_from("<I", data, 0x10)[0]
    put("<I", mdl0 + 4, struct.unpack_from("<I", data, mdl0 + 4)[0] + delta)
    put("<I", 0x08, len(out))
    info = m.base + 0x14
    nv, npoly, ntri, nquad = struct.unpack_from("<4H", data, info + 0x10)
    gone = [p for p in old_geo["polygons"] if m.shapes[p[1]]["name"] in clear_set]
    g_quads = sum(1 for p in gone if len(p[0]) == 4)
    g_tris = len(gone) - g_quads
    a_quads = sum(p["counts"]["quads"] for p in plans.values())
    a_tris = sum(p["counts"]["triangles"] for p in plans.values())
    a_verts = sum(p["counts"]["vertices"] for p in plans.values())
    g_verts = 4 * g_quads + 3 * g_tris

    def clamp(v):
        return max(0, min(0xFFFF, v))

    put("<4H", info + 0x10, clamp(nv - g_verts + a_verts), clamp(npoly - len(gone) + a_quads + a_tris),
        clamp(ntri - g_tris + a_tris), clamp(nquad - g_quads + a_quads))
    # bounding box (x, y, z = min x, min y, max z; w, h, d) in units of boxPosScale: widened, never shrunk
    bs = m.box_pos_scale
    bx = struct.unpack_from("<6h", data, info + 0x18)
    x0, y0, z1 = bx[0] / FX * bs, bx[1] / FX * bs, bx[2] / FX * bs
    x1, y1, z0 = x0 + bx[3] / FX * bs, y0 + bx[4] / FX * bs, z1 - bx[5] / FX * bs
    outside = False
    for plan in plans.values():
        for pts in plan["intended"]:
            for p in pts:
                if not (x0 <= p[0] <= x1 and y0 <= p[1] <= y1 and z0 <= p[2] <= z1):
                    outside = True
                x0, x1, y0, y1 = min(x0, p[0]), max(x1, p[0]), min(y0, p[1]), max(y1, p[1])
                z0, z1 = min(z0, p[2]), max(z1, p[2])

    def b16(v, up, scale):
        q = v / scale * FX
        q = math.ceil(q) if up else math.floor(q)
        return q if -0x8000 <= q <= 0x7FFF else None

    box = bx
    if outside:
        scale = bs
        for _ in range(16):                    # widen boxPosScale by powers of two until the box fits
            box = (b16(x0, False, scale), b16(y0, False, scale), b16(z1, True, scale), b16(x1 - x0, True, scale),
                   b16(y1 - y0, True, scale), b16(z1 - z0, True, scale))
            if None not in box:
                break
            scale *= 2
        else:
            raise ModelEditRefused("bounding box out of range")
        if scale != bs:
            put("<ii", info + 0x24, int(round(scale * FX)), int(round(FX / scale)))
    if box != bx:
        put("<6h", info + 0x18, *box)
    new = bytes(out)
    worst = _verify(data, new, nsb, old_geo, plans, clear_set, allowed, name)
    return new, {"shapes": shape_reports, "readback_max_error": worst, "size_before": len(data),
                 "size_after": len(new), "sha256_before": hashlib.sha256(data).hexdigest(),
                 "sha256_after": hashlib.sha256(new).hexdigest(), "bounding_box_changed": box != bx,
                 "material_note": "new polygons are drawn with their shape's own material and texture"}


def _verify(old: bytes, new: bytes, nsb_old, old_geo, plans, clear_set, allowed, name) -> float:
    stray = [i for i in range(len(old)) if old[i] != new[i] and i not in allowed]
    if stray:
        raise ModelEditRefused(f"internal: bytes outside the allowed fields changed (first at 0x{stray[0]:X})")
    try:
        nsb = Nsbmd(new, name)
        geo = model_geometry(nsb)
    except NitroError as exc:
        raise ModelEditRefused(f"edited model does not read back: {exc}") from None
    m_old, m = nsb_old.models[0], nsb.models[0]
    if [s["name"] for s in m.shapes] != [s["name"] for s in m_old.shapes] or m.materials != m_old.materials:
        raise ModelEditRefused("internal: shapes or materials changed")
    if m.sbc != m_old.sbc or len(m.nodes) != len(m_old.nodes):
        raise ModelEditRefused("internal: render commands or nodes changed")
    worst = 0.0
    for si, sh in enumerate(m.shapes):
        mine_old = [p for p in old_geo["polygons"] if p[1] == si]
        mine_new = [p for p in geo["polygons"] if p[1] == si]
        cleared = sh["name"] in clear_set
        keep = [] if cleared else mine_old
        added = plans[si]["intended"] if si in plans else []
        if mine_new[:len(keep)] != keep or len(mine_new) != len(keep) + len(added):
            raise ModelEditRefused(f"internal: shape {sh['name']!r} reads back with the wrong polygons")
        rows = old_geo["shape_matrix"][si][0][0] if si in old_geo["shape_matrix"] else ((1, 0, 0),) * 3
        sx = max(1.0, max(abs(v) for row in rows for v in row))
        tol = POS_TOLERANCE_UNITS / FX * m.pos_scale * sx * 2
        for (verts, _shape, _node), want in zip(mine_new[len(keep):], added):
            if len(verts) != len(want):
                raise ModelEditRefused("internal: a new polygon reads back with a different corner count")
            for a, b in zip(verts, want):
                d = max(abs(x - y) for x, y in zip(a, b))
                worst = max(worst, d)
                if d > tol:
                    raise ModelEditRefused("a new polygon reads back elsewhere than asked")
        if not cleared and si in plans:
            again = shape_polygons_with_texcoords(new, m, si)
            before = shape_polygons_with_texcoords(old, m_old, si)
            if again[:len(before)] != before:
                raise ModelEditRefused("internal: the shape's texture coordinates changed")
    return worst



def _sbc_pos_scale_ok(sbc: bytes) -> Optional[str]:
    """None if posScale is applied only around shape draws (POSSCALE 0x0B ... 0x2B with no node or matrix
    command inside, as in all 119 stage models with posScale != 1); otherwise why not."""
    lens = {0x00: 1, 0x01: 1, 0x02: 3, 0x03: 2, 0x04: 2, 0x05: 2, 0x0B: 1, 0x0C: 3, 0x0D: 3}
    i, inside, seen = 0, False, False
    while i < len(sbc):
        op = sbc[i]
        c, v = op & 0x1F, op >> 5
        if c == 0x06:
            n = 4 + (1 if v in (1, 2) else 2 if v == 3 else 0)
        elif c in (0x07, 0x08):
            n = 2 + (1 if v in (1, 2) else 2 if v == 3 else 0)
        elif c == 0x09:
            n = 3 + 3 * (sbc[i + 2] if i + 2 < len(sbc) else 0)
        elif c == 0x0A:
            n = 9
        elif c in lens:
            n = lens[c]
        else:
            return f"unknown render command 0x{op:02X}"
        if c == 0x0B:
            inside, seen = (v == 0), True
        elif c == 0x05 and not inside:
            return "a shape is drawn outside the position-scale block"
        elif inside and c in (0x03, 0x06, 0x07, 0x08, 0x09, 0x0A):
            return "a node or matrix command sits inside the position-scale block"
        if c == 0x01:
            break
        i += n
    return None if seen else "the model never applies its position scale"


def _encode_vertex(q: Tuple[int, int, int], prev: Optional[Tuple[int, int, int]]) -> Tuple[int, List[int]]:
    """Shortest GX vertex command that lands exactly on raw coordinates q (1/4096 units)."""
    x, y, z = q
    if prev is not None:
        px, py, pz = prev
        if z == pz:
            return 0x25, [(x & 0xFFFF) | ((y & 0xFFFF) << 16)]
        if y == py:
            return 0x26, [(x & 0xFFFF) | ((z & 0xFFFF) << 16)]
        if x == px:
            return 0x27, [(y & 0xFFFF) | ((z & 0xFFFF) << 16)]
        d = (x - px, y - py, z - pz)
        if all(-512 <= c <= 511 for c in d):
            return 0x28, [(d[0] & 0x3FF) | ((d[1] & 0x3FF) << 10) | ((d[2] & 0x3FF) << 20)]
    if all(c % 64 == 0 and -512 <= c // 64 <= 511 for c in q):
        return 0x24, [((x // 64) & 0x3FF) | (((y // 64) & 0x3FF) << 10) | (((z // 64) & 0x3FF) << 20)]
    return 0x23, [(x & 0xFFFF) | ((y & 0xFFFF) << 16), z & 0xFFFF]


def rescale_positions(data: bytes, new_scale: float, name: str = "") -> Tuple[bytes, dict]:
    """Change the model's position scale (posScale) and re-encode every vertex so the model looks the same.

    Vertices are stored in 4.12 fixed point and multiplied by posScale (render command POSSCALE), so they
    reach +-8 x posScale; a larger posScale widens the range in which polygons can be drawn (spec §15, the
    owner's "size unit"). Every vertex is rounded to the new grid (1/4096 x new posScale) and written with the
    shortest exact vertex command; every other command is copied. The display lists are rewritten in place
    when they fill the end of the model, otherwise appended."""
    try:
        nsb = Nsbmd(data, name)
        old_geo = model_geometry(nsb)
    except NitroError as exc:
        raise ModelEditRefused(f"model unreadable: {exc}") from None
    if len(nsb.models) != 1 or nsb.has_textures:
        raise ModelEditRefused("only single-model files without embedded textures are supported")
    m = nsb.models[0]
    if m.base + m.size != len(data):
        raise ModelEditRefused("the model does not end the file")
    why = _sbc_pos_scale_ok(bytes(m.sbc))
    if why:
        raise ModelEditRefused(f"position scale cannot be changed: {why}")
    old_scale = m.pos_scale
    if not (0 < new_scale <= 64) or abs(new_scale * FX - round(new_scale * FX)) > 1e-9:
        raise ModelEditRefused("position scale must be a multiple of 1/4096 between 0 and 64")
    ratio = old_scale / new_scale
    r = BinReader(data)
    counts = {"vertices": 0, "commands": 0}
    new_dls: List[bytes] = []
    for sh in m.shapes:
        p, end = sh["dl_offset"], sh["dl_offset"] + sh["dl_size"]
        cmds: List[Tuple[int, List[int]]] = []
        x = y = z = 0.0                      # current vertex, raw 1/4096 units at the old scale
        prev: Optional[Tuple[int, int, int]] = None
        while p + 4 <= end:
            packed = r.u32(p)
            p += 4
            for k in range(4):
                op = (packed >> (8 * k)) & 0xFF
                n = GX_PARAMS.get(op)
                if n is None:
                    raise ModelEditRefused(f"unknown GX command 0x{op:02X} in shape {sh['name']}")
                if op == 0:
                    continue
                if p + 4 * n > end:
                    raise ModelEditRefused(f"GX command 0x{op:02X} runs past the display list")
                a = [r.u32(p + 4 * i) for i in range(n)]
                p += 4 * n
                counts["commands"] += 1
                if 0x23 <= op <= 0x28:
                    if op == 0x23:
                        x, y, z = _s16(a[0] & 0xFFFF), _s16(a[0] >> 16), _s16(a[1] & 0xFFFF)
                    elif op == 0x24:
                        x, y, z = _s10(a[0]) * 64, _s10(a[0] >> 10) * 64, _s10(a[0] >> 20) * 64
                    elif op == 0x25:
                        x, y = _s16(a[0] & 0xFFFF), _s16(a[0] >> 16)
                    elif op == 0x26:
                        x, z = _s16(a[0] & 0xFFFF), _s16(a[0] >> 16)
                    elif op == 0x27:
                        y, z = _s16(a[0] & 0xFFFF), _s16(a[0] >> 16)
                    else:
                        x, y, z = x + _s10(a[0]), y + _s10(a[0] >> 10), z + _s10(a[0] >> 20)
                    q = tuple(int(round(v * ratio)) for v in (x, y, z))
                    if any(not -0x8000 <= c <= VTX16_LIMIT for c in q):
                        raise ModelEditRefused("a vertex does not fit at the new scale")
                    cmds.append(_encode_vertex(q, prev))
                    prev = q
                    counts["vertices"] += 1
                else:
                    if op not in (0x20, 0x21, 0x22, 0x40, 0x41):
                        prev = None              # be conservative: next vertex absolute
                    cmds.append((op, a))
        new_dls.append(_pack(cmds))
    out = bytearray(data)
    allowed = set()

    def put(fmt, off, *vals):
        struct.pack_into(fmt, out, off, *vals)
        allowed.update(range(off, off + struct.calcsize(fmt)))

    region = sorted((sh["dl_offset"], sh["dl_offset"] + sh["dl_size"]) for sh in m.shapes)
    start = region[0][0] if region else len(data)
    contiguous = bool(region) and region[-1][1] == len(data) and all(
        b[0] - a[1] < 4 and b[0] >= a[1] for a, b in zip(region, region[1:]))
    if contiguous and start % 4 == 0:
        del out[start:]
        allowed.update(range(start, len(data)))
        mode = "rewritten in place"
    else:
        out.extend(bytes((-len(out)) % 4))
        mode = "appended"
    for sh, dl in zip(m.shapes, new_dls):
        at = len(out)
        out.extend(dl)
        put("<II", sh["offset"] + 8, at - sh["offset"], len(dl))
    delta = len(out) - len(data)
    put("<I", m.base, m.size + delta)
    mdl0 = struct.unpack_from("<I", data, 0x10)[0]
    put("<I", mdl0 + 4, struct.unpack_from("<I", data, mdl0 + 4)[0] + delta)
    put("<I", 0x08, len(out))
    info = m.base + 0x14
    put("<ii", info + 0x08, int(round(new_scale * FX)), int(round(FX / new_scale)))
    new = bytes(out)
    stray = [i for i in range(min(len(data), len(new))) if data[i] != new[i] and i not in allowed]
    if stray:
        raise ModelEditRefused(f"internal: bytes outside the allowed fields changed (first at 0x{stray[0]:X})")
    try:
        nsb2 = Nsbmd(new, name)
        geo = model_geometry(nsb2)
    except NitroError as exc:
        raise ModelEditRefused(f"rescaled model does not read back: {exc}") from None
    if len(geo["polygons"]) != len(old_geo["polygons"]):
        raise ModelEditRefused("internal: polygon count changed")
    worst = 0.0
    for (va, sa, _na), (vb, sb, _nb) in zip(old_geo["polygons"], geo["polygons"]):
        if sa != sb or len(va) != len(vb):
            raise ModelEditRefused("internal: polygon order changed")
        for p1, p2 in zip(va, vb):
            worst = max(worst, max(abs(u - v) for u, v in zip(p1, p2)))
    limit = 0.5 * new_scale / FX * math.sqrt(3) + 1e-6   # half a step of the new grid on each axis, any node rotation
    if worst > 1.01 * limit * max(1.0, max((abs(c) for n in m.nodes for c in n["scale"]), default=1.0)):
        raise ModelEditRefused(f"rescaled vertices moved by {worst:.5f}")
    m2 = nsb2.models[0]
    for si in range(len(m.shapes)):
        a1 = [[t for _p, t in poly] for poly in shape_polygons_with_texcoords(data, m, si)]
        a2 = [[t for _p, t in poly] for poly in shape_polygons_with_texcoords(new, m2, si)]
        if a1 != a2:
            raise ModelEditRefused("internal: texture coordinates changed")
    return new, {"pos_scale_before": old_scale, "pos_scale_after": new_scale, "drawing_range_before": 8 * old_scale,
                 "drawing_range_after": 8 * new_scale, "max_vertex_move": worst, "display_lists": mode, **counts,
                 "size_before": len(data), "size_after": len(new)}
