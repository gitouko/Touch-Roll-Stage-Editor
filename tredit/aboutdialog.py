"""Help > About. The paragraph about the editor comes from `about.txt` next to this file, written by the
editor's author; the rest (version, licences, what the editor does with your ROM) is filled in here."""
from __future__ import annotations

import html
import os
from typing import Optional

from PySide6 import QtCore, QtGui, QtWidgets

Qt = QtCore.Qt
HERE = os.path.dirname(os.path.abspath(__file__))
ABOUT_FILE = os.path.join(HERE, "about.txt")


def author_text(path: str = ABOUT_FILE) -> str:
    """The author's own words (plain text, blank lines between paragraphs), or "" if there are none."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def about_html(name: str, version: str, text: Optional[str] = None) -> str:
    text = author_text() if text is None else text.strip()
    parts = [f"<h2 style='margin-bottom:2px'>{html.escape(name)}</h2>",
             f"<p style='color:#8a8f9c; margin-top:0'>Version {html.escape(version)}</p>"]
    if text:
        for para in text.split("\n\n"):
            parts.append("<p>" + html.escape(para.strip()).replace("\n", "<br>") + "</p>")
    else:
        parts.append("<p>A stage editor for Super Monkey Ball: Touch &amp; Roll (Nintendo DS, USA). It is mainly an "
                     "<b>object placement tool</b> with some useful stage tools, not 3D modelling software: "
                     "detailed shapes are built in a 3D program and imported as .obj files.</p>")
    parts.append("<p>Your ROM is only read. Every save writes a separate copy, checked against what you see in the "
                 "editor, and a .patch.json file that lists what changed.</p>")
    parts.append("<p style='color:#8a8f9c'>The code was written by Claude, an AI model made by Anthropic, under the "
                 "author's direction; every feature that changes the game was checked in-game before it was "
                 "unlocked.</p>")
    parts.append("<p style='color:#8a8f9c'>Released under the MIT License. Interface font: Inter, SIL Open Font "
                 "License 1.1. Super Monkey Ball and Touch &amp; Roll belong to SEGA; the editor contains no part of "
                 "the game and works on your own copy.</p>")
    return "\n".join(parts)


class AboutDialog(QtWidgets.QDialog):
    def __init__(self, name: str, version: str, icon: Optional[QtGui.QIcon] = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"About {name}")
        self.resize(520, 380)
        lay = QtWidgets.QHBoxLayout(self)
        if icon is not None and not icon.isNull():
            pic = QtWidgets.QLabel()
            pic.setPixmap(icon.pixmap(64, 64))
            pic.setAlignment(Qt.AlignTop)
            lay.addWidget(pic)
        right = QtWidgets.QVBoxLayout()
        self.text = QtWidgets.QTextBrowser()
        self.text.setOpenExternalLinks(True)
        self.text.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.text.setHtml(about_html(name, version))
        right.addWidget(self.text, 1)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        right.addWidget(buttons)
        lay.addLayout(right, 1)
