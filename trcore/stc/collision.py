"""Stage collision geometry (read-only): quads, triangles, edges and corner points per group.

Decoded from the game's ball-collision code (ARM9, USA rev 0; spec §17):
  * bit 0 of a cell's type mask -> descriptor +0x8C count / +0x90 offset: QUADS, 0x34 bytes
  * bit 1                       -> +0x84 / +0x88: TRIANGLES, 0x34 bytes
  * bit 2                       -> +0x7C / +0x80: EDGES, 0x20 bytes
  * bit 3                       -> +0x74 / +0x78: CORNER POINTS, 0x10 bytes
Face record (0x2046050 triangles, 0x2046460 quads):
  +0x00 u32 (always 0)  +0x04 P (corner 0, 3 x Q20.12)  +0x10 N (unit normal)
  +0x1C E1, +0x28 E2: dual vectors; a point Q lies on the face when u = (Q-P).E1 >= 0, v = (Q-P).E2 >= 0
  and u + v <= 1 (triangle) or u <= 1, v <= 1 (quad, a parallelogram). The ball is only pushed out
  from the front (it must come from the side the normal points to).
Edge record (0x2045DE0): +0x00 A, +0x0C unit direction D, +0x18 length L, +0x1C u32 (always 0).
Point record (0x2045C1C): +0x00 P, +0x0C u32 (always 0).
Coordinates are stored rest-pose positions, like the objects (spec §15/§16).
"""
from __future__ import annotations

import struct
from typing import Dict, List, Optional, Tuple

FX = 4096
TABLES = {  # kind: (cell bit, count offset, data offset, record size)
    "quad": (0, 0x8C, 0x90, 0x34),
    "triangle": (1, 0x84, 0x88, 0x34),
    "edge": (2, 0x7C, 0x80, 0x20),
    "point": (3, 0x74, 0x78, 0x10),
}
BALL_MARGIN = 0.5      # cells list a piece of geometry within its x/z extent plus this (spec §17)

Vec = Tuple[float, float, float]


class CollisionError(Exception):
    pass


def _u32(b, o):
    return struct.unpack_from("<I", b, o)[0]


def _vec(b, o) -> Vec:
    if o < 0 or o + 12 > len(b):
        raise CollisionError(f"record at 0x{o:X} runs past the end of the file")
    return tuple(v / FX for v in struct.unpack_from("<3i", b, o))


def _solve(rows, rhs) -> Optional[Vec]:
    (a, b, c), (d, e, f), (g, h, i) = rows
    det = a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)
    if abs(det) < 1e-12:
        return None
    inv = ((e * i - f * h, c * h - b * i, b * f - c * e),
           (f * g - d * i, a * i - c * g, c * d - a * f),
           (d * h - e * g, b * g - a * h, a * e - b * d))
    return tuple(sum(inv[r][k] * rhs[k] for k in range(3)) / det for r in range(3))


def face_corners(p: Vec, n: Vec, e1: Vec, e2: Vec, quad: bool) -> Optional[List[Vec]]:
    """Corners of a face from its stored corner P, normal N and dual vectors E1, E2."""
    a = _solve((e1, e2, n), (1.0, 0.0, 0.0))
    b = _solve((e1, e2, n), (0.0, 1.0, 0.0))
    if a is None or b is None:
        return None
    v1 = tuple(p[k] + a[k] for k in range(3))
    v2 = tuple(p[k] + b[k] for k in range(3))
    if quad:
        return [p, v1, tuple(p[k] + a[k] + b[k] for k in range(3)), v2]
    return [p, v1, v2]


def read_group(data: bytes, descriptor_offset: int) -> Dict[str, list]:
    out: Dict[str, list] = {}
    for kind, (_bit, coff, doff, size) in TABLES.items():
        n, off = _u32(data, descriptor_offset + coff), _u32(data, descriptor_offset + doff)
        if n and off + n * size > len(data):
            raise CollisionError(f"{kind} table runs past the end of the file")
        recs = []
        for i in range(n):
            o = off + size * i
            if kind in ("quad", "triangle"):
                p, nn, e1, e2 = _vec(data, o + 4), _vec(data, o + 0x10), _vec(data, o + 0x1C), _vec(data, o + 0x28)
                recs.append({"offset": o, "word_00": _u32(data, o), "normal": nn,
                             "corners": face_corners(p, nn, e1, e2, kind == "quad")})
            elif kind == "edge":
                a, d = _vec(data, o), _vec(data, o + 0x0C)
                length = struct.unpack_from("<i", data, o + 0x18)[0] / FX
                recs.append({"offset": o, "start": a, "direction": d, "length": length,
                             "end": tuple(a[k] + d[k] * length for k in range(3)), "word_1C": _u32(data, o + 0x1C)})
            else:
                recs.append({"offset": o, "point": _vec(data, o), "word_0C": _u32(data, o + 0x0C)})
        out[kind] = recs
    return out


def read_collision(data: bytes) -> List[Dict[str, list]]:
    n, base = _u32(data, 0x14), _u32(data, 0x18)
    return [read_group(data, base + 0xCC * g) for g in range(n)]


def extent(kind: str, rec: dict) -> Optional[Tuple[float, float, float, float]]:
    """x/z extent (min x, max x, min z, max z) of one piece of collision geometry."""
    if kind in ("quad", "triangle"):
        pts = rec["corners"]
        if not pts:
            return None
    elif kind == "edge":
        pts = [rec["start"], rec["end"]]
    else:
        pts = [rec["point"]]
    xs, zs = [p[0] for p in pts], [p[2] for p in pts]
    return min(xs), max(xs), min(zs), max(zs)


def survey_stage(data: bytes, model_triangles=None) -> dict:
    """Counters: record checks, cell-list rule (margin 0.5) and, if given, closeness to the model's corners."""
    import collections
    import math
    from .cells import read_grids
    c = collections.Counter()
    grids = read_grids(data)
    groups = read_collision(data)
    near = None
    if model_triangles:
        buckets = collections.defaultdict(list)
        for t in model_triangles:
            for v in t[:3]:
                buckets[tuple(int(math.floor(x)) for x in v)].append(v)

        def near(v):
            k = tuple(int(math.floor(x)) for x in v)
            best = 9e9
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for dz in (-1, 0, 1):
                        for m in buckets.get((k[0] + dx, k[1] + dy, k[2] + dz), ()):
                            best = min(best, math.dist(v, m))
            return best
    for g, (col, grid) in enumerate(zip(groups, grids)):
        listed = {bit: collections.defaultdict(set) for bit in range(4)}
        for cell, (mask, _arr, lists) in enumerate(grid.cells):
            for bit in range(4):
                for i in (lists.get(bit, (0, None))[1] or ()):
                    listed[bit][i].add(cell)
        for kind, (bit, _c, _d, _s) in TABLES.items():
            for i, rec in enumerate(col[kind]):
                c[f"{kind}s"] += 1
                w = rec.get("word_00", rec.get("word_1C", rec.get("word_0C", 0)))
                if w:
                    c[f"{kind}s with a nonzero spare word"] += 1
                if kind in ("quad", "triangle"):
                    if rec["corners"] is None:
                        c[f"{kind}s degenerate"] += 1
                        continue
                    if abs(math.sqrt(sum(x * x for x in rec["normal"])) - 1) > 0.01:
                        c[f"{kind}s with a non-unit normal"] += 1
                    if near:
                        d = max(near(v) for v in rec["corners"])
                        c[f"{kind}s with every corner within 0.02 of a model corner"] += d < 0.02
                if kind == "edge" and abs(math.sqrt(sum(x * x for x in rec["direction"])) - 1) > 0.01:
                    c["edges with a non-unit direction"] += 1
                if grid.present:
                    ext = extent(kind, rec)
                    cx, cz = (ext[0] + ext[1]) / 2, (ext[2] + ext[3]) / 2
                    hx, hz = (ext[1] - ext[0]) / 2, (ext[3] - ext[2]) / 2
                    fp = grid.footprint(int(round(cx * FX)), int(round(cz * FX)),
                                        int(round((hx + BALL_MARGIN) * FX)), int(round((hz + BALL_MARGIN) * FX)))
                    got = listed[bit][i]
                    c[f"{kind}s: cells exactly by the 0.5 rule"] += got == fp
                    fp2 = grid.footprint(int(round(cx * FX)), int(round(cz * FX)),
                                         int(round((hx + BALL_MARGIN + 0.25) * FX)),
                                         int(round((hz + BALL_MARGIN + 0.25) * FX)))
                    c[f"{kind}s: cells within the 0.75 rule"] += got <= fp2
                    c[f"{kind}s in no cell"] += not got
    return dict(c)


def dual_vectors(p: Vec, v1: Vec, v2: Vec) -> Tuple[Vec, Vec, Vec]:
    """Normal and dual vectors (N, E1, E2) of the face with corners p, v1, v2 (inverse of face_corners)."""
    import math
    a = tuple(v1[k] - p[k] for k in range(3))
    b = tuple(v2[k] - p[k] for k in range(3))
    n = (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])
    ln = math.sqrt(sum(x * x for x in n))
    if ln < 1e-12:
        raise CollisionError("degenerate face")
    n = tuple(x / ln for x in n)
    # rows of the inverse of [a b n] (columns) give E1 (a* ), E2 (b*), N
    e1 = _solve((a, b, n), (1.0, 0.0, 0.0))
    cols = ((a[0], b[0], n[0]), (a[1], b[1], n[1]), (a[2], b[2], n[2]))
    inv_t = [_solve(cols, unit) for unit in ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))]
    # inv_t[k] = column k of M^-1 where M = [a b n]; E1 is row 0 of M^-1, E2 row 1
    e1 = tuple(inv_t[k][0] for k in range(3))
    e2 = tuple(inv_t[k][1] for k in range(3))
    return n, e1, e2


def probe(data: bytes, pos: Vec, radius: float = 0.5, group: int = 0) -> Optional[dict]:
    """What a ball at `pos` (the group's rest-pose coordinates) is touching, found the way the game does:
    only through the cell under it (None if it is outside the grid). Static check, not a physics model:
    a face counts when the ball's centre is in front of it within `radius` and its foot point lies on
    the face; an edge or point counts when it is within `radius` of the centre."""
    import math
    from .cells import read_grids
    grid = read_grids(data)[group]
    if not grid.present:
        return None
    i = (int(round(pos[0] * FX)) - grid.ox) // grid.sx
    j = (int(round(pos[2] * FX)) - grid.oz) // grid.sz
    if not (0 <= i < grid.nx and 0 <= j < grid.nz):
        return None
    _mask, _arr, lists = grid.cells[j * grid.nx + i]
    col = read_collision(data)[group]
    out = {"cell": j * grid.nx + i, "quad": [], "triangle": [], "edge": [], "point": []}
    for kind, (bit, _c, _d, size) in TABLES.items():
        for li in (lists.get(bit, (0, None))[1] or ()):
            rec = col[kind][li]
            if kind in ("quad", "triangle"):
                cs = rec["corners"]
                if not cs:
                    continue
                n = rec["normal"]
                p = cs[0]
                d = sum((pos[k] - p[k]) * n[k] for k in range(3))
                if not -1e-3 <= d <= radius + 1e-3:
                    continue
                foot = tuple(pos[k] - d * n[k] for k in range(3))
                v1, v2 = (cs[1], cs[3]) if kind == "quad" else (cs[1], cs[2])
                _n, e1, e2 = dual_vectors(p, v1, v2)
                u = sum((foot[k] - p[k]) * e1[k] for k in range(3))
                v = sum((foot[k] - p[k]) * e2[k] for k in range(3))
                ok = (u >= -1e-4 and v >= -1e-4 and (u + v <= 1 + 1e-4 if kind == "triangle" else
                                                    (u <= 1 + 1e-4 and v <= 1 + 1e-4)))
                if ok:
                    out[kind].append(li)
            elif kind == "edge":
                a, b = rec["start"], rec["end"]
                ab = tuple(b[k] - a[k] for k in range(3))
                L2 = sum(x * x for x in ab) or 1e-12
                t = max(0.0, min(1.0, sum((pos[k] - a[k]) * ab[k] for k in range(3)) / L2))
                q = tuple(a[k] + ab[k] * t for k in range(3))
                if math.dist(q, pos) <= radius + 1e-3:
                    out[kind].append(li)
            else:
                if math.dist(rec["point"], pos) <= radius + 1e-3:
                    out[kind].append(li)
    out["touching"] = any(out[k] for k in ("quad", "triangle", "edge", "point"))
    return out
