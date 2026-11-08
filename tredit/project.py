"""Editor model: everything the editor does, without any GUI code.

The GUI only calls this module. Every change becomes an edit of a known field
(trcore.stc.edit), is applied to the stage's original bytes and parsed back;
what the editor shows is read from those re-parsed bytes, so the screen always
shows exactly what a save would write. Saving goes through
trcore.editplan.build_edit_rom (verified ROM copy + patch record).

Only fields whose meaning was confirmed in-game (evidence Level 4) can be
changed: start position and facing, goal position / facing / red flag, bumper
position, banana position / bunch flag. Goals, bumpers and bananas can be added
(to the stage's fixed part), deleted and moved anywhere inside the collision grid;
those changes go through trcore.stc.objects (edit test 3).

Geometry (0.3, edit test 4): platforms (a box or a flat floor; drawn with the stage's own texture as the
placeholder and/or solid), invisible floors and blocks, decorations without collision, and areas whose
collision is removed. They go through trcore.stc.geometry_edit (collision) and trcore.nitro.nsbmd_write
(model), always in the stage's fixed part (group 0).

Keyframes (0.7): the moving parts' animation as keys per channel (trcore.keyframes), baked by
trcore.nitro.nsbca_write (set_length / set_keys); confirmed in-game by edit tests 6 and 8.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from typing import Dict, List, Optional, Sequence, Tuple

from trcore import lz10
from trcore.editplan import (PLAN_SCHEMA, PLAN_SCHEMA_V2, PLAN_SCHEMA_V3, PLAN_SCHEMA_V5, PLAN_SCHEMA_V6,
                             PLAN_SCHEMA_V7, STAGE_ANIM_FORMAT, build_edit_rom)
from trcore.keyframes import CHANNELS, MAX_FRAMES, KeyframeError, channel_kind, check_keys, node_channels
from trcore.names import NamesError, apply_names, read_names
from trcore.newstage import DEFAULT_PLATFORM, NewStageError, build_new_stage, pick_platform_shape, texture_choices
from trcore.nitro.nsbca import Nsbca
from trcore.nitro.nsbca_write import AnimEditRefused, edit_animation
from trcore.nitro.nsbmd import NitroError
from trcore.nitro.nsbmd import Nsbmd, bounds as model_bounds, model_geometry
from trcore.nitro.nsbmd_write import DEFAULT_TEXELS_PER_UNIT, ModelEditRefused, edit_model, rescale_positions
from trcore.stc.collision import CollisionError, read_collision
from trcore.stc.geometry_edit import CollisionEditRefused, apply_collision_ops, box_polygons, floor_polygon
from trcore.rom import NdsRom, RomError
from trcore.romwrite import RomWriteRefused, patch_record_path
from trcore.stagemodel import group_kinds, load_stage_model
from trcore.stc.cells import KIND_POOL, CellError, coverage_problem, read_grids
from trcore.stc.document import StageDocument
from trcore.stc.edit import EditRefused, apply_edits
from trcore.stc.objects import MAX_BANANA_VALUE, ObjectEditRefused, apply_object_ops

FX = 4096
ANGLE_FULL = 0x10000

# Names (edit tests 7 and 7b). Stage names are also printed when a stage starts, with a capitals-only pixel font
# that crashes on lower case; only characters seen working there are offered.
# World names are drawn with the game's normal text font; the characters below all appear in the game's
# own messages. "%" is never allowed (stage names are used as a format string).
STAGE_NAME_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 -?!':(#).+=&/,*;@<>")
WORLD_NAME_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789 !\"'()+,-./?")
STAGE_NAME_MAX, WORLD_NAME_MAX = 24, 24       # characters
STAGE_NAME_WARN, WORLD_NAME_WARN = 21, 18     # longest names in the game (wider ones were not tested)

# Keyframes. Edit test 6 confirmed new values on axes that already move and any animation length; edit
# test 8 (passed 2026-11-08) confirmed fixed axes that start moving, turning, and a 64.7 KB animation.

# Edit test 9 (passed 2026-11-08) confirmed tilted / rolled goals and bumpers, turned platforms (sloped
# collision faces) and imported OBJ meshes (collision triangles).
ROTATION_TEST9_PASSED = True                  # passed in-game 2026-11-08
# Goal / bumper sizes: the game draws a sized goal or bumper at its size, but the release acceptance test (and
# the game's code, run in an emulator) showed that it ignores the size for a goal's finish line and a bumper's
# bounce: a goal taller than about 1.2 cannot be finished at all, and a bumper bounces at its normal size
# whatever its drawn size. So sizes are shown and kept, but never edited.
SIZES_WORK_IN_GAME = False
TEXTURE_TEST10_PASSED = True                  # other stage textures and own UVs: passed in-game 2026-11-08
SIZE_RANGE = (0.1, 10.0)                      # goal / bumper size along each of its own axes
SIZE_LOCKED = ("the game draws a goal's or bumper's size but ignores it for the finish line and the bounce, so "
               "sizes cannot be edited")
LARGEST_GAME_MODEL_TRIANGLES = 3336           # 5-7's model, the most detailed stage in the game
LARGEST_GAME_COLLISION_FACES = 2513           # 3-3's collision
MESH_TRIANGLE_LIMIT = 3000                    # per imported model
LARGEST_GAME_ANIMATION = 46092                # bytes (decoded): 5-6, the largest animation in the game
LARGEST_TESTED_ANIMATION = 64720              # bytes (decoded): 2-9 in edit test 8, played in-game

# kind -> (record source, pool key, position field, position target suffix)
KINDS = {
    "start": ("start", None, "position", "position"),
    "goal": ("pool", "A", "translation", "translation"),
    "bumper": ("pool", "B", "translation", "translation"),
    "banana": ("pool", "C", "position", "position"),
}
TARGET_PREFIX = {"start": "start", "goal": "pool_A", "bumper": "pool_B", "banana": "pool_C"}
PREFIX_KIND = {v: k for k, v in TARGET_PREFIX.items()}
FLAG_TARGET = {("goal", "red"): "u32_30", ("banana", "bunch"): "u32_0C"}


class EditorError(Exception):
    pass


# --------------------------------------------------------------------------- angles

def angle_to_degrees(word: int) -> float:
    return (word & 0xFFFF) * 360.0 / ANGLE_FULL


def degrees_to_angle(deg: float, old_word: int = 0) -> int:
    return (old_word & ~0xFFFF) | (int(round(deg % 360.0 * ANGLE_FULL / 360.0)) % ANGLE_FULL)


def goal_heading(matrix) -> Optional[float]:
    """Facing of a goal's front (local +z) in the start-angle convention: 0 = towards -z,
    increasing = turning left. Stored matrices are applied transposed, so the front is row 2."""
    fx, _fy, fz = _unit_rows(matrix)[2]
    if math.hypot(fx, fz) < 0.5:
        return None
    return math.degrees(math.atan2(-fx, -fz)) % 360.0


def yaw_rows(deg: float):
    """Stored-form rotation by deg about +y (positive turns left)."""
    a = math.radians(deg)
    c, s = math.cos(a), math.sin(a)
    return ((c, 0.0, -s), (0.0, 1.0, 0.0), (s, 0.0, c))


def rotate_matrix(m, deg: float):
    r = yaw_rows(deg)
    return [[sum(m[i][k] * r[k][j] for k in range(3)) for j in range(3)] for i in range(3)]


def parse_size(value) -> Tuple[float, float, float]:
    """(x, y, z) sizes from a number (all three) or three numbers, each within SIZE_RANGE."""
    try:
        vals = (float(value),) * 3 if isinstance(value, (int, float)) else tuple(float(v) for v in value)
    except (TypeError, ValueError):
        raise EditorError("a size needs x, y and z") from None
    if len(vals) != 3 or not all(math.isfinite(v) for v in vals):
        raise EditorError("a size needs x, y and z")
    lo, hi = SIZE_RANGE
    if not all(lo - 1e-9 <= v <= hi + 1e-9 for v in vals):
        raise EditorError(f"each size must be between {lo:g} and {hi:g}")
    return tuple(round(v, 3) for v in vals)


def _mat3(a, b):
    return tuple(tuple(sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)) for i in range(3))


def turn_rows(tilt: float, yaw: float, roll: float):
    """Stored-form rows of Rz(roll) . Rx(tilt) . Ry(yaw) (row vectors): the thing is tilted about its own
    x axis and rolled about its own z axis first, then turned about the vertical (degrees).
    Tilt > 0 leans its top towards its local +z; roll > 0 leans its top towards its local -x."""
    t, y, r = (math.radians(v) for v in (tilt, yaw, roll))
    ct, st, cy, sy, cr, sr = math.cos(t), math.sin(t), math.cos(y), math.sin(y), math.cos(r), math.sin(r)
    Rz = ((cr, sr, 0.0), (-sr, cr, 0.0), (0.0, 0.0, 1.0))
    Rx = ((1.0, 0.0, 0.0), (0.0, ct, st), (0.0, -st, ct))
    Ry = ((cy, 0.0, -sy), (0.0, 1.0, 0.0), (sy, 0.0, cy))
    return _mat3(_mat3(Rz, Rx), Ry)


def turn_angles(m) -> Tuple[float, float, float]:
    """Inverse of turn_rows: (tilt, yaw, roll) in degrees (tilt within +-90)."""
    st = max(-1.0, min(1.0, -m[2][1]))
    ct = math.hypot(m[2][0], m[2][2])
    t = math.atan2(st, ct)
    if ct > 1e-9:
        y = math.atan2(m[2][0], m[2][2])
        r = math.atan2(m[0][1], m[1][1])
    else:                                    # tilted straight up or down: keep it all in the turn
        y = math.atan2(-m[0][2], m[0][0])
        r = 0.0
    return math.degrees(t), math.degrees(y), math.degrees(r)


def _unit_rows(m):
    """Rows scaled to length 1 (the rotation part of a sized goal or bumper)."""
    out = []
    for row in m:
        n = math.sqrt(sum(v * v for v in row))
        out.append(tuple(v / n for v in row) if n > 1e-12 else tuple(row))
    return tuple(out)


def object_scale(m) -> Tuple[float, float, float]:
    """Size of a goal or bumper along its own x, y, z: the lengths of its matrix rows (row k is where its
    local axis k goes), rounded to 1/1000 so a stored 1.0 reads back as 1.0."""
    return tuple(round(math.sqrt(sum(v * v for v in row)), 3) for row in m)


def _raw_size(raw) -> Tuple[float, float, float]:
    """object_scale of a matrix stored as nine 1/4096 integers."""
    return object_scale([[v / FX for v in raw[3 * r:3 * r + 3]] for r in range(3)])


def object_rows(tilt: float, facing: float, rotate: float, size=(1.0, 1.0, 1.0)):
    """Rows of a goal or bumper matrix from the editor's Tilt / Facing / Rotate and Size X / Y / Z. Facing is
    the direction of the object's front (local +z) as for goal_heading: 0 = towards -z, larger turns left.
    The size scales the object along its own axes before it is turned (M = diag(size) . R)."""
    rows = turn_rows(tilt, facing - 180.0, rotate)
    if tuple(size) == (1.0, 1.0, 1.0):
        return rows
    return tuple(tuple(v * float(k) for v in row) for row, k in zip(rows, size))


def object_turns(m) -> Tuple[float, float, float]:
    """(tilt, facing, rotate) of a goal or bumper matrix (rows as stored; a size is ignored)."""
    t, y, r = turn_angles(_unit_rows(m))
    return t, (y + 180.0) % 360.0, r


def _rotated(polys, centre, rows):
    """Polygons turned by rows (row vectors) about centre."""
    out = []
    for poly in polys:
        q = []
        for p in poly:
            d = (p[0] - centre[0], p[1] - centre[1], p[2] - centre[2])
            q.append([centre[j] + sum(d[k] * rows[k][j] for k in range(3)) for j in range(3)])
        out.append(q)
    return out


def _q(v: float) -> int:
    return int(round(v * FX))


def _rows_of(raw) -> tuple:
    """Stored 9 values (1/4096 units) -> rows."""
    v = [x / FX for x in raw]
    return ((v[0], v[1], v[2]), (v[3], v[4], v[5]), (v[6], v[7], v[8]))


# --------------------------------------------------------------------------- stage state

OBJECT_KINDS = ("goal", "bumper", "banana")
POS_FIELD = {"goal": "translation", "bumper": "translation", "banana": "position"}
KIND_TITLE = {"start": "Start", "goal": "Goal", "bumper": "Bumper", "banana": "Banana"}
DEFAULT_HEIGHT = {"banana": 0.5, "bumper": 0.0, "goal": 0.0}
IDENTITY_ROWS = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))


def _empty_state() -> dict:
    return {"fields": {}, "positions": {}, "deleted": [], "added": [], "geometry": [],
            "replace": None, "pos_scale": None, "anim": None}


def _copy_anim(a: Optional[dict]) -> Optional[dict]:
    """{"frames": n or None, "keys": {"node:channel": [keys]}} or None."""
    if not a:
        return None
    return {"frames": a.get("frames"), "keys": {k: [dict(x, handles=list(x["handles"])) if "handles" in x else dict(x)
                                                    for x in v] for k, v in (a.get("keys") or {}).items()}}


def _copy_replace(r: Optional[dict]) -> Optional[dict]:
    return None if r is None else json.loads(json.dumps(r))


def _copy_names(n: dict) -> dict:
    return {"stages": dict(n.get("stages") or {}), "worlds": dict(n.get("worlds") or {})}


def _copy_geo(g: dict) -> dict:
    return {k: (list(v) if isinstance(v, (list, tuple)) else v) for k, v in g.items()}


def _copy_state(st: dict) -> dict:
    return {"fields": dict(st["fields"]), "positions": dict(st["positions"]), "deleted": list(st["deleted"]),
            "added": [dict(a) for a in st["added"]], "geometry": [_copy_geo(g) for g in st.get("geometry", [])],
            "replace": _copy_replace(st.get("replace")), "pos_scale": st.get("pos_scale"),
            "anim": _copy_anim(st.get("anim"))}


def new_stage_params(platform: Optional[dict] = None, start: Optional[dict] = None) -> dict:
    """Normalised Create New Stage parameters: a platform box and the start (default: 1.5 from the platform's
    -x end, on its top, facing +x along it - as trcore.newstage does)."""
    pf = dict(DEFAULT_PLATFORM, **(platform or {}))
    pf = {"x": sorted(_qf(v) for v in pf["x"]), "z": sorted(_qf(v) for v in pf["z"]),
          "top": _qf(pf["top"]), "thickness": _qf(pf["thickness"])}
    if pf["x"][1] - pf["x"][0] < 1 or pf["z"][1] - pf["z"][0] < 1:
        raise EditorError("the platform must be at least 1 x 1")
    if pf["thickness"] <= 0:
        raise EditorError("the platform's thickness must be more than 0")
    if start is None:
        start = {"position": [pf["x"][0] + 1.5, pf["top"], (pf["z"][0] + pf["z"][1]) / 2], "heading": 270.0}
    st = {"position": [_qf(v) for v in start["position"]], "heading": float(start.get("heading", 270.0)) % 360.0}
    return {"platform": pf, "start": st}


# --------------------------------------------------------------------------- geometry items

GEO_KINDS = {
    # kind: (visible, solid, default thickness, title)
    "platform": (True, True, 1.0, "Platform"),
    "invisible": (False, True, 0.0, "Invisible floor"),
    "decoration": (True, False, 0.0, "Decoration"),
    "remove": (False, False, None, "No-collision area"),
}
MIN_GEO_SIZE = 0.25


def _qf(v: float) -> float:
    """A float on the stored 1/4096 grid."""
    return round(float(v) * FX) / FX


def geo_title(g: dict) -> str:
    if g["type"] == "remove":
        return "No-collision area"
    if g["type"] == "mesh":
        return f"Model ({g.get('name') or 'OBJ'})" if g["visible"] else f"Invisible model ({g.get('name') or 'OBJ'})"
    vis, solid, th = g["visible"], g["solid"], g["thickness"]
    if vis and solid:
        return "Platform" if th > 0 else "Floor panel"
    if solid:
        return "Invisible block" if th > 0 else "Invisible floor"
    return "Decoration"


def geo_rotation(g: dict) -> Tuple[float, float, float]:
    r = g.get("rot") or (0.0, 0.0, 0.0)
    return float(r[0]), float(r[1]), float(r[2])


def stage_texture_rgba(rom, key: str, model_bytes: Optional[bytes], texture: str,
                       palette: Optional[str]) -> Optional[tuple]:
    """(width, height, RGBA bytes) of one of a stage's textures with a palette (its .nsbtx, or textures
    embedded in the model), or None."""
    from trcore.nitro.nsbtx import read_textures
    tex = None
    if model_bytes is not None and Nsbmd(model_bytes, key).has_textures:
        tex = read_textures(model_bytes, key)
    if tex is None and rom is not None:
        path = f"model/stage/{key}_nsbtx.lz"
        if path in rom.id_by_path:
            raw = rom.file_bytes(path)
            tex = read_textures(lz10.decode(raw)[0] if raw[:1] == b"\x10" else raw, path)
    if tex is None or texture not in tex.textures:
        return None
    return tex.rgba(texture, palette)


def mesh_scale(g: dict) -> Tuple[float, float, float]:
    """An imported model's size factors along its own x, y, z (stored as one number when all equal)."""
    s = g.get("scale", 1.0)
    if isinstance(s, (list, tuple)):
        return float(s[0]), float(s[1]), float(s[2])
    return float(s), float(s), float(s)


def mesh_parts(g: dict) -> List[tuple]:
    """(corners, material slot, texture coordinates (u, v) per corner or None) of every triangle of an
    imported model as built: scaled along its own axes, turned about its origin, placed at its position.
    With Both sides, the reversed copies follow the originals."""
    rows = turn_rows(*geo_rotation(g))
    sx, sy, sz = mesh_scale(g)
    px, py, pz = g["pos"]
    verts = [(v[0] * sx, v[1] * sy, v[2] * sz) for v in g["verts"]]
    moved = [(px + sum(v[k] * rows[k][0] for k in range(3)), py + sum(v[k] * rows[k][1] for k in range(3)),
              pz + sum(v[k] * rows[k][2] for k in range(3))) for v in verts]
    fmat = g.get("fmat")
    fuv = g.get("fuv") if g.get("uv_mode") == "uv" else None
    uvs = g.get("uvs") or []
    out = []
    for n, f in enumerate(g["faces"]):
        uv = [tuple(uvs[k]) for k in fuv[n]] if fuv and fuv[n] is not None else None
        out.append(([list(moved[i]) for i in f], fmat[n] if fmat else 0, uv))
    if g.get("both_sides"):
        out += [(list(reversed(c)), m, list(reversed(uv)) if uv else None) for c, m, uv in out]
    return out


def geo_parts(g: dict) -> List[tuple]:
    """(corners, material slot, (u, v) per corner or None) of every drawn polygon of a platform or model."""
    if g["type"] == "mesh":
        return mesh_parts(g)
    return [(p, 0, None) for p in geo_polygons(g)]


def geo_polygons(g: dict) -> List[list]:
    """Stage-coordinate polygons of a platform (turned about its centre if it has a rotation) or of an
    imported model (scaled, turned about its origin, placed at its position)."""
    if g["type"] == "mesh":
        return [c for c, _m, _uv in mesh_parts(g)]
    rot = geo_rotation(g)
    turned = any(abs(a) > 1e-9 for a in rot)
    if g["thickness"] > 0:                   # a turned box shows its underside too
        polys = box_polygons(g["x"], [g["top"] - g["thickness"], g["top"]], g["z"], bottom=turned)
    else:
        polys = [floor_polygon(g["x"], g["z"], g["top"])]
    if turned:
        centre = ((g["x"][0] + g["x"][1]) / 2, g["top"] - g["thickness"] / 2, (g["z"][0] + g["z"][1]) / 2)
        polys = _rotated(polys, centre, turn_rows(rot[0], rot[1], rot[2]))
    return polys


def geo_bounds(g: dict) -> Tuple[List[float], List[float], List[float]]:
    """[min, max] in x, y and z of the item as it will be built."""
    if g["type"] == "remove":
        return list(g["x"]), list(g["y"]) if g.get("y") else [-1.0, 3.0], list(g["z"])
    pts = [p for poly in geo_polygons(g) for p in poly]
    return ([min(p[0] for p in pts), max(p[0] for p in pts)], [min(p[1] for p in pts), max(p[1] for p in pts)],
            [min(p[2] for p in pts), max(p[2] for p in pts)])


def geo_is_turned(g: dict) -> bool:
    return g["type"] == "mesh" or (g["type"] == "platform" and any(abs(a) > 1e-9 for a in geo_rotation(g)))


def check_geo(g: dict) -> dict:
    """Normalise and validate one geometry item (raises EditorError)."""
    g = _copy_geo(g)
    if g.get("type") not in ("platform", "remove", "mesh"):
        raise EditorError(f"unknown geometry type {g.get('type')!r}")
    if g["type"] == "mesh":
        return _check_mesh(g)
    for ax in ("x", "z"):
        v = g.get(ax)
        if not isinstance(v, (list, tuple)) or len(v) != 2:
            raise EditorError(f"{ax} needs [min, max]")
        a, b = sorted(_qf(t) for t in v)
        if b - a < MIN_GEO_SIZE:
            raise EditorError(f"the {ax} size must be at least {MIN_GEO_SIZE}")
        g[ax] = [a, b]
    if g["type"] == "platform":
        rot = g.get("rot")
        if rot is not None:
            try:
                rot = [round(float(a), 3) for a in rot]
            except (TypeError, ValueError):
                rot = None
            if rot is None or len(rot) != 3 or not all(math.isfinite(a) for a in rot):
                raise EditorError("a rotation needs tilt, turn and roll in degrees")
            rot = [((a + 180.0) % 360.0) - 180.0 for a in rot]
            if all(abs(a) < 1e-9 for a in rot):
                rot = None
        if rot is None:
            g.pop("rot", None)
        else:
            g["rot"] = rot
        g["top"] = _qf(g.get("top", 0.0))
        g["thickness"] = max(0.0, _qf(g.get("thickness", 0.0)))
        g["visible"], g["solid"] = bool(g.get("visible", True)), bool(g.get("solid", True))
        if not (g["visible"] or g["solid"]):
            raise EditorError("a platform must be drawn, solid, or both")
        tex = g.get("texture")
        if tex is None or tex == "":
            g.pop("texture", None)
        elif not isinstance(tex, str) or len(tex) > 64:
            raise EditorError("a platform's texture is the name of one of the stage's shapes")
        g.pop("y", None)
    else:
        y = g.get("y")
        if y is not None:
            a, b = sorted(_qf(t) for t in y)
            if b - a < MIN_GEO_SIZE:
                raise EditorError("the height range must be at least 0.25")
            g["y"] = [a, b]
        else:
            g["y"] = None
        for k in ("top", "thickness", "visible", "solid", "texture", "rot"):
            g.pop(k, None)
    return g


def _check_mesh(g: dict) -> dict:
    """Validate an imported model item: {type: mesh, name, verts [[x,y,z]], faces [[i,j,k]], pos, scale
    (one number or x, y, z), rot, visible, solid, both_sides} and optionally its own texture coordinates
    (uvs [[u,v]], fuv: per triangle three indices into uvs or None, uv_mode "uv" | "projected") and
    materials (materials [names], fmat: per triangle an index into materials, textures: per material the
    stage shape it is drawn with, or None for the stage's main one)."""
    try:
        verts = [tuple(round(float(c), 5) for c in v) for v in g["verts"]]
        faces = [tuple(int(i) for i in f) for f in g["faces"]]
        pos = [_qf(c) for c in g.get("pos", (0.0, 0.0, 0.0))]
        sc = g.get("scale", 1.0)
        sc = [float(v) for v in sc] if isinstance(sc, (list, tuple)) else [float(sc)] * 3
        rot = [round(float(a), 3) for a in (g.get("rot") or (0.0, 0.0, 0.0))]
    except (KeyError, TypeError, ValueError):
        raise EditorError("an imported model needs vertices, triangles, a position and a scale") from None
    if len(sc) != 3 or not all(math.isfinite(v) and 0.001 <= v <= 1000.0 for v in sc):
        raise EditorError("the model's scale must be between 0.001 and 1000 along each axis")
    sc = [round(v, 6) for v in sc]
    scale = sc[0] if sc[0] == sc[1] == sc[2] else sc
    if not verts or not faces:
        raise EditorError("the model has no triangles")
    if any(len(v) != 3 or not all(math.isfinite(c) for c in v) for v in verts):
        raise EditorError("bad vertex in the model")
    if any(len(f) != 3 or min(f) < 0 or max(f) >= len(verts) for f in faces):
        raise EditorError("bad triangle in the model")
    n = len(faces) * (2 if g.get("both_sides") else 1)
    if n > MESH_TRIANGLE_LIMIT:
        raise EditorError(f"the model has {n} triangles; at most {MESH_TRIANGLE_LIMIT} can be imported at once")
    if len(pos) != 3 or len(rot) != 3:
        raise EditorError("an imported model needs a position x, y, z and a rotation")
    out = {"id": g.get("id"), "type": "mesh", "name": str(g.get("name") or "model")[:64], "verts": verts,
           "faces": faces, "pos": pos, "scale": scale, "rot": [((a + 180.0) % 360.0) - 180.0 for a in rot],
           "visible": bool(g.get("visible", True)), "solid": bool(g.get("solid", True)),
           "both_sides": bool(g.get("both_sides", False))}
    if not (out["visible"] or out["solid"]):
        raise EditorError("a model must be drawn, solid, or both")
    if g.get("source"):
        out["source"] = str(g["source"])[:64]
    try:
        if g.get("uvs") is not None:
            uvs = [(round(float(u), 6), round(float(v), 6)) for u, v in g["uvs"]]
            if not all(math.isfinite(c) for uv in uvs for c in uv):
                raise ValueError
            fuv = [None if f is None else tuple(int(k) for k in f) for f in g.get("fuv") or []]
            if len(fuv) != len(faces) or any(f is not None and (len(f) != 3 or min(f) < 0 or max(f) >= len(uvs))
                                             for f in fuv):
                raise ValueError
            out.update({"uvs": uvs, "fuv": fuv})
        mode = g.get("uv_mode") or ("uv" if "uvs" in out else "projected")
        if mode not in ("uv", "projected") or (mode == "uv" and "uvs" not in out):
            raise ValueError
        if "uvs" in out:
            out["uv_mode"] = mode
        if g.get("materials") is not None:
            mats = [str(m)[:64] for m in g["materials"]]
            fmat = [int(k) for k in g.get("fmat") or [0] * len(faces)]
            if not mats or len(fmat) != len(faces) or any(not 0 <= k < len(mats) for k in fmat):
                raise ValueError
            out.update({"materials": mats, "fmat": fmat})
        if g.get("textures") is not None:
            n = len(out.get("materials") or [None])
            tex = [None if t in (None, "") else str(t)[:64] for t in g["textures"]]
            if len(tex) != n:
                raise ValueError
            if any(t is not None for t in tex):
                out["textures"] = tex
    except (TypeError, ValueError):
        raise EditorError("the model's texture coordinates or materials do not fit its triangles") from None
    return out


def _height_on(poly, x: float, z: float) -> Optional[float]:
    """Height of an upward-facing convex polygon at (x, z), or None if (x, z) is outside it."""
    a, b, c = poly[0], poly[1], poly[2]
    n = ((b[1] - a[1]) * (c[2] - a[2]) - (b[2] - a[2]) * (c[1] - a[1]),
         (b[2] - a[2]) * (c[0] - a[0]) - (b[0] - a[0]) * (c[2] - a[2]),
         (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]))
    if n[1] <= 1e-9:
        return None
    sign = 0
    for k in range(len(poly)):
        p, q = poly[k], poly[(k + 1) % len(poly)]
        cr = (q[0] - p[0]) * (z - p[2]) - (q[2] - p[2]) * (x - p[0])
        if abs(cr) < 1e-12:
            continue
        s = 1 if cr > 0 else -1
        if sign and s != sign:
            return None
        sign = s
    return a[1] - (n[0] * (x - a[0]) + n[2] * (z - a[2])) / n[1]


def _split_id(oid: str) -> Tuple[str, Optional[int], Optional[str]]:
    """'banana:3' -> ('banana', 3, None); 'banana:n2' -> ('banana', None, 'banana:n2')."""
    kind, rest = oid.split(":", 1)
    if kind not in KINDS:
        raise EditorError(f"unknown object {oid!r}")
    if rest.startswith("n"):
        return kind, None, oid
    return kind, int(rest), None


class StageState:
    """One stage: original bytes, the editing state, and the bytes/document rebuilt from it.

    State (plain data, snapshotted for undo):
      fields    {target: raw}   start fields and non-position fields of original objects
      positions {id: raw xyz}   moved original goals / bumpers / bananas
      deleted   [id]            removed original objects
      added     [{id, kind, group, pos, bunch, red, matrix}]   new objects (group 0)
    A moved original object stays a plain field edit while its original collision cells still cover it,
    and is re-listed (trcore.stc.objects "move") otherwise; additions and removals always go through
    trcore.stc.objects. The current bytes are always rebuilt from the original."""

    def __init__(self, project: "Project", entry: dict):
        self.project = project
        self.entry = entry
        self.key = entry["stage_key"]
        self.rom_original = project.rom.stage_file_decoded(self.key)
        self.rom_doc = StageDocument(self.rom_original, self.key)
        if self.rom_doc.has_errors():
            raise EditorError(f"{self.key}: stage file has structural errors; it can be viewed only with trcore")
        try:
            self.rom_model = project.rom.stage_model_decoded(self.key)
        except (RomError, KeyError, ValueError):
            self.rom_model = None
        # the BASE every edit applies to: the ROM's stage, or a new stage built from it (Create New Stage)
        self.original, self.original_doc, self.base_model = self.rom_original, self.rom_doc, self.rom_model
        self._base_params: Optional[dict] = None
        self._base_shape: Optional[str] = None
        self._base_cache: Dict[str, tuple] = {}
        self.state = _empty_state()
        self.current = self.original
        self.doc = self.original_doc
        self.order: Dict[str, List[str]] = {}
        self._field_edits: List[dict] = []
        self._ops: List[dict] = []
        self._collision_ops: List[dict] = []
        self._model_adds: Dict[str, list] = {}
        self.current_model: Optional[bytes] = None
        self._platform_shape = None
        self._platform_shape_error = None
        self._faces_cache = None
        self._tex_choices: Optional[List[dict]] = None
        self._tex_images: Dict[str, Optional[tuple]] = {}
        self._model = None
        self.model_error = None
        self._kinds = None
        self._grids = None
        self._anim_loaded = False
        self.rom_anim: Optional[bytes] = None
        self.anim_error: Optional[str] = None
        self._anim_orig = None                 # trcore JointAnim of the ROM's animation
        self._anim_cur = None                  # ... of the current (edited) animation
        self._anim_cache: Dict[tuple, list] = {}
        self.current_anim: Optional[bytes] = None
        self._anim_ops: List[dict] = []
        self._compute_order(self.state)

    # ---- animation (moving parts)
    def _load_anim(self):
        if self._anim_loaded:
            return
        self._anim_loaded = True
        path = STAGE_ANIM_FORMAT.format(self.key)
        if path not in self.project.rom.id_by_path:
            return
        try:
            raw = self.project.rom.file_bytes(path)
            self.rom_anim = lz10.decode(raw)[0] if raw[:1] == b"\x10" else raw
            self._anim_orig = Nsbca(self.rom_anim, path).anims[0]
        except (NitroError, lz10.LZ10Error, IndexError, ValueError) as exc:
            self.anim_error = str(exc)
            self.rom_anim = self._anim_orig = None

    @property
    def anim_path(self) -> str:
        return STAGE_ANIM_FORMAT.format(self.key)

    @property
    def anim_bytes(self) -> Optional[bytes]:
        """The stage's animation as it will be saved (None: the stage has none)."""
        self._load_anim()
        return self.current_anim if self.current_anim is not None else self.rom_anim

    def _anim(self, original: bool = False):
        self._load_anim()
        if original or self.current_anim is None:
            return self._anim_orig
        if self._anim_cur is None:
            self._anim_cur = Nsbca(self.current_anim, self.anim_path).anims[0]
        return self._anim_cur

    @property
    def original_frames(self) -> Optional[int]:
        a = self._anim(True)
        return a.frames if a else None

    @property
    def frames(self) -> Optional[int]:
        a = self._anim()
        return a.frames if a else None

    def anim_parts(self) -> List[dict]:
        """The moving parts: {node, name, group (collision group that follows it or None), objects,
        channels {channel: "moving" | "fixed" | "none"}} for every animated node of the original."""
        a = self._anim(True)
        m = self.model
        if a is None or not m or self.is_new_stage:
            return []
        names = [n["name"] for n in m["model"].nodes]
        groups = {g.get("name_text_04"): g["index"] for g in self.original_doc.groups if g.get("index")}
        counts: Dict[int, int] = {}
        for kind in OBJECT_KINDS:
            for r in self.original_doc.pools[KIND_POOL[kind]]["records"]:
                counts[r["group"]] = counts.get(r["group"], 0) + 1
        out = []
        for nd in a.nodes:
            if not nd.animated or nd.index >= len(names):
                continue
            g = groups.get(names[nd.index])
            kinds = {ch: channel_kind(nd, ch) for ch in CHANNELS}
            if kinds["rx"] == "moving":            # one rotation track: say which turns actually change
                for ch in ("rx", "ry", "rz"):
                    vals = self.channel_values(nd.index, ch, original=True)
                    kinds[ch] = "moving" if vals and max(vals) - min(vals) > 0.01 else "fixed"
            out.append({"node": nd.index, "name": names[nd.index], "group": g,
                        "objects": counts.get(g, 0) if g else 0, "channels": kinds})
        return out

    @property
    def anim_has_turning(self) -> bool:
        """True if the original animation turns a part (its tracks hold rotations per frame)."""
        a = self._anim(True)
        return bool(a) and any(channel_kind(nd, "rx") == "moving" for nd in a.nodes)

    def channel_values(self, node: int, ch: str, original: bool = False) -> List[float]:
        """Per-frame values of a channel (units, or degrees for turns)."""
        a = self._anim(original)
        if a is None:
            return []
        key = ("orig" if a is self._anim_orig else "cur", node)
        if key not in self._anim_cache:
            self._anim_cache[key] = node_channels(a.nodes[node], a.frames)
        return self._anim_cache[key][ch]

    def channel_keys(self, node: int, ch: str) -> Optional[List[dict]]:
        keys = ((self.state.get("anim") or {}).get("keys") or {}).get(f"{node}:{ch}")
        return [dict(k) for k in keys] if keys else None

    def channel_editable(self, node: int, ch: str) -> Tuple[bool, str]:
        """(can it be keyed now, why not)."""
        if self.is_new_stage:
            return False, "a new stage has no moving parts"
        if not any(p["node"] == node for p in self.anim_parts()):
            return False, "only the stage's moving parts can be animated"
        return True, ""

    def length_editable(self) -> Tuple[bool, str]:
        if self.is_new_stage or not self.anim_parts():
            return False, "this stage has no moving parts"
        return True, ""

    def plan_anim(self) -> List[dict]:
        return [dict(op) for op in self._anim_ops]

    def _anim_ops_for(self, state: dict) -> List[dict]:
        a = state.get("anim")
        if not a:
            return []
        ops = []
        if a.get("frames"):
            ops.append({"op": "set_length", "frames": int(a["frames"]), "label": "length"})
        for k in sorted(a.get("keys") or {}, key=lambda s: (int(s.split(":")[0]), CHANNELS.index(s.split(":")[1]))):
            node, ch = k.split(":")
            ops.append({"op": "set_keys", "node": int(node), "channel": ch, "keys": [dict(x) for x in a["keys"][k]],
                        "label": f"node {node} {ch}"})
        return ops

    # ---- model outline (read once)
    @property
    def model(self) -> Optional[dict]:
        if self._model is None and self.model_error is None:
            try:
                if self._base_params is None:
                    self._model = load_stage_model(self.project.rom, self.key) or {}
                else:
                    f = Nsbmd(self.base_model, self.key)
                    geo = model_geometry(f, 0)
                    self._model = {"path": self.project.rom.profile()["stage_model_format"].format(self.key),
                                   "model": f.models[0], "triangles": geo["triangles"], "notes": geo["notes"],
                                   "bounds": model_bounds(geo["triangles"])}
            except NitroError as exc:
                self.model_error = str(exc)
                self._model = {}
        return self._model or None

    # ---- the base (ROM stage or a new stage)
    @property
    def is_new_stage(self) -> bool:
        return self._base_params is not None

    def _base_tuple(self):
        return (self.original, self.original_doc, self.base_model, self._base_params, self._base_shape,
                self._model, self.model_error, self._kinds, self._grids, self._platform_shape,
                self._platform_shape_error)

    def _restore_base(self, t):
        (self.original, self.original_doc, self.base_model, self._base_params, self._base_shape, self._model,
         self.model_error, self._kinds, self._grids, self._platform_shape, self._platform_shape_error) = t
        self._faces_cache = None
        self._tex_choices = None
        self._tex_images = {}

    def _set_base(self, params: Optional[dict]):
        """Switch the base to the ROM stage (None) or to a new stage built with `params`."""
        if params == self._base_params:
            return
        if params is None:
            self.original, self.original_doc, self.base_model = self.rom_original, self.rom_doc, self.rom_model
            self._base_shape = None
        else:
            if self.rom_model is None:
                raise EditorError("this stage has no model file, so it cannot be replaced by a new stage")
            key = json.dumps(params, sort_keys=True)
            if key not in self._base_cache:
                try:
                    s_bytes, m_bytes, entry = build_new_stage(
                        self.rom_original, self.rom_model, self.key, platform=params["platform"],
                        start=params["start"], shape=params.get("shape"))
                except NewStageError as exc:
                    raise EditorError(f"the new stage cannot be built: {exc}") from None
                self._base_cache[key] = (s_bytes, m_bytes, entry["new_stage"]["shape"])
            s_bytes, m_bytes, shape = self._base_cache[key]
            self.original, self.base_model, self._base_shape = s_bytes, m_bytes, shape
            self.original_doc = StageDocument(s_bytes, self.key)
        self._base_params = _copy_replace(params)
        self._model = None
        self.model_error = None
        self._kinds = None
        self._grids = None
        self._platform_shape = self._base_shape
        self._platform_shape_error = None
        self._faces_cache = None
        self._tex_choices = None
        self._tex_images = {}

    @property
    def pos_scale(self) -> Optional[float]:
        """The model's position scale as it will be saved."""
        if self.state.get("pos_scale"):
            return float(self.state["pos_scale"])
        m = self.model
        return m["model"].pos_scale if m else None

    @property
    def original_pos_scale(self) -> Optional[float]:
        if self.rom_model is None:
            return None
        try:
            return Nsbmd(self.rom_model, self.key).models[0].pos_scale
        except NitroError:
            return None

    @property
    def group_kinds(self) -> Dict[int, str]:
        if self._kinds is None:
            m = self.model
            self._kinds = group_kinds(self.original_doc, m["model"] if m else None)
        return self._kinds

    @property
    def grids(self):
        if self._grids is None:
            try:
                self._grids = read_grids(self.original)
            except CellError:
                self._grids = []
        return self._grids

    @property
    def platform_shape(self) -> Optional[str]:
        """The model shape that draws new platforms (its texture is the placeholder), or None."""
        if self._platform_shape is None and self._platform_shape_error is None:
            try:
                mb = self.rom_model
                if mb is None:
                    raise NewStageError("this stage has no model file")
                self._platform_shape = pick_platform_shape(mb, self.original, self.key)
            except (NewStageError, NitroError, RomError, ValueError, KeyError) as exc:
                self._platform_shape_error = str(exc)
        return self._platform_shape

    # ---- placeholder textures (the stage's own, until custom textures come)
    def texture_choices(self) -> List[dict]:
        """The stage textures new platforms and models can be drawn with: [{shape, texture, palette, orig_w,
        orig_h, label, default}] (trcore.newstage.texture_choices), the stage's main one first."""
        if self._tex_choices is None:
            out: List[dict] = []
            try:
                if self.rom_model is not None:
                    out = texture_choices(self.rom_model, self.original, self.key)
            except (NitroError, NewStageError, ValueError, KeyError, IndexError):
                out = []
            main = self.platform_shape
            for c in out:
                c["default"] = c["shape"] == main
                c["label"] = c["texture"] + (f" ({c['palette']})" if c.get("palette") else "")
            out.sort(key=lambda c: not c["default"])
            self._tex_choices = out
        return [dict(c) for c in self._tex_choices]

    def texture_image(self, shape: str) -> Optional[tuple]:
        """(width, height, RGBA bytes) of the texture a shape is drawn with, or None."""
        if shape not in self._tex_images:
            img = None
            c = next((c for c in self.texture_choices() if c["shape"] == shape), None)
            if c is not None:
                try:
                    img = stage_texture_rgba(self.project.rom, self.key, self.rom_model, c["texture"], c["palette"])
                except (NitroError, RomError, ValueError, KeyError, IndexError):
                    img = None
            self._tex_images[shape] = img
        return self._tex_images[shape]

    def _texture_shape(self, name: Optional[str]) -> str:
        if name:
            for c in self.texture_choices():
                if c["shape"] == name:
                    return name
            raise EditorError(f"{name} is not one of this stage's textures for new platforms and models")
        shape = self.platform_shape
        if not shape:
            raise EditorError(f"platforms cannot be drawn in this stage: {self._platform_shape_error}")
        return shape

    def _compose_geometry(self, state: dict):
        """Geometry items -> (collision ops, {shape: model polygons}), in the items' order. A polygon with
        its own texture coordinates is passed as {"corners", "st"} (texels of the shape's texture)."""
        coll: List[dict] = []
        adds: Dict[str, list] = {}
        sizes = {c["shape"]: (c["orig_w"], c["orig_h"]) for c in self.texture_choices()}
        for g in state.get("geometry", []):
            if g["type"] == "remove":
                op = {"op": "remove_box", "group": 0, "x": list(g["x"]), "z": list(g["z"]), "label": g["id"]}
                if g.get("y"):
                    op["y"] = list(g["y"])
                coll.append(op)
                continue
            parts = geo_parts(g)
            if g["solid"]:
                coll.append({"op": "add_polygons", "group": 0, "polygons": [c for c, _m, _uv in parts],
                             "label": g["id"]})
            if g["visible"]:
                if g["type"] == "mesh":
                    tex = g.get("textures") or []
                    shapes = [self._texture_shape(tex[k] if k < len(tex) else None)
                              for k in range(max(1, len(g.get("materials") or [None])))]
                else:
                    shapes = [self._texture_shape(g.get("texture"))]
                for corners, slot, uv in parts:
                    shape = shapes[slot] if slot < len(shapes) else shapes[0]
                    w, h = sizes.get(shape, (0, 0))
                    if uv is not None and w and h:
                        adds.setdefault(shape, []).append(
                            {"corners": corners, "st": [[u * w, (1.0 - v) * h] for u, v in uv]})
                    else:
                        adds.setdefault(shape, []).append(corners)
        return coll, adds

    def can_restructure(self) -> Optional[str]:
        """None if objects can be added / removed / re-listed in this stage, else the reason."""
        if self.original_doc.layout_variant != "common":
            return "this stage file uses an unusual layout"
        if not self.grids or not self.grids[0].present:
            return "this stage's collision cells could not be read"
        return None

    # ---- original records
    def _record(self, kind: str, index: int, doc: Optional[StageDocument] = None) -> dict:
        doc = doc or self.original_doc
        src, pool, _f, _t = KINDS[kind]
        recs = (doc.start or {}).get("records", []) if src == "start" else doc.pools.get(pool, {}).get("records", [])
        if not 0 <= index < len(recs):
            raise EditorError(f"{kind} {index} does not exist")
        return recs[index]

    def original_raw(self, target: str) -> Tuple[int, ...]:
        prefix, rest = target.split("[", 1)
        idx, field = rest.split("].")
        kind = PREFIX_KIND[prefix]
        rec = self._record(kind, int(idx))
        if field in ("position", "translation"):
            return tuple(_q(v) for v in rec[field])
        if field == "matrix":
            return tuple(_q(v) for row in rec["matrix"] for v in row)
        return (rec[field],)

    def original_position(self, kind: str, index: int) -> Tuple[int, int, int]:
        return tuple(_q(v) for v in self._record(kind, index)[POS_FIELD[kind]])

    def _covered(self, kind: str, index: int, pos_raw) -> bool:
        """True if the object's ORIGINAL collision cells still cover pos_raw (or nothing can be re-listed)."""
        if self.can_restructure():
            return True
        rec = self._record(kind, index)
        g = rec["group"]
        if g >= len(self.grids) or not self.grids[g].present:
            return True
        recs = self.original_doc.pools[KIND_POOL[kind]]["records"]
        local = sum(1 for r in recs[:index] if r["group"] == g)
        pos = [v / FX for v in pos_raw]
        return coverage_problem(self.grids[g], kind, local, pos, rec[POS_FIELD[kind]]) is None

    # ---- composing the state into field edits + object operations
    @staticmethod
    def _edit_entry(target: str, orig, raw) -> dict:
        field = target.rsplit(".", 1)[1]
        if field in ("position", "translation", "matrix"):
            return {"target": target, "from": [v / FX for v in orig], "to": [v / FX for v in raw]}
        return {"target": target, "from": orig[0], "to": raw[0]}

    def _compose(self, state: dict) -> Tuple[List[dict], List[dict]]:
        deleted = set(state["deleted"])
        fields: Dict[str, Tuple[int, ...]] = {}
        for t, v in state["fields"].items():
            prefix, rest = t.split("[", 1)
            kind = PREFIX_KIND[prefix]
            if f"{kind}:{int(rest.split(']')[0])}" in deleted:
                continue
            if v != self.original_raw(t):
                fields[t] = v
        ops: List[dict] = []
        for oid in sorted(deleted, key=lambda x: (x.split(":")[0], int(x.split(":")[1]))):
            kind, i, _ = _split_id(oid)
            ops.append({"op": "remove", "kind": kind, "index": i,
                        "expect_position": [v / FX for v in self.original_position(kind, i)]})
        for oid, pos in sorted(state["positions"].items()):
            kind, i, _ = _split_id(oid)
            if oid in deleted or tuple(pos) == self.original_position(kind, i):
                continue
            if self._covered(kind, i, pos):
                fields[f"{TARGET_PREFIX[kind]}[{i}].{POS_FIELD[kind]}"] = tuple(pos)
            else:
                ops.append({"op": "move", "kind": kind, "index": i,
                            "from": [v / FX for v in self.original_position(kind, i)],
                            "to": [v / FX for v in pos]})
        for a in state["added"]:
            op = {"op": "add", "kind": a["kind"], "group": a["group"], "position": [v / FX for v in a["pos"]]}
            if a["kind"] == "banana":
                op["bunch"] = bool(a.get("bunch"))
            if a["kind"] == "goal":
                op["red"] = bool(a.get("red"))
                op["matrix"] = [v / FX for v in a["matrix"]]
            elif a["kind"] == "bumper" and a.get("matrix"):
                op["matrix"] = [v / FX for v in a["matrix"]]
            ops.append(op)
        edits = [self._edit_entry(t, self.original_raw(t), v) for t, v in sorted(fields.items())]
        return edits, ops

    def _compute_order(self, state: dict):
        """Stable ids of the records in the rebuilt pools, in pool order (mirrors trcore.stc.objects)."""
        deleted = set(state["deleted"])
        ngroups = len(self.original_doc.groups)
        for kind in OBJECT_KINDS:
            recs = self.original_doc.pools[KIND_POOL[kind]]["records"]
            added = [a for a in state["added"] if a["kind"] == kind]
            order = []
            for g in range(ngroups):
                order += [f"{kind}:{r['index']}" for r in recs if r["group"] == g and f"{kind}:{r['index']}" not in deleted]
                order += [a["id"] for a in added if a["group"] == g]
            self.order[kind] = order

    def set_state(self, state: dict):
        """Rebuild the stage from `state`; raises EditorError (and changes nothing) if it is refused."""
        saved_base = self._base_tuple()
        try:
            self._set_state(state)
        except EditorError:
            self._restore_base(saved_base)
            raise

    def _set_state(self, state: dict):
        self._set_base(state.get("replace"))
        edits, ops = self._compose(state)
        coll, adds = self._compose_geometry(state)
        if (ops or coll) and self.can_restructure():
            raise EditorError(f"objects and collision cannot be changed here: {self.can_restructure()}")
        cur = self.original
        model = self.base_model if state.get("replace") else None
        if state.get("pos_scale"):
            try:
                model, _ = rescale_positions(self.base_model, float(state["pos_scale"]), self.key)
            except (ModelEditRefused, TypeError, ValueError) as exc:
                raise EditorError(f"the drawing range cannot be changed for this stage: {exc}") from None
        try:
            if edits:
                cur, _ = apply_edits(cur, edits, self.key)
            if coll:                                   # before objects: a grown grid can hold new objects
                cur, _ = apply_collision_ops(cur, coll, self.key)
            if ops:
                cur, _ = apply_object_ops(cur, ops, self.key)
            if adds:
                model, _ = edit_model(model if model is not None else self.base_model, adds, (), self.key,
                                      tex_source=self.rom_model, default_texels=DEFAULT_TEXELS_PER_UNIT)
        except (EditRefused, ObjectEditRefused, CollisionEditRefused) as exc:
            raise EditorError(str(exc)) from None
        except ModelEditRefused as exc:
            hint = " (Stage > Drawing range… can widen the area platforms may be drawn in)" \
                if "too far out" in str(exc) else ""
            raise EditorError(f"the platform cannot be drawn: {exc}{hint}") from None
        anim_ops = self._anim_ops_for(state)
        new_anim = None
        if anim_ops:
            if state.get("replace"):
                raise EditorError("a new stage has no moving parts to animate")
            self._load_anim()
            if self.rom_anim is None:
                raise EditorError(f"this stage has no animation{': ' + self.anim_error if self.anim_error else ''}")
            try:
                new_anim, _rep = edit_animation(self.rom_anim, anim_ops, self.key)
            except AnimEditRefused as exc:
                raise EditorError(f"the animation cannot be written: {exc}") from None
        doc = StageDocument(cur, self.key) if cur is not self.original else self.original_doc
        value = banana_value(doc)
        if value > MAX_BANANA_VALUE:
            raise EditorError(f"the stage's banana value would be {value}; the game counts it in one byte "
                              f"(at most {MAX_BANANA_VALUE}; a bunch counts 5)")
        old_order = self.order
        self._compute_order(state)
        for kind in OBJECT_KINDS:
            if len(self.order[kind]) != len(doc.pools[KIND_POOL[kind]]["records"]):
                self.order = old_order
                raise EditorError("internal: object order does not match the rebuilt stage")
        self.state = _copy_state(state)
        self.current, self.doc = cur, doc
        self._field_edits, self._ops = edits, ops
        self._collision_ops, self._model_adds, self.current_model = coll, adds, model
        self._faces_cache = None
        if new_anim != self.current_anim:
            self._anim_cur = None
            self._anim_cache = {k: v for k, v in self._anim_cache.items() if k[0] == "orig"}
        self.current_anim, self._anim_ops = new_anim, anim_ops

    # ---- what a plan / save contains
    def plan_edits(self) -> List[dict]:
        return list(self._field_edits)

    def plan_ops(self) -> List[dict]:
        return list(self._ops)

    def plan_collision(self) -> List[dict]:
        return [dict(op) for op in self._collision_ops]

    def plan_model(self) -> List[dict]:
        ops = []
        if self.state.get("pos_scale"):
            ops.append({"op": "set_pos_scale", "scale": float(self.state["pos_scale"]), "label": "drawing range"})
        return ops + [{"op": "add_polygons", "shape": shape, "polygons": polys, "label": "editor platforms"}
                      for shape, polys in self._model_adds.items()]

    @property
    def changed(self) -> bool:
        return bool(self._field_edits or self._ops or self.state.get("geometry") or self.state.get("replace")
                    or self.state.get("pos_scale") or self._anim_ops)

    # ---- geometry view for the GUI
    def geometry(self) -> List[dict]:
        out = []
        counts: Dict[str, int] = {}
        for g in self.state.get("geometry", []):
            t = geo_title(g)
            counts[t] = counts.get(t, 0) + 1
            o = _copy_geo(g)
            o.update({"kind": "geometry", "label": f"{t} {counts[t]}", "title": t, "deletable": True,
                      "shape": self.platform_shape if g["type"] in ("platform", "mesh") and g["visible"] else None,
                      "turned": geo_is_turned(g), "rotation_unlocked": self.project.rotation_unlocked})
            if g["type"] == "platform":
                o["size"] = (g["x"][1] - g["x"][0], g["thickness"], g["z"][1] - g["z"][0])
                if g["visible"] and g.get("texture"):
                    o["shape"] = g["texture"]
            elif g["type"] == "mesh":
                o["size"] = mesh_scale(g)
            if o["turned"]:
                bx, by, bz = geo_bounds(g)
                o["bounds"] = (bx, by, bz)
                o["polygons"] = geo_polygons(g)
                if g["type"] == "mesh":
                    o.update({"x": bx, "z": bz, "top": by[1], "thickness": by[1] - by[0],
                              "triangles": len(o["polygons"])})
            out.append(o)
        return out

    def collision_faces(self) -> List[tuple]:
        """(corners, normal, group) of every collision face as the stage now stands (rest pose)."""
        if self._faces_cache is None:
            try:
                groups = read_collision(self.current)
            except CollisionError:
                groups = []
            self._faces_cache = [(f["corners"], f["normal"], g) for g, col in enumerate(groups)
                                 for k in ("quad", "triangle") for f in col[k] if f["corners"]]
        return self._faces_cache

    def platform_floor(self, x: float, z: float, y_limit: float) -> Optional[float]:
        """Highest top of a drawn or solid platform under (x, z) that is not above y_limit."""
        best = None
        for g in self.state.get("geometry", []):
            if g["type"] == "remove":
                continue
            if not geo_is_turned(g):
                if g["x"][0] <= x <= g["x"][1] and g["z"][0] <= z <= g["z"][1] and g["top"] <= y_limit:
                    best = g["top"] if best is None else max(best, g["top"])
                continue
            for poly in geo_polygons(g):             # turned platforms and models: their upward faces
                y = _height_on(poly, x, z)
                if y is not None and y <= y_limit:
                    best = y if best is None else max(best, y)
        return best

    @property
    def banana_value(self) -> int:
        return banana_value(self.doc)

    # ---- object view for the GUI
    def objects(self) -> List[dict]:
        out = []
        kinds = self.group_kinds
        names = {g["index"]: g.get("name_text_04") for g in self.doc.groups}
        st = self.state
        moved = {oid for oid, pos in st["positions"].items()
                 if tuple(pos) != self.original_position(*_split_id(oid)[:2])}
        field_objs = set()
        for t in st["fields"]:
            if st["fields"][t] != self.original_raw(t):
                prefix, rest = t.split("[", 1)
                field_objs.add(f"{PREFIX_KIND[prefix]}:{int(rest.split(']')[0])}")
        new_number = {}
        for kind in OBJECT_KINDS:
            for n, a in enumerate([a for a in st["added"] if a["kind"] == kind]):
                new_number[a["id"]] = n + 1
        for kind in ("start",) + OBJECT_KINDS:
            src, pool, pfield, _t = KINDS[kind]
            recs = (self.doc.start or {}).get("records", []) if src == "start" \
                else self.doc.pools.get(pool, {}).get("records", [])
            ids = [f"start:{r['index']}" for r in recs] if kind == "start" else self.order[kind]
            for rec, oid in zip(recs, ids):
                _k, orig_i, new_id = _split_id(oid)
                grp = 0 if kind == "start" else rec["group"]
                is_new = new_id is not None
                label = (f"New {KIND_TITLE[kind].lower()} {new_number.get(new_id, '?')}" if is_new
                         else f"{KIND_TITLE[kind]} {orig_i + 1}")
                o = {"id": oid, "kind": kind, "index": orig_i, "label": label, "new": is_new, "group": grp,
                     "group_name": names.get(grp), "group_kind": kinds.get(grp), "in_moving_group": bool(grp),
                     "position": list(rec[pfield]), "editable": {"position"}, "heading": None,
                     "offset": rec["offset"], "collision_warning": None,
                     "changed": is_new or oid in moved or oid in field_objs,
                     "deletable": kind != "start" and not self.can_restructure()}
                if kind == "start":
                    o["heading"] = angle_to_degrees(rec["u32_0C"])
                    o["editable"].add("heading")
                elif kind == "goal":
                    o["heading"] = goal_heading(rec["matrix"])
                    o["matrix"] = rec["matrix"]
                    o["tilt"], _f, o["rotate"] = object_turns(rec["matrix"])
                    o["size"] = object_scale(rec["matrix"])
                    if self.project.scale_unlocked:
                        o["editable"].add("size")
                    o["red"] = rec["u32_30"] == 1
                    o["editable"].add("red")
                    base_m = IDENTITY_ROWS if is_new else self._record("goal", orig_i)["matrix"]
                    if o["heading"] is not None and goal_heading(base_m) is not None:
                        o["editable"].add("heading")
                    if self.project.rotation_unlocked:
                        o["editable"].add("rotation")
                elif kind == "bumper":
                    o["matrix"] = rec["matrix"]
                    o["tilt"], o["heading"], o["rotate"] = object_turns(rec["matrix"])
                    o["size"] = object_scale(rec["matrix"])
                    if self.project.rotation_unlocked:
                        o["editable"].update(("rotation", "heading"))
                    if self.project.scale_unlocked:
                        o["editable"].add("size")
                elif kind == "banana":
                    o["bunch"] = rec["u32_0C"] == 1
                    o["editable"].add("bunch")
                out.append(o)
        return out

    def _current_rows(self, state: dict, kind: str, i: Optional[int], new_id: Optional[str]):
        """A goal's or bumper's matrix rows in `state`."""
        if new_id:
            raw = self._added(state, new_id).get("matrix")
            return _rows_of(raw) if raw else IDENTITY_ROWS
        raw = state["fields"].get(f"{TARGET_PREFIX[kind]}[{i}].matrix")
        return _rows_of(raw) if raw else self._record(kind, i)["matrix"]

    def _set_matrix(self, st: dict, kind: str, i: Optional[int], new_id: Optional[str], rows) -> dict:
        raw = tuple(_q(v) for row in rows for v in row)
        if new_id:
            self._added(st, new_id)["matrix"] = raw
        else:
            t = f"{TARGET_PREFIX[kind]}[{i}].matrix"
            if raw == self.original_raw(t):
                st["fields"].pop(t, None)
            else:
                st["fields"][t] = raw
        return st

    # ---- state changes (pure: return a new state)
    def _added(self, state: dict, new_id: str) -> dict:
        for a in state["added"]:
            if a["id"] == new_id:
                return a
        raise EditorError(f"{new_id} does not exist")

    def mutate(self, state: dict, obj_id: str, prop: str, value) -> dict:
        kind, i, new_id = _split_id(obj_id)
        st = _copy_state(state)
        if obj_id in st["deleted"]:
            raise EditorError(f"{obj_id} was deleted")
        if prop == "position":
            vals = tuple(_q(float(v)) for v in value) if isinstance(value, (list, tuple)) else ()
            if len(vals) != 3:
                raise EditorError("position needs x, y, z")
            if new_id:
                self._added(st, new_id)["pos"] = vals
            elif kind == "start":
                st["fields"][f"start[{i}].position"] = vals
            elif vals == self.original_position(kind, i):
                st["positions"].pop(obj_id, None)
            else:
                st["positions"][obj_id] = vals
            return st
        if prop == "heading" and kind == "start":
            t = f"start[{i}].u32_0C"
            st["fields"][t] = (degrees_to_angle(float(value), self.original_raw(t)[0]),)
            return st
        if prop == "heading" and kind == "goal":
            base_m = IDENTITY_ROWS if new_id else self._record("goal", i)["matrix"]
            if new_id and self._added(st, new_id).get("matrix"):
                base_m = _rows_of(self._added(st, new_id)["matrix"])
            elif not new_id and st["fields"].get(f"pool_A[{i}].matrix"):
                base_m = _rows_of(st["fields"][f"pool_A[{i}].matrix"])
            h0 = goal_heading(base_m)
            if h0 is None:
                raise EditorError("this goal points straight up or down; its facing cannot be edited")
            d = (float(value) - h0) % 360.0
            same = abs(d) < 1e-9 or abs(d - 360.0) < 1e-9
            rows = base_m if same else rotate_matrix(base_m, d)
            raw = tuple(_q(v) for row in rows for v in row)
            if new_id:
                self._added(st, new_id)["matrix"] = raw
            else:
                st["fields"][f"pool_A[{i}].matrix"] = raw
            return st
        if prop in ("heading", "rotation") and kind in ("goal", "bumper"):
            if not self.project.rotation_unlocked:
                raise EditorError("tilting and rolling goals and bumpers (and turning bumpers) is checked in-game "
                                  "by edit test 9 first")
            if prop == "heading":
                cur = self._current_rows(st, kind, i, new_id)
                t, _f, r = object_turns(cur)
                value = (t, float(value), r)
            try:
                tilt, facing, rotate = (float(v) for v in value)
            except (TypeError, ValueError):
                raise EditorError("a rotation needs tilt, facing and rotate in degrees") from None
            size = object_scale(self._current_rows(st, kind, i, new_id))
            return self._set_matrix(st, kind, i, new_id, object_rows(tilt, facing, rotate, size))
        if prop == "size" and kind in ("goal", "bumper"):
            if not self.project.scale_unlocked:
                raise EditorError(SIZE_LOCKED)
            size = parse_size(value)
            cur = _unit_rows(self._current_rows(st, kind, i, new_id))
            rows = cur if size == (1.0, 1.0, 1.0) else tuple(tuple(v * k for v in row) for row, k in zip(cur, size))
            return self._set_matrix(st, kind, i, new_id, rows)
        if prop == "size":
            raise EditorError("the start stores only a position and a facing" if kind == "start" else
                              "bananas store only a position and whether they are a bunch")
        if (kind, prop) in FLAG_TARGET:
            if new_id:
                self._added(st, new_id)[prop] = bool(value)
            else:
                st["fields"][f"{TARGET_PREFIX[kind]}[{i}].{FLAG_TARGET[(kind, prop)]}"] = (1 if value else 0,)
            return st
        raise EditorError(f"{prop} of a {kind} cannot be edited")


def banana_value(doc: StageDocument) -> int:
    return sum(5 if r["u32_0C"] == 1 else 1 if r["u32_0C"] == 0 else 0 for r in doc.pools["C"]["records"])


# --------------------------------------------------------------------------- project

class Project:
    def __init__(self, rom_path: str, rom: Optional[NdsRom] = None):
        """rom_path: the ROM file (opened read-only). rom: an already loaded NdsRom for that file (tests)."""
        self.rom_path = os.path.abspath(rom_path)
        try:
            self.rom = rom if rom is not None else NdsRom.from_file(self.rom_path)
        except (OSError, RomError) as exc:
            raise EditorError(f"cannot open ROM: {exc}") from None
        self.rom_sha1 = hashlib.sha1(self.rom.data).hexdigest()
        if not self.rom.profile():
            raise EditorError("this ROM is not a supported Touch & Roll version (USA rev 0 expected)")
        self.opened_editor_output = _editor_output_record(self.rom_path, self.rom_sha1)
        try:
            sm = self.rom.stage_map()
        except RomError as exc:
            raise EditorError(f"stage map could not be read: {exc}") from None
        self.worlds = sm["worlds"]
        self.entries: Dict[str, dict] = {}
        for w in sm["worlds"]:
            for s in w["stages"]:
                label = f"{w['world']}-{s['stage']} {s['title'] or s['stage_key']}"
                self.entries.setdefault(s["stage_key"], dict(s, label=label, world_title=w["title"]))
        names = set(self.rom.stage_names())
        self.other_keys = []
        for k in sm["stage_files_not_in_main_game"]:
            self.entries[k] = {"stage_key": k, "label": k, "world": None, "stage": None, "title": None,
                               "loadable": k in names}
            self.other_keys.append(k)
        self.stages: Dict[str, StageState] = {}
        self.undo_stack: List[dict] = []
        self.redo_stack: List[dict] = []
        self._next_action = 1
        self._next_new = 1
        self.saved_action: Optional[int] = None       # id of the newest action included in the last save
        self.output_path: Optional[str] = None
        self.title = "Touch & Roll editor edits"
        self.names = {"stages": {}, "worlds": {}}     # only the renamed ones: {"1-1": "NAME"}, {"1": "Name"}
        self._names_table = None
        self._names_error: Optional[str] = None
        self.allow_untested = False                   # True only in edit-test builders (not in the editor)

    @property
    def rotation_unlocked(self) -> bool:
        """Tilt / roll of goals and bumpers, turned platforms and imported models (edit test 9)."""
        return ROTATION_TEST9_PASSED or self.allow_untested

    @property
    def scale_unlocked(self) -> bool:
        """Size X / Y / Z of goals and bumpers: locked, because the game ignores the size for the finish line and
        the bounce (only edit-test builders may write one)."""
        return SIZES_WORK_IN_GAME or self.allow_untested

    @property
    def textures_unlocked(self) -> bool:
        """Drawing platforms and models with the stage's other textures, and with a model's own texture
        coordinates (edit test 10)."""
        return TEXTURE_TEST10_PASSED or self.allow_untested

    # ---- world and stage names
    @property
    def names_table(self):
        """The ROM's name tables (trcore.names), or None when names cannot be edited (see names_error)."""
        if self._names_table is None and self._names_error is None:
            try:
                self._names_table = read_names(self.rom)
            except NamesError as exc:
                self._names_error = str(exc)
                if self.opened_editor_output:
                    self._names_error = ("this is a copy whose names were already edited; open the original ROM "
                                         "(the editor offers it when you open the copy) to rename")
        return self._names_table

    @property
    def names_error(self) -> Optional[str]:
        _ = self.names_table
        return self._names_error

    def original_world_name(self, world: int) -> str:
        t = self.names_table
        return t.worlds[world - 1][0] if t else (self.worlds[world - 1].get("title") or f"World {world}")

    def original_stage_name(self, world: int, stage: int) -> str:
        t = self.names_table
        if t:
            return t.stages[(world - 1) * 10 + stage - 1]
        return (self.worlds[world - 1]["stages"][stage - 1].get("title") or "").upper()

    def world_name(self, world: int) -> str:
        return self.names["worlds"].get(str(world)) or self.original_world_name(world)

    def stage_name(self, world: int, stage: int) -> str:
        return self.names["stages"].get(f"{world}-{stage}") or self.original_stage_name(world, stage)

    @staticmethod
    def name_problem(kind: str, name: str) -> Optional[str]:
        """Why a stage or world name cannot be used, or None."""
        chars, mx = (STAGE_NAME_CHARS, STAGE_NAME_MAX) if kind == "stage" else (WORLD_NAME_CHARS, WORLD_NAME_MAX)
        if not name or not name.strip():
            return "the name must not be empty"
        if len(name) > mx:
            return f"at most {mx} characters"
        bad = "".join(sorted({c for c in name if c not in chars}))
        if bad:
            if kind == "stage":
                return (f"{bad!r} cannot be used: stage names may use capitals, digits, spaces and "
                        "- ? ! ' : ( # ) . + = & / , * ; @ < > (the game shows them with a capitals-only font "
                        "when the stage starts)")
            return f"{bad!r} cannot be used: letters, digits, spaces and ! \" ' ( ) + , - . / ? only"
        return None

    @staticmethod
    def name_warning(kind: str, name: str) -> Optional[str]:
        limit = STAGE_NAME_WARN if kind == "stage" else WORLD_NAME_WARN
        if name and len(name) > limit:
            return f"longer than any name in the game ({limit} characters); it may not fit on every screen"
        return None

    def check_names(self, names: dict) -> dict:
        """Normalise and check {"stages": {...}, "worlds": {...}}; returns (normalised names, space report).
        Names equal to the original are dropped. Raises EditorError."""
        if self.names_table is None:
            raise EditorError(f"names cannot be edited here: {self.names_error}")
        out = {"stages": {}, "worlds": {}}
        for key, name in (names.get("stages") or {}).items():
            try:
                w, st = (int(x) for x in str(key).split("-"))
            except ValueError:
                raise EditorError(f"stage {key!r}: use 'world-stage'") from None
            if not (1 <= w <= 12 and 1 <= st <= 10):
                raise EditorError(f"stage {key} does not exist")
            why = self.name_problem("stage", name)
            if why:
                raise EditorError(f"stage {key}: {why}")
            if name != self.original_stage_name(w, st):
                out["stages"][f"{w}-{st}"] = name
        for key, name in (names.get("worlds") or {}).items():
            w = int(key)
            if not 1 <= w <= 12:
                raise EditorError(f"world {key} does not exist")
            why = self.name_problem("world", name)
            if why:
                raise EditorError(f"world {key}: {why}")
            if name != self.original_world_name(w):
                out["worlds"][str(w)] = name
        try:
            _arm9, report, _allowed = apply_names(self.names_table, out)
        except NamesError as exc:
            raise EditorError(str(exc)) from None
        return out, report

    def names_space(self, names: Optional[dict] = None) -> dict:
        """Bytes used / available for the stage names and the world names (for the space meters)."""
        t = self.names_table
        if t is None:
            return {}
        n = names if names is not None else self.names
        stages = [n.get("stages", {}).get(t.stage_key(i), s) for i, s in enumerate(t.stages)]
        worlds = {n.get("worlds", {}).get(str(w + 1), t.worlds[w][0]) for w in range(12)}
        return {"stage_used": sum(len(x.encode()) + 1 for x in stages), "stage_bytes": t.stage_capacity_bytes(),
                "world_used": sum((len(x.encode()) + 4) // 4 * 4 for x in worlds), "world_bytes": t.world_pool_bytes()}

    def set_names(self, names: dict, label: str = "rename") -> bool:
        new, _report = self.check_names(names)
        if new == self.names:
            return False
        self.undo_stack.append({"id": self._next_action, "key": None, "names_before": _copy_names(self.names),
                                "names_after": _copy_names(new), "label": label})
        self._next_action += 1
        self.redo_stack.clear()
        self.names = new
        return True

    @property
    def names_changed(self) -> bool:
        return bool(self.names["stages"] or self.names["worlds"])

    def has_edits(self) -> bool:
        return bool(self.changed_stages()) or self.names_changed

    def stage_label(self, key: str) -> str:
        e = self.entries[key]
        lab = e["label"]
        if e.get("world"):
            k = f"{e['world']}-{e['stage']}"
            if k in self.names["stages"]:
                lab = f"{k} {self.names['stages'][k]}"
        st = self.stages.get(key)
        if st is not None and st.state.get("replace"):
            lab += "  [new stage]"
        return lab

    # ---- stages
    def stage(self, key: str) -> StageState:
        if key not in self.stages:
            if key not in self.entries:
                raise EditorError(f"unknown stage {key}")
            self.stages[key] = StageState(self, self.entries[key])
        return self.stages[key]

    # ---- changes with undo
    def _commit(self, key: str, new_state: dict, label: str) -> bool:
        st = self.stage(key)
        if new_state == st.state:
            return False
        before = _copy_state(st.state)
        st.set_state(new_state)                  # raises EditorError and leaves the stage unchanged
        self.undo_stack.append({"id": self._next_action, "key": key, "before": before,
                                "after": _copy_state(st.state), "label": label})
        self._next_action += 1
        self.redo_stack.clear()
        return True

    def change(self, key: str, obj_id: str, prop: str, value, label: Optional[str] = None) -> bool:
        st = self.stage(key)
        if obj_id.startswith("geo:"):
            return self._commit(key, self._mutate_geometry(st.state, obj_id, prop, value),
                                label or f"{prop} of {obj_id}")
        return self._commit(key, st.mutate(st.state, obj_id, prop, value), label or f"{prop} of {obj_id}")

    # ---- Create New Stage / Restore original / Drawing range
    def replace_with_new_stage(self, key: str, platform: Optional[dict] = None, start: Optional[dict] = None,
                               keep_edits: bool = False) -> bool:
        """Replace the stage by a new one: one platform and the start (trcore.newstage). keep_edits=True only
        changes the platform of a stage that is already new (objects and geometry stay); otherwise every edit
        of the stage is dropped. The drawing range setting is kept. The original is untouched in the ROM, so
        restore_original_stage() brings it back in the next save."""
        st = self.stage(key)
        if self.entries[key].get("world") is None:
            raise EditorError("only stages of the 12 worlds can be replaced by a new stage")
        params = new_stage_params(platform, start)
        if keep_edits and st.state.get("replace"):
            new = _copy_state(st.state)
        else:
            new = _empty_state()
            new["pos_scale"] = st.state.get("pos_scale")
        new["replace"] = params
        return self._commit(key, new, "edit the new stage's platform" if keep_edits else "create a new stage")

    def restore_original_stage(self, key: str) -> bool:
        st = self.stage(key)
        if not st.state.get("replace"):
            return False
        new = _empty_state()
        new["pos_scale"] = st.state.get("pos_scale")
        return self._commit(key, new, "restore the original stage")

    def set_drawing_range(self, key: str, scale: Optional[float]) -> bool:
        """Model position scale for the stage (drawing range = +-8 x scale); None = the model's own."""
        st = self.stage(key)
        new = _copy_state(st.state)
        orig = st.original_pos_scale
        new["pos_scale"] = None if scale is None or (orig is not None and abs(float(scale) - orig) < 1e-9) \
            else float(scale)
        return self._commit(key, new, "change the drawing range")

    # ---- keyframes
    def set_anim_keys(self, key: str, node: int, ch: str, keys: Optional[List[dict]],
                      allow_untested: bool = False) -> bool:
        """Keys for one channel of a moving part (None: back to the original motion). `allow_untested` was
        used by edit test 8's builder before the test passed; everything it covered is confirmed now."""
        st = self.stage(key)
        if ch not in CHANNELS:
            raise EditorError(f"unknown channel {ch!r}")
        ok, why = st.channel_editable(node, ch)
        if keys and not ok and not (allow_untested and st.anim_parts() and not st.is_new_stage):
            raise EditorError(why)                 # removing keys (back to the original motion) is always allowed
        new = _copy_state(st.state)
        a = new.get("anim") or {"frames": None, "keys": {}}
        frames = st.frames or 0
        if keys:
            try:
                keys = check_keys(keys, frames, f"{ch}")
            except KeyframeError as exc:
                raise EditorError(str(exc)) from None
            a["keys"][f"{node}:{ch}"] = keys
        else:
            a["keys"].pop(f"{node}:{ch}", None)
        new["anim"] = a if (a.get("keys") or a.get("frames")) else None
        return self._commit(key, new, f"keys of {ch}" if keys else f"reset {ch}")

    def set_anim_length(self, key: str, frames: Optional[int], allow_untested: bool = False) -> bool:
        """New animation length in frames (None: the original). Everything is stretched to fit; keys move
        with it."""
        st = self.stage(key)
        ok, why = st.length_editable()
        if not ok and not (allow_untested and st.anim_parts() and not st.is_new_stage):
            raise EditorError(why)
        orig = st.original_frames
        cur = st.frames
        target = orig if frames is None else int(frames)
        if not 2 <= target <= MAX_FRAMES:
            raise EditorError(f"an animation has 2 to {MAX_FRAMES} frames ({MAX_FRAMES / 60:.0f} s)")
        new = _copy_state(st.state)
        a = new.get("anim") or {"frames": None, "keys": {}}
        if target != cur:
            for k, keys in a["keys"].items():
                moved = []
                for x in keys:
                    f = int(round(x["frame"] * (target - 1) / (cur - 1)))
                    if moved and moved[-1]["frame"] == f:
                        raise EditorError(f"two keys of {k.split(':')[1]} would land on frame {f}; delete one first")
                    moved.append(dict(x, frame=f))
                a["keys"][k] = moved
        a["frames"] = None if target == orig else target
        new["anim"] = a if (a.get("keys") or a.get("frames")) else None
        return self._commit(key, new, "change the animation's length")

    def keys_from_original(self, key: str, node: int, ch: str) -> List[dict]:
        """Keys that reproduce the channel's current motion closely (for 'Edit with keys')."""
        from trcore.keyframes import keys_from_samples
        st = self.stage(key)
        vals = st.channel_values(node, ch)
        return keys_from_samples(vals, unit=1 / FX if ch[0] == "t" else 0.05)

    # ---- geometry (platforms, invisible collision, decorations, no-collision areas)
    @staticmethod
    def _geo_index(state: dict, gid: str) -> int:
        for i, g in enumerate(state.get("geometry", [])):
            if g["id"] == gid:
                return i
        raise EditorError(f"{gid} does not exist")

    def _mutate_geometry(self, state: dict, gid: str, prop: str, value) -> dict:
        st = _copy_state(state)
        i = self._geo_index(st, gid)
        g = st["geometry"][i]
        if (g["type"] == "mesh" or (prop == "rot" and value and any(abs(float(a)) > 1e-9 for a in value))) \
                and not self.rotation_unlocked:
            raise EditorError("turned platforms and imported models are checked in-game by edit test 9 first")
        if not self.textures_unlocked and ((prop == "texture" and (value[1] if isinstance(value, (list, tuple))
                                                                  else value))
                                           or (prop == "uv_mode" and value == "uv")):
            raise EditorError("the stage's other textures and a model's own texture coordinates are checked "
                              "in-game by edit test 10 first")
        if prop == "rot":
            if g["type"] == "remove":
                raise EditorError("a no-collision area cannot be turned")
            g["rot"] = [float(a) for a in value] if value else None
            if g["type"] == "mesh" and g["rot"] is None:
                g["rot"] = [0.0, 0.0, 0.0]
            st["geometry"][i] = check_geo(g)
            return st
        if g["type"] == "mesh":
            if prop == "move":
                g["pos"] = [g["pos"][k] + float(value[k]) for k in range(3)]
            elif prop == "pos":
                g["pos"] = [float(v) for v in value]
            elif prop == "scale":
                g["scale"] = [float(v) for v in value] if isinstance(value, (list, tuple)) else float(value)
            elif prop in ("visible", "solid", "both_sides"):
                g[prop] = bool(value)
            elif prop == "texture":
                slot, name = (0, value) if not isinstance(value, (list, tuple)) else (int(value[0]), value[1])
                n = max(1, len(g.get("materials") or [None]))
                if not 0 <= slot < n:
                    raise EditorError("the model has no such material")
                tex = list(g.get("textures") or [None] * n)
                tex[slot] = name or None
                g["textures"] = tex
            elif prop == "uv_mode":
                if value == "uv" and not g.get("uvs"):
                    raise EditorError("this model has no texture coordinates of its own")
                g["uv_mode"] = value
            else:
                raise EditorError(f"{prop} of an imported model cannot be edited")
            st["geometry"][i] = check_geo(g)
            return st
        if prop in ("x", "z", "y"):
            g[prop] = None if value is None else [float(v) for v in value]
        elif prop in ("top", "thickness"):
            if g["type"] != "platform":
                raise EditorError(f"{prop} applies to platforms only")
            g[prop] = float(value)
        elif prop in ("visible", "solid"):
            if g["type"] != "platform":
                raise EditorError(f"{prop} applies to platforms only")
            g[prop] = bool(value)
        elif prop == "texture":
            if g["type"] != "platform":
                raise EditorError("a no-collision area has no texture")
            g["texture"] = value or None
        elif prop == "size":
            try:
                w, h, d = (float(v) for v in value)
            except (TypeError, ValueError):
                raise EditorError("a size needs x, y and z") from None
            if min(w, d) < MIN_GEO_SIZE - 1e-9:
                raise EditorError(f"the size along x and z must be at least {MIN_GEO_SIZE}")
            cx, cz = (g["x"][0] + g["x"][1]) / 2, (g["z"][0] + g["z"][1]) / 2
            g["x"], g["z"] = [cx - w / 2, cx + w / 2], [cz - d / 2, cz + d / 2]
            if g["type"] == "platform":
                if h < 0:
                    raise EditorError("the thickness cannot be negative")
                g["thickness"] = h
            elif g.get("y"):
                cy = (g["y"][0] + g["y"][1]) / 2
                g["y"] = [cy - h / 2, cy + h / 2]
        elif prop == "move":
            dx, dy, dz = (float(v) for v in value)
            g["x"] = [g["x"][0] + dx, g["x"][1] + dx]
            g["z"] = [g["z"][0] + dz, g["z"][1] + dz]
            if g["type"] == "platform":
                g["top"] = g["top"] + dy
            elif g.get("y"):
                g["y"] = [g["y"][0] + dy, g["y"][1] + dy]
        else:
            raise EditorError(f"{prop} of a geometry item cannot be edited")
        st["geometry"][i] = check_geo(g)
        return st

    def add_geometry(self, key: str, kind: str, x, z, top: Optional[float] = None,
                     thickness: Optional[float] = None, y=None, texture: Optional[str] = None) -> str:
        """kind: platform | invisible | decoration | remove. texture: a shape of StageState.texture_choices
        for drawn kinds (None: the stage's main texture). Returns the new item's id."""
        if kind not in GEO_KINDS:
            raise EditorError(f"unknown geometry kind {kind!r}")
        st = self.stage(key)
        why = st.can_restructure()
        if why:
            raise EditorError(f"collision cannot be changed here: {why}")
        vis, solid, th, _t = GEO_KINDS[kind]
        gid = f"geo:n{self._next_new}"
        if kind == "remove":
            g = {"id": gid, "type": "remove", "x": list(x), "z": list(z), "y": list(y) if y else None}
        else:
            g = {"id": gid, "type": "platform", "x": list(x), "z": list(z), "top": 0.0 if top is None else top,
                 "thickness": th if thickness is None else thickness, "visible": vis, "solid": solid}
            if texture and vis:
                if not self.textures_unlocked:
                    raise EditorError("the stage's other textures are checked in-game by edit test 10 first")
                st._texture_shape(texture)              # refuses a texture the stage does not have
                g["texture"] = texture
        state = _copy_state(st.state)
        state["geometry"].append(check_geo(g))
        self._commit(key, state, f"add {GEO_KINDS[kind][3].lower()}")
        self._next_new += 1
        return gid

    def add_model(self, key: str, mesh: dict, position, scale: float = 1.0, rotation=(0.0, 0.0, 0.0),
                  visible: bool = True, solid: bool = True, both_sides: bool = False) -> str:
        """Add an imported model (tredit.objimport.read_obj's result) to a new stage; returns its id."""
        st = self.stage(key)
        if not self.rotation_unlocked:
            raise EditorError("imported models are checked in-game by edit test 9 first")
        if not st.is_new_stage:
            raise EditorError("models can be imported into new stages only (Stage > Create new stage here…)")
        why = st.can_restructure()
        if why:
            raise EditorError(f"collision cannot be changed here: {why}")
        gid = f"geo:n{self._next_new}"
        g = {"id": gid, "type": "mesh", "name": mesh.get("name"), "verts": mesh["verts"], "faces": mesh["faces"],
             "pos": list(position), "scale": scale, "rot": list(rotation), "visible": visible, "solid": solid,
             "both_sides": both_sides}
        state = _copy_state(st.state)
        state["geometry"].append(check_geo(g))
        self._commit(key, state, f"import {mesh.get('name') or 'model'}")
        self._next_new += 1
        return gid

    def add_models(self, key: str, parsed: dict, position, scale=1.0, rotation=(0.0, 0.0, 0.0),
                   visible: bool = True, solid: bool = True, both_sides: bool = False,
                   textures: Optional[Dict[str, Optional[str]]] = None, use_uvs: bool = True,
                   only: Optional[Sequence[int]] = None) -> List[str]:
        """Import every object of an OBJ file (tredit.objimport.read_obj's result) as its own model piece,
        laid out as in the file: the file's bottom middle goes to `position`, the whole file is scaled
        (one number or x, y, z) and turned as one, and each piece keeps that size and turn so it can be
        edited on its own afterwards. textures: {material name: stage shape or None}. use_uvs: draw with the
        file's texture coordinates where it has them. only: indices of the objects to import (default all).
        One undo step. Returns the new pieces' ids."""
        st = self.stage(key)
        if not self.rotation_unlocked:
            raise EditorError("imported models are checked in-game by edit test 9 first")
        if not st.is_new_stage:
            raise EditorError("models can be imported into new stages only (Stage > Create new stage here…)")
        why = st.can_restructure()
        if why:
            raise EditorError(f"collision cannot be changed here: {why}")
        objs = parsed.get("objects") or [dict(parsed, origin=(0.0, 0.0, 0.0))]
        pick = list(range(len(objs))) if only is None else [int(k) for k in only]
        if not pick or any(not 0 <= k < len(objs) for k in pick):
            raise EditorError("choose at least one object of the file")
        textures = dict(textures or {})
        if not self.textures_unlocked:
            if any(textures.values()):
                raise EditorError("the stage's other textures are checked in-game by edit test 10 first")
            use_uvs = False
        n = sum(len(objs[k]["faces"]) for k in pick) * (2 if both_sides else 1)
        if n > MESH_TRIANGLE_LIMIT:
            raise EditorError(f"these objects have {n} triangles; at most {MESH_TRIANGLE_LIMIT} can be imported at "
                              "once")
        sc = [float(v) for v in scale] if isinstance(scale, (list, tuple)) else [float(scale)] * 3
        rows = turn_rows(*(float(a) for a in rotation))
        px, py, pz = (float(v) for v in position)
        state = _copy_state(st.state)
        ids = []
        for k in pick:
            o = objs[k]
            ox, oy, oz = (o["origin"][j] * sc[j] for j in range(3))
            at = [(px, py, pz)[j] + ox * rows[0][j] + oy * rows[1][j] + oz * rows[2][j] for j in range(3)]
            gid = f"geo:n{self._next_new + len(ids)}"
            g = {"id": gid, "type": "mesh", "name": o.get("name"), "source": parsed.get("name"),
                 "verts": o["verts"], "faces": o["faces"],
                 "pos": at, "scale": sc if len(set(sc)) > 1 else sc[0], "rot": list(rotation), "visible": visible,
                 "solid": solid, "both_sides": both_sides}
            if o.get("materials"):
                g["materials"], g["fmat"] = list(o["materials"]), list(o["fmat"])
                g["textures"] = [textures.get(m) for m in o["materials"]]
            if o.get("uvs"):
                g["uvs"], g["fuv"] = o["uvs"], o["fuv"]
                g["uv_mode"] = "uv" if use_uvs else "projected"
            state["geometry"].append(check_geo(g))
            ids.append(gid)
        name = parsed.get("name") or "model"
        self._commit(key, state, f"import {name}" + (f" ({len(ids)} objects)" if len(ids) > 1 else ""))
        self._next_new += len(ids)
        return ids

    def stage_budget(self, key: str) -> dict:
        """Triangles drawn and collision faces of a stage as it now stands, with the game's largest."""
        st = self.stage(key)
        model = st.current_model
        tris = 0
        if model:
            try:
                tris = len(model_geometry(Nsbmd(model, key), 0)["triangles"])
            except NitroError:
                tris = 0
        faces = len(st.collision_faces())
        return {"triangles": tris, "faces": faces, "largest_triangles": LARGEST_GAME_MODEL_TRIANGLES,
                "largest_faces": LARGEST_GAME_COLLISION_FACES}

    def add_object(self, key: str, kind: str, position, bunch: bool = False, red: bool = False,
                   heading: Optional[float] = None, group: int = 0, rows=None) -> str:
        """Add a goal, bumper or banana (to the stage's fixed part); returns the new object's id.
        rows: a goal's or bumper's whole matrix (turn and size, e.g. of the object it duplicates)."""
        if kind not in OBJECT_KINDS:
            raise EditorError(f"cannot add a {kind}")
        st = self.stage(key)
        why = st.can_restructure()
        if why:
            raise EditorError(f"objects cannot be added here: {why}")
        new_id = f"{kind}:n{self._next_new}"
        pos = tuple(_q(float(v)) for v in position)
        if len(pos) != 3:
            raise EditorError("position needs x, y, z")
        a = {"id": new_id, "kind": kind, "group": group, "pos": pos, "bunch": bool(bunch), "red": bool(red)}
        if rows is not None and kind in ("goal", "bumper"):
            a["matrix"] = tuple(_q(float(v)) for row in rows for v in row)
        elif kind == "goal":
            rows = IDENTITY_ROWS
            if heading is not None:
                d = (float(heading) - 180.0) % 360.0
                rows = rows if abs(d) < 1e-9 else rotate_matrix(IDENTITY_ROWS, d)
            a["matrix"] = tuple(_q(v) for row in rows for v in row)
        state = _copy_state(st.state)
        state["added"].append(a)
        self._commit(key, state, f"add {kind}")
        self._next_new += 1
        return new_id

    def default_heading_towards_start(self, key: str, position) -> Optional[float]:
        """Facing that points a new goal's front at the stage's first start."""
        starts = (self.stage(key).doc.start or {}).get("records", [])
        if not starts:
            return None
        sx, _sy, sz = starts[0]["position"]
        dx, dz = sx - position[0], sz - position[2]
        if math.hypot(dx, dz) < 1e-6:
            return None
        return math.degrees(math.atan2(-dx, -dz)) % 360.0

    def delete_object(self, key: str, obj_id: str) -> bool:
        if obj_id.startswith("geo:"):
            st = self.stage(key)
            state = _copy_state(st.state)
            state["geometry"].pop(self._geo_index(state, obj_id))
            return self._commit(key, state, f"delete {obj_id}")
        kind, i, new_id = _split_id(obj_id)
        if kind == "start":
            raise EditorError("starts cannot be deleted")
        st = self.stage(key)
        why = st.can_restructure()
        if why:
            raise EditorError(f"objects cannot be deleted here: {why}")
        state = _copy_state(st.state)
        if new_id:
            state["added"] = [a for a in state["added"] if a["id"] != new_id]
        else:
            if obj_id in state["deleted"]:
                return False
            state["deleted"].append(obj_id)
            state["positions"].pop(obj_id, None)
            prefix = f"{TARGET_PREFIX[kind]}[{i}]."
            state["fields"] = {t: v for t, v in state["fields"].items() if not t.startswith(prefix)}
        return self._commit(key, state, f"delete {obj_id}")

    def duplicate_object(self, key: str, obj_id: str, offset=(1.0, 0.0, 0.0)) -> str:
        if obj_id.startswith("geo:"):
            st = self.stage(key)
            state = _copy_state(st.state)
            g = _copy_geo(state["geometry"][self._geo_index(state, obj_id)])
            g["id"] = gid = f"geo:n{self._next_new}"
            if g["type"] == "mesh":                      # a model piece: beside the original, same everything
                bx = geo_bounds(g)[0]
                g["pos"] = [g["pos"][0] + (bx[1] - bx[0]) + offset[0], g["pos"][1], g["pos"][2]]
            else:
                w = g["x"][1] - g["x"][0]
                g["x"] = [g["x"][0] + w + offset[0], g["x"][1] + w + offset[0]]
            state["geometry"].append(check_geo(g))
            self._commit(key, state, f"duplicate {obj_id}")
            self._next_new += 1
            return gid
        o = {x["id"]: x for x in self.stage(key).objects()}.get(obj_id)
        if not o or o["kind"] == "start":
            raise EditorError("select a goal, bumper or banana to duplicate")
        pos = [o["position"][k] + offset[k] for k in range(3)]
        rows = o.get("matrix")
        if rows and not self.scale_unlocked and object_scale(rows) != (1.0, 1.0, 1.0):
            rows = _unit_rows(rows)       # a copy goes into the fixed part, where a size would break the goal line
        return self.add_object(key, o["kind"], pos, bunch=bool(o.get("bunch")), red=bool(o.get("red")),
                               heading=o.get("heading"), rows=rows)

    def reset_object(self, key: str, obj_id: str) -> bool:
        """Undo every change of one ORIGINAL object (position, flags, facing, deletion)."""
        if obj_id.startswith("geo:"):
            raise EditorError("added geometry has no original to go back to; delete it instead")
        kind, i, new_id = _split_id(obj_id)
        if new_id:
            raise EditorError("a new object has no original to go back to; delete it instead")
        st = self.stage(key)
        state = _copy_state(st.state)
        prefix = f"{TARGET_PREFIX[kind]}[{i}]."
        state["fields"] = {t: v for t, v in state["fields"].items() if not t.startswith(prefix)}
        state["positions"].pop(obj_id, None)
        state["deleted"] = [d for d in state["deleted"] if d != obj_id]
        return self._commit(key, state, f"reset {obj_id}")

    def _restore(self, key, state):
        self.stage(key).set_state(state)

    def undo(self) -> Optional[dict]:
        if not self.undo_stack:
            return None
        a = self.undo_stack.pop()
        if "names_before" in a:
            self.names = _copy_names(a["names_before"])
        else:
            self._restore(a["key"], a["before"])
        self.redo_stack.append(a)
        return a

    def redo(self) -> Optional[dict]:
        if not self.redo_stack:
            return None
        a = self.redo_stack.pop()
        if "names_after" in a:
            self.names = _copy_names(a["names_after"])
        else:
            self._restore(a["key"], a["after"])
        self.undo_stack.append(a)
        return a

    def changed_stages(self) -> List[str]:
        return sorted(k for k, st in self.stages.items() if st.changed)

    @property
    def edit_count(self) -> int:
        return sum(len(st.plan_edits()) + len(st.plan_ops()) + len(st.state.get("geometry", []))
                   + bool(st.state.get("replace")) + bool(st.state.get("pos_scale")) + len(st.plan_anim())
                   for st in self.stages.values()) + len(self.names["stages"]) + len(self.names["worlds"])

    @property
    def dirty(self) -> bool:
        """True when the edits differ from what was last saved (or nothing was saved and there are edits)."""
        top = self.undo_stack[-1]["id"] if self.undo_stack else None
        return top != self.saved_action

    # ---- plans
    def plan(self) -> dict:
        stages = []
        uses_objects = uses_geometry = uses_v6 = uses_v7 = False
        for k in self.changed_stages():
            st = self.stages[k]
            e = st.entry
            where = f"World {e['world']}-{e['stage']} {e.get('title') or ''}".strip() if e.get("world") else k
            entry = {"stage_key": k, "file": self.rom.profile()["stage_file_format"].format(k), "where": where}
            if st.plan_edits():
                entry["edits"] = st.plan_edits()
            if st.plan_ops():
                entry["objects"] = st.plan_ops()
                uses_objects = True
            if st.state.get("replace"):
                uses_v6 = True
                entry["replace_with"] = {"new_stage": _copy_replace(st.state["replace"])}
                entry["model_file"] = self.rom.profile()["stage_model_format"].format(k)
            if st.state.get("pos_scale"):
                uses_v6 = True
                entry["editor_pos_scale"] = float(st.state["pos_scale"])
            if st.state.get("geometry"):
                uses_geometry = True
                entry["editor_geometry"] = [_copy_geo(g) for g in st.state["geometry"]]
                if st.plan_collision():
                    entry["collision"] = st.plan_collision()
            if st.plan_model():
                entry["model"] = st.plan_model()
                entry["model_file"] = self.rom.profile()["stage_model_format"].format(k)
            if st.plan_anim():
                uses_v7 = True
                entry["animation"] = st.plan_anim()
                entry["animation_file"] = st.anim_path
            stages.append(entry)
        schema = PLAN_SCHEMA_V3 if uses_geometry else PLAN_SCHEMA_V2 if uses_objects else PLAN_SCHEMA
        if self.names_changed:
            schema = PLAN_SCHEMA_V5
        if uses_v6:
            schema = PLAN_SCHEMA_V6
        if uses_v7:
            schema = PLAN_SCHEMA_V7
        p = {"schema": schema, "title": self.title,
             "base_rom_sha1": self.rom_sha1, "stages": stages, "made_with": "tredit"}
        if self.names_changed:
            p["names"] = _copy_names(self.names)
        if self.output_path:
            p["editor_output_name"] = os.path.basename(self.output_path)
        return p

    def export_plan(self, path: str):
        if not self.has_edits():
            raise EditorError("there are no edits to export")
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(self.plan(), fh, indent=1)
            fh.write("\n")

    def import_plan(self, path: str) -> int:
        """Load edits from an edit plan or from a ROM copy's .patch.json; returns the number of changes."""
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError) as exc:
            raise EditorError(f"cannot read {os.path.basename(path)}: {exc}") from None
        plan = data.get("plan") if isinstance(data, dict) and data.get("schema") == "trcore.rom_patch_record/1" else data
        if isinstance(plan, dict) and plan.get("schema") == "trcore.edit_plan/4":
            raise EditorError("this plan edits animations or a model's drawing range (a trcore test plan); the "
                              "editor cannot load those for editing yet")
        if not isinstance(plan, dict) or plan.get("schema") not in (PLAN_SCHEMA, PLAN_SCHEMA_V2, PLAN_SCHEMA_V3,
                                                                     PLAN_SCHEMA_V5, PLAN_SCHEMA_V6, PLAN_SCHEMA_V7):
            raise EditorError("not an edit plan (or a patch record that contains one)")
        if plan.get("base_rom_sha1") and plan["base_rom_sha1"].lower() != self.rom_sha1:
            raise EditorError("these edits were made for a different ROM file than the one open now")
        new_names = None
        if plan.get("names"):
            merged = _copy_names(self.names)
            for kind in ("stages", "worlds"):
                for k, v in (plan["names"].get(kind) or {}).items():
                    if not isinstance(v, str):
                        raise EditorError("names given per language cannot be edited in the editor yet")
                    merged[kind][str(k)] = v
            new_names, _rep = self.check_names(merged)
        staged = []
        next_new = self._next_new
        self._next_new_import = next_new
        for s in plan.get("stages", []):
            key = s.get("stage_key")
            if key not in self.entries:
                raise EditorError(f"plan stage {key!r} is not in this ROM")
            st = self.stage(key)
            state = _copy_state(st.state)
            n = 0
            editor_made = "editor_geometry" in s or "editor_pos_scale" in s or "replace_with" in s
            if (s.get("collision") or s.get("model") or s.get("header")) and not editor_made:
                raise EditorError(f"{key}: this plan changes collision or the model in a way the editor did not "
                                  "make (for example a trcore test plan); it cannot be loaded for editing")
            if s.get("replace_with"):
                ns = (s["replace_with"] or {}).get("new_stage") or {}
                state = _empty_state()
                state["replace"] = new_stage_params(ns.get("platform"), ns.get("start"))
                n += 1
            if s.get("editor_pos_scale"):
                state["pos_scale"] = float(s["editor_pos_scale"])
                n += 1
            if s.get("animation"):
                ops = s["animation"]
                if not all(isinstance(o, dict) and o.get("op") in ("set_length", "set_keys") for o in ops):
                    raise EditorError(f"{key}: this plan edits the animation in a way the editor did not make (a "
                                      "trcore test plan); it cannot be loaded for editing")
                a = {"frames": None, "keys": {}}
                for o in ops:
                    try:
                        if o["op"] == "set_length":
                            a["frames"] = int(o["frames"])
                        else:
                            ch = o["channel"]
                            if ch not in CHANNELS:
                                raise EditorError(f"{key}: unknown channel {ch!r}")
                            a["keys"][f"{int(o['node'])}:{ch}"] = check_keys(o["keys"], a["frames"] or
                                                                             (st.original_frames or 0), ch)
                            n += 1
                    except (KeyError, TypeError, ValueError) as exc:
                        raise EditorError(f"{key}: bad animation entry in the plan ({exc})") from None
                if a["frames"]:
                    n += 1
                state["anim"] = a
            saved_base = st._base_tuple()
            try:
                st._set_base(state.get("replace"))        # edits and objects are relative to this base
                n = self._import_stage_items(s, st, state, n, key)
            finally:
                st._restore_base(saved_base)
            next_new = self._next_new_import
            staged.append((key, state, n))
        applied = []
        try:
            for key, state, n in staged:
                if n:
                    before = _copy_state(self.stage(key).state)
                    self.stage(key).set_state(state)
                    applied.append((key, before, n))
        except EditorError:
            for key, before, _n in applied:             # all or nothing
                self.stage(key).set_state(before)
            raise
        for key, before, _n in applied:
            self.undo_stack.append({"id": self._next_action, "key": key, "before": before,
                                    "after": _copy_state(self.stage(key).state),
                                    "label": f"import {os.path.basename(path)}"})
            self._next_action += 1
        renamed = 0
        if new_names is not None and new_names != self.names:
            renamed = len(new_names["stages"]) + len(new_names["worlds"])
            self.undo_stack.append({"id": self._next_action, "key": None, "names_before": _copy_names(self.names),
                                    "names_after": _copy_names(new_names), "label": f"import {os.path.basename(path)}"})
            self._next_action += 1
            self.names = new_names
        if applied or renamed:
            self.redo_stack.clear()
        self._next_new = next_new
        if plan.get("title"):
            self.title = plan["title"]
        return sum(n for _k, _b, n in applied) + renamed

    def _import_stage_items(self, s: dict, st: "StageState", state: dict, n: int, key: str) -> int:
        """Geometry, field edits and object operations of one plan entry, checked against the stage's
        current base; adds them to `state`. Returns the running count of changes."""
        next_new = self._next_new_import
        for g in s.get("editor_geometry", []):
            if not isinstance(g, dict) or not str(g.get("id", "")).startswith("geo:"):
                raise EditorError(f"{key}: bad geometry entry in the plan")
            g = check_geo(g)
            g["id"] = f"geo:n{next_new}"
            next_new += 1
            state["geometry"].append(g)
            n += 1
        for e in s.get("edits", []):
            t = e.get("target", "")
            try:
                orig = st.original_raw(t)
            except (EditorError, ValueError, KeyError, IndexError):
                raise EditorError(f"{key}: unknown target {t!r}") from None
            field = t.rsplit(".", 1)[-1]
            if field in ("position", "translation", "matrix"):
                frm, to = tuple(_q(v) for v in e["from"]), tuple(_q(v) for v in e["to"])
            else:
                frm = (int(e["from"], 0) if isinstance(e["from"], str) else e["from"],)
                to = (int(e["to"], 0) if isinstance(e["to"], str) else e["to"],)
            if frm != orig:
                raise EditorError(f"{key} {t}: plan expects {e['from']}, the ROM has {[v / FX for v in orig]}")
            if field == "matrix" and not self.scale_unlocked and _raw_size(to) != _raw_size(orig):
                raise EditorError(f"{key}: these edits change the size of a goal or bumper, and {SIZE_LOCKED}")
            prefix, rest = t.split("[", 1)
            kind = PREFIX_KIND[prefix]
            if kind in OBJECT_KINDS and field == POS_FIELD[kind]:
                state["positions"][f"{kind}:{int(rest.split(']')[0])}"] = to
            else:
                state["fields"][t] = to
            n += 1
        for op in s.get("objects", []):
            kind = op.get("kind")
            if kind not in OBJECT_KINDS:
                raise EditorError(f"{key}: unknown object kind {kind!r}")
            if op.get("op") in ("remove", "move"):
                i = op.get("index")
                try:
                    orig = st.original_position(kind, i)
                except (EditorError, TypeError):
                    raise EditorError(f"{key}: {kind} {i!r} does not exist") from None
                want = op.get("expect_position") if op["op"] == "remove" else op.get("from")
                if want is None or tuple(_q(v) for v in want) != orig:
                    raise EditorError(f"{key}: {kind} {i} is not where the plan expects it")
                if op["op"] == "remove":
                    state["deleted"].append(f"{kind}:{i}")
                else:
                    state["positions"][f"{kind}:{i}"] = tuple(_q(v) for v in op["to"])
            elif op.get("op") == "add":
                a = {"id": f"{kind}:n{next_new}", "kind": kind, "group": int(op.get("group", 0)),
                     "pos": tuple(_q(v) for v in op["position"]), "bunch": bool(op.get("bunch")),
                     "red": bool(op.get("red"))}
                if kind == "goal":
                    m = op.get("matrix")
                    a["matrix"] = tuple(_q(v) for v in m) if m else tuple(_q(v) for r in IDENTITY_ROWS for v in r)
                elif kind == "bumper" and op.get("matrix"):
                    a["matrix"] = tuple(_q(v) for v in op["matrix"])
                if a.get("matrix") and not self.scale_unlocked and _raw_size(a["matrix"]) != (1.0, 1.0, 1.0):
                    raise EditorError(f"{key}: these edits add a sized {kind}, and {SIZE_LOCKED}")
                next_new += 1
                state["added"].append(a)
            else:
                raise EditorError(f"{key}: unknown object operation {op.get('op')!r}")
            n += 1
        self._next_new_import = next_new
        return n

    # ---- saving
    def save(self, out_path: str, replace_own: bool = True) -> dict:
        """Write a ROM copy with all edits. replace_own allows replacing a copy this editor wrote before
        (checked against its patch record); the opened ROM itself is never replaced."""
        if not self.has_edits():
            raise EditorError("there are no edits to save")
        out_abs = os.path.abspath(out_path)
        if out_abs == self.rom_path or (os.path.exists(out_abs) and os.path.samefile(out_abs, self.rom_path)):
            raise EditorError("that is the ROM you opened; save the edits to a new copy instead")
        base_info = None
        if self.opened_editor_output:          # edits are stacked on an earlier editor copy: keep the chain
            r = self.opened_editor_output
            base_info = {"base_rom_was_editor_copy": {
                "name": os.path.basename(self.rom_path), "sha1": self.rom_sha1,
                "made_from": r.get("input_rom"), "its_plan": r.get("plan")}}
        try:
            rec = build_edit_rom(self.rom_path, out_abs, self.plan(), replace_own=replace_own, extra=base_info)
        except (EditRefused, RomWriteRefused) as exc:
            raise EditorError(str(exc)) from None
        except PermissionError as exc:
            raise EditorError(f"could not write {os.path.basename(out_abs)} ({exc.strerror}); "
                              "if it is open in BizHawk, close the ROM there first") from None
        except OSError as exc:
            raise EditorError(f"could not write {os.path.basename(out_abs)}: {exc}") from None
        self.output_path = out_abs
        self.saved_action = self.undo_stack[-1]["id"] if self.undo_stack else None
        return rec


def verify_saved_rom(out_path: str, project: Project) -> List[str]:
    """Re-read a saved copy and confirm every changed stage decodes to the editor's bytes."""
    problems = []
    try:
        with open(out_path, "rb") as fh:
            rom = type(project.rom)(fh.read(), out_path)      # same ROM type/profile as the opened ROM
        fmt = project.rom.profile()["stage_file_format"]
        changed = set(project.changed_stages())
        for k in sorted(changed):
            got, _ = lz10.decode(rom.file_bytes(fmt.format(k)))
            if got != project.stages[k].current:
                problems.append(f"{k}: saved stage differs from the editor's bytes")
        for k in project.entries:
            path = fmt.format(k)
            if k not in changed and path in rom.id_by_path and rom.file_bytes(path) != project.rom.file_bytes(path):
                problems.append(f"{k}: an unedited stage changed")
        mfmt = project.rom.profile().get("stage_model_format")
        if mfmt:
            for k in project.entries:
                path = mfmt.format(k)
                if path not in rom.id_by_path:
                    continue
                want = project.stages[k].current_model if k in project.stages else None
                if want is not None:
                    got, _ = lz10.decode(rom.file_bytes(path))
                    if got != want:
                        problems.append(f"{k}: saved model differs from the editor's")
                elif rom.file_bytes(path) != project.rom.file_bytes(path):
                    problems.append(f"{k}: a model without platforms changed")
        for k in project.entries:
            path = STAGE_ANIM_FORMAT.format(k)
            if path not in rom.id_by_path:
                continue
            want = project.stages[k].current_anim if k in project.stages else None
            if want is not None:
                got, _ = lz10.decode(rom.file_bytes(path))
                if got != want:
                    problems.append(f"{k}: saved animation differs from the editor's")
            elif rom.file_bytes(path) != project.rom.file_bytes(path):
                problems.append(f"{k}: an animation without keyframe edits changed")
        if project.names_table is not None:
            try:
                got = read_names(rom, strict=False)
                t = project.names_table
                want_st = [project.stage_name(i // 10 + 1, i % 10 + 1) for i in range(120)]
                want_w = [project.world_name(w + 1) for w in range(12)]
                if got.stages != want_st:
                    problems.append("saved stage names differ from the editor's")
                if [ns[0] for ns in got.worlds] != want_w or any(len(set(ns)) != 1 for ns in got.worlds
                                                                  if project.names["worlds"]):
                    problems.append("saved world names differ from the editor's")
                prof = project.rom.profile()
                if project.names_changed:
                    stray = [i for i, (a, b) in enumerate(zip(project.rom.arm9, rom.arm9)) if a != b and not any(
                        s <= project.rom.arm9_ram + i < e for s, e in prof["name_mask"])]
                    if stray:
                        problems.append("the program changed outside the name text")
                elif rom.arm9 != project.rom.arm9:
                    problems.append("the program changed although no name was edited")
                _ = t
            except NamesError as exc:
                problems.append(f"names could not be read back: {exc}")
    except (OSError, RomError, lz10.LZ10Error) as exc:
        problems.append(f"could not re-read the saved copy: {exc}")
    return problems


def find_original_rom(copy_path: str, record: dict) -> Optional[str]:
    """Look next to an editor-made copy (and one folder up) for the ROM its record says it was made from."""
    want = ((record or {}).get("input_rom") or {}).get("sha1")
    if not want:
        return None
    size = os.path.getsize(copy_path)
    here = os.path.dirname(os.path.abspath(copy_path))
    seen = set()
    for folder in (here, os.path.dirname(here)):
        try:
            names = sorted(os.listdir(folder))
        except OSError:
            continue
        for n in names:
            cand = os.path.join(folder, n)
            if not n.lower().endswith(".nds") or cand in seen or os.path.abspath(cand) == os.path.abspath(copy_path):
                continue
            seen.add(cand)
            try:
                if os.path.getsize(cand) != size:
                    continue
                h = hashlib.sha1()
                with open(cand, "rb") as fh:
                    for chunk in iter(lambda: fh.read(1 << 20), b""):
                        h.update(chunk)
            except OSError:
                continue
            if h.hexdigest() == want:
                return cand
    return None


def _editor_output_record(rom_path: str, sha1: str) -> Optional[dict]:
    """The patch record next to rom_path if it says this exact file was written by trcore."""
    try:
        with open(patch_record_path(rom_path), "r", encoding="utf-8") as fh:
            rec = json.load(fh)
    except (OSError, ValueError):
        return None
    if isinstance(rec, dict) and rec.get("schema") == "trcore.rom_patch_record/1" \
            and (rec.get("output_rom") or {}).get("sha1") == sha1:
        return rec
    return None
