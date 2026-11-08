"""Edit a group's collision geometry: remove what lies inside a box, add new polygons (spec §17).

Rules this follows (spec §16/§17, Level 2 from game code + corpus; edit test 4 checks them in-game):
  * the four tables (quads, triangles, edges, corner points) belong to one group; cell bits 0-3 list
    their records by local index (sorted u16, 0xFFFF-terminated; offset 0 = empty list);
  * the ball tests only what the cell under it lists, and a ball outside the grid tests nothing
    (cell lookup 0x02044CD4 returns no cell), so new geometry must lie inside the grid: the grid is
    grown by whole cells when needed (only the loader 0x02046CC4 and the lookup read its fields).

How (nothing that already exists is moved, as for objects):
  * a changed table is rebuilt (kept records in their order and byte-for-byte, then the new ones) and
    appended at the end of the file; the group's count/offset is updated;
  * kept records keep exactly the cells they had; new records get the cells of their x/z extent plus
    0.75 (the game's own rule is extent + 0.5; every original piece but one fits 0.75);
  * new lists and new per-cell offset arrays are appended (each cell keeps its own array, as in every
    original file); cell entries are rewritten in place, or a new cell table is appended if the grid grows.

Operations (in order; "group" defaults to 0):
  {"op": "remove_box", "group": 0, "x": [x0, x1], "z": [z0, z1], "y": [y0, y1]}
      Removes the collision inside the box. Any of x / z / y may be left out (no limit on that axis).
      Faces and edges that cross the box's x or z sides are cut there; the parts outside stay, and the
      cut gets edges and corner points so the ball rolls over it as over any other floor edge. Only
      pieces lying entirely within the y range are affected.
  {"op": "clear", "group": 0}
      Removes all of the group's collision (faces, edges, points); used to empty a stage.
  {"op": "add_polygons", "group": 0, "polygons": [[[x, y, z], ...], ...]}
      Adds convex planar polygons. The ball collides from the side of (P1 - P0) x (P2 - P0). A planar
      parallelogram becomes a quad, anything else a fan of triangles; every polygon side becomes an edge
      and every corner a corner point.
"""
from __future__ import annotations

import hashlib
import math
import struct
from typing import Dict, List, Optional, Sequence, Set, Tuple

from .cells import FX, KIND_BIT, MARGIN, CellError, goal_extent, read_grids
from .cells import survey_stage as survey_cells
from .collision import TABLES, CollisionError, dual_vectors, face_corners, read_collision
from .document import StageDocument

KINDS = ("quad", "triangle", "edge", "point")
RECORD_SIZE = {k: TABLES[k][3] for k in KINDS}
NEW_MARGIN = 0.75
GAME_MARGIN = 0.5
EPS = 1e-6
MAX_CELLS = 16384
DECODE_TOLERANCE = 0.02
Q_LIMIT = (1 << 31) - 1
OBJECT_KINDS = {"goal": ("A", 0x24), "bumper": ("B", 0x24), "banana": ("C", 0x00)}

Vec = Tuple[float, float, float]


class CollisionEditRefused(Exception):
    pass


# ----------------------------------------------------------------------------- small vector helpers
def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _add(a, b):
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def _mul(a, s):
    return (a[0] * s, a[1] * s, a[2] * s)


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _len(a):
    return math.sqrt(_dot(a, a))


def _q(v: float, what: str = "value") -> int:
    if not isinstance(v, (int, float)) or not math.isfinite(v):
        raise CollisionEditRefused(f"{what} is not a finite number: {v!r}")
    r = int(round(v * FX))
    if not -Q_LIMIT <= r <= Q_LIMIT:
        raise CollisionEditRefused(f"{what} {v} is out of range")
    return r


def _point(v, what="point") -> Vec:
    if not isinstance(v, (list, tuple)) or len(v) != 3:
        raise CollisionEditRefused(f"{what} must be [x, y, z], got {v!r}")
    for c in v:
        _q(c, what)
    # stored values are Q20.12: work with exactly what will be written
    return tuple(_q(c, what) / FX for c in v)


def newell_normal(poly: Sequence[Vec]) -> Vec:
    """Area-weighted normal (length = 2 x area), following the polygon's winding."""
    n = (0.0, 0.0, 0.0)
    for i in range(len(poly)):
        n = _add(n, _cross(poly[i], poly[(i + 1) % len(poly)]))
    return n


def polygon_area(poly: Sequence[Vec]) -> float:
    return _len(newell_normal(poly)) / 2 if len(poly) >= 3 else 0.0


def clean_polygon(poly: Sequence[Vec]) -> List[Vec]:
    """Drop repeated and collinear corners."""
    pts: List[Vec] = []
    for p in poly:
        if not pts or _len(_sub(p, pts[-1])) > 1e-5:
            pts.append(tuple(p))
    if len(pts) > 1 and _len(_sub(pts[0], pts[-1])) <= 1e-5:
        pts.pop()
    changed = True
    while changed and len(pts) >= 3:
        changed = False
        for i in range(len(pts)):
            a, b, c = pts[i - 1], pts[i], pts[(i + 1) % len(pts)]
            ab, bc = _sub(b, a), _sub(c, b)
            if _len(_cross(ab, bc)) <= 1e-9 * max(1.0, _len(ab) * _len(bc)) and _dot(ab, bc) >= 0:
                del pts[i]
                changed = True
                break
            if _len(_cross(ab, bc)) <= 1e-9 * max(1.0, _len(ab) * _len(bc)):
                del pts[i]                      # a spike folding back: also degenerate
                changed = True
                break
    return pts


def check_polygon(poly: Sequence[Vec], what: str = "polygon") -> List[Vec]:
    pts = clean_polygon(poly)
    if len(pts) < 3:
        raise CollisionEditRefused(f"{what} has fewer than 3 distinct corners")
    n = newell_normal(pts)
    ln = _len(n)
    if ln / 2 < 1e-4:
        raise CollisionEditRefused(f"{what} has no area")
    u = _mul(n, 1 / ln)
    c = _mul(pts[0], 1.0)
    for p in pts:
        if abs(_dot(_sub(p, c), u)) > 1e-3:
            raise CollisionEditRefused(f"{what} is not flat (corner {list(p)} is off its plane)")
    for i in range(len(pts)):
        a, b, cc = pts[i - 1], pts[i], pts[(i + 1) % len(pts)]
        if _dot(_cross(_sub(b, a), _sub(cc, b)), u) <= 0:
            raise CollisionEditRefused(f"{what} is not convex at corner {list(b)}")
    return pts


def is_parallelogram(pts: Sequence[Vec]) -> bool:
    return len(pts) == 4 and _len(_sub(_add(pts[0], pts[2]), _add(pts[1], pts[3]))) < 1e-5


MAX_FACE_SIZE = 4.0     # longest side of a stored face; see faces_of_polygon


def _subdivide_triangle(t: List[Vec], out: List[List[Vec]]):
    a, b, c = t
    if max(_len(_sub(b, a)), _len(_sub(c, b)), _len(_sub(a, c))) <= MAX_FACE_SIZE + 1e-9:
        out.append([a, b, c])
        return
    ab, bc, ca = _mul(_add(a, b), 0.5), _mul(_add(b, c), 0.5), _mul(_add(c, a), 0.5)
    for tt in ([a, ab, ca], [ab, b, bc], [ca, bc, c], [ab, bc, ca]):
        _subdivide_triangle(tt, out)


def faces_of_polygon(pts: Sequence[Vec]) -> List[List[Vec]]:
    """Collision faces for a convex polygon: quads if it is a parallelogram, else a triangle fan; either
    way split so that no side is longer than MAX_FACE_SIZE. Faces store their extent through dual vectors
    (1/length in Q20.12), so a long face comes back slightly too long or short (about length^2 / 8192:
    0.05 for 20 units); 4-unit pieces keep that below 0.002. The polygon's outline is unchanged."""
    pts = clean_polygon(pts)
    if len(pts) < 3:
        return []
    if is_parallelogram(pts):
        c0 = pts[0]
        a, b = _sub(pts[1], c0), _sub(pts[3], c0)
        na = max(1, math.ceil(_len(a) / MAX_FACE_SIZE - 1e-9))
        nb = max(1, math.ceil(_len(b) / MAX_FACE_SIZE - 1e-9))

        def P(i, j):
            return _add(c0, _add(_mul(a, i / na), _mul(b, j / nb)))

        return [[P(i, j), P(i + 1, j), P(i + 1, j + 1), P(i, j + 1)] for j in range(nb) for i in range(na)]
    out: List[List[Vec]] = []
    for i in range(1, len(pts) - 1):
        _subdivide_triangle([pts[0], pts[i], pts[i + 1]], out)
    return out


def clip(poly: Sequence[Vec], axis: int, value: float, keep: str) -> List[Vec]:
    """Sutherland-Hodgman: keep the part with coordinate <= value ('le') or >= value ('ge')."""
    if not poly:
        return []
    out: List[Vec] = []
    n = len(poly)

    def inside(p):
        return (p[axis] - value) if keep == "ge" else (value - p[axis])

    for i in range(n):
        a, b = poly[i], poly[(i + 1) % n]
        da, db = inside(a), inside(b)
        if da >= -EPS:
            out.append(a)
        if (da > EPS and db < -EPS) or (da < -EPS and db > EPS):
            t = da / (da - db)
            p = list(_add(a, _mul(_sub(b, a), t)))
            p[axis] = value
            out.append(tuple(p))
    return clean_polygon(out)


class Box:
    def __init__(self, op: dict):
        self.r = {}
        for axis, key in ((0, "x"), (1, "y"), (2, "z")):
            v = op.get(key)
            if v is None:
                self.r[axis] = (None, None)
                continue
            if not isinstance(v, (list, tuple)) or len(v) != 2:
                raise CollisionEditRefused(f"remove_box: {key} must be [min, max]")
            lo, hi = (_q(c, key) / FX for c in v)
            if not lo < hi:
                raise CollisionEditRefused(f"remove_box: {key} min must be below max")
            self.r[axis] = (lo, hi)
        if self.r[0] == (None, None) and self.r[2] == (None, None):
            raise CollisionEditRefused("remove_box: give x and/or z (otherwise it would remove everything)")

    def in_height(self, pts) -> bool:
        lo, hi = self.r[1]
        return all((lo is None or p[1] >= lo - EPS) and (hi is None or p[1] <= hi + EPS) for p in pts)

    def planes(self):
        for axis in (0, 2):
            lo, hi = self.r[axis]
            if lo is not None:
                yield axis, lo
            if hi is not None:
                yield axis, hi

    def on_boundary(self, poly) -> bool:
        """True if the polygon lies in one of the box's side planes: it touches the box but does not reach
        into it, so it is kept whole (the open box decides)."""
        for axis, v in self.planes():
            if all(abs(p[axis] - v) < 1e-6 for p in poly):
                return True
        return False

    def inside_part(self, poly):
        for axis in (0, 2):
            lo, hi = self.r[axis]
            if lo is not None:
                poly = clip(poly, axis, lo, "ge")
            if hi is not None:
                poly = clip(poly, axis, hi, "le")
        return poly

    def outside_pieces(self, poly) -> List[List[Vec]]:
        (x0, x1), (z0, z1) = self.r[0], self.r[2]
        pieces = []
        mid = poly
        if x0 is not None:
            pieces.append(clip(poly, 0, x0, "le"))
            mid = clip(mid, 0, x0, "ge")
        if x1 is not None:
            pieces.append(clip(poly, 0, x1, "ge"))
            mid = clip(mid, 0, x1, "le")
        if z0 is not None:
            pieces.append(clip(mid, 2, z0, "le"))
        if z1 is not None:
            pieces.append(clip(mid, 2, z1, "ge"))
        return [p for p in pieces if len(p) >= 3 and polygon_area(p) > 1e-6]

    def strictly_inside(self, p) -> bool:
        for axis in (0, 2):
            lo, hi = self.r[axis]
            if lo is not None and not p[axis] > lo + EPS:
                return False
            if hi is not None and not p[axis] < hi - EPS:
                return False
        return self.in_height([p])

    def segment_interval(self, a, b) -> Optional[Tuple[float, float]]:
        """Parameter range of a->b inside the (closed) box in x and z."""
        t0, t1 = 0.0, 1.0
        for axis in (0, 2):
            lo, hi = self.r[axis]
            d = b[axis] - a[axis]
            for bound, sign in ((lo, 1), (hi, -1)):
                if bound is None:
                    continue
                # inside: sign * (p - bound) >= 0
                num = sign * (a[axis] - bound)
                den = sign * d
                if abs(den) < 1e-12:
                    if num < -EPS:
                        return None
                    continue
                t = -num / den
                if den > 0:
                    t0 = max(t0, t)
                else:
                    t1 = min(t1, t)
        return (t0, t1) if t0 < t1 else None


# ----------------------------------------------------------------------------- records
class _Rec:
    """source: 'kept' (original, unchanged), 'cut' (a piece of original geometry left by a cut, or the
    edges/points along the cut) or 'added' (from add_polygons)."""
    __slots__ = ("kind", "data", "orig", "cells", "geom", "source", "born")

    def __init__(self, kind, data, orig=None, cells=None, geom=None, source=None):
        self.kind, self.data, self.orig, self.cells, self.geom = kind, bytes(data), orig, cells, geom
        self.source = source or ("kept" if orig is not None else "added")
        self.born = _GroupEdit.current_op if orig is None else -1      # op that made it (-1: original)


def encode_face(corners: Sequence[Vec]) -> Tuple[str, bytes]:
    if len(corners) == 4:
        kind, p, v1, v2 = "quad", corners[0], corners[1], corners[3]
    elif len(corners) == 3:
        kind, (p, v1, v2) = "triangle", corners
    else:
        raise CollisionEditRefused("internal: a face needs 3 or 4 corners")
    try:
        n, e1, e2 = dual_vectors(p, v1, v2)
    except CollisionError as exc:
        raise CollisionEditRefused(f"face {[list(c) for c in corners]}: {exc}") from None
    vals = [_q(c, "face value") for vec in (p, n, e1, e2) for c in vec]
    return kind, struct.pack("<I12i", 0, *vals)


def encode_edge(a: Vec, b: Vec) -> bytes:
    d = _sub(b, a)
    ln = _len(d)
    if ln < 1e-6:
        raise CollisionEditRefused("internal: zero-length edge")
    u = _mul(d, 1 / ln)
    return struct.pack("<3i3iiI", *(_q(c, "edge") for c in a), *(_q(c, "edge") for c in u), _q(ln, "edge"), 0)


def encode_point(p: Vec) -> bytes:
    return struct.pack("<3iI", *(_q(c, "point") for c in p), 0)


def _decode(kind: str, data: bytes) -> dict:
    vec = lambda o: tuple(v / FX for v in struct.unpack_from("<3i", data, o))
    if kind in ("quad", "triangle"):
        p, n, e1, e2 = vec(4), vec(0x10), vec(0x1C), vec(0x28)
        return {"corners": face_corners(p, n, e1, e2, kind == "quad"), "normal": n}
    if kind == "edge":
        a, d = vec(0), vec(0x0C)
        ln = struct.unpack_from("<i", data, 0x18)[0] / FX
        return {"start": a, "end": _add(a, _mul(d, ln))}
    return {"point": vec(0)}


def _points_of(kind: str, geom: dict) -> List[Vec]:
    if kind in ("quad", "triangle"):
        return list(geom["corners"])
    if kind == "edge":
        return [geom["start"], geom["end"]]
    return [geom["point"]]


def extent_of(points: Sequence[Vec]) -> Tuple[float, float, float, float]:
    xs, zs = [p[0] for p in points], [p[2] for p in points]
    return min(xs), max(xs), min(zs), max(zs)


def _key(p: Vec) -> Tuple[int, int, int]:
    return tuple(int(round(c * FX)) for c in p)


class _GroupEdit:
    """Records of one group, in table order, with the cells each is listed in."""
    current_op = -1

    def __init__(self, data: bytes, g: int, grid, col: dict):
        self.g = g
        self.grid = grid
        self.recs: Dict[str, List[_Rec]] = {}
        listed = {bit: {} for bit in range(4)}
        for cell, (mask, _arr, lists) in enumerate(grid.cells):
            for bit in range(4):
                for i in (lists.get(bit, (0, None))[1] or ()):
                    listed[bit].setdefault(i, set()).add(cell)
        for kind in KINDS:
            bit, _c, _d, size = TABLES[kind]
            out = []
            for i, rec in enumerate(col[kind]):
                raw = data[rec["offset"]:rec["offset"] + size]
                geom = _decode(kind, raw)
                if kind in ("quad", "triangle") and geom["corners"] is None:
                    geom = None                     # degenerate original face: kept as it is, never cut
                out.append(_Rec(kind, raw, i, set(listed[bit].get(i, ())), geom))
            self.recs[kind] = out
        self.changed: Set[str] = set()
        self.new_points: Set[Tuple[int, int, int]] = set()

    def add_face_polygon(self, pts: Sequence[Vec], source: str = "added") -> int:
        n = 0
        for corners in faces_of_polygon(pts):
            kind, raw = encode_face(corners)
            self.recs[kind].append(_Rec(kind, raw, None, None, {"corners": list(corners),
                                                                 "intended": list(corners)}, source))
            self.changed.add(kind)
            n += 1
        return n

    def add_edge(self, a: Vec, b: Vec, seen: Set, source: str = "added") -> bool:
        a, b = tuple(_q(c) / FX for c in a), tuple(_q(c) / FX for c in b)
        k = tuple(sorted((_key(a), _key(b))))
        if k in seen or k[0] == k[1]:
            return False
        seen.add(k)
        self.recs["edge"].append(_Rec("edge", encode_edge(a, b), None, None,
                                      {"start": a, "end": b, "intended": [a, b]}, source))
        self.changed.add("edge")
        return True

    def add_point(self, p: Vec, source: str = "added") -> bool:
        p = tuple(_q(c) / FX for c in p)
        k = _key(p)
        if k in self.new_points:
            return False
        self.new_points.add(k)
        self.recs["point"].append(_Rec("point", encode_point(p), None, None, {"point": p, "intended": [p]}, source))
        self.changed.add("point")
        return True


# ----------------------------------------------------------------------------- operations
def _remove_box(ge: _GroupEdit, op: dict) -> dict:
    box = Box(op)
    rep = {"faces_removed": 0, "faces_cut": 0, "face_pieces_added": 0, "edges_removed": 0, "edges_cut": 0,
           "edge_pieces_added": 0, "points_removed": 0, "cut_edges_added": 0, "cut_points_added": 0,
           "kept_crossing_height_range": 0}
    seen_edges: Set = set()
    cut_edges: List[Tuple[Vec, Vec]] = []
    for kind in ("quad", "triangle"):
        keep = []
        for r in ge.recs[kind]:
            if r.geom is None:
                keep.append(r)
                continue
            pts = list(r.geom["corners"])
            inside = box.inside_part(pts)
            if len(inside) < 3 or polygon_area(inside) <= 1e-6 or box.on_boundary(pts):
                keep.append(r)
                continue
            if not box.in_height(pts):
                lo, hi = box.r[1]
                if any((lo is None or p[1] > lo + EPS) and (hi is None or p[1] < hi - EPS) for p in pts):
                    rep["kept_crossing_height_range"] += 1     # partly inside the y range: left whole
                keep.append(r)
                continue
            ge.changed.add(kind)
            pieces = box.outside_pieces(pts)
            if not pieces:
                rep["faces_removed"] += 1
                continue
            rep["faces_cut"] += 1
            for piece in pieces:
                for corners in faces_of_polygon(piece):
                    k2, raw = encode_face(corners)
                    target = keep if k2 == kind else ge.recs[k2]
                    target.append(_Rec(k2, raw, None, None, {"corners": list(corners), "intended": list(corners)},
                                       "cut"))
                    ge.changed.add(k2)
                    rep["face_pieces_added"] += 1
                for i in range(len(piece)):
                    a, b = piece[i], piece[(i + 1) % len(piece)]
                    for axis, v in box.planes():
                        if abs(a[axis] - v) < 1e-6 and abs(b[axis] - v) < 1e-6 and \
                                not all(abs(c[axis] - v) < 1e-6 for c in pts):
                            cut_edges.append((a, b))
                            break
        ge.recs[kind] = keep
    keep = []
    for r in ge.recs["edge"]:
        a, b = r.geom["start"], r.geom["end"]
        iv = box.segment_interval(a, b) if box.in_height([a, b]) else None
        ln = _len(_sub(b, a))
        if iv is None or (iv[1] - iv[0]) * ln <= 1e-6:
            keep.append(r)
            continue
        mid = _add(a, _mul(_sub(b, a), (iv[0] + iv[1]) / 2))
        if not box.strictly_inside(mid):
            keep.append(r)
            continue
        ge.changed.add("edge")
        parts = []
        if iv[0] * ln > 1e-6:
            parts.append((a, _add(a, _mul(_sub(b, a), iv[0]))))
        if (1 - iv[1]) * ln > 1e-6:
            parts.append((_add(a, _mul(_sub(b, a), iv[1])), b))
        if not parts:
            rep["edges_removed"] += 1
        else:
            rep["edges_cut"] += 1
        for p0, p1 in parts:
            p0, p1 = tuple(_q(c) / FX for c in p0), tuple(_q(c) / FX for c in p1)
            keep.append(_Rec("edge", encode_edge(p0, p1), None, None, {"start": p0, "end": p1,
                                                                        "intended": [p0, p1]}, "cut"))
            rep["edge_pieces_added"] += 1
    ge.recs["edge"] = keep
    keep = []
    for r in ge.recs["point"]:
        if box.strictly_inside(r.geom["point"]):
            rep["points_removed"] += 1
            ge.changed.add("point")
        else:
            keep.append(r)
    ge.recs["point"] = keep
    for r in ge.recs["edge"]:
        if r.orig is None:
            seen_edges.add(tuple(sorted((_key(r.geom["start"]), _key(r.geom["end"])))))
    for a, b in cut_edges:
        if ge.add_edge(a, b, seen_edges, "cut"):
            rep["cut_edges_added"] += 1
        for p in (a, b):
            if ge.add_point(p, "cut"):
                rep["cut_points_added"] += 1
    for kind in KINDS:                         # kept records first, in their order, then the new ones
        ge.recs[kind].sort(key=lambda r: r.orig is None)
    rep["box"] = {k: list(v) for k, v in zip("xyz", (box.r[0], box.r[1], box.r[2])) if v != (None, None)}
    return rep


def _add_polygons(ge: _GroupEdit, op: dict) -> dict:
    polys = op.get("polygons")
    if not isinstance(polys, list) or not polys:
        raise CollisionEditRefused("add_polygons: give a non-empty 'polygons' list")
    rep = {"polygons": len(polys), "faces_added": 0, "edges_added": 0, "points_added": 0}
    seen: Set = set()
    for i, poly in enumerate(polys):
        if not isinstance(poly, list):
            raise CollisionEditRefused(f"polygon {i}: must be a list of [x, y, z] corners")
        pts = check_polygon([_point(p, f"polygon {i} corner") for p in poly], f"polygon {i}")
        rep["faces_added"] += ge.add_face_polygon(pts)
        for k in range(len(pts)):
            if ge.add_edge(pts[k], pts[(k + 1) % len(pts)], seen):
                rep["edges_added"] += 1
            if ge.add_point(pts[k]):
                rep["points_added"] += 1
    return rep


# ----------------------------------------------------------------------------- grid
class _GridPlan:
    """Old grid, possibly grown by whole cells; maps old cell indices to new ones."""

    def __init__(self, grid, need: Optional[Tuple[float, float, float, float]]):
        self.old = grid
        self.nx, self.nz, self.ox, self.oz, self.sx, self.sz = grid.nx, grid.nz, grid.ox, grid.oz, grid.sx, grid.sz
        self.di = self.dj = 0
        if need:
            x0, x1, z0, z1 = (_q(v) for v in need)
            add_l = max(0, -((x0 - grid.ox) // grid.sx))
            add_r = max(0, -(-(x1 + 1 - (grid.ox + grid.nx * grid.sx)) // grid.sx))
            add_t = max(0, -((z0 - grid.oz) // grid.sz))
            add_b = max(0, -(-(z1 + 1 - (grid.oz + grid.nz * grid.sz)) // grid.sz))
            self.di, self.dj = add_l, add_t
            self.nx, self.nz = grid.nx + add_l + add_r, grid.nz + add_t + add_b
            self.ox, self.oz = grid.ox - add_l * grid.sx, grid.oz - add_t * grid.sz
        if self.nx * self.nz > MAX_CELLS or self.nx > 0x7FFF or self.nz > 0x7FFF:
            raise CollisionEditRefused(f"the collision grid would need {self.nx} x {self.nz} cells; that is too large")

    @property
    def grown(self) -> bool:
        return (self.nx, self.nz) != (self.old.nx, self.old.nz)

    def new_index(self, c_old: int) -> int:
        i, j = c_old % self.old.nx, c_old // self.old.nx
        return (j + self.dj) * self.nx + (i + self.di)

    def old_cells_new(self) -> Set[int]:
        return {self.new_index(c) for c in range(self.old.nx * self.old.nz)}

    def footprint(self, ext, margin: float) -> Set[int]:
        x0, x1, z0, z1 = ext
        i0 = max(0, (_q(x0 - margin) - self.ox) // self.sx)
        i1 = min(self.nx - 1, (_q(x1 + margin) - self.ox) // self.sx)
        j0 = max(0, (_q(z0 - margin) - self.oz) // self.sz)
        j1 = min(self.nz - 1, (_q(z1 + margin) - self.oz) // self.sz)
        return {j * self.nx + i for i in range(i0, i1 + 1) for j in range(j0, j1 + 1)}

    def footprint_raw(self, x, z, rx, rz) -> Set[int]:
        i0 = max(0, (x - rx - self.ox) // self.sx)
        i1 = min(self.nx - 1, (x + rx - self.ox) // self.sx)
        j0 = max(0, (z - rz - self.oz) // self.sz)
        j1 = min(self.nz - 1, (z + rz - self.oz) // self.sz)
        return {j * self.nx + i for i in range(i0, i1 + 1) for j in range(j0, j1 + 1)}

    def covers(self, ext, margin: float) -> bool:
        x0, x1, z0, z1 = ext
        return (_q(x0 - margin) >= self.ox and _q(x1 + margin) < self.ox + self.nx * self.sx and
                _q(z0 - margin) >= self.oz and _q(z1 + margin) < self.oz + self.nz * self.sz)


# ----------------------------------------------------------------------------- main entry
def apply_collision_ops(decoded: bytes, ops: List[dict], name: str = "") -> Tuple[bytes, dict]:
    """Return (new_bytes, report). Raises CollisionEditRefused with the reason if anything is off."""
    doc = StageDocument(decoded, name)
    if doc.container != "plain" or doc.has_errors():
        raise CollisionEditRefused("needs a decoded stage without structural errors")
    if doc.layout_variant != "common":
        raise CollisionEditRefused(f"layout variant {doc.layout_variant!r} is not supported for collision edits")
    if not ops:
        raise CollisionEditRefused("no collision operations given")
    try:
        grids = read_grids(decoded)
        cols = read_collision(decoded)
    except (CellError, CollisionError) as exc:
        raise CollisionEditRefused(f"collision data unreadable: {exc}") from None
    base = struct.unpack_from("<I", decoded, 0x18)[0]
    edits: Dict[int, _GroupEdit] = {}
    report = {"ops": [], "groups": {}}
    removal_boxes: Dict[int, List[Box]] = {}
    for op_index, op in enumerate(ops):
        _GroupEdit.current_op = op_index
        g = op.get("group", 0)
        if not isinstance(g, int) or not 0 <= g < len(grids):
            raise CollisionEditRefused(f"group {g!r} does not exist ({len(grids)} groups)")
        if not grids[g].present:
            raise CollisionEditRefused(f"group {g} has no collision grid")
        ge = edits.get(g) or edits.setdefault(g, _GroupEdit(decoded, g, grids[g], cols[g]))
        what = op.get("op")
        if what == "remove_box":
            rep = _remove_box(ge, op)
            removal_boxes.setdefault(g, []).append((op_index, Box(op)))
        elif what == "clear":
            rep = {f"{k}s_removed": len(ge.recs[k]) for k in KINDS}
            for k in KINDS:
                if ge.recs[k]:
                    ge.changed.add(k)
                ge.recs[k] = []
            ge.new_points.clear()
        elif what == "add_polygons":
            rep = _add_polygons(ge, op)
        else:
            raise CollisionEditRefused(f"op must be remove_box, clear or add_polygons, got {what!r}")
        report["ops"].append({"op": what, "group": g, "label": op.get("label"), **rep})
    _GroupEdit.current_op = -1

    out = bytearray(decoded)
    allowed: Set[int] = set()

    def append(blob: bytes, align: int = 4) -> int:
        out.extend(bytes((-len(out)) % align))
        off = len(out)
        out.extend(blob)
        return off

    def allow(o, n):
        allowed.update(range(o, o + n))

    for g, ge in sorted(edits.items()):
        d = base + 0xCC * g
        # only added geometry can make the grid grow: pieces left by a cut lie where the original did
        new_pts = [p for kind in KINDS for r in ge.recs[kind] if r.source == "added" for p in _points_of(kind, r.geom)]
        need = None
        if new_pts:
            x0, x1, z0, z1 = extent_of(new_pts)
            need = (x0 - NEW_MARGIN, x1 + NEW_MARGIN, z0 - NEW_MARGIN, z1 + NEW_MARGIN)
        gp = _GridPlan(ge.grid, need)
        old_new = gp.old_cells_new()
        new_cells = set(range(gp.nx * gp.nz)) - old_new
        grep = {"tables": {}, "grid": {"before": [ge.grid.nx, ge.grid.nz], "after": [gp.nx, gp.nz],
                                       "grown": gp.grown}}
        # cells of every record in the new grid
        for kind in KINDS:
            for r in ge.recs[kind]:
                if r.orig is None:
                    r.cells = gp.footprint(extent_of(_points_of(kind, r.geom)), NEW_MARGIN)
                    if not r.cells:
                        raise CollisionEditRefused("internal: new collision outside the grid")
                else:
                    cells = {gp.new_index(c) for c in r.cells}
                    if new_cells and r.geom is not None:
                        cells |= gp.footprint(extent_of(_points_of(kind, r.geom)), NEW_MARGIN) & new_cells
                    r.cells = cells
        # tables
        for kind in KINDS:
            bit, coff, doff, size = TABLES[kind]
            if kind not in ge.changed:
                continue
            recs = ge.recs[kind]
            blob = b"".join(r.data for r in recs)
            off = append(blob, 4) if recs else struct.unpack_from("<I", decoded, d + doff)[0]
            struct.pack_into("<I", out, d + coff, len(recs))
            struct.pack_into("<I", out, d + doff, off)
            allow(d + coff, 4)
            allow(d + doff, 4)
            grep["tables"][kind] = {"records": len(recs), "kept": sum(1 for r in recs if r.orig is not None),
                                    "new": sum(1 for r in recs if r.orig is None), "offset": f"0x{off:X}"}
        # lists for bits 0-3, objects for new cells
        list_cache: Dict[Tuple[int, ...], int] = {}

        def list_offset(items):
            if not items:
                return 0
            key = tuple(items)
            if key not in list_cache:
                list_cache[key] = append(struct.pack(f"<{len(key) + 1}H", *key, 0xFFFF), 2)
            return list_cache[key]

        per_cell: Dict[int, Dict[int, List[int]]] = {}
        for kind in KINDS:
            bit = TABLES[kind][0]
            for li, r in enumerate(ge.recs[kind]):
                for c in r.cells:
                    per_cell.setdefault(c, {}).setdefault(bit, []).append(li)
        obj_lists: Dict[int, Dict[int, List[int]]] = {}
        if new_cells:
            for kind, (pool, poff) in OBJECT_KINDS.items():
                local = 0
                for rec in doc.pools[pool]["records"]:
                    if rec["group"] != g:
                        continue
                    o = int(rec["offset"], 16)
                    x, _y, z = struct.unpack_from("<3i", decoded, o + poff)
                    rx = rz = MARGIN
                    if kind == "goal":
                        rx, rz = goal_extent(list(struct.unpack_from("<9i", decoded, o)))
                    for c in gp.footprint_raw(x, z, rx + gp.sx, rz + gp.sz) & new_cells:
                        obj_lists.setdefault(c, {}).setdefault(KIND_BIT[kind], []).append(local)
                    local += 1
        rewritten = 0
        entries: List[Tuple[int, int]] = []
        for c in range(gp.nx * gp.nz):
            lists = per_cell.get(c, {})
            if c in old_new:
                c_old = None
                i, j = c % gp.nx - gp.di, c // gp.nx - gp.dj
                c_old = j * ge.grid.nx + i
                mask, arr, old_lists = ge.grid.cells[c_old]
                ptrs = {b2: old_lists[b2][0] for b2 in old_lists}
                same = True
                for bit in range(4):
                    want = sorted(lists.get(bit, []))
                    have = old_lists.get(bit, (0, None))[1] or []
                    if want != have:
                        same = False
                        ptrs[bit] = list_offset(want)
                if same:
                    entries.append((mask, arr))
                    continue
            else:
                mask = 0xF
                ptrs = {bit: list_offset(sorted(lists.get(bit, []))) for bit in range(4)}
                for b2, items in obj_lists.get(c, {}).items():
                    mask |= 1 << b2
                    ptrs[b2] = list_offset(items)
            offs = [ptrs.get(b2, 0) for b2 in range(32) if mask >> b2 & 1]
            arr_off = append(struct.pack(f"<{len(offs)}I", *offs), 4)
            entries.append((mask, arr_off))
            rewritten += 1
        if gp.grown:
            table = append(b"".join(struct.pack("<II", m, a) for m, a in entries), 4)
            struct.pack_into("<hhii", out, d + 0x40, gp.nx, gp.nz, gp.ox, gp.oz)
            struct.pack_into("<I", out, d + 0x54, table)
            allow(d + 0x40, 0x0C)
            allow(d + 0x54, 4)
            grep["grid"].update({"origin_after": [gp.ox / FX, gp.oz / FX], "table": f"0x{table:X}",
                                 "cells_added": len(new_cells)})
        else:
            for c, (m, a) in enumerate(entries):
                ent = ge.grid.table + 8 * c
                if (m, a) != ge.grid.cells[c][:2]:
                    struct.pack_into("<II", out, ent, m, a)
                    allow(ent, 8)
        grep["cells_rewritten"] = rewritten
        report["groups"][str(g)] = grep

    new = bytes(out)
    _verify(decoded, new, doc, edits, removal_boxes, allowed, name)
    report["decoded_size_before"] = len(decoded)
    report["decoded_size_after"] = len(new)
    report["decoded_sha256_before"] = hashlib.sha256(decoded).hexdigest()
    report["decoded_sha256_after"] = hashlib.sha256(new).hexdigest()
    return new, report


# ----------------------------------------------------------------------------- checks
def _listing(grid) -> Dict[int, Dict[int, Set[int]]]:
    """bit -> local index -> cells listing it."""
    listed: Dict[int, Dict[int, Set[int]]] = {bit: {} for bit in range(32)}
    for c, (_mask, _a, lists) in enumerate(grid.cells):
        for bit, (_lp, items) in lists.items():
            for i in (items or ()):
                listed[bit].setdefault(i, set()).add(c)
    return listed


def _verify(old: bytes, new: bytes, doc_old, edits: Dict[int, _GroupEdit], boxes, allowed, name):
    # 1. nothing inside the original length changed except the allowed fields
    stray = [i for i in range(len(old)) if old[i] != new[i] and i not in allowed]
    if stray:
        raise CollisionEditRefused(f"internal: bytes outside the allowed fields changed (first at 0x{stray[0]:X})")
    # 2. the stage parses cleanly with the same structure and objects
    doc = StageDocument(new, name)
    if doc.has_errors():
        raise CollisionEditRefused("edited stage has error diagnostics: " +
                                   "; ".join(d["code"] for d in doc.diagnostics if d["severity"] == "error"))
    if doc.layout_variant != doc_old.layout_variant or len(doc.groups) != len(doc_old.groups):
        raise CollisionEditRefused("edited stage changed its layout")
    stats, stats_old = survey_cells(new, doc), survey_cells(old, doc_old)
    for bad in ("cells without bits 0-3", "cells with bits above 6", "lists not sorted / with repeats",
                "list index out of range", "bananas in no cell", "bumpers in no cell", "goals in no cell"):
        if stats.get(bad, 0) > stats_old.get(bad, 0):
            raise CollisionEditRefused(f"collision lists: {bad}")
    grids = read_grids(new)
    old_grids = read_grids(old)
    cols = read_collision(new)
    for g, ge in edits.items():
        grid, ogrid = grids[g], old_grids[g]
        # every cell has its own array (the loader relocates each array once per cell)
        arrs = [a for _m, a, _l in grid.cells]
        if len(set(arrs)) != len(arrs) or 0 in arrs:
            raise CollisionEditRefused("internal: cell arrays shared or missing")
        listed = _listing(grid)
        old_listed = _listing(ogrid)
        if any(mask & 0xF != 0xF for mask, _a, _l in grid.cells):
            raise CollisionEditRefused("internal: a cell lost bits 0-3")
        gp_dx = (ogrid.ox - grid.ox) // grid.sx
        gp_dz = (ogrid.oz - grid.oz) // grid.sz
        if grid.sx != ogrid.sx or grid.sz != ogrid.sz or (ogrid.ox - grid.ox) % grid.sx or (ogrid.oz - grid.oz) % grid.sz:
            raise CollisionEditRefused("internal: the grid moved by a fraction of a cell")

        def map_old(c):
            i, j = c % ogrid.nx, c // ogrid.nx
            return (j + gp_dz) * grid.nx + (i + gp_dx)

        old_new = {map_old(c) for c in range(ogrid.nx * ogrid.nz)}
        for kind in KINDS:
            bit, _co, _do, size = TABLES[kind]
            got = cols[g][kind]
            want = ge.recs[kind]
            if len(got) != len(want):
                raise CollisionEditRefused(f"{kind} table has {len(got)} records, expected {len(want)}")
            for li, (rec, r) in enumerate(zip(got, want)):
                raw = new[rec["offset"]:rec["offset"] + size]
                if raw != r.data:
                    raise CollisionEditRefused(f"internal: {kind} {li} bytes differ from what was built")
                cells = listed[bit].get(li, set())
                if not cells and r.orig is None:
                    raise CollisionEditRefused(f"{kind} {li} of group {g} is in no cell")
                if r.orig is not None:
                    if {c for c in cells if c in old_new} != {map_old(c) for c in old_listed[bit].get(r.orig, ())}:
                        raise CollisionEditRefused(f"internal: kept {kind} {r.orig} changed cells")
                    continue
                geom = _decode(kind, raw)
                pts = _points_of(kind, geom) if geom.get("corners", True) is not None else None
                if pts is None:
                    raise CollisionEditRefused(f"new {kind} {li} decodes as degenerate")
                for p, q in zip(pts, r.geom["intended"]):
                    if _len(_sub(p, q)) > DECODE_TOLERANCE:
                        raise CollisionEditRefused(f"new {kind} {li} decodes {_len(_sub(p, q)):.4f} away from "
                                                   "where it was meant to be")
                ext = extent_of(pts)
                need = grid.footprint(*(_q((ext[0] + ext[1]) / 2), _q((ext[2] + ext[3]) / 2),
                                        _q((ext[1] - ext[0]) / 2 + GAME_MARGIN), _q((ext[3] - ext[2]) / 2 + GAME_MARGIN)))
                if not need <= cells:
                    raise CollisionEditRefused(f"new {kind} {li} is missing cells the game's rule needs")
                x0, x1, z0, z1 = ext
                if r.source == "added" and not (x0 - GAME_MARGIN >= grid.ox / FX and x1 + GAME_MARGIN < (grid.ox + grid.nx * grid.sx) / FX
                        and z0 - GAME_MARGIN >= grid.oz / FX and z1 + GAME_MARGIN < (grid.oz + grid.nz * grid.sz) / FX):
                    raise CollisionEditRefused(f"new {kind} {li} is not fully inside the grid")
                if kind in ("quad", "triangle"):
                    n = geom["normal"]
                    if abs(_len(n) - 1) > 0.01:
                        raise CollisionEditRefused(f"new {kind} {li} normal is not unit length")
        # object lists in old cells unchanged, objects listed as before
        for c in range(ogrid.nx * ogrid.nz):
            (m1, _a1, l1), (m2, _a2, l2) = ogrid.cells[c], grid.cells[map_old(c)]
            if m1 != m2:
                raise CollisionEditRefused("internal: a cell's type mask changed")
            for b2 in range(4, 7):
                if l1.get(b2, (0, None))[1] != l2.get(b2, (0, None))[1]:
                    raise CollisionEditRefused(f"internal: object list (bit {b2}) changed")
        # 3. nothing left inside a removal box
        for box_op, box in boxes.get(g, []):
            for kind in KINDS:
                for li, r in enumerate(ge.recs[kind]):
                    if r.geom is None or r.born > box_op:      # added after this removal: not affected by it
                        continue
                    pts = _points_of(kind, r.geom)
                    if not box.in_height(pts):
                        continue
                    if kind in ("quad", "triangle"):
                        ins = box.inside_part(pts)
                        if len(ins) >= 3 and polygon_area(ins) > 1e-4 and not box.on_boundary(pts):
                            raise CollisionEditRefused(f"{kind} {li} still reaches into a removal box")
                    elif kind == "edge":
                        iv = box.segment_interval(*pts)
                        if iv and (iv[1] - iv[0]) * _len(_sub(pts[1], pts[0])) > 1e-4 and \
                                box.strictly_inside(_add(pts[0], _mul(_sub(pts[1], pts[0]), (iv[0] + iv[1]) / 2))):
                            raise CollisionEditRefused(f"edge {li} still reaches into a removal box")
                    elif box.strictly_inside(pts[0]):
                        raise CollisionEditRefused(f"point {li} is still inside a removal box")
    # 4. every offset the game relocates stays inside the file
    n = len(new)
    base = struct.unpack_from("<I", new, 0x18)[0]
    vals = [base, struct.unpack_from("<I", new, 0x3C)[0]]
    for g in range(len(doc.groups)):
        d = base + 0xCC * g
        vals += [struct.unpack_from("<I", new, d + o)[0] for o in (0x04, 0x08, 0x54, 0x78, 0x80, 0x88, 0x90,
                                                                    0x98, 0xA0, 0xA8, 0xAC)]
        for kind in KINDS:
            _b, coff, doff, size = TABLES[kind]
            cnt, off = struct.unpack_from("<II", new, d + coff)
            if cnt and off + cnt * size > n:
                raise CollisionEditRefused(f"internal: {kind} table runs past the end of the file")
    for v in vals:
        if v and not v < n:
            raise CollisionEditRefused(f"internal: relocated offset 0x{v:X} outside the file")
    for grid in grids:
        for mask, arr, _lists in grid.cells:
            if arr and not arr + 4 * bin(mask).count("1") <= n:
                raise CollisionEditRefused("internal: cell array outside the file")


# ----------------------------------------------------------------------------- helpers for plans
def box_polygons(x: Sequence[float], y: Sequence[float], z: Sequence[float], bottom: bool = False) -> List[list]:
    """Outward-facing quads of an axis-aligned box: top, 4 sides (and the bottom if asked)."""
    (x0, x1), (y0, y1), (z0, z1) = x, y, z
    P = lambda a, b, c: [a, b, c]
    faces = [
        [P(x0, y1, z0), P(x0, y1, z1), P(x1, y1, z1), P(x1, y1, z0)],     # top (+y)
        [P(x1, y0, z0), P(x1, y1, z0), P(x1, y1, z1), P(x1, y0, z1)],     # +x
        [P(x0, y0, z1), P(x0, y1, z1), P(x0, y1, z0), P(x0, y0, z0)],     # -x
        [P(x1, y0, z1), P(x1, y1, z1), P(x0, y1, z1), P(x0, y0, z1)],     # +z
        [P(x0, y0, z0), P(x0, y1, z0), P(x1, y1, z0), P(x1, y0, z0)],     # -z
    ]
    if bottom:
        faces.append([P(x0, y0, z0), P(x1, y0, z0), P(x1, y0, z1), P(x0, y0, z1)])
    return faces


def floor_polygon(x: Sequence[float], z: Sequence[float], y: float, up: bool = True) -> list:
    (x0, x1), (z0, z1) = x, z
    poly = [[x0, y, z0], [x0, y, z1], [x1, y, z1], [x1, y, z0]]
    return poly if up else poly[::-1]
