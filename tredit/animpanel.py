"""The keyframe editor (the Animation dock).

Left: the stage's moving parts and their channels (Move X/Y/Z, Turn X/Y/Z). Middle: a curve view with the
original motion in grey, the motion as it will be saved, the keys and the current frame. Right: the
animation's length, the selected key's frame, value and curve type.

The panel never changes the stage itself: it emits keysCommitted / lengthCommitted and the main window
calls tredit.project (undo, plan, save, verification), then refreshes the panel.
"""
from __future__ import annotations

import math
from typing import List, Optional

from PySide6 import QtCore, QtGui, QtWidgets

from trcore.keyframes import (DEFAULT_HANDLES, INTERP_HELP, INTERP_LABELS, INTERPS, KeyframeError, bake,
                              check_keys, value_at)

Qt = QtCore.Qt
Signal = QtCore.Signal

CHANNEL_NAMES = {
    "tx": "Move X (right +)", "ty": "Move Y (up +)", "tz": "Move Z (back +)",
    "rx": "Turn X (tilt)", "ry": "Turn Y (turn, left +)", "rz": "Turn Z (roll)",
}
KIND_TEXT = {"moving": "moves", "fixed": "fixed", "none": "fixed", "rest": "fixed"}
ACCENT = QtGui.QColor(38, 120, 220)
SELECTED = QtGui.QColor(245, 150, 30)
CURSOR = QtGui.QColor(220, 60, 60)


def _nice_step(span: float, target: float) -> float:
    if span <= 0:
        return 1.0
    raw = span / max(1.0, target)
    mag = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 5, 10):
        if m * mag >= raw:
            return m * mag
    return 10 * mag


class CurveView(QtWidgets.QWidget):
    """One channel's curve. Keys can be dragged (frame and value), added with a double-click, deleted with
    Del; a Bézier key shows its two timing handles. Clicking elsewhere moves the current frame."""
    keysEdited = Signal(list)          # finished change: the whole key list
    keySelected = Signal(int)          # index into the keys, or -1
    keyMoving = Signal(int, int, float)
    frameScrubbed = Signal(float)

    ML, MR, MT, MB = 58, 14, 12, 26

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(280, 170)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMouseTracking(True)
        self.frames = 1
        self.original: List[float] = []
        self.current: List[float] = []
        self.keys: Optional[List[dict]] = None
        self.sel = -1
        self.frame = 0.0
        self.editable = False
        self.angle = False
        self.message = ""
        self.vlo, self.vhi = -1.0, 1.0
        self._work: Optional[List[dict]] = None     # keys while dragging
        self._preview: Optional[List[float]] = None
        self._drag = None

    # ---- data
    def set_data(self, frames: int, original, current, keys, editable: bool, angle: bool, message: str = "",
                 refit: bool = True):
        self.frames = max(1, frames)
        self.original, self.current = list(original), list(current)
        self.keys = [dict(k) for k in keys] if keys else None
        self.editable, self.angle, self.message = editable, angle, message
        if self.keys is None or not 0 <= self.sel < len(self.keys):
            self.sel = -1
        self._work = self._preview = None
        self._drag = None
        vals = self.original + self.current + [k["value"] for k in self.keys or []]
        if refit or (vals and (min(vals) < self.vlo or max(vals) > self.vhi)):
            self.fit()
        self.update()

    def set_frame(self, f: float):
        self.frame = f
        self.update()

    def select(self, i: int):
        self.sel = i if self.keys and 0 <= i < len(self.keys) else -1
        self.update()

    def fit(self):
        vals = self.original + self.current + [k["value"] for k in self.keys or []]
        if not vals:
            self.vlo, self.vhi = -1.0, 1.0
            return
        lo, hi = min(vals), max(vals)
        min_span = 10.0 if self.angle else 1.0
        if hi - lo < min_span:
            mid = (hi + lo) / 2
            lo, hi = mid - min_span / 2, mid + min_span / 2
        pad = (hi - lo) * 0.12
        self.vlo, self.vhi = lo - pad, hi + pad

    # ---- mapping
    def _rect(self) -> QtCore.QRectF:
        return QtCore.QRectF(self.ML, self.MT, max(10, self.width() - self.ML - self.MR),
                             max(10, self.height() - self.MT - self.MB))

    def _x(self, f: float) -> float:
        r = self._rect()
        return r.left() + r.width() * (f / max(1, self.frames - 1))

    def _f(self, x: float) -> float:
        r = self._rect()
        return (x - r.left()) / r.width() * max(1, self.frames - 1)

    def _y(self, v: float) -> float:
        r = self._rect()
        return r.bottom() - r.height() * (v - self.vlo) / ((self.vhi - self.vlo) or 1.0)

    def _v(self, y: float) -> float:
        r = self._rect()
        return self.vlo + (r.bottom() - y) / r.height() * ((self.vhi - self.vlo) or 1.0)

    def _keys(self) -> Optional[List[dict]]:
        return self._work if self._work is not None else self.keys

    def _handle_points(self, keys, i):
        """Pixel positions of key i's two Bézier handles (or None)."""
        if not keys or not 0 <= i < len(keys) - 1 or keys[i]["interp"] != "bezier":
            return None
        a, b = keys[i], keys[i + 1]
        h = a.get("handles") or DEFAULT_HANDLES
        df = b["frame"] - a["frame"]
        dv = b["value"] - a["value"]
        if abs(dv) < 1e-9:
            dv = (self.vhi - self.vlo) * 0.25
        p1 = QtCore.QPointF(self._x(a["frame"] + h[0] * df), self._y(a["value"] + h[1] * dv))
        p2 = QtCore.QPointF(self._x(a["frame"] + h[2] * df), self._y(a["value"] + h[3] * dv))
        return p1, p2, df, dv

    # ---- painting
    def paintEvent(self, _ev):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        pal = self.palette()
        r = self._rect()
        p.fillRect(self.rect(), pal.window())
        p.fillRect(r, pal.base())
        grid = pal.mid().color()
        grid.setAlpha(90)
        text = pal.text().color()
        small = QtGui.QFont(self.font())
        small.setPointSizeF(max(7.0, self.font().pointSizeF() * 0.85))
        p.setFont(small)
        # frames
        fstep = max(1, int(_nice_step(self.frames - 1, r.width() / 70)))
        f = 0
        while f <= self.frames - 1:
            x = self._x(f)
            p.setPen(QtGui.QPen(grid, 1))
            p.drawLine(QtCore.QPointF(x, r.top()), QtCore.QPointF(x, r.bottom()))
            p.setPen(text)
            p.drawText(QtCore.QRectF(x - 30, r.bottom() + 3, 60, 14), Qt.AlignHCenter, str(f))
            f += fstep
        # values
        vstep = _nice_step(self.vhi - self.vlo, r.height() / 34)
        v = math.ceil(self.vlo / vstep) * vstep
        while v <= self.vhi + 1e-9:
            y = self._y(v)
            pen = QtGui.QPen(grid.darker(140) if abs(v) < vstep / 2 else grid, 1)
            p.setPen(pen)
            p.drawLine(QtCore.QPointF(r.left(), y), QtCore.QPointF(r.right(), y))
            p.setPen(text)
            lab = f"{v:g}°" if self.angle else f"{v:g}"
            p.drawText(QtCore.QRectF(0, y - 7, self.ML - 6, 14), Qt.AlignRight | Qt.AlignVCenter, lab)
            v += vstep
        p.setPen(text)
        p.drawText(QtCore.QRectF(r.left(), self.height() - 13, r.width(), 13), Qt.AlignRight,
                   "frame (60 = 1 s)")
        p.setClipRect(r.adjusted(-6, -6, 6, 6))

        def poly(vals):
            path = QtGui.QPainterPath()
            for g, val in enumerate(vals):
                pt = QtCore.QPointF(self._x(g), self._y(val))
                if g == 0:
                    path.moveTo(pt)
                else:
                    path.lineTo(pt)
            return path
        if self.original:
            pen = QtGui.QPen(QtGui.QColor(150, 150, 150), 1.5, Qt.DashLine)
            p.setPen(pen)
            p.drawPath(poly(self.original))
        cur = self._preview if self._preview is not None else self.current
        if cur and (self.keys or self._work is not None or cur != self.original):
            p.setPen(QtGui.QPen(ACCENT, 2.0))
            p.drawPath(poly(cur))
        keys = self._keys()
        if keys:
            hp = self._handle_points(keys, self.sel)
            if hp:
                p1, p2, _df, _dv = hp
                a, b = keys[self.sel], keys[self.sel + 1]
                pa = QtCore.QPointF(self._x(a["frame"]), self._y(a["value"]))
                pb = QtCore.QPointF(self._x(b["frame"]), self._y(b["value"]))
                p.setPen(QtGui.QPen(SELECTED, 1))
                p.drawLine(pa, p1)
                p.drawLine(pb, p2)
                p.setBrush(SELECTED)
                for q in (p1, p2):
                    p.drawEllipse(q, 4, 4)
            for i, k in enumerate(keys):
                c = QtCore.QPointF(self._x(k["frame"]), self._y(k["value"]))
                d = 6.0
                diamond = QtGui.QPolygonF([c + QtCore.QPointF(0, -d), c + QtCore.QPointF(d, 0),
                                           c + QtCore.QPointF(0, d), c + QtCore.QPointF(-d, 0)])
                if not self.editable:
                    p.setPen(QtGui.QPen(QtGui.QColor(130, 130, 130), 1.5))
                    p.setBrush(pal.base())
                elif i == self.sel:
                    p.setPen(QtGui.QPen(SELECTED.darker(130), 1.5))
                    p.setBrush(SELECTED)
                else:
                    p.setPen(QtGui.QPen(ACCENT, 1.5))
                    p.setBrush(pal.base())
                p.drawPolygon(diamond)
        p.setClipping(False)
        x = self._x(self.frame)
        p.setPen(QtGui.QPen(CURSOR, 1.5))
        p.drawLine(QtCore.QPointF(x, r.top()), QtCore.QPointF(x, r.bottom()))
        if self.message:
            p.setPen(text)
            box = r.adjusted(12, 8, -12, -8)
            p.drawText(box, Qt.AlignTop | Qt.AlignHCenter | Qt.TextWordWrap, self.message)
        p.end()

    # ---- hit tests
    def _key_at(self, pos) -> int:
        keys = self._keys() or []
        best, bd = -1, 9.0
        for i, k in enumerate(keys):
            d = math.hypot(self._x(k["frame"]) - pos.x(), self._y(k["value"]) - pos.y())
            if d < bd:
                best, bd = i, d
        return best

    def _handle_at(self, pos) -> Optional[str]:
        hp = self._handle_points(self._keys(), self.sel)
        if not hp:
            return None
        for name, q in (("h1", hp[0]), ("h2", hp[1])):
            if math.hypot(q.x() - pos.x(), q.y() - pos.y()) < 8:
                return name
        return None

    def _snap(self, v: float, fine: bool) -> float:
        if self.angle:
            step = 0.01 if fine else 0.5
        else:
            step = 1.0 / 4096 if fine else 0.05
        return round(v / step) * step

    def _rebake(self):
        try:
            self._preview = bake(self._work, self.frames)
        except KeyframeError:
            self._preview = None

    # ---- mouse
    def mousePressEvent(self, ev):
        self.setFocus()
        if ev.button() != Qt.LeftButton:
            return
        pos = ev.position()
        if self.editable and self.keys:
            h = self._handle_at(pos)
            if h:
                self._work = [dict(k) for k in self.keys]
                self._drag = (h, self.sel)
                return
            i = self._key_at(pos)
            if i >= 0:
                self.sel = i
                self.keySelected.emit(i)
                self._work = [dict(k) for k in self.keys]
                self._drag = ("key", i)
                self.update()
                return
        elif self.keys:
            i = self._key_at(pos)
            if i >= 0:
                self.sel = i
                self.keySelected.emit(i)
                self.update()
                return
        self._drag = ("scrub",)
        self._scrub(pos.x())

    def _scrub(self, x):
        f = max(0.0, min(float(self.frames - 1), round(self._f(x))))
        self.frame = f
        self.frameScrubbed.emit(f)
        self.update()

    def mouseMoveEvent(self, ev):
        pos = ev.position()
        if not self._drag:
            over = self.editable and (self._handle_at(pos) or self._key_at(pos) >= 0)
            self.setCursor(Qt.PointingHandCursor if over else Qt.ArrowCursor)
            return
        fine = bool(ev.modifiers() & Qt.ShiftModifier)
        kind = self._drag[0]
        if kind == "scrub":
            self._scrub(pos.x())
            return
        i = self._drag[1]
        keys = self._work
        if kind == "key":
            lo = keys[i - 1]["frame"] + 1 if i > 0 else 0
            hi = keys[i + 1]["frame"] - 1 if i + 1 < len(keys) else self.frames - 1
            f = int(max(lo, min(hi, round(self._f(pos.x())))))
            v = self._snap(self._v(pos.y()), fine)
            keys[i] = dict(keys[i], frame=f, value=v)
            self.keyMoving.emit(i, f, v)
        else:
            hp = self._handle_points(keys, i)
            if not hp:
                return
            _p1, _p2, df, dv = hp
            a = keys[i]
            hx = (self._f(pos.x()) - a["frame"]) / df
            hy = (self._v(pos.y()) - a["value"]) / dv
            h = list(a.get("handles") or DEFAULT_HANDLES)
            j = 0 if kind == "h1" else 2
            h[j] = round(max(0.0, min(1.0, hx)), 3)
            h[j + 1] = round(max(-9.5, min(9.5, hy)), 3)
            keys[i] = dict(a, handles=h)
        self._rebake()
        self.update()

    def mouseReleaseEvent(self, ev):
        drag, self._drag = self._drag, None
        if drag and drag[0] != "scrub" and self._work is not None:
            work, self._work = self._work, None
            if work != self.keys:
                self.keys = work
                self.keysEdited.emit([dict(k) for k in work])
                return
            self._preview = None
            self.update()

    def mouseDoubleClickEvent(self, ev):
        if not (self.editable and self.keys) or ev.button() != Qt.LeftButton:
            return
        pos = ev.position()
        if self._key_at(pos) >= 0:
            return
        f = int(max(0, min(self.frames - 1, round(self._f(pos.x())))))
        if any(k["frame"] == f for k in self.keys):
            return
        prev = [k for k in self.keys if k["frame"] < f]
        interp = prev[-1]["interp"] if prev else self.keys[0]["interp"]
        nk = {"frame": f, "value": round(value_at(self.keys, f), 6), "interp": interp}
        if interp == "bezier":
            nk["handles"] = list(DEFAULT_HANDLES)
        keys = sorted([dict(k) for k in self.keys] + [nk], key=lambda k: k["frame"])
        self.sel = next(i for i, k in enumerate(keys) if k["frame"] == f)
        self.keys = keys
        self.keysEdited.emit([dict(k) for k in keys])

    def contextMenuEvent(self, ev):
        if not (self.editable and self.keys):
            return
        i = self._key_at(ev.pos())
        if i < 0:
            return
        self.sel = i
        self.keySelected.emit(i)
        m = QtWidgets.QMenu(self)
        for t in INTERPS:
            a = m.addAction(f"Curve to the next key: {INTERP_LABELS[t]}")
            a.setCheckable(True)
            a.setChecked(self.keys[i]["interp"] == t)
            a.setToolTip(INTERP_HELP[t])
            a.triggered.connect(lambda _c=False, t=t: self.set_interp(t))
        m.addSeparator()
        d = m.addAction("Delete key")
        d.setEnabled(len(self.keys) > 1)
        d.triggered.connect(self.delete_selected)
        m.exec(ev.globalPos())

    # ---- key changes
    def set_interp(self, t: str):
        if not (self.editable and self.keys and 0 <= self.sel < len(self.keys)):
            return
        keys = [dict(k) for k in self.keys]
        keys[self.sel]["interp"] = t
        if t == "bezier":
            keys[self.sel].setdefault("handles", list(DEFAULT_HANDLES))
        else:
            keys[self.sel].pop("handles", None)
        self.keys = keys
        self.keysEdited.emit([dict(k) for k in keys])

    def delete_selected(self):
        if not (self.editable and self.keys and 0 <= self.sel < len(self.keys)) or len(self.keys) < 2:
            return
        keys = [dict(k) for j, k in enumerate(self.keys) if j != self.sel]
        self.sel = min(self.sel, len(keys) - 1)
        self.keys = keys
        self.keysEdited.emit([dict(k) for k in keys])

    def _nudge(self, df: int, dv: float):
        keys = [dict(k) for k in self.keys]
        i = self.sel
        k = keys[i]
        lo = keys[i - 1]["frame"] + 1 if i > 0 else 0
        hi = keys[i + 1]["frame"] - 1 if i + 1 < len(keys) else self.frames - 1
        keys[i] = dict(k, frame=int(max(lo, min(hi, k["frame"] + df))), value=round(k["value"] + dv, 6))
        if keys != self.keys:
            self.keys = keys
            self.keysEdited.emit([dict(x) for x in keys])

    def event(self, ev):
        if ev.type() == QtCore.QEvent.ShortcutOverride and self.editable and self.keys and self.sel >= 0:
            if ev.key() in (Qt.Key_Delete, Qt.Key_Backspace, Qt.Key_Left, Qt.Key_Right, Qt.Key_Up, Qt.Key_Down):
                ev.accept()
                return True
        return super().event(ev)

    def keyPressEvent(self, ev):
        if self.editable and self.keys and self.sel >= 0:
            fine = bool(ev.modifiers() & Qt.ShiftModifier)
            step = (0.01 if fine else 0.5) if self.angle else (1.0 / 4096 if fine else 0.05)
            k = ev.key()
            if k in (Qt.Key_Delete, Qt.Key_Backspace):
                self.delete_selected()
                return
            if k in (Qt.Key_Left, Qt.Key_Right):
                self._nudge(-1 if k == Qt.Key_Left else 1, 0.0)
                return
            if k in (Qt.Key_Up, Qt.Key_Down):
                self._nudge(0, step if k == Qt.Key_Up else -step)
                return
        super().keyPressEvent(ev)


class AnimationPanel(QtWidgets.QWidget):
    keysCommitted = Signal(int, str, object)       # node, channel, keys (list) or None (original motion)
    lengthCommitted = Signal(object)               # frames (int) or None (original length)
    frameScrubbed = Signal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.project = None
        self.warn_size = True                       # Settings > Warnings
        self.key: Optional[str] = None
        self.node: Optional[int] = None
        self.channel: Optional[str] = None
        self._tree_key = None
        self._loading = False
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        split = QtWidgets.QSplitter(Qt.Horizontal)
        lay.addWidget(split)

        self.tree = QtWidgets.QTreeWidget()
        self.tree.setHeaderLabels(["Moving part / channel", ""])
        self.tree.setColumnCount(2)
        self.tree.setMinimumWidth(230)
        self.tree.header().setStretchLastSection(False)
        self.tree.header().setSectionResizeMode(0, QtWidgets.QHeaderView.Stretch)
        self.tree.header().setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeToContents)
        self.tree.currentItemChanged.connect(self._tree_changed)
        split.addWidget(self.tree)

        self.curve = CurveView()
        self.curve.keysEdited.connect(self._keys_edited)
        self.curve.keySelected.connect(lambda _i: self._show_key())
        self.curve.keyMoving.connect(self._key_moving)
        self.curve.frameScrubbed.connect(self.frameScrubbed)
        split.addWidget(self.curve)

        side = QtWidgets.QWidget()
        side.setMinimumWidth(250)
        side.setMaximumWidth(330)
        sl = QtWidgets.QVBoxLayout(side)
        sl.setContentsMargins(4, 0, 0, 0)
        # length
        g1 = QtWidgets.QGroupBox("Animation length")
        f1 = QtWidgets.QGridLayout(g1)
        self.len_spin = QtWidgets.QSpinBox()
        self.len_spin.setRange(2, 8191)
        self.len_spin.setSuffix(" frames")
        self.len_secs = QtWidgets.QLabel("")
        self.len_apply = QtWidgets.QPushButton("Apply")
        self.len_apply.setToolTip("Stretch the whole animation (every part, every key) to this length")
        self.len_reset = QtWidgets.QPushButton("Original")
        self.len_reset.setToolTip("Back to the stage's own length")
        self.len_spin.valueChanged.connect(lambda v: self.len_secs.setText(f"= {(v - 1) / 60:.2f} s per loop"))
        self.len_apply.clicked.connect(lambda: self.lengthCommitted.emit(self.len_spin.value()))
        self.len_reset.clicked.connect(lambda: self.lengthCommitted.emit(None))
        f1.addWidget(self.len_spin, 0, 0)
        f1.addWidget(self.len_secs, 0, 1)
        f1.addWidget(self.len_apply, 1, 0)
        f1.addWidget(self.len_reset, 1, 1)
        self.len_note = QtWidgets.QLabel("")
        self.len_note.setWordWrap(True)
        self.len_note.setStyleSheet("color: gray;")
        f1.addWidget(self.len_note, 2, 0, 1, 2)
        sl.addWidget(g1)
        # channel
        g2 = QtWidgets.QGroupBox("Channel")
        f2 = QtWidgets.QVBoxLayout(g2)
        self.ch_title = QtWidgets.QLabel("Pick a channel on the left.")
        self.ch_title.setWordWrap(True)
        f2.addWidget(self.ch_title)
        row = QtWidgets.QHBoxLayout()
        self.btn_edit = QtWidgets.QPushButton("Edit with keys")
        self.btn_edit.setToolTip("Turn the current motion into a few keys you can change")
        self.btn_add = QtWidgets.QPushButton("Add key")
        self.btn_add.setToolTip("Add a key at the current frame (or double-click the curve)")
        row.addWidget(self.btn_edit)
        row.addWidget(self.btn_add)
        f2.addLayout(row)
        row = QtWidgets.QHBoxLayout()
        self.btn_del = QtWidgets.QPushButton("Delete key")
        self.btn_del.setToolTip("Delete the selected key (Del)")
        self.btn_reset = QtWidgets.QPushButton("Original motion")
        self.btn_reset.setToolTip("Remove the keys: this channel moves as in the original stage again")
        row.addWidget(self.btn_del)
        row.addWidget(self.btn_reset)
        f2.addLayout(row)
        self.btn_edit.clicked.connect(self._edit_with_keys)
        self.btn_add.clicked.connect(self._add_key_here)
        self.btn_del.clicked.connect(self.curve.delete_selected)
        self.btn_reset.clicked.connect(self._reset_channel)
        sl.addWidget(g2)
        # key
        g3 = QtWidgets.QGroupBox("Selected key")
        f3 = QtWidgets.QFormLayout(g3)
        self.k_frame = QtWidgets.QSpinBox()
        self.k_frame.setRange(0, 8190)
        self.k_value = QtWidgets.QDoubleSpinBox()
        self.k_value.setDecimals(4)
        self.k_value.setRange(-4096.0, 4096.0)
        self.k_value.setSingleStep(0.05)
        self.k_interp = QtWidgets.QComboBox()
        for t in INTERPS:
            self.k_interp.addItem(INTERP_LABELS[t], t)
            self.k_interp.setItemData(self.k_interp.count() - 1, INTERP_HELP[t], Qt.ToolTipRole)
        self.k_help = QtWidgets.QLabel("")
        self.k_help.setWordWrap(True)
        self.k_help.setStyleSheet("color: gray;")
        hrow = QtWidgets.QHBoxLayout()
        self.k_handles = []
        for i in range(4):
            sb = QtWidgets.QDoubleSpinBox()
            sb.setDecimals(3)
            if i % 2 == 0:
                sb.setRange(0.0, 1.0)
            else:
                sb.setRange(-9.5, 9.5)
            sb.setSingleStep(0.05)
            sb.setToolTip(["Handle 1: time (0-1)", "Handle 1: progress", "Handle 2: time (0-1)",
                           "Handle 2: progress"][i])
            sb.editingFinished.connect(self._key_fields_changed)
            hrow.addWidget(sb)
            self.k_handles.append(sb)
        self.k_handles_box = QtWidgets.QWidget()
        self.k_handles_box.setLayout(hrow)
        hrow.setContentsMargins(0, 0, 0, 0)
        f3.addRow("Frame", self.k_frame)
        f3.addRow("Value", self.k_value)
        f3.addRow("Curve to next", self.k_interp)
        f3.addRow("Handles", self.k_handles_box)
        self.k_form = f3
        f3.addRow(self.k_help)
        self.k_frame.editingFinished.connect(self._key_fields_changed)
        self.k_value.editingFinished.connect(self._key_fields_changed)
        self.k_interp.activated.connect(lambda _i: self.curve.set_interp(self.k_interp.currentData()))
        sl.addWidget(g3)
        self.g_key = g3
        self.note = QtWidgets.QLabel("")
        self.note.setWordWrap(True)
        sl.addWidget(self.note)
        sl.addStretch(1)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidget(side)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setMinimumWidth(270)
        scroll.setMaximumWidth(350)
        split.addWidget(scroll)
        split.setSizes([260, 600, 280])
        self.set_stage(None, None)

    # ---- stage
    def set_stage(self, project, key: Optional[str]):
        if project is not self.project or key != self.key:
            self.project, self.key = project, key
            self.node = self.channel = None
            self._tree_key = None
        self.refresh()

    def _st(self):
        return self.project.stage(self.key) if self.project and self.key else None

    def refresh(self, refit: bool = False):
        st = self._st()
        parts = st.anim_parts() if st else []
        self._loading = True
        try:
            sig = (self.key, tuple(p["node"] for p in parts))
            if sig != self._tree_key:
                self._fill_tree(st, parts)
                self._tree_key = sig
                refit = True
            self._update_tree_marks(st, parts)
            self._update_length(st, parts)
        finally:
            self._loading = False
        self._update_channel(refit=refit)

    def _fill_tree(self, st, parts):
        self.tree.clear()
        self._items = {}
        first = None
        for n, part in enumerate(parts, 1):
            it = QtWidgets.QTreeWidgetItem([f"Part {n}: {part['name']}", ""])
            tip = [f"Model node {part['node']} ({part['name']})"]
            if part["group"]:
                tip.append(f"Its collision (group {part['group']}) follows it in-game.")
            else:
                tip.append("It has no collision group: only its drawing moves.")
            if part["objects"]:
                tip.append(f"{part['objects']} object(s) ride on it.")
            it.setToolTip(0, "\n".join(tip))
            it.setFlags(it.flags() & ~Qt.ItemIsSelectable)
            self.tree.addTopLevelItem(it)
            for ch in ("tx", "ty", "tz", "rx", "ry", "rz"):
                c = QtWidgets.QTreeWidgetItem([CHANNEL_NAMES[ch], ""])
                c.setData(0, Qt.UserRole, (part["node"], ch))
                it.addChild(c)
                self._items[(part["node"], ch)] = c
                if first is None and part["channels"][ch] == "moving":
                    first = c
            it.setExpanded(n == 1)
        if not parts:
            it = QtWidgets.QTreeWidgetItem(["This stage has no moving parts." if st else "No stage open.", ""])
            it.setFlags(Qt.NoItemFlags)
            self.tree.addTopLevelItem(it)
        if first is not None:
            self.tree.setCurrentItem(first)
            self.node, self.channel = first.data(0, Qt.UserRole)

    def _update_tree_marks(self, st, parts):
        for part in parts:
            for ch in ("tx", "ty", "tz", "rx", "ry", "rz"):
                it = self._items.get((part["node"], ch))
                if it is None:
                    continue
                keyed = bool(st.channel_keys(part["node"], ch))
                ok, why = st.channel_editable(part["node"], ch)
                status = "● keys" if keyed else KIND_TEXT.get(part["channels"][ch], "")
                it.setText(1, status)
                f = it.font(0)
                f.setBold(keyed)
                it.setFont(0, f)
                it.setFont(1, f)
                col = self.palette().text().color() if ok else QtGui.QColor(140, 140, 140)
                it.setForeground(0, col)
                it.setForeground(1, col)
                it.setToolTip(0, f"Locked: {why}" if why else f"{CHANNEL_NAMES[ch]}: {status}")
                it.setToolTip(1, f"Locked: {why}" if why else "")

    def _update_length(self, st, parts):
        frames = st.frames if st and parts else None
        on = bool(frames)
        ok, why = st.length_editable() if st else (False, "")
        for w in (self.len_spin, self.len_apply, self.len_reset):
            w.setEnabled(on and ok)
        if frames:
            self.len_spin.setValue(frames)
            self.len_secs.setText(f"= {(frames - 1) / 60:.2f} s per loop")
            orig = st.original_frames
            note = f"Original: {orig} frames ({(orig - 1) / 60:.2f} s). Changing it stretches every part and key."
            if not ok:
                note = why[0].upper() + why[1:] + "."
            size = len(st.anim_bytes or b"")
            from tredit.project import LARGEST_TESTED_ANIMATION
            note += f"\nAnimation data: {size / 1024:.1f} KB"
            if self.warn_size and size > LARGEST_TESTED_ANIMATION:
                note += (f" — larger than any animation tested in the game ({LARGEST_TESTED_ANIMATION / 1024:.1f} KB). "
                         "It may not fit in the game's memory; try it in an emulator first")
            self.len_note.setText(note)
        else:
            self.len_secs.setText("")
            self.len_note.setText("")

    def _tree_changed(self, cur, _prev):
        if self._loading or cur is None:
            return
        d = cur.data(0, Qt.UserRole)
        if not d:
            return
        self.node, self.channel = d
        if cur.parent() is not None:
            cur.parent().setExpanded(True)
        self.curve.sel = -1
        self._update_channel(refit=True)

    # ---- channel
    def _update_channel(self, refit: bool = False):
        st = self._st()
        node, ch = self.node, self.channel
        valid = bool(st and node is not None and ch and any(p["node"] == node for p in st.anim_parts()))
        if not valid:
            self.curve.set_data(1, [], [], None, False, False,
                                "Pick a channel on the left." if st and st.anim_parts() else "")
            self.ch_title.setText("Pick a channel on the left." if st and st.anim_parts() else
                                  "This stage has no moving parts." if st else "")
            for w in (self.btn_edit, self.btn_add, self.btn_del, self.btn_reset):
                w.setEnabled(False)
            self.g_key.setEnabled(False)
            self.note.setText("")
            return
        frames = st.frames
        orig = st.channel_values(node, ch, original=True)
        if len(orig) != frames and len(orig) > 1:          # original drawn stretched to the current length
            k = (len(orig) - 1) / (frames - 1)
            orig = [orig[min(len(orig) - 1, int(round(g * k)))] for g in range(frames)]
        cur = st.channel_values(node, ch)
        keys = st.channel_keys(node, ch)
        ok, why = st.channel_editable(node, ch)
        angle = ch[0] == "r"
        part_n = next(i for i, p in enumerate(st.anim_parts(), 1) if p["node"] == node)
        msg = ""
        if not ok:
            msg = f"Locked: {why}."
        elif not keys:
            msg = "Grey: the motion as it is. Press “Edit with keys” to change it."
        self.curve.set_data(frames, orig, cur, keys, ok, angle, msg, refit=refit)
        self.ch_title.setText(f"<b>Part {part_n} · {CHANNEL_NAMES[ch]}</b><br>"
                              + ("Keys (blue) replace the original motion (grey)." if keys else
                                 "Original motion." if ok else why[0].upper() + why[1:] + "."))
        self.btn_edit.setEnabled(ok and not keys)
        self.btn_add.setEnabled(ok and bool(keys))
        self.btn_del.setEnabled(ok and bool(keys) and len(keys) > 1)
        self.btn_reset.setEnabled(bool(keys))
        unit = "°" if angle else " u"
        self.k_value.setSuffix(unit)
        self.k_value.setSingleStep(0.5 if angle else 0.05)
        self.k_frame.setMaximum(max(0, frames - 1))
        jump = cur[-1] - cur[0] if cur else 0.0
        if angle:                                     # a full turn (360°) ends where it started
            jump = (jump + 180.0) % 360.0 - 180.0
        if cur and abs(jump) > (0.01 if angle else 1.0 / 512):
            self.note.setText("Note: this channel ends at a different value than it starts, so the part jumps "
                              "back when the animation repeats. Give the last frame the first key's value for a "
                              "smooth loop.")
        else:
            self.note.setText("")
        self._show_key()

    def _show_key(self):
        keys = self.curve.keys
        i = self.curve.sel
        on = bool(self.curve.editable and keys and 0 <= i < len(keys))
        self.g_key.setEnabled(on)
        self.btn_del.setEnabled(bool(self.curve.editable and keys and len(keys) > 1 and 0 <= i < len(keys)))
        if not on:
            self.k_help.setText("Click a key (◆) to select it." if keys else "")
            self._show_handles(False)
            return
        k = keys[i]
        self._loading = True
        try:
            self.k_frame.setValue(k["frame"])
            self.k_value.setValue(k["value"])
            self.k_interp.setCurrentIndex(INTERPS.index(k["interp"]))
            last = i == len(keys) - 1
            self.k_interp.setEnabled(not last)
            self.k_help.setText("The last key: the value holds until the end." if last else INTERP_HELP[k["interp"]])
            bez = k["interp"] == "bezier" and not last
            self._show_handles(bez)
            if bez:
                for sb, v in zip(self.k_handles, k.get("handles") or DEFAULT_HANDLES):
                    sb.setValue(v)
        finally:
            self._loading = False

    def _show_handles(self, on: bool):
        self.k_handles_box.setVisible(on)
        lab = self.k_form.labelForField(self.k_handles_box)
        if lab is not None:
            lab.setVisible(on)

    def _key_moving(self, i, f, v):
        self._loading = True
        try:
            self.k_frame.setValue(f)
            self.k_value.setValue(v)
        finally:
            self._loading = False

    def _key_fields_changed(self):
        if self._loading:
            return
        keys = self.curve.keys
        i = self.curve.sel
        if not (self.curve.editable and keys and 0 <= i < len(keys)):
            return
        new = [dict(k) for k in keys]
        new[i]["frame"] = self.k_frame.value()
        new[i]["value"] = round(self.k_value.value(), 6)
        if new[i]["interp"] == "bezier":
            new[i]["handles"] = [round(sb.value(), 3) for sb in self.k_handles]
        if any(k["frame"] == new[i]["frame"] for j, k in enumerate(new) if j != i):
            self._show_key()
            return
        new.sort(key=lambda k: k["frame"])
        if new != keys:
            self.curve.sel = next(j for j, k in enumerate(new) if k is not None and k["frame"] == self.k_frame.value())
            self._keys_edited(new)

    # ---- actions
    def _keys_edited(self, keys):
        if self.node is None or not self.channel:
            return
        try:
            keys = check_keys(keys, self._st().frames, self.channel)
        except KeyframeError:
            self.refresh()
            return
        self.keysCommitted.emit(self.node, self.channel, keys)

    def _edit_with_keys(self):
        if self.node is None or not self.channel:
            return
        keys = self.project.keys_from_original(self.key, self.node, self.channel)
        self.curve.sel = 0
        self.keysCommitted.emit(self.node, self.channel, keys)

    def _add_key_here(self):
        keys = self.curve.keys
        if not keys:
            return
        f = int(round(self.curve.frame))
        if any(k["frame"] == f for k in keys):
            self.curve.select(next(i for i, k in enumerate(keys) if k["frame"] == f))
            self._show_key()
            return
        prev = [k for k in keys if k["frame"] < f]
        interp = prev[-1]["interp"] if prev else keys[0]["interp"]
        nk = {"frame": f, "value": round(value_at(keys, f), 6), "interp": interp}
        if interp == "bezier":
            nk["handles"] = list(DEFAULT_HANDLES)
        new = sorted([dict(k) for k in keys] + [nk], key=lambda k: k["frame"])
        self.curve.sel = next(i for i, k in enumerate(new) if k["frame"] == f)
        self._keys_edited(new)

    def _reset_channel(self):
        if self.node is not None and self.channel:
            self.keysCommitted.emit(self.node, self.channel, None)

    def set_frame(self, f: float):
        self.curve.set_frame(f)
