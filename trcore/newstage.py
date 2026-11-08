"""Replace a stage with a new one built from scratch: one platform and a start (owner's "Create New
Stage"; edit test 5 checks it in-game).

The new stage keeps the replaced stage's file STRUCTURE and every unknown byte: the same header values
(apart from the intro-camera bounds), the same groups (moving parts stay, emptied), the same model file
with its nodes, materials and render commands (every shape cleared so it draws nothing), and the same
textures and animations. On top of that it puts:
  * a platform: a box (top + four sides) as model polygons in one static shape (the stage's own texture
    is the placeholder) and as collision in group 0;
  * the start, moved and turned;
  * optionally a goal and bananas.
All of it goes through the verified plan operations (field edits, objects, collision, model, header),
so the result is checked like any other edit.
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence, Tuple

from .nitro.nsbmd import Nsbmd, model_geometry
from .stc.document import StageDocument
from .stc.geometry_edit import box_polygons

FX = 4096
ANGLE_FULL = 0x10000
DEFAULT_PLATFORM = {"x": [-5.0, 5.0], "z": [-3.0, 3.0], "top": 0.0, "thickness": 1.0}


class NewStageError(Exception):
    pass


def _newell_up_area(verts) -> float:
    n = 0.0
    for i in range(len(verts)):
        a, b = verts[i], verts[(i + 1) % len(verts)]
        n += a[2] * b[0] - a[0] * b[2]
    return n / 2


def moving_node_names(doc: StageDocument, stage_bytes: bytes) -> set:
    """Names of the model nodes that carry moving parts: the names of groups 1+ (spec §15)."""
    import struct
    names = set()
    n, base = struct.unpack_from("<II", stage_bytes, 0x14)
    for g in range(1, n):
        o = struct.unpack_from("<I", stage_bytes, base + 0xCC * g + 8)[0]
        if o:
            end = stage_bytes.index(b"\0", o)
            names.add(stage_bytes[o:end].decode("ascii", "replace"))
    return names


def pick_platform_shape(model_bytes: bytes, stage_bytes: bytes, name: str = "") -> str:
    """The shape to draw new platforms with: the one with the most upward-facing area among shapes drawn
    under a node that is not a moving part (not a group 1+ name, not 'MOT_...')."""
    nsb = Nsbmd(model_bytes, name)
    m = nsb.models[0]
    geo = model_geometry(nsb)
    moving = moving_node_names(None, stage_bytes)
    area: Dict[int, float] = {}
    for verts, si, node in geo["polygons"]:
        nd = m.nodes[node]["name"] if node is not None and isinstance(m.nodes[node], dict) else ""
        if nd in moving or nd.startswith("MOT_"):
            continue
        a = _newell_up_area(verts)
        if a > 0:
            area[si] = area.get(si, 0.0) + a
    for si, (_mtx, node) in geo["shape_matrix"].items():
        nd = m.nodes[node]["name"] if node is not None and isinstance(m.nodes[node], dict) else ""
        if nd in moving or nd.startswith("MOT_"):
            area.pop(si, None)
    if not area:
        raise NewStageError("no static shape with an upward-facing polygon to draw a platform with")
    return m.shapes[max(area, key=area.get)]["name"]


def texture_choices(model_bytes: bytes, stage_bytes: bytes, name: str = "") -> List[dict]:
    """The stage textures new platforms and imported models can be drawn with (placeholders until custom
    textures come): one static shape per texture + palette pair of the model's materials.

    A shape qualifies when the render commands draw it, its node is not a moving part (group 1+ name or
    'MOT_...') and its transform is not mirrored or flat; its material must name a texture. When several
    shapes share a texture + palette, the one with the most upward-facing area is kept.
    Returns [{shape, material, texture, palette, orig_w, orig_h, up_area}], largest upward area first."""
    from .nitro.nsbtx import material_bindings, material_texture_params
    nsb = Nsbmd(model_bytes, name)
    m = nsb.models[0]
    geo = model_geometry(nsb)
    moving = moving_node_names(None, stage_bytes)
    binds = material_bindings(nsb)
    params = material_texture_params(nsb)
    area: Dict[int, float] = {}
    for verts, si, _node in geo["polygons"]:
        a = _newell_up_area(verts)
        if a > 0:
            area[si] = area.get(si, 0.0) + a
    best: Dict[Tuple[str, Optional[str]], dict] = {}
    for si, (mtx, node) in sorted(geo["shape_matrix"].items()):
        nd = m.nodes[node]["name"] if node is not None and isinstance(m.nodes[node], dict) else ""
        if nd in moving or nd.startswith("MOT_"):
            continue
        r = mtx[0]
        det = (r[0][0] * (r[1][1] * r[2][2] - r[1][2] * r[2][1]) - r[0][1] * (r[1][0] * r[2][2] - r[1][2] * r[2][0])
               + r[0][2] * (r[1][0] * r[2][1] - r[1][1] * r[2][0]))
        if det <= 1e-9:
            continue
        mi = geo["shape_material"].get(si)
        tex, pal = binds.get(mi, (None, None)) if mi is not None else (None, None)
        if not tex:
            continue
        p = params.get(mi, {})
        c = {"shape": m.shapes[si]["name"], "material": mi, "texture": tex, "palette": pal,
             "orig_w": p.get("orig_w") or 0, "orig_h": p.get("orig_h") or 0, "up_area": round(area.get(si, 0.0), 4)}
        k = (tex, pal)
        if k not in best or c["up_area"] > best[k]["up_area"]:
            best[k] = c
    return sorted(best.values(), key=lambda c: (-c["up_area"], c["texture"], c["palette"] or ""))


def goal_matrix(heading: float) -> List[float]:
    """Stored goal matrix (9 values, rows) whose front faces `heading` (0 = towards -z, + = turn left)."""
    d = math.radians((float(heading) - 180.0) % 360.0)
    c, s = math.cos(d), math.sin(d)
    rows = ((c, 0.0, -s), (0.0, 1.0, 0.0), (s, 0.0, c))
    return [round(v, 6) + 0.0 for row in rows for v in row]


def heading_towards(frm: Sequence[float], to: Sequence[float]) -> float:
    dx, dz = to[0] - frm[0], to[2] - frm[2]
    return math.degrees(math.atan2(-dx, -dz)) % 360.0


def platform_polygons(platform: Dict) -> List[list]:
    x, z = platform["x"], platform["z"]
    top = float(platform.get("top", 0.0))
    th = float(platform.get("thickness", 1.0))
    if not (x[0] < x[1] and z[0] < z[1] and th > 0):
        raise NewStageError("platform needs x/z ranges (min < max) and a positive thickness")
    return box_polygons(x, [top - th, top], z)


def new_stage_entry(stage_bytes: bytes, model_bytes: bytes, stage_key: str, *,
                    platform: Optional[Dict] = None, start: Optional[Dict] = None,
                    goal: Optional[Dict] = None, bananas: Sequence[Sequence[float]] = (),
                    shape: Optional[str] = None, set_bounds: bool = True, where: Optional[str] = None,
                    label: str = "new stage") -> dict:
    """Plan-schema-3 stage entry that turns `stage_key` into a new stage (see the module docstring).
    start = {"position": [x, y, z], "heading": degrees}; goal = {"position": ..., "heading": ..., "red": bool}."""
    doc = StageDocument(stage_bytes, stage_key)
    if doc.has_errors():
        raise NewStageError("the stage to replace has structural errors")
    platform = dict(DEFAULT_PLATFORM, **(platform or {}))
    polys = platform_polygons(platform)
    top = float(platform["top"])
    x0, x1 = platform["x"]
    z0, z1 = platform["z"]
    if start is None:
        start = {"position": [x0 + 1.5, top, (z0 + z1) / 2], "heading": 270.0}  # facing +x along the platform
    starts = (doc.start or {}).get("records", [])
    if not starts:
        raise NewStageError("the stage has no start record to move")
    shape = shape or pick_platform_shape(model_bytes, stage_bytes, stage_key)
    edits = []
    s0 = starts[0]
    pos = [float(v) for v in start["position"]]
    if [round(v * FX) for v in s0["position"]] != [round(v * FX) for v in pos]:
        edits.append({"target": "start[0].position", "from": list(s0["position"]), "to": pos})
    word = (s0["u32_0C"] & ~0xFFFF) | (int(round(float(start.get("heading", 0.0)) % 360.0 * ANGLE_FULL / 360.0))
                                      % ANGLE_FULL)
    if word != s0["u32_0C"]:
        edits.append({"target": "start[0].u32_0C", "from": s0["u32_0C"], "to": word})
    objects = []
    for pool, kind, key in (("A", "goal", "translation"), ("B", "bumper", "translation"), ("C", "banana", "position")):
        for r in doc.pools[pool]["records"]:
            objects.append({"op": "remove", "kind": kind, "index": r["index"], "expect_position": list(r[key])})
    if goal:
        gpos = [float(v) for v in goal["position"]]
        heading = goal.get("heading")
        if heading is None:
            heading = heading_towards(gpos, pos)
        objects.append({"op": "add", "kind": "goal", "group": 0, "position": gpos, "red": bool(goal.get("red")),
                        "matrix": goal_matrix(heading)})
    for b in bananas:
        objects.append({"op": "add", "kind": "banana", "group": 0, "position": [float(v) for v in b[:3]],
                        "bunch": bool(b[3]) if len(b) > 3 else False})
    collision = [{"op": "clear", "group": g} for g in range(len(doc.groups))]
    collision.append({"op": "add_polygons", "group": 0, "polygons": polys, "label": f"{label}: platform"})
    model = [{"op": "clear_shapes", "shapes": "all", "label": f"{label}: the old stage's drawing"},
             {"op": "add_polygons", "shape": shape, "polygons": polys, "label": f"{label}: platform"}]
    entry = {"stage_key": stage_key, "where": where, "new_stage": {
        "platform": platform, "shape": shape, "start": {"position": pos, "heading": start.get("heading", 0.0)},
        "goal": goal, "bananas": [list(b) for b in bananas]},
        "edits": edits, "objects": objects, "collision": collision, "model": model}
    if set_bounds:
        entry["header"] = {"bounds": {"min": [x0, top - float(platform["thickness"]), z0], "max": [x1, top + 1.0, z1]}}
    return entry


def build_new_stage(stage_bytes: bytes, model_bytes: bytes, stage_key: str, *, platform: Optional[Dict] = None,
                    start: Optional[Dict] = None, shape: Optional[str] = None) -> Tuple[bytes, bytes, dict]:
    """The new stage itself: (stage bytes, model bytes, entry) for a bare new stage (one platform and the start),
    built with exactly the operations of new_stage_entry in the plan's order (field edits, header, collision,
    objects, model), as edit test 5 did. The editor uses it as the base the owner's further edits apply to."""
    from .nitro.nsbmd_write import ModelEditRefused, edit_model
    from .stc.edit import EditRefused, apply_edits
    from .stc.geometry_edit import CollisionEditRefused, apply_collision_ops
    from .stc.objects import ObjectEditRefused, apply_object_ops
    from .stc.stagehead import HeaderEditRefused, apply_header
    entry = new_stage_entry(stage_bytes, model_bytes, stage_key, platform=platform, start=start, shape=shape)
    try:
        cur = stage_bytes
        if entry["edits"]:
            cur, _ = apply_edits(cur, entry["edits"], stage_key)
        cur, _ = apply_header(cur, entry["header"], stage_key)
        cur, _ = apply_collision_ops(cur, entry["collision"], stage_key)
        cur, _ = apply_object_ops(cur, entry["objects"], stage_key)
        names = [s["name"] for s in Nsbmd(model_bytes, stage_key).models[0].shapes]
        adds = {}
        for op in entry["model"]:
            if op["op"] == "add_polygons":
                adds.setdefault(op["shape"], []).extend(op["polygons"])
        model, _ = edit_model(model_bytes, adds, names, stage_key)
    except (EditRefused, HeaderEditRefused, CollisionEditRefused, ObjectEditRefused, ModelEditRefused) as exc:
        raise NewStageError(str(exc)) from None
    return cur, model, entry
