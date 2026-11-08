"""Dialogs for the Stage menu: Create New Stage (and editing its platform) and Drawing range."""
from __future__ import annotations

from typing import Optional

from PySide6 import QtCore, QtWidgets

Qt = QtCore.Qt


class NewStageDialog(QtWidgets.QDialog):
    """Size and height of the new stage's starting platform. The start goes 1.5 in from the platform's
    -x end, facing along it (as in edit test 5)."""

    def __init__(self, stage_label: str, drawing_range: Optional[float], parent=None, current: Optional[dict] = None,
                 size=(10.0, 6.0)):
        super().__init__(parent)
        editing = current is not None
        self.setWindowTitle("Edit the new stage's platform" if editing else "Create a new stage")
        lay = QtWidgets.QVBoxLayout(self)
        if editing:
            text = (f"<b>{stage_label}</b> is a new stage. Change its starting platform here; your objects and "
                    "platforms stay.")
        else:
            text = (f"<b>Replace {stage_label} with a new, empty stage.</b><br><br>"
                    "The new stage has <b>one platform and the start</b>, nothing else: no goal, no bananas. Add "
                    "those afterwards like in any other stage. The old stage's drawing, collision and objects, and "
                    "<b>your edits to it</b>, are left out of the copy you save.<br><br>"
                    "Your ROM keeps the original. <i>Stage &gt; Restore original stage</i> brings it back at any "
                    "time, even after saving and closing the editor.")
        lab = QtWidgets.QLabel(text)
        lab.setWordWrap(True)
        lay.addWidget(lab)
        form = QtWidgets.QFormLayout()
        lim = drawing_range or 64.0

        def spin(lo, hi, val, step=0.5, dec=2):
            sb = QtWidgets.QDoubleSpinBox()
            sb.setRange(lo, hi)
            sb.setDecimals(dec)
            sb.setSingleStep(step)
            sb.setValue(val)
            return sb
        ln, wd = (min(float(v), 2 * lim) for v in size)
        pf = (current or {}).get("platform") or {"x": [-ln / 2, ln / 2], "z": [-wd / 2, wd / 2], "top": 0.0,
                                                 "thickness": 1.0}
        self.length = spin(1.0, 2 * lim, pf["x"][1] - pf["x"][0])
        self.width = spin(1.0, 2 * lim, pf["z"][1] - pf["z"][0])
        self.top = spin(-lim, lim, pf["top"], 0.25)
        self.thickness = spin(0.25, 16.0, pf["thickness"], 0.25)
        form.addRow("Length (along the start's direction)", self.length)
        form.addRow("Width", self.width)
        form.addRow("Height of the top", self.top)
        form.addRow("Thickness", self.thickness)
        lay.addLayout(form)
        note = QtWidgets.QLabel(
            f"The platform is centred on the stage's middle and drawn with one of the stage's own textures. "
            f"It must stay within the drawing range (±{lim:g}); <i>Stage &gt; Drawing range…</i> widens it.")
        note.setWordWrap(True)
        note.setStyleSheet("color: gray;")
        lay.addWidget(note)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        bb.button(QtWidgets.QDialogButtonBox.Ok).setText("Apply" if editing else "Replace this stage")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def platform(self) -> dict:
        lx, wz = self.length.value() / 2, self.width.value() / 2
        return {"x": [-lx, lx], "z": [-wz, wz], "top": self.top.value(), "thickness": self.thickness.value()}


class DrawingRangeDialog(QtWidgets.QDialog):
    """Choose the stage model's position scale: original, x2 or x4 (edit test 6, item U)."""

    def __init__(self, stage_label: str, original: float, current: float, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Drawing range")
        lay = QtWidgets.QVBoxLayout(self)
        lab = QtWidgets.QLabel(
            f"<b>{stage_label}</b>: how far from the stage's centre platforms can be drawn.<br><br>"
            "A model can only draw within ±8 × its position scale. A larger range keeps the stage looking the "
            "same: every point of the model is stored on a slightly coarser grid, still finer than 1/500 of a "
            "unit. Collision and objects have no such limit.")
        lab.setWordWrap(True)
        lay.addWidget(lab)
        self.group = QtWidgets.QButtonGroup(self)
        self.options = []
        for k, (mult, name) in enumerate(((1, "Original"), (2, "Twice as far"), (4, "Four times as far"))):
            scale = original * mult
            rb = QtWidgets.QRadioButton(f"{name}: ±{8 * scale:g} (position scale {scale:g})")
            rb.setChecked(abs(scale - current) < 1e-9)
            self.group.addButton(rb, k)
            self.options.append(scale)
            lay.addWidget(rb)
        if self.group.checkedId() < 0:
            self.group.button(0).setChecked(True)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def scale(self) -> float:
        return self.options[max(0, self.group.checkedId())]
