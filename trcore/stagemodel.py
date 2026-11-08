"""A stage's own model (model/stage/<key>_nsbmd.lz) as geometry in stage coordinates.

Evidence (docs/research_notes/stc_format_spec.md §15, reproducible with
`python -m trcore stage-models ROM`):
  * the game builds the path from the same stage name as the .stc
    ('model/stage/%s_nsbmd.lz' next to 'stcoli/%s_stc.lz' in ARM9);
  * decoded triangle counts equal the model headers' triangle + 2 x quad counts
    in every stage model;
  * stage objects sit on the model's floor without any extra transform (starts,
    goals and bumpers at height 0 above the floor, bananas slightly above), so
    model space = stage space at the stage's rest pose;
  * a moving group's transform is the inverse of the model node with the same
    name in most groups, i.e. stored object positions are rest-pose positions.
"""
from __future__ import annotations

import collections
from typing import Dict, List, Optional

from .nitro import nsbmd as N
from .rom import NdsRom
from .stc.document import StageDocument


def load_stage_model(rom: NdsRom, stage_key: str) -> Optional[dict]:
    """{"path", "model", "triangles", "notes", "bounds"} or None when the ROM has no model for the stage."""
    data = rom.stage_model_decoded(stage_key)
    if data is None:
        return None
    f = N.Nsbmd(data, stage_key)
    if not f.models:
        raise N.NitroError(f"{stage_key}: model file has no models")
    geo = N.model_geometry(f, 0)
    return {"path": rom.profile()["stage_model_format"].format(stage_key), "model": f.models[0],
            "triangles": geo["triangles"], "notes": geo["notes"], "bounds": N.bounds(geo["triangles"])}


def floor_below(tris, p, tol: float = 0.02) -> Optional[float]:
    """Height of the highest model surface at p's (x, z) that is not above p (within tol)."""
    best = None
    px, py, pz = p
    for t in tris:
        a, b, c = t[0], t[1], t[2]
        if px < min(a[0], b[0], c[0]) - 1e-6 or px > max(a[0], b[0], c[0]) + 1e-6:
            continue
        if pz < min(a[2], b[2], c[2]) - 1e-6 or pz > max(a[2], b[2], c[2]) + 1e-6:
            continue
        d = (b[2] - c[2]) * (a[0] - c[0]) + (c[0] - b[0]) * (a[2] - c[2])
        if abs(d) < 1e-9:
            continue
        l1 = ((b[2] - c[2]) * (px - c[0]) + (c[0] - b[0]) * (pz - c[2])) / d
        l2 = ((c[2] - a[2]) * (px - c[0]) + (a[0] - c[0]) * (pz - c[2])) / d
        l3 = 1 - l1 - l2
        if min(l1, l2, l3) < -1e-6:
            continue
        y = l1 * a[1] + l2 * b[1] + l3 * c[1]
        if y <= py + tol and (best is None or y > best):
            best = y
    return best


def _is_identity(m, t=None, eps=1e-3) -> bool:
    ok = all(abs(m[i][j] - (1.0 if i == j else 0.0)) < eps for i in range(3) for j in range(3))
    return ok and (t is None or all(abs(x) < eps for x in t))


def group_kinds(doc: StageDocument, model) -> Dict[int, str]:
    """Classify each group by how its transform relates to the model node of the same name."""
    nodes = {n["name"]: model.node_matrix(i) for i, n in enumerate(model.nodes)} if model else {}
    out = {}
    for g in doc.groups:
        gm = (tuple(tuple(r) for r in g["matrix"]), tuple(g["translation"]))
        n = nodes.get(g.get("name_text_04"))
        if n is not None:
            r, t = N.mat_then(gm, n)
            out[g["index"]] = "node_inverse" if _is_identity(r, t) else "node_other"
        else:
            out[g["index"]] = ("static" if g["index"] == 0 else "group") + (
                "_identity" if _is_identity(gm[0], gm[1]) else "_transform")
    return out


def stage_objects(doc: StageDocument):
    items = [("start", 0, r["position"]) for r in (doc.start or {}).get("records", [])]
    for key, kind, field in (("A", "goal", "translation"), ("B", "bumper", "translation"),
                             ("C", "banana", "position")):
        items += [(kind, r["group"], r[field]) for r in doc.pools.get(key, {}).get("records", [])]
    return items


def _gap_bucket(gap: Optional[float]) -> str:
    if gap is None:
        return "no surface below"
    if abs(gap) < 0.02:
        return "on surface (|gap| < 0.02)"
    if gap < 2:
        return "0.02 - 2 above"
    return "more than 2 above"


def survey(rom: NdsRom) -> dict:
    """Whole-ROM check of every stage model against its stage file."""
    prof = rom.profile()
    stage_keys = sorted(p[len("stcoli/"):-len("_stc.lz")] for p in rom.id_by_path
                        if p.startswith("stcoli/") and p.endswith("_stc.lz"))
    by_kind = collections.defaultdict(collections.Counter)
    by_group = collections.defaultdict(collections.Counter)
    relation = collections.Counter()
    tri_mismatch, errors, without_model, models = [], [], [], 0
    for key in stage_keys:
        try:
            sm = load_stage_model(rom, key)
        except N.NitroError as exc:
            errors.append(f"{key}: {exc}")
            continue
        if sm is None:
            without_model.append(key)
            continue
        models += 1
        m = sm["model"]
        if len(sm["triangles"]) != m.num_triangle + 2 * m.num_quad:
            tri_mismatch.append(key)
        doc = StageDocument(rom.stage_file_decoded(key), key)
        kinds = group_kinds(doc, m)
        for g, k in kinds.items():
            relation[k] += 1
        for kind, grp, p in stage_objects(doc):
            fy = floor_below(sm["triangles"], p)
            b = _gap_bucket(None if fy is None else p[1] - fy)
            by_kind[kind][b] += 1
            by_group[kinds.get(grp, "none")][b] += 1
    names = set(rom.stage_names())
    return {
        "schema": "trcore.stage_model_survey/1",
        "model_path_format": prof["stage_model_format"],
        "stage_files": len(stage_keys), "models_checked": models,
        "stage_files_without_model": without_model,
        "stage_files_without_model_that_have_a_name": [k for k in without_model if k in names],
        "parse_errors": errors,
        "triangle_count_mismatches": tri_mismatch,
        "object_height_above_model_surface": {k: dict(v) for k, v in sorted(by_kind.items())},
        "by_group_kind": {k: dict(v) for k, v in sorted(by_group.items())},
        "group_transform_vs_model_node": dict(sorted(relation.items())),
    }
