"""Read Wavefront .obj files for Geometry > Import OBJ model.

What is read:
* vertices ("v x y z", with or without a w or a vertex colour after them);
* texture coordinates ("vt u v");
* faces ("f" with v, v/vt, v//vn or v/vt/vn corners, negative indices counted back from the end);
* objects and groups ("o" and "g"): each one becomes its own piece in the editor, which can be moved,
  turned, sized and textured separately. If a file has "o" lines, they split it (Blender, Wings3D);
  otherwise "g" lines do (programs that write groups only);
* materials ("usemtl", and the names and image files in the .mtl next to the file, shown when you pick the
  stage texture each material is drawn with);
* lines continued with a backslash.
Normals, smoothing groups, lines ("l"), points ("p"), curves and surfaces are skipped: the game lights
stage models itself, and lines and points have no area.

Faces with more than three corners are split into triangles (ear clipping, so concave faces work too).
Faces keep their corner order: seen from the side where the corners go counter-clockwise, a face is drawn
and solid. That is the usual convention of 3D programs (Blender, Wings3D, Maya, 3ds Max, SketchUp).

Every piece is centred on its own bottom middle ("origin" says where that point is, measured from the
bottom middle of the whole file), so placing the pieces at position + origin keeps the layout of the file.
"""
from __future__ import annotations

import math
import os
from typing import Dict, List, Optional, Sequence, Tuple

MAX_FILE_BYTES = 32 * 1024 * 1024
NO_MATERIAL = "(no material)"

Vec = Tuple[float, float, float]


class ObjError(ValueError):
    pass


def _newell(pts: Sequence[Vec]) -> Vec:
    nx = ny = nz = 0.0
    for i, a in enumerate(pts):
        b = pts[(i + 1) % len(pts)]
        nx += (a[1] - b[1]) * (a[2] + b[2])
        ny += (a[2] - b[2]) * (a[0] + b[0])
        nz += (a[0] - b[0]) * (a[1] + b[1])
    return nx, ny, nz


def _area2(a, b, c) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def triangulate(verts: Sequence[Vec], idx: Sequence[int]) -> List[Tuple[int, int, int]]:
    """Split one face (vertex indices in order) into triangles with the same orientation."""
    idx = list(idx)
    if len(idx) < 3:
        return []
    if len(idx) == 3:
        return [(idx[0], idx[1], idx[2])]
    pts = [verts[i] for i in idx]
    n = _newell(pts)
    ax = max(range(3), key=lambda k: abs(n[k]))
    keep = [k for k in range(3) if k != ax]
    flip = n[ax] < 0
    p2 = [(p[keep[0]], p[keep[1]]) for p in pts]
    if (ax == 1) != flip:                   # dropping y swaps the handedness of (x, z)
        p2 = [(x, -y) for x, y in p2]
    ring = list(range(len(idx)))
    out = []
    guard = 0
    while len(ring) > 3 and guard < 10000:
        guard += 1
        found = False
        for k in range(len(ring)):
            i0, i1, i2 = ring[k - 1], ring[k], ring[(k + 1) % len(ring)]
            a, b, c = p2[i0], p2[i1], p2[i2]
            if _area2(a, b, c) <= 1e-12:
                continue
            inside = False
            for j in ring:
                if j in (i0, i1, i2):
                    continue
                q = p2[j]
                if _area2(a, b, q) >= 0 and _area2(b, c, q) >= 0 and _area2(c, a, q) >= 0:
                    inside = True
                    break
            if inside:
                continue
            out.append((idx[i0], idx[i1], idx[i2]))
            ring.pop(k)
            found = True
            break
        if not found:                       # self-intersecting or degenerate: finish with a fan
            break
    for k in range(1, len(ring) - 1):
        out.append((idx[ring[0]], idx[ring[k]], idx[ring[k + 1]]))
    return out


def _tri_area(a: Vec, b: Vec, c: Vec) -> float:
    u = (b[0] - a[0], b[1] - a[1], b[2] - a[2])
    v = (c[0] - a[0], c[1] - a[1], c[2] - a[2])
    cr = (u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0])
    return 0.5 * math.sqrt(cr[0] ** 2 + cr[1] ** 2 + cr[2] ** 2)


def _logical_lines(text: str):
    """(line number, text) with comments removed and backslash continuations joined."""
    pending, start = "", 0
    for ln, line in enumerate(text.splitlines(), 1):
        line = line.split("#", 1)[0].rstrip()
        if not pending:
            start = ln
        if line.endswith("\\"):
            pending += line[:-1] + " "
            continue
        yield start, (pending + line).strip()
        pending = ""
    if pending.strip():
        yield start, pending.strip()


def _index(token: str, count: int, ln: int, what: str = "vertex") -> int:
    try:
        i = int(token)
    except ValueError:
        raise ObjError(f"line {ln}: bad face corner {token!r}") from None
    i = i - 1 if i > 0 else count + i
    if not 0 <= i < count:
        raise ObjError(f"line {ln}: face corner {token!r} refers to a {what} that does not exist")
    return i


def _vt_index(token: str, count: int) -> Optional[int]:
    """A texture coordinate index, or None when it is broken (the face is then drawn without the file's
    texture coordinates instead of refusing the whole file)."""
    try:
        i = int(token)
    except ValueError:
        return None
    i = i - 1 if i > 0 else count + i
    return i if 0 <= i < count else None


def _bottom_middle(points: Sequence[Vec]) -> Tuple[Vec, Vec]:
    lo = [min(v[k] for v in points) for k in range(3)]
    hi = [max(v[k] for v in points) for k in range(3)]
    return ((lo[0] + hi[0]) / 2, lo[1], (lo[2] + hi[2]) / 2), (hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2])


def parse_obj(text: str, up: str = "y", name: str = "model") -> dict:
    """Parse OBJ text. up: "y" (most programs) or "z" (some CAD and SketchUp exports).

    Returns {name, verts, faces, faces_in_file, dropped, size} for the whole file as one piece (verts
    centred on its bottom middle) and
      objects: [{name, verts, faces, uvs, fuv, materials, fmat, origin, size, dropped}], one per object or
               group with faces (uvs / fuv only when its faces have texture coordinates: fuv holds, per
               triangle, three indices into uvs, or None for a triangle without them);
      materials: every material name used, in order; mtllib: the .mtl files named; has_uv."""
    if up not in ("y", "z"):
        raise ObjError("the up axis must be y or z")
    raw: List[Vec] = []
    raw_vt: List[Tuple[float, float]] = []
    faces_in: List[Tuple[List[int], List[Optional[int]], str, int]] = []    # (v, vt, material, piece)
    o_names: List[str] = []
    g_names: List[str] = []
    o_of_face: List[int] = []
    g_of_face: List[int] = []
    cur_o = cur_g = -1
    material = ""
    mtllib: List[str] = []
    for ln, line in _logical_lines(text):
        if not line:
            continue
        parts = line.split()
        tag = parts[0]
        if tag == "v":
            try:
                x, y, z = (float(t) for t in parts[1:4])
            except ValueError:
                raise ObjError(f"line {ln}: a vertex needs three numbers") from None
            if not all(math.isfinite(c) for c in (x, y, z)):
                raise ObjError(f"line {ln}: a vertex is not a number")
            raw.append((x, z, -y) if up == "z" else (x, y, z))
        elif tag == "vt":
            try:
                u = float(parts[1])
                v = float(parts[2]) if len(parts) > 2 else 0.0
            except (ValueError, IndexError):
                raise ObjError(f"line {ln}: a texture coordinate needs a number") from None
            if not (math.isfinite(u) and math.isfinite(v)):
                raise ObjError(f"line {ln}: a texture coordinate is not a number")
            raw_vt.append((u, v))
        elif tag == "f":
            vi: List[int] = []
            ti: List[Optional[int]] = []
            for t in parts[1:]:
                bits = t.split("/")
                vi.append(_index(bits[0], len(raw), ln, "vertex"))
                ti.append(_vt_index(bits[1], len(raw_vt)) if len(bits) > 1 and bits[1] else None)
            if len(vi) >= 3:
                faces_in.append((vi, ti, material, len(faces_in)))
                o_of_face.append(cur_o)
                g_of_face.append(cur_g)
        elif tag == "o":
            o_names.append(" ".join(parts[1:]) or f"object {len(o_names) + 1}")
            cur_o = len(o_names) - 1
        elif tag == "g":
            g_names.append(" ".join(parts[1:]) or "default")
            cur_g = len(g_names) - 1
        elif tag == "usemtl":
            material = " ".join(parts[1:])
        elif tag == "mtllib":
            mtllib.extend(parts[1:])
    if not faces_in:
        raise ObjError("the file has no faces (only vertices, lines or points?)")

    # the whole file as one piece (as editor 0.8 read it)
    xs = [v[0] for v in raw]
    span = max(1e-9, max(xs) - min(xs), max(v[1] for v in raw) - min(v[1] for v in raw),
               max(v[2] for v in raw) - min(v[2] for v in raw))
    flat = 1e-10 * span * span
    tri_of_face: List[List[Tuple[Tuple[int, int, int], Tuple[Optional[int], ...]]]] = []
    drops: List[int] = []
    for vi, ti, _m, _k in faces_in:
        kept = []
        gone = 0
        for a, b, c in triangulate([raw[i] for i in vi], range(len(vi))):
            t = (vi[a], vi[b], vi[c])
            if _tri_area(raw[t[0]], raw[t[1]], raw[t[2]]) <= flat:
                gone += 1
                continue
            kept.append((t, (ti[a], ti[b], ti[c])))
        tri_of_face.append(kept)
        drops.append(gone)
    dropped = sum(drops)
    tris = [t for kept in tri_of_face for t, _uv in kept]
    if not tris:
        raise ObjError("every face of the model is flat (zero area)")
    used = sorted({i for t in tris for i in t})
    remap = {old: new for new, old in enumerate(used)}
    (cx, by, cz), size = _bottom_middle([raw[i] for i in used])
    verts = [(raw[i][0] - cx, raw[i][1] - by, raw[i][2] - cz) for i in used]
    faces = [(remap[a], remap[b], remap[c]) for a, b, c in tris]

    # pieces: objects, else groups, else the whole file
    if o_names:
        piece_of, piece_names = o_of_face, o_names
    elif g_names:
        piece_of, piece_names = g_of_face, g_names
    else:
        piece_of, piece_names = [0] * len(faces_in), [name]
    order: List[int] = []
    for k in piece_of:
        if k not in order:
            order.append(k)
    materials_all: List[str] = []
    objects = []
    for k in order:
        f_ids = [n for n in range(len(faces_in)) if piece_of[n] == k]
        ptris = [(t, uv, faces_in[n][2] or NO_MATERIAL) for n in f_ids for t, uv in tri_of_face[n]]
        dropped_here = sum(drops[n] for n in f_ids)
        if not ptris:
            continue
        pused = sorted({i for t, _uv, _m in ptris for i in t})
        premap = {old: new for new, old in enumerate(pused)}
        (px, py, pz), psize = _bottom_middle([raw[i] for i in pused])
        mats: List[str] = []
        for _t, _uv, m in ptris:
            if m not in mats:
                mats.append(m)
            if m not in materials_all:
                materials_all.append(m)
        piece = {"name": (piece_names[k] if k >= 0 else name)[:64],
                 "verts": [(raw[i][0] - px, raw[i][1] - py, raw[i][2] - pz) for i in pused],
                 "faces": [(premap[a], premap[b], premap[c]) for (a, b, c), _uv, _m in ptris],
                 "materials": mats, "fmat": [mats.index(m) for _t, _uv, m in ptris],
                 "origin": (px - cx, py - by, pz - cz), "size": psize, "dropped": dropped_here}
        if any(None not in uv for _t, uv, _m in ptris):
            vt_used = sorted({j for _t, uv, _m in ptris if None not in uv for j in uv})
            vt_map = {old: new for new, old in enumerate(vt_used)}
            piece["uvs"] = [raw_vt[j] for j in vt_used]
            piece["fuv"] = [None if None in uv else tuple(vt_map[j] for j in uv) for _t, uv, _m in ptris]
        objects.append(piece)
    return {"name": name, "verts": verts, "faces": faces, "faces_in_file": len(faces_in), "dropped": dropped,
            "size": size, "objects": objects, "materials": materials_all, "mtllib": mtllib,
            "has_uv": any("uvs" in o for o in objects)}


def parse_mtl(text: str) -> Dict[str, dict]:
    """Material names of an .mtl file with their colour (Kd) and image (map_Kd, file name only)."""
    out: Dict[str, dict] = {}
    cur = None
    for _ln, line in _logical_lines(text):
        parts = line.split()
        if not parts:
            continue
        if parts[0] == "newmtl":
            cur = " ".join(parts[1:])
            out[cur] = {}
        elif cur is not None and parts[0] == "Kd" and len(parts) >= 4:
            try:
                out[cur]["Kd"] = tuple(max(0.0, min(1.0, float(v))) for v in parts[1:4])
            except ValueError:
                pass
        elif cur is not None and parts[0] == "map_Kd" and len(parts) > 1:
            out[cur]["map_Kd"] = os.path.basename(parts[-1].replace("\\", "/"))
    return out


def read_obj(path: str, up: str = "y") -> dict:
    """parse_obj on a file, plus "material_info" from the .mtl files it names that sit next to it."""
    try:
        if os.path.getsize(path) > MAX_FILE_BYTES:
            raise ObjError(f"the file is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB")
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError as exc:
        raise ObjError(f"cannot read the file: {exc}") from None
    out = parse_obj(text, up, os.path.splitext(os.path.basename(path))[0])
    info: Dict[str, dict] = {}
    folder = os.path.dirname(os.path.abspath(path))
    for lib in out["mtllib"]:
        p = os.path.join(folder, os.path.basename(lib.replace("\\", "/")))
        try:
            if os.path.isfile(p) and os.path.getsize(p) <= MAX_FILE_BYTES:
                with open(p, "r", encoding="utf-8", errors="replace") as fh:
                    info.update(parse_mtl(fh.read()))
        except OSError:
            continue
    out["material_info"] = info
    return out
