"""Keyframes for the stages' moving parts: keys with a curve type, baked to one value per frame.

The game stores sampled values (spec §18), so the keys and curve types live in the editor's project and
plan, and are baked here into one value per frame when the animation is written
(`trcore/nitro/nsbca_write.py`, ops `set_length` / `set_keys`).

A key is {"frame": int, "value": float, "interp": one of INTERPS, "handles": [x1, y1, x2, y2] (bezier)}.
`interp` is the curve from this key to the next one:
  linear       a straight line
  ease_in      starts slowly and speeds up           (CSS ease-in:     cubic-bezier(0.42, 0, 1, 1))
  ease_out     slows down towards the next key       (CSS ease-out:    cubic-bezier(0, 0, 0.58, 1))
  ease_in_out  slow at both ends                     (CSS ease-in-out: cubic-bezier(0.42, 0, 0.58, 1))
  bezier       your own timing curve: handles (x1, y1, x2, y2) as in CSS cubic-bezier; x within 0..1,
               y may leave 0..1 to overshoot
  smooth       a Catmull-Rom spline through the neighbouring keys (no handles)
  hold         keeps this key's value until the next key, then jumps
Before the first key the first value holds; after the last key the last value holds.

Channels: "tx", "ty", "tz" (position in stage units) and "rx", "ry", "rz" (turn in degrees). A part's
rotation is the three turns applied X first, then Y, then Z, about the part's own origin (row vectors,
v' = v . Rx . Ry . Rz, the model's convention). A positive Turn Y turns left seen from above, like an
object's facing.
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence, Tuple

INTERPS = ("linear", "ease_in", "ease_out", "ease_in_out", "bezier", "smooth", "hold")
INTERP_LABELS = {
    "linear": "Linear",
    "ease_in": "Ease In",
    "ease_out": "Ease Out",
    "ease_in_out": "Ease In/Out",
    "bezier": "Bézier",
    "smooth": "Smooth (spline)",
    "hold": "Hold",
}
INTERP_HELP = {
    "linear": "Constant speed to the next key.",
    "ease_in": "Starts slowly and speeds up towards the next key.",
    "ease_out": "Starts fast and slows down as it reaches the next key.",
    "ease_in_out": "Slow at both ends, fastest in the middle.",
    "bezier": "Your own timing curve: drag the two handles in the curve view.",
    "smooth": "A smooth spline through this key and its neighbours (no handles).",
    "hold": "Stays at this key's value, then jumps to the next key's value.",
}
EASE = {"linear": (0.0, 0.0, 1.0, 1.0), "ease_in": (0.42, 0.0, 1.0, 1.0), "ease_out": (0.0, 0.0, 0.58, 1.0),
        "ease_in_out": (0.42, 0.0, 0.58, 1.0)}
DEFAULT_HANDLES = (0.33, 0.0, 0.67, 1.0)
CHANNELS = ("tx", "ty", "tz", "rx", "ry", "rz")
CHANNEL_LABELS = {"tx": "Move X", "ty": "Move Y", "tz": "Move Z", "rx": "Turn X", "ry": "Turn Y", "rz": "Turn Z"}
MAX_FRAMES = 0x1FFF                      # a track's last-frame field has 13 bits (spec §18)
HANDLE_Y_LIMIT = 10.0


class KeyframeError(ValueError):
    pass


# ---------------------------------------------------------------------------- keys
def check_keys(keys, frames: int, label: str = "") -> List[dict]:
    """Validated, sorted copy of `keys` for an animation of `frames` frames (frames 0 .. frames - 1)."""
    if not isinstance(keys, (list, tuple)) or not keys:
        raise KeyframeError(f"{label}: needs at least one key")
    out, seen = [], set()
    for k in keys:
        if not isinstance(k, dict):
            raise KeyframeError(f"{label}: a key must be an object")
        try:
            f = int(k["frame"])
            v = float(k["value"])
        except (KeyError, TypeError, ValueError):
            raise KeyframeError(f"{label}: a key needs 'frame' and 'value'") from None
        if f != k["frame"] and not (isinstance(k["frame"], float) and k["frame"].is_integer()):
            raise KeyframeError(f"{label}: key frames are whole numbers")
        if not 0 <= f < frames:
            raise KeyframeError(f"{label}: key at frame {f} is outside the animation (0 to {frames - 1})")
        if f in seen:
            raise KeyframeError(f"{label}: two keys at frame {f}")
        if not math.isfinite(v):
            raise KeyframeError(f"{label}: key value must be a number")
        interp = k.get("interp", "linear")
        if interp not in INTERPS:
            raise KeyframeError(f"{label}: unknown curve type {interp!r}")
        nk = {"frame": f, "value": v, "interp": interp}
        if interp == "bezier":
            h = k.get("handles", DEFAULT_HANDLES)
            try:
                h = [float(x) for x in h]
            except (TypeError, ValueError):
                raise KeyframeError(f"{label}: bezier handles must be four numbers") from None
            if len(h) != 4 or not all(math.isfinite(x) for x in h):
                raise KeyframeError(f"{label}: bezier handles must be four numbers")
            if not (0.0 <= h[0] <= 1.0 and 0.0 <= h[2] <= 1.0):
                raise KeyframeError(f"{label}: bezier handle x values must be within 0 and 1")
            if abs(h[1]) > HANDLE_Y_LIMIT or abs(h[3]) > HANDLE_Y_LIMIT:
                raise KeyframeError(f"{label}: bezier handle y values must be within ±{HANDLE_Y_LIMIT:g}")
            nk["handles"] = h
        seen.add(f)
        out.append(nk)
    out.sort(key=lambda k: k["frame"])
    return out


def cubic_bezier(x1: float, y1: float, x2: float, y2: float, u: float) -> float:
    """CSS cubic-bezier timing: progress at time u (0..1) of the curve (0,0) (x1,y1) (x2,y2) (1,1)."""
    if u <= 0.0:
        return 0.0
    if u >= 1.0:
        return 1.0

    def bx(t):
        return 3 * (1 - t) ** 2 * t * x1 + 3 * (1 - t) * t * t * x2 + t ** 3

    def by(t):
        return 3 * (1 - t) ** 2 * t * y1 + 3 * (1 - t) * t * t * y2 + t ** 3

    def dbx(t):
        return 3 * (1 - t) ** 2 * x1 + 6 * (1 - t) * t * (x2 - x1) + 3 * t * t * (1 - x2)

    t = u
    for _ in range(8):                       # Newton first, bisection if it does not converge
        err = bx(t) - u
        if abs(err) < 1e-9:
            return by(t)
        d = dbx(t)
        if abs(d) < 1e-9:
            break
        t -= err / d
        if not 0.0 <= t <= 1.0:
            break
    lo, hi = 0.0, 1.0
    t = u
    for _ in range(60):
        x = bx(t)
        if abs(x - u) < 1e-10:
            break
        if x < u:
            lo = t
        else:
            hi = t
        t = (lo + hi) / 2
    return by(t)


def _hermite(v0, v1, m0, m1, df, u):
    u2, u3 = u * u, u * u * u
    return ((2 * u3 - 3 * u2 + 1) * v0 + (u3 - 2 * u2 + u) * df * m0 + (-2 * u3 + 3 * u2) * v1 +
            (u3 - u2) * df * m1)


def _tangent(keys, i):
    """Catmull-Rom tangent (value per frame) at key i, one-sided at the ends."""
    n = len(keys)
    if n < 2:
        return 0.0
    a = keys[max(0, i - 1)]
    b = keys[min(n - 1, i + 1)]
    df = b["frame"] - a["frame"]
    return (b["value"] - a["value"]) / df if df else 0.0


def segment_value(keys: Sequence[dict], i: int, frame: float) -> float:
    """Value at `frame` on the segment from key i to key i + 1."""
    a, b = keys[i], keys[i + 1]
    df = b["frame"] - a["frame"]
    u = (frame - a["frame"]) / df
    interp = a["interp"]
    if interp == "hold":
        return a["value"] if u < 1.0 else b["value"]
    if interp == "smooth":
        return _hermite(a["value"], b["value"], _tangent(keys, i), _tangent(keys, i + 1), df, u)
    h = a.get("handles", DEFAULT_HANDLES) if interp == "bezier" else EASE[interp]
    p = u if interp == "linear" else cubic_bezier(h[0], h[1], h[2], h[3], u)
    return a["value"] + (b["value"] - a["value"]) * p


def value_at(keys: Sequence[dict], frame: float) -> float:
    if frame <= keys[0]["frame"]:
        return keys[0]["value"]
    if frame >= keys[-1]["frame"]:
        return keys[-1]["value"]
    lo, hi = 0, len(keys) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if keys[mid]["frame"] <= frame:
            lo = mid
        else:
            hi = mid
    return segment_value(keys, lo, frame)


def bake(keys, frames: int, label: str = "") -> List[float]:
    """One value per frame (0 .. frames - 1) from `keys`."""
    ks = check_keys(keys, frames, label)
    return [value_at(ks, g) for g in range(frames)]


# ---------------------------------------------------------------------------- keys from samples
def _fit_error(values, i0, i1, interp):
    v0, v1 = values[i0], values[i1]
    ks = [{"frame": i0, "value": v0, "interp": interp}, {"frame": i1, "value": v1, "interp": "linear"}]
    worst, at = 0.0, (i0 + i1) // 2
    for g in range(i0 + 1, i1):
        e = abs(segment_value(ks, 0, g) - values[g])
        if e > worst:
            worst, at = e, g
    return worst, at


def _fit_bezier(values, i0, i1):
    """Bézier handles that best match the segment i0..i1 (least squares, Nelder-Mead), or None."""
    v0, v1 = values[i0], values[i1]
    if abs(v1 - v0) < 1e-12 or i1 - i0 < 3:
        return None
    pts = [((g - i0) / (i1 - i0), (values[g] - v0) / (v1 - v0)) for g in range(i0 + 1, i1)]

    def clamp(h):
        return (min(1.0, max(0.0, h[0])), min(3.0, max(-2.0, h[1])), min(1.0, max(0.0, h[2])),
                min(3.0, max(-2.0, h[3])))

    def cost(h):
        h = clamp(h)
        return sum((cubic_bezier(h[0], h[1], h[2], h[3], u) - p) ** 2 for u, p in pts)
    simplex = [(0.42, 0.0, 0.58, 1.0)]
    for k in range(4):
        q = list(simplex[0])
        q[k] += 0.15
        simplex.append(tuple(q))
    vals = [cost(h) for h in simplex]
    for _ in range(160):
        order = sorted(range(5), key=lambda j: vals[j])
        simplex, vals = [simplex[j] for j in order], [vals[j] for j in order]
        if vals[-1] - vals[0] < 1e-14:
            break
        cen = [sum(h[k] for h in simplex[:4]) / 4 for k in range(4)]
        refl = tuple(cen[k] + (cen[k] - simplex[4][k]) for k in range(4))
        fr = cost(refl)
        if fr < vals[0]:
            exp = tuple(cen[k] + 2 * (cen[k] - simplex[4][k]) for k in range(4))
            fe = cost(exp)
            simplex[4], vals[4] = (exp, fe) if fe < fr else (refl, fr)
        elif fr < vals[3]:
            simplex[4], vals[4] = refl, fr
        else:
            con = tuple(cen[k] + 0.5 * (simplex[4][k] - cen[k]) for k in range(4))
            fc = cost(con)
            if fc < vals[4]:
                simplex[4], vals[4] = con, fc
            else:
                simplex = [simplex[0]] + [tuple(simplex[0][k] + 0.5 * (h[k] - simplex[0][k]) for k in range(4))
                                          for h in simplex[1:]]
                vals = [vals[0]] + [cost(h) for h in simplex[1:]]
    best = clamp(simplex[min(range(5), key=lambda j: vals[j])])
    return [round(x, 3) for x in best]


def keys_from_samples(values: Sequence[float], tol: Optional[float] = None, unit: float = 1.0 / 4096) -> List[dict]:
    """Few keys that reproduce per-frame `values` within `tol` (default: 1 % of their range, at least one
    storage unit). Keys go at the turning points; each segment gets the first of Linear, Ease In/Out, Ease
    In, Ease Out or a fitted Bézier that fits, and is split where none does."""
    n = len(values)
    if n == 0:
        return []
    if tol is None:
        tol = max(unit, 0.01 * (max(values) - min(values)))
    if n == 1:
        return [{"frame": 0, "value": float(values[0]), "interp": "linear"}]
    pts = {0, n - 1}
    for i in range(1, n - 1):
        d0, d1 = values[i] - values[i - 1], values[i + 1] - values[i]
        if d0 * d1 < 0 or (abs(d0) <= 1e-12) != (abs(d1) <= 1e-12):
            pts.add(i)
    pts = sorted(pts)
    out: List[Tuple[int, str, Optional[list]]] = []
    stack = [(pts[k], pts[k + 1]) for k in range(len(pts) - 2, -1, -1)]
    while stack:
        i0, i1 = stack.pop()
        best = None
        for interp in ("linear", "ease_in_out", "ease_in", "ease_out"):
            err, _at = _fit_error(values, i0, i1, interp)
            if best is None or err < best[0] - 1e-12:
                best = (err, interp, None)
            if err <= tol:
                break
        if best[0] > tol:
            h = _fit_bezier(values, i0, i1)
            if h is not None:
                ks = [{"frame": i0, "value": values[i0], "interp": "bezier", "handles": h},
                      {"frame": i1, "value": values[i1], "interp": "linear"}]
                err = max((abs(segment_value(ks, 0, g) - values[g]) for g in range(i0 + 1, i1)), default=0.0)
                if err < best[0]:
                    best = (err, "bezier", h)
        err, interp, h = best
        if err <= tol or i1 - i0 <= 1:
            out.append((i0, interp, h))
        else:
            _e, at = _fit_error(values, i0, i1, "linear")
            at = min(max(at, i0 + 1), i1 - 1)
            stack.append((at, i1))
            stack.append((i0, at))
    keys = []
    for f, interp, h in out:
        k = {"frame": f, "value": float(values[f]), "interp": interp}
        if h is not None:
            k["handles"] = h
        keys.append(k)
    keys.append({"frame": n - 1, "value": float(values[n - 1]), "interp": "linear"})
    return keys


# ---------------------------------------------------------------------------- rotations
def euler_matrix(rx: float, ry: float, rz: float):
    """Rows of Rx . Ry . Rz (degrees; row vectors: X first, then Y, then Z)."""
    a, b, c = (math.radians(v) for v in (rx, ry, rz))
    ca, sa, cb, sb, cc, sc = math.cos(a), math.sin(a), math.cos(b), math.sin(b), math.cos(c), math.sin(c)
    X = ((1.0, 0.0, 0.0), (0.0, ca, sa), (0.0, -sa, ca))
    Y = ((cb, 0.0, -sb), (0.0, 1.0, 0.0), (sb, 0.0, cb))
    Z = ((cc, sc, 0.0), (-sc, cc, 0.0), (0.0, 0.0, 1.0))

    def mul(p, q):
        return tuple(tuple(sum(p[i][k] * q[k][j] for k in range(3)) for j in range(3)) for i in range(3))
    return mul(mul(X, Y), Z)


def matrix_euler(m) -> Tuple[float, float, float]:
    """Inverse of euler_matrix (degrees). Turn Y within ±90."""
    cb = math.hypot(m[1][2], m[2][2])
    b = math.atan2(-m[0][2], cb)
    if cb > 1e-9:
        a = math.atan2(m[1][2], m[2][2])
        c = math.atan2(m[0][1], m[0][0])
    else:                                    # gimbal lock: put everything into Turn X
        a = math.atan2(-m[2][1], m[1][1])
        c = 0.0
    return math.degrees(a), math.degrees(b), math.degrees(c)


def _near(v: float, ref: float) -> float:
    return v + 360.0 * round((ref - v) / 360.0)


def euler_sequence(matrices) -> List[Tuple[float, float, float]]:
    """Turns for a run of rotation matrices, continuous from frame to frame: of the two angle triples that
    give each matrix, the one closest to the previous frame is kept, so a part spinning about any one
    axis gives one steadily growing angle (Turn Y may pass ±90)."""
    out: List[Tuple[float, float, float]] = []
    for m in matrices:
        a, b, c = matrix_euler(m)
        if not out:
            out.append((a, b, c))
            continue
        pa, pb, pc = out[-1]
        best = None
        for t in ((a, b, c), (a + 180.0, 180.0 - b, c + 180.0)):
            cand = (_near(t[0], pa), _near(t[1], pb), _near(t[2], pc))
            d = abs(cand[0] - pa) + abs(cand[1] - pb) + abs(cand[2] - pc)
            if best is None or d < best[0] - 1e-9:
                best = (d, cand)
        out.append(best[1])
    return out


def unwrap(values: Sequence[float]) -> List[float]:
    """Angles in degrees made continuous (no jumps of 360)."""
    out: List[float] = []
    for v in values:
        if out:
            prev = out[-1]
            v = v + 360.0 * round((prev - v) / 360.0)
        out.append(v)
    return out


# ---------------------------------------------------------------------------- reading channels
def _rot_rows(r):
    return r if isinstance(r, tuple) and len(r) == 3 and isinstance(r[0], tuple) else None


def channel_kind(node, ch: str) -> str:
    """'moving' (stored per frame), 'fixed' (one value) or 'none' (identity / rest, nothing stored) for a
    trcore.nitro.nsbca NodeAnim channel."""
    from .nitro.nsbca import Track
    if ch[0] == "t":
        if not isinstance(node.t, list):
            return "none" if node.t is None else "rest"
        return "moving" if isinstance(node.t["xyz".index(ch[1])], Track) else "fixed"
    if isinstance(node.r, Track):
        return "moving"
    if node.r is None:
        return "none"
    return "rest" if node.r == "rest" else "fixed"


def node_channels(node, frames: int) -> Dict[str, List[float]]:
    """Per-frame values of the six channels of a NodeAnim (translation in units, turns in degrees,
    unwrapped). Rotations between stored samples follow the reader's interpolation."""
    out: Dict[str, List[float]] = {}
    ts = [[], [], []]
    mats = []
    ident = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
    for g in range(frames):
        t, r, _s = node.pose(g)
        for ax in range(3):
            ts[ax].append(t[ax] if isinstance(t, tuple) else 0.0)
        mats.append(_rot_rows(r) or ident)
    eul = euler_sequence(mats)
    for ax in range(3):
        out["t" + "xyz"[ax]] = ts[ax]
        out["r" + "xyz"[ax]] = [e[ax] for e in eul]
    return out
