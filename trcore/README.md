> In this repository `trcore/` is the editor's file-format core. It comes from
> [Touch & Roll Research](https://github.com/gitouko/Touch-Roll-Research), where the `docs/` and `tools/`
> paths below are.

# trcore — research core and ROM-copy tools

`trcore` indexes the extracted Touch & Roll assets by **content**, decodes and encodes LZ10, parses
every `.stc` stage file into a lossless, evidence-annotated document, reads the stage map from the
ROM, and can write **new ROM copies** with replaced files. It never modifies your original ROM, the
repository's `data/`, or anything it did not create.

Requirements: Python 3.8 or newer, standard library only (the editor in `tredit/` additionally needs
Python 3.10+ and PySide6). Run everything from the repository root.

## Commands

```bat
:: one click: index + parse all stages + reports into generated\trcore\
run_trcore_validate.bat

py -3 -m trcore index                       :: classify and pair every file under data\
py -3 -m trcore validate --out generated\trcore
py -3 -m trcore roundtrip                   :: no-op export identity check, whole corpus
py -3 -m trcore inspect data\raw\smb1_ex03.stc
py -3 -m trcore inspect data\grouped\smb1_ex03\stage.stc --json ex03.json
py -3 -m trcore hexview data\raw\smb1_be02.stc --offset 0x100 --length 0x70
py -3 -m unittest discover -s tests -t . -v :: test suite (about 25 s with data\, under 1 s without)

:: ROM tools (read the ROM; write only NEW copies)
py -3 -m trcore rom-info  "Touch & Roll (USA).nds"
py -3 -m trcore stagemap  "Touch & Roll (USA).nds" --out generated\trcore
py -3 -m trcore rom-replace "Touch & Roll (USA).nds" my_copy.nds stcoli/smb1_be01_stc.lz edited_be01.stc
py -3 -m trcore make-gate "Touch & Roll (USA).nds" TR_gate_W1-1_W1-2.nds
py -3 -m trcore stage-edit "Touch & Roll (USA).nds" TR_edit1_World1.nds docs\experiments\edit1_world1\plan.json
py -3 -m trcore ramdump-compare dump.bin --rom "Touch & Roll (USA).nds"
py -3 -m trcore stage-models "Touch & Roll (USA).nds" --out generated\trcore   :: model reader + floor-fit check
py -3 -m trcore cells "Touch & Roll (USA).nds" --main-only                     :: collision cell list rules
```

The desktop editor built on trcore lives in `tredit/`; start it with `run_editor.bat` (see
`docs/editor_v1.md`).

Set `TRCORE_ROM` to your ROM path to also run the ROM tests (and `pip install lupa` to run the
BizHawk script test under real Lua). See `docs/round_trip_gate.md` for the in-game gate and
`tools/bizhawk/trcore_dump_stage.lua` for the memory-dump script.

Exit codes: `0` no errors · `1` errors found · `2` usage or I/O problem (for example an output
directory inside `data\`).

## What `validate` writes (all under `--out`, default `generated\trcore\`)

| File | Contents |
|---|---|
| `corpus_index.json` | every file: layer, stage key, container (from bytes), signature, size, SHA-256, Git blob id, LZ10 stats, issues; cross-layer pairs, duplicates, missing copies |
| `stages\<stage_key>.<sha8>.json` | one report per stage key, named by key **and** decoded-content hash, so reports never overwrite each other |
| `validation_matrix.json` / `.md` | pass/fail/n.a. of every structural check over the unique stage contents, failing stage keys, layout variants, round-trip totals |
| `run_summary.json` | which files were created / replaced / unchanged, older files not produced by this run, problems, exit code |

Reports contain no timestamps or absolute paths: two runs over the same data are byte-identical
(apart from `run_summary.json`). `generated/` is git-ignored; reports are derived artifacts, not
sources of truth.

## Guarantees

* Compression state and file type come from the bytes, never from the name or folder.
* A stage key comes from the original asset name, or from a grouped directory name **only** when an
  originally-named copy with the same content exists; `stage`, `model`, … are never stage keys.
* Every read is bounds-checked; malformed input yields diagnostics, not crashes (fuzz-tested).
* `StageDocument` keeps the original bytes; parsed values are views with their offsets;
  unclaimed bytes are reported as zero-fill or opaque ranges, never dropped.
* No-op export is byte-identical for every stage source (plain and compressed).

## ROM writing rules

`rom-replace` / `make-gate` refuse to write over the input ROM or any existing file, build the new ROM
in memory, verify that nothing but the target files changed (plus the used-ROM size and header CRC
when a file has to move), and write a `.patch.json` record next to the output. A decoded `.stc` given
as the source is compressed exactly the way the game's own compressor does it.

## Editing stage fields

`stage-edit` applies a JSON edit plan (format in `trcore/editplan.py`). Only fields of start records
and object-pool records can be edited (position, matrix, translation and the flag words); every edit
must state the field's current value, and the edit is refused if any byte outside the edited fields
would change or the stage's parsed structure would differ. Each run writes a new ROM copy plus a
`.patch.json` with old/new bytes, offsets, hashes and the stated prediction.

## Adding, removing and moving objects (plan schema `trcore.edit_plan/2`)

A stage entry can also carry `"objects": [...]` with `add`, `remove` and `move` operations (format in
`trcore/stc/objects.py`). The changed pool is rebuilt as one contiguous array at the end of the stage
file, slices and header totals are updated, and that object type's collision cell lists are rebuilt
for the affected groups. Nothing that already exists is moved. Every result is checked before
writing (structure, lists, coverage, offsets, the 255 banana-value limit). Status: confirmed in-game
(edit test 3).

## Collision and model geometry (plan schema `trcore.edit_plan/3`)

A stage entry can also carry `"collision": [...]` (`remove_box`, `add_polygons`; format in
`trcore/stc/geometry_edit.py`) and `"model": [...]` (`add_polygons` to a named shape of the stage's model;
`trcore/nitro/nsbmd_write.py`). Collision tables are rebuilt at the end of the stage file with the kept
records unchanged and first, cell lists for bits 0–3 are rebuilt, and the collision grid grows when new
geometry lies outside it (spec §17.3). Model polygons are drawn with the shape's own material and texture;
the model file is replaced too. Every result is re-read and checked before writing. Status: confirmed
in-game (edit test 4, `docs/experiments/edit4_collision/`). `ramdump-compare` also checks the stage's
model in memory.

## Animations and drawing range (plan schema `trcore.edit_plan/4`)

A stage entry can carry `"animation": [...]` for the stage's `model/stage/<key>_nsbca.lz`:
* `scale_translation` `{nodes | "animated", axis, factor}`;
* `mirror_rotation` `{node, axis}`;
* `retime` `{factor}` (2–8; the animation becomes (frames − 1) × factor + 1 frames long).

The model list accepts `{"op": "set_pos_scale", "scale": s}` as its first op. It changes the model's position
scale and re-encodes every vertex, so polygons can be drawn within ±8 × s (spec §15). Every edit is read
back and compared before writing. Status: confirmed in-game (edit test 6,
`docs/experiments/edit6_animation/`). `ramdump-compare` also checks the stage's animation in memory.

## World and stage names (plan schema `trcore.edit_plan/5`)

A plan can carry a top-level `"names"`: `{"stages": {"1-1": "NEW NAME"}, "worlds": {"1": "Name"}}`. A world
also accepts names per language: `{"en": …, "de": …, "fr": …, "es": …, "it": …}`.
* The names live in the ARM9 program (spec §20). `trcore/names.py` repacks their two areas and updates the
  pointers. `romwrite` then patches the ARM9 in place and verifies that nothing else changed.
* Stage names: capitals, digits, space and - ? ! ' : ( # ) . + = & / , * ; @ < > (lower case crashes the
  game at stage start). World names: printable ASCII. "%" is refused in both.
* `py -3 -m trcore names <ROM>` lists the names; `ramdump-compare` also compares the name areas in memory.
* Status: confirmed in-game (edit tests 7 and 7b, `docs/experiments/edit7_names/`, `edit7b_names/`).

## Replacing a stage with a new one (plan schema `trcore.edit_plan/6`)

A stage entry can carry `"replace_with": {"new_stage": {"platform": {...}, "start": {...}, "shape": ...}}`
together with `"stage_key"` and `"model_file"`. The stage is first rebuilt as a new stage with one
platform and the start (`trcore/newstage.py`, `build_new_stage`, exactly what edit test 5 confirmed). The
entry's other edits (fields, objects, collision, model, animation) then apply to that new stage instead of
the original, and the new model is always written. All values are optional:
* `platform` `{x: [x0, x1], z: [z0, z1], top, thickness}`; default 10 × 6 at height 0;
* `start` `{position: [x, y, z], heading}`; default 1.5 in from the platform's −x end, facing along it;
* `shape`: the model shape whose texture the platform uses; default the shape with the most floor.

Status: the stage it builds is byte-identical to edit test 5's 1-2 (N), which passed in-game.

## Keyframes (plan schema `trcore.edit_plan/7`)

A stage's `"animation"` list can hold the keyframe ops instead of the edit-test-6 ops (not mixed):
* `{"op": "set_length", "frames": n}`: first op only. Every moving track is stretched to n frames (2–8191).
* `{"op": "set_keys", "node": i, "channel": "tx"|"ty"|"tz"|"rx"|"ry"|"rz", "keys": [...]}`: the channel
  becomes the keys baked to one value per frame.
  * Keys: `{"frame", "value", "interp", "handles"}` (`trcore/keyframes.py`).
  * Curve types: linear, ease_in, ease_out, ease_in_out, bezier, smooth, hold.
  * Turns are in degrees, applied X, then Y, then Z.
  * A fixed axis becomes a moving one.
  * A turn rebuilds the node's rotation track: new rotations go into the pivot table when they turn about
    one axis, otherwise into the compressed table.

Every result is read back and compared frame by frame. Status:
* keys on moving axes and new lengths: confirmed in-game by edit test 6;
* fixed axes, turning and a 64.7 KB animation: edit test 8 (`docs/experiments/edit8_keyframes/`), passed
  2026-11-08.

## Evidence

The editor GUI lives in `tredit/`. Field meanings are confirmed only where
the spec's §14 lists an in-game test (start, goals incl. red flag, bumper position, bananas incl. bunch);
see
`docs/research_notes/stc_format_spec.md` for the evidence and the experiments that confirm them.

## Layout

```
trcore/lz10.py          strict LZ10 decoder + verified encoder matching the game's compressor
trcore/rom.py           read-only DS ROM access, ROM profile, stage map
trcore/romwrite.py      verified "replace a file in a NEW ROM copy"
trcore/stc/edit.py      same-size field edits of known record fields, with structure checks
trcore/stc/objects.py   add / remove / move goals, bumpers, bananas (pools + collision cell lists)
trcore/stc/cells.py     per-group collision cell grid: reader, footprint rule, coverage check
trcore/stc/collision.py collision tables reader (quads, triangles, edges, points) + contact probe
trcore/stc/geometry_edit.py  remove / add collision geometry, rebuild lists, grow the grid
trcore/nitro/nsbmd_write.py  add polygons to / clear shapes of a stage model; change its position scale
trcore/stc/stagehead.py header values edits may set (intro-camera bounds)
trcore/newstage.py      replace a stage with a new one (one platform + start; Create New Stage)
trcore/nitro/nsbca.py   node animations (moving parts): tracks, frames, poses
trcore/nitro/nsbca_write.py  edit node animations (scale, mirror, retime) and rebuild the file
trcore/names.py         world and stage names (ARM9 text): read, repack, verified ARM9 patch
trcore/keyframes.py     keys and curve types baked to frames; turns as angles; keys from samples
trcore/nitro/nsbtx.py   textures -> RGBA, material texture/palette bindings
trcore/editplan.py      JSON edit plan -> new ROM copy + patch record
trcore/ramdump.py       compare a BizHawk Main RAM dump with the expected stage file
trcore/nitro/nsbmd.py   read-only NITRO model reader (nodes, structure code, display lists -> triangles)
trcore/stagemodel.py    a stage's own model in stage coordinates + the whole-ROM floor-fit survey
trcore/binreader.py     bounds-checked LE reads, Q20.12
trcore/assets.py        content classification, name parsing, corpus index
trcore/stc/layout.py    layout constants + evidence registry (E01..E21, in-game confirmations)
trcore/stc/document.py  StageDocument: views, pools, groups, variants, coverage, checks
trcore/report.py        deterministic JSON/Markdown, safe output writer
trcore/cli.py           command line
tests/                  unittest suite; synthetic fixtures (no game bytes) + corpus tests
tools/bizhawk/          BizHawk Lua script that dumps memory when a stage loads
```
