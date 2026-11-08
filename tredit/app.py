"""Touch & Roll Stage Editor — desktop GUI (PySide6). All editing logic is in tredit.project."""
from __future__ import annotations

import math
import os
import re
import struct
import subprocess
import sys
from typing import Optional

from PySide6 import QtCore, QtGui, QtWidgets

from trcore.stagemodel import floor_below

from . import EDITOR_NAME, __version__
from .mapview import COLORS, PROJECTIONS, MapView
from .project import DEFAULT_HEIGHT, GEO_KINDS, EditorError, Project, find_original_rom, verify_saved_rom
from .animpanel import AnimationPanel
from .namesdialog import NamesDialog
from .stagedialogs import DrawingRangeDialog, NewStageDialog
from .scene3d import scene_from_state
from .view3d import View3D
from .widgets import TextureCombo, XYZRow, make_icons
from .settings import Prefs, SettingsDialog, backup_dir
from .startpage import StartPage
from trcore.romwrite import patch_record_path

Qt = QtCore.Qt
KIND_NAMES = {"start": "Start", "goal": "Goal", "bumper": "Bumper", "banana": "Banana"}
HERE = os.path.dirname(os.path.abspath(__file__))
UI_FONT = "Inter"            # bundled in tredit/fonts (SIL Open Font License, tredit/fonts/OFL.txt)
UI_FONT_SIZE = 9
MAX_RECENT = 8


def load_ui_font(app, size: int = UI_FONT_SIZE) -> bool:
    """Use the bundled Inter font for the whole interface; keep Qt's default if it cannot be loaded."""
    ok = UI_FONT in QtGui.QFontDatabase.families()
    if not ok:
        fonts = os.path.join(HERE, "fonts")
        for f in sorted(os.listdir(fonts)) if os.path.isdir(fonts) else []:
            if f.lower().endswith((".otf", ".ttf")):
                ok = QtGui.QFontDatabase.addApplicationFont(os.path.join(fonts, f)) >= 0 or ok
    if ok and UI_FONT in QtGui.QFontDatabase.families():
        app.setFont(QtGui.QFont(UI_FONT, int(size)))
        return True
    f = app.font()
    f.setPointSize(int(size))
    app.setFont(f)
    return False


def apply_theme(app, theme: str = "dark"):
    """Dark (the editor's own), light, or like Windows (follows the system's light / dark choice)."""
    app.setStyle("Fusion")
    if theme == "system":
        try:
            dark = app.styleHints().colorScheme() == Qt.ColorScheme.Dark
        except AttributeError:                       # Qt before 6.5
            dark = False
        theme = "dark" if dark else "light"
    if theme == "light":
        pal = app.style().standardPalette()
        pal.setColor(QtGui.QPalette.Highlight, QtGui.QColor(230, 140, 30))
        pal.setColor(QtGui.QPalette.HighlightedText, QtGui.QColor(20, 20, 20))
        app.setPalette(pal)
        return
    pal = QtGui.QPalette()
    pal.setColor(QtGui.QPalette.Window, QtGui.QColor(40, 42, 50))
    pal.setColor(QtGui.QPalette.WindowText, QtGui.QColor(225, 228, 235))
    pal.setColor(QtGui.QPalette.Base, QtGui.QColor(28, 30, 38))
    pal.setColor(QtGui.QPalette.AlternateBase, QtGui.QColor(40, 42, 50))
    pal.setColor(QtGui.QPalette.Text, QtGui.QColor(225, 228, 235))
    pal.setColor(QtGui.QPalette.Button, QtGui.QColor(50, 53, 62))
    pal.setColor(QtGui.QPalette.ButtonText, QtGui.QColor(225, 228, 235))
    pal.setColor(QtGui.QPalette.Highlight, QtGui.QColor(230, 140, 30))
    pal.setColor(QtGui.QPalette.HighlightedText, QtGui.QColor(20, 20, 20))
    pal.setColor(QtGui.QPalette.ToolTipBase, QtGui.QColor(50, 53, 62))
    pal.setColor(QtGui.QPalette.ToolTipText, QtGui.QColor(225, 228, 235))
    pal.setColor(QtGui.QPalette.Disabled, QtGui.QPalette.Text, QtGui.QColor(120, 124, 135))
    pal.setColor(QtGui.QPalette.Disabled, QtGui.QPalette.WindowText, QtGui.QColor(120, 124, 135))
    pal.setColor(QtGui.QPalette.Disabled, QtGui.QPalette.ButtonText, QtGui.QColor(120, 124, 135))
    app.setPalette(pal)


APP_USER_MODEL_ID = "TouchRollResearch.StageEditor"


def set_windows_app_id() -> bool:
    """Give the editor its own identity in the Windows taskbar. Windows groups taskbar buttons by
    AppUserModelID, and without one every Python program shares python.exe's, so the taskbar shows Python's
    icon instead of the editor's. Must run before the first window is created."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
        return True
    except (AttributeError, OSError):
        return False


def _cached_icon_path() -> str:
    base = QtCore.QStandardPaths.writableLocation(QtCore.QStandardPaths.AppDataLocation)
    return os.path.join(base, "icon.png") if base else ""


def _show_crash(exc_type, exc, tb):
    """Uncaught errors: shown in a window (the editor runs without a console) and written to a log."""
    import traceback
    text = "".join(traceback.format_exception(exc_type, exc, tb))
    log = ""
    base = QtCore.QStandardPaths.writableLocation(QtCore.QStandardPaths.AppDataLocation)
    if base:
        try:
            os.makedirs(base, exist_ok=True)
            log = os.path.join(base, "error.log")
            with open(log, "a", encoding="utf-8") as fh:
                fh.write(text + "\n")
        except OSError:
            log = ""
    if QtWidgets.QApplication.instance() is not None:
        box = QtWidgets.QMessageBox(QtWidgets.QMessageBox.Critical, EDITOR_NAME,
                                    "Something went wrong inside the editor. Your ROM was not changed."
                                    + (f"\n\nThe details were saved to {log}." if log else ""))
        box.setDetailedText(text)
        box.exec()
    else:
        sys.__excepthook__(exc_type, exc, tb)


def icon_from_rgba(w: int, h: int, rgba: bytes) -> QtGui.QIcon:
    """A crisp (pixel-doubled) icon in the usual sizes from small RGBA pixel art."""
    img = QtGui.QImage(rgba, w, h, 4 * w, QtGui.QImage.Format_RGBA8888).copy()
    icon = QtGui.QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256):
        icon.addPixmap(QtGui.QPixmap.fromImage(img.scaled(size, size, Qt.KeepAspectRatio, Qt.FastTransformation)))
    return icon


def editor_icon(rom_path: Optional[str] = None) -> Optional[QtGui.QIcon]:
    """The editor's icon: tredit/icon.png if it is there (kept out of the public repository), otherwise the
    menu icon of the user's own ROM (cyan background transparent), otherwise None."""
    for local in (os.path.join(HERE, "icon.png"), _cached_icon_path()):
        if local and os.path.exists(local):
            img = QtGui.QImage(local)
            if not img.isNull():
                img = img.convertToFormat(QtGui.QImage.Format_RGBA8888)
                return icon_from_rgba(img.width(), img.height(), bytes(img.constBits()))
    if rom_path and os.path.exists(rom_path):
        try:
            with open(rom_path, "rb") as fh:
                head = fh.read(0x200)
                bo = int.from_bytes(head[0x68:0x6C], "little")
                fh.seek(0)
                data = fh.read(max(0x200, bo + 0x240)) if bo else b""
            from trcore.rom import NdsRom
            ic = NdsRom.__new__(NdsRom)
            ic.data = data
            res = NdsRom.banner_icon(ic)
            if res:
                cache = _cached_icon_path()          # remembered, so the next start shows it right away
                if cache:
                    try:
                        os.makedirs(os.path.dirname(cache), exist_ok=True)
                        QtGui.QImage(res[2], res[0], res[1], 4 * res[0],
                                     QtGui.QImage.Format_RGBA8888).copy().save(cache)
                    except OSError:
                        pass
                return icon_from_rgba(*res)
        except (OSError, ValueError, IndexError, struct.error):
            return None
    return None

LEGEND_HTML = """<b>Map legend</b><br>
<span style='color:#3cdc5a'>&#9679;</span> start (arrow = facing)<br>
<span style='color:#46a0ff'>&#9644;</span> goal (arrow = front) &nbsp; <span style='color:#f03c3c'>&#9644;</span> red goal<br>
<span style='color:#e646e6'>&#9679;</span> bumper &nbsp; <span style='color:#ffdc28'>&#9679;</span> banana,
three dots = bunch (5)<br>
white ring = changed or new &nbsp; dashed outline = in a moving part<br>
Add: toolbar or Object menu, then click on the map &middot; Del deletes &middot; Ctrl+D duplicates<br>
<span style='color:#3ce65a'>&#9632;</span> platform &nbsp; <span style='color:#5adcff'>&#9633;</span> invisible
&nbsp; <span style='color:#faaa3c'>&#9633;</span> decoration &nbsp; <span style='color:#ff5050'>&#9638;</span>
no-collision area &middot; P / I / R then drag a rectangle &middot; C shows collision<br>
Top view: forward (the start's direction at angle 0) is up, x to the right.<br>
Drag to move &middot; arrows / PgUp / PgDn nudge &middot; Q / E turn (Shift = 1&deg;) &middot;
wheel zoom &middot; right-drag pan &middot; F fit"""


class PropertiesPanel(QtWidgets.QWidget):
    changed = QtCore.Signal(str, str, object)      # obj_id, property, value

    def __init__(self, parent=None):
        super().__init__(parent)
        self.obj = None
        self.warn_moving = True                     # Settings > Warnings
        lay = QtWidgets.QVBoxLayout(self)
        self.title = QtWidgets.QLabel("Nothing selected")
        f = self.title.font()
        f.setBold(True)
        f.setPointSize(f.pointSize() + 1)
        self.title.setFont(f)
        lay.addWidget(self.title)
        self.sub = QtWidgets.QLabel("")
        self.sub.setWordWrap(True)
        lay.addWidget(self.sub)
        form = QtWidgets.QFormLayout()
        self.pos = []
        for axis in "XYZ":
            sb = QtWidgets.QDoubleSpinBox()
            sb.setRange(-500000, 500000)
            sb.setDecimals(4)
            sb.setSingleStep(0.25)
            sb.setKeyboardTracking(False)
            sb.editingFinished.connect(self._pos_edited)
            self.pos.append(sb)
            form.addRow(f"{axis}", sb)
        self.heading = QtWidgets.QDoubleSpinBox()
        self.heading.setRange(-720, 720)
        self.heading.setDecimals(2)
        self.heading.setSingleStep(15)
        self.heading.setWrapping(False)
        self.heading.setSuffix("°")
        self.heading.setKeyboardTracking(False)
        self.heading.editingFinished.connect(self._heading_edited)
        form.addRow("Swivel (facing)", self.heading)

        def angle_spin():
            sb = QtWidgets.QDoubleSpinBox()
            sb.setRange(-180, 180)
            sb.setDecimals(2)
            sb.setSingleStep(5)
            sb.setSuffix("°")
            sb.setKeyboardTracking(False)
            sb.editingFinished.connect(self._rotation_edited)
            return sb
        self.tilt = angle_spin()
        self.tilt.setToolTip("Leans it forwards (towards its front) or, below 0, backwards")
        form.addRow("Tilt", self.tilt)
        self.roll = angle_spin()
        self.roll.setToolTip("Rolls it about its front: above 0 counter-clockwise as seen from the front")
        form.addRow("Rotate", self.roll)
        self.size = XYZRow(0.1, 10.0, 0.1, 3, tips=("Width (its own X)", "Height (its own Y)", "Depth (its own Z)"))
        self.size.edited.connect(lambda v: self._emit("size", list(v)))
        form.addRow("Size", self.size)
        self.red = QtWidgets.QCheckBox("Red goal")
        self.red.clicked.connect(lambda v: self._emit("red", bool(v)))
        form.addRow("", self.red)
        self.bunch = QtWidgets.QCheckBox("Bunch (worth 5)")
        self.bunch.clicked.connect(lambda v: self._emit("bunch", bool(v)))
        form.addRow("", self.bunch)
        lay.addLayout(form)
        self.note = QtWidgets.QLabel("")
        self.note.setWordWrap(True)
        self.note.setStyleSheet("color: #8a8f9c;")
        lay.addWidget(self.note)
        lay.addStretch(1)
        self.show_object(None)

    def _emit(self, prop, value):
        if self.obj:
            self.changed.emit(self.obj["id"], prop, value)

    def _pos_edited(self):
        if not self.obj:
            return
        new = [sb.value() for sb in self.pos]
        if any(abs(a - b) > 1e-9 for a, b in zip(new, self.obj["position"])):
            self._emit("position", new)

    def _heading_edited(self):
        if self.obj and self.obj.get("heading") is not None:
            v = self.heading.value() % 360.0
            if abs(v - self.obj["heading"]) > 1e-6:
                self._emit("heading", v)

    def _rotation_edited(self):
        o = self.obj
        if not o or "rotation" not in o.get("editable", ()):
            return
        new = (self.tilt.value(), self.heading.value() % 360.0, self.roll.value())
        old = (o.get("tilt") or 0.0, o.get("heading") or 0.0, o.get("rotate") or 0.0)
        if any(abs(a - b) > 1e-6 for a, b in zip(new, old)):
            self._emit("rotation", new)

    def show_object(self, obj: Optional[dict]):
        self.obj = obj
        widgets = self.pos + [self.heading, self.tilt, self.roll, self.red, self.bunch]
        self.size.set_values((1.0, 1.0, 1.0))
        self.size.setEnabled(False)
        for w in widgets:
            w.blockSignals(True)
        try:
            if obj is None:
                self.title.setText("Nothing selected")
                self.sub.setText("Click an object on the map.")
                self.note.setText("")
                for w in widgets:
                    w.setEnabled(False)
                for sb in self.pos:
                    sb.setValue(0)
                self.heading.setValue(0)
                self.tilt.setValue(0)
                self.roll.setValue(0)
                self.red.setChecked(False)
                self.bunch.setChecked(False)
                self.red.setVisible(False)
                self.bunch.setVisible(False)
                return
            k = obj["kind"]
            self.title.setText(obj.get("label") or f"{KIND_NAMES[k]} {obj['index'] + 1}")
            grp = obj.get("group_name") or f"group {obj['group']}"
            moving = " — may move with this part" if obj["in_moving_group"] else ""
            self.sub.setText(f"Group: {grp}{moving}\nRecord at {obj['offset']} in the stage file")
            for sb, v in zip(self.pos, obj["position"]):
                sb.setValue(v)
                sb.setEnabled("position" in obj["editable"])
            h = obj.get("heading")
            self.heading.setEnabled("heading" in obj["editable"])
            self.heading.setValue(h if h is not None else 0)
            turnable = "rotation" in obj["editable"]
            self.tilt.setEnabled(turnable)
            self.roll.setEnabled(turnable)
            self.tilt.setValue(obj.get("tilt") or 0.0)
            self.roll.setValue(obj.get("rotate") or 0.0)
            if k == "start":
                why = "The game stores only the start's facing, not a tilt or roll."
            elif k == "banana":
                why = "Bananas spin by themselves; the game stores no rotation for them."
            elif not turnable:
                why = "Tilt and Rotate unlock once edit test 9 passes in-game."
            else:
                why = ""
            for w, tip in ((self.tilt, "Leans it forwards (towards its front) or, below 0, backwards"),
                           (self.roll, "Rolls it about its front: above 0 counter-clockwise as seen from the front")):
                w.setToolTip(why or tip)
            self.size.set_values(obj.get("size") or (1.0, 1.0, 1.0))
            sizable = "size" in obj["editable"]
            self.size.setEnabled(sizable)
            if k == "start":
                self.size.set_tooltip("The game stores only the start's position and facing, so it has no size.")
            elif k == "banana":
                self.size.set_tooltip("Bananas store only a position and whether they are a bunch; they have no "
                                      "size.")
            elif not sizable:
                self.size.set_tooltip("Shown as stored. The game draws a goal or bumper at its size but ignores it "
                                      "for the finish line and the bounce, so sizes can't be changed.")
            else:
                self.size.set_tooltip("")
            self.red.setVisible(k == "goal")
            self.red.setChecked(bool(obj.get("red")))
            self.red.setEnabled("red" in obj["editable"])
            self.bunch.setVisible(k == "banana")
            self.bunch.setChecked(bool(obj.get("bunch")))
            self.bunch.setEnabled("bunch" in obj["editable"])
            notes = []
            self.heading.setToolTip("")
            if k == "bumper" and "heading" not in obj["editable"]:
                notes.append("Turning a bumper unlocks once edit test 9 passes in-game.")
                self.heading.setToolTip("locked until edit test 9 passes")
            if k == "banana":
                self.heading.setToolTip("Bananas spin by themselves; the game stores no rotation for them.")
            if k == "goal" and "heading" not in obj["editable"]:
                notes.append("This goal is tilted so far that its facing cannot be edited.")
            if k == "start":
                notes.append("Facing: 0° = towards -z (up on the top view); larger turns left.")
            if k == "goal":
                notes.append("Facing is the direction of the goal's front (the side you roll through).")
            if (k in ("goal", "bumper") and not obj.get("in_moving_group")
                    and tuple(obj.get("size") or (1.0, 1.0, 1.0)) != (1.0, 1.0, 1.0)):
                notes.append("Stored with a size: the game draws it at that size but ignores the size for its "
                             + ("finish line." if k == "goal" else "bounce."))
            if obj.get("new"):
                notes.insert(0, "New object (added in this editor). Del deletes it.")
            if obj["in_moving_group"] and k != "start" and self.warn_moving:
                notes.append("Moving part: moving this far or deleting it is not yet confirmed in-game.")
            notes.append("Values are shown as stored (1/4096 steps).")
            self.note.setText("\n".join(notes))
        finally:
            for w in widgets:
                w.blockSignals(False)


class GeometryPanel(QtWidgets.QWidget):
    changed = QtCore.Signal(str, str, object)      # geo id, property, value

    def __init__(self, parent=None):
        super().__init__(parent)
        self.g = None
        lay = QtWidgets.QVBoxLayout(self)
        self.title = QtWidgets.QLabel("")
        f = self.title.font()
        f.setBold(True)
        f.setPointSize(f.pointSize() + 1)
        self.title.setFont(f)
        lay.addWidget(self.title)
        self.sub = QtWidgets.QLabel("")
        self.sub.setWordWrap(True)
        lay.addWidget(self.sub)
        form = QtWidgets.QFormLayout()

        def spin(lo=-5000, hi=5000, step=0.25):
            sb = QtWidgets.QDoubleSpinBox()
            sb.setRange(lo, hi)
            sb.setDecimals(3)
            sb.setSingleStep(step)
            sb.setKeyboardTracking(False)
            return sb
        self.x0, self.x1, self.z0, self.z1 = spin(), spin(), spin(), spin()
        self.range_rows = []
        for a, b, axis in ((self.x0, self.x1, "x"), (self.z0, self.z1, "z")):
            row = QtWidgets.QHBoxLayout()
            self.range_rows.append(row)
            row.addWidget(a)
            row.addWidget(QtWidgets.QLabel("to"))
            row.addWidget(b)
            a.editingFinished.connect(lambda ax=axis: self._range_edited(ax))
            b.editingFinished.connect(lambda ax=axis: self._range_edited(ax))
            form.addRow(f"{axis.upper()} from", row)
        self.top = spin()
        self.top.editingFinished.connect(lambda: self._value_edited("top", self.top))
        form.addRow("Top height", self.top)
        self.thick = spin(0, 100)
        self.thick.editingFinished.connect(lambda: self._value_edited("thickness", self.thick))
        form.addRow("Thickness", self.thick)
        self.gsize = XYZRow(0, 5000, 0.25, 3, tips=("Width (X)", "Thickness (Y)", "Depth (Z)"), keep=True)
        self.gsize.keep.setChecked(False)
        self.gsize.edited.connect(lambda v: self._emit("size", list(v)))
        form.addRow("Size", self.gsize)
        self.texture = TextureCombo()
        self.texture.picked.connect(lambda shape: self._emit("texture", shape))
        form.addRow("Texture", self.texture)
        self.visible = QtWidgets.QCheckBox("Drawn (placeholder texture)")
        self.visible.clicked.connect(lambda v: self._emit("visible", bool(v)))
        form.addRow("", self.visible)
        self.solid = QtWidgets.QCheckBox("Solid (collision)")
        self.solid.clicked.connect(lambda v: self._emit("solid", bool(v)))
        form.addRow("", self.solid)
        self.limit_y = QtWidgets.QCheckBox("Only within a height range")
        self.limit_y.clicked.connect(self._limit_toggled)
        form.addRow("", self.limit_y)
        self.y0, self.y1 = spin(), spin()
        self.y_row = QtWidgets.QWidget()
        row = QtWidgets.QHBoxLayout(self.y_row)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.y0)
        row.addWidget(QtWidgets.QLabel("to"))
        row.addWidget(self.y1)
        self.y0.editingFinished.connect(lambda: self._range_edited("y"))
        self.y1.editingFinished.connect(lambda: self._range_edited("y"))
        self.y_label = QtWidgets.QLabel("Y from")
        form.addRow(self.y_label, self.y_row)
        # imported models: position and scale
        self.mpos = [spin() for _ in range(3)]
        self.mpos_row = QtWidgets.QWidget()
        row = QtWidgets.QHBoxLayout(self.mpos_row)
        row.setContentsMargins(0, 0, 0, 0)
        for sb in self.mpos:
            row.addWidget(sb)
            sb.editingFinished.connect(self._mpos_edited)
        form.addRow("Position X Y Z", self.mpos_row)
        self.scale = XYZRow(0.001, 1000, 0.1, 4, tips=("Scale along its own X", "Scale along its own Y (up)",
                                                       "Scale along its own Z"))
        self.scale.keep.setChecked(True)
        self.scale.edited.connect(self._scale_edited)
        form.addRow("Scale", self.scale)
        self.mat_box = QtWidgets.QWidget()
        self.mat_form = QtWidgets.QFormLayout(self.mat_box)
        self.mat_form.setContentsMargins(0, 0, 0, 0)
        self.mat_combos: list = []
        form.addRow("Textures", self.mat_box)
        self.use_uv = QtWidgets.QCheckBox("Use the model's own texture coordinates (UVs)")
        self.use_uv.setToolTip("Lays the texture on the model the way your 3D program's UV map does. Off: the "
                               "texture is laid on from above, like on platforms.")
        self.use_uv.clicked.connect(lambda v: self._emit("uv_mode", "uv" if v else "projected"))
        form.addRow("", self.use_uv)
        self.both = QtWidgets.QCheckBox("Both sides (adds the back of every face)")
        self.both.clicked.connect(lambda v: self._emit("both_sides", bool(v)))
        form.addRow("", self.both)
        # turning (platforms and models)
        self.rot = []
        self.rot_row = QtWidgets.QWidget()
        row = QtWidgets.QHBoxLayout(self.rot_row)
        row.setContentsMargins(0, 0, 0, 0)
        for tip in ("Tilt: about its own x axis", "Swivel: about the vertical axis (above 0 turns left)",
                    "Rotate: about its own z axis"):
            sb = spin(-180, 180, 5)
            sb.setDecimals(2)
            sb.setSuffix("°")
            sb.setToolTip(tip)
            sb.editingFinished.connect(self._rot_edited)
            row.addWidget(sb)
            self.rot.append(sb)
        form.addRow("Tilt / Swivel / Rotate", self.rot_row)
        lay.addLayout(form)
        self.form = form
        self.note = QtWidgets.QLabel("")
        self.note.setWordWrap(True)
        self.note.setStyleSheet("color: #8a8f9c;")
        lay.addWidget(self.note)
        lay.addStretch(1)
        self._platform_widgets = [self.top, self.thick, self.visible, self.solid]
        self._remove_widgets = [self.limit_y, self.y0, self.y1]
        self.tex_choices: list = []
        self.tex_icons: dict = {}
        self.tex_unlocked = False

    def set_textures(self, choices: list, icons: dict, unlocked: bool):
        """The stage's textures for the Texture pickers (StageState.texture_choices)."""
        self.tex_choices, self.tex_icons, self.tex_unlocked = list(choices), dict(icons), bool(unlocked)
        self.texture.set_choices(self.tex_choices, self.tex_icons)
        self._mat_names = None

    def _emit(self, prop, value):
        if self.g:
            self.changed.emit(self.g["id"], prop, value)

    def _scale_edited(self, vals):
        v = [float(x) for x in vals]
        self._emit("scale", v[0] if v[0] == v[1] == v[2] else v)

    def _fill_materials(self, g: dict):
        names = list(g.get("materials") or ["(whole model)"])
        if getattr(self, "_mat_names", None) != names:
            while self.mat_form.rowCount():
                self.mat_form.removeRow(0)
            self.mat_combos = []
            for k, nm in enumerate(names):
                cb = TextureCombo()
                cb.picked.connect(lambda shape, slot=k: self._emit("texture", [slot, shape]))
                lab = QtWidgets.QLabel(nm)
                lab.setTextFormat(Qt.PlainText)
                self.mat_form.addRow(lab, cb)
                self.mat_combos.append(cb)
            self._mat_names = names
        tex = g.get("textures") or []
        for k, cb in enumerate(self.mat_combos):
            cb.set_choices(self.tex_choices, self.tex_icons)
            cb.set_current(tex[k] if k < len(tex) else None)
            cb.setEnabled(self.tex_unlocked and bool(self.tex_choices))
            cb.setToolTip("" if self.tex_unlocked else "The stage's other textures unlock once edit test 10 passes "
                                                        "in-game. Until then models use the main texture.")

    def _mpos_edited(self):
        if self.g and self.g["type"] == "mesh":
            new = [sb.value() for sb in self.mpos]
            if any(abs(a - b) > 1e-9 for a, b in zip(new, self.g["pos"])):
                self._emit("pos", new)

    def _rot_edited(self):
        if not self.g or self.g["type"] == "remove":
            return
        new = [sb.value() for sb in self.rot]
        old = list(self.g.get("rot") or (0.0, 0.0, 0.0))
        if any(abs(a - b) > 1e-6 for a, b in zip(new, old)):
            self._emit("rot", new)

    def _range_edited(self, axis):
        if not self.g:
            return
        a, b = {"x": (self.x0, self.x1), "z": (self.z0, self.z1), "y": (self.y0, self.y1)}[axis]
        new = [a.value(), b.value()]
        old = self.g.get(axis)
        if old is None or any(abs(u - v) > 1e-9 for u, v in zip(new, old)):
            self._emit(axis, new)

    def _value_edited(self, prop, sb):
        if self.g and abs(sb.value() - self.g.get(prop, 0.0)) > 1e-9:
            self._emit(prop, sb.value())

    def _limit_toggled(self, on):
        if not self.g:
            return
        if on:
            self._emit("y", [-1.0, 2.0])
        else:
            self._emit("y", None)

    def show_geometry(self, g: Optional[dict]):
        self.g = g
        ws = [self.x0, self.x1, self.z0, self.z1, self.top, self.thick, self.visible, self.solid, self.limit_y,
              self.y0, self.y1, self.both, self.use_uv] + self.mpos + self.rot
        for w in ws:
            w.blockSignals(True)
        try:
            if g is None:
                return
            self.title.setText(g["label"])
            plat = g["type"] == "platform"
            mesh = g["type"] == "mesh"
            self.x0.setValue(g["x"][0])
            self.x1.setValue(g["x"][1])
            self.z0.setValue(g["z"][0])
            self.z1.setValue(g["z"][1])
            for row in self.range_rows:
                self.form.setRowVisible(row, not mesh)
            for w in self._platform_widgets:
                self.form.setRowVisible(w, plat or (mesh and w in (self.visible, self.solid)))
            for w in (self.limit_y, self.y_row):
                self.form.setRowVisible(w, not plat and not mesh)
            for w in (self.mpos_row, self.scale, self.both, self.mat_box, self.use_uv):
                self.form.setRowVisible(w, mesh)
            self.form.setRowVisible(self.gsize, not mesh)
            self.form.setRowVisible(self.texture, plat)
            if not mesh:
                y = g.get("y")
                th = g["thickness"] if plat else ((y[1] - y[0]) if y else 0.0)
                self.gsize.set_values((g["x"][1] - g["x"][0], th, g["z"][1] - g["z"][0]))
                self.gsize.boxes[1].setEnabled(plat or bool(y))
            self.form.setRowVisible(self.rot_row, plat or mesh)
            unlocked = bool(g.get("rotation_unlocked", True))
            for sb, v in zip(self.rot, g.get("rot") or (0.0, 0.0, 0.0)):
                sb.setValue(v)
                sb.setEnabled(unlocked)
                if not unlocked:
                    sb.setToolTip("Turning platforms unlocks once edit test 9 passes in-game.")
            for w in self.mpos + [self.scale, self.both, self.visible, self.solid]:
                w.setEnabled(unlocked or not mesh)
            if plat:
                self.texture.set_current(g.get("texture"))
                self.texture.setEnabled(self.tex_unlocked and bool(self.tex_choices) and g["visible"])
                self.texture.setToolTip("" if self.tex_unlocked else "The stage's other textures unlock once edit "
                                        "test 10 passes in-game. Until then platforms use the main texture.")
            if mesh:
                for sb, v in zip(self.mpos, g["pos"]):
                    sb.setValue(v)
                self.scale.set_values(g.get("size") or (1.0, 1.0, 1.0))
                self._fill_materials(g)
                has_uv = bool(g.get("uvs"))
                self.use_uv.setChecked(g.get("uv_mode") == "uv")
                self.use_uv.setEnabled(has_uv and self.tex_unlocked)
                self.use_uv.setText("Use the model's own texture coordinates (UVs)" if has_uv else
                                    "Use the model's own texture coordinates (the file has none)")
                self.both.setChecked(bool(g.get("both_sides")))
                self.visible.setChecked(g["visible"])
                self.solid.setChecked(g["solid"])
                src = g.get("source")
                what = f"Object “{g.get('name')}” from {src}.obj" if src and src != g.get("name") else \
                    f"Imported from {g.get('name')}.obj"
                self.sub.setText(f"{what}: {g.get('triangles', len(g['faces']))} triangles."
                                 + ("" if g["visible"] else "\nNot drawn (collision only)."))
                notes = ["Position is where the model's bottom middle goes.",
                         "Each face is drawn and solid from the side its corners go round counter-clockwise; "
                         "tick Both sides if parts of the model are missing or you fall through them."]
            elif plat:
                self.top.setValue(g["top"])
                self.thick.setValue(g["thickness"])
                self.visible.setChecked(g["visible"])
                self.solid.setChecked(g["solid"])
                self.sub.setText("Added geometry in the stage's fixed part."
                                 + ("" if g["visible"] else "\nNot drawn (collision only)."))
                notes = ["Thickness 0 makes a flat panel (seen from above only).",
                         "Solid: the ball collides with it. Drawn: it appears in-game (placeholder texture)."]
            else:
                y = g.get("y")
                self.limit_y.setChecked(y is not None)
                self.y0.setEnabled(y is not None)
                self.y1.setEnabled(y is not None)
                if y:
                    self.y0.setValue(y[0])
                    self.y1.setValue(y[1])
                self.sub.setText("Removes the stage's collision inside this area (what is drawn stays).")
                notes = ["Faces and edges crossing the area's sides are cut there; the rest stays solid.",
                         "Applies in the order the items were added: geometry added later is not removed."]
            notes.append("Drag on the top view to move it; on the side view to change its height.")
            self.note.setText("\n".join(notes))
        finally:
            for w in ws:
                w.blockSignals(False)


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, rom_path: Optional[str] = None, settings: Optional[QtCore.QSettings] = None):
        super().__init__()
        self.settings = settings or QtCore.QSettings("TouchRollResearch", "tredit")
        self.prefs = Prefs(self.settings, self)
        if not self.prefs.get("bizhawk") and self.settings.value("bizhawk", ""):
            self.prefs.set("bizhawk", str(self.settings.value("bizhawk", "")))      # from editor 0.9 and older
        self._last_texture = {}                    # stage key -> texture shape picked last
        self._backup_count = None
        self.project: Optional[Project] = None
        self.stage_key: Optional[str] = None
        self.selected: Optional[str] = None
        self.objects = {}
        self.resize(1400, 900)

        self.top = MapView("top", self.describe)
        self.side = MapView("right", self.describe)
        self.geometry = {}
        self.show_collision = True
        self.pending_geo = None
        for v in (self.top, self.side):
            v.on_select = self.select
            v.on_move = self.move_object
            v.floor_height = self.floor_height
            v.describe_geo = self.describe_geo
            v.on_geo_move = self.move_geometry
        self.top.on_rect = self.geo_rect_drawn
        self.top.on_hover = self.hover_top
        self.top.on_place = self.place_at
        self.pending_add = None
        self.side_combo = QtWidgets.QComboBox()
        self.side_combo.addItem("Automatic (along the longer side)", "auto")
        for key in ("right", "behind"):
            self.side_combo.addItem(PROJECTIONS[key][3], key)
        self.side_combo.currentIndexChanged.connect(self.change_side_projection)
        side_box = QtWidgets.QWidget()
        sl = QtWidgets.QVBoxLayout(side_box)
        sl.setContentsMargins(0, 0, 0, 0)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("Side view:"))
        row.addWidget(self.side_combo)
        row.addStretch(1)
        sl.addLayout(row)
        sl.addWidget(self.side)
        split = QtWidgets.QSplitter(Qt.Vertical)
        split.addWidget(self.top)
        split.addWidget(side_box)
        split.setSizes([640, 240])
        self.view3d = View3D()
        self.view3d.set_callbacks(self.select, self.move_object, self.move_geometry)
        self._scene_src = None             # (stage key, model bytes, collision faces) the 3D scene was built from
        self.tabs = QtWidgets.QTabWidget()
        self.tabs.addTab(split, "Top and side")
        self.tabs.addTab(self.view3d, "3D view")
        self.tabs.currentChanged.connect(lambda _i: self._sync_3d())
        self.start_page = StartPage(EDITOR_NAME, __version__)
        self.start_page.open_clicked.connect(self.choose_rom)
        self.start_page.recent_clicked.connect(self._open_recent)
        self.start_page.guide_clicked.connect(self.open_guide)
        self.start_page.show_toggled.connect(lambda on: self.prefs.set("show_start_page", bool(on)))
        self.central = QtWidgets.QStackedWidget()
        self.central.addWidget(self.start_page)
        self.central.addWidget(self.tabs)
        self.setCentralWidget(self.central)

        self.tree = QtWidgets.QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.itemActivated.connect(self.tree_activated)
        self.tree.itemClicked.connect(self.tree_activated)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._tree_menu)
        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText("Find a stage (name, 1-3, smb1_be06)…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.filter_tree)
        stages_box = QtWidgets.QWidget()
        sbl = QtWidgets.QVBoxLayout(stages_box)
        sbl.setContentsMargins(0, 0, 0, 0)
        sbl.addWidget(self.search)
        sbl.addWidget(self.tree, 1)
        dock = QtWidgets.QDockWidget("Stages", self)
        self.stages_dock = dock
        dock.setObjectName("stages")
        dock.setWidget(stages_box)
        self.addDockWidget(Qt.LeftDockWidgetArea, dock)

        self.props = PropertiesPanel()
        self.props.changed.connect(self.property_changed)
        self.geo_panel = GeometryPanel()
        self.geo_panel.changed.connect(self.property_changed)
        self.sel_stack = QtWidgets.QStackedWidget()
        self.sel_stack.addWidget(self.props)
        self.sel_stack.addWidget(self.geo_panel)
        self.info = QtWidgets.QPlainTextEdit()
        self.info.setReadOnly(True)
        self.info.setStyleSheet("font-family: Consolas, 'DejaVu Sans Mono', monospace; font-size: 9pt;")
        legend = QtWidgets.QLabel(LEGEND_HTML)
        legend.setWordWrap(True)
        legend.setTextFormat(Qt.RichText)
        right = QtWidgets.QTabWidget()
        right.addTab(self.sel_stack, "Selected")
        right.addTab(self.info, "Stage info (read-only)")
        rbox = QtWidgets.QWidget()
        rl = QtWidgets.QVBoxLayout(rbox)
        rl.addWidget(right, 1)
        rl.addWidget(legend)
        dock2 = QtWidgets.QDockWidget("Properties", self)
        self.props_dock = dock2
        dock2.setObjectName("properties")
        dock2.setWidget(rbox)
        self.addDockWidget(Qt.RightDockWidgetArea, dock2)

        self.anim_panel = AnimationPanel()
        self.anim_panel.keysCommitted.connect(self.apply_keys)
        self.anim_panel.lengthCommitted.connect(self.apply_length)
        self.anim_panel.frameScrubbed.connect(self._panel_frame)
        self.anim_dock = QtWidgets.QDockWidget("Animation keyframes", self)
        self.anim_dock.setObjectName("animation")
        self.anim_dock.setWidget(self.anim_panel)
        self.addDockWidget(Qt.BottomDockWidgetArea, self.anim_dock)
        self.anim_dock.hide()
        self.anim_dock.visibilityChanged.connect(self._anim_dock_shown)
        self.view3d.btn_keys.clicked.connect(self.show_keyframes)
        self.view3d.on_frame = self.anim_panel.set_frame

        self.status_edits = QtWidgets.QLabel("")
        self.statusBar().addPermanentWidget(self.status_edits)
        self._build_actions()
        self.backup_timer = QtCore.QTimer(self)
        self.backup_timer.timeout.connect(self.backup_now)
        self.prefs.changed.connect(self.apply_setting)
        self.apply_all_settings()
        self.update_title()
        if rom_path:
            QtCore.QTimer.singleShot(0, lambda: self.open_rom(rom_path))
        else:
            self.statusBar().showMessage("File > Open ROM… to start (your original ROM is never modified).")
        self._show_central()

    # ------------------------------------------------------------------ actions
    def _act(self, menu, text, slot, shortcut=None, checkable=False, checked=False):
        a = QtGui.QAction(text, self)
        if shortcut:
            a.setShortcut(QtGui.QKeySequence(shortcut))
        a.setCheckable(checkable)
        if checkable:
            a.setChecked(checked)
            a.toggled.connect(slot)
        else:
            a.triggered.connect(slot)
        menu.addAction(a)
        return a

    def _build_actions(self):
        mb = self.menuBar()
        f = mb.addMenu("&File")
        self.a_open = self._act(f, "&Open ROM…", self.choose_rom, QtGui.QKeySequence.Open)
        self.recent_menu = f.addMenu("Open &recent")
        self.recent_menu.aboutToShow.connect(self._fill_recent)
        f.addSeparator()
        self.a_save = self._act(f, "&Save", self.save, QtGui.QKeySequence.Save)
        self.a_save_as = self._act(f, "Save as &new copy…", self.save_as, "Ctrl+Shift+S")
        f.addSeparator()
        self.a_import = self._act(f, "&Import edits (plan or .patch.json)…", self.import_edits)
        self.a_export = self._act(f, "&Export edits as plan (.json)…", self.export_edits)
        f.addSeparator()
        self.a_play = self._act(f, "&Play saved copy in BizHawk", self.play, "F5")
        self.a_bizhawk = self._act(f, "Set BizHawk (EmuHawk.exe) location…", self.choose_bizhawk)
        f.addSeparator()
        self.a_quit = self._act(f, "&Quit", self.close, QtGui.QKeySequence.Quit)
        e = mb.addMenu("&Edit")
        self.a_undo = self._act(e, "&Undo", self.undo, QtGui.QKeySequence.Undo)
        self.a_redo = self._act(e, "&Redo", self.redo, "Ctrl+Y")
        self.a_redo.setShortcuts([QtGui.QKeySequence("Ctrl+Y"), QtGui.QKeySequence("Ctrl+Shift+Z")])
        e.addSeparator()
        self.a_delete = self._act(e, "&Delete selected object", self.delete_selected, QtGui.QKeySequence.Delete)
        self.a_dup = self._act(e, "D&uplicate selected object", self.duplicate_selected, "Ctrl+D")
        e.addSeparator()
        self.a_turn = [self._act(e, "Turn left", lambda: self.turn(self.prefs.get("turn_step")), "Q"),
                       self._act(e, "Turn right", lambda: self.turn(-self.prefs.get("turn_step")), "E"),
                       self._act(e, "Turn left (fine)", lambda: self.turn(self.prefs.get("turn_fine")), "Shift+Q"),
                       self._act(e, "Turn right (fine)", lambda: self.turn(-self.prefs.get("turn_fine")), "Shift+E")]
        self.a_reset = self._act(e, "Reset selected object to original", self.reset_selected)
        e.addSeparator()
        self.a_names = self._act(e, "World and stage &names…", self.edit_names, "Ctrl+Shift+N")
        e.addSeparator()
        self.a_settings = self._act(e, "&Settings…", self.open_settings, "Ctrl+,")
        self.a_settings.setToolTip("Everything the editor lets you set: look, editing, defaults, 3D view, saving, "
                                   "warnings")
        o = mb.addMenu("&Object")
        tb = self.addToolBar("Objects")
        self.objects_toolbar = tb
        tb.setObjectName("objects_toolbar")
        tb.setMovable(False)
        self.add_actions = {}
        for kind, label, key in (("banana", "Add &banana", "1"), ("bunch", "Add banana b&unch", "2"),
                                 ("bumper", "Add bum&per", "3"), ("goal", "Add &goal", "4")):
            a = self._act(o, label, lambda _c=False, k=kind: self.begin_add(k), key)
            a.setToolTip(f"{label.replace('&', '')} ({key}), then click on the top view")
            tb.addAction(a)
            self.add_actions[kind] = a
        o.addSeparator()
        tb.addSeparator()
        o.addAction(self.a_dup)
        o.addAction(self.a_delete)
        g = mb.addMenu("&Geometry")
        self.geo_actions = {}
        for kind, label, key in (("platform", "Add &platform", "P"), ("invisible", "Add &invisible floor", "I"),
                                 ("decoration", "Add &decoration (drawn, not solid)", None),
                                 ("remove", "&Remove collision in an area", "R")):
            a = self._act(g, label, lambda _c=False, k=kind: self.begin_geo(k), key)
            a.setToolTip(f"{label.replace('&', '')}{f' ({key})' if key else ''}: drag a rectangle on the top view")
            if kind != "decoration":
                tb.addAction(a)
            self.geo_actions[kind] = a
        g.addSeparator()
        self.a_import_obj = self._act(g, "Import &OBJ model… (Recommended)", self.import_model)
        self.a_import_obj.setToolTip("Build the shape in a 3D program and bring it in as an .obj file: the "
                                     "recommended way to make custom geometry (new stages only)")
        g.addSeparator()
        g.addAction(self.a_dup)
        g.addAction(self.a_delete)
        tb.addSeparator()
        tb.addAction(self.a_dup)
        tb.addAction(self.a_delete)
        esc = QtGui.QShortcut(QtGui.QKeySequence(Qt.Key_Escape), self)
        esc.activated.connect(self.cancel_add)
        sm = mb.addMenu("&Stage")
        self.a_new_stage = self._act(sm, "Create &new stage here…", self.create_new_stage)
        self.a_new_stage.setToolTip("Replace this stage with an empty one: one platform and the start")
        self.a_edit_new = self._act(sm, "&Edit the new stage's platform…", lambda: self.create_new_stage(edit=True))
        self.a_restore = self._act(sm, "&Restore original stage…", self.restore_original)
        sm.addSeparator()
        self.a_range = self._act(sm, "&Drawing range…", self.drawing_range)
        sm.addSeparator()
        self.a_keys = self.anim_dock.toggleViewAction()
        self.a_keys.setText("&Animation keyframes")
        self.a_keys.setShortcut(QtGui.QKeySequence("Ctrl+K"))
        self.a_keys.setToolTip("Edit how the stage's moving parts move")
        sm.addAction(self.a_keys)
        sm.addSeparator()
        sm.addAction(self.a_names)
        v = mb.addMenu("&View")
        self.a_fit = self._act(v, "Zoom to &fit", self.fit_views, "F")
        self.a_view_map = self._act(v, "&Top and side views", lambda: self.tabs.setCurrentIndex(0), "Ctrl+1")
        self.a_view_3d = self._act(v, "&3D view", lambda: self.tabs.setCurrentIndex(1), "Ctrl+2")
        v.addAction(self.a_keys)
        self.a_grid = self._act(v, "Show &grid", self.toggle_grid, checkable=True, checked=True)
        self.a_show_coll = self._act(v, "Show &collision", self.toggle_collision, "C", checkable=True, checked=True)
        self.a_follow = self._act(v, "Keep height above the floor while dragging", self.toggle_follow, checkable=True,
                                  checked=True)
        snap = v.addMenu("&Snap when dragging")
        grp = QtGui.QActionGroup(self)
        self.snap_actions = {}
        for label, val in (("Off (1/4096)", 0.0), ("0.125", 0.125), ("0.25", 0.25), ("0.5", 0.5), ("1", 1.0)):
            a = QtGui.QAction(label, self, checkable=True)
            a.setChecked(val == 0.25)
            a.triggered.connect(lambda _c=False, x=val: self.set_snap(x))
            grp.addAction(a)
            snap.addAction(a)
            self.snap_actions[val] = a
        self.set_snap(self.prefs.get("snap"))
        h = mb.addMenu("&Help")
        self.a_guide = self._act(h, "&Guide", self.open_guide, "F1")
        self.a_packages = self._act(h, "&Packages…", self.open_packages)
        self.a_packages.setToolTip("The Python packages the editor uses; installs missing ones")
        self.a_about = self._act(h, "&About", self.about)
        for key, dx, dz, dy in ((Qt.Key_Up, 0, -1, 0), (Qt.Key_Down, 0, 1, 0), (Qt.Key_Left, -1, 0, 0),
                                (Qt.Key_Right, 1, 0, 0), (Qt.Key_PageUp, 0, 0, 1), (Qt.Key_PageDown, 0, 0, -1)):
            sc = QtGui.QShortcut(QtGui.QKeySequence(key), self)
            sc.activated.connect(lambda dx=dx, dz=dz, dy=dy: self.nudge(dx, dy, dz))
        self.update_actions()

    def update_actions(self):
        p = self.project
        has = bool(p and p.has_edits())
        self.a_save.setEnabled(has)
        self.a_save_as.setEnabled(has)
        self.a_export.setEnabled(has)
        self.a_import.setEnabled(p is not None)
        self.a_undo.setEnabled(bool(p and p.undo_stack))
        self.a_redo.setEnabled(bool(p and p.redo_stack))
        self.a_undo.setText(f"&Undo {p.undo_stack[-1]['label']}" if p and p.undo_stack else "&Undo")
        self.a_redo.setText(f"&Redo {p.redo_stack[-1]['label']}" if p and p.redo_stack else "&Redo")
        self.a_play.setEnabled(bool(p and p.output_path))
        n = p.edit_count if p else 0
        nn = len(p.names["stages"]) + len(p.names["worlds"]) if p else 0
        txt = f"{n - nn} change(s) in {len(p.changed_stages())} stage(s)" if p and (n - nn or not nn) else ""
        if nn:
            txt += (", " if txt else "") + f"{nn} name(s) changed"
        if hasattr(self, "a_names"):
            self.a_names.setEnabled(p is not None)
        if hasattr(self, "a_new_stage"):
            st = p.stage(self.stage_key) if p and self.stage_key else None
            in_world = bool(st and st.entry.get("world"))
            self.a_new_stage.setEnabled(in_world and not st.is_new_stage)
            self.a_edit_new.setEnabled(bool(st and st.is_new_stage))
            self.a_restore.setEnabled(bool(st and st.is_new_stage))
            self.a_range.setEnabled(bool(st and st.original_pos_scale))
            can_import = bool(st and st.is_new_stage and p.rotation_unlocked and not st.can_restructure())
            self.a_import_obj.setEnabled(can_import)
            if not (p and p.rotation_unlocked):
                self.a_import_obj.setStatusTip("Importing models unlocks once edit test 9 passes in-game.")
            elif not (st and st.is_new_stage):
                self.a_import_obj.setStatusTip("Models can be imported into new stages: Stage > Create new stage "
                                               "here… first.")
            else:
                self.a_import_obj.setStatusTip("")
        if p and self.stage_key:
            txt = f"Banana value {p.stage(self.stage_key).banana_value} / 255   ·   " + txt
        self.status_edits.setText(txt)
        sel = (self.objects.get(self.selected) or self.geometry.get(self.selected)) if p else None
        can_struct = bool(p and self.stage_key and not p.stage(self.stage_key).can_restructure())
        for a in list(self.add_actions.values()) + list(self.geo_actions.values()):
            a.setEnabled(can_struct)
        self.a_delete.setEnabled(bool(sel and sel.get("deletable")))
        self.a_dup.setEnabled(bool(sel and sel["kind"] != "start" and can_struct))

    def update_title(self):
        t = EDITOR_NAME
        if self.project:
            t += f" — {os.path.basename(self.project.rom_path)}"
            if self.project.output_path:
                t += f" → {os.path.basename(self.project.output_path)}"
            if self.project.dirty and self.project.has_edits():
                t += " *"
        self.setWindowTitle(t)

    # ------------------------------------------------------------------ settings
    def open_settings(self):
        SettingsDialog(self.prefs, self, open_backups=self.open_backup_folder).exec()

    def open_backup_folder(self):
        d = backup_dir()
        os.makedirs(d, exist_ok=True)
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(d))

    def apply_all_settings(self):
        for key in ("snap", "follow_floor", "show_grid", "show_collision", "side_view", "marker_scale", "coll_colour",
                    "fov", "orbit_speed", "invert_orbit", "background", "turn_step", "click_size", "warn_moving",
                    "warn_anim_size", "backup_minutes", "show_start_page"):
            self.apply_setting(key)

    def apply_setting(self, key: str):
        """Make one setting take effect now (called whenever it changes)."""
        from . import mapview, view3d
        v = self.prefs.get(key)
        app = QtWidgets.QApplication.instance()
        if key == "theme":
            apply_theme(app, v)
        elif key == "font_size":
            load_ui_font(app, v)
        elif key == "snap":
            self.set_snap(v)
        elif key == "follow_floor":
            self.a_follow.setChecked(bool(v))
            self.top.follow_floor = bool(v)
        elif key == "show_grid":
            self.a_grid.setChecked(bool(v))
        elif key == "show_collision":
            self.a_show_coll.setChecked(bool(v))
        elif key == "side_view":
            i = self.side_combo.findData(v)
            if i >= 0 and i != self.side_combo.currentIndex():
                self.side_combo.setCurrentIndex(i)
        elif key in ("marker_scale", "coll_colour", "coll_moving_colour"):
            mapview.APPEARANCE["marker_scale"] = float(self.prefs.get("marker_scale"))
            mapview.APPEARANCE["coll_fixed"] = self.prefs.colour("coll_colour")
            mapview.APPEARANCE["coll_moving"] = self.prefs.colour("coll_moving_colour")
            for view in (self.top, self.side):
                for m in view.markers.values():
                    m.refresh_size()
                if view.collision_item is not None:
                    view.collision_item.update()
                view.viewport().update()
        elif key in ("fov", "orbit_speed", "invert_orbit", "background", "smooth_textures"):
            c = self.prefs.colour("background")
            view3d.VIEW.update({"fov": float(self.prefs.get("fov")), "orbit_speed": float(self.prefs.get("orbit_speed")),
                                "invert_orbit": bool(self.prefs.get("invert_orbit")),
                                "background": (c.redF(), c.greenF(), c.blueF()),
                                "smooth": bool(self.prefs.get("smooth_textures"))})
            self.view3d.appearance_changed(reupload=key == "smooth_textures")
        elif key in ("turn_step", "turn_fine"):
            big, fine = self.prefs.get("turn_step"), self.prefs.get("turn_fine")
            for a, (side, deg) in zip(self.a_turn, (("left", big), ("right", big), ("left", fine), ("right", fine))):
                a.setText(f"Turn {side} {deg:g}°")
        elif key == "click_size":
            self.top.click_size = float(v)
        elif key == "warn_moving":
            self.props.warn_moving = bool(v)
            self._show_selection()
        elif key == "warn_anim_size":
            self.anim_panel.warn_size = bool(v)
        elif key == "backup_minutes":
            if v:
                self.backup_timer.start(int(v) * 60 * 1000)
            else:
                self.backup_timer.stop()
        elif key == "show_start_page":
            self.start_page.set_show(bool(v))
            self._show_central()

    def _show_central(self):
        start = self.project is None and bool(self.prefs.get("show_start_page"))
        if start:
            self.start_page.set_recent(self._recent())
        self.central.setCurrentWidget(self.start_page if start else self.tabs)
        for dock in (self.stages_dock, self.props_dock):        # the start page gets the whole window
            dock.setVisible(not start)
        self.objects_toolbar.setVisible(not start)

    def _open_recent(self, path: str):
        if self.confirm_discard():
            self.open_rom(path)

    def backup_now(self) -> Optional[str]:
        """Write a plan of the current edits to the backup folder (only when something changed since the last
        backup); keep the newest backup_keep per ROM. Returns the file written, or None."""
        p = self.project
        if not p or not p.has_edits():
            return None
        mark = (id(p), p.edit_count, p.undo_stack[-1]["id"] if p.undo_stack else None)
        if mark == self._backup_count:
            return None
        import time
        name = re.sub(r"[^A-Za-z0-9._ -]+", "_", os.path.splitext(os.path.basename(p.rom_path))[0])[:60]
        folder = os.path.join(backup_dir(), name)
        try:
            os.makedirs(folder, exist_ok=True)
            out = os.path.join(folder, time.strftime("%Y-%m-%d_%H-%M-%S") + ".plan.json")
            p.export_plan(out)
        except (OSError, EditorError) as exc:
            self.statusBar().showMessage(f"Backup failed: {exc}", 8000)
            return None
        self._backup_count = mark
        keep = int(self.prefs.get("backup_keep"))
        files = sorted(f for f in os.listdir(folder) if f.endswith(".plan.json"))
        for f in files[:-keep]:
            try:
                os.remove(os.path.join(folder, f))
            except OSError:
                pass
        self.statusBar().showMessage(f"Backed up your edits ({os.path.basename(out)})", 4000)
        return out

    # ------------------------------------------------------------------ helpers
    def error(self, msg):
        QtWidgets.QMessageBox.warning(self, EDITOR_NAME, str(msg))

    def notice(self, msg):
        QtWidgets.QMessageBox.information(self, EDITOR_NAME, str(msg))

    def describe(self, o: dict) -> str:
        p = ", ".join(f"{v:.4g}" for v in o["position"])
        s = f"{o.get('label') or KIND_NAMES[o['kind']]}  ({p})"
        if o.get("heading") is not None:
            s += f"  facing {o['heading']:.1f}°"
        if o["kind"] == "goal":
            s += "  RED" if o.get("red") else ""
        if o["kind"] == "banana" and o.get("bunch"):
            s += "  bunch"
        if o["in_moving_group"]:
            s += f"\nin part '{o.get('group_name')}' (may move)"
        return s

    def floor_height(self, x, z, y_limit):
        st = self.project and self.stage_key and self.project.stage(self.stage_key)
        if not st:
            return None
        m = st.model
        f = floor_below(m["triangles"], (x, y_limit, z), tol=0.0) if m else None
        pf = st.platform_floor(x, z, y_limit)
        if pf is not None and (f is None or pf > f):
            f = pf
        return f

    def describe_geo(self, g: dict) -> str:
        s = f"{g.get('label', 'geometry')}  x {g['x'][0]:g}..{g['x'][1]:g}  z {g['z'][0]:g}..{g['z'][1]:g}"
        if g["type"] == "mesh":
            sc = g.get("size") or (g["scale"],) * 3
            scale = f"{sc[0]:g}" if sc[0] == sc[1] == sc[2] else "{:g} × {:g} × {:g}".format(*sc)
            s = (f"{g.get('label', 'model')}  at {g['pos'][0]:g}, {g['pos'][1]:g}, {g['pos'][2]:g}  scale "
                 f"{scale}  {g.get('triangles', len(g['faces']))} triangles")
        elif g["type"] == "platform":
            s += f"  top {g['top']:g}" + (f"  thickness {g['thickness']:g}" if g["thickness"] else "  (flat)")
            if g.get("rot"):
                s += "  turned {:g}° / {:g}° / {:g}°".format(*g["rot"])
        elif g.get("y"):
            s += f"  y {g['y'][0]:g}..{g['y'][1]:g}"
        return s

    def hover_top(self, sp):
        if not self.project or not self.stage_key:
            return
        x, z = sp
        f = self.floor_height(x, z, 1e9)
        surf = f"  surface y {f:.3f}" if f is not None else ""
        self.statusBar().showMessage(f"x {x:.3f}  z {z:.3f}{surf}")

    # ------------------------------------------------------------------ ROM / stages
    def choose_rom(self):
        if not self.confirm_discard():
            return
        start = self.settings.value("last_rom_dir", "")
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Open Touch & Roll ROM (it is never modified)",
                                                        start, "Nintendo DS ROM (*.nds)")
        if path:
            self.open_rom(path)

    def _recent(self) -> list:
        v = self.settings.value("recent_roms", [])
        if isinstance(v, str):
            v = [v]
        return [p for p in (v or []) if isinstance(p, str)]

    def _remember_rom(self, path: str):
        path = os.path.abspath(path)
        lst = [path] + [p for p in self._recent() if os.path.abspath(p) != path]
        self.settings.setValue("recent_roms", lst[:MAX_RECENT])
        self.settings.setValue("last_rom", path)

    def _fill_recent(self):
        self.recent_menu.clear()
        lst = [p for p in self._recent() if os.path.exists(p)]
        if not lst:
            a = self.recent_menu.addAction("(none yet)")
            a.setEnabled(False)
            return
        for p in lst:
            a = self.recent_menu.addAction(os.path.basename(p))
            a.setToolTip(p)
            a.triggered.connect(lambda _c=False, path=p: (self.confirm_discard() and self.open_rom(path)))
        self.recent_menu.addSeparator()
        self.recent_menu.addAction("Clear this list", lambda: self.settings.setValue("recent_roms", []))

    def open_guide(self, page: str = ""):
        from .guidewindow import GuideWindow
        if getattr(self, "guide_window", None) is None:
            self.guide_window = GuideWindow(EDITOR_NAME, self)
            if not self.windowIcon().isNull():
                self.guide_window.setWindowIcon(self.windowIcon())
        if page:
            self.guide_window.show_page(page)
        else:
            self.guide_window.show()
            self.guide_window.raise_()
            self.guide_window.activateWindow()

    def open_rom(self, path):
        try:
            QtWidgets.QApplication.setOverrideCursor(Qt.WaitCursor)
            proj = Project(path)
        except EditorError as exc:
            QtWidgets.QApplication.restoreOverrideCursor()
            self.error(exc)
            return
        QtWidgets.QApplication.restoreOverrideCursor()
        self.settings.setValue("last_rom_dir", os.path.dirname(os.path.abspath(path)))
        self._remember_rom(path)
        ic = editor_icon(path)
        if ic is not None:
            self.setWindowIcon(ic)
            QtWidgets.QApplication.instance().setWindowIcon(ic)
        rec = proj.opened_editor_output
        if rec:
            orig = find_original_rom(path, rec)
            if orig and self.ask(
                    f"{os.path.basename(path)} is a copy this editor saved from {os.path.basename(orig)}.\n\n"
                    f"Open {os.path.basename(orig)} and load this copy's edits instead? (Recommended: every save "
                    "then stays one step from the original, and Save keeps replacing this copy.)"):
                try:
                    base = Project(orig)
                    if rec.get("plan"):
                        base.import_plan(patch_record_path(path))
                    base.output_path = os.path.abspath(path)
                    base.saved_action = base.undo_stack[-1]["id"] if base.undo_stack else None
                except EditorError as exc:
                    self.error(f"Could not load the edits onto the original ({exc}); opening the copy as it is.")
                else:
                    self.open_project(base, orig)
                    return
        self.open_project(proj, path)

    def ask(self, question) -> bool:
        return QtWidgets.QMessageBox.question(self, EDITOR_NAME, question) == QtWidgets.QMessageBox.Yes

    def open_project(self, proj: Project, path: str):
        self.project = proj
        self._backup_count = None
        self._last_texture = {}
        self._show_central()
        self.stage_key = None
        self.fill_tree()
        self.update_actions()
        self.update_title()
        if proj.opened_editor_output:
            src = (proj.opened_editor_output.get("input_rom") or {}).get("name", "the original ROM")
            self.notice(
                f"This ROM is a copy the editor saved earlier (made from {src}). New edits will be added on "
                "top of it, and saves will record that chain.\n\nTo keep every save one step from the original "
                f"instead, open {src} and use File > Import edits with "
                f"{os.path.splitext(os.path.basename(path))[0]}.patch.json, then Save over the copy.")
        first = proj.worlds[0]["stages"][0]["stage_key"] if proj.worlds else None
        if first:
            self.load_stage(first)

    def fill_tree(self):
        self.tree.clear()
        p = self.project
        changed = set(p.changed_stages())
        self.tree_items = {}
        self.world_items = {}
        for w in p.worlds:
            wi = QtWidgets.QTreeWidgetItem([self._world_label(w["world"])])
            self.tree.addTopLevelItem(wi)
            self.world_items[w["world"]] = wi
            for s in w["stages"]:
                it = QtWidgets.QTreeWidgetItem([self._stage_label(p.entries[s["stage_key"]], s["stage_key"] in changed)])
                it.setData(0, Qt.UserRole, s["stage_key"])
                it.setToolTip(0, f"{s['stage_key']} (stage id {s['stage_id']})")
                wi.addChild(it)
                self.tree_items[s["stage_key"]] = it
        oi = QtWidgets.QTreeWidgetItem(["Other stage files (party / unused)"])
        self.tree.addTopLevelItem(oi)
        for k in p.other_keys:
            e = p.entries[k]
            it = QtWidgets.QTreeWidgetItem([self._stage_label(e, k in changed)])
            it.setData(0, Qt.UserRole, k)
            it.setToolTip(0, k if e.get("loadable") else f"{k}: not in the game's stage name table (never loaded)")
            oi.addChild(it)
            self.tree_items[k] = it
        self.tree.expandItem(self.tree.topLevelItem(0))

    def _stage_label(self, e, changed):
        if e.get("world"):
            lab = self.project.stage_label(e["stage_key"]) if self.project else e["label"]
        else:
            lab = e["stage_key"] + ("" if e.get("loadable", True) else "  (unused)")
        return ("● " if changed else "") + lab

    def _world_label(self, world: int) -> str:
        p = self.project
        title = p.world_name(world) if str(world) in p.names["worlds"] else p.worlds[world - 1]["title"]
        return f"World {world} — {title}"

    def refresh_names(self):
        """After renaming (or undoing a rename): tree labels, info panel, actions, title."""
        if not self.project:
            return
        for w, it in getattr(self, "world_items", {}).items():
            it.setText(0, self._world_label(w))
        self.refresh_tree_marks()
        if self.stage_key:
            self.info.setPlainText(self.stage_info(self.project.stage(self.stage_key)))
        self.update_actions()
        self.update_title()

    def _tree_menu(self, pos):
        it = self.tree.itemAt(pos)
        key = it.data(0, Qt.UserRole) if it else None
        if not key or not self.project:
            return
        if key != self.stage_key:
            self.load_stage(key)
        m = QtWidgets.QMenu(self)
        for a in (self.a_new_stage, self.a_edit_new, self.a_restore, self.a_range, self.a_names):
            m.addAction(a)
        m.exec(self.tree.viewport().mapToGlobal(pos))

    def create_new_stage(self, edit: bool = False):
        p, key = self.project, self.stage_key
        if not p or not key:
            return
        st = p.stage(key)
        cur = st.state.get("replace") if edit else None
        dlg = NewStageDialog(p.stage_label(key).replace("  [new stage]", ""), 8 * (st.pos_scale or 0) or None,
                             self, current=cur,
                             size=(self.prefs.get("new_stage_length"), self.prefs.get("new_stage_width")))
        if dlg.exec() != QtWidgets.QDialog.Accepted:
            return
        try:
            QtWidgets.QApplication.setOverrideCursor(Qt.WaitCursor)
            done = p.replace_with_new_stage(key, dlg.platform(), keep_edits=edit)
        except EditorError as exc:
            QtWidgets.QApplication.restoreOverrideCursor()
            self.error(exc)
            return
        QtWidgets.QApplication.restoreOverrideCursor()
        if done:
            self.load_stage(key)
            self.sync()
            self.statusBar().showMessage(
                "New stage created: one platform and the start. Add goals, bananas and platforms as usual."
                if not edit else "The new stage's platform changed.", 10000)

    def restore_original(self):
        p, key = self.project, self.stage_key
        if not p or not key or not p.stage(key).is_new_stage:
            return
        if not self.ask("Bring back the original stage?\n\nThe new stage and everything you added to it are "
                        "removed (Undo brings them back)."):
            return
        if p.restore_original_stage(key):
            self.load_stage(key)
            self.sync()

    def drawing_range(self):
        p, key = self.project, self.stage_key
        if not p or not key:
            return
        st = p.stage(key)
        orig = st.original_pos_scale
        if not orig:
            self.error("This stage has no model whose drawing range could be changed.")
            return
        dlg = DrawingRangeDialog(p.stage_label(key), orig, st.pos_scale or orig, self)
        if dlg.exec() != QtWidgets.QDialog.Accepted:
            return
        try:
            QtWidgets.QApplication.setOverrideCursor(Qt.WaitCursor)
            done = p.set_drawing_range(key, dlg.scale())
        except EditorError as exc:
            QtWidgets.QApplication.restoreOverrideCursor()
            self.error(exc)
            return
        QtWidgets.QApplication.restoreOverrideCursor()
        if done:
            self.sync()
            self.statusBar().showMessage(f"Drawing range of this stage: ±{8 * dlg.scale():g}.", 8000)

    def edit_names(self):
        p = self.project
        if not p:
            return
        if p.names_error:
            self.error(f"Names cannot be edited here: {p.names_error}")
            return
        e = p.entries.get(self.stage_key) if self.stage_key else None
        dlg = NamesDialog(p, self, world=(e or {}).get("world") or 1, show_warnings=self.prefs.get("warn_names"))
        if dlg.exec() != QtWidgets.QDialog.Accepted:
            return
        try:
            if p.set_names(dlg.result_names(), "rename worlds / stages"):
                self.statusBar().showMessage("Names changed. They are written to the ROM copy when you save.", 8000)
        except EditorError as exc:
            self.error(exc)
        self.refresh_names()

    def filter_tree(self, text: str):
        """Show only the stages whose label, world-stage number or file key contains the text."""
        t = text.strip().lower()
        if not self.project or not hasattr(self, "tree_items"):
            return
        exact = re.fullmatch(r"\d{1,2}-\d{1,2}", t) is not None       # "2-1" means stage 2-1, not 2-10
        for i in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(i)
            any_shown = False
            for j in range(top.childCount()):
                it = top.child(j)
                key = it.data(0, Qt.UserRole)
                hay = f"{it.text(0)} {key} {top.text(0)}".lower()
                label = it.text(0).lstrip("● ").lower()
                show = not t or (label.startswith(t + " ") if exact else t in hay)
                it.setHidden(not show)
                any_shown = any_shown or show
            top.setHidden(not any_shown)
            if t and any_shown:
                top.setExpanded(True)

    def refresh_tree_marks(self):
        changed = set(self.project.changed_stages())
        for k, it in self.tree_items.items():
            it.setText(0, self._stage_label(self.project.entries[k], k in changed))

    def tree_activated(self, item, _col=0):
        k = item.data(0, Qt.UserRole)
        if k and k != self.stage_key:
            self.load_stage(k)

    def load_stage(self, key):
        try:
            QtWidgets.QApplication.setOverrideCursor(Qt.WaitCursor)
            st = self.project.stage(key)
            tris = st.model["triangles"] if st.model else []
        except EditorError as exc:
            QtWidgets.QApplication.restoreOverrideCursor()
            self.error(exc)
            return
        finally:
            if QtWidgets.QApplication.overrideCursor():
                QtWidgets.QApplication.restoreOverrideCursor()
        self.stage_key = key
        self.selected = None
        objs = st.objects()
        self.objects = {o["id"]: o for o in objs}
        geo = st.geometry()
        self.geometry = {g["id"]: g for g in geo}
        bounds = self._bounds(st)
        self.side.proj = self._side_proj_for(st)
        self.top.set_stage(tris, objs, bounds)
        self.side.set_stage(tris, objs, bounds)
        self._sync_geometry_views(st)
        self._load_textures(st)
        self.sel_stack.setCurrentWidget(self.props)
        self.props.show_object(None)
        self.info.setPlainText(self.stage_info(st))
        it = self.tree_items.get(key)
        if it:
            self.tree.setCurrentItem(it)
        if self.anim_dock.isVisible():
            self.anim_panel.set_stage(self.project, key)
        msg = f"{self.project.stage_label(key) if st.entry.get('world') else st.entry['label']} ({key})"
        if not st.model:
            msg += " — no model file for this stage; objects only" if not st.model_error else \
                f" — model could not be read: {st.model_error}"
        self.statusBar().showMessage(msg, 8000)
        self.update_actions()

    def _load_textures(self, st):
        """Hand the stage's textures (with thumbnails) to the geometry panel."""
        try:
            choices = st.texture_choices()
            icons = make_icons(choices, st.texture_image)
        except Exception:                               # noqa: BLE001 - thumbnails must never block editing
            choices, icons = [], {}
        self.geo_panel.set_textures(choices, icons, self.project.textures_unlocked)
        self._tex_icons = (st.key, choices, icons)

    @staticmethod
    def _bounds(st):
        """Header 0x1C..0x33 (bounds-like values, Level 2) for a dashed reference rectangle."""
        h = st.doc.header
        try:
            lo = [h[f"bounds_min_{a}"]["value"] for a in "xyz"]
            hi = [h[f"bounds_max_{a}"]["value"] for a in "xyz"]
        except KeyError:
            return None
        return lo, hi

    # ------------------------------------------------------------------ keyframes
    def show_keyframes(self):
        self.anim_dock.show()
        self.anim_dock.raise_()

    def _anim_dock_shown(self, visible: bool):
        if not visible or not self.project:
            return
        self.anim_panel.set_stage(self.project, self.stage_key)
        if self.tabs.currentWidget() is not self.view3d:
            self.tabs.setCurrentWidget(self.view3d)
        self.anim_panel.set_frame(self.view3d.frame)

    def _panel_frame(self, f: float):
        self.view3d.stop()
        self.view3d.set_frame(f)

    def apply_keys(self, node: int, ch: str, keys):
        if not self.project or not self.stage_key:
            return
        try:
            QtWidgets.QApplication.setOverrideCursor(Qt.WaitCursor)
            try:
                self.project.set_anim_keys(self.stage_key, node, ch, keys)
            finally:
                QtWidgets.QApplication.restoreOverrideCursor()
        except EditorError as exc:
            self.error(exc)
            self.anim_panel.refresh()
            return
        self.sync()

    def apply_length(self, frames):
        if not self.project or not self.stage_key:
            return
        try:
            QtWidgets.QApplication.setOverrideCursor(Qt.WaitCursor)
            try:
                self.project.set_anim_length(self.stage_key, frames)
            finally:
                QtWidgets.QApplication.restoreOverrideCursor()
        except EditorError as exc:
            self.error(exc)
            self.anim_panel.refresh()
            return
        self.sync()
        self.anim_panel.refresh(refit=True)

    def stage_info(self, st) -> str:
        e = st.entry
        lines = [f"Stage:  {self.project.stage_label(st.key) if e.get('world') else e['label']}",
                 f"Key:    {st.key}", f"File:   stcoli/{st.key}_stc.lz",
                 f"Model:  {st.model['path'] if st.model else '(none)'}"]
        if st.is_new_stage:
            pf = st.state["replace"]["platform"]
            lines.append(f"NEW STAGE: platform x {pf['x'][0]:g}..{pf['x'][1]:g}, z {pf['z'][0]:g}..{pf['z'][1]:g}, "
                         f"top {pf['top']:g}, thickness {pf['thickness']:g} (Stage > Restore original stage "
                         "brings the original back)")
        if st.model:
            lines.append(f"        {len(st.model['triangles'])} triangles, {len(st.model['model'].nodes)} nodes, "
                         f"posScale {st.model['model'].pos_scale:g}")
        if st.pos_scale and st.original_pos_scale:
            lines.append(f"Drawing range: ±{8 * st.pos_scale:g}" + (
                f" (widened from ±{8 * st.original_pos_scale:g})" if st.state.get("pos_scale") else ""))
            for n in st.model["notes"]:
                lines.append(f"        note: {n}")
        if st.anim_parts():
            line = f"Animation: {len(st.anim_parts())} moving part(s), {st.frames} frames ({(st.frames - 1) / 60:.2f} s)"
            if st.state.get("anim"):
                a = st.state["anim"]
                bits = []
                if a.get("frames"):
                    bits.append(f"length changed from {st.original_frames}")
                if a.get("keys"):
                    bits.append(f"{len(a['keys'])} channel(s) with keys")
                line += " — " + ", ".join(bits)
            lines.append(line)
        d = st.doc
        lines.append(f"Layout: {d.layout_variant}")
        lines.append("Objects: " + ", ".join(f"{len([o for o in self.objects.values() if o['kind'] == k])} {k}"
                                             for k in ("start", "goal", "bumper", "banana")))
        lines.append("")
        lines.append("Groups (parts) — read-only:")
        kinds = st.group_kinds
        for g in d.groups:
            n = {k: g["slices"][k]["count"] for k in "ABC"}
            t = ", ".join(f"{v:g}" for v in g["translation"])
            lines.append(f"  {g['index']:2d} {g.get('name_text_04') or '?':24s} goals {n['A']} bumpers {n['B']} "
                         f"bananas {n['C']}  [{kinds.get(g['index'])}]  T=({t})")
        lines.append("")
        lines.append(f"Banana value: {st.banana_value} / 255 (a bunch counts 5)")
        lines.append("")
        lines.append("Edits in this stage:")
        pe, ops = st.plan_edits(), st.plan_ops()
        if not pe and not ops and not st.state.get("geometry"):
            lines.append("  (none)")
        for g in st.geometry():
            lines.append(f"  {self.describe_geo(g)}")
        if st.platform_shape:
            lines.append(f"  (platforms are drawn with the texture of shape '{st.platform_shape}')")
        for x in pe:
            lines.append(f"  {x['target']}: {x['from']} -> {x['to']}")
        for op in ops:
            where = op.get("position") or op.get("to") or op.get("expect_position")
            idx = f" {op['index'] + 1}" if "index" in op else ""
            lines.append(f"  {op['op']} {op['kind']}{idx} at ({', '.join(f'{v:g}' for v in where)})"
                         + ("  [collision cells rebuilt]" if op["op"] in ("move", "add") else ""))
        why = st.can_restructure()
        if why:
            lines.append(f"  (adding / deleting is not available here: {why})")
        return "\n".join(lines)

    # ------------------------------------------------------------------ selection & edits
    def select(self, oid):
        self.selected = oid if (oid in self.objects or oid in self.geometry) else None
        self.top.set_selected(self.selected)
        self.side.set_selected(self.selected)
        self.view3d.set_selected(self.selected)
        self._show_selection()
        self.update_actions()               # Delete / Duplicate follow the selection (0.2.1 fix)

    def _show_selection(self):
        g = self.geometry.get(self.selected)
        if g is not None:
            self.sel_stack.setCurrentWidget(self.geo_panel)
            self.geo_panel.show_geometry(g)
        else:
            self.sel_stack.setCurrentWidget(self.props)
            self.props.show_object(self.objects.get(self.selected))

    def _sync_geometry_views(self, st):
        geo = list(self.geometry.values())
        faces = st.collision_faces() if self.show_collision else None
        for v in (self.top, self.side):
            v.sync_geometry(geo)
            v.set_collision(faces)
        self._sync_3d()

    def _sync_3d(self):
        """Rebuild the 3D scene when the 3D tab is showing and the stage's model or collision changed;
        otherwise just refresh its objects and geometry."""
        if self.tabs.currentWidget() is not self.view3d or not self.project or not self.stage_key:
            return
        st = self.project.stage(self.stage_key)
        model = st.current_model
        faces = st.collision_faces()
        anim = st.anim_bytes
        src = self._scene_src
        sc = self.view3d.gl.scene if self.view3d.gl else None
        if src and sc is not None and src[0] == self.stage_key and src[1] is model and src[2] is faces:
            sc.objects = st.objects()
            sc.geometry = st.geometry()
            if src[3] is not anim:
                sc.set_animation(anim)
                self._scene_src = (self.stage_key, model, faces, anim)
                self.view3d.animation_changed()
            self.view3d.set_selected(self.selected)
            return
        try:
            QtWidgets.QApplication.setOverrideCursor(Qt.WaitCursor)
            scene = scene_from_state(st)
        except Exception as exc:                       # noqa: BLE001 - the 3D view must never break editing
            scene = None
            self.statusBar().showMessage(f"3D view: {exc}", 8000)
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()
        keep = bool(src and src[0] == self.stage_key)
        self._scene_src = (self.stage_key, model, faces, anim)
        self.view3d.set_scene(scene, keep_camera=keep)
        self.view3d.set_show_collision(self.show_collision)
        self.view3d.set_selected(self.selected)

    def fit_views(self):
        if self.tabs.currentWidget() is self.view3d and self.view3d.gl:
            self.view3d.gl.view_fit()
        else:
            self.top.fit()
            self.side.fit()

    def sync(self):
        st = self.project.stage(self.stage_key)
        objs = st.objects()
        self.objects = {o["id"]: o for o in objs}
        self.geometry = {g["id"]: g for g in st.geometry()}
        if self.selected not in self.objects and self.selected not in self.geometry:
            self.selected = None
        self.top.sync_objects(objs)
        self.side.sync_objects(objs)
        self._sync_geometry_views(st)
        tex = getattr(self, "_tex_icons", None)
        if not tex or tex[0] != self.stage_key or tex[1] != st.texture_choices():
            self._load_textures(st)
        self.top.set_selected(self.selected)
        self.side.set_selected(self.selected)
        self.view3d.set_selected(self.selected)
        self._show_selection()
        self.info.setPlainText(self.stage_info(st))
        self.refresh_tree_marks()
        self.update_actions()
        self.update_title()
        if self.anim_dock.isVisible():
            self.anim_panel.set_stage(self.project, self.stage_key)

    def do_change(self, oid, prop, value, label=None):
        if not self.project or not self.stage_key:
            return
        try:
            self.project.change(self.stage_key, oid, prop, value, label)
        except EditorError as exc:
            self.error(exc)
        self.sync()

    def move_object(self, oid, pos):
        self.do_change(oid, "position", pos, f"move {oid}")

    def move_geometry(self, gid, delta):
        self.do_change(gid, "move", delta, f"move {gid}")

    # ---- geometry
    def begin_geo(self, kind):
        if not self.project or not self.stage_key:
            return
        self.cancel_add()
        self.pending_geo = kind
        self.top.set_rect_mode(True)
        what = {"platform": "platform", "invisible": "invisible floor", "decoration": "decoration",
                "remove": "area whose collision is removed"}[kind]
        self.statusBar().showMessage(f"Drag a rectangle on the top view for the {what} (a click makes 4 x 4; "
                                     "Esc cancels)")

    def geo_rect_drawn(self, x0, z0, x1, z1):
        kind = self.pending_geo
        self.pending_geo = None
        self.top.set_rect_mode(False)
        if not kind or not self.project or not self.stage_key:
            return
        cx, cz = (x0 + x1) / 2, (z0 + z1) / 2
        f = self.floor_height(cx, cz, 1e9)
        tex = None
        if kind in ("platform", "decoration") and self.prefs.get("platform_texture") == "last" \
                and self.project.textures_unlocked:
            tex = self._last_texture.get(self.stage_key)
        try:
            gid = self.project.add_geometry(self.stage_key, kind, [x0, x1], [z0, z1],
                                            top=(f if f is not None else 0.0) if kind != "remove" else None,
                                            thickness=self.prefs.get("platform_thickness") if kind == "platform"
                                            else None, texture=tex)
        except EditorError as exc:
            self.error(exc)
            self.sync()
            return
        self.selected = gid
        self.sync()
        g = self.geometry.get(gid)
        if g:
            self.statusBar().showMessage(f"Added {self.describe_geo(g)}", 8000)

    # ---- adding / deleting
    def begin_add(self, kind):
        if not self.project or not self.stage_key:
            return
        self.pending_add = kind
        self.top.set_place_mode(True)
        name = "banana bunch" if kind == "bunch" else kind
        self.statusBar().showMessage(f"Click on the top view to place a {name} (Esc cancels)")

    def cancel_add(self):
        if getattr(self, "pending_add", None) or getattr(self, "pending_geo", None):
            self.pending_add = None
            self.pending_geo = None
            self.top.set_place_mode(False)
            self.top.set_rect_mode(False)
            self.statusBar().showMessage("Adding cancelled", 3000)

    def place_at(self, x, z):
        kind = getattr(self, "pending_add", None)
        self.pending_add = None
        self.top.set_place_mode(False)
        if not kind or not self.project or not self.stage_key:
            return
        real = "banana" if kind == "bunch" else kind
        f = self.floor_height(x, z, 1e9)
        y = (f if f is not None else 0.0) + (self.prefs.get("banana_height") if real == "banana" else DEFAULT_HEIGHT[real])
        snap = self.top.snap
        if snap:
            x, z = round(x / snap) * snap, round(z / snap) * snap
        heading = self.project.default_heading_towards_start(self.stage_key, (x, y, z)) \
            if real == "goal" and self.prefs.get("goal_faces_start") else None
        try:
            new_id = self.project.add_object(self.stage_key, real, (x, y, z), bunch=(kind == "bunch"),
                                             heading=heading)
        except EditorError as exc:
            self.error(exc)
            self.sync()
            return
        self.selected = new_id
        self.sync()
        note = "" if f is not None else "  (no floor under this point: check its height)"
        self.statusBar().showMessage(f"Added {self.objects[new_id]['label']} at ({x:.2f}, {y:.2f}, {z:.2f}){note}", 8000)

    def delete_selected(self):
        o = self.objects.get(self.selected) or self.geometry.get(self.selected)
        if not o or not o.get("deletable"):
            return
        try:
            self.project.delete_object(self.stage_key, o["id"])
        except EditorError as exc:
            self.error(exc)
        self.selected = None
        self.sync()

    def duplicate_selected(self):
        o = self.objects.get(self.selected) or self.geometry.get(self.selected)
        if not o or o["kind"] == "start":
            return
        try:
            step = self.top.snap or 1.0
            new_id = self.project.duplicate_object(self.stage_key, o["id"], (max(step, 1.0), 0.0, 0.0))
        except EditorError as exc:
            self.error(exc)
            self.sync()
            return
        self.selected = new_id
        self.sync()

    def property_changed(self, oid, prop, value):
        if prop == "texture" and self.stage_key:
            shape = value[1] if isinstance(value, (list, tuple)) else value
            self._last_texture[self.stage_key] = shape
        self.do_change(oid, prop, value, f"{prop} of {oid}")

    def nudge(self, dx, dy, dz):
        step = self.prefs.get("nudge_step") or self.top.snap or 0.25
        g = self.geometry.get(self.selected)
        if g is not None:
            self.do_change(g["id"], "move", [dx * step, dy * step, dz * step], f"nudge {g['id']}")
            return
        o = self.objects.get(self.selected)
        if not o or "position" not in o["editable"]:
            return
        p = list(o["position"])
        p[0] += dx * step
        p[1] += dy * step
        p[2] += dz * step
        self.do_change(o["id"], "position", p, f"nudge {o['id']}")

    def turn(self, deg):
        g = self.geometry.get(self.selected)
        if g is not None and g["type"] in ("platform", "mesh") and g.get("rotation_unlocked"):
            r = list(g.get("rot") or (0.0, 0.0, 0.0))
            r[1] = ((r[1] + deg + 180.0) % 360.0) - 180.0
            self.do_change(g["id"], "rot", r, f"turn {g['id']}")
            return
        o = self.objects.get(self.selected)
        if not o or "heading" not in o.get("editable", ()):
            return
        self.do_change(o["id"], "heading", (o["heading"] + deg) % 360.0, f"turn {o['id']}")

    def import_model(self):
        from .modeldialog import ImportModelDialog
        if not self.project or not self.stage_key:
            return
        start = self.settings.value("last_obj_dir", "")
        path, _f = QtWidgets.QFileDialog.getOpenFileName(self, "Import OBJ model", start,
                                                         "Wavefront OBJ (*.obj);;All files (*)")
        if not path:
            return
        self.settings.setValue("last_obj_dir", os.path.dirname(path))
        st = self.project.stage(self.stage_key)
        starts = (st.doc.start or {}).get("records", [])
        pos = list(starts[0]["position"]) if starts else [0.0, 0.0, 0.0]
        if starts:                                   # a little ahead of the start, on its floor
            h = math.radians(st.objects()[0].get("heading") or 0.0)
            pos = [pos[0] - 6.0 * math.sin(h), pos[1], pos[2] - 6.0 * math.cos(h)]
        tex = getattr(self, "_tex_icons", None)
        choices, icons = (tex[1], tex[2]) if tex and tex[0] == self.stage_key else ([], {})
        d = ImportModelDialog(path, pos, self.project.stage_budget(self.stage_key), self, textures=choices,
                              icons=icons, textures_unlocked=self.project.textures_unlocked,
                              defaults={"up": self.prefs.get("obj_up"), "both_sides": self.prefs.get("obj_both_sides"),
                                        "use_uvs": self.prefs.get("obj_use_uvs"),
                                        "warn_budget": self.prefs.get("warn_budget")})
        if d.exec() != QtWidgets.QDialog.Accepted or d.mesh is None:
            return
        v = d.values()
        try:
            QtWidgets.QApplication.setOverrideCursor(Qt.WaitCursor)
            try:
                ids = self.project.add_models(self.stage_key, v["mesh"], v["position"], v["scale"], v["rotation"],
                                              v["visible"], v["solid"], v["both_sides"], textures=v["textures"],
                                              use_uvs=v["use_uvs"], only=v["only"])
            finally:
                QtWidgets.QApplication.restoreOverrideCursor()
        except EditorError as exc:
            self.error(exc)
            return
        self.selected = ids[0] if ids else None
        self.sync()
        if len(ids) > 1:
            self.statusBar().showMessage(f"Imported {len(ids)} objects from {os.path.basename(path)}; each one is "
                                         "listed and can be selected and edited on its own.", 10000)

    def reset_selected(self):
        o = self.objects.get(self.selected)
        if not o:
            return
        try:
            self.project.reset_object(self.stage_key, o["id"])
        except EditorError as exc:
            self.error(exc)
        self.sync()

    def undo(self):
        a = self.project and self.project.undo()
        self._after_history(a)

    def redo(self):
        a = self.project and self.project.redo()
        self._after_history(a)

    def _after_history(self, a):
        if not a:
            return
        if a.get("key") is None:                    # a rename
            self.refresh_names()
            return
        if a["key"] != self.stage_key or (a["before"].get("replace") != a["after"].get("replace")):
            self.load_stage(a["key"])
        self.sync()

    # ------------------------------------------------------------------ view options
    def _side_proj_for(self, st) -> str:
        choice = self.side_combo.currentData()
        if choice != "auto":
            return choice
        pts = [o["position"] for o in self.objects.values()]
        if st.model and st.model["bounds"]:
            b = st.model["bounds"]
            pts += [b["min"], b["max"]]
        if not pts:
            return "right"
        ex = max(p[0] for p in pts) - min(p[0] for p in pts)
        ez = max(p[2] for p in pts) - min(p[2] for p in pts)
        return "behind" if ex > ez else "right"

    def change_side_projection(self, _i):
        if self.stage_key:
            self.side.proj = self._side_proj_for(self.project.stage(self.stage_key))
            st = self.project.stage(self.stage_key)
            self.side.set_stage(st.model["triangles"] if st.model else [], list(self.objects.values()),
                                self._bounds(st))
            self.side.sync_geometry(list(self.geometry.values()))
            self.side.set_collision(st.collision_faces() if self.show_collision else None)
            self.side.set_selected(self.selected)

    def toggle_collision(self, on):
        self.prefs.set("show_collision", bool(on))
        self.show_collision = on
        self.view3d.set_show_collision(on)
        if self.project and self.stage_key:
            self._sync_geometry_views(self.project.stage(self.stage_key))

    def toggle_grid(self, on):
        self.prefs.set("show_grid", bool(on))
        for v in (self.top, self.side):
            v.show_grid = on
            v.viewport().update()
            v.scene().update()

    def toggle_follow(self, on):
        self.prefs.set("follow_floor", bool(on))
        self.top.follow_floor = on

    def set_snap(self, val):
        if hasattr(self, "prefs"):
            self.prefs.set("snap", float(val))
        a = getattr(self, "snap_actions", {}).get(val)
        if a is not None and not a.isChecked():
            a.setChecked(True)
        self.top.snap = self.side.snap = val
        if hasattr(self, "view3d"):
            self.view3d.set_snap(val)

    # ------------------------------------------------------------------ saving
    def save(self):
        if not self.project:
            return
        if not self.project.output_path:
            return self.save_as()
        self._write(self.project.output_path, replace_own=True)

    def save_as(self):
        if not self.project:
            return
        name = self.prefs.get("save_name") or "My Touch & Roll hack.nds"
        if not name.lower().endswith(".nds"):
            name += ".nds"
        start = self.project.output_path or os.path.join(os.path.dirname(self.project.rom_path), name)
        dlg = QtWidgets.QFileDialog(self, "Save edited ROM as a new copy", start, "Nintendo DS ROM (*.nds)")
        dlg.setAcceptMode(QtWidgets.QFileDialog.AcceptSave)
        dlg.setDefaultSuffix("nds")
        dlg.setOption(QtWidgets.QFileDialog.DontConfirmOverwrite, True)
        if not dlg.exec():
            return
        path = dlg.selectedFiles()[0]
        if os.path.exists(path):
            r = QtWidgets.QMessageBox.question(
                self, EDITOR_NAME,
                f"{os.path.basename(path)} already exists.\n\nReplace it? This is only allowed for a copy this "
                "editor saved from the same original ROM and that has not been changed since.")
            if r != QtWidgets.QMessageBox.Yes:
                return
        self._write(path, replace_own=True)

    def _write(self, path, replace_own):
        try:
            QtWidgets.QApplication.setOverrideCursor(Qt.WaitCursor)
            rec = self.project.save(path, replace_own=replace_own)
            problems = verify_saved_rom(path, self.project)
        except EditorError as exc:
            QtWidgets.QApplication.restoreOverrideCursor()
            self.error(exc)
            return
        QtWidgets.QApplication.restoreOverrideCursor()
        if problems:
            self.error("The copy was written but re-reading it found problems:\n" + "\n".join(problems))
        verb = "Replaced" if rec.get("replaced_previous_output") else "Saved"
        self.statusBar().showMessage(f"{verb} {os.path.basename(path)} and {os.path.basename(rec['record_path'])} "
                                     f"(checked: only the edited stage files changed)", 10000)
        self.update_actions()
        self.update_title()
        if not problems and self.prefs.get("play_after_save"):
            self.play()

    def import_edits(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Import edits", self.settings.value("last_rom_dir", ""),
                                                        "Edit plan or patch record (*.json)")
        if not path:
            return
        try:
            n = self.project.import_plan(path)
        except EditorError as exc:
            self.error(exc)
            return
        self.refresh_tree_marks()
        if self.stage_key:
            self.sync()
        self.update_actions()
        self.statusBar().showMessage(f"Imported {n} edit(s) from {os.path.basename(path)}", 8000)

    def export_edits(self):
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Export edits as a plan (contains no game data)",
                                                        os.path.join(self.settings.value("last_rom_dir", ""),
                                                                     "my_edits.plan.json"), "Edit plan (*.json)")
        if not path:
            return
        try:
            self.project.export_plan(path)
        except (EditorError, OSError) as exc:
            self.error(exc)
            return
        self.statusBar().showMessage(f"Exported edits to {os.path.basename(path)}", 8000)

    # ------------------------------------------------------------------ BizHawk
    def choose_bizhawk(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Where is EmuHawk.exe?",
                                                        self.prefs.get("bizhawk"), "EmuHawk (EmuHawk*)")
        if path:
            self.prefs.set("bizhawk", path)

    def play(self):
        p = self.project
        if not p or not p.output_path:
            return
        if p.dirty:
            r = QtWidgets.QMessageBox.question(self, EDITOR_NAME, "Save the latest edits before playing?")
            if r == QtWidgets.QMessageBox.Yes:
                self.save()
        exe = self.prefs.get("bizhawk")
        if not exe or not os.path.exists(exe):
            self.choose_bizhawk()
            exe = self.prefs.get("bizhawk")
            if not exe:
                return
        try:
            subprocess.Popen([exe, p.output_path], cwd=os.path.dirname(exe))
        except OSError as exc:
            self.error(f"Could not start BizHawk: {exc}")

    # ------------------------------------------------------------------ closing
    def confirm_discard(self) -> bool:
        p = self.project
        if not p or not p.has_edits() or not p.dirty:
            return True
        r = QtWidgets.QMessageBox.question(
            self, EDITOR_NAME, "There are unsaved edits. Save them first?",
            QtWidgets.QMessageBox.Save | QtWidgets.QMessageBox.Discard | QtWidgets.QMessageBox.Cancel)
        if r == QtWidgets.QMessageBox.Save:
            self.save()
            return not p.dirty
        return r == QtWidgets.QMessageBox.Discard

    def closeEvent(self, ev):
        if self.confirm_discard():
            ev.accept()
        else:
            ev.ignore()

    def open_packages(self):
        from .packages import PackagesDialog
        PackagesDialog(self).exec()

    def about(self):
        from .aboutdialog import AboutDialog
        AboutDialog(EDITOR_NAME, __version__, self.windowIcon(), self).exec()


def self_test() -> int:
    """`--self-test`: check that a packaged copy has everything it needs (guide, fonts, icons of the views),
    without opening a window. Exit code 0 when all is there."""
    from .guidewindow import guide_pages
    problems = []
    if len(guide_pages()) < 15:
        problems.append("guide pages missing")
    fonts = os.path.join(HERE, "fonts")
    if not (os.path.isdir(fonts) and any(f.lower().endswith((".otf", ".ttf")) for f in os.listdir(fonts))):
        problems.append("Inter font missing")
    try:
        from . import view3d
        if view3d.available() is not None:
            problems.append(f"3D view: {view3d.available()}")
    except Exception as exc:                          # noqa: BLE001
        problems.append(f"3D view: {exc}")
    with open(os.path.join(_log_dir(), "self_test.txt"), "w", encoding="utf-8") as fh:
        fh.write(f"{EDITOR_NAME} {__version__}: " + ("ok" if not problems else "; ".join(problems)) + "\n")
    return 1 if problems else 0


def _log_dir() -> str:
    d = QtCore.QStandardPaths.writableLocation(QtCore.QStandardPaths.AppDataLocation) or os.path.expanduser("~")
    os.makedirs(d, exist_ok=True)
    return d


def main(argv=None) -> int:
    argv = list(sys.argv if argv is None else argv)
    if "--self-test" in argv:
        QtWidgets.QApplication.setApplicationName(EDITOR_NAME)
        QtWidgets.QApplication.setOrganizationName("TouchRollResearch")
        return self_test()
    set_windows_app_id()
    QtWidgets.QApplication.setApplicationName(EDITOR_NAME)
    QtWidgets.QApplication.setOrganizationName("TouchRollResearch")
    prefs = Prefs(QtCore.QSettings("TouchRollResearch", "tredit"))
    fmt = QtGui.QSurfaceFormat.defaultFormat()            # smooth edges in the 3D view (before any GL widget)
    fmt.setSamples(int(prefs.get("msaa")))
    QtGui.QSurfaceFormat.setDefaultFormat(fmt)
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(argv)
    sys.excepthook = _show_crash
    apply_theme(app, prefs.get("theme"))
    load_ui_font(app, prefs.get("font_size"))
    rom = argv[1] if len(argv) > 1 else None
    if rom is None and prefs.get("open_last_rom"):
        last = QtCore.QSettings("TouchRollResearch", "tredit").value("last_rom", "")
        rom = last if last and os.path.isfile(last) else None
    ic = editor_icon(rom or QtCore.QSettings("TouchRollResearch", "tredit").value("last_rom", ""))
    if ic is not None:
        app.setWindowIcon(ic)
    w = MainWindow(rom)
    if ic is not None:
        w.setWindowIcon(ic)
    w.show()
    return app.exec()
