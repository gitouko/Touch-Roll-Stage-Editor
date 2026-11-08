"""Lossless, read-only StageDocument for LCTS (.stc) stage files.

Source-of-truth rules (contract §10):
  * ``source`` (the bytes as given, compressed or not) and ``decoded`` (the
    LCTS stream) are immutable and authoritative.
  * Every parsed value is a VIEW: it records the offset and raw bytes it was
    read from. Nothing is reconstructed from parsed values.
  * Bytes not claimed by any view are reported as zero-fill or opaque ranges,
    never dropped.
  * There is no edit API. ``export_decoded()`` / ``export_source()`` return the
    original buffers, which is what the no-op round-trip tests verify.
  * Malformed input produces diagnostics, not exceptions.
"""
from __future__ import annotations

import hashlib
import math
from typing import Dict, List, Optional, Tuple

from .. import lz10
from ..binreader import BinReader, ReadOutOfBounds
from . import layout as L


def _hex(n: int) -> str:
    return f"0x{n:X}"


class Diagnostic(dict):
    """severity: info | warning | error."""

    def __init__(self, severity: str, code: str, message: str, offset: Optional[int] = None,
                 evidence: Optional[str] = None):
        super().__init__(severity=severity, code=code, message=message,
                         offset=_hex(offset) if offset is not None else None, evidence=evidence)


def matrix_stats(m: List[List[float]]) -> dict:
    det = (m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1])
           - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
           + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0]))
    row_norms = [math.sqrt(sum(v * v for v in row)) for row in m]
    max_dot = max(abs(sum(m[i][k] * m[j][k] for k in range(3))) for i in range(3) for j in range(3) if i < j)
    tol = 0.01  # Q20.12 rounding of cos/sin terms stays well inside this
    return {
        "det": round(det, 6),
        "row_norms": [round(n, 6) for n in row_norms],
        "orthonormal": abs(det - 1) < tol and all(abs(n - 1) < tol for n in row_norms) and max_dot < tol,
    }


class StageDocument:
    """Parse once at construction; all attributes are read-only results."""

    def __init__(self, source: bytes, name: str = ""):
        self.name = name
        self.source = bytes(source)
        self.source_sha256 = hashlib.sha256(self.source).hexdigest()
        self.container: Optional[str] = None
        self.lz10_info: Optional[dict] = None
        self.decoded: Optional[bytes] = None
        self.decoded_sha256: Optional[str] = None
        self.layout_variant = "unparsed"
        self.header: Dict[str, dict] = {}
        self.start: dict = {}
        self.groups: List[dict] = []
        self.pools: Dict[str, dict] = {}
        self.strings: List[dict] = []
        self.diagnostics: List[Diagnostic] = []
        self.checks: Dict[str, str] = {}
        self._spans: Dict[Tuple[int, int, str], dict] = {}
        self.parse_complete = False
        self.descriptor_array_ok: Optional[bool] = None
        self.box2_default: Optional[bool] = None
        self.pool_totals: Dict[str, int] = {}
        self.pool_totals_source: Optional[str] = None
        self.coverage: Optional[dict] = None
        try:
            self._open_container()
            if self.decoded is not None:
                self._parse()
        except Exception as exc:  # reported, never swallowed silently
            self.diag("error", "internal_parse_error", f"{type(exc).__name__}: {exc}")
        self._finalize()

    # ------------------------------------------------------------ public API
    @classmethod
    def from_file(cls, path: str, name: str = "") -> "StageDocument":
        with open(path, "rb") as fh:
            return cls(fh.read(), name or path)

    def export_source(self) -> bytes:
        """Unchanged original bytes (compressed passthrough when the input was LZ10)."""
        return self.source

    def export_decoded(self) -> Optional[bytes]:
        """Unchanged decoded LCTS stream."""
        return self.decoded

    @property
    def spans(self) -> List[dict]:
        return [self._spans[k] for k in sorted(self._spans)]

    def diag(self, severity, code, message, offset=None, evidence=None):
        self.diagnostics.append(Diagnostic(severity, code, message, offset, evidence))

    def has_errors(self) -> bool:
        return any(d["severity"] == "error" for d in self.diagnostics)

    # ------------------------------------------------------------- container
    def _open_container(self):
        src = self.source
        if src[:4] == b"LCTS":
            self.container = "plain"
            self.decoded = src
        elif src[:1] == b"\x10":
            try:
                dec, info = lz10.decode(src)
            except lz10.LZ10Error as exc:
                self.container = "lz10_invalid"
                self.diag("error", "lz10_" + exc.code, exc.message, exc.offset)
                return
            self.container = "lz10"
            self.lz10_info = info.as_dict()
            if dec[:4] != b"LCTS":
                self.diag("error", "decoded_not_lcts", f"decoded stream starts with {dec[:4].hex(' ')}", 0, "E01")
                return
            self.decoded = dec
        else:
            self.container = "unknown"
            self.diag("error", "not_lcts", f"input starts with {src[:4].hex(' ')}; neither LCTS nor LZ10", 0, "E01")
            return
        self.decoded_sha256 = hashlib.sha256(self.decoded).hexdigest()

    # ---------------------------------------------------------------- spans
    def _span(self, start, end, kind, label, evidence=None, stride=None):
        key = (start, end, kind)
        if key in self._spans:
            sp = self._spans[key]
            if label not in sp["labels"]:
                sp["labels"].append(label)
            return
        self._spans[key] = {"start": _hex(start), "end": _hex(end), "length": end - start, "kind": kind,
                            "labels": [label], "evidence": evidence, "record_stride": _hex(stride) if stride else None,
                            "_s": start, "_e": end}

    # ---------------------------------------------------------------- parse
    def _parse(self):
        r = BinReader(self.decoded, self.name)
        n = len(r)
        if n < L.HEADER_SIZE:
            self.diag("error", "truncated_header", f"stream is {n} bytes, header needs {L.HEADER_SIZE}", 0)
            return
        for off, fname, ftype, ev in L.HEADER_FIELDS:
            raw = r.slice(off, 4)
            entry = {"offset": _hex(off), "raw": raw.hex(" "), "type": ftype, "evidence": ev}
            if ftype == "magic":
                entry["value"] = raw.decode("ascii", "replace")
            elif ftype == "u32":
                entry["value"] = r.u32(off)
            else:
                entry["value"] = r.q12(off)
                entry["s32"] = r.s32(off)
            self.header[fname] = entry
        hv = {k: v["value"] for k, v in self.header.items()}
        self._span(0, L.HEADER_FIELDS_END, "header", "header_fields", "E01")
        if r.is_zero(L.HEADER_FIELDS_END, L.HEADER_SIZE - L.HEADER_FIELDS_END):
            self._span(L.HEADER_FIELDS_END, L.HEADER_SIZE, "zero_reserved", "header_reserved_zero", "E18")
        else:
            self.diag("warning", "header_reserved_nonzero", "0x64..0xE3 is not all zero (unseen in corpus)",
                      L.HEADER_FIELDS_END, "E18")
        if hv["u32_04"] != 0x12:
            self.diag("warning", "u32_04_unseen_value", f"u32(0x04)={hv['u32_04']:#x}; corpus always 0x12", 4, "E02")
        mins = (hv["bounds_min_x"], hv["bounds_min_y"], hv["bounds_min_z"])
        maxs = (hv["bounds_max_x"], hv["bounds_max_y"], hv["bounds_max_z"])
        if not all(a <= b for a, b in zip(mins, maxs)):
            self.diag("warning", "bounds_min_gt_max", "a bounds min value exceeds its max", 0x1C, "E03")
        box2 = tuple(hv[k] for k in ("box2_a_x", "box2_a_y", "box2_a_z", "box2_b_x", "box2_b_y", "box2_b_z"))
        self.box2_default = box2 == L.BOX2_DEFAULT

        self._parse_groups(r, hv)
        self._parse_start_and_variant(r, hv)
        self._parse_pools(r)
        self.parse_complete = True

    def _parse_groups(self, r: BinReader, hv: dict):
        F, N = hv["descriptor_base"], hv["group_count"]
        self.descriptor_array_ok = (F % 4 == 0 and F >= L.HEADER_SIZE and 0 < N <= L.MAX_GROUPS
                                    and F + N * L.DESCRIPTOR_SIZE <= len(r))
        if not self.descriptor_array_ok:
            self.diag("error", "descriptor_array_invalid",
                      f"F={F:#x}, N={N}, F+N*0xCC={F + N * L.DESCRIPTOR_SIZE:#x}, len={len(r):#x}", 0x18, "E04")
            return
        self._span(F, F + N * L.DESCRIPTOR_SIZE, "group_descriptors", "group_descriptor_array",
                   "E04", L.DESCRIPTOR_SIZE)
        seen_strings = {}
        for k in range(N):
            D = F + k * L.DESCRIPTOR_SIZE
            q = r.q12
            mat = [[q(D + 0x10 + 4 * (3 * row + col)) for col in range(3)] for row in range(3)]
            g = {
                "index": k, "offset": _hex(D),
                "u32_00": r.u32(D), "name_offset_04": r.u32(D + 4), "name_offset_08": r.u32(D + 8),
                "u32_0C": r.u32(D + 0xC),
                "matrix": mat, "matrix_stats": matrix_stats(mat),
                "translation": [q(D + 0x34), q(D + 0x38), q(D + 0x3C)],
                "grid": {"nx": r.u16(D + 0x40), "nz": r.u16(D + 0x42), "origin_x": q(D + 0x44),
                         "origin_z": q(D + 0x48), "step_x": q(D + 0x4C), "step_z": q(D + 0x50),
                         "cell_table_offset": r.u32(D + 0x54), "evidence": "E14"},
                "u32_58": r.u32(D + 0x58),
                "q12_5C_64": [q(D + 0x5C), q(D + 0x60), q(D + 0x64)],
                "u32_68_70": [r.u32(D + 0x68), r.u32(D + 0x6C), r.u32(D + 0x70)],
                "tables": {}, "slices": {},
                "tail_zero": r.is_zero(D + 0xAC, L.DESCRIPTOR_SIZE - 0xAC),
            }
            if not g["tail_zero"]:
                self.diag("warning", "descriptor_tail_nonzero", f"group {k} +0xAC..+0xCB not zero", D + 0xAC, "E04")
            if g["matrix_stats"]["det"] <= 0:
                self.diag("warning", "group_matrix_det_nonpositive",
                          f"group {k} det={g['matrix_stats']['det']}", D + 0x10, "E13")
            for key in ("name_offset_04", "name_offset_08"):
                off = g[key]
                s = r.cstring(off)
                g[key.replace("offset", "text")] = s
                if s is None:
                    self.diag("warning", "group_name_unresolved", f"group {k} {key}={off:#x}", D + 4, "E19")
                elif off not in seen_strings:
                    seen_strings[off] = s
                    self._span(off, off + len(s) + 1, "string", f"name:{s}", "E19")
            if g["name_offset_04"] != g["name_offset_08"]:
                self.diag("info", "group_names_differ",
                          f"group {k}: +0x04 {g['name_text_04']!r} vs +0x08 {g['name_text_08']!r}", D + 4, "E19")
            for tname, toff, stride in L.GROUP_TABLES:
                cnt, ptr = r.u32(D + toff), r.u32(D + toff + 4)
                t = {"count": cnt, "offset": ptr, "record_stride": stride, "evidence": "E16"}
                if cnt and stride:
                    end = ptr + cnt * stride
                    t["in_bounds"] = cnt <= L.MAX_TABLE_RECORDS and ptr >= L.HEADER_SIZE and end <= len(r)
                    if t["in_bounds"]:
                        self._span(ptr, end, "opaque_records", f"group{k}.{tname}", "E16", stride)
                    else:
                        self.diag("error", "group_table_out_of_bounds",
                                  f"group {k} {tname}: {cnt} x {stride:#x} at {ptr:#x}", D + toff, "E16")
                elif cnt:
                    t["in_bounds"] = ptr < len(r)
                    if not t["in_bounds"]:
                        self.diag("error", "group_table_out_of_bounds", f"group {k} {tname} offset {ptr:#x}",
                                  D + toff, "E16")
                g["tables"][tname] = t
            grid = g["grid"]
            cells = grid["nx"] * grid["nz"]
            cto = grid["cell_table_offset"]
            if cells and cto:
                end = cto + cells * L.CELL_ENTRY_SIZE
                grid["cell_table_in_bounds"] = cto >= L.HEADER_SIZE and end <= len(r)
                if grid["cell_table_in_bounds"]:
                    self._span(cto, end, "opaque_records", f"group{k}.cell_table", "E15", L.CELL_ENTRY_SIZE)
                else:
                    self.diag("error", "cell_table_out_of_bounds", f"group {k}: {cells} cells at {cto:#x}",
                              D + 0x54, "E15")
            for pkey, _total, soff, _stride, _label, _ev in L.POOLS:
                g["slices"][pkey] = {"count": r.u32(D + soff), "offset": r.u32(D + soff + 4)}
            self.groups.append(g)
        self.strings = [{"offset": _hex(o), "text": s} for o, s in sorted(seen_strings.items())]

    def _parse_start_and_variant(self, r: BinReader, hv: dict):
        sc, sp = hv["start_count"], hv["start_offset"]
        start_ok = (0 < sc <= L.MAX_START_RECORDS and sp % 4 == 0 and sp >= L.HEADER_SIZE
                    and sp + sc * L.START_RECORD_SIZE <= len(r))
        slice_sums = {p[0]: sum(g["slices"][p[0]]["count"] for g in self.groups) for p in L.POOLS}
        if start_ok:
            self.layout_variant = "common"
            self.pool_totals = {p[0]: hv[p[1]] for p in L.POOLS}
            self.pool_totals_source = "header 0x40/0x44/0x48"
            recs = []
            for i in range(sc):
                o = sp + i * L.START_RECORD_SIZE
                recs.append({"index": i, "offset": _hex(o),
                             "position": [r.q12(o), r.q12(o + 4), r.q12(o + 8)],
                             "u32_0C": r.u32(o + 0xC)})
            self.start = {"count": sc, "offset": _hex(sp), "records": recs, "evidence": "E05"}
            self._span(sp, sp + sc * L.START_RECORD_SIZE, "records", "start_records", "E05", L.START_RECORD_SIZE)
            return
        shifted = {k: r.u32(off) for k, off in L.NO_START_SLOT_TOTAL_OFFSETS.items()}
        if self.groups and shifted == slice_sums:
            self.layout_variant = "no_start_slot"
            self.pool_totals = shifted
            self.pool_totals_source = "header 0x3C/0x40/0x44 (variant)"
            self.start = {"count": 0, "offset": None, "records": [], "evidence": "E20",
                          "note": f"u32(0x38)={sc:#x}, u32(0x3C)={sp:#x} do not form a valid start pair"}
            self.diag("info", "layout_variant_no_start_slot",
                      "no valid start pair; pool totals found one word earlier and equal the group slice sums",
                      0x38, "E20")
            if r.is_zero(L.HEADER_SIZE, L.START_RECORD_SIZE):
                self.diag("info", "start_area_zero", "0xE4..0xFF is all zero", L.HEADER_SIZE, "E20")
            return
        self.layout_variant = "unknown"
        self.pool_totals = slice_sums
        self.pool_totals_source = "group slice sums (header totals unresolved)"
        self.start = {"count": 0, "offset": None, "records": [], "evidence": "E05"}
        self.diag("warning", "unrecognized_layout_variant",
                  f"start pair u32(0x38)={sc:#x}, u32(0x3C)={sp:#x} invalid and no known variant matches", 0x38, "E05")

    def _parse_pools(self, r: BinReader):
        if not self.descriptor_array_ok:
            return
        for pkey, _tf, _soff, stride, label, ev in L.POOLS:
            total = self.pool_totals[pkey]
            slices = [(g["index"], g["slices"][pkey]["count"], g["slices"][pkey]["offset"]) for g in self.groups]
            p0 = next((off for _, _, off in slices if off), None)
            pool = {"candidate_label": label, "record_stride": stride, "total": total, "evidence": ev,
                    "start": _hex(p0) if p0 is not None else None, "records": [],
                    "referenced": sum(c for _, c, _ in slices), "contiguous": True, "in_bounds": True}
            self.pools[pkey] = pool
            if total == 0 and pool["referenced"] == 0:
                continue
            if total > L.MAX_POOL_RECORDS or p0 is None:
                pool["in_bounds"] = False
                self.diag("error", "pool_unlocatable", f"pool {pkey}: total={total}, start={p0}", None, ev)
                continue
            end = p0 + total * stride
            if p0 < L.HEADER_SIZE or end > len(r):
                pool["in_bounds"] = False
                self.diag("error", "pool_out_of_bounds", f"pool {pkey}: {total} x {stride:#x} at {p0:#x}", p0, ev)
                continue
            owner = [None] * total
            cur = p0
            for gi, cnt, off in slices:
                if not cnt:
                    continue
                if off != cur:
                    pool["contiguous"] = False
                    self.diag("warning", "pool_slice_not_contiguous",
                              f"pool {pkey} group {gi}: slice at {off:#x}, expected {cur:#x}", off, "E10")
                first = (off - p0) // stride if off >= p0 and (off - p0) % stride == 0 else None
                if first is None or first + cnt > total:
                    self.diag("error", "pool_slice_outside_pool",
                              f"pool {pkey} group {gi}: {cnt} at {off:#x} not inside pool", off, "E10")
                else:
                    for i in range(first, first + cnt):
                        owner[i] = gi
                cur = off + cnt * stride
            if pool["referenced"] > total:
                self.diag("warning", "pool_slices_exceed_total",
                          f"pool {pkey}: slices reference {pool['referenced']} > total {total}", None, "E06")
            unref = sum(1 for o in owner if o is None)
            pool["unreferenced"] = unref
            if unref:
                self.diag("info", "pool_unreferenced_records",
                          f"pool {pkey} ({label} candidate): {unref} of {total} records not in any group slice",
                          p0, "E10")
            self._span(p0, end, "records", f"pool_{pkey}_{label}_candidate", ev, stride)
            for i in range(total):
                o = p0 + i * stride
                rec = {"index": i, "offset": _hex(o), "group": owner[i]}
                if pkey in ("A", "B"):
                    mat = [[r.q12(o + 4 * (3 * row + col)) for col in range(3)] for row in range(3)]
                    rec["matrix"] = mat
                    rec["matrix_stats"] = matrix_stats(mat)
                    rec["translation"] = [r.q12(o + 0x24), r.q12(o + 0x28), r.q12(o + 0x2C)]
                    if pkey == "A":
                        rec["u32_30"] = r.u32(o + 0x30)
                        if rec["u32_30"] not in (0, 1):
                            self.diag("warning", "pool_a_word30_unseen_value",
                                      f"record {i}: {rec['u32_30']:#x} (corpus: 0/1 only)", o + 0x30, "E11")
                    if rec["matrix_stats"]["det"] <= 0:
                        self.diag("warning", f"pool_{pkey}_matrix_det_nonpositive",
                                  f"record {i} det={rec['matrix_stats']['det']}", o, ev)
                    elif not rec["matrix_stats"]["orthonormal"]:
                        self.diag("info", f"pool_{pkey}_matrix_scaled",
                                  f"record {i} det={rec['matrix_stats']['det']} (not orthonormal)", o, ev)
                else:
                    rec["position"] = [r.q12(o), r.q12(o + 4), r.q12(o + 8)]
                    rec["u32_0C"] = r.u32(o + 0xC)
                    if rec["u32_0C"] not in (0, 1):
                        self.diag("warning", "pool_c_word0C_unseen_value",
                                  f"record {i}: {rec['u32_0C']:#x} (corpus: 0/1 only)", o + 0xC, "E12")
                pool["records"].append(rec)

    # -------------------------------------------------------------- finalize
    def _finalize(self):
        data = self.decoded
        spans = self.spans
        # overlap check
        prev = None
        for sp in spans:
            if prev is not None and sp["_s"] < prev["_e"]:
                self.diag("warning", "span_overlap",
                          f"{prev['labels']} [{prev['start']},{prev['end']}) overlaps "
                          f"{sp['labels']} [{sp['start']},{sp['end']})", sp["_s"])
            if prev is None or sp["_e"] > prev["_e"]:
                prev = sp
        self.coverage = self._coverage(data, spans) if data is not None else None
        self._run_checks()

    @staticmethod
    def _coverage(data: bytes, spans: List[dict]) -> dict:
        n = len(data)
        claimed = bytearray(n)  # 1 = claimed by a view
        by_kind: Dict[str, int] = {}
        for sp in spans:
            s, e = max(0, sp["_s"]), min(n, sp["_e"])
            if e > s:
                claimed[s:e] = b"\x01" * (e - s)
        for sp in spans:
            by_kind[sp["kind"]] = by_kind.get(sp["kind"], 0) + sp["length"]
        gaps = []
        i = 0
        while i < n:
            if claimed[i]:
                i += 1
                continue
            j = i
            while j < n and not claimed[j]:
                j += 1
            zero = not any(data[i:j])
            gaps.append({"start": _hex(i), "end": _hex(j), "length": j - i, "kind": "zero_fill" if zero else "opaque"})
            i = j
        claimed_total = sum(claimed)
        zero_total = sum(g["length"] for g in gaps if g["kind"] == "zero_fill")
        opaque_total = sum(g["length"] for g in gaps if g["kind"] == "opaque")
        assert claimed_total + zero_total + opaque_total == n
        return {"length": n, "claimed_by_views": claimed_total, "zero_fill": zero_total, "opaque": opaque_total,
                "claimed_by_kind": dict(sorted(by_kind.items())),
                "opaque_ranges": [g for g in gaps if g["kind"] == "opaque"],
                "zero_fill_ranges": len([g for g in gaps if g["kind"] == "zero_fill"])}

    def _run_checks(self):
        c = self.checks

        def put(name, cond):
            c[name] = "n/a" if cond is None else ("pass" if cond else "fail")

        parsed = self.decoded is not None and bool(self.header)
        hv = {k: v["value"] for k, v in self.header.items()} if parsed else {}
        put("magic_lcts", (self.decoded[:4] == b"LCTS") if self.decoded is not None else False)
        put("u32_04_is_0x12", hv.get("u32_04") == 0x12 if parsed else None)
        put("header_reserved_zero", (not any(self.decoded[0x64:0xE4])) if parsed else None)
        put("bounds_min_le_max", all(hv[f"bounds_min_{a}"] <= hv[f"bounds_max_{a}"] for a in "xyz") if parsed else None)
        put("descriptor_array_in_bounds", self.descriptor_array_ok if parsed else None)
        g = self.groups
        put("descriptor_tails_zero", all(x["tail_zero"] for x in g) if g else None)
        put("group_names_resolve", all(x["name_text_04"] is not None and x["name_text_08"] is not None for x in g)
            if g else None)
        put("group_name_offsets_equal", all(x["name_offset_04"] == x["name_offset_08"] for x in g) if g else None)
        put("group_matrices_det_positive", all(x["matrix_stats"]["det"] > 0 for x in g) if g else None)
        put("start_slot_valid", self.layout_variant == "common" if parsed else None)
        if self.pools:
            put("pools_in_bounds", all(p["in_bounds"] for p in self.pools.values()))
            put("pool_slices_contiguous", all(p["contiguous"] for p in self.pools.values()))
            put("pool_slices_within_totals", all(p["referenced"] <= p["total"] for p in self.pools.values()))
            put("pools_fully_referenced", all(p.get("unreferenced", 0) == 0 for p in self.pools.values()))
            a = self.pools.get("A", {}).get("records", [])
            b = self.pools.get("B", {}).get("records", [])
            put("pool_a_matrices_orthonormal", all(x["matrix_stats"]["orthonormal"] for x in a) if a else None)
            put("pool_b_matrices_orthonormal", all(x["matrix_stats"]["orthonormal"] for x in b) if b else None)
        else:
            for k in ("pools_in_bounds", "pool_slices_contiguous", "pool_slices_within_totals",
                      "pools_fully_referenced", "pool_a_matrices_orthonormal", "pool_b_matrices_orthonormal"):
                c[k] = "n/a"
        tables = [t for x in g for t in x["tables"].values() if t["count"]]
        put("group_tables_in_bounds", all(t.get("in_bounds", False) for t in tables) if tables else None)
        cells = [x["grid"] for x in g if x["grid"]["nx"] * x["grid"]["nz"] and x["grid"]["cell_table_offset"]]
        put("cell_tables_in_bounds", all(x.get("cell_table_in_bounds", False) for x in cells) if cells else None)
        put("no_span_overlaps", not any(d["code"] == "span_overlap" for d in self.diagnostics) if parsed else None)
        put("box2_default_iff_u32_08_not_2",
            (self.box2_default == (hv.get("u32_08") != 2)) if parsed else None)
        put("roundtrip_source_identity", self.export_source() == self.source)
        put("roundtrip_decoded_identity",
            (hashlib.sha256(self.export_decoded()).hexdigest() == self.decoded_sha256)
            if self.decoded is not None else None)
        put("no_error_diagnostics", not self.has_errors())

    # --------------------------------------------------------------- summary
    def facts(self) -> dict:
        """Small, stable summary used by reports and tests."""
        return {
            "layout_variant": self.layout_variant,
            "group_count": len(self.groups),
            "group_names": [g["name_text_04"] for g in self.groups],
            "pool_totals": dict(self.pool_totals),
            "pool_totals_source": self.pool_totals_source,
            "pool_referenced": {k: p["referenced"] for k, p in self.pools.items()},
            "pool_unreferenced": {k: p.get("unreferenced", 0) for k, p in self.pools.items()},
            "pool_owner_groups": {k: sorted({r["group"] for r in p["records"] if r["group"] is not None})
                                  for k, p in self.pools.items()},
            "pool_a_u32_30": [r["u32_30"] for r in self.pools.get("A", {}).get("records", [])],
            "pool_c_u32_0C_counts": _value_counts(r["u32_0C"] for r in self.pools.get("C", {}).get("records", [])),
            "start_records": len(self.start.get("records", [])) if self.start else 0,
        }


def _value_counts(values) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for v in values:
        out[str(v)] = out.get(str(v), 0) + 1
    return dict(sorted(out.items()))
