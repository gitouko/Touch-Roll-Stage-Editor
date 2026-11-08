"""Small shared widgets: the X / Y / Z size row and the stage-texture picker."""
from __future__ import annotations

from typing import Callable, Dict, List, Optional, Sequence

from PySide6 import QtCore, QtGui, QtWidgets

Qt = QtCore.Qt
THUMB = 48


def texture_icon(w: int, h: int, rgba: bytes, size: int = THUMB) -> QtGui.QIcon:
    """A square icon of a texture. Narrow textures (the game's 4 x 64 side gradients) are repeated across
    so they show as a swatch instead of a thin line."""
    img = QtGui.QImage(rgba, w, h, 4 * w, QtGui.QImage.Format_RGBA8888).copy()
    side = max(w, h)
    sq = QtGui.QImage(side, side, QtGui.QImage.Format_RGBA8888)
    sq.fill(Qt.transparent)
    p = QtGui.QPainter(sq)
    p.drawTiledPixmap(0, 0, side, side, QtGui.QPixmap.fromImage(img))
    p.end()
    pm = QtGui.QPixmap.fromImage(sq.scaled(size, size, Qt.IgnoreAspectRatio, Qt.FastTransformation))
    return QtGui.QIcon(pm)


class XYZRow(QtWidgets.QWidget):
    """Three number boxes for x, y and z, with "Keep proportions" (changing one changes all three by the
    same factor). Emits edited((x, y, z)) when the user finishes an edit."""
    edited = QtCore.Signal(tuple)

    def __init__(self, lo: float, hi: float, step: float, decimals: int = 3, suffix: str = "",
                 tips: Sequence[str] = ("Along X", "Along Y (up)", "Along Z"), keep: bool = True, parent=None):
        super().__init__(parent)
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.boxes: List[QtWidgets.QDoubleSpinBox] = []
        for tip, axis in zip(tips, "XYZ"):
            sb = QtWidgets.QDoubleSpinBox()
            sb.setRange(lo, hi)
            sb.setDecimals(decimals)
            sb.setSingleStep(step)
            sb.setSuffix(suffix)
            sb.setPrefix(f"{axis} ")
            sb.setToolTip(tip)
            sb.setKeyboardTracking(False)
            sb.editingFinished.connect(lambda k=len(self.boxes): self._finished(k))
            lay.addWidget(sb)
            self.boxes.append(sb)
        self.keep = QtWidgets.QCheckBox("Keep proportions")
        self.keep.setToolTip("Change all three together, by the same factor")
        self.keep.setVisible(keep)
        lay.addWidget(self.keep)
        self._shown = (1.0, 1.0, 1.0)

    def set_values(self, vals: Sequence[float]):
        for sb, v in zip(self.boxes, vals):
            sb.blockSignals(True)
            sb.setValue(float(v))
            sb.blockSignals(False)
        self._shown = tuple(sb.value() for sb in self.boxes)

    def values(self) -> tuple:
        return tuple(sb.value() for sb in self.boxes)

    def setEnabled(self, on: bool):                 # noqa: N802 - Qt name
        super().setEnabled(on)
        for sb in self.boxes:
            sb.setEnabled(on)
        self.keep.setEnabled(on)

    def set_tooltip(self, tip: str):
        for sb, axis in zip(self.boxes, "XYZ"):
            sb.setToolTip(tip or f"Along {axis}")

    def _finished(self, k: int):
        new = list(self.values())
        old = self._shown
        if abs(new[k] - old[k]) < 1e-9:
            return
        if self.keep.isChecked() and abs(old[k]) > 1e-9:
            f = new[k] / old[k]
            for j, sb in enumerate(self.boxes):
                if j != k:
                    lo, hi = sb.minimum(), sb.maximum()
                    new[j] = max(lo, min(hi, round(old[j] * f, sb.decimals())))
        self.set_values(new)
        self.edited.emit(tuple(new))


class TextureCombo(QtWidgets.QComboBox):
    """Picks one of the stage's textures (the shapes of trcore.newstage.texture_choices). The stage's main
    floor texture comes first; picking it stores None ("the stage's main texture")."""
    picked = QtCore.Signal(object)                  # shape name or None

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setIconSize(QtCore.QSize(32, 32))
        self.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.setMinimumContentsLength(14)
        self.activated.connect(self._activated)

    def set_choices(self, choices: List[dict], icons: Dict[str, QtGui.QIcon]):
        self.blockSignals(True)
        self.clear()
        for c in choices:
            label = c["label"] + ("  — main" if c.get("default") else "")
            self.addItem(icons.get(c["shape"], QtGui.QIcon()), label, None if c.get("default") else c["shape"])
            self.setItemData(self.count() - 1, f"{c['orig_w']} × {c['orig_h']} texels, drawn by the stage's shape "
                                               f"{c['shape']}", Qt.ToolTipRole)
        self.blockSignals(False)

    def set_current(self, shape: Optional[str]):
        self.blockSignals(True)
        i = self.findData(shape) if shape else 0
        self.setCurrentIndex(max(0, i))
        self.blockSignals(False)

    def current_shape(self) -> Optional[str]:
        return self.currentData()

    def _activated(self, _i):
        self.picked.emit(self.currentData())


def make_icons(choices: List[dict], image_of: Callable[[str], Optional[tuple]]) -> Dict[str, QtGui.QIcon]:
    out = {}
    for c in choices:
        img = image_of(c["shape"])
        if img:
            out[c["shape"]] = texture_icon(*img)
    return out
