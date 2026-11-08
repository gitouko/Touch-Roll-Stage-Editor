"""Apply an edit plan (JSON) to stage files and write a NEW ROM copy.

Plan format (schema "trcore.edit_plan/1"; "trcore.edit_plan/2" additionally allows an "objects" list
per stage with add / remove / move operations, see trcore/stc/objects.py; "trcore.edit_plan/3" also
allows "collision" (remove_box / clear / add_polygons, trcore/stc/geometry_edit.py), "model"
({"op": "add_polygons", "shape": name, "polygons": [...]} and {"op": "clear_shapes", "shapes": "all" |
[names]}, trcore/nitro/nsbmd_write.py; replaces the stage's model file as well; a polygon is a list of 3 or 4
corners, or {"corners": [...], "st": [[s, t], ...]} with its own texture coordinates in texels) and "header"
({"bounds": {"min": [...], "max": [...]}}, trcore/stc/stagehead.py). Per stage the order is: field
edits, header, collision, objects, model. trcore/newstage.py builds such an entry for a new stage.
"trcore.edit_plan/4" adds the model op {"op": "set_pos_scale", "scale": 4} (drawing range = 8 x scale,
applied before the other model ops) and "animation": a list of ops for the stage's .nsbca
(scale_translation / mirror_rotation / retime, trcore/nitro/nsbca_write.py; "animation_file" overrides the
path)).
"trcore.edit_plan/5" adds a top-level "names": {"stages": {"1-1": "NEW NAME", ...}, "worlds": {"1": "Name"
or {"en": ..., "de": ..., "fr": ..., "es": ..., "it": ...}}} (trcore/names.py; patches the ARM9 text).
"trcore.edit_plan/6" adds per stage "replace_with": {"new_stage": {"platform": {...}, "start": {...},
"shape": optional}}: the stage is first rebuilt as a bare new stage (trcore/newstage.py build_new_stage,
exactly edit test 5's operations), and every other operation of the entry then applies to that new stage.
"trcore.edit_plan/7" adds the keyframe animation ops "set_length" and "set_keys" (trcore/keyframes.py,
trcore/nitro/nsbca_write.py; the editor's keyframe editor):
{
  "schema": "trcore.edit_plan/1",
  "title": "short description",
  "base_rom_sha1": "<sha1 of the ROM the plan was written for>",   (optional but recommended)
  "stages": [
    {"stage_key": "smb1_be01",               (or "file": "stcoli/smb1_be01_stc.lz")
     "where": "World 1-1 Elementary",        (free text)
     "edits": [{"target": "pool_C[1].position", "from": [3.45, 0.5, 0], "to": [3.45, 0.5, 2],
                "prediction": "what should be seen in-game"}]}
  ]
}
"""
from __future__ import annotations

import hashlib
import json
from typing import Optional

from . import lz10, romwrite
from .rom import NdsRom
from .stc.edit import EditRefused, apply_edits
from .nitro.nsbmd import NitroError, Nsbmd
from .names import NamesError, apply_names, arm9_patches, read_names
from .newstage import NewStageError, build_new_stage
from .nitro.nsbca_write import AnimEditRefused, edit_animation
from .nitro.nsbmd_write import DEFAULT_TEXELS_PER_UNIT, ModelEditRefused, edit_model, rescale_positions
from .stc.stagehead import HeaderEditRefused, apply_header
from .stc.geometry_edit import CollisionEditRefused, apply_collision_ops
from .stc.objects import ObjectEditRefused, apply_object_ops

PLAN_SCHEMA = "trcore.edit_plan/1"          # field edits only
PLAN_SCHEMA_V2 = "trcore.edit_plan/2"       # field edits + "objects" (add / remove / move, trcore/stc/objects.py)
PLAN_SCHEMA_V3 = "trcore.edit_plan/3"       # + "collision" and "model" (geometry, Milestone 4)
PLAN_SCHEMA_V4 = "trcore.edit_plan/4"       # + "animation" ops and the model op "set_pos_scale" (Milestone 6)
PLAN_SCHEMA_V5 = "trcore.edit_plan/5"       # + top-level "names" (world / stage names, Milestone 7)
PLAN_SCHEMA_V6 = "trcore.edit_plan/6"       # + per-stage "replace_with" (editor's Create New Stage, Milestone 8)
PLAN_SCHEMA_V7 = "trcore.edit_plan/7"       # + keyframe animation ops set_length / set_keys (Milestone 9)
PLAN_SCHEMAS = (PLAN_SCHEMA, PLAN_SCHEMA_V2, PLAN_SCHEMA_V3, PLAN_SCHEMA_V4, PLAN_SCHEMA_V5, PLAN_SCHEMA_V6,
                PLAN_SCHEMA_V7)
KEYFRAME_OPS = ("set_length", "set_keys")
STAGE_ANIM_FORMAT = "model/stage/{}_nsbca.lz"


def model_shape_names(model_bytes: bytes, label: str = "") -> list:
    try:
        return [s["name"] for s in Nsbmd(model_bytes, label).models[0].shapes]
    except (NitroError, IndexError) as exc:
        raise EditRefused(f"{label}: model unreadable: {exc}") from None


def check_plan(plan: dict) -> dict:
    if plan.get("schema") not in PLAN_SCHEMAS:
        raise EditRefused(f"plan schema must be one of {PLAN_SCHEMAS}")
    level = PLAN_SCHEMAS.index(plan["schema"])
    if plan.get("names") and level < 4:
        raise EditRefused(f"name edits need schema {PLAN_SCHEMA_V5!r}")
    if plan.get("names") is not None and not isinstance(plan["names"], dict):
        raise EditRefused("'names' must be an object with 'stages' and/or 'worlds'")
    if not plan.get("stages") and not plan.get("names"):
        raise EditRefused("plan has no stages")
    for st in plan.get("stages") or []:
        if st.get("objects") and level < 1:
            raise EditRefused(f"object operations need schema {PLAN_SCHEMA_V2!r} or later")
        if (st.get("collision") or st.get("model")) and level < 2:
            raise EditRefused(f"collision and model operations need schema {PLAN_SCHEMA_V3!r}")
        if st.get("model") and not (st.get("stage_key") or st.get("model_file")):
            raise EditRefused("model operations need 'stage_key' (the model file is found from it) or 'model_file'")
        if st.get("header") and level < 2:
            raise EditRefused(f"header edits need schema {PLAN_SCHEMA_V3!r}")
        if (st.get("animation") or any(m.get("op") == "set_pos_scale" for m in st.get("model") or [])) and level < 3:
            raise EditRefused(f"animation and set_pos_scale operations need schema {PLAN_SCHEMA_V4!r}")
        if st.get("animation") and not (st.get("stage_key") or st.get("animation_file")):
            raise EditRefused("animation operations need 'stage_key' or 'animation_file'")
        if any((a or {}).get("op") in KEYFRAME_OPS for a in st.get("animation") or []) and level < 6:
            raise EditRefused(f"set_length / set_keys need schema {PLAN_SCHEMA_V7!r}")
        if st.get("replace_with") and level < 5:
            raise EditRefused(f"replace_with needs schema {PLAN_SCHEMA_V6!r}")
        if st.get("replace_with") and not isinstance((st["replace_with"] or {}).get("new_stage"), dict):
            raise EditRefused("replace_with must be {\"new_stage\": {...}}")
        if not any(st.get(k) for k in ("edits", "objects", "collision", "model", "header", "animation",
                                         "replace_with")):
            raise EditRefused(f"{st.get('stage_key') or st.get('file')}: nothing to do")
    return plan


def load_plan(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        plan = json.load(fh)
    return check_plan(plan)


def build_edit_rom(rom_path: str, out_path: str, plan: dict, replace_own: bool = False,
                   extra: Optional[dict] = None) -> dict:
    check_plan(plan)
    rom = NdsRom.from_file(rom_path)
    sha1 = hashlib.sha1(rom.data).hexdigest()
    if plan.get("base_rom_sha1") and plan["base_rom_sha1"].lower() != sha1:
        raise EditRefused(f"plan was written for ROM {plan['base_rom_sha1']}, this ROM is {sha1}")
    prof = rom.profile()
    replacements, stage_records, seen = [], [], set()
    for st in plan.get("stages") or []:
        path = st.get("file")
        if not path:
            if not prof or not st.get("stage_key"):
                raise EditRefused("each stage needs 'file', or 'stage_key' on a profiled ROM")
            path = prof["stage_file_format"].format(st["stage_key"])
        if path in seen:
            raise EditRefused(f"{path} appears twice in the plan; put all its edits in one entry")
        seen.add(path)
        comp = rom.file_bytes(path)
        dec, _ = lz10.decode(comp)
        label = st.get("stage_key") or path
        base, base_model, replaced = dec, None, None
        if st.get("replace_with"):
            ns = st["replace_with"]["new_stage"]
            mp = st.get("model_file") or (prof["stage_model_format"].format(st["stage_key"])
                                          if prof and st.get("stage_key") else None)
            if not mp or not st.get("stage_key"):
                raise EditRefused("replace_with needs 'stage_key' and 'model_file' (or a recognised ROM)")
            try:
                mraw = rom.file_bytes(mp)
                base, base_model, nentry = build_new_stage(
                    dec, lz10.decode(mraw)[0] if mraw[:1] == b"\x10" else mraw, st["stage_key"],
                    platform=ns.get("platform"), start=ns.get("start"), shape=ns.get("shape"))
            except (NewStageError, KeyError, lz10.LZ10Error) as exc:
                raise EditRefused(f"{label}: new stage: {exc}") from None
            replaced = {"platform": nentry["new_stage"]["platform"], "shape": nentry["new_stage"]["shape"],
                        "start": nentry["new_stage"]["start"],
                        "base_sha256": hashlib.sha256(base).hexdigest(),
                        "base_model_sha256": hashlib.sha256(base_model).hexdigest()}
        new_dec, recs = apply_edits(base, st["edits"], label) if st.get("edits") else (base, [])
        obj_report = coll_report = head_report = None
        model_record = None
        if st.get("header"):
            try:
                new_dec, head_report = apply_header(new_dec, st["header"], label)
            except HeaderEditRefused as exc:
                raise EditRefused(f"{label} header: {exc}") from None
        # collision before objects: a grown grid lets objects be placed on new geometry outside the old one
        if st.get("collision"):
            try:
                new_dec, coll_report = apply_collision_ops(new_dec, st["collision"], label)
            except CollisionEditRefused as exc:
                raise EditRefused(f"{label} collision: {exc}") from None
        if st.get("objects"):
            try:
                new_dec, obj_report = apply_object_ops(new_dec, st["objects"], label)
            except ObjectEditRefused as exc:
                raise EditRefused(f"{label}: {exc}") from None
        if new_dec != dec:
            new_comp = lz10.encode(new_dec)
            if lz10.decode(new_comp)[0] != new_dec:
                raise EditRefused(f"{label}: compressed stage does not decode back to the edited bytes")
            replacements.append((path, new_comp, False))
        if st.get("model") or base_model is not None:
            mpath = st.get("model_file")
            if not mpath:
                if not prof or "stage_model_format" not in prof or not st.get("stage_key"):
                    raise EditRefused("model operations need 'model_file', or 'stage_key' on a recognised ROM")
                mpath = prof["stage_model_format"].format(st["stage_key"])
            if mpath in seen:
                raise EditRefused(f"{mpath} appears twice in the plan")
            seen.add(mpath)
            mcomp = rom.file_bytes(mpath)
            compressed = mcomp[:1] == b"\x10"
            rom_model = lz10.decode(mcomp)[0] if compressed else mcomp
            mdec = base_model if base_model is not None else rom_model
            shape_names = model_shape_names(mdec, label)
            adds: dict = {}                     # shape -> polygons (one append per shape)
            clear: list = []
            op_list = []
            scale_report = None
            new_m = mdec
            for k, mop in enumerate(st.get("model") or []):
                if mop.get("op") == "set_pos_scale":
                    if k != 0:
                        raise EditRefused(f"{label} model op {k}: set_pos_scale must be the first model op")
                    try:
                        new_m, scale_report = rescale_positions(mdec, float(mop.get("scale", 0)), label)
                    except (ModelEditRefused, ValueError, TypeError) as exc:
                        raise EditRefused(f"{label} model op {k}: {exc}") from None
                    op_list.append({"op": "set_pos_scale", "scale": float(mop["scale"]), "label": mop.get("label"),
                                    "test": mop.get("test")})
                elif mop.get("op") == "add_polygons":
                    if not mop.get("polygons"):
                        raise EditRefused(f"{label} model op {k}: no polygons")
                    adds.setdefault(mop.get("shape"), []).extend(mop["polygons"])
                    op_list.append({"op": "add_polygons", "shape": mop.get("shape"), "label": mop.get("label"),
                                    "test": mop.get("test"), "polygons": len(mop["polygons"])})
                elif mop.get("op") == "clear_shapes":
                    names = shape_names if mop.get("shapes") == "all" else list(mop.get("shapes") or [])
                    if not names:
                        raise EditRefused(f"{label} model op {k}: no shapes to clear")
                    clear += [n for n in names if n not in clear]
                    op_list.append({"op": "clear_shapes", "shapes": names, "label": mop.get("label"),
                                    "test": mop.get("test")})
                else:
                    raise EditRefused(f"{label} model op {k}: op must be set_pos_scale, add_polygons or clear_shapes")
            mrep = {"shapes": {}, "readback_max_error": 0.0}
            if adds or clear:
                try:
                    new_m, mrep = edit_model(new_m, adds, clear, label, tex_source=rom_model,
                                             default_texels=DEFAULT_TEXELS_PER_UNIT)
                except ModelEditRefused as exc:
                    raise EditRefused(f"{label} model: {exc}") from None
            mout = lz10.encode(new_m) if compressed else new_m
            if compressed and lz10.decode(mout)[0] != new_m:
                raise EditRefused(f"{label}: compressed model does not decode back to the edited bytes")
            replacements.append((mpath, mout, False))
            ops_report = []
            for nm, r in mrep["shapes"].items():
                ops_report.append(dict(r, readback_max_error=mrep["readback_max_error"],
                                       ops=[o for o in op_list if o.get("shape") == nm]))
            model_record = {"file": mpath, "size_before": len(rom_model), "size_after": len(new_m),
                            "sha256_before": hashlib.sha256(rom_model).hexdigest(),
                            "sha256_after": hashlib.sha256(new_m).hexdigest(), "ops": ops_report,
                            "plan_ops": op_list, "shapes_cleared": clear, "pos_scale": scale_report}
        anim_record = None
        if st.get("animation"):
            apath = st.get("animation_file") or STAGE_ANIM_FORMAT.format(st["stage_key"])
            if apath not in rom.id_by_path:
                raise EditRefused(f"{label}: no animation file {apath}")
            if apath in seen:
                raise EditRefused(f"{apath} appears twice in the plan")
            seen.add(apath)
            acomp = rom.file_bytes(apath)
            acompressed = acomp[:1] == b"\x10"
            adec = lz10.decode(acomp)[0] if acompressed else acomp
            try:
                new_a, arep = edit_animation(adec, st["animation"], label)
            except AnimEditRefused as exc:
                raise EditRefused(f"{label} animation: {exc}") from None
            aout = lz10.encode(new_a) if acompressed else new_a
            if acompressed and lz10.decode(aout)[0] != new_a:
                raise EditRefused(f"{label}: compressed animation does not decode back to the edited bytes")
            replacements.append((apath, aout, False))
            anim_record = dict(arep, file=apath, size_before=len(adec), size_after=len(new_a),
                               sha256_before=hashlib.sha256(adec).hexdigest(),
                               sha256_after=hashlib.sha256(new_a).hexdigest())
        stage_records.append({"file": path, "stage_key": st.get("stage_key"), "where": st.get("where"),
                              "decoded_size": len(dec), "decoded_size_after": len(new_dec),
                              "decoded_sha256_before": hashlib.sha256(dec).hexdigest(),
                              "decoded_sha256_after": hashlib.sha256(new_dec).hexdigest(),
                              "edits": recs, "objects": obj_report, "collision": coll_report,
                              "model": model_record, "header": head_report, "animation": anim_record,
                              "replaced_with_new_stage": replaced,
                              "new_stage": st.get("new_stage")})
    patches, names_report = [], None
    if plan.get("names"):
        try:
            table = read_names(rom)
            new_arm9, names_report, allowed = apply_names(table, plan["names"])
            patches = arm9_patches(rom.arm9, new_arm9, rom.arm9_ram, allowed)
        except NamesError as exc:
            raise EditRefused(f"names: {exc}") from None
    return romwrite.write_rom_copy_multi(
        rom_path, out_path, replacements, note=plan.get("title"),
        extra={"plan_schema": plan["schema"], "stage_edits": stage_records, "names": names_report,
               "status": "generated - not yet verified in-game", "plan": plan, **(extra or {})},
        replace_own=replace_own, arm9_patches=patches)
