"""The page shown while no ROM is open: open your ROM, pick a recent one, or read the guide."""
from __future__ import annotations

import os
from typing import List

from PySide6 import QtCore, QtWidgets

Qt = QtCore.Qt


class StartPage(QtWidgets.QWidget):
    open_clicked = QtCore.Signal()
    recent_clicked = QtCore.Signal(str)
    guide_clicked = QtCore.Signal(str)
    show_toggled = QtCore.Signal(bool)

    def __init__(self, name: str, version: str, parent=None):
        super().__init__(parent)
        outer = QtWidgets.QVBoxLayout(self)
        outer.addStretch(1)
        box = QtWidgets.QWidget()
        box.setMaximumWidth(620)
        lay = QtWidgets.QVBoxLayout(box)
        title = QtWidgets.QLabel(name)
        f = title.font()
        f.setPointSize(f.pointSize() + 9)
        f.setBold(True)
        title.setFont(f)
        lay.addWidget(title)
        sub = QtWidgets.QLabel(f"Version {version} · stages of Super Monkey Ball: Touch &amp; Roll (Nintendo DS, USA)")
        sub.setTextFormat(Qt.RichText)
        sub.setStyleSheet("color: #8a8f9c;")
        lay.addWidget(sub)
        lay.addSpacing(18)
        self.open_button = QtWidgets.QPushButton("Open your Touch && Roll ROM…")
        self.open_button.setMinimumHeight(46)
        bf = self.open_button.font()
        bf.setPointSize(bf.pointSize() + 2)
        bf.setBold(True)
        self.open_button.setFont(bf)
        self.open_button.setToolTip("Ctrl+O. Pick your own copy of the game (the USA version).")
        self.open_button.clicked.connect(self.open_clicked)
        lay.addWidget(self.open_button)
        safe = QtWidgets.QLabel("Your ROM is only read. Every save writes a separate, checked copy next to a small "
                                ".patch.json file that lists what changed.")
        safe.setWordWrap(True)
        safe.setStyleSheet("color: #8a8f9c;")
        lay.addWidget(safe)
        lay.addSpacing(14)
        self.recent_title = QtWidgets.QLabel("<b>Recent ROMs</b>")
        lay.addWidget(self.recent_title)
        self.recent_box = QtWidgets.QVBoxLayout()
        lay.addLayout(self.recent_box)
        lay.addSpacing(14)
        row = QtWidgets.QHBoxLayout()
        self.guide_button = QtWidgets.QPushButton("New here? Read “Getting started”")
        self.guide_button.clicked.connect(lambda: self.guide_clicked.emit("getting_started.html"))
        self.guide_all = QtWidgets.QPushButton("The whole guide (F1)")
        self.guide_all.clicked.connect(lambda: self.guide_clicked.emit(""))
        row.addWidget(self.guide_button)
        row.addWidget(self.guide_all)
        row.addStretch(1)
        lay.addLayout(row)
        lay.addSpacing(10)
        self.show_check = QtWidgets.QCheckBox("Show this page when no ROM is open")
        self.show_check.setChecked(True)
        self.show_check.toggled.connect(self.show_toggled)
        lay.addWidget(self.show_check)
        h = QtWidgets.QHBoxLayout()
        h.addStretch(1)
        h.addWidget(box)
        h.addStretch(1)
        outer.addLayout(h)
        outer.addStretch(2)
        self.recent_buttons: List[QtWidgets.QPushButton] = []

    def set_recent(self, paths: List[str]):
        for b in self.recent_buttons:
            self.recent_box.removeWidget(b)
            b.hide()
            b.setParent(None)
            b.deleteLater()
        self.recent_buttons = []
        shown = [p for p in paths if os.path.isfile(p)][:5]
        for p in shown:
            b = QtWidgets.QPushButton(os.path.basename(p).replace("&", "&&"))
            b.setToolTip(p)
            b.setStyleSheet("text-align: left; padding: 6px 10px;")
            b.setIcon(self.style().standardIcon(QtWidgets.QStyle.SP_FileIcon))
            b.clicked.connect(lambda _c=False, path=p: self.recent_clicked.emit(path))
            self.recent_box.addWidget(b)
            self.recent_buttons.append(b)
        self.recent_title.setVisible(bool(shown))

    def set_show(self, on: bool):
        self.show_check.blockSignals(True)
        self.show_check.setChecked(on)
        self.show_check.blockSignals(False)
