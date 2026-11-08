"""Content-based asset classification and corpus index.

Rules this module follows (contract §§5-6):
  * Compression state and file type come from the BYTES, never from the
    extension or the folder name. Names are only used to derive a stage key
    and an expected type, and disagreements are reported as issues.
  * The stage key comes from the original asset name (``smb1_be01.stc``,
    ``smb1_be01_stc.lz``) or, for grouped role files (``<stage>/stage.stc``),
    from the parent directory. Role words such as ``stage`` or ``model`` are
    never accepted as stage keys.
  * Paths in output are relative to the scan root, POSIX style.
  * Nothing is written next to the inputs; the index is returned as data.
"""
from __future__ import annotations

import hashlib
import os
import struct
from typing import Dict, Iterable, List, Optional, Tuple

from . import lz10

MAX_INPUT_BYTES = 64 * 1024 * 1024

# NITRO (Nintendo DS SDK) container magics -> conventional file extension.
NITRO_MAGIC_TO_EXT = {
    b"BMD0": "nsbmd",
    b"BTX0": "nsbtx",
    b"BCA0": "nsbca",
    b"BMA0": "nsbma",
    b"BTA0": "nsbta",
    b"BTP0": "nsbtp",
    b"BVA0": "nsbva",
}
STC_MAGIC = b"LCTS"

EXT_TO_SIGNATURE = {"stc": "LCTS"}
EXT_TO_SIGNATURE.update({ext: magic.decode("ascii") for magic, ext in NITRO_MAGIC_TO_EXT.items()})

# Role labels are descriptive only (NITRO naming conventions), not game semantics.
EXT_TO_ROLE = {
    "stc": "stage_data",
    "nsbmd": "model",
    "nsbtx": "texture",
    "nsbca": "joint_animation",
    "nsbma": "material_animation",
    "nsbta": "texture_srt_animation",
    "nsbtp": "texture_pattern_animation",
    "nsbva": "visibility_animation",
}
KNOWN_EXTS = tuple(sorted(EXT_TO_ROLE))

# Basenames the old grouping script produced. They name a ROLE, never a stage.
GROUPED_ROLE_FILES = {
    "stage.stc": "stc",
    "model.nsbmd": "nsbmd",
    "texture.nsbtx": "nsbtx",
    "animation.nsbca": "nsbca",
}
RESERVED_STAGE_WORDS = {"stage", "model", "texture", "animation", "stage_data"} | set(EXT_TO_ROLE.values())


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def git_blob_sha1_hex(data: bytes) -> str:
    """Git object id of the file content: sha1(b"blob <len>\\0" + data)."""
    h = hashlib.sha1()
    h.update(b"blob %d\x00" % len(data))
    h.update(data)
    return h.hexdigest()


def issue(severity: str, code: str, message: str) -> dict:
    return {"severity": severity, "code": code, "message": message}


# ---------------------------------------------------------------- names

def parse_name(relpath: str) -> dict:
    """Derive (stage_key, ext, naming form) from a relative POSIX path.

    Returns a dict with keys: stage_key (normalized lower-case or None),
    stage_key_original, ext, form, lz_suffix, issues.
    """
    parts = relpath.split("/")
    base = parts[-1]
    lower = base.lower()
    out = {"stage_key": None, "stage_key_original": None, "ext": None, "form": None,
           "lz_suffix": lower.endswith(".lz"), "issues": []}

    if lower in GROUPED_ROLE_FILES:
        out["ext"] = GROUPED_ROLE_FILES[lower]
        out["form"] = "grouped_role_file"
        if len(parts) >= 2:
            parent = parts[-2]
            if parent.lower() in RESERVED_STAGE_WORDS or not parent:
                out["issues"].append(issue("error", "ambiguous_stage_key",
                                           f"grouped role file under directory {parent!r}"))
            else:
                out["stage_key_original"] = parent
                out["stage_key"] = parent.lower()
        else:
            out["issues"].append(issue("error", "ambiguous_stage_key",
                                       "role file name without a stage directory"))
        return out

    stem = base[:-3] if out["lz_suffix"] else base
    stem_lower = stem.lower()
    key = None
    for ext in KNOWN_EXTS:
        if stem_lower.endswith("." + ext):
            key, out["ext"], out["form"] = stem[: -len(ext) - 1], ext, "dotted"
            break
        if stem_lower.endswith("_" + ext):
            key, out["ext"], out["form"] = stem[: -len(ext) - 1], ext, "flattened"
            break
    if out["form"] and out["lz_suffix"]:
        out["form"] += "_lz"
    if key is None:
        out["form"] = "unrecognized"
        return out
    if not key or key.lower() in RESERVED_STAGE_WORDS:
        out["issues"].append(issue("error", "ambiguous_stage_key",
                                   f"stage key {key!r} is empty or a reserved role word"))
        return out
    out["stage_key_original"] = key
    out["stage_key"] = key.lower()
    return out


# ---------------------------------------------------------- classification

def _nitro_header(data: bytes) -> dict:
    res = {"magic": data[:4].decode("ascii", "replace")}
    if len(data) < 16:
        res["ok"] = False
        res["problem"] = "shorter than the 16-byte NITRO header"
        return res
    bom, version, filesize, header_size, blocks = struct.unpack_from("<HHIHH", data, 4)
    res.update({"bom": f"0x{bom:04X}", "version": version, "declared_size": filesize,
                "header_size": header_size, "block_count": blocks})
    problems = []
    if bom != 0xFEFF:
        problems.append("byte-order mark is not 0xFEFF")
    if filesize != len(data):
        problems.append(f"declared size {filesize} != actual {len(data)}")
    if header_size != 16:
        problems.append(f"header size {header_size} != 16")
    res["ok"] = not problems
    if problems:
        res["problem"] = "; ".join(problems)
    return res


def _payload_signature(payload: bytes) -> Tuple[Optional[str], Optional[str], Optional[dict]]:
    """Return (signature, payload_ext, nitro_header) for plain (uncompressed) bytes."""
    head = payload[:4]
    if head == STC_MAGIC:
        return "LCTS", "stc", None
    if head in NITRO_MAGIC_TO_EXT:
        return head.decode("ascii"), NITRO_MAGIC_TO_EXT[head], _nitro_header(payload)
    return None, None, None


def classify_bytes(data: bytes, max_output: int = lz10.DEFAULT_MAX_OUTPUT):
    """Classify by content. Returns (info_dict, payload_bytes_or_None).

    container: 'plain' | 'lz10' | 'lz10_invalid' | 'unknown' | 'empty'
    The payload is the plain content (or the decoded stream for lz10).
    """
    info = {"container": None, "signature": None, "payload_ext": None,
            "lz10": None, "nitro": None, "error": None}
    if not data:
        info["container"] = "empty"
        return info, None
    sig, pext, nitro = _payload_signature(data)
    if sig is not None:
        info.update(container="plain", signature=sig, payload_ext=pext, nitro=nitro)
        return info, data
    if data[0] == 0x10:
        try:
            decoded, lzinfo = lz10.decode(data, max_output=max_output)
        except lz10.LZ10Error as exc:
            info.update(container="lz10_invalid", error={"code": exc.code, "message": exc.message,
                                                         "offset": exc.offset})
            return info, None
        sig, pext, nitro = _payload_signature(decoded)
        info.update(container="lz10", signature=sig, payload_ext=pext, nitro=nitro, lz10=lzinfo.as_dict())
        return info, decoded
    info["container"] = "unknown"
    info["error"] = {"code": "unrecognized_content", "message": f"first bytes {data[:8].hex(' ')}",
                     "offset": 0}
    return info, None


# --------------------------------------------------------------- the index

def iter_files(root: str, subdirs: Iterable[str]) -> List[str]:
    """Relative POSIX paths of regular files under root/subdir, sorted. Skips dot-dirs."""
    found = []
    for sub in subdirs:
        base = os.path.join(root, sub)
        if not os.path.isdir(base):
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
            for fn in filenames:
                full = os.path.join(dirpath, fn)
                if os.path.isfile(full) and not os.path.islink(full):
                    found.append(os.path.relpath(full, root).replace(os.sep, "/"))
    return sorted(found)


def describe_asset(root: str, relpath: str, memo: Optional[dict] = None) -> dict:
    """Index one file. ``memo`` caches classification by source SHA-256."""
    full = os.path.join(root, relpath)
    name = parse_name(relpath)
    parts = relpath.split("/")
    layer = "/".join(parts[:-2]) if name["form"] == "grouped_role_file" else "/".join(parts[:-1])
    rec = {"path": relpath, "layer": layer, "stage_key": name["stage_key"],
           "stage_key_original": name["stage_key_original"], "name_ext": name["ext"],
           "name_form": name["form"], "role": EXT_TO_ROLE.get(name["ext"]) if name["ext"] else None}
    issues = list(name["issues"])
    size = os.path.getsize(full)
    rec["size"] = size
    if size > MAX_INPUT_BYTES:
        rec.update(container="too_large", issues=issues + [issue("error", "too_large",
                                                                 f"{size} bytes exceeds {MAX_INPUT_BYTES}")])
        return rec
    with open(full, "rb") as fh:
        data = fh.read()
    src_sha = sha256_hex(data)
    rec["sha256"] = src_sha
    rec["git_blob_sha1"] = git_blob_sha1_hex(data)
    if memo is not None and src_sha in memo:
        info, payload_sha, payload_size = memo[src_sha]
    else:
        info, payload = classify_bytes(data)
        payload_sha = sha256_hex(payload) if payload is not None else None
        payload_size = len(payload) if payload is not None else None
        if memo is not None:
            memo[src_sha] = (info, payload_sha, payload_size)
    rec.update({"container": info["container"], "signature": info["signature"],
                "payload_ext": info["payload_ext"], "payload_size": payload_size,
                "payload_sha256": payload_sha, "lz10": info["lz10"], "nitro": info["nitro"]})
    if info["error"]:
        issues.append(issue("error", info["error"]["code"], info["error"]["message"]))
    if info["container"] == "unknown":
        pass  # already reported via error
    if info["container"] == "lz10" and info["signature"] is None:
        issues.append(issue("warning", "unknown_decoded_signature", "LZ10 stream decodes to unrecognized data"))
    if info["nitro"] is not None and not info["nitro"].get("ok", False):
        issues.append(issue("warning", "nitro_header_inconsistent", info["nitro"].get("problem", "")))
    if info["lz10"] and info["lz10"]["trailing_bytes"] and not info["lz10"]["trailing_all_zero"]:
        issues.append(issue("warning", "lz10_nonzero_trailing_bytes",
                            f"{info['lz10']['trailing_bytes']} non-zero bytes after the stream"))
    # name vs content
    if rec["name_ext"] and info["payload_ext"] and rec["name_ext"] != info["payload_ext"]:
        issues.append(issue("error", "extension_signature_mismatch",
                            f"name says .{rec['name_ext']} but content is {info['signature']}"))
    if info["container"] == "lz10" and not name["lz_suffix"]:
        issues.append(issue("warning", "compressed_content_without_lz_suffix",
                            "file is LZ10-compressed but its name has no .lz suffix"))
    if info["container"] == "plain" and name["lz_suffix"]:
        issues.append(issue("warning", "lz_suffix_on_plain_content", "name ends in .lz but content is not compressed"))
    if info["container"] == "lz10" and any(p.lower() == "decompressed" for p in parts[:-1]):
        issues.append(issue("warning", "folder_name_contradicts_content",
                            "folder is named 'decompressed' but the file is LZ10-compressed"))
    if name["form"] == "unrecognized":
        issues.append(issue("warning", "unrecognized_name", "no stage key / extension could be derived from the name"))
    rec["issues"] = issues
    return rec


def build_index(root: str, subdirs: Iterable[str] = ("data",)) -> dict:
    subdirs = list(subdirs)
    memo: dict = {}
    assets = [describe_asset(root, p, memo) for p in iter_files(root, subdirs)]

    # Pair copies of the same (stage_key, ext) across layers.
    groups: Dict[Tuple[str, str], List[dict]] = {}
    for a in assets:
        if a.get("stage_key") and a.get("name_ext"):
            groups.setdefault((a["stage_key"], a["name_ext"]), []).append(a)
    # A stage key taken from a grouped directory name is only trusted when an
    # originally-named copy of the same content exists in another layer.
    for members in groups.values():
        originals = {m.get("payload_sha256") for m in members if m["name_form"] != "grouped_role_file"}
        for m in members:
            if m["name_form"] == "grouped_role_file" and m.get("payload_sha256") not in originals:
                m["issues"].append(issue("warning", "grouped_stage_key_unconfirmed",
                                         "stage key comes only from a directory name; no originally-named "
                                         "copy with the same content was found"))
    layers = sorted({a["layer"] for a in assets})
    pairs = []
    for (key, ext), members in sorted(groups.items()):
        payloads = sorted({m.get("payload_sha256") for m in members if m.get("payload_sha256")})
        by_layer: Dict[str, List[str]] = {}
        for m in members:
            by_layer.setdefault(m["layer"], []).append(m["path"])
        status = "consistent" if len(payloads) == 1 and all(m.get("payload_sha256") for m in members) \
            else ("undecodable_member" if len(payloads) <= 1 else "payload_mismatch")
        pairs.append({"stage_key": key, "ext": ext, "role": EXT_TO_ROLE.get(ext),
                      "layers": {k: sorted(v) for k, v in sorted(by_layer.items())},
                      "payload_sha256": payloads[0] if len(payloads) == 1 else payloads,
                      "status": status,
                      "multiple_in_one_layer": sorted(k for k, v in by_layer.items() if len(v) > 1)})
    missing_by_layer = {}
    for layer in layers:
        missing = [f"{p['stage_key']}.{p['ext']}" for p in pairs if layer not in p["layers"]]
        if missing:
            missing_by_layer[layer] = missing
    # Same payload under different stage keys (same type): flag, never merge.
    by_payload: Dict[Tuple[str, str], set] = {}
    for p in pairs:
        if isinstance(p["payload_sha256"], str):
            by_payload.setdefault((p["ext"], p["payload_sha256"]), set()).add(p["stage_key"])
    duplicates = [{"ext": ext, "payload_sha256": sha, "stage_keys": sorted(keys)}
                  for (ext, sha), keys in sorted(by_payload.items()) if len(keys) > 1]
    duplicates.sort(key=lambda d: (d["ext"], d["stage_keys"]))

    def count(pred):
        return sum(1 for a in assets if pred(a))

    container_of = {a["path"]: a.get("container") for a in assets}

    issue_counts: Dict[str, int] = {}
    for a in assets:
        for it in a.get("issues", []):
            k = f"{it['severity']}:{it['code']}"
            issue_counts[k] = issue_counts.get(k, 0) + 1
    container_by_layer: Dict[str, Dict[str, int]] = {}
    for a in assets:
        k = f"{a.get('container')}|{a.get('signature')}"
        container_by_layer.setdefault(a["layer"], {})
        container_by_layer[a["layer"]][k] = container_by_layer[a["layer"]].get(k, 0) + 1
    summary = {
        "files": len(assets),
        "layers": layers,
        "container_by_layer": {k: dict(sorted(v.items())) for k, v in sorted(container_by_layer.items())},
        "lz10_files": count(lambda a: a.get("container") == "lz10"),
        "lz10_invalid": count(lambda a: a.get("container") == "lz10_invalid"),
        "unknown_content": count(lambda a: a.get("container") in ("unknown", "empty", "too_large")),
        "pairs": len(pairs),
        "pairs_by_status": _count_by(pairs, "status"),
        "pairs_with_lz10_and_plain_copies": sum(
            1 for p in pairs if p["status"] == "consistent" and _has_both(p, container_of)),
        "duplicate_content_groups": len(duplicates),
        "issue_counts": dict(sorted(issue_counts.items())),
        "error_count": sum(v for k, v in issue_counts.items() if k.startswith("error:"))
                       + sum(1 for p in pairs if p["status"] != "consistent"),
    }
    return {"schema": "trcore.corpus_index/1", "scan_subdirs": subdirs, "summary": summary,
            "missing_by_layer": missing_by_layer, "duplicates": duplicates,
            "pairs": pairs, "assets": assets}


def _count_by(items, key):
    out: Dict[str, int] = {}
    for it in items:
        out[it[key]] = out.get(it[key], 0) + 1
    return dict(sorted(out.items()))


def _has_both(pair: dict, container_of: Dict[str, Optional[str]]) -> bool:
    kinds = {container_of.get(p) for v in pair["layers"].values() for p in v}
    return "lz10" in kinds and "plain" in kinds
