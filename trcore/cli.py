"""Command-line entry point:  py -3 -m trcore <command> [options]

Commands (all read-only with respect to the inputs):
  index      content-classify every file under data/ and pair copies across layers
  validate   index + parse every stage file + per-stage reports + validation matrix
  roundtrip  no-op export check: every LCTS source re-exports byte-identically
  inspect    human-readable summary of one .stc / .lz file (optionally full JSON)
  hexview    bounded hex/word dump of a range of a DECODED stage stream

Exit codes: 0 = no errors, 1 = errors found (or warnings with --fail-on-warning),
2 = usage or I/O problem. Output text is ASCII so it is safe in any Windows console.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
from typing import Dict, List, Optional

from . import TOOL_NAME, __version__
from . import assets as A
from . import report as R
from .stc.document import StageDocument

DEFAULT_OUT = os.path.join("generated", "trcore")


def _p(msg: str = "") -> None:
    print(msg.encode("ascii", "replace").decode("ascii"))


# ------------------------------------------------------------------ helpers

def _stage_sources(index: dict) -> Dict[str, dict]:
    """stage_key -> {path, payload_sha256} for consistent .stc pairs (prefers a plain copy)."""
    by_path = {a["path"]: a for a in index["assets"]}
    out = {}
    for p in index["pairs"]:
        if p["ext"] != "stc" or p["status"] != "consistent":
            continue
        members = [by_path[x] for v in p["layers"].values() for x in v]
        members.sort(key=lambda a: (a.get("container") != "plain", a["path"]))
        out[p["stage_key"]] = {"path": members[0]["path"], "payload_sha256": p["payload_sha256"]}
    return dict(sorted(out.items()))


def run_roundtrip(root: str, index: dict) -> dict:
    """No-op preservation: every file re-exports identically; LCTS payloads match their pair hash."""
    pair_sha = {(p["stage_key"], p["ext"]): p["payload_sha256"] for p in index["pairs"]}
    res = {"lcts_sources_checked": 0, "lcts_source_identity_pass": 0, "lcts_decoded_matches_pair": 0,
           "other_sources_checked": 0, "other_source_identity_pass": 0, "failures": []}
    for a in index["assets"]:
        path = os.path.join(root, *a["path"].split("/"))
        with open(path, "rb") as fh:
            data = fh.read()
        if a.get("signature") == "LCTS":
            res["lcts_sources_checked"] += 1
            doc = StageDocument(data, a["path"])
            if doc.export_source() == data:
                res["lcts_source_identity_pass"] += 1
            else:
                res["failures"].append({"path": a["path"], "problem": "source export differs"})
            exp = doc.export_decoded()
            want = pair_sha.get((a.get("stage_key"), a.get("name_ext")))
            if exp is not None and hashlib.sha256(exp).hexdigest() == want:
                res["lcts_decoded_matches_pair"] += 1
            else:
                res["failures"].append({"path": a["path"], "problem": "decoded export does not match pair payload"})
        else:
            # Non-stage assets have no document model; the check is that classification
            # never alters the buffer it was given.
            res["other_sources_checked"] += 1
            before = hashlib.sha256(data).hexdigest()
            A.classify_bytes(data)
            if hashlib.sha256(data).hexdigest() == before == a.get("sha256"):
                res["other_source_identity_pass"] += 1
            else:
                res["failures"].append({"path": a["path"], "problem": "source bytes changed or hash mismatch"})
    return res


def _print_index_summary(s: dict) -> None:
    _p(f"files: {s['files']}  lz10: {s['lz10_files']}  lz10_invalid: {s['lz10_invalid']}  "
       f"unknown: {s['unknown_content']}")
    for layer, kinds in s["container_by_layer"].items():
        _p(f"  {layer}: " + ", ".join(f"{k}={v}" for k, v in kinds.items()))
    _p(f"pairs: {s['pairs']} {s['pairs_by_status']}  (lz10+plain copies agree: {s['pairs_with_lz10_and_plain_copies']})")
    _p(f"duplicate-content groups: {s['duplicate_content_groups']}")
    for k, v in s["issue_counts"].items():
        _p(f"  {k}: {v}")


def _require_files(index: dict, root: str) -> None:
    if not index["assets"]:
        raise R.OutputRefused(f"no input files found under {os.path.join(root, 'data')}")


# ----------------------------------------------------------------- commands

def cmd_index(args) -> int:
    index = A.build_index(args.root)
    _require_files(index, args.root)
    s = index["summary"]
    _print_index_summary(s)
    for layer, missing in index["missing_by_layer"].items():
        _p(f"missing in {layer}: {', '.join(missing)}")
    if args.out:
        w = R.OutputWriter(args.out, [os.path.join(args.root, "data")])
        p = w.write_text("corpus_index.json", R.dumps(index))
        _p(f"wrote {p}")
    warn = sum(v for k, v in s["issue_counts"].items() if k.startswith("warning:"))
    if s["error_count"]:
        return 1
    return 1 if (args.fail_on_warning and warn) else 0


def cmd_validate(args) -> int:
    root = args.root
    w = R.OutputWriter(args.out, [os.path.join(root, "data")])
    index = A.build_index(root)
    _require_files(index, root)
    _print_index_summary(index["summary"])
    sources = _stage_sources(index)
    by_payload: Dict[str, List[str]] = {}
    for key, src in sources.items():
        by_payload.setdefault(src["payload_sha256"], []).append(key)

    written = set()
    entries = []
    docs_with_errors = []
    for payload, keys in sorted(by_payload.items(), key=lambda kv: sorted(kv[1])[0]):
        keys = sorted(keys)
        canonical = None
        for key in keys:  # every key is parsed from its own file
            src = sources[key]
            doc = StageDocument.from_file(os.path.join(root, *src["path"].split("/")), src["path"])
            if doc.has_errors():
                docs_with_errors.append(key)
            rep = R.stage_report(doc, key, src["path"], [k for k in keys if k != key])
            name = "stages/" + R.report_filename(key, doc.decoded_sha256, doc.source_sha256)
            w.write_text(name, R.dumps(rep))
            written.add(name)
            if canonical is None:
                canonical = doc
        entries.append({"stage_key": keys[0], "checks": canonical.checks, "variant": canonical.layout_variant,
                        "diag_codes": sorted({d["code"] for d in canonical.diagnostics})})
    matrix = R.validation_matrix(entries)
    rt = run_roundtrip(root, index)
    matrix["roundtrip"] = {k: v for k, v in rt.items()}
    w.write_text("corpus_index.json", R.dumps(index))
    w.write_text("validation_matrix.json", R.dumps(matrix))
    w.write_text("validation_matrix.md", R.matrix_markdown(matrix, index["summary"]))

    stage_dir = os.path.join(w.out_dir, "stages")
    stale = sorted("stages/" + f for f in os.listdir(stage_dir) if "stages/" + f not in written) \
        if os.path.isdir(stage_dir) else []
    problems = []
    if index["summary"]["error_count"]:
        problems.append(f"{index['summary']['error_count']} index error(s)")
    if docs_with_errors:
        problems.append(f"error diagnostics in: {', '.join(docs_with_errors)}")
    if rt["failures"]:
        problems.append(f"{len(rt['failures'])} round-trip failure(s)")
    summary = {"tool": R.tool_id(), "stage_keys": len(sources), "unique_stage_contents": len(entries),
               "writes": w.log, "stale_files_not_written_this_run": stale, "problems": problems,
               "exit_code": 1 if problems else 0}
    w.write_text("run_summary.json", R.dumps(summary))

    _p(f"stage keys: {len(sources)}  unique contents parsed: {len(entries)}")
    for name, c in matrix["checks"].items():
        if c["fail"]:
            _p(f"  {name}: fail {c['fail']} -> {', '.join(matrix['failing_stage_keys'][name])}")
    _p(f"layout variants: " + ", ".join(f"{k}={len(v)}" for k, v in matrix["layout_variants"].items()))
    _p(f"round-trip: LCTS {rt['lcts_source_identity_pass']}/{rt['lcts_sources_checked']} source-identical, "
       f"{rt['lcts_decoded_matches_pair']}/{rt['lcts_sources_checked']} decoded==pair; "
       f"other {rt['other_source_identity_pass']}/{rt['other_sources_checked']}")
    changed = [x for x in w.log if x["status"] == "replaced"]
    _p(f"outputs: {w.out_dir}  (created {sum(1 for x in w.log if x['status'] == 'created')}, "
       f"replaced {len(changed)}, unchanged {sum(1 for x in w.log if x['status'] == 'unchanged')})")
    if stale:
        _p(f"note: {len(stale)} older file(s) in stages/ were not produced by this run (left in place)")
    for pr in problems:
        _p(f"PROBLEM: {pr}")
    return 1 if problems else 0


def cmd_roundtrip(args) -> int:
    index = A.build_index(args.root)
    _require_files(index, args.root)
    rt = run_roundtrip(args.root, index)
    _p(f"LCTS sources: {rt['lcts_sources_checked']}  source-identical: {rt['lcts_source_identity_pass']}  "
       f"decoded==pair: {rt['lcts_decoded_matches_pair']}")
    _p(f"other sources: {rt['other_sources_checked']}  identical: {rt['other_source_identity_pass']}")
    for f in rt["failures"]:
        _p(f"FAIL {f['path']}: {f['problem']}")
    return 1 if rt["failures"] or index["summary"]["error_count"] else 0


def cmd_inspect(args) -> int:
    doc = StageDocument.from_file(args.file, os.path.basename(args.file))
    _p(f"file: {args.file}")
    _p(f"container: {doc.container}  source {len(doc.source)} B sha256 {doc.source_sha256}")
    if doc.decoded is not None:
        _p(f"decoded: {len(doc.decoded)} B sha256 {doc.decoded_sha256}")
    _p(f"layout variant: {doc.layout_variant}")
    if doc.header:
        h = {k: v["value"] for k, v in doc.header.items()}
        _p(f"header: u32_04={h['u32_04']:#x} u32_08={h['u32_08']} u32_0C={h['u32_0C']:#x} u32_10={h['u32_10']:#x} "
           f"u32_34={h['u32_34']} F={h['descriptor_base']:#x} groups={h['group_count']}")
        _p("bounds (Q20.12): min (%g, %g, %g) max (%g, %g, %g)" % tuple(
            h[k] for k in ("bounds_min_x", "bounds_min_y", "bounds_min_z", "bounds_max_x", "bounds_max_y",
                           "bounds_max_z")))
    for r in doc.start.get("records", []) if doc.start else []:
        _p(f"start[{r['index']}] @{r['offset']}: pos {tuple(r['position'])} u32_0C={r['u32_0C']:#x}")
    for g in doc.groups:
        sl = ", ".join(f"{k}={v['count']}@{v['offset']:#x}" for k, v in g["slices"].items())
        _p(f"group {g['index']} @{g['offset']} {g['name_text_04']!r}: det={g['matrix_stats']['det']} "
           f"t={tuple(g['translation'])} grid={g['grid']['nx']}x{g['grid']['nz']} slices[{sl}]")
    for k, p in doc.pools.items():
        if not p["total"]:
            continue
        _p(f"pool {k} ({p['candidate_label']} candidate, stride {p['record_stride']:#x}) @{p['start']}: "
           f"total {p['total']}, referenced {p['referenced']}, unreferenced {p.get('unreferenced', 0)}")
        if k == "A":
            for r in p["records"]:
                _p(f"   A[{r['index']}] @{r['offset']} group={r['group']} t={tuple(r['translation'])} "
                   f"u32_30={r['u32_30']} det={r['matrix_stats']['det']}")
    if doc.coverage:
        c = doc.coverage
        _p(f"coverage: {c['claimed_by_views']}/{c['length']} B in views, {c['zero_fill']} zero-fill, "
           f"{c['opaque']} opaque in {len(c['opaque_ranges'])} range(s)")
    for d in doc.diagnostics:
        _p(f"[{d['severity']}] {d['code']}: {d['message']}" + (f" @{d['offset']}" if d["offset"] else ""))
    fails = [k for k, v in doc.checks.items() if v == "fail"]
    _p(f"checks failing: {', '.join(fails) if fails else 'none'}")
    if args.json:
        out = os.path.abspath(args.json)
        if out == os.path.abspath(args.file) or not out.lower().endswith(".json"):
            _p("refusing: --json must be a new .json path, never the input file")
            return 2
        with open(out, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(R.dumps(R.stage_report(doc, None, os.path.basename(args.file))))
        _p(f"wrote {out}")
    return 1 if doc.has_errors() else 0


def cmd_hexview(args) -> int:
    doc = StageDocument.from_file(args.file)
    if doc.decoded is None:
        _p("cannot decode this file as an LCTS stage stream")
        return 1
    data = doc.decoded
    off, length = args.offset, min(args.length, 0x1000)
    if off < 0 or off >= len(data):
        _p(f"offset {off:#x} outside decoded stream of {len(data):#x} bytes")
        return 2
    end = min(len(data), off + length)
    _p(f"decoded stream {len(data):#x} bytes; showing [{off:#x}, {end:#x})  words: u32 | Q20.12")
    for row in range(off, end, 16):
        chunk = data[row:min(end, row + 16)]
        words = []
        for k in range(0, len(chunk) - 3, 4):
            v = int.from_bytes(chunk[k:k + 4], "little")
            s = v - (1 << 32) if v & 0x80000000 else v
            words.append(f"{v:08x}|{s / 4096.0:g}")
        _p(f"{row:06x}: {chunk.hex(' '):<48}  {'  '.join(words)}")
    return 0


# ------------------------------------------------------------ ROM commands

def _load_rom(path: str):
    from .rom import NdsRom
    return NdsRom.from_file(path)


def cmd_rom_info(args) -> int:
    rom = _load_rom(args.rom)
    i = rom.identity()
    _p(f"title {i['title']}  game code {i['game_code']}  maker {i['maker_code']}  version {i['rom_version']}")
    _p(f"size {i['size']}  sha1 {i['sha1']}  crc32 {i['crc32']}  header CRC ok: {i['header_crc_ok']}")
    _p(f"NitroFS files: {i['files']}  used ROM size: 0x{i['used_rom_size']:X}")
    _p(f"code fingerprint {rom.code_fingerprint()}  profile: {(rom.profile() or {}).get('label', 'none')}")
    return 0


def cmd_stagemap(args) -> int:
    from .rom import stage_map_markdown
    rom = _load_rom(args.rom)
    m = rom.stage_map()
    for w in m["worlds"]:
        _p(f"World {w['world']:2d} {w['title']}: " +
           ", ".join(f"{s['stage']}={s['stage_key']}{'*' if s['flags'] & 1 else ''}" for s in w["stages"]))
    _p("(* = flag bit 0, bonus-stage candidate)")
    _p("anchors: " + ", ".join(f"{k} {'ok' if v['ok'] else 'MISMATCH'}" for k, v in m["anchors"].items()))
    if args.out:
        w = R.OutputWriter(args.out, [])
        w.write_text("stage_map.json", R.dumps(m))
        w.write_text("stage_map.md", stage_map_markdown(m))
        _p(f"wrote stage_map.json and stage_map.md to {w.out_dir}")
    for pr in m["problems"]:
        _p(f"PROBLEM: {pr}")
    return 1 if m["problems"] else 0


def cmd_stage_models(args) -> int:
    from .stagemodel import survey
    rom = _load_rom(args.rom)
    rep = survey(rom)
    _p(f"stage files: {rep['stage_files']}, models checked: {rep['models_checked']}, "
       f"without model: {len(rep['stage_files_without_model'])} "
       f"({len(rep['stage_files_without_model_that_have_a_name'])} of them have a name in the game's table)")
    _p(f"triangle-count mismatches: {len(rep['triangle_count_mismatches'])}, parse errors: {len(rep['parse_errors'])}")
    _p("object height above the model surface below it:")
    for k, v in rep["object_height_above_model_surface"].items():
        _p(f"  {k:7s} " + ", ".join(f"{b}: {n}" for b, n in sorted(v.items())))
    _p("group transform vs model node of the same name: " +
       ", ".join(f"{k}: {n}" for k, n in rep["group_transform_vs_model_node"].items()))
    if args.out:
        w = R.OutputWriter(args.out, [])
        w.write_text("stage_model_survey.json", R.dumps(rep))
        _p(f"wrote stage_model_survey.json to {w.out_dir}")
    for e in rep["parse_errors"]:
        _p(f"PROBLEM: {e}")
    return 1 if rep["parse_errors"] or rep["triangle_count_mismatches"] else 0


def cmd_cells(args) -> int:
    from collections import Counter
    from .stc.cells import survey_stage
    rom = _load_rom(args.rom)
    keys = [s["stage_key"] for w in rom.stage_map()["worlds"] for s in w["stages"]] if args.main_only else \
        sorted(p[len("stcoli/"):-len("_stc.lz")] for p in rom.id_by_path if p.startswith("stcoli/"))
    total = Counter()
    for k in keys:
        data = rom.stage_file_decoded(k)
        total.update(survey_stage(data, StageDocument(data, k)))
    _p(f"stages: {len(keys)}" + (" (main game)" if args.main_only else " (all stage files)"))
    for k, v in sorted(total.items()):
        _p(f"  {k}: {v}")
    if args.out:
        w = R.OutputWriter(args.out, [])
        w.write_text("cell_survey.json", R.dumps({"schema": "trcore.cell_survey/1", "main_only": args.main_only,
                                                  "stages": len(keys), "counts": dict(sorted(total.items()))}))
    return 0


def cmd_collision(args) -> int:
    from collections import Counter
    from .stagemodel import load_stage_model
    from .stc.collision import survey_stage as coll_survey
    rom = _load_rom(args.rom)
    keys = [s["stage_key"] for w in rom.stage_map()["worlds"] for s in w["stages"]] if args.main_only else \
        sorted(p[len("stcoli/"):-len("_stc.lz")] for p in rom.id_by_path if p.startswith("stcoli/"))
    total = Counter()
    for k in keys:
        sm = load_stage_model(rom, k)
        total.update(coll_survey(rom.stage_file_decoded(k), sm["triangles"] if sm else None))
    _p(f"stages: {len(keys)}" + (" (main game)" if args.main_only else " (all stage files)"))
    for k, v in sorted(total.items()):
        _p(f"  {k}: {v}")
    if args.out:
        w = R.OutputWriter(args.out, [])
        w.write_text("collision_survey.json", R.dumps({"schema": "trcore.collision_survey/1", "main_only": args.main_only,
                                                       "stages": len(keys), "counts": dict(sorted(total.items()))}))
    return 0


def _source_bytes(path: str) -> bytes:
    from . import lz10
    with open(path, "rb") as fh:
        data = fh.read()
    if data[:4] == b"LCTS":
        return lz10.encode(data)          # decoded stage: compress like the game does
    if data[:1] == b"\x10":
        lz10.decode(data)                 # must be a valid LZ10 stream
        return data
    raise ValueError("source must be a decoded .stc (LCTS) or an LZ10-compressed file")


def cmd_rom_replace(args) -> int:
    from . import romwrite
    try:
        data = _source_bytes(args.source)
        rec = romwrite.write_rom_copy(args.rom, args.out, args.nitro_path, data, force_relocate=args.relocate)
    except (romwrite.RomWriteRefused, ValueError) as exc:
        _p(f"refused: {exc}")
        return 1
    r0 = rec["replacements"][0]
    _p(f"wrote {args.out}  ({r0['placement']}, {r0['old']['size']} -> {r0['new']['size']} bytes)")
    _p(f"output sha1 {rec['output_rom']['sha1']}  identical to input: {rec['output_identical_to_input']}")
    _p(f"patch record: {rec['record_path']}")
    return 0


def cmd_make_gate(args) -> int:
    """Gate ROM: W1-1 file moved (same bytes); W1-2 re-encoded differently (same content)."""
    from . import lz10, romwrite
    rom = _load_rom(args.rom)
    m = rom.stage_map()
    s11 = m["worlds"][0]["stages"][0]
    s12 = m["worlds"][0]["stages"][1]
    alt = lz10.encode(rom.stage_file_decoded(s12["stage_key"]), max_len=17)
    if alt == rom.file_bytes(s12["file"]):
        _p("alternative encoding came out identical; cannot build a meaningful gate")
        return 1
    note = (f"Gate ROM. W1-1 {s11['stage_key']}: identical compressed bytes, moved to the end of the ROM. "
            f"W1-2 {s12['stage_key']}: different but equivalent LZ10 encoding (max match 17). "
            "Both must play exactly like the original.")
    try:
        rec = romwrite.write_rom_copy_multi(args.rom, args.out, [
            (s11["file"], rom.file_bytes(s11["file"]), True),
            (s12["file"], alt, False)], note=note)
    except romwrite.RomWriteRefused as exc:
        _p(f"refused: {exc}")
        return 1
    for r in rec["replacements"]:
        _p(f"  {r['file']}: {r['placement']} at {r['new']['start']}, {r['old']['size']} -> {r['new']['size']} bytes")
    _p(f"wrote {args.out}  sha1 {rec['output_rom']['sha1']}")
    _p(f"patch record: {rec['record_path']}")
    return 0


def cmd_stage_edit(args) -> int:
    from . import editplan
    from .stc.edit import EditRefused
    from .romwrite import RomWriteRefused
    try:
        plan = editplan.load_plan(args.plan)
        rec = editplan.build_edit_rom(args.rom, args.out, plan)
    except (EditRefused, RomWriteRefused, ValueError) as exc:
        _p(f"refused: {exc}")
        return 1
    for st in rec["stage_edits"]:
        _p(f"{st['where'] or st['file']}:")
        for e in st["edits"]:
            _p(f"  {e['target']} @{e['offset']}: {e['old_value']} -> {e['new_value']}")
        if st.get("objects"):
            for op in st["objects"]["ops"]:
                where = op.get("position") or op.get("to") or op.get("expect_position")
                _p(f"  {op['op']} {op['kind']}" + (f" {op['index']}" if "index" in op else "") + f" at {where}")
            for k, v in st["objects"]["cells"].items():
                _p(f"    {k}: {v['objects']} object(s), {v['cells_rewritten']} cell list(s) rewritten")
            _p(f"    decoded size {st['decoded_size']} -> {st['decoded_size_after']}, "
               f"banana value {st['objects']['banana_value']}")
        if st.get("collision"):
            for op in st["collision"]["ops"]:
                counts = ", ".join(f"{k} {v}" for k, v in op.items()
                                   if isinstance(v, int) and k != "group" and v)
                _p(f"  collision {op['op']} (group {op['group']}){': ' + op['label'] if op.get('label') else ''}")
                _p(f"    {counts}")
            for g, gr in st["collision"]["groups"].items():
                tabs = ", ".join(f"{k} {v['kept']}+{v['new']}" for k, v in gr["tables"].items())
                grid = gr["grid"]
                _p(f"    group {g}: tables (kept+new) {tabs}; grid {grid['before'][0]}x{grid['before'][1]}"
                   + (f" -> {grid['after'][0]}x{grid['after'][1]}" if grid["grown"] else "")
                   + f"; {gr['cells_rewritten']} cell(s) rewritten")
            _p(f"    decoded size {st['decoded_size']} -> {st['decoded_size_after']}")
        if st.get("model"):
            ps = st["model"].get("pos_scale")
            if ps:
                _p(f"  model: position scale {ps['pos_scale_before']:g} -> {ps['pos_scale_after']:g} (drawing range "
                   f"+-{ps['drawing_range_before']:g} -> +-{ps['drawing_range_after']:g}); {ps['vertices']} vertices "
                   f"re-encoded, moved at most {ps['max_vertex_move']:.5f}")
            if st["model"].get("shapes_cleared"):
                _p(f"  model: {len(st['model']['shapes_cleared'])} shape(s) cleared (draw nothing)")
            for op in st["model"]["ops"]:
                if not op.get("polygons_added"):
                    continue
                _p(f"  model: {op['polygons_added']} polygon(s) added to shape {op['shape']} "
                   f"(read back within {op['readback_max_error']:.4f})")
                for sub in op.get("ops", []):
                    _p(f"    {sub['polygons']} x {sub.get('label') or ''}")
            _p(f"    {st['model']['file']}: {st['model']['size_before']} -> {st['model']['size_after']} bytes")
        if st.get("replaced_with_new_stage"):
            ns = st["replaced_with_new_stage"]
            pf = ns["platform"]
            _p(f"  replaced by a NEW STAGE: platform x {pf['x']} z {pf['z']} top {pf['top']} (shape {ns['shape']}), "
               f"start {ns['start']['position']} facing {ns['start']['heading']}")
        if st.get("header"):
            _p(f"  header bounds: {st['header']['bounds_before']} -> {st['header']['bounds_after']}")
        if st.get("animation"):
            an = st["animation"]
            for op in an["ops"]:
                extra = {k: v for k, v in op.items() if k not in ("op", "label") and v is not None}
                _p(f"  animation {op['op']}: {extra}")
            _p(f"    {an['file']}: {an['frames_before']} -> {an['frames_after']} frames, "
               f"{an['size_before']} -> {an['size_after']} bytes")
    nm = rec.get("names")
    if nm:
        _p("names (ARM9 text):")
        for k, v in nm["stages_changed"].items():
            _p(f"  stage {k}: {v['from']!r} -> {v['to']!r}")
        for k, v in nm["worlds_changed"].items():
            to = v["to"]
            shown = next(iter(to.values())) if len(set(to.values())) == 1 else to
            _p(f"  world {k}: {v['from']!r} -> {shown!r}" + (" (all languages)" if len(to) == 5 and
                                                               len(set(to.values())) == 1 else ""))
        sa, wp = nm["stage_area"], nm["world_pool"]
        _p(f"  stage-name area: {sa['used_after']} of {sa['bytes']} bytes used (before: {sa['used_before']}); "
           f"world-name pool: {wp['used_after']} of {wp['bytes']} bytes; {nm['bytes_changed']} ARM9 bytes changed")
    for r in rec["replacements"]:
        _p(f"  {r['file']}: {r['placement']}, {r['old']['size']} -> {r['new']['size']} bytes")
    if rec.get("arm9_patches"):
        ps = rec["arm9_patches"]
        _p(f"  ARM9: {len(ps)} area(s) patched in place, {sum(r['size'] for r in ps)} bytes "
           f"({ps[0]['address']} .. {ps[-1]['address']})")
    _p(f"wrote {args.out}  sha1 {rec['output_rom']['sha1']}")
    _p(f"patch record: {rec['record_path']}")
    return 0


def cmd_names(args) -> int:
    from .names import LANGS, NamesError, read_names
    try:
        t = read_names(_load_rom(args.rom), strict=False)
    except NamesError as exc:
        _p(f"refused: {exc}")
        return 1
    for w in range(12):
        names = t.worlds[w]
        _p(f"World {w + 1}: {names[0]}" + ("" if len(set(names)) == 1 else
                                          "  (" + ", ".join(f"{l}: {n}" for l, n in zip(LANGS, names)) + ")"))
        _p("   " + " | ".join(f"{w + 1}-{s + 1} {t.stages[w * 10 + s]}" for s in range(10)))
    d = t.as_dict()
    _p(f"stage-name area: {d['stage_area_used']} of {d['stage_area_bytes']} bytes used; "
       f"world-name pool: {d['world_pool_bytes']} bytes")
    return 0


def cmd_ramdump_compare(args) -> int:
    from . import ramdump
    rom = _load_rom(args.rom)
    with open(args.dump, "rb") as fh:
        dump_bytes = fh.read()
    a = ramdump.analyze_dump(dump_bytes, rom)
    _p(f"stage {a['stage_id_loaded']} ({a['stage_key']}) at {a['stage_data_ptr']}, {a['expected_size']} bytes")
    w = a["words"]
    _p(f"words: {w['equal']} equal, {w['relocated']} offset->pointer, {w['other']} other (of {w['total']}); "
       f"tail equal: {a['tail_bytes_equal']}")
    for k, v in a["relocated_fields"].items():
        _p(f"  offset->pointer: {k} x{v}")
    for d in a["other_diffs"][:25]:
        _p(f"  other: {d['offset']} {d['field']}: file {d['file']} ram {d['ram']}")
    result = {k: v for k, v in a.items() if not k.startswith("_")}
    model = ramdump.analyze_model(dump_bytes, rom, a["stage_key"])
    result["model"] = model
    if model.get("found"):
        _p(f"model at {model['address']}: {model['words_different']} of {model['words_total']} words differ "
           f"({model['different_in_material_block']} in the material block, {model['different_elsewhere']} elsewhere); "
           f"display lists equal: {model['display_lists_equal']}")
    else:
        _p(f"model: not found ({model.get('reason')})")
    nm = ramdump.analyze_names(dump_bytes, rom)
    result["names"] = nm
    if nm.get("checked"):
        _p(f"names in memory: {nm['ranges_equal']} of {nm['ranges']} areas equal to the ROM's ARM9"
           + (f"; first difference at {nm['first_difference']}" if nm.get("first_difference") else ""))
    anim = ramdump.analyze_animation(dump_bytes, rom, a["stage_key"])
    result["animation"] = anim
    if anim.get("found"):
        _p(f"animation at {anim['address']}: {anim['words_different']} of {anim['words_total']} words differ; "
           f"equal to the file: {anim['equal']}")
    else:
        _p(f"animation: not found ({anim.get('reason')})")
    code = 0
    if args.against:
        with open(args.against, "rb") as fh:
            b = ramdump.analyze_dump(fh.read(), _load_rom(args.against_rom or args.rom))
        c = ramdump.compare_two(a, b)
        result["against"] = {"dump": os.path.basename(args.against), **c}
        _p(f"against {os.path.basename(args.against)}: same stage {c['same_stage']}, "
           f"identical after pointer normalisation: {c['identical_after_normalizing_pointers']} "
           f"({c['differing_words']} differing words)")
        code = 0 if c["identical_after_normalizing_pointers"] else 1
    if args.json:
        with open(args.json, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(R.dumps(result))
        _p(f"wrote {args.json}")
    return code


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="trcore", description=f"{TOOL_NAME} {__version__}: read-only "
                                 "research core for Touch & Roll stage data.")
    ap.add_argument("--version", action="version", version=f"{TOOL_NAME} {__version__}")
    sub = ap.add_subparsers(dest="command")
    p = sub.add_parser("index", help="classify and pair every file under data/")
    p.add_argument("--root", default=".", help="repository root (default: current directory)")
    p.add_argument("--out", default=None, help="also write corpus_index.json into this directory")
    p.add_argument("--fail-on-warning", action="store_true")
    p.set_defaults(func=cmd_index)
    p = sub.add_parser("validate", help="index + parse all stage files + reports + matrix")
    p.add_argument("--root", default=".")
    p.add_argument("--out", default=DEFAULT_OUT, help=f"output directory (default: {DEFAULT_OUT})")
    p.set_defaults(func=cmd_validate)
    p = sub.add_parser("roundtrip", help="no-op export identity check over the whole corpus")
    p.add_argument("--root", default=".")
    p.set_defaults(func=cmd_roundtrip)
    p = sub.add_parser("inspect", help="summarize one .stc or LZ10-compressed stage file")
    p.add_argument("file")
    p.add_argument("--json", default=None, help="write the full stage report to this new .json path")
    p.set_defaults(func=cmd_inspect)
    p = sub.add_parser("hexview", help="hex + word view of a decoded range (max 0x1000 bytes)")
    p.add_argument("file")
    p.add_argument("--offset", type=lambda s: int(s, 0), default=0)
    p.add_argument("--length", type=lambda s: int(s, 0), default=0x100)
    p.set_defaults(func=cmd_hexview)
    p = sub.add_parser("rom-info", help="ROM identity, header CRC, file count, trcore profile")
    p.add_argument("rom")
    p.set_defaults(func=cmd_rom_info)
    p = sub.add_parser("stagemap", help="world/stage -> stage ID -> file map read from the ROM")
    p.add_argument("rom")
    p.add_argument("--out", default=None, help="write stage_map.json and stage_map.md here")
    p.set_defaults(func=cmd_stagemap)
    p = sub.add_parser("stage-models", help="check every stage model against its stage file")
    p.add_argument("rom")
    p.add_argument("--out", help="directory for stage_model_survey.json")
    p.set_defaults(func=cmd_stage_models)
    p = sub.add_parser("cells", help="check every stage's collision cell lists against the known rules")
    p.add_argument("rom")
    p.add_argument("--main-only", action="store_true", help="only the 120 stages used by the 12 worlds")
    p.add_argument("--out", help="directory for cell_survey.json")
    p.set_defaults(func=cmd_cells)
    p = sub.add_parser("collision", help="decode every stage's collision geometry and check it against the rules")
    p.add_argument("rom")
    p.add_argument("--main-only", action="store_true", help="only the 120 stages used by the 12 worlds")
    p.add_argument("--out", help="directory for collision_survey.json")
    p.set_defaults(func=cmd_collision)
    p = sub.add_parser("rom-replace", help="write a NEW ROM copy with one NitroFS file replaced")
    p.add_argument("rom", help="input ROM (never modified)")
    p.add_argument("out", help="new output .nds path (must not exist)")
    p.add_argument("nitro_path", help="file inside the ROM, e.g. stcoli/smb1_be01_stc.lz")
    p.add_argument("source", help="decoded .stc (compressed for you) or an LZ10 file")
    p.add_argument("--relocate", action="store_true", help="move the file to the end even if it fits")
    p.set_defaults(func=cmd_rom_replace)
    p = sub.add_parser("make-gate", help="build the round-trip gate ROM copy (see docs)")
    p.add_argument("rom")
    p.add_argument("out")
    p.set_defaults(func=cmd_make_gate)
    p = sub.add_parser("stage-edit", help="apply an edit plan (JSON) and write a NEW ROM copy")
    p.add_argument("rom", help="input ROM (never modified)")
    p.add_argument("out", help="new output .nds path (must not exist)")
    p.add_argument("plan", help="edit plan JSON (see trcore/editplan.py)")
    p.set_defaults(func=cmd_stage_edit)
    p = sub.add_parser("names", help="list the world and stage names stored in a ROM")
    p.add_argument("rom")
    p.set_defaults(func=cmd_names)
    p = sub.add_parser("ramdump-compare", help="compare a BizHawk Main RAM dump with the expected stage file")
    p.add_argument("dump")
    p.add_argument("--rom", required=True, help="the ROM that was running when the dump was taken")
    p.add_argument("--against", default=None, help="a second dump (e.g. from the original ROM) to compare")
    p.add_argument("--against-rom", default=None, help="ROM for the second dump (default: --rom)")
    p.add_argument("--json", default=None, help="write the full comparison to this .json path")
    p.set_defaults(func=cmd_ramdump_compare)
    return ap


def main(argv: Optional[List[str]] = None) -> int:
    from .lz10 import LZ10EncodeError, LZ10Error
    from .ramdump import DumpError
    from .nitro.nsbmd import NitroError
    from .rom import RomError
    ap = build_parser()
    args = ap.parse_args(argv)
    if not getattr(args, "func", None):
        ap.print_help()
        return 2
    try:
        return args.func(args)
    except BrokenPipeError:            # output piped into e.g. `head`: stop quietly
        try:
            sys.stdout = open(os.devnull, "w")
        except OSError:
            pass
        return 0
    except (OSError, R.OutputRefused) as exc:
        _p(f"error: {exc}")
        return 2
    except (RomError, DumpError, LZ10Error, LZ10EncodeError, NitroError) as exc:
        _p(f"error: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
