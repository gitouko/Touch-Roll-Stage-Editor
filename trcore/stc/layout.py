"""Evidence-annotated layout constants for decompressed LCTS (.stc) streams.

Every structural constant used by the parser is defined here together with the
evidence record that justifies it. Levels follow the project contract:
  0 historical lead, 1 directly observed bytes, 2 cross-sample structural
  corroboration, 3 controlled mutation, 4 confirmed in-game.
The "level" of each entry is its structural evidence; meanings confirmed by
controlled in-game edits (Level 4, for the stated meaning only) are listed in
IN_GAME below and attached to the entries as "in_game". Other labels (grid,
table_7C ...) are hypothesis names only.

Basis for every level-2 entry: all 183 unique decompressed .stc files in
gitouko/touch-roll-research @ 4ddcea8c22373730d263639433bdc59448b06060.
See docs/research_notes/stc_format_spec.md for the full write-up.
"""
from __future__ import annotations

# "in_game": confirmations by controlled in-game edits (Level 4 for the stated meaning only).
EVIDENCE = {
    "E01": {"level": 2, "claim": "Bytes 'LCTS' at 0x00", "basis": "183/183"},
    "E02": {"level": 2, "claim": "u32 at 0x04 == 0x12 (meaning unknown)", "basis": "183/183"},
    "E03": {"level": 2, "claim": "0x1C..0x33 = six Q20.12 values, min<=max per axis (bounds-like)",
            "basis": "min<=max 183/183; semantics inferred"},
    "E04": {"level": 2, "claim": "u32(0x18) = base F of an array of u32(0x14) group descriptors, 0xCC bytes each",
            "basis": "the game's loader (ARM9 0x02041D40..) loops u32(0x14) times stepping 0xCC from u32(0x18); "
                     "in all 25 files where u32(0x14) > u32(0x34) the extra descriptors are valid "
                     "(names resolve, tails zero, det>0). An earlier draft wrongly used u32(0x34)"},
    "E05": {"level": 2, "claim": "u32(0x38) start-record count, u32(0x3C) start-record offset; record 0x10 bytes: "
                                 "xyz Q20.12 + u32 angle-like word (16-bit binary angle candidate)",
            "basis": "182/183 main-game files (exception: variant E20); 12 four-player party stages in the ROM pack 4 "
                     "records at a 0x10 stride. The historical 0x1C size was 0x10 plus padding to a 32-byte boundary"},
    "E06": {"level": 2, "claim": "u32(0x40/0x44/0x48) = total records in pools A/B/C",
            "basis": "total >= referenced 181/183 per pool; exception is variant E20"},
    "E07": {"level": 2, "claim": "Pool A record 0x34: 3x3 Q20.12 + xyz Q20.12 + u32 word (goal candidate)",
            "basis": "orthonormal 166/167 files; one scaled case compensated by its group scale. "
                     "Historical 0x40 stride refuted (0x34 padded to 32-byte alignment)"},
    "E08": {"level": 2, "claim": "Pool B record 0x30: 3x3 Q20.12 + xyz Q20.12 (bumper candidate)",
            "basis": "11/11 files orthonormal, pools contiguous"},
    "E09": {"level": 2, "claim": "Pool C record 0x10: xyz Q20.12 + u32 word (banana candidate)",
            "basis": "pools contiguous 183/183"},
    "E10": {"level": 2, "claim": "Descriptor (count, offset) slices at +0x94/+0x9C/+0xA4 split pools A/B/C "
                                 "into contiguous per-group runs (objects belong to a group)",
            "basis": "contiguous 183/183, no overlaps; every record belongs to exactly one slice "
                     "(2,454 in group 0, 470 in moving groups)"},
    "E11": {"level": 2, "claim": "Pool A word +0x30 in {0,1} (red-goal candidate; meaning NOT confirmed)",
            "basis": "value 1 in exactly 5 stages, one record each, incl. both user-reported red-goal stages"},
    "E12": {"level": 2, "claim": "Pool C word +0x0C in {0,1} (meaning unknown; bunch hypothesis untested)",
            "basis": "0: 2173 records, 1: 193 records in 58 files"},
    "E13": {"level": 2, "claim": "Descriptor +0x10 3x3 Q20.12 linear map (rotation*scale, det>0), +0x34 xyz",
            "basis": "det>0 in all groups; objects appear to be expressed in their group's frame"},
    "E14": {"level": 2, "claim": "Descriptor +0x40 u16 nx, +0x42 u16 nz, +0x44/+0x48 Q20.12 origin x/z, "
                                 "+0x4C/+0x50 Q20.12 step x/z (grid candidate)",
            "basis": "static-group grid covers header bounds in 151/183"},
    "E15": {"level": 2, "claim": "Descriptor +0x54 = offset of an nx*nz table of 8-byte entries",
            "basis": "table end aligns with next known region in 404/428 groups"},
    "E16": {"level": 2, "claim": "Descriptor (count, offset) tables: +0x7C stride 0x20, +0x84 stride 0x34, "
                                 "+0x8C stride 0x34; +0x74 stride 0x10 per game code (E23; not yet used by this "
                                 "parser). Contents: collision primitives, not decoded",
            "basis": "stride fits 183/183 (+0x7C, +0x8C), 181/181 (+0x84)"},
    "E17": {"level": 2, "claim": "0x4C..0x63 = six Q20.12 values, default (0,0,0,1,1,1); non-default iff u32(0x08)==2",
            "basis": "7/7 non-default files have u32(0x08)==2"},
    "E18": {"level": 2, "claim": "0x64..0xE3 all zero", "basis": "183/183"},
    "E19": {"level": 2, "claim": "Descriptor +0x04/+0x08 = offsets of NUL-terminated ASCII names (usually equal)",
            "basis": "182/183 equal; names like START01, MOT_<stage>b"},
    "E21": {"level": 1, "claim": "u32(0x34) <= group_count; meaning unknown (equal in 158/183 files)",
            "basis": "never larger than u32(0x14) in the corpus"},
    "E20": {"level": 1, "claim": "Variant 'no_start_slot': no valid start pair; pool totals at 0x3C/0x40/0x44",
            "basis": "single file in corpus; totals equal its group slice sums exactly"},
    "E22": {"level": 2, "claim": "Load-time relocation (complete list): header 0x18, 0x3C; descriptor +0x04, +0x08, "
                                 "+0x54, +0x78, +0x80, +0x88, +0x90, +0x98, +0xA0, +0xA8, +0xAC (tree, recursive); "
                                 "each cell entry +0x04 and every offset in the array it points to",
            "basis": "game code ARM9 0x02046CC4..0x02046F74 (USA rev 0); matches the offset->pointer words seen in "
                     "all RAM dumps"},
    "E23": {"level": 2, "claim": "Cell entry = u32 type mask + offset of one list offset per set bit (bit order); "
                                 "lists are sorted u16 indices ending in 0xFFFF; bits 0-3 index tables +0x90/+0x88/+0x80/"
                                 "+0x78 (records 0x34 by corpus fit; 0x34, 0x20, 0x10 by code), bit 4 goals, "
                                 "bit 5 bumpers, bit 6 bananas; "
                                 "indices local to the group's slice; the ball only tests objects listed in its cell",
            "basis": "game code 0x02044C68 (lookup), 0x020444F4 (ball collision), 0x020AC0F4 (banana pickup); all 120 "
                     "main-game stages: every list sorted, unique, in range, every object in >= 1 cell"},
    "E25": {"level": 2, "claim": "Collision tables: +0x8C/+0x90 quads (0x34: u32 0, P, N, dual vectors E1 E2; "
                                 "parallelogram test u<=1, v<=1), +0x84/+0x88 triangles (same record, u+v<=1), "
                                 "+0x7C/+0x80 edges (0x20: start, unit direction, length, u32 0), +0x74/+0x78 corner "
                                 "points (0x10: point, u32 0); faces push the ball out only from the front",
            "basis": "game code 0x020444F4 dispatch, 0x02046460 (quads), 0x02046050 (triangles), 0x02045DE0 (edges), "
                     "0x02045C1C (points); 120 main stages: spare words always 0, normals unit, stored vectors equal "
                     "the ones recomputed from the corners (98% within 2/4096, all normals same sign)"},
    "E26": {"level": 2, "claim": "Collision is generated from the stage model at rest: model parallelogram quads -> "
                                 "quads, other quads -> two triangles (either diagonal), triangles -> triangles; edges "
                                 "and corner points of the faces; cells list each piece within its x/z extent + 0.5",
            "basis": "120 main stages: 96% of collision faces have every corner within 0.02 of a model corner; "
                     "95% of model parallelograms are collision quads; cell rule exact for 96-97% of pieces, "
                     "all within extent + 0.75"},
    "E27": {"level": 2, "claim": "Collision can be edited per group: tables rebuilt at the end (kept records first and "
                                 "unchanged), bit 0-3 lists rebuilt, new pieces listed in extent + 0.75; a ball outside "
                                 "the grid touches nothing, and the grid may grow by whole cells (new cell table); "
                                 "polygons appended to a model shape's display list are drawn with its material",
            "basis": "game code 0x02044CD4 (lookup returns no cell outside the grid), 0x02046CC4 (loader walks "
                     "nx*nz cells); only these two read the grid size; spec section 17.3"},
    "E28": {"level": 2, "claim": "A stage can be rebuilt from scratch inside its own file: all collision and objects "
                                 "removed, every model shape cleared (END_VTXS only), one platform added, start moved, "
                                 "header bounds set; groups, nodes, materials, textures and animations kept",
            "basis": "trcore/newstage.py; spec section 17.4"},
    "E30": {"level": 2, "claim": "World and stage names are ARM9 text (stage-name table 0x020DD71C, world names = "
                                 "messages 34-45 of the message index 0x020D6B00) and can be rewritten in place, "
                                 "repacked, with the pointers updated",
            "basis": "trcore/names.py; spec section 20"},
    "E29": {"level": 2, "claim": "Stage animations (.nsbca) can be edited and rebuilt (values scaled, rotations mirrored "
                                 "and re-encoded, a new length with resampled tracks); moving collision follows the "
                                 "edited animation. A model's position scale can be raised with every vertex "
                                 "re-encoded, widening its drawing range",
            "basis": "trcore/nitro/nsbca_write.py, trcore/nitro/nsbmd_write.py rescale_positions; spec sections 15, 18"},
    "E32": {"level": 2, "claim": "Goal and bumper matrices may hold any rotation; collision faces of any slope and "
                                 "collision triangles can be added; arbitrary triangle meshes can be appended to a "
                                 "model shape; the start record's +0x0E has no visible effect",
            "basis": "tredit/project.py object_rows, geo_polygons; trcore/stc/geometry_edit.py add_polygons"},
    "E33": {"level": 2, "claim": "Goal and bumper matrices may hold a size along each axis (rows scaled), which "
                                 "the game draws (but see E34, E35: only drawn); new "
                                 "polygons can be added to any static shape of a stage model, so with any texture + "
                                 "palette pair its materials use, and with texture coordinates taken from an "
                                 "imported model's UV map",
            "basis": "tredit/project.py object_rows / object_scale, StageState.texture_choices; "
                     "trcore/newstage.py texture_choices; trcore/nitro/nsbmd_write.py edit_model; "
                     "edit test 10 passed in-game"},
    "E31": {"level": 2, "claim": "Animation keys baked to one value per frame: a fixed translation axis can become a "
                                 "track, parts without rotation can get rotation tracks (new pivot and compressed "
                                 "rotations appended), any length, and larger animation files",
            "basis": "trcore/keyframes.py, trcore/nitro/nsbca_write.py set_length / set_keys; spec section 18"},
    "E34": {"level": 2, "claim": "Goal finish line: every frame the game walks all goals of all groups and "
                                 "tests the ball's last move (group frame) against each goal: the goal matrix is "
                                 "moved up by local (0, 1.5, 0), both ball positions are taken into it with the "
                                 "TRANSPOSED matrix (only the inverse of a pure turn), and the goal counts when "
                                 "local z changes sign with |x| <= 1.5 and |y| <= 1.5 at the crossing (either "
                                 "direction). A size s therefore shrinks the line to 1.5 / s and lifts its middle "
                                 "to 1.5 * s_y: a goal taller than about 1.18 is out of reach of a ball on the "
                                 "floor (centre 0.5 up)",
            "basis": "game code 0x020AAED4 (loop), 0x020AA48C (crossing test), 0x0203E768 / 0x0203E650 (transposed "
                     "multiply); emulator runs of the acceptance ROM (tools/emulator/)"},
    "E35": {"level": 2, "claim": "Bumper bounce: a sphere of radius ball radius + 0.3 around t + M * (0, 0.375, 0); "
                                 "the matrix only moves that centre, so a bumper's size changes its drawing and "
                                 "not its bounce",
            "basis": "game code 0x02044DEC; emulator runs (a bumper 3 x 1 x 3 bounces exactly like one at 1 x 1 x 1)"},
    "E24": {"level": 2, "claim": "Header pool totals size runtime arrays (goal 0x2C-byte structs, bumper and banana "
                                 "state bytes, indexed by group order + local index); banana value total is summed by "
                                 "walking `total` 0x10-byte records from group 0's slice, so each pool must stay one "
                                 "contiguous array in group order",
            "basis": "game code 0x020ABF98 (goals), 0x02042334 (bumpers), 0x020ACB10 / 0x020AC404 (bananas)"},
}

HEADER_SIZE = 0xE4          # 0x00..0xE3 (fixed fields + reserved zero block)
HEADER_FIELDS_END = 0x64
DESCRIPTOR_SIZE = 0xCC
START_RECORD_SIZE = 0x10

MAX_GROUPS = 4096           # sanity caps: far above anything observed (max 22)
MAX_START_RECORDS = 64
MAX_POOL_RECORDS = 1 << 16
MAX_TABLE_RECORDS = 1 << 20

# (offset, name, type, evidence)  type in {"magic", "u32", "q12"}
HEADER_FIELDS = [
    (0x00, "magic", "magic", "E01"),
    (0x04, "u32_04", "u32", "E02"),
    (0x08, "u32_08", "u32", "E17"),
    (0x0C, "u32_0C", "u32", None),
    (0x10, "u32_10", "u32", None),
    (0x14, "group_count", "u32", "E04"),
    (0x18, "descriptor_base", "u32", "E04"),
    (0x1C, "bounds_min_x", "q12", "E03"),
    (0x20, "bounds_min_y", "q12", "E03"),
    (0x24, "bounds_min_z", "q12", "E03"),
    (0x28, "bounds_max_x", "q12", "E03"),
    (0x2C, "bounds_max_y", "q12", "E03"),
    (0x30, "bounds_max_z", "q12", "E03"),
    (0x34, "u32_34", "u32", "E21"),
    (0x38, "start_count", "u32", "E05"),
    (0x3C, "start_offset", "u32", "E05"),
    (0x40, "pool_a_total", "u32", "E06"),
    (0x44, "pool_b_total", "u32", "E06"),
    (0x48, "pool_c_total", "u32", "E06"),
    (0x4C, "box2_a_x", "q12", "E17"),
    (0x50, "box2_a_y", "q12", "E17"),
    (0x54, "box2_a_z", "q12", "E17"),
    (0x58, "box2_b_x", "q12", "E17"),
    (0x5C, "box2_b_y", "q12", "E17"),
    (0x60, "box2_b_z", "q12", "E17"),
]
BOX2_DEFAULT = (0.0, 0.0, 0.0, 1.0, 1.0, 1.0)

# Object pools: key, header-total field, descriptor slice offset, stride, candidate label, evidence
POOLS = [
    ("A", "pool_a_total", 0x94, 0x34, "goal", "E07"),
    ("B", "pool_b_total", 0x9C, 0x30, "bumper", "E08"),
    ("C", "pool_c_total", 0xA4, 0x10, "banana", "E09"),
]
# Variant E20 stores the three totals one word earlier.
NO_START_SLOT_TOTAL_OFFSETS = {"A": 0x3C, "B": 0x40, "C": 0x44}

# Descriptor (count, offset) tables with known record stride (contents opaque).
GROUP_TABLES = [
    ("table_74", 0x74, None),
    ("table_7C", 0x7C, 0x20),
    ("table_84", 0x84, 0x34),
    ("table_8C", 0x8C, 0x34),
]
CELL_ENTRY_SIZE = 8

IN_GAME = {
    "E05": "start +0x0C = facing angle (0x10000 = 360 deg, increasing turns left; angle 0 faces -z): "
           "edit test 1, W1-3",
    "E07": "pool A +0x24 = goal position (model and trigger): edit test 1, W1-4",
    "E09": "pool C +0x00 = banana position: edit test 1, W1-1",
    "E12": "pool C +0x0C: 1 = banana bunch worth 5, 0 = single: edit test 1, W1-2",
    "E11": "pool A +0x30: 1 = red goal, 0 = normal goal (colour): edit test 2, W1-1 and W8-1",
    "E08": "pool B +0x24 = bumper position: edit test 2, W5-2",
    "E13": "objects in a moving group are placed in its frame and move with it: edit test 2, W5-6; "
           "matrices are applied transposed (world = M^T * local), goal front = local +z: edit test 2, W1-3",
}
IN_GAME.update({
    "E22": "stage files may grow: pools, lists and cell arrays appended at the end are loaded and relocated: "
           "edit test 3",
    "E23": "the ball only touches objects listed in its cell (control banana not collectable; same move with "
           "lists rebuilt collectable): edit test 3, W1-2",
    "E24": "pools rebuilt contiguously at the end with slices and totals updated: banana/goal/bumper added, "
           "banana/bumper removed, all working: edit test 3, W1-1 and W5-2",
})
IN_GAME.update({
    "E25": "faces push the ball out only where the tables say: collision removed from a cut strip lets the ball fall "
           "through the drawn floor, the cut leaves the rest solid: edit test 4, W1-1",
    "E27": "collision removed (cut at a box), added (invisible floor, solid box) and the grid grown by 5 columns "
           "all work; polygons appended to a model shape are drawn with the stage texture, with or without "
           "collision: edit test 4, W1-1 and W1-2",
})
IN_GAME["E28"] = ("new stages load and play (1-1, 1-2, 1-3, 1-7), also with no goal and no bananas, with 10 moving "
                  "parts emptied, and at y -60 (no fixed fall-out height): edit test 5")
IN_GAME["E30"] = ("world names (any case, longer than before) shown on the world select; stage names in capitals, "
                  "digits, space and - ? ! ' : ( # ) . + = & / , * ; @ < > shown and the stages play, also with "
                  "names on odd addresses; lower case in a stage name crashes the game at stage start: edit tests 7, 7b")
IN_GAME["E29"] = ("1-7 platforms rise twice as high, 1-8's bridge swings the other way, 1-9's platforms move at half "
                  "speed (601-frame rebuilt animation), the ball riding them; 1-1 at position scale 4 looks unchanged "
                  "and draws a solid platform at x 18-22, beyond the old limit of 16; animations, models and stage "
                  "files in memory equal the files: edit test 6")
IN_GAME["E32"] = ("1-1 goals tilted 20 and rolled 30, bumpers tilted 45 and rolled 60; 1-2 a 10-degree ramp and a "
                  "platform turned 30 and banked 8; 1-3 an imported 480-triangle track with rails, an arch decoration "
                  "and a two-sided fin; 1-4 start +0x0E = 0x1000 without visible effect; stage files in memory equal "
                  "the files: edit test 9")
IN_GAME["E31"] = ("1-7 with every curve type, a fixed axis moving in Hold steps and 161 frames; 1-9 platforms that "
                  "had no rotation spin and rock (300 pivot + 279 compressed rotations added), their collision "
                  "following; 1-8's re-keyed turn at 481 frames; 2-9 with a 64,720-byte animation; all four "
                  "animations in memory equal the files: edit test 8")
IN_GAME["E33"] = ("1-1 a goal at 1.5 x 1.5 x 1.5, a narrow goal (0.6 x 1 x 1), a wide flat bumper (2 x 0.5 x 2) and a "
                  "tall one (1 x 2 x 1) are drawn at their sizes (their finish line and bounce do not follow: E34, "
                  "E35; the stage was finished through the narrow goal); 1-5 pads drawn with each of the stage's "
                  "three textures (two shapes the editor never used before); 1-6 a new stage with a three-object OBJ drawn with three textures and "
                  "its own UVs, a pillar and a letter F sized on their own, a textured pad and wall; stage files and "
                  "model display lists in memory equal the files: edit test 10")
IN_GAME["E34"] = ("acceptance test: 2-1's and 2-4's goals at 1.25 x 1.25 x 1.25 cannot be finished (owner, BizHawk); "
                  "in the emulator the same goals finish at heights 1 and 1.15 (also 1.25 / 2 wide at height 1) and "
                  "fail at 1.25 high; 1-1's original goal and 2-3's new goal finish")
IN_GAME["E35"] = ("emulator, acceptance 2-1: a ball passing 0.7 from a bumper's centre bounces and one passing 0.95 "
                  "rolls on, identically for sizes 1 x 1 x 1 and 3 x 1 x 3")
IN_GAME["E05"] += "; start +0x00 = start position: edit test 2, W1-2"
IN_GAME["E05"] += "; screen right = forward x up (facing +x, +z is right; facing -z, -x is left): edit test 4"
IN_GAME["E07"] += "; pool A +0x00 = goal orientation (applied transposed): edit test 2, W1-3"
for _k, _v in IN_GAME.items():
    EVIDENCE[_k]["in_game"] = _v

