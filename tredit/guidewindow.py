"""Help > Guide: the editor's built-in guide (pages in tredit/guide/, listed in index.json)."""
from __future__ import annotations

import json
import os
import re
import unicodedata
from typing import List, Tuple

from PySide6 import QtCore, QtGui, QtWidgets

Qt = QtCore.Qt
GUIDE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "guide")
STYLE = """
body { font-size: 10.5pt; line-height: 140%; }
h1 { font-size: 17pt; margin-bottom: 6px; }
h2 { font-size: 12.5pt; margin-top: 16px; margin-bottom: 4px; }
p { margin-top: 4px; margin-bottom: 8px; }
code { font-family: Consolas, monospace; }
th { border-bottom: 1px solid #777; }
td { border-bottom: 1px solid #444; }
"""


def guide_pages() -> List[Tuple[str, str, str]]:
    """(file, title, html) of every page, in order."""
    try:
        with open(os.path.join(GUIDE_DIR, "index.json"), encoding="utf-8") as fh:
            index = json.load(fh)
    except (OSError, ValueError):
        return []
    out = []
    for name, title in index:
        try:
            with open(os.path.join(GUIDE_DIR, name), encoding="utf-8") as fh:
                out.append((name, title, fh.read()))
        except OSError:
            continue
    return out


def _fold(text: str) -> str:
    """Lower case without accents, so "bezier" finds "Bézier"."""
    return "".join(c for c in unicodedata.normalize("NFKD", text.lower()) if not unicodedata.combining(c))


def _plain(html: str) -> str:
    return _fold(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).replace("&amp;", "&"))


class GuideWindow(QtWidgets.QWidget):
    """A topic list with a search box on the left, the page on the right. Stays open next to the editor."""

    def __init__(self, title: str, parent=None):
        super().__init__(parent, Qt.Window)
        self.setWindowTitle(f"{title} — Guide")
        self.resize(980, 720)
        self.pages = guide_pages()
        lay = QtWidgets.QHBoxLayout(self)
        split = QtWidgets.QSplitter(Qt.Horizontal)
        lay.addWidget(split)
        left = QtWidgets.QWidget()
        ll = QtWidgets.QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText("Search the guide…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._filter)
        ll.addWidget(self.search)
        self.topics = QtWidgets.QListWidget()
        for name, title_, _html in self.pages:
            it = QtWidgets.QListWidgetItem(title_)
            it.setData(Qt.UserRole, name)
            self.topics.addItem(it)
        self.topics.currentItemChanged.connect(self._show)
        ll.addWidget(self.topics)
        split.addWidget(left)
        self.text = QtWidgets.QTextBrowser()
        self.text.setOpenLinks(False)
        self.text.anchorClicked.connect(self._link)
        self.text.document().setDefaultStyleSheet(STYLE)
        self.text.document().setDocumentMargin(18)
        split.addWidget(self.text)
        split.setSizes([230, 750])
        if self.pages:
            self.topics.setCurrentRow(0)
        else:
            self.text.setHtml("<p>The guide's pages were not found next to the editor.</p>")

    def _show(self, cur, _prev=None):
        if cur is None:
            return
        name = cur.data(Qt.UserRole)
        for n, _t, html in self.pages:
            if n == name:
                self.text.setHtml(html)
                words = self.search.text().strip()
                if words:
                    self.text.find(words.split()[0])
                break

    def _filter(self, text: str):
        words = _fold(text).split()
        first = None
        for row in range(self.topics.count()):
            it = self.topics.item(row)
            html = next(h for n, _t, h in self.pages if n == it.data(Qt.UserRole))
            hit = all(w in _plain(html) or w in _fold(it.text()) for w in words)
            it.setHidden(not hit)
            if hit and first is None:
                first = it
        cur = self.topics.currentItem()
        if first is not None and (cur is None or cur.isHidden()):
            self.topics.setCurrentItem(first)
        elif cur is not None:
            self._show(cur)

    def _link(self, url: QtCore.QUrl):
        target = url.toString()
        for row in range(self.topics.count()):
            if self.topics.item(row).data(Qt.UserRole) == target:
                self.topics.setCurrentRow(row)
                return
        QtGui.QDesktopServices.openUrl(url)

    def show_page(self, name: str):
        for row in range(self.topics.count()):
            if self.topics.item(row).data(Qt.UserRole) == name:
                self.topics.setCurrentRow(row)
                break
        self.show()
        self.raise_()
        self.activateWindow()
