"""Geometry > Import OBJ model: which objects, where they go, how big, turned how, drawn and/or solid, and
which of the stage's textures each material is drawn with."""
from __future__ import annotations

import html
from typing import Dict, List, Optional

from PySide6 import QtCore, QtGui, QtWidgets

from .objimport import ObjError, read_obj
from .widgets import TextureCombo, XYZRow

Qt = QtCore.Qt


class ImportModelDialog(QtWidgets.QDialog):
    def __init__(self, path: str, position, budget: dict, parent=None, textures: Optional[List[dict]] = None,
                 icons: Optional[Dict[str, QtGui.QIcon]] = None, textures_unlocked: bool = False,
                 defaults: Optional[dict] = None):
        super().__init__(parent)
        self.setWindowTitle("Import OBJ model")
        self.path = path
        self.mesh: Optional[dict] = None
        self.error: Optional[str] = None
        self.budget = budget
        self.textures = list(textures or [])
        self.icons = dict(icons or {})
        self.textures_unlocked = bool(textures_unlocked)
        self.defaults = {"up": "y", "both_sides": False, "use_uvs": True, "warn_budget": True}
        self.defaults.update(defaults or {})
        lay = QtWidgets.QVBoxLayout(self)
        self.info = QtWidgets.QLabel("")
        self.info.setWordWrap(True)
        lay.addWidget(self.info)
        cols = QtWidgets.QHBoxLayout()
        lay.addLayout(cols)

        left = QtWidgets.QFormLayout()
        cols.addLayout(left, 3)
        self.up = QtWidgets.QComboBox()
        self.up.addItem("Y is up (Blender, Wings3D, Maya, 3ds Max, most programs)", "y")
        self.up.addItem("Z is up (some CAD and SketchUp exports)", "z")
        self.up.setCurrentIndex(1 if self.defaults["up"] == "z" else 0)
        self.up.currentIndexChanged.connect(self._reload)
        left.addRow("Up axis", self.up)
        self.scale = XYZRow(0.001, 1000.0, 0.1, 4, tips=("Scale along X", "Scale along Y (up)", "Scale along Z"))
        self.scale.keep.setChecked(True)
        self.scale.set_values((1.0, 1.0, 1.0))
        self.scale.edited.connect(lambda _v: self._update())
        left.addRow("Scale", self.scale)
        self.size = QtWidgets.QLabel("")
        left.addRow("Size in the stage", self.size)

        def spin(lo, hi, val, step, dec=3, suffix=""):
            sb = QtWidgets.QDoubleSpinBox()
            sb.setRange(lo, hi)
            sb.setDecimals(dec)
            sb.setSingleStep(step)
            sb.setValue(val)
            sb.setSuffix(suffix)
            return sb
        row = QtWidgets.QHBoxLayout()
        self.pos = [spin(-4000, 4000, float(v), 0.25) for v in position]
        for sb in self.pos:
            row.addWidget(sb)
        left.addRow("Position X Y Z", row)
        row = QtWidgets.QHBoxLayout()
        self.rot = [spin(-180, 180, 0.0, 5, 2, "°") for _ in range(3)]
        for sb, tip in zip(self.rot, ("Tilt", "Swivel", "Rotate")):
            sb.setToolTip(tip)
            row.addWidget(sb)
        left.addRow("Tilt / Swivel / Rotate", row)
        self.visible = QtWidgets.QCheckBox("Drawn (with the textures picked on the right)")
        self.visible.setChecked(True)
        self.solid = QtWidgets.QCheckBox("Solid (the ball rolls on it)")
        self.solid.setChecked(True)
        self.both = QtWidgets.QCheckBox("Both sides (adds the back of every face)")
        self.both.setChecked(bool(self.defaults["both_sides"]))
        for cb in (self.visible, self.solid, self.both):
            cb.toggled.connect(self._update)
            left.addRow("", cb)

        right = QtWidgets.QVBoxLayout()
        cols.addLayout(right, 2)
        right.addWidget(QtWidgets.QLabel("<b>Objects in the file</b> (each becomes its own piece)"))
        self.objects = QtWidgets.QListWidget()
        self.objects.setMinimumHeight(110)
        self.objects.itemChanged.connect(lambda _i: self._update())
        right.addWidget(self.objects)
        right.addWidget(QtWidgets.QLabel("<b>Textures</b> (the stage's own, for now)"))
        self.mat_box = QtWidgets.QWidget()
        self.mat_form = QtWidgets.QFormLayout(self.mat_box)
        self.mat_form.setContentsMargins(0, 0, 0, 0)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.mat_box)
        scroll.setMinimumHeight(110)
        right.addWidget(scroll)
        self.use_uv = QtWidgets.QCheckBox("Use the file's texture coordinates (UVs)")
        self.use_uv.setToolTip("Lays each texture on the way your 3D program's UV map does. Off: textures are "
                               "laid on from above, like on platforms.")
        right.addWidget(self.use_uv)
        self.mat_combos: Dict[str, TextureCombo] = {}

        self.note = QtWidgets.QLabel("")
        self.note.setWordWrap(True)
        self.note.setStyleSheet("color: #8a8f9c;")
        lay.addWidget(self.note)
        self.buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        self.buttons.button(QtWidgets.QDialogButtonBox.Ok).setText("Import")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        lay.addWidget(self.buttons)
        self._reload()

    # ------------------------------------------------------------------ reading the file
    def _reload(self):
        try:
            self.mesh = read_obj(self.path, self.up.currentData())
            self.error = None
        except ObjError as exc:
            self.mesh = None
            self.error = str(exc)
        self._fill()
        self._update()

    def _fill(self):
        self.objects.blockSignals(True)
        self.objects.clear()
        while self.mat_form.rowCount():
            self.mat_form.removeRow(0)
        self.mat_combos = {}
        if self.mesh is not None:
            for o in self.mesh["objects"]:
                w, h, d = o["size"]
                it = QtWidgets.QListWidgetItem(f"{o['name']} — {len(o['faces'])} triangles, "
                                               f"{w:.2f} × {h:.2f} × {d:.2f}")
                it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
                it.setCheckState(Qt.Checked)
                self.objects.addItem(it)
            info = self.mesh.get("material_info") or {}
            for m in self.mesh["materials"]:
                cb = TextureCombo()
                cb.set_choices(self.textures, self.icons)
                cb.setEnabled(self.textures_unlocked and bool(self.textures))
                if not self.textures_unlocked:
                    cb.setToolTip("The stage's other textures unlock once edit test 10 passes in-game. Until then "
                                  "everything is drawn with the stage's main texture.")
                image = info.get(m, {}).get("map_Kd")
                label = QtWidgets.QLabel(html.escape(m) + (f"<br><span style='color:#8a8f9c'>{html.escape(image)}</span>"
                                                          if image else ""))
                label.setTextFormat(Qt.RichText)
                self.mat_form.addRow(label, cb)
                self.mat_combos[m] = cb
            has_uv = bool(self.mesh.get("has_uv"))
            self.use_uv.setEnabled(has_uv and self.textures_unlocked)
            self.use_uv.setChecked(has_uv and self.textures_unlocked and bool(self.defaults["use_uvs"]))
            self.use_uv.setText("Use the file's texture coordinates (UVs)" if has_uv else
                                "Use the file's texture coordinates (the file has none)")
        self.objects.blockSignals(False)

    def _chosen(self) -> List[int]:
        return [k for k in range(self.objects.count()) if self.objects.item(k).checkState() == Qt.Checked]

    # ------------------------------------------------------------------ summary and limits
    def _update(self):
        ok = self.buttons.button(QtWidgets.QDialogButtonBox.Ok)
        if self.mesh is None:
            self.info.setText(f"<b>This file cannot be imported.</b><br>{html.escape(str(self.error))}")
            self.size.setText("")
            ok.setEnabled(False)
            return
        m = self.mesh
        pick = self._chosen()
        objs = [m["objects"][k] for k in pick]
        tris = sum(len(o["faces"]) for o in objs) * (2 if self.both.isChecked() else 1)
        dropped = f" ({m['dropped']} flat faces left out)" if m["dropped"] else ""
        n = len(m["objects"])
        self.info.setText(f"<b>{html.escape(m['name'])}.obj</b>: {n} object{'s' if n != 1 else ''}, "
                          f"{sum(len(o['faces']) for o in m['objects'])} triangles{dropped}.")
        s = self.scale.values()
        w, h, d = (v * k for v, k in zip(m["size"], s))
        self.size.setText(f"{w:.2f} wide × {h:.2f} high × {d:.2f} deep (whole file)")
        b = self.budget
        lines = ["The position is where the bottom middle of the whole file goes; the objects keep their "
                 "places around it. After importing, each object can be selected, moved, turned, sized and "
                 "textured on its own."]
        if not self.textures_unlocked:
            lines.append("For now everything is drawn with the stage's main texture; picking other stage "
                         "textures unlocks once edit test 10 passes in-game. Your own textures come in 1.1.")
        else:
            lines.append("The textures are placeholders from the stage itself; your own textures come in 1.1.")
        warn = bool(self.defaults["warn_budget"])
        if warn and self.visible.isChecked() and b["triangles"] + tris > b["largest_triangles"]:
            lines.append(f"With this model the stage draws {b['triangles'] + tris} triangles; the most detailed "
                         f"stage in the game draws {b['largest_triangles']}. It may slow the game down or "
                         "flicker; try it in an emulator.")
        if warn and self.solid.isChecked() and b["faces"] + tris > b["largest_faces"]:
            lines.append(f"With this model the stage has {b['faces'] + tris} collision faces; the most in the "
                         f"game is {b['largest_faces']}.")
        self.note.setText("\n".join(lines))
        ok.setEnabled(bool(pick) and (self.visible.isChecked() or self.solid.isChecked()))

    def values(self) -> dict:
        s = list(self.scale.values())
        return {"mesh": self.mesh, "position": [sb.value() for sb in self.pos],
                "scale": s[0] if s[0] == s[1] == s[2] else s, "rotation": [sb.value() for sb in self.rot],
                "visible": self.visible.isChecked(), "solid": self.solid.isChecked(),
                "both_sides": self.both.isChecked(), "only": self._chosen(),
                "textures": {m: cb.current_shape() for m, cb in self.mat_combos.items()},
                "use_uvs": self.use_uv.isChecked()}
