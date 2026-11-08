"""Read-only NITRO texture reader (.nsbtx BTX0 / TEX0 block, also an .nsbmd's own TEX0) -> RGBA pixels.

TEX0 header (offsets from the block start): +0x0C u16 texture data size >> 3, +0x0E u16 texture dictionary,
+0x14 u32 texture data, +0x1C u16 compressed (4x4) data size >> 3, +0x24 u32 4x4 texel data, +0x28 u32 4x4
palette-index data, +0x30 u32 palette data size >> 3, +0x34 u32 palette dictionary, +0x38 u32 palette data.
Texture dictionary entry: u32 texImageParam (bits 0-15 data offset >> 3, 20-22 width = 8 << n, 23-25
height = 8 << n, 26-28 format, 29 colour 0 transparent), u32 extra. Palette entry: u16 offset >> 3, u16 flags.
Formats (DS GPU): 1 A3I5, 2 4-colour, 3 16-colour, 4 256-colour, 5 4x4 compressed, 6 A5I3, 7 direct.
Colours are RGB555.
"""
from __future__ import annotations

import struct
from typing import Dict, List, Optional, Tuple

from ..binreader import BinReader, ReadOutOfBounds
from .nsbmd import NitroError, read_dict

FORMAT_NAMES = {1: "A3I5", 2: "4-colour", 3: "16-colour", 4: "256-colour", 5: "4x4 compressed", 6: "A5I3", 7: "direct"}


def rgb555(c: int) -> Tuple[int, int, int]:
    r, g, b = c & 0x1F, (c >> 5) & 0x1F, (c >> 10) & 0x1F
    return (r << 3) | (r >> 2), (g << 3) | (g >> 2), (b << 3) | (b >> 2)


class Texture:
    __slots__ = ("name", "param", "offset", "width", "height", "format", "color0_transparent")

    def __init__(self, name: str, param: int):
        self.name = name
        self.param = param
        self.offset = (param & 0xFFFF) << 3
        self.width = 8 << ((param >> 20) & 7)
        self.height = 8 << ((param >> 23) & 7)
        self.format = (param >> 26) & 7
        self.color0_transparent = bool(param >> 29 & 1)


class Tex0:
    def __init__(self, data: bytes, block: int, name: str = ""):
        self.data = data
        self.name = name
        r = BinReader(data, name)
        try:
            if data[block:block + 4] != b"TEX0":
                raise NitroError(f"{name}: no TEX0 block at 0x{block:X}")
            self.tex_data = block + r.u32(block + 0x14)
            self.comp_data = block + r.u32(block + 0x24)
            self.comp_info = block + r.u32(block + 0x28)
            self.pal_data = block + r.u32(block + 0x38)
            self.pal_size = r.u32(block + 0x30) << 3
            self.textures: Dict[str, Texture] = {}
            for tname, raw in read_dict(r, block + r.u16(block + 0x0E)):
                self.textures[tname] = Texture(tname, struct.unpack_from("<I", raw)[0])
            self.palettes: Dict[str, int] = {}
            for pname, raw in read_dict(r, block + r.u32(block + 0x34)):
                self.palettes[pname] = struct.unpack_from("<H", raw)[0] << 3
        except (ReadOutOfBounds, struct.error) as exc:
            raise NitroError(f"{name}: {exc}") from None

    def _pal(self, off: int, i: int) -> Tuple[int, int, int]:
        o = self.pal_data + off + 2 * i
        if o + 2 > len(self.data):
            return 255, 0, 255
        return rgb555(struct.unpack_from("<H", self.data, o)[0])

    def rgba(self, tex_name: str, pal_name: Optional[str] = None) -> Tuple[int, int, bytes]:
        """(width, height, RGBA bytes, rows top to bottom) of a texture with a palette."""
        t = self.textures[tex_name]
        w, h, f = t.width, t.height, t.format
        poff = self.palettes.get(pal_name, 0) if pal_name else 0
        d = self.data
        out = bytearray(w * h * 4)
        base = self.tex_data + t.offset

        def put(x, y, rgb, a):
            k = 4 * (y * w + x)
            out[k:k + 4] = bytes((rgb[0], rgb[1], rgb[2], a))

        if f in (2, 3, 4):
            bpp = {2: 2, 3: 4, 4: 8}[f]
            per = 8 // bpp
            mask = (1 << bpp) - 1
            for y in range(h):
                for x in range(w):
                    i = y * w + x
                    o = base + i // per
                    if o >= len(d):
                        continue
                    idx = (d[o] >> ((i % per) * bpp)) & mask
                    a = 0 if (idx == 0 and t.color0_transparent) else 255
                    put(x, y, self._pal(poff, idx), a)
        elif f in (1, 6):
            ibits = 5 if f == 1 else 3
            for y in range(h):
                for x in range(w):
                    o = base + y * w + x
                    if o >= len(d):
                        continue
                    b = d[o]
                    idx = b & ((1 << ibits) - 1)
                    a = b >> ibits
                    a = (a * 255) // 7 if f == 1 else (a * 255) // 31
                    put(x, y, self._pal(poff, idx), a)
        elif f == 7:
            for y in range(h):
                for x in range(w):
                    o = base + 2 * (y * w + x)
                    if o + 2 > len(d):
                        continue
                    c = struct.unpack_from("<H", d, o)[0]
                    put(x, y, rgb555(c), 255 if c & 0x8000 else 0)
        elif f == 5:
            blocks_x = w // 4
            cbase = self.comp_data + t.offset
            ibase = self.comp_info + t.offset // 2
            for by in range(h // 4):
                for bx in range(blocks_x):
                    n = by * blocks_x + bx
                    if cbase + 4 * n + 4 > len(d) or ibase + 2 * n + 2 > len(d):
                        continue
                    texels = struct.unpack_from("<I", d, cbase + 4 * n)[0]
                    info = struct.unpack_from("<H", d, ibase + 2 * n)[0]
                    pa = poff + ((info & 0x3FFF) << 2)
                    mode = info >> 14
                    c0, c1 = self._pal(pa, 0), self._pal(pa, 1)
                    cols = [(c0, 255), (c1, 255)]
                    if mode == 0:
                        cols += [(self._pal(pa, 2), 255), ((0, 0, 0), 0)]
                    elif mode == 1:
                        cols += [(tuple((a + b) // 2 for a, b in zip(c0, c1)), 255), ((0, 0, 0), 0)]
                    elif mode == 2:
                        cols += [(self._pal(pa, 2), 255), (self._pal(pa, 3), 255)]
                    else:
                        cols += [(tuple((5 * a + 3 * b) // 8 for a, b in zip(c0, c1)), 255),
                                 (tuple((3 * a + 5 * b) // 8 for a, b in zip(c0, c1)), 255)]
                    for k in range(16):
                        rgb, a = cols[(texels >> (2 * k)) & 3]
                        put(bx * 4 + (k & 3), by * 4 + (k >> 2), rgb, a)
        return w, h, bytes(out)


def read_textures(data: bytes, name: str = "") -> Optional[Tex0]:
    """The TEX0 block of a BTX0 (or BMD0) file, or None if it has none."""
    if data[:4] not in (b"BTX0", b"BMD0"):
        raise NitroError(f"{name}: not a BTX0/BMD0 file")
    n = struct.unpack_from("<H", data, 0x0E)[0]
    for i in range(n):
        b = struct.unpack_from("<I", data, 0x10 + 4 * i)[0]
        if data[b:b + 4] == b"TEX0":
            return Tex0(data, b, name)
    return None


def material_bindings(nsb, model_index: int = 0) -> Dict[int, Tuple[Optional[str], Optional[str]]]:
    """material index -> (texture name, palette name), from the model's texture/palette-to-material lists
    (material block: u16 offset of the texture dictionary, u16 offset of the palette dictionary; entries:
    u16 offset of a u8 material-index list, u8 count, u8 flag)."""
    m = nsb.models[model_index]
    r = nsb.r
    mb = m.base + m.ofs_mat
    out: Dict[int, List[Optional[str]]] = {i: [None, None] for i in range(len(m.materials))}
    for slot, dict_off in ((0, r.u16(mb)), (1, r.u16(mb + 2))):
        for name, raw in read_dict(r, mb + dict_off):
            lst, count = struct.unpack_from("<HB", raw)
            for k in range(count):
                mi = r.u8(mb + lst + k)
                if mi in out:
                    out[mi][slot] = name
    return {k: (v[0], v[1]) for k, v in out.items()}


def material_texture_params(nsb, model_index: int = 0) -> Dict[int, dict]:
    """material index -> {repeat_s, repeat_t, flip_s, flip_t, orig_w, orig_h, poly_attr, alpha, diffuse}."""
    from .nsbmd import dict_offsets
    m = nsb.models[model_index]
    r = nsb.r
    mb = m.base + m.ofs_mat
    out = {}
    for i, (_name, rel) in enumerate(dict_offsets(r, mb + 4)):
        mo = mb + rel
        da, pa, tip = r.u32(mo + 4), r.u32(mo + 0x0C), r.u32(mo + 0x14)
        out[i] = {"repeat_s": bool(tip >> 16 & 1), "repeat_t": bool(tip >> 17 & 1),
                  "flip_s": bool(tip >> 18 & 1), "flip_t": bool(tip >> 19 & 1),
                  "orig_w": r.u16(mo + 0x20), "orig_h": r.u16(mo + 0x22), "poly_attr": pa,
                  "alpha": (pa >> 16) & 31, "diffuse": rgb555(da & 0x7FFF), "ambient": rgb555((da >> 16) & 0x7FFF),
                  "back": bool(pa & 0x40), "front": bool(pa & 0x80)}
    return out
