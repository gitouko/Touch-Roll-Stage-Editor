"""Read-only Nintendo DS ROM access: header, NitroFS (FNT/FAT), ARM9, overlays,
plus the Touch & Roll stage map (world/stage -> stage ID -> stage file).

Nothing here writes. Game-specific addresses live in PROFILES, keyed by game
code and ROM SHA-1, and every value read through a profile is validated before
it is reported. A ROM without a matching profile still gets header/NitroFS
access but no stage map.
"""
from __future__ import annotations

import hashlib
import struct
import zlib
from typing import Dict, List, Optional, Tuple

from . import lz10

MAX_ROM_BYTES = 512 * 1024 * 1024
ARM9_RAM_BASE = 0x02000000
MAIN_RAM_SIZE = 4 * 1024 * 1024


class RomError(Exception):
    pass


def crc16(data: bytes, crc: int = 0xFFFF) -> int:
    """CRC-16 used by the DS header (poly 0xA001 reflected, init 0xFFFF)."""
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


# ---------------------------------------------------------------------------
# Game profile for Super Monkey Ball: Touch & Roll (USA), found by static
# analysis of the ROM with SHA-1 3b0bf7f4e35112cd2d91ffbf2a874aec300d7a17
# (see docs/research_notes/stc_format_spec.md §10). Keyed by game code and
# code_fingerprint(), so ROM copies with replaced data files still match.
PROFILES = {
    ("AMOE", "9ba399896ac53667cc66b08508e4721462e8c239"): {
        "label": "Super Monkey Ball: Touch & Roll (USA) rev 0",
        "stage_name_table": 0x020C4B4C,       # u32 pointers to C strings, ARM9
        "stage_name_count": 250,
        "stage_file_format": "stcoli/{}_stc.lz",  # ARM9 0x020C5370 holds 'stcoli/%s_stc.lz'
        "stage_model_format": "model/stage/{}_nsbmd.lz",  # ARM9 0x020C531C holds 'model/stage/%s_nsbmd.lz'
        "world_table": 0x02200E70,            # overlay 0: u32 pointers to world structs
        "world_table_overlay": 0,
        "world_count": 12,
        "world_struct": {"entries_ptr": 4, "stage_count": 8, "u32_0C": 0xC},
        "stage_entry_size": 8,                # {u32 stage_id, u32 flags}
        # RAM variables (for emulator scripts), found in the loader at ARM9 0x02041BBC
        "ram_loaded_stage_id": 0x020E8FC8,
        "ram_requested_stage_id": 0x020E8FD8,
        "ram_stage_data_ptr": 0x020E9208,
        "ram_world_index": 0x021FBF3C,        # s8, main-game mode
        "ram_stage_index": 0x021FBF30,        # s8, main-game mode
        # Names (ARM9 text, spec section 20, trcore/names.py)
        "stage_title_table": 0x020DD71C,      # 120 u32 pointers, world x 10 + stage
        "stage_title_count": 120,
        "stage_title_area": (0x020DD104, 0x020DD71C),   # the stage-name strings and nothing else
        "message_index": 0x020D6B00,          # 223 u32 pointers to 5-language string-pointer arrays
        "world_name_messages": (34, 12),      # first message, count
        # ARM9 bytes a name edit may change; masked by code_fingerprint(masked=True) so that ROM copies
        # with edited names keep this profile
        "name_mask": [(0x020D7D7C, 0x020D7D94), (0x020D7E24, 0x020D7E30), (0x020D7EA8, 0x020D7EB4),
                      (0x020D7EE4, 0x020D7EF0), (0x020D7F5C, 0x020D7F68), (0x020D7F8C, 0x020D7F98),
                      (0x020D7FB0, 0x020D7FD4), (0x020D891C, 0x020D893C), (0x020D895C, 0x020D896C),
                      (0x020D898C, 0x020D89AC), (0x020D89FC, 0x020D8ABC), (0x020D8ADC, 0x020D8AEC),
                      (0x020D8AFC, 0x020D8B0C), (0x020D8B7C, 0x020D8B8C), (0x020D8C5C, 0x020D8C9C),
                      (0x020D8CDC, 0x020D8CFC), (0x020D8D7C, 0x020D8D8C), (0x020D8D9C, 0x020D8DAC),
                      (0x020D8DBC, 0x020D8DDC), (0x020D8FA8, 0x020D9070), (0x020D9084, 0x020D90E8),
                      (0x020D92C8, 0x020D9304), (0x020D9318, 0x020D932C), (0x020D9340, 0x020D9354),
                      (0x020D9A0C, 0x020D9A20), (0x020D9A34, 0x020D9A48), (0x020D9A70, 0x020D9A84),
                      (0x020D9AAC, 0x020D9AC0), (0x020D9AE8, 0x020D9AFC), (0x020D9B10, 0x020D9B24),
                      (0x020D9B38, 0x020D9B4C), (0x020D9B60, 0x020D9BC4), (0x020DD104, 0x020DD8FC)],
        "masked_fingerprint": "930538015bc4ea2b4aa01a5116634219dd858c0c",
    },
}

# Display titles from the community wiki Bananapedia (Super Monkey Ball: Touch & Roll/Worlds), checked
# against the owner's five confirmed mappings. They are labels only, never used for lookup. (An older
# note here said the titles are images; they are in fact ARM9 text: stage names in capitals at
# 0x020DD71C, world names as messages 34-45 of the message table at 0x020D6B00, and both match this list.
# See docs/research_notes/m7_names.md.)
WORLD_TITLES = ["Wet 'N' Windy", "Junglistic Journey", "Blistering Sands", "Fantascene Falls",
                "Palarvar Lava", "Ice Lolly Land", "Tempest Storm", "Meteorite Mayhem",
                "Big Bang Boom", "Zero G Station", "Studio One", "Mesh Mosh"]
STAGE_TITLES = [
    ["Elementary", "Sine Wave", "Curvature", "Progenitor", "Bare", "Anaconda", "Fluctuant Travelator",
     "Swathe", "Assimilate", "Spiral Staircase"],
    ["Mini Bump", "Double Scepter", "Offsetting Incline", "Pincer", "Depressions", "Starfish",
     "Deviant Drop", "Receding Curves", "Displacement", "Labyrinthine"],
    ["Racket", "Basin", "Parasite", "Web Matrix", "Reverberate", "Beveled Edges", "Betting Course",
     "Halo", "Infinity", "Millipede"],
    ["Epicenter", "Wavy Brick Road", "Rollercoaster", "Descending Rings", "Cells", "Scythe", "Serrated",
     "Oscillating Waves", "Oblivion", "Whirl"],
    ["Deviation", "Sword", "Slot", "Long Winding Road", "Pit", "Conveyor", "Formation", "Swing",
     "Tunnel Vision", "Leviathan"],
    ["Meandering Road", "Slalom", "Coil", "Tilted Stretch", "Roulette", "Acute", "Menacing Conclusion",
     "Impediment", "Free Ride", "Fluctuation"],
    ["Deception", "Lateral Undulation", "Suspension", "Undulating Road", "Skatepark", "Congealment",
     "Plummet Chasm", "Tornado", "Rippling Road", "Rotary Bypass"],
    ["Hollow Diamond", "Spiral Under Stars", "Back Round", "Staircase", "Spinning Sun", "Parallel Peaks",
     "Circular Waves", "Obstacle Course", "Pinball Pursuit", "Road to Nowhere"],
    ["Gyration", "Converse", "Motion Gear", "Deflecting Protrusion", "Rotary", "Armada", "Trail",
     "Pendulum", "Wheel of Fortune", "Starlight Express"],
    ["Ascension", "Rails", "Perpetual Motion", "Scraper Descent", "Drop Downhill Drop", "Entwined",
     "Intertwined", "Revolution", "Sieve Track", "Slinky"],
    ["Backslide", "Cherry Drop", "Truffles", "Asymmetry", "Scallop", "Transience", "Divergence",
     "Wormhole", "Prohibition", "Locomote"],
    ["Whiplash", "Lickshot", "Spasmodic", "Gesticulation", "Protuberance", "Gauge", "Permeation",
     "Cavity", "Aperture", "Palpitation"],
]
# Mappings confirmed in-game by the project owner (world, stage) -> stage key.
OWNER_CONFIRMED = {(5, 2): "smb1_ad02", (5, 6): "smb1_ad21", (8, 1): "smb1_be02",
                   (9, 2): "smb1_ex03", (12, 5): "smb2_ex30"}


class NdsRom:
    """Immutable view of a .nds file."""

    def __init__(self, data: bytes, name: str = ""):
        self._profile = None
        if len(data) < 0x200:
            raise RomError("file too small to be a DS ROM")
        if len(data) > MAX_ROM_BYTES:
            raise RomError("file larger than 512 MiB")
        self.data = bytes(data)
        self.name = name
        d = self.data
        self.header = {
            "title": d[0:12].rstrip(b"\0").decode("ascii", "replace"),
            "game_code": d[0x0C:0x10].decode("ascii", "replace"),
            "maker_code": d[0x10:0x12].decode("ascii", "replace"),
            "rom_version": d[0x1E],
        }
        (self.arm9_off, self.arm9_entry, self.arm9_ram, self.arm9_size,
         self.arm7_off, self.arm7_entry, self.arm7_ram, self.arm7_size,
         self.fnt_off, self.fnt_size, self.fat_off, self.fat_size,
         self.ov9_off, self.ov9_size, self.ov7_off, self.ov7_size) = struct.unpack_from("<16I", d, 0x20)
        self.banner_off = struct.unpack_from("<I", d, 0x68)[0]
        self.used_rom_size = struct.unpack_from("<I", d, 0x80)[0]
        self.header_crc_stored = struct.unpack_from("<H", d, 0x15E)[0]
        self.header_crc_computed = crc16(d[:0x15E])
        for off, size, what in ((self.fnt_off, self.fnt_size, "FNT"), (self.fat_off, self.fat_size, "FAT"),
                                (self.arm9_off, self.arm9_size, "ARM9")):
            if off + size > len(d) or size == 0:
                raise RomError(f"{what} outside the file")
        self.fat = [struct.unpack_from("<II", d, self.fat_off + 8 * i) for i in range(self.fat_size // 8)]
        for i, (s, e) in enumerate(self.fat):
            if e < s or e > len(d):
                raise RomError(f"FAT entry {i} invalid ({s:#x}-{e:#x})")
        self.paths = self._walk_fnt()
        self.id_by_path = {p: i for i, p in self.paths.items()}
        self.arm9 = d[self.arm9_off:self.arm9_off + self.arm9_size]

    @classmethod
    def from_file(cls, path: str) -> "NdsRom":
        with open(path, "rb") as fh:
            return cls(fh.read(), path)

    # ------------------------------------------------------------ banner
    def banner_icon(self) -> Optional[Tuple[int, int, bytes]]:
        """The cartridge's own 32 x 32 menu icon as (32, 32, RGBA bytes), colour 0 transparent (DS rule),
        or None if the ROM has no banner. Read from the user's ROM; nothing of it is stored by the tools."""
        if len(self.data) < 0x6C:
            return None
        bo = struct.unpack_from("<I", self.data, 0x68)[0]
        if not bo or bo + 0x240 > len(self.data):
            return None
        bmp = self.data[bo + 0x20:bo + 0x220]
        pal = []
        for i in range(16):
            v = struct.unpack_from("<H", self.data, bo + 0x220 + 2 * i)[0]
            r, g, b = v & 31, (v >> 5) & 31, (v >> 10) & 31
            pal.append(bytes(((r << 3) | (r >> 2), (g << 3) | (g >> 2), (b << 3) | (b >> 2), 0 if i == 0 else 255)))
        out = bytearray(32 * 32 * 4)
        for t in range(16):
            tx, ty = t % 4, t // 4
            for y in range(8):
                for x in range(8):
                    v = (bmp[t * 32 + y * 4 + x // 2] >> ((x & 1) * 4)) & 15
                    k = 4 * ((ty * 8 + y) * 32 + tx * 8 + x)
                    out[k:k + 4] = pal[v]
        return 32, 32, bytes(out)

    # ------------------------------------------------------------ identity
    def identity(self) -> dict:
        d = self.data
        return {"size": len(d), "sha1": hashlib.sha1(d).hexdigest(), "md5": hashlib.md5(d).hexdigest(),
                "sha256": hashlib.sha256(d).hexdigest(), "crc32": f"{zlib.crc32(d) & 0xFFFFFFFF:08x}",
                **self.header, "header_crc_ok": self.header_crc_stored == self.header_crc_computed,
                "used_rom_size": self.used_rom_size, "files": len(self.fat)}

    def code_fingerprint(self, mask=None) -> str:
        """SHA-1 over ARM9, ARM7, the ARM9 overlay table and all overlay files.

        Replacing data files (stages, models) never changes it, so edited ROM
        copies keep the same profile; any code difference (other region or
        revision) changes it. `mask`: ARM9 RAM ranges [(start, end)] hashed as
        zeros (the bytes a name edit may change).
        """
        h = hashlib.sha1()
        arm9 = self.arm9
        if mask:
            b = bytearray(arm9)
            for s, e in mask:
                lo, hi = max(0, s - self.arm9_ram), min(len(b), e - self.arm9_ram)
                if lo < hi:
                    b[lo:hi] = bytes(hi - lo)
            arm9 = bytes(b)
        h.update(arm9)
        h.update(self.data[self.arm7_off:self.arm7_off + self.arm7_size])
        h.update(self.data[self.ov9_off:self.ov9_off + self.ov9_size])
        for ov in self.overlays():
            h.update(ov["data"])
        return h.hexdigest()

    def profile(self) -> Optional[dict]:
        """The known-ROM profile; also for copies whose only code change is edited names."""
        if self._profile is None:
            p = PROFILES.get((self.header["game_code"], self.code_fingerprint()))
            if p is None:
                for (code, _fp), q in PROFILES.items():
                    if code == self.header["game_code"] and q.get("masked_fingerprint") and \
                            self.code_fingerprint(q["name_mask"]) == q["masked_fingerprint"]:
                        p = q
                        break
            self._profile = p or False
        return self._profile or None

    # -------------------------------------------------------------- NitroFS
    def _walk_fnt(self) -> Dict[int, str]:
        d, base = self.data, self.fnt_off
        ndirs = struct.unpack_from("<H", d, base + 6)[0]
        paths: Dict[int, str] = {}
        seen = set()

        def walk(dir_id: int, prefix: str, depth: int):
            idx = dir_id & 0xFFF
            if idx >= ndirs or dir_id in seen or depth > 32:
                raise RomError(f"bad FNT directory {dir_id:#x}")
            seen.add(dir_id)
            sub, fid = struct.unpack_from("<IH", d, base + 8 * idx)
            p = base + sub
            while True:
                if p >= base + self.fnt_size:
                    raise RomError("FNT entry runs past the table")
                tl = d[p]
                p += 1
                if tl == 0:
                    return
                name = d[p:p + (tl & 0x7F)].decode("ascii", "replace")
                p += tl & 0x7F
                if tl & 0x80:
                    child = struct.unpack_from("<H", d, p)[0]
                    p += 2
                    walk(child, prefix + name + "/", depth + 1)
                else:
                    paths[fid] = prefix + name
                    fid += 1

        walk(0xF000, "", 0)
        for i in range(len(self.fat)):
            paths.setdefault(i, f"<unnamed:{i}>")   # overlay files have no name
        return paths

    def file_range(self, path_or_id) -> tuple:
        fid = self.id_by_path.get(path_or_id) if isinstance(path_or_id, str) else path_or_id
        if fid is None or not (0 <= fid < len(self.fat)):
            raise RomError(f"no file {path_or_id!r} in NitroFS")
        return (fid,) + self.fat[fid]

    def file_bytes(self, path_or_id) -> bytes:
        _, s, e = self.file_range(path_or_id)
        return self.data[s:e]

    def overlays(self) -> List[dict]:
        out = []
        for i in range(self.ov9_size // 32):
            oid, ram, size, bss, s0, s1, fid, flags = struct.unpack_from("<8I", self.data, self.ov9_off + 32 * i)
            s, e = self.fat[fid]
            out.append({"id": oid, "ram": ram, "size": size, "file_id": fid,
                        "compressed": bool((flags >> 24) & 1), "data": self.data[s:e]})
        return out

    # ------------------------------------------------------------ memory
    def _read_u32(self, addr: int, region: bytes, base: int) -> int:
        o = addr - base
        if o < 0 or o + 4 > len(region):
            raise RomError(f"address {addr:#x} outside region {base:#x}+{len(region):#x}")
        return struct.unpack_from("<I", region, o)[0]

    def _cstring(self, addr: int, region: bytes, base: int) -> str:
        o = addr - base
        if o < 0 or o >= len(region):
            raise RomError(f"string address {addr:#x} outside region")
        e = region.find(b"\0", o, o + 64)
        if e < 0:
            raise RomError(f"unterminated string at {addr:#x}")
        s = region[o:e]
        if not all(0x20 <= b < 0x7F for b in s):
            raise RomError(f"non-ASCII string at {addr:#x}")
        return s.decode("ascii")

    # ------------------------------------------------------------ stage map
    def stage_names(self) -> List[str]:
        prof = self._need_profile()
        if self.arm9_ram != ARM9_RAM_BASE:
            raise RomError("unexpected ARM9 load address")
        return [self._cstring(self._read_u32(prof["stage_name_table"] + 4 * i, self.arm9, self.arm9_ram),
                              self.arm9, self.arm9_ram)
                for i in range(prof["stage_name_count"])]

    def _need_profile(self) -> dict:
        prof = self.profile()
        if prof is None:
            raise RomError("no profile for this ROM (game code + SHA-1); stage map addresses unknown")
        return prof

    def stage_map(self) -> dict:
        """World/stage -> stage ID -> name -> file, validated. Raises RomError on any inconsistency."""
        prof = self._need_profile()
        names = self.stage_names()
        ov = {o["id"]: o for o in self.overlays()}[prof["world_table_overlay"]]
        if ov["compressed"]:
            raise RomError("world-table overlay is compressed; not supported")
        region, base = ov["data"], ov["ram"]
        ws = prof["world_struct"]
        worlds = []
        problems = []
        for w in range(prof["world_count"]):
            wp = self._read_u32(prof["world_table"] + 4 * w, region, base)
            count = self._read_u32(wp + ws["stage_count"], region, base)
            ep = self._read_u32(wp + ws["entries_ptr"], region, base)
            if count != 10:
                problems.append(f"world {w + 1}: {count} stages (expected 10)")
            stages = []
            for s in range(count):
                sid = self._read_u32(ep + prof["stage_entry_size"] * s, region, base)
                flags = self._read_u32(ep + prof["stage_entry_size"] * s + 4, region, base)
                if sid >= len(names):
                    problems.append(f"world {w + 1} stage {s + 1}: id {sid} outside name table")
                    continue
                key = names[sid]
                path = prof["stage_file_format"].format(key)
                if path not in self.id_by_path:
                    problems.append(f"world {w + 1} stage {s + 1}: {path} missing from NitroFS")
                if flags not in (0, 1):
                    problems.append(f"world {w + 1} stage {s + 1}: unseen flags {flags:#x}")
                title = STAGE_TITLES[w][s] if w < len(STAGE_TITLES) and s < 10 else None
                stages.append({"world": w + 1, "stage": s + 1, "title": title, "stage_id": sid,
                               "stage_key": key, "file": path, "flags": flags,
                               "bonus_candidate": bool(flags & 1)})
            worlds.append({"world": w + 1, "title": WORLD_TITLES[w] if w < len(WORLD_TITLES) else None,
                           "struct_addr": f"0x{wp:08X}",
                           "u32_0C": self._read_u32(wp + ws["u32_0C"], region, base), "stages": stages})
        by_ws = {(s["world"], s["stage"]): s["stage_key"] for w in worlds for s in w["stages"]}
        anchors = {f"W{w}-{s}": {"expected": k, "found": by_ws.get((w, s)), "ok": by_ws.get((w, s)) == k}
                   for (w, s), k in sorted(OWNER_CONFIRMED.items())}
        if not all(a["ok"] for a in anchors.values()):
            problems.append("owner-confirmed anchors do not match")
        used = sorted({s["stage_key"] for w in worlds for s in w["stages"]})
        stage_files = sorted(p[len("stcoli/"):-len("_stc.lz")] for p in self.id_by_path
                             if p.startswith("stcoli/") and p.endswith("_stc.lz"))
        return {
            "schema": "trcore.stage_map/1",
            "rom": dict({k: v for k, v in self.identity().items() if k in ("game_code", "sha1", "rom_version")},
                        code_fingerprint=self.code_fingerprint()),
            "profile": prof["label"],
            "source": {"name_table": f"ARM9 0x{prof['stage_name_table']:08X} ({len(names)} names)",
                       "world_table": f"overlay {prof['world_table_overlay']} 0x{prof['world_table']:08X}",
                       "titles": "Bananapedia community wiki (titles are images in the ROM)"},
            "anchors": anchors,
            "problems": problems,
            "worlds": worlds,
            "main_game_stage_keys": used,
            "stage_files_not_in_main_game": [k for k in stage_files if k not in used],
            "names_without_file": [n for n in names if prof["stage_file_format"].format(n) not in self.id_by_path],
            "stage_files_without_name": [k for k in stage_files if k not in names],
        }

    def stage_file_decoded(self, stage_key: str) -> bytes:
        prof = self._need_profile()
        data, _ = lz10.decode(self.file_bytes(prof["stage_file_format"].format(stage_key)))
        return data

    def stage_model_decoded(self, stage_key: str) -> Optional[bytes]:
        """The stage's own model (NSBMD), or None when the ROM has no model file for it."""
        prof = self._need_profile()
        path = prof["stage_model_format"].format(stage_key)
        if path not in self.id_by_path:
            return None
        data = self.file_bytes(path)
        return lz10.decode(data)[0] if data[:1] == b"\x10" else data


def stage_map_markdown(m: dict) -> str:
    lines = ["# Touch & Roll main-game stage map", "",
             f"ROM: `{m['rom']['game_code']}` rev {m['rom']['rom_version']}, SHA-1 `{m['rom']['sha1']}`. "
             f"Read from {m['source']['world_table']} and {m['source']['name_table']}. "
             f"Titles: {m['source']['titles']}.", "",
             "Anchors confirmed in-game by the project owner: " +
             ", ".join(f"{k} = `{v['expected']}` ({'match' if v['ok'] else 'MISMATCH'})"
                       for k, v in m["anchors"].items()) + ".", ""]
    if m["problems"]:
        lines += ["**Problems:** " + "; ".join(m["problems"]), ""]
    lines += ["`flags` bit 0 is set on stage 5 of every world; in the files checked it marks a no-goal "
              "banana stage (bonus candidate, not confirmed in-game).", ""]
    for w in m["worlds"]:
        lines += [f"## World {w['world']}: {w['title']}", "", "| Stage | Title | Stage ID | File | Flags |",
                  "|---:|---|---:|---|---:|"]
        for s in w["stages"]:
            lines.append(f"| {s['stage']} | {s['title']} | {s['stage_id']} | `{s['stage_key']}` | {s['flags']} |")
        lines.append("")
    lines += [f"Stage files in the ROM that no main-game slot uses ({len(m['stage_files_not_in_main_game'])}): " +
              ", ".join(f"`{k}`" for k in m["stage_files_not_in_main_game"]), ""]
    return "\n".join(lines)
