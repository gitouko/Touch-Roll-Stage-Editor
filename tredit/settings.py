"""Edit > Settings: every preference the editor has, in one place.

SETTINGS lists them (key, default, kind, page, label, help). `Prefs` reads and writes them through
QSettings with their defaults and types; `SettingsDialog` shows them page by page. The main window applies
a setting as soon as it changes (MainWindow.apply_settings), so nothing needs a restart except the 3D
view's smoothing of edges, which the graphics driver sets up when the 3D view is created.
"""
from __future__ import annotations

import os
from typing import Any, Callable, Dict, List, Optional, Tuple

from PySide6 import QtCore, QtGui, QtWidgets

Qt = QtCore.Qt

# kind: "bool" | ("int", lo, hi) | ("float", lo, hi, step) | ("choice", [(value, label), ...]) | "colour" |
#       "text" | "exe"
SETTINGS: List[Tuple[str, Any, Any, str, str, str]] = [
    # General
    ("theme", "dark", ("choice", [("dark", "Dark"), ("light", "Light"), ("system", "Like Windows")]), "General",
     "Colour theme", "How the editor's windows look."),
    ("font_size", 9, ("int", 8, 14), "General", "Text size",
     "Size of the text in menus, panels and dialogs (points)."),
    ("open_last_rom", False, "bool", "General", "Open the last ROM when the editor starts",
     "Skips the start page and opens the ROM you used last time."),
    ("show_start_page", True, "bool", "General", "Show the start page while no ROM is open",
     "A page with big buttons to open your ROM, your recent ROMs and the guide."),
    # Editing
    ("snap", 0.25, ("choice", [(0.0, "Off (1/4096)"), (0.125, "0.125"), (0.25, "0.25"), (0.5, "0.5"),
                               (1.0, "1")]), "Editing",
     "Snap when dragging", "Dragged objects and shapes land on this grid. Same as View > Snap when dragging."),
    ("nudge_step", 0.0, ("float", 0.0, 10.0, 0.125), "Editing", "Arrow keys move by",
     "How far the arrow keys and Page Up / Page Down move the selection. 0 uses the snap step."),
    ("turn_step", 15.0, ("float", 1.0, 90.0, 1.0), "Editing", "Q and E turn by",
     "Degrees per press of Q or E."),
    ("turn_fine", 1.0, ("float", 0.1, 15.0, 0.1), "Editing", "Shift+Q and Shift+E turn by",
     "Degrees per press of Shift+Q or Shift+E, for fine turning."),
    ("follow_floor", True, "bool", "Editing", "Keep height above the floor while dragging",
     "A dragged object keeps its height above whatever floor is under it. Same as the View menu item."),
    ("banana_height", 0.5, ("float", 0.0, 5.0, 0.05), "Editing", "New bananas float this high",
     "Height of new bananas and bunches above the floor you click on."),
    ("goal_faces_start", True, "bool", "Editing", "New goals face the start",
     "A new goal turns its front towards the start, so you can roll straight into it."),
    # New platforms and models
    ("click_size", 4.0, ("float", 0.25, 64.0, 0.25), "New platforms and models", "A click makes a square of",
     "Clicking instead of dragging a rectangle adds a square shape this wide."),
    ("platform_thickness", 1.0, ("float", 0.0, 20.0, 0.25), "New platforms and models", "Thickness of new platforms",
     "0 makes flat panels."),
    ("platform_texture", "main", ("choice", [("main", "The stage's main floor texture"),
                                             ("last", "The texture you picked last in this stage")]),
     "New platforms and models", "Texture of new platforms", "Placeholder textures come from the stage itself."),
    ("new_stage_length", 10.0, ("float", 1.0, 64.0, 0.5), "New platforms and models", "New stage: platform length",
     "Starting value in Stage > Create new stage here."),
    ("new_stage_width", 6.0, ("float", 1.0, 64.0, 0.5), "New platforms and models", "New stage: platform width",
     "Starting value in Stage > Create new stage here."),
    ("obj_up", "y", ("choice", [("y", "Y is up (Blender, Wings3D, most programs)"), ("z", "Z is up")]),
     "New platforms and models", "OBJ files: up axis", "Starting value in the import window."),
    ("obj_both_sides", False, "bool", "New platforms and models", "OBJ files: Both sides ticked",
     "Starting value in the import window."),
    ("obj_use_uvs", True, "bool", "New platforms and models", "OBJ files: use the file's texture coordinates",
     "Starting value in the import window (when the file has them)."),
    # Map
    ("show_grid", True, "bool", "Map", "Show the grid", "Same as View > Show grid."),
    ("show_collision", True, "bool", "Map", "Show collision", "Same as View > Show collision (C)."),
    ("side_view", "auto", ("choice", [("auto", "Automatic (along the longer side)"), ("right", "From the right"),
                                      ("behind", "From behind")]), "Map", "Side view",
     "Which way the side view under the top view looks."),
    ("marker_scale", 1.0, ("float", 0.5, 3.0, 0.25), "Map", "Size of object markers",
     "Bigger markers are easier to click on large screens."),
    ("coll_colour", "#5ae6ff", "colour", "Map", "Collision of the fixed part", "Colour of the collision overlay."),
    ("coll_moving_colour", "#e696ff", "colour", "Map", "Collision of moving parts",
     "Colour of the collision overlay for parts that move."),
    # 3D view
    ("fov", 50.0, ("float", 30.0, 90.0, 5.0), "3D view", "Field of view", "Wider shows more, narrower zooms in."),
    ("orbit_speed", 1.0, ("float", 0.25, 3.0, 0.25), "3D view", "Mouse speed when turning the view", ""),
    ("invert_orbit", False, "bool", "3D view", "Invert up and down when turning the view", ""),
    ("smooth_textures", False, "bool", "3D view", "Smooth textures",
     "Off shows crisp pixels, like the DS. On blurs them a little."),
    ("background", "#404d73", "colour", "3D view", "Background colour", ""),
    ("msaa", 4, ("choice", [(0, "Off"), (2, "2×"), (4, "4×"), (8, "8×")]), "3D view", "Smooth edges",
     "Smoother edges cost a little speed. Takes effect after restarting the editor."),
    # Saving and testing
    ("bizhawk", "", "exe", "Saving and testing", "BizHawk (EmuHawk.exe)",
     "Used by File > Play saved copy in BizHawk (F5)."),
    ("save_name", "My Touch & Roll hack.nds", "text", "Saving and testing", "Name suggested for a new copy", ""),
    ("play_after_save", False, "bool", "Saving and testing", "Start BizHawk after every save", ""),
    ("backup_minutes", 5, ("int", 0, 120), "Saving and testing", "Back up your edits every (minutes)",
     "Writes a small plan of your edits (no game data) to the backup folder. 0 turns it off. Bring a backup "
     "back with File > Import edits."),
    ("backup_keep", 20, ("int", 1, 500), "Saving and testing", "Keep this many backups per ROM", ""),
    # Warnings
    ("warn_budget", True, "bool", "Warnings", "Warn when a stage gets bigger than the game's own",
     "In the import window: more triangles or collision faces than the game's most detailed stage."),
    ("warn_moving", True, "bool", "Warnings", "Point out objects on moving parts",
     "A note on the right when the selected object rides on a moving part."),
    ("warn_names", True, "bool", "Warnings", "Warn about very long names",
     "In World and stage names: names longer than the longest names in the game."),
    ("warn_anim_size", True, "bool", "Warnings", "Warn when an animation gets bigger than any tried in the game",
     "In the keyframe editor."),
]
SPEC = {s[0]: s for s in SETTINGS}
PAGES = []
for _s in SETTINGS:
    if _s[3] not in PAGES:
        PAGES.append(_s[3])


def backup_dir() -> str:
    base = QtCore.QStandardPaths.writableLocation(QtCore.QStandardPaths.AppDataLocation) or os.path.expanduser("~")
    return os.path.join(base, "backups")


class Prefs(QtCore.QObject):
    """Typed access to the settings, with their defaults. `changed(key)` fires after a change."""
    changed = QtCore.Signal(str)

    def __init__(self, qsettings: Optional[QtCore.QSettings] = None, parent=None):
        super().__init__(parent)
        self.qs = qsettings or QtCore.QSettings("TouchRollResearch", "tredit")

    def get(self, key: str):
        _k, default, kind, *_ = SPEC[key]
        raw = self.qs.value(f"prefs/{key}", None)
        if raw is None:
            return default
        try:
            if kind == "bool":
                return raw if isinstance(raw, bool) else str(raw).lower() in ("1", "true", "yes")
            if isinstance(kind, tuple) and kind[0] == "int":
                return max(kind[1], min(kind[2], int(raw)))
            if isinstance(kind, tuple) and kind[0] == "float":
                return max(kind[1], min(kind[2], float(raw)))
            if isinstance(kind, tuple) and kind[0] == "choice":
                for v, _l in kind[1]:
                    if str(v) == str(raw):
                        return v
                return default
            if kind == "colour":
                return raw if QtGui.QColor(str(raw)).isValid() else default
            return str(raw)
        except (TypeError, ValueError):
            return default

    def set(self, key: str, value):
        if key not in SPEC:
            raise KeyError(key)
        if self.get(key) == value and self.qs.value(f"prefs/{key}", None) is not None:
            return
        self.qs.setValue(f"prefs/{key}", value)
        self.changed.emit(key)

    def reset(self, keys=None):
        for key in keys or list(SPEC):
            self.qs.remove(f"prefs/{key}")
            self.changed.emit(key)

    def colour(self, key: str) -> QtGui.QColor:
        return QtGui.QColor(self.get(key))


class ColourButton(QtWidgets.QPushButton):
    picked = QtCore.Signal(str)

    def __init__(self, value: str, parent=None):
        super().__init__(parent)
        self.value = value
        self.clicked.connect(self._choose)
        self._show()

    def _show(self):
        self.setText(self.value)
        self.setStyleSheet(f"QPushButton {{ background: {self.value}; color: "
                           f"{'#000' if QtGui.QColor(self.value).lightness() > 128 else '#fff'}; }}")

    def set_value(self, value: str):
        self.value = value
        self._show()

    def _choose(self):
        c = QtWidgets.QColorDialog.getColor(QtGui.QColor(self.value), self, "Choose a colour")
        if c.isValid():
            self.set_value(c.name())
            self.picked.emit(self.value)


class SettingsDialog(QtWidgets.QDialog):
    """All settings, one page per topic. Changes apply at once; Reset puts a page back to its defaults."""

    def __init__(self, prefs: Prefs, parent=None, open_backups: Optional[Callable[[], None]] = None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.prefs = prefs
        self.editors: Dict[str, QtWidgets.QWidget] = {}
        self.resize(760, 520)
        lay = QtWidgets.QHBoxLayout(self)
        self.pages = QtWidgets.QListWidget()
        self.pages.setFixedWidth(190)
        self.stack = QtWidgets.QStackedWidget()
        lay.addWidget(self.pages)
        right = QtWidgets.QVBoxLayout()
        right.addWidget(self.stack, 1)
        lay.addLayout(right, 1)
        for page in PAGES:
            self.pages.addItem(page)
            w = QtWidgets.QWidget()
            form = QtWidgets.QFormLayout(w)
            form.setFieldGrowthPolicy(QtWidgets.QFormLayout.ExpandingFieldsGrow)
            for key, _d, kind, pg, label, help_ in SETTINGS:
                if pg != page:
                    continue
                ed = self._editor(key, kind)
                ed.setToolTip(help_)
                self.editors[key] = ed
                if kind == "bool":
                    form.addRow("", ed)
                else:
                    lab = QtWidgets.QLabel(label)
                    lab.setToolTip(help_)
                    form.addRow(lab, ed)
                if help_:
                    h = QtWidgets.QLabel(help_)
                    h.setWordWrap(True)
                    h.setStyleSheet("color: #8a8f9c; font-size: 8pt;")
                    form.addRow("", h)
            if page == "Saving and testing":
                row = QtWidgets.QHBoxLayout()
                b = QtWidgets.QPushButton("Open the backup folder")
                b.setToolTip(backup_dir())
                b.clicked.connect(open_backups or (lambda: None))
                self.backup_button = b
                row.addWidget(b)
                row.addStretch(1)
                form.addRow("", row)
            scroll = QtWidgets.QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setWidget(w)
            self.stack.addWidget(scroll)
        self.pages.currentRowChanged.connect(self.stack.setCurrentIndex)
        self.pages.setCurrentRow(0)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Close)
        self.reset_page = buttons.addButton("Reset this page", QtWidgets.QDialogButtonBox.ResetRole)
        self.reset_page.clicked.connect(self._reset_page)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        right.addWidget(buttons)
        prefs.changed.connect(self._refresh)

    # ---- editors
    def _editor(self, key: str, kind):
        val = self.prefs.get(key)
        if kind == "bool":
            label = SPEC[key][4]
            cb = QtWidgets.QCheckBox(label)
            cb.setChecked(bool(val))
            cb.toggled.connect(lambda v, k=key: self.prefs.set(k, bool(v)))
            return cb
        if kind in ("text", "exe"):
            le = QtWidgets.QLineEdit(str(val))
            le.editingFinished.connect(lambda k=key, w=le: self.prefs.set(k, w.text().strip()))
            if kind == "text":
                return le
            box = QtWidgets.QWidget()
            row = QtWidgets.QHBoxLayout(box)
            row.setContentsMargins(0, 0, 0, 0)
            row.addWidget(le, 1)
            b = QtWidgets.QPushButton("Browse…")

            def browse(_c=False, k=key, w=le):
                path, _f = QtWidgets.QFileDialog.getOpenFileName(self, "Where is EmuHawk.exe?", w.text(),
                                                                 "EmuHawk (EmuHawk*)")
                if path:
                    w.setText(path)
                    self.prefs.set(k, path)
            b.clicked.connect(browse)
            row.addWidget(b)
            box.line = le
            return box
        if kind == "colour":
            b = ColourButton(str(val))
            b.picked.connect(lambda v, k=key: self.prefs.set(k, v))
            return b
        if kind[0] == "int":
            sb = QtWidgets.QSpinBox()
            sb.setRange(kind[1], kind[2])
            sb.setValue(int(val))
            sb.setKeyboardTracking(False)
            sb.valueChanged.connect(lambda v, k=key: self.prefs.set(k, int(v)))
            return sb
        if kind[0] == "float":
            sb = QtWidgets.QDoubleSpinBox()
            sb.setRange(kind[1], kind[2])
            sb.setSingleStep(kind[3])
            sb.setDecimals(3 if kind[3] < 0.1 else 2)
            sb.setValue(float(val))
            sb.setKeyboardTracking(False)
            sb.valueChanged.connect(lambda v, k=key: self.prefs.set(k, float(v)))
            return sb
        cb = QtWidgets.QComboBox()
        for v, label in kind[1]:
            cb.addItem(label, v)
        cb.setCurrentIndex(max(0, [v for v, _l in kind[1]].index(val) if val in [v for v, _l in kind[1]] else 0))
        cb.activated.connect(lambda _i, k=key, w=cb: self.prefs.set(k, w.currentData()))
        return cb

    def _refresh(self, key: str):
        ed = self.editors.get(key)
        if ed is None:
            return
        val = self.prefs.get(key)
        kind = SPEC[key][2]
        ed.blockSignals(True)
        try:
            if kind == "bool":
                ed.setChecked(bool(val))
            elif kind == "text":
                ed.setText(str(val))
            elif kind == "exe":
                ed.line.setText(str(val))
            elif kind == "colour":
                ed.set_value(str(val))
            elif kind[0] in ("int", "float"):
                ed.setValue(val)
            else:
                i = ed.findData(val)
                ed.setCurrentIndex(max(0, i))
        finally:
            ed.blockSignals(False)

    def _reset_page(self):
        page = PAGES[self.pages.currentRow()]
        self.prefs.reset([k for k, _d, _kind, pg, _l, _h in SETTINGS if pg == page])
