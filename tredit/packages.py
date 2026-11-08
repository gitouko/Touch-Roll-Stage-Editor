"""The Python packages the editor uses, and Help > Packages…, which shows them and installs missing ones.

The editor itself needs PySide6. The 3D view also needs PyOpenGL and numpy; without them everything else
still works. The packaged program (Touch & Roll Stage Editor.exe) carries all of them, so there is nothing
to install there.
"""
from __future__ import annotations

import importlib
import importlib.metadata
import importlib.util
import sys
from typing import List, Optional, Tuple

from PySide6 import QtCore, QtWidgets

Qt = QtCore.Qt

# (import name, distribution name, pip requirement, what it is for, required?)
PACKAGES: List[Tuple[str, str, str, str, bool]] = [
    ("PySide6", "PySide6", "PySide6>=6.5", "the editor's windows (Qt for Python)", True),
    ("OpenGL", "PyOpenGL", "PyOpenGL>=3.1", "the 3D view", False),
    ("numpy", "numpy", "numpy>=1.21", "the 3D view", False),
]
MIN_PYTHON = (3, 10)


def frozen() -> bool:
    """True in the packaged program, which carries its packages."""
    return bool(getattr(sys, "frozen", False))


def status() -> List[dict]:
    """[{module, dist, requirement, purpose, required, version or None}] for every package."""
    out = []
    for mod, dist, req, purpose, required in PACKAGES:
        version = None
        if importlib.util.find_spec(mod) is not None:
            try:
                version = importlib.metadata.version(dist)
            except importlib.metadata.PackageNotFoundError:
                version = "installed"
        out.append({"module": mod, "dist": dist, "requirement": req, "purpose": purpose, "required": required,
                    "version": version})
    return out


def missing(optional: bool = True) -> List[str]:
    """pip requirements of the packages that are not installed."""
    return [s["requirement"] for s in status() if s["version"] is None and (optional or s["required"])]


def python_exe() -> str:
    """The console Python next to the running one (pythonw.exe has no output for pip to write to)."""
    exe = sys.executable
    if exe.lower().endswith("pythonw.exe"):
        exe = exe[:-len("pythonw.exe")] + "python.exe"
    return exe


def pip_command(reqs: List[str]) -> Optional[List[str]]:
    """The command that installs `reqs` for this Python (for the current user), or None when there is
    nothing to install or the editor is the packaged program."""
    if frozen() or not reqs:
        return None
    return [python_exe(), "-m", "pip", "install", "--user", "--upgrade", *reqs]


class PackagesDialog(QtWidgets.QDialog):
    """Lists the packages with their versions; Install runs pip for the missing ones and shows its output."""
    installed = QtCore.Signal(bool)

    def __init__(self, parent=None, only: Optional[List[str]] = None):
        super().__init__(parent)
        self.setWindowTitle("Packages")
        self.resize(640, 420)
        self.only = only
        lay = QtWidgets.QVBoxLayout(self)
        py = ".".join(str(v) for v in sys.version_info[:3])
        intro = QtWidgets.QLabel(
            "The packaged program carries everything it needs." if frozen() else
            f"The editor runs on Python {py} ({'fine' if sys.version_info >= MIN_PYTHON else 'too old: 3.10 or newer'}"
            f"). These are the packages it uses:")
        intro.setWordWrap(True)
        lay.addWidget(intro)
        self.table = QtWidgets.QTreeWidget()
        self.table.setHeaderLabels(["Package", "Used for", "Installed"])
        self.table.setRootIsDecorated(False)
        lay.addWidget(self.table)
        self.log = QtWidgets.QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setPlaceholderText("pip's messages appear here while installing.")
        lay.addWidget(self.log, 1)
        self.note = QtWidgets.QLabel("")
        self.note.setWordWrap(True)
        lay.addWidget(self.note)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Close)
        self.install_button = buttons.addButton("Install missing packages", QtWidgets.QDialogButtonBox.ActionRole)
        self.install_button.clicked.connect(self.install)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        lay.addWidget(buttons)
        self.proc: Optional[QtCore.QProcess] = None
        self.refresh()

    def _wanted(self) -> List[str]:
        reqs = missing()
        return [r for r in reqs if self.only is None or any(r.startswith(o) for o in self.only)]

    def refresh(self):
        self.table.clear()
        for s in status():
            it = QtWidgets.QTreeWidgetItem([s["dist"], s["purpose"] + ("" if s["required"] else " (optional)"),
                                            s["version"] or "missing"])
            self.table.addTopLevelItem(it)
        for i in range(3):
            self.table.resizeColumnToContents(i)
        want = self._wanted()
        cmd = pip_command(want)
        self.install_button.setEnabled(cmd is not None and self.proc is None)
        if frozen():
            self.note.setText("")
        elif want:
            self.note.setText("Install downloads them from the Python Package Index with pip, for your user only "
                              "(about 20 MB for the 3D view, about 100 MB for PySide6). The same command by "
                              "hand:  " + " ".join(["py -3"] + cmd[1:]))
        else:
            self.note.setText("Everything is installed.")

    def install(self):
        cmd = pip_command(self._wanted())
        if cmd is None:
            return
        self.log.appendPlainText("> " + " ".join(cmd))
        self.proc = QtCore.QProcess(self)
        self.proc.setProcessChannelMode(QtCore.QProcess.MergedChannels)
        self.proc.readyReadStandardOutput.connect(
            lambda: self.log.appendPlainText(bytes(self.proc.readAllStandardOutput()).decode(errors="replace").rstrip()))
        self.proc.finished.connect(self._finished)
        self.proc.errorOccurred.connect(lambda _e: self._finished(-1, None))
        self.install_button.setEnabled(False)
        self.proc.start(cmd[0], cmd[1:])

    def _finished(self, code, _status):
        ok = code == 0
        self.proc = None
        importlib.invalidate_caches()
        self.refresh()
        by_hand = " ".join(["py -3", "-m", "pip", "install", "--user", *self._wanted()])
        self.note.setText("Installed. Restart the editor to use them." if ok else
                          "pip could not install them; its messages above say why. In a command prompt the same "
                          f"command is:  {by_hand}")
        self.installed.emit(ok)
