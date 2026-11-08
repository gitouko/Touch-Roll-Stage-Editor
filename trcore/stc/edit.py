"""Field-level, same-size edits of a decoded LCTS stage stream.

Only fields of known records can be edited (start records and object-pool
records); offsets, counts, descriptors and undecoded bytes cannot. Every edit
must state the field's current value ("from"), so a plan written for one file
cannot silently change another. After editing, the stage is re-parsed and the
edit is refused unless:
  * no error diagnostics appear,
  * the parsed structure (groups, pools, spans) is unchanged,
  * the only changed bytes are inside the edited fields.

Values: "q12" fields take numbers and are stored as round(value * 4096);
"u32" fields take integers (or "0x..." strings).
"""
from __future__ import annotations

import hashlib
import re
import struct
from typing import List, Tuple

from .document import StageDocument

# target kind -> (record source, field offset in record, type, element count)
EDITABLE = {
    "start.position": ("start", 0x00, "q12", 3),
    "start.u32_0C": ("start", 0x0C, "u32", 1),
    "pool_A.matrix": ("A", 0x00, "q12", 9),
    "pool_A.translation": ("A", 0x24, "q12", 3),
    "pool_A.u32_30": ("A", 0x30, "u32", 1),
    "pool_B.matrix": ("B", 0x00, "q12", 9),
    "pool_B.translation": ("B", 0x24, "q12", 3),
    "pool_C.position": ("C", 0x00, "q12", 3),
    "pool_C.u32_0C": ("C", 0x0C, "u32", 1),
}
TARGET_RE = re.compile(r"^(start|pool_[ABC])\[(\d+)\]\.(\w+)$")
Q12_MIN, Q12_MAX = -(1 << 31), (1 << 31) - 1


class EditRefused(Exception):
    pass


def _to_list(v, n):
    vals = v if isinstance(v, (list, tuple)) else [v]
    if len(vals) != n:
        raise EditRefused(f"expected {n} value(s), got {len(vals)}")
    return list(vals)


def _encode(ftype: str, values) -> Tuple[bytes, list]:
    out = bytearray()
    stored = []
    for v in values:
        if ftype == "q12":
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise EditRefused(f"q12 value must be a number, got {v!r}")
            i = int(round(v * 4096))
            if not (Q12_MIN <= i <= Q12_MAX):
                raise EditRefused(f"value {v} is out of range for Q20.12")
            out += struct.pack("<i", i)
            stored.append(i / 4096)
        else:
            if isinstance(v, str):
                v = int(v, 0)
            if isinstance(v, bool) or not isinstance(v, int) or not (0 <= v <= 0xFFFFFFFF):
                raise EditRefused(f"u32 value must be an integer 0..0xFFFFFFFF, got {v!r}")
            out += struct.pack("<I", v)
            stored.append(v)
    return bytes(out), stored


def _decode(ftype: str, raw: bytes) -> list:
    n = len(raw) // 4
    if ftype == "q12":
        return [x / 4096 for x in struct.unpack(f"<{n}i", raw)]
    return list(struct.unpack(f"<{n}I", raw))


def _record_offset(doc: StageDocument, source: str, index: int) -> int:
    if source == "start":
        recs = (doc.start or {}).get("records", [])
    else:
        recs = doc.pools.get(source, {}).get("records", [])
    if not (0 <= index < len(recs)):
        raise EditRefused(f"record index {index} does not exist ({len(recs)} record(s))")
    return int(recs[index]["offset"], 16)


def _structure(doc: StageDocument):
    return ([(s["start"], s["end"], s["kind"]) for s in doc.spans], doc.layout_variant,
            doc.pool_totals, len(doc.groups))


def apply_edits(decoded: bytes, edits: List[dict], name: str = "") -> Tuple[bytes, List[dict]]:
    """Apply edits [{target, from, to, prediction?}] and return (new_bytes, records)."""
    doc = StageDocument(decoded, name)
    if doc.container != "plain":
        raise EditRefused("apply_edits needs a decoded LCTS stream")
    if doc.has_errors():
        raise EditRefused("source stage has error diagnostics; refusing to edit it")
    if not edits:
        raise EditRefused("no edits given")
    buf = bytearray(decoded)
    allowed = set()
    records = []
    for e in edits:
        target = e.get("target", "")
        m = TARGET_RE.match(target)
        if not m:
            raise EditRefused(f"bad target {target!r} (example: pool_C[1].position)")
        kind = f"{m.group(1)}.{m.group(3)}"
        if kind not in EDITABLE:
            raise EditRefused(f"{kind} is not an editable field; editable: {', '.join(sorted(EDITABLE))}")
        source, foff, ftype, n = EDITABLE[kind]
        rec_off = _record_offset(doc, source, int(m.group(2)))
        off = rec_off + foff
        width = 4 * n
        if "from" not in e or "to" not in e:
            raise EditRefused(f"{target}: every edit needs 'from' (current value) and 'to'")
        old_raw = bytes(buf[off:off + width])
        from_raw, _ = _encode(ftype, _to_list(e["from"], n))
        if from_raw != old_raw:
            raise EditRefused(f"{target}: current value is {_decode(ftype, old_raw)}, plan says {e['from']}")
        new_raw, stored = _encode(ftype, _to_list(e["to"], n))
        if any(o in allowed for o in range(off, off + width)):
            raise EditRefused(f"{target}: edited twice in one plan")
        buf[off:off + width] = new_raw
        allowed.update(range(off, off + width))
        records.append({
            "target": target, "field_type": ftype, "offset": f"0x{off:X}", "width": width,
            "old_raw": old_raw.hex(" "), "new_raw": new_raw.hex(" "),
            "old_value": _decode(ftype, old_raw), "new_value": stored,
            "unchanged": old_raw == new_raw, "prediction": e.get("prediction"),
        })
    new = bytes(buf)
    changed = {i for i in range(len(new)) if new[i] != decoded[i]}
    if not changed <= allowed:
        raise EditRefused("internal error: bytes outside the edited fields changed")
    new_doc = StageDocument(new, name)
    if new_doc.has_errors():
        raise EditRefused("edited stage has error diagnostics: " +
                          "; ".join(d["code"] for d in new_doc.diagnostics if d["severity"] == "error"))
    if _structure(new_doc) != _structure(doc):
        raise EditRefused("edit changed the parsed structure of the stage")
    for r in records:
        r["decoded_sha256_before"] = hashlib.sha256(decoded).hexdigest()
        r["decoded_sha256_after"] = hashlib.sha256(new).hexdigest()
    return new, records
