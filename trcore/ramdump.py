"""Compare an emulator Main-RAM dump with the stage file the game should have loaded.

The dump is the raw 4 MiB DS Main RAM (0x02000000-0x023FFFFF) written by
tools/bizhawk/trcore_dump_stage.lua. Using the ROM profile, this module reads
the loaded stage ID and the stage-data pointer from the dump, decodes the
expected stage file from the ROM, and classifies every aligned 32-bit word of
the in-RAM copy:

  equal      identical to the file
  relocated  RAM value == file value + load address (an offset the game turned
             into a pointer)
  other      anything else (runtime state, or a real mismatch)

Each differing word is labelled with the trcore field it belongs to.
"""
from __future__ import annotations

import struct
from typing import Dict, List, Optional

from .rom import ARM9_RAM_BASE, MAIN_RAM_SIZE, NdsRom, RomError
from .stc import layout as L
from .stc.document import StageDocument


class DumpError(Exception):
    pass


def _u32(buf: bytes, addr: int) -> int:
    o = addr - ARM9_RAM_BASE
    if o < 0 or o + 4 > len(buf):
        raise DumpError(f"address {addr:#x} outside the dump")
    return struct.unpack_from("<I", buf, o)[0]


def field_labeler(doc: StageDocument):
    """Return f(offset) -> label using header fields, descriptors, records and spans."""
    header = {int(v["offset"], 16): k for k, v in doc.header.items()}
    desc = []
    if doc.groups:
        F = doc.header["descriptor_base"]["value"]
        desc = [(F + g["index"] * L.DESCRIPTOR_SIZE, g["index"]) for g in doc.groups]
    rec_spans = [s for s in doc.spans if s.get("record_stride")]

    def label(off: int) -> str:
        if off in header:
            return f"header.{header[off]}"
        for base, k in desc:
            if base <= off < base + L.DESCRIPTOR_SIZE:
                return f"group{k}+0x{off - base:02X}"
        for s in rec_spans:
            if s["_s"] <= off < s["_e"]:
                stride = int(s["record_stride"], 16)
                return f"{s['labels'][0]}[{(off - s['_s']) // stride}]+0x{(off - s['_s']) % stride:02X}"
        for s in doc.spans:
            if s["_s"] <= off < s["_e"]:
                return f"{s['labels'][0]}+0x{off - s['_s']:X}"
        return "unclaimed"
    return label


def analyze_dump(dump: bytes, rom: NdsRom) -> dict:
    if len(dump) != MAIN_RAM_SIZE:
        raise DumpError(f"dump is {len(dump)} bytes; expected {MAIN_RAM_SIZE} (4 MiB Main RAM)")
    prof = rom.profile()
    if prof is None:
        raise DumpError("ROM has no trcore profile (unknown game code / code fingerprint)")
    loaded = _u32(dump, prof["ram_loaded_stage_id"])
    requested = _u32(dump, prof["ram_requested_stage_id"])
    ptr = _u32(dump, prof["ram_stage_data_ptr"])
    names = rom.stage_names()
    if loaded >= len(names):
        raise DumpError(f"loaded stage id {loaded} is not a valid stage")
    key = names[loaded]
    try:
        expected = rom.stage_file_decoded(key)
    except (RomError, KeyError) as exc:
        raise DumpError(f"cannot read the expected stage file for {key}: {exc}")
    start = ptr - ARM9_RAM_BASE
    if start < 0 or start + len(expected) > len(dump):
        raise DumpError(f"stage pointer {ptr:#x} does not point inside Main RAM")
    ram = dump[start:start + len(expected)]
    if ram[:4] != b"LCTS":
        raise DumpError(f"no 'LCTS' at stage pointer {ptr:#x}")
    doc = StageDocument(expected, key)
    label = field_labeler(doc)
    equal = relocated = other = 0
    reloc_labels: Dict[str, int] = {}
    others: List[dict] = []
    n4 = len(expected) // 4 * 4
    for o in range(0, n4, 4):
        fw = struct.unpack_from("<I", expected, o)[0]
        rw = struct.unpack_from("<I", ram, o)[0]
        if fw == rw:
            equal += 1
        elif fw != 0 and rw == (fw + ptr) & 0xFFFFFFFF:
            relocated += 1
            lab = label(o)
            generic = lab.split("[")[0] + ("+" + lab.split("+")[-1] if "[" in lab else "")
            reloc_labels[generic] = reloc_labels.get(generic, 0) + 1
        else:
            other += 1
            if len(others) < 200:
                others.append({"offset": f"0x{o:X}", "field": label(o), "file": f"0x{fw:08X}",
                               "ram": f"0x{rw:08X}"})
    tail_equal = expected[n4:] == ram[n4:]
    return {
        "schema": "trcore.ramdump_compare/1",
        "stage_id_loaded": loaded, "stage_id_requested": requested, "stage_key": key,
        "stage_data_ptr": f"0x{ptr:08X}", "expected_size": len(expected),
        "words": {"total": n4 // 4, "equal": equal, "relocated": relocated, "other": other},
        "tail_bytes_equal": tail_equal,
        "relocated_fields": dict(sorted(reloc_labels.items())),
        "other_diffs": others,
        "other_diffs_truncated": other > len(others),
        "_normalized": _normalize(expected, ram, ptr),
    }


def analyze_model(dump: bytes, rom: NdsRom, stage_key: str) -> dict:
    """Find the stage's model (NSBMD) in a Main-RAM dump and compare it with the ROM's model file.

    The game keeps the model file as loaded; the texture setup (NNS) rewrites some material fields, so
    differences inside the material block are expected. Display lists (what is drawn) must be equal."""
    model = rom.stage_model_decoded(stage_key)
    if model is None:
        return {"stage_key": stage_key, "found": False, "reason": "no model file for this stage"}
    from .nitro.nsbmd import Nsbmd
    head = model[:16]
    hits = []
    i = dump.find(head)
    while i >= 0 and len(hits) < 8:
        hits.append(i)
        i = dump.find(head, i + 4)
    if not hits:
        return {"stage_key": stage_key, "found": False, "reason": "the model's file header is not in memory"}
    m = Nsbmd(model, stage_key).models[0]
    mat0, mat1 = m.base + m.ofs_mat, m.base + m.ofs_shp
    dls = [(s["name"], s["dl_offset"], s["dl_offset"] + s["dl_size"]) for s in m.shapes]
    best = None
    for at in hits:
        ram = dump[at:at + len(model)]
        diff = [o for o in range(0, min(len(ram), len(model)) // 4 * 4, 4) if ram[o:o + 4] != model[o:o + 4]]
        in_mat = sum(1 for o in diff if mat0 <= o < mat1)
        in_dl = {n: sum(1 for o in diff if a <= o < b) for n, a, b in dls}
        res = {"address": f"0x{ARM9_RAM_BASE + at:08X}", "size": len(model),
               "words_total": len(model) // 4, "words_different": len(diff),
               "different_in_material_block": in_mat, "different_elsewhere": len(diff) - in_mat,
               "display_lists_equal": all(v == 0 for v in in_dl.values()),
               "display_list_differences": in_dl,
               "first_other_differences": [f"0x{o:X}" for o in diff if not mat0 <= o < mat1][:20]}
        if best is None or res["different_elsewhere"] < best["different_elsewhere"]:
            best = res
    return {"stage_key": stage_key, "found": True, "copies_with_this_header": len(hits), **best}


def analyze_names(dump: bytes, rom: NdsRom) -> dict:
    """Compare the ARM9 name areas (stage table and strings, world-name entries and strings; spec section
    20) in a Main-RAM dump with the ROM's ARM9."""
    prof = rom.profile()
    if not prof or "name_mask" not in prof:
        return {"checked": False, "reason": "names not known for this ROM"}
    ranges = prof["name_mask"]
    equal, first = 0, None
    for s, e in ranges:
        ram = dump[s - ARM9_RAM_BASE:e - ARM9_RAM_BASE]
        want = rom.arm9[s - rom.arm9_ram:e - rom.arm9_ram]
        if ram == want:
            equal += 1
        elif first is None:
            k = next(i for i in range(min(len(ram), len(want))) if ram[i] != want[i]) if len(ram) == len(want) else 0
            first = f"0x{s + k:08X}"
    return {"checked": True, "ranges": len(ranges), "ranges_equal": equal, "equal": equal == len(ranges),
            "bytes": sum(e - s for s, e in ranges), "first_difference": first}


def analyze_animation(dump: bytes, rom: NdsRom, stage_key: str) -> dict:
    """Find the stage's joint animation (NSBCA, model/stage/<key>_nsbca.lz) in a Main-RAM dump and compare it
    with the ROM's file. Joint animations are read in place, so every word should be equal."""
    from . import lz10
    path = f"model/stage/{stage_key}_nsbca.lz"
    if path not in rom.id_by_path:
        return {"stage_key": stage_key, "found": False, "reason": "no animation file for this stage"}
    comp = rom.file_bytes(path)
    anim = lz10.decode(comp)[0] if comp[:1] == b"\x10" else comp
    hits = []
    i = dump.find(anim[:16])
    while i >= 0 and len(hits) < 8:
        hits.append(i)
        i = dump.find(anim[:16], i + 4)
    if not hits:
        return {"stage_key": stage_key, "found": False, "file": path,
                "reason": "the animation's file header is not in memory"}
    best = None
    for at in hits:
        ram = dump[at:at + len(anim)]
        diff = [o for o in range(0, min(len(ram), len(anim)) // 4 * 4, 4) if ram[o:o + 4] != anim[o:o + 4]]
        res = {"address": f"0x{ARM9_RAM_BASE + at:08X}", "size": len(anim), "words_total": len(anim) // 4,
               "words_different": len(diff), "equal": not diff and ram[len(anim) // 4 * 4:] == anim[len(anim) // 4 * 4:],
               "first_differences": [f"0x{o:X}" for o in diff[:20]]}
        if best is None or res["words_different"] < best["words_different"]:
            best = res
    return {"stage_key": stage_key, "found": True, "file": path, "copies_with_this_header": len(hits), **best}


def _normalize(expected: bytes, ram: bytes, ptr: int) -> bytes:
    """RAM copy with relocated words turned back into file offsets (for dump-vs-dump comparison)."""
    out = bytearray(ram)
    for o in range(0, len(expected) // 4 * 4, 4):
        fw = struct.unpack_from("<I", expected, o)[0]
        rw = struct.unpack_from("<I", ram, o)[0]
        if fw != rw and fw != 0 and rw == (fw + ptr) & 0xFFFFFFFF:
            struct.pack_into("<I", out, o, fw)
    return bytes(out)


def compare_two(a: dict, b: dict) -> dict:
    same_stage = a["stage_key"] == b["stage_key"]
    na, nb = a["_normalized"], b["_normalized"]
    diffs = [f"0x{o:X}" for o in range(0, min(len(na), len(nb)), 4) if na[o:o + 4] != nb[o:o + 4]]
    return {"same_stage": same_stage, "same_length": len(na) == len(nb),
            "identical_after_normalizing_pointers": same_stage and na == nb,
            "differing_word_offsets": diffs[:200], "differing_words": len(diffs)}
