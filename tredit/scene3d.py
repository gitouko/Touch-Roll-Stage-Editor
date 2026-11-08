"""What the 3D view draws, built without OpenGL (so it can be tested anywhere).

A StageScene holds, for one stage as it currently stands in the editor:
* the model's polygons per shape (local positions, texture coordinates, a light shade per corner), the
  shape's rest matrix and the node that moves it;
* every material's texture as RGBA pixels plus wrap / cull / alpha settings;
* the stage's joint animation (if it has one) and, for any frame, the pose of each animated node;
* collision faces per group, and which model node moves each group (by name, spec §15);
* the editor's objects and geometry items.

Matrices are trcore's row-vector affine pairs ((r0, r1, r2), t); `gl_matrix` turns one into the 16
floats OpenGL expects (column-major, column vectors).
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Tuple

from trcore import lz10
from trcore.nitro.nsbca import Nsbca, node_local_matrix
from trcore.nitro.nsbmd import NitroError, Nsbmd, mat_apply, mat_inverse, mat_then, model_geometry
from trcore.nitro.nsbmd_write import shape_polygons_with_texcoords
from trcore.nitro.nsbtx import material_bindings, material_texture_params, read_textures

IDENTITY = (((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)), (0.0, 0.0, 0.0))
LIGHT = (0.35, 0.85, 0.40)                  # direction towards the light (normalised below)
_L = math.sqrt(sum(c * c for c in LIGHT))
LIGHT = tuple(c / _L for c in LIGHT)


def gl_matrix(m) -> List[float]:
    (r0, r1, r2), t = m
    return [r0[0], r0[1], r0[2], 0.0, r1[0], r1[1], r1[2], 0.0, r2[0], r2[1], r2[2], 0.0, t[0], t[1], t[2], 1.0]


def _normal(poly) -> Tuple[float, float, float]:
    nx = ny = nz = 0.0
    for i in range(len(poly)):
        a, b = poly[i], poly[(i + 1) % len(poly)]
        nx += (a[1] - b[1]) * (a[2] + b[2])
        ny += (a[2] - b[2]) * (a[0] + b[0])
        nz += (a[0] - b[0]) * (a[1] + b[1])
    ln = math.sqrt(nx * nx + ny * ny + nz * nz)
    return (0.0, 1.0, 0.0) if ln < 1e-12 else (nx / ln, ny / ln, nz / ln)


def shade_for(normal) -> float:
    d = sum(a * b for a, b in zip(normal, LIGHT))
    return 0.55 + 0.45 * max(0.0, d)


def _rot_only(m):
    return (m[0], (0.0, 0.0, 0.0))


class Batch:
    """One shape's triangles: flat lists of floats ready for vertex arrays."""
    __slots__ = ("shape", "name", "node", "material", "rest", "positions", "texcoords", "shades", "count")

    def __init__(self, shape, name, node, material, rest):
        self.shape, self.name, self.node, self.material, self.rest = shape, name, node, material, rest
        self.positions: List[float] = []
        self.texcoords: List[float] = []
        self.shades: List[float] = []
        self.count = 0


class Material:
    __slots__ = ("index", "texture", "palette", "image", "repeat_s", "repeat_t", "flip_s", "flip_t",
                 "alpha", "cull", "colour", "has_alpha")

    def __init__(self, index):
        self.index = index
        self.texture = self.palette = None
        self.image = None                    # (width, height, RGBA bytes)
        self.repeat_s = self.repeat_t = True
        self.flip_s = self.flip_t = False
        self.alpha = 1.0
        self.cull = "back"                   # "back", "front", "none"
        self.colour = (0.8, 0.8, 0.8)
        self.has_alpha = False


class StageScene:
    def __init__(self, rom, key: str, model_bytes: Optional[bytes], groups=(), faces=(), objects=(),
                 geometry=(), anim_bytes: Optional[bytes] = None):
        self.key = key
        self.batches: List[Batch] = []
        self.materials: Dict[int, Material] = {}
        self.model = None
        self.anim = None
        self.frames = 1
        self.errors: List[str] = []
        self.node_rest_world: Dict[int, tuple] = {}
        self.node_names: List[str] = []
        if model_bytes:
            try:
                self._load_model(rom, key, model_bytes)
            except NitroError as exc:
                self.errors.append(f"model: {exc}")
        if anim_bytes is not None:
            self.set_animation(anim_bytes)
        else:
            self._load_animation(rom, key)
        self.group_node: Dict[int, int] = {}
        for g in groups:
            name = g.get("name_text_04")
            if g.get("index", 0) and name in self.node_names:
                self.group_node[g["index"]] = self.node_names.index(name)
        self.faces = list(faces)
        self.objects = list(objects)
        self.geometry = list(geometry)
        self.bounds = self._bounds()

    # ---------------------------------------------------------------- loading
    def _load_model(self, rom, key, data):
        nsb = Nsbmd(data, key)
        m = nsb.models[0]
        self.model = m
        self.node_names = [n["name"] for n in m.nodes]
        geo = model_geometry(nsb)
        for si, (mtx, node) in sorted(geo["shape_matrix"].items()):
            if node is not None and node not in self.node_rest_world:
                self.node_rest_world[node] = mtx
        tex = None
        try:
            tex = read_textures(data, key) if nsb.has_textures else None
            if tex is None and rom is not None:
                path = f"model/stage/{key}_nsbtx.lz"
                if path in rom.id_by_path:
                    raw = rom.file_bytes(path)
                    tex = read_textures(lz10.decode(raw)[0] if raw[:1] == b"\x10" else raw, path)
        except (NitroError, ValueError, KeyError) as exc:
            self.errors.append(f"textures: {exc}")
        binds = material_bindings(nsb)
        params = material_texture_params(nsb)
        for mi in range(len(m.materials)):
            mat = Material(mi)
            p = params.get(mi, {})
            mat.repeat_s, mat.repeat_t = p.get("repeat_s", True), p.get("repeat_t", True)
            mat.flip_s, mat.flip_t = p.get("flip_s", False), p.get("flip_t", False)
            mat.alpha = p.get("alpha", 31) / 31.0 if p.get("alpha", 31) else 1.0
            front, back = p.get("front", True), p.get("back", False)
            mat.cull = "none" if (front and back) else ("back" if front else "front")
            d = p.get("diffuse", (204, 204, 204))
            mat.colour = tuple(c / 255.0 for c in d)
            tn, pn = binds.get(mi, (None, None))
            mat.texture, mat.palette = tn, pn
            if tex is not None and tn and tn in tex.textures:
                try:
                    mat.image = tex.rgba(tn, pn)
                    t = tex.textures[tn]
                    mat.has_alpha = t.color0_transparent or t.format in (1, 6, 7, 5)
                except (NitroError, KeyError, IndexError, ValueError) as exc:
                    self.errors.append(f"texture {tn}: {exc}")
            self.materials[mi] = mat
        for si, sh in enumerate(m.shapes):
            if si not in geo["shape_matrix"]:
                continue                         # never drawn by the render commands
            mtx, node = geo["shape_matrix"][si]
            mi = geo["shape_material"].get(si)
            b = Batch(si, sh["name"], node, mi, mtx)
            img = self.materials[mi].image if mi in self.materials else None
            w, h = (img[0], img[1]) if img else (1, 1)
            rot = _rot_only(mtx)
            for poly in shape_polygons_with_texcoords(data, m, si):
                pts = [p for p, _t in poly]
                if len(pts) < 3:
                    continue
                n = _normal([mat_apply(rot, p) for p in pts])
                s = shade_for(n)
                for k in range(1, len(poly) - 1):
                    for (p, t) in (poly[0], poly[k], poly[k + 1]):
                        b.positions.extend(p)
                        b.texcoords.extend((t[0] / w, t[1] / h))
                        b.shades.append(s)
                        b.count += 1
            if b.count:
                self.batches.append(b)

    def _load_animation(self, rom, key):
        if rom is None or self.model is None:
            return
        path = f"model/stage/{key}_nsbca.lz"
        try:
            if path not in rom.id_by_path:
                return
            raw = rom.file_bytes(path)
            nsb = Nsbca(lz10.decode(raw)[0] if raw[:1] == b"\x10" else raw, path)
            if nsb.anims:
                self.anim = nsb.anims[0]
                self.frames = max(1, self.anim.frames)
        except (NitroError, ValueError, KeyError) as exc:
            self.errors.append(f"animation: {exc}")
            self.anim = None

    def set_animation(self, data: Optional[bytes]):
        """Use these (decoded .nsbca) bytes as the stage's animation, e.g. after a keyframe edit."""
        self.anim, self.frames = None, 1
        if not data or self.model is None:
            return
        try:
            nsb = Nsbca(data, f"{self.key} animation")
            if nsb.anims:
                self.anim = nsb.anims[0]
                self.frames = max(1, self.anim.frames)
        except (NitroError, ValueError) as exc:
            self.errors.append(f"animation: {exc}")

    # ---------------------------------------------------------------- poses
    @property
    def animated_nodes(self) -> List[int]:
        if not self.anim or self.model is None:
            return []
        return [n for n in self.anim.animated_nodes() if n < len(self.model.nodes)]

    def node_world(self, node: int, frame: float):
        """World matrix of a node at a frame (parents taken as at rest)."""
        rest_world = self.node_rest_world.get(node)
        local_rest = self.model.node_matrix(node)
        if rest_world is None:
            rest_world = local_rest
        local_now = node_local_matrix(self.model, node, self.anim, frame)
        inv = mat_inverse(local_rest)
        parent = mat_then(inv, rest_world) if inv else IDENTITY
        return mat_then(local_now, parent)

    def pose(self, frame: float) -> Dict[int, tuple]:
        """{node: (world matrix now, change from rest)} for every animated node."""
        out = {}
        if self.anim is None:
            return out
        f = max(0.0, min(float(frame), self.frames - 1))
        for n in self.animated_nodes:
            now = self.node_world(n, f)
            rest = self.node_rest_world.get(n) or self.model.node_matrix(n)
            inv = mat_inverse(rest)
            out[n] = (now, mat_then(inv, now) if inv else IDENTITY)
        return out

    def batch_matrix(self, b: Batch, pose) -> tuple:
        if b.node in pose:
            return pose[b.node][0]
        return b.rest

    def group_delta(self, group: int, pose):
        n = self.group_node.get(group)
        return pose[n][1] if n in pose else None

    def object_position(self, o: dict, pose) -> Tuple[float, float, float]:
        p = tuple(o["position"])
        d = self.group_delta(o.get("group", 0), pose) if o.get("group") else None
        return mat_apply(d, p) if d else p

    # ---------------------------------------------------------------- extents
    def _bounds(self):
        lo, hi = [math.inf] * 3, [-math.inf] * 3

        def add(p):
            for i in range(3):
                lo[i], hi[i] = min(lo[i], p[i]), max(hi[i], p[i])
        for b in self.batches:
            ps = b.positions
            for k in range(0, len(ps), 9):          # one corner per triangle is enough for extents
                add(mat_apply(b.rest, ps[k:k + 3]))
        for corners, _n, _g in self.faces[:20000]:
            add(corners[0])
        for o in self.objects:
            add(o["position"])
        if lo[0] == math.inf:
            return (-10.0, -2.0, -10.0), (10.0, 2.0, 10.0)
        return tuple(lo), tuple(hi)


def scene_from_state(st) -> StageScene:
    """StageScene for an editor StageState (tredit.project) as it currently stands."""
    rom = st.project.rom
    model = st.current_model if st.current_model is not None else rom.stage_model_decoded(st.key)
    anim = getattr(st, "anim_bytes", None)
    return StageScene(rom, st.key, model, groups=st.doc.groups, faces=st.collision_faces(),
                      objects=st.objects(), geometry=st.geometry(), anim_bytes=anim)


# ---------------------------------------------------------------- simple object meshes (triangles, local)
def _box(x0, x1, y0, y1, z0, z1):
    P = [(x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0), (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)]
    quads = [(4, 5, 6, 7), (1, 0, 3, 2), (0, 4, 7, 3), (5, 1, 2, 6), (3, 7, 6, 2), (0, 1, 5, 4)]
    tris = []
    for a, b, c, d in quads:
        tris += [P[a], P[b], P[c], P[a], P[c], P[d]]
    return tris


def _cylinder(r, y0, y1, n=12):
    tris = []
    for i in range(n):
        a0, a1 = 2 * math.pi * i / n, 2 * math.pi * (i + 1) / n
        p0, p1 = (r * math.cos(a0), r * math.sin(a0)), (r * math.cos(a1), r * math.sin(a1))
        tris += [(p0[0], y0, p0[1]), (p1[0], y1, p1[1]), (p1[0], y0, p1[1]),
                 (p0[0], y0, p0[1]), (p0[0], y1, p0[1]), (p1[0], y1, p1[1]),
                 (0, y1, 0), (p1[0], y1, p1[1]), (p0[0], y1, p0[1])]
    return tris


def _octahedron(r):
    P = [(r, 0, 0), (-r, 0, 0), (0, r, 0), (0, -r, 0), (0, 0, r), (0, 0, -r)]
    faces = [(0, 2, 4), (4, 2, 1), (1, 2, 5), (5, 2, 0), (4, 3, 0), (1, 3, 4), (5, 3, 1), (0, 3, 5)]
    return [P[i] for f in faces for i in f]


def object_mesh(o: dict) -> List[Tuple[float, float, float]]:
    """Triangles (object-local, y up, facing -z at heading 0) for an editor object."""
    k = o["kind"]
    if k == "start":
        return _cylinder(0.12, 0.0, 0.6, 8) + [(-0.35, 0.05, 0.25), (0.35, 0.05, 0.25), (0.0, 0.05, -0.6)]
    if k == "goal":
        return _box(-1.3, -1.1, 0.0, 1.6, -0.1, 0.1) + _box(1.1, 1.3, 0.0, 1.6, -0.1, 0.1) + \
            _box(-1.3, 1.3, 1.6, 1.8, -0.1, 0.1)
    if k == "bumper":
        return _cylinder(0.5, 0.0, 0.5, 14)
    return _octahedron(0.35 if o.get("bunch") else 0.22)


OBJECT_COLOURS = {"start": (0.24, 0.86, 0.35), "goal": (0.27, 0.63, 1.0), "goal_red": (0.94, 0.24, 0.24),
                  "bumper": (0.9, 0.27, 0.9), "banana": (1.0, 0.86, 0.16)}


def object_matrix(o: dict, pos) -> tuple:
    """Placement of an object mesh: a goal's or bumper's own matrix; otherwise heading turns about y (game
    angle: 0 faces -z)."""
    h = o.get("heading")
    if o["kind"] in ("goal", "bumper") and o.get("matrix"):
        r = tuple(tuple(row) for row in o["matrix"])
        return (r, tuple(pos))
    if h is None:
        return (IDENTITY[0], tuple(pos))
    a = math.radians(h)
    c, s = math.cos(a), math.sin(a)
    return (((c, 0.0, -s), (0.0, 1.0, 0.0), (s, 0.0, c)), tuple(pos))
