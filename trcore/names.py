"""World and stage names (ARM9 text; spec §20, docs/research_notes/m7_names.md).

* Stage names: a table of 120 pointers (world x 10 + stage) to capitalised strings. The strings fill an
  area of their own; nothing else lives there and nothing else points into it.
* World names: messages 34-45 of the game's message index. Each message is five string pointers
  (English, German, French, Spanish, Italian); each language has its own copy of the string.

Writing (`apply_names`): the stage-name area is rewritten packed, in table order, one byte apart (no
padding), and every table pointer is updated, so a name may be longer than its old slot as long as the
total fits. The world-name strings' slots form a pool: each distinct new name is written once into the
pool (4-byte aligned) and every language pointer of that world points at it. Unused pool bytes are
zeroed. Only these areas, the stage table and the twelve world-name message entries may change; the result
is re-read and checked.

All addresses are ARM9 RAM addresses of the USA ROM (profile keys in trcore/rom.py).
"""
from __future__ import annotations

import struct
from typing import Dict, List, Tuple

LANGS = ("en", "de", "fr", "es", "it")
LANG_NAMES = {"en": "English", "de": "German", "fr": "French", "es": "Spanish", "it": "Italian"}
STAGES_PER_WORLD = 10
# World names are drawn with the NFTR text font: printable ASCII (edit test 7). Stage names are ALSO drawn at
# stage start with an 8x7 sprite font indexed by character - 0x20 without a range check (69 cells), so only
# 0x20-0x5F (space, punctuation, digits, capitals) can be used; lower case crashed 1-3 (edit test 7). A
# stage name is used there as a printf format string, so "%" is never allowed in any name.
WORLD_ALLOWED = set(range(0x20, 0x7F)) - {ord("%")}
STAGE_ALLOWED = set(range(0x20, 0x60)) - {ord("%")}
ALLOWED = WORLD_ALLOWED                                   # kept for callers of the first version


class NamesError(Exception):
    pass


def _u32(arm9: bytes, base: int, addr: int) -> int:
    o = addr - base
    if not 0 <= o <= len(arm9) - 4:
        raise NamesError(f"address 0x{addr:08X} is outside the ARM9")
    return struct.unpack_from("<I", arm9, o)[0]


def _cstr_end(arm9: bytes, base: int, addr: int) -> int:
    o = addr - base
    if not 0 <= o < len(arm9):
        raise NamesError(f"string at 0x{addr:08X} is outside the ARM9")
    e = arm9.find(b"\0", o)
    if e < 0:
        raise NamesError(f"string at 0x{addr:08X} has no end")
    return base + e


def _cstr(arm9: bytes, base: int, addr: int) -> str:
    e = _cstr_end(arm9, base, addr)
    return arm9[addr - base:e - base].decode("utf-8", "replace")


class NameTable:
    """The names as stored, with their places. strict=True (needed for writing) also checks that the layout is
    the original one and raises NamesError otherwise; strict=False only reads the names (edited copies)."""

    def __init__(self, arm9: bytes, base: int, profile: dict, others=(), strict: bool = True):
        self.arm9, self.base = arm9, base
        self.stage_table = profile["stage_title_table"]
        self.stage_count = profile["stage_title_count"]
        self.stage_area = tuple(profile["stage_title_area"])
        self.message_index = profile["message_index"]
        first, count = profile["world_name_messages"]
        self.stage_ptrs = [_u32(arm9, base, self.stage_table + 4 * i) for i in range(self.stage_count)]
        self.stages = [_cstr(arm9, base, p) for p in self.stage_ptrs]
        lo, hi = self.stage_area
        for i, p in enumerate(self.stage_ptrs):
            if not lo <= p < hi or _cstr_end(arm9, base, p) >= hi:
                raise NamesError(f"stage name {i} is not inside the stage-name area")
        self.world_entries = [_u32(arm9, base, self.message_index + 4 * (first + w)) for w in range(count)]
        self.world_ptrs = [[_u32(arm9, base, e + 4 * k) for k in range(len(LANGS))] for e in self.world_entries]
        self.worlds = [[_cstr(arm9, base, p) for p in ps] for ps in self.world_ptrs]
        self.strict = strict
        if not strict:
            self.world_slots = []
            return
        self._check_stage_area()
        self.world_slots = self._world_slots()
        self._check_no_other_pointers(others)

    # ---- layout checks
    def _check_stage_area(self):
        lo, hi = self.stage_area
        starts = set(self.stage_ptrs)
        pos = lo
        while pos < hi:
            if self.arm9[pos - self.base] == 0:
                pos += 1
                continue
            if pos not in starts:
                raise NamesError(f"something other than a stage name lives in the stage-name area (0x{pos:08X})")
            pos = _cstr_end(self.arm9, self.base, pos) + 1

    def _world_slots(self) -> List[Tuple[int, int]]:
        """Runs [start, end) of adjacent world-name string slots (a slot = the string, its NUL and padding)."""
        allp = sorted({p for ps in self.world_ptrs for p in ps})
        if len(allp) != sum(len(ps) for ps in self.world_ptrs):
            raise NamesError("two world-name pointers share a string: these names were already edited; "
                             "names are always written starting from the original ROM")
        slots = []
        for p in allp:
            e = _cstr_end(self.arm9, self.base, p) + 1
            while e - self.base < len(self.arm9) and self.arm9[e - self.base] == 0 and (e % 4):
                e += 1
            slots.append((p, e))
        runs: List[List[int]] = []
        for s, e in slots:
            if runs and runs[-1][1] == s:
                runs[-1][1] = e
            else:
                runs.append([s, e])
        return [(s, e) for s, e in runs]

    def _check_no_other_pointers(self, others):
        """No word in the ARM9 or the given overlays, other than the tables themselves, may point into the
        stage-name area or a world-name slot (it would read a moved or changed string)."""
        ranges = [self.stage_area] + list(self.world_slots)
        own = set(range(self.stage_table, self.stage_table + 4 * self.stage_count, 4))
        for e in self.world_entries:
            own.update(range(e, e + 4 * len(LANGS), 4))
        regions = [(self.base, self.arm9)] + list(others)
        lo_all = min(r[0] for r in ranges)
        hi_all = max(r[1] for r in ranges)
        for base, data in regions:
            for i in range(0, len(data) - 3, 4):
                v = struct.unpack_from("<I", data, i)[0]
                if lo_all <= v < hi_all and any(a <= v < b for a, b in ranges):
                    if base == self.base and (base + i) in own:
                        continue
                    raise NamesError(f"0x{base + i:08X} also points into the name strings (0x{v:08X}); "
                                     "names cannot be moved safely")

    # ---- views
    def stage_key(self, index: int) -> str:
        return f"{index // STAGES_PER_WORLD + 1}-{index % STAGES_PER_WORLD + 1}"

    def stage_capacity_bytes(self) -> int:
        return self.stage_area[1] - self.stage_area[0]

    def world_pool_bytes(self) -> int:
        return sum(e - s for s, e in self.world_slots)

    def as_dict(self) -> dict:
        return {"stages": {self.stage_key(i): n for i, n in enumerate(self.stages)},
                "worlds": {str(w + 1): dict(zip(LANGS, ns)) for w, ns in enumerate(self.worlds)},
                "stage_area_bytes": self.stage_capacity_bytes(),
                "stage_area_used": sum(len(n.encode()) + 1 for n in self.stages),
                "world_pool_bytes": self.world_pool_bytes()}


def read_names(rom, strict: bool = True) -> NameTable:
    prof = rom.profile()
    if not prof or "stage_title_table" not in prof:
        raise NamesError("this ROM is not one whose name tables are known (USA AMOE rev 0 only)")
    others = [(o["ram"], o["data"]) for o in rom.overlays() if not o.get("compressed")]
    return NameTable(rom.arm9, rom.arm9_ram, prof, others, strict)


def check_name(name: str, what: str, max_len: int = 64, allowed=WORLD_ALLOWED) -> str:
    if not isinstance(name, str) or not name:
        raise NamesError(f"{what}: the name must not be empty")
    if len(name) > max_len:
        raise NamesError(f"{what}: at most {max_len} characters")
    bad = sorted({c for c in name if ord(c) not in allowed})
    if bad:
        if allowed is STAGE_ALLOWED:
            why = "stage names may use capitals, digits, space and punctuation only (no lower case, no %)"
        else:
            why = "names may use plain English letters, digits, space and punctuation only (no %)"
        raise NamesError(f"{what}: {''.join(bad)!r} cannot be used: {why}")
    return name


def _stage_index(key) -> int:
    if isinstance(key, int):
        i = key
    else:
        try:
            w, s = (int(x) for x in str(key).split("-"))
        except ValueError:
            raise NamesError(f"stage {key!r}: use 'world-stage', for example '1-1'") from None
        if not (1 <= w <= 12 and 1 <= s <= STAGES_PER_WORLD):
            raise NamesError(f"stage {key!r} does not exist")
        i = (w - 1) * STAGES_PER_WORLD + (s - 1)
    if not 0 <= i < 120:
        raise NamesError(f"stage index {key!r} out of range")
    return i


def apply_names(table: NameTable, edits: dict) -> Tuple[bytes, dict, List[Tuple[int, int]]]:
    """Return (new ARM9 bytes, report, allowed byte ranges [(start, end) RAM addresses])."""
    if not table.strict:
        raise NamesError("internal: names can only be written from a strictly checked (original) table")
    arm9, base = table.arm9, table.base
    new_stages = list(table.stages)
    changed_stages = {}
    for key, name in (edits.get("stages") or {}).items():
        i = _stage_index(key)
        new_stages[i] = check_name(name, f"stage {table.stage_key(i)}", allowed=STAGE_ALLOWED)
        changed_stages[table.stage_key(i)] = {"from": table.stages[i], "to": name}
    new_worlds = [list(ns) for ns in table.worlds]
    changed_worlds = {}
    for key, val in (edits.get("worlds") or {}).items():
        try:
            w = int(key) - 1
        except ValueError:
            raise NamesError(f"world {key!r}: use its number, 1-12") from None
        if not 0 <= w < len(new_worlds):
            raise NamesError(f"world {key!r} does not exist")
        if isinstance(val, str):
            per = {lang: val for lang in LANGS}
        elif isinstance(val, dict) and val and set(val) <= set(LANGS):
            per = dict(val)
        else:
            raise NamesError(f"world {key}: give one name, or names per language ({', '.join(LANGS)})")
        for lang, name in per.items():
            new_worlds[w][LANGS.index(lang)] = check_name(name, f"world {key} ({LANG_NAMES[lang]})")
        changed_worlds[str(w + 1)] = {"from": table.worlds[w][0], "to": per}

    out = bytearray(arm9)
    allowed: List[Tuple[int, int]] = []

    def put32(addr, v):
        struct.pack_into("<I", out, addr - base, v)

    # stage names: repack the whole area in table order
    lo, hi = table.stage_area
    blob = bytearray()
    ptrs = []
    for n in new_stages:
        ptrs.append(lo + len(blob))
        blob += n.encode("ascii") + b"\0"
    if len(blob) > hi - lo:
        raise NamesError(f"the stage names need {len(blob)} bytes; the game has room for {hi - lo} "
                         f"({len(blob) - (hi - lo)} too many): shorten some names")
    out[lo - base:hi - base] = bytes(blob) + bytes(hi - lo - len(blob))
    for i, p in enumerate(ptrs):
        put32(table.stage_table + 4 * i, p)
    allowed += [(lo, hi), (table.stage_table, table.stage_table + 4 * table.stage_count)]

    # world names: one copy of each distinct string in the pool, all pointers of a world to it
    runs = [list(r) for r in table.world_slots]
    free = [[s, e] for s, e in runs]
    for s, e in runs:
        out[s - base:e - base] = bytes(e - s)
    placed: Dict[str, int] = {}
    order = []
    for ns in new_worlds:
        for n in ns:
            if n not in order:
                order.append(n)
    for n in sorted(order, key=lambda x: -len(x)):          # longest first: fewer leftovers
        need = len(n.encode("ascii")) + 1
        for f in free:
            start = (f[0] + 3) & ~3
            if start + need <= f[1]:
                out[start - base:start - base + need] = n.encode("ascii") + b"\0"
                placed[n] = start
                f[0] = start + need
                break
        else:
            raise NamesError(f"the world names do not fit in the game's space for them ({table.world_pool_bytes()} "
                             f"bytes in {len(runs)} pieces); shorten '{n}'")
    for w, ns in enumerate(new_worlds):
        for k, n in enumerate(ns):
            put32(table.world_entries[w] + 4 * k, placed[n])
    allowed += [(s, e) for s, e in runs]
    allowed += [(e, e + 4 * len(LANGS)) for e in table.world_entries]

    new = bytes(out)
    _verify(table, new, new_stages, new_worlds, allowed)
    report = {"stages_changed": changed_stages, "worlds_changed": changed_worlds,
              "stage_area": {"bytes": hi - lo, "used_before": sum(len(n.encode()) + 1 for n in table.stages),
                             "used_after": len(blob)},
              "world_pool": {"bytes": table.world_pool_bytes(), "pieces": len(runs),
                             "used_after": sum(len(n) + 1 for n in placed)},
              "bytes_changed": sum(1 for a, b in zip(arm9, new) if a != b)}
    return new, report, allowed


def _verify(table: NameTable, new: bytes, stages, worlds, allowed):
    base = table.base
    for i, (a, b) in enumerate(zip(table.arm9, new)):
        if a != b and not any(s <= base + i < e for s, e in allowed):
            raise NamesError(f"internal: ARM9 byte 0x{base + i:08X} changed outside the name areas")
    if len(new) != len(table.arm9):
        raise NamesError("internal: ARM9 size changed")
    sp = [_u32(new, base, table.stage_table + 4 * i) for i in range(table.stage_count)]
    if [_cstr(new, base, p) for p in sp] != stages:
        raise NamesError("internal: stage names do not read back")
    lo, hi = table.stage_area
    if any(not lo <= p < hi for p in sp):
        raise NamesError("internal: a stage name pointer leaves its area")
    for w, e in enumerate(table.world_entries):
        ps = [_u32(new, base, e + 4 * k) for k in range(len(LANGS))]
        if [_cstr(new, base, p) for p in ps] != worlds[w]:
            raise NamesError(f"internal: world {w + 1} does not read back")
        if any(not any(s <= p < t for s, t in table.world_slots) for p in ps):
            raise NamesError(f"internal: world {w + 1} points outside the world-name pool")


def arm9_patches(old: bytes, new: bytes, base: int, ranges) -> List[Tuple[int, bytes, bytes]]:
    """One patch (RAM address, old bytes, new bytes) per allowed range that changed (ranges merged first)."""
    merged: List[List[int]] = []
    for s, e in sorted(ranges):
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    out = []
    for s, e in merged:
        a, b = old[s - base:e - base], new[s - base:e - base]
        if a != b:
            out.append((s, a, b))
    rest = bytearray(old)
    for s, a, b in out:
        rest[s - base:s - base + len(b)] = b
    if bytes(rest) != new:
        raise NamesError("internal: ARM9 changes outside the name areas")
    return out
