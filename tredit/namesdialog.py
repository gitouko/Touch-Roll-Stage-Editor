"""World and stage names dialog (edit test 7): one world at a time, its name and its ten stage names,
with live checks, the original name beside every field and meters for the game's limited text space."""
from __future__ import annotations

from typing import Dict, Optional

from PySide6 import QtCore, QtWidgets

Qt = QtCore.Qt
OK_STYLE = ""
BAD_STYLE = "QLineEdit { border: 2px solid #d04040; }"
WARN_STYLE = "QLineEdit { border: 2px solid #d09a20; }"

INTRO = ("<b>Rename worlds and stages.</b> World names appear in the speech bubble on the world select. "
         "Stage names appear there too, and again when a stage starts. That second time the game uses a "
         "pixel font with <b>capitals only</b>, so stage names are typed in capitals for you. "
         "Changes apply to all five languages. Empty a field (or press ↺) to go back to the original.")


class NamesDialog(QtWidgets.QDialog):
    def __init__(self, project, parent=None, world: int = 1, show_warnings: bool = True):
        super().__init__(parent)
        self.project = project
        self.show_warnings = show_warnings
        self.setWindowTitle("World and stage names")
        self.resize(760, 560)
        self.edits: Dict[str, Dict[str, str]] = {"stages": dict(project.names["stages"]),
                                                 "worlds": dict(project.names["worlds"])}
        self.world = world
        lay = QtWidgets.QVBoxLayout(self)
        intro = QtWidgets.QLabel(INTRO)
        intro.setWordWrap(True)
        lay.addWidget(intro)
        body = QtWidgets.QHBoxLayout()
        self.world_list = QtWidgets.QListWidget()
        self.world_list.setMinimumWidth(260)
        self.world_list.setMaximumWidth(300)
        for w in range(1, 13):
            self.world_list.addItem(f"World {w}")
        self.world_list.currentRowChanged.connect(self._world_changed)
        body.addWidget(self.world_list)
        right = QtWidgets.QWidget()
        self.form = QtWidgets.QGridLayout(right)
        self.form.setColumnStretch(1, 1)
        hdr = [QtWidgets.QLabel("<b>Name</b>"), QtWidgets.QLabel("<b>Original</b>")]
        self.form.addWidget(hdr[0], 0, 1)
        self.form.addWidget(hdr[1], 0, 2)
        self.fields = {}
        rows = [("world", 0)] + [("stage", s) for s in range(1, 11)]
        for r, (kind, s) in enumerate(rows, start=1):
            lab = QtWidgets.QLabel("World name" if kind == "world" else f"Stage {s}")
            edit = QtWidgets.QLineEdit()
            orig = QtWidgets.QLabel()
            orig.setStyleSheet("color: gray;")
            reset = QtWidgets.QToolButton()
            reset.setText("↺")
            reset.setToolTip("Back to the original name")
            msg = QtWidgets.QLabel()
            msg.setWordWrap(True)
            msg.setStyleSheet("color: #b03030; font-size: 8pt;")
            self.form.addWidget(lab, 2 * r - 1, 0)
            self.form.addWidget(edit, 2 * r - 1, 1)
            self.form.addWidget(orig, 2 * r - 1, 2)
            self.form.addWidget(reset, 2 * r - 1, 3)
            self.form.addWidget(msg, 2 * r, 1, 1, 3)
            msg.hide()
            key = "world" if kind == "world" else f"stage{s}"
            self.fields[key] = (edit, orig, msg, kind, s)
            edit.textEdited.connect(lambda text, k=key: self._edited(k, text))
            reset.clicked.connect(lambda _c=False, k=key: self._reset(k))
            if kind == "world":
                line = QtWidgets.QFrame()
                line.setFrameShape(QtWidgets.QFrame.HLine)
        self.form.setRowStretch(2 * len(rows) + 1, 1)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(right)
        body.addWidget(scroll, 1)
        lay.addLayout(body, 1)
        meters = QtWidgets.QHBoxLayout()
        self.stage_meter = QtWidgets.QProgressBar()
        self.world_meter = QtWidgets.QProgressBar()
        for m, tip in ((self.stage_meter, "Space the game has for all 120 stage names"),
                       (self.world_meter, "Space the game has for the 12 world names")):
            m.setToolTip(tip)
            m.setTextVisible(True)
        meters.addWidget(QtWidgets.QLabel("Stage-name space:"))
        meters.addWidget(self.stage_meter, 1)
        meters.addWidget(QtWidgets.QLabel("World-name space:"))
        meters.addWidget(self.world_meter, 1)
        lay.addLayout(meters)
        self.summary = QtWidgets.QLabel()
        self.summary.setWordWrap(True)
        lay.addWidget(self.summary)
        self.buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        self.buttons.button(QtWidgets.QDialogButtonBox.Ok).setText("Apply names")
        self.buttons.accepted.connect(self._accept)
        self.buttons.rejected.connect(self.reject)
        lay.addWidget(self.buttons)
        self.error: Optional[str] = None
        self.world_list.setCurrentRow(world - 1)
        self._refresh_all()

    # ---- data
    def _key(self, kind, s):
        return str(self.world) if kind == "world" else f"{self.world}-{s}"

    def _bucket(self, kind):
        return self.edits["worlds" if kind == "world" else "stages"]

    def _original(self, kind, s):
        p = self.project
        return p.original_world_name(self.world) if kind == "world" else p.original_stage_name(self.world, s)

    def current(self, kind, s) -> str:
        return self._bucket(kind).get(self._key(kind, s)) or self._original(kind, s)

    # ---- events
    def _world_changed(self, row):
        if row < 0:
            return
        self.world = row + 1
        for key, (edit, orig, msg, kind, s) in self.fields.items():
            edit.setText(self.current(kind, s))
            orig.setText(self._original(kind, s))
            orig.setToolTip(f"Original: {self._original(kind, s)}")
        self._refresh_all()

    def _edited(self, key, text):
        edit, _orig, _msg, kind, s = self.fields[key]
        if kind == "stage" and text != text.upper():
            pos = edit.cursorPosition()
            text = text.upper()
            edit.setText(text)
            edit.setCursorPosition(pos)
        k = self._key(kind, s)
        if not text or text == self._original(kind, s):
            self._bucket(kind).pop(k, None)
        else:
            self._bucket(kind)[k] = text
        self._refresh_all()

    def _reset(self, key):
        edit, _orig, _msg, kind, s = self.fields[key]
        self._bucket(kind).pop(self._key(kind, s), None)
        edit.setText(self._original(kind, s))
        self._refresh_all()

    # ---- checks
    def problems(self):
        """[(key text, message)] for every name that cannot be used, across all worlds."""
        p = self.project
        out = []
        for k, v in self.edits["stages"].items():
            why = p.name_problem("stage", v)
            if why:
                out.append((f"Stage {k}", why))
        for k, v in self.edits["worlds"].items():
            why = p.name_problem("world", v)
            if why:
                out.append((f"World {k}", why))
        return out

    def _refresh_all(self):
        p = self.project
        for key, (edit, _orig, msg, kind, s) in self.fields.items():
            text = edit.text()
            why = p.name_problem(kind, text) if text else None
            warn = p.name_warning(kind, text) if text and not why and self.show_warnings else None
            edit.setStyleSheet(BAD_STYLE if why else WARN_STYLE if warn else OK_STYLE)
            changed = self._key(kind, s) in self._bucket(kind)
            f = edit.font()
            f.setBold(changed)
            edit.setFont(f)
            if why or warn:
                msg.setText(why or warn)
                msg.setStyleSheet("color: #b03030; font-size: 8pt;" if why else "color: #9a6a00; font-size: 8pt;")
                msg.show()
            else:
                msg.hide()
        for w in range(1, 13):
            n = sum(1 for k in self.edits["stages"] if k.startswith(f"{w}-")) + (str(w) in self.edits["worlds"])
            name = self.edits["worlds"].get(str(w)) or p.original_world_name(w)
            self.world_list.item(w - 1).setText(f"World {w}: {name}" + (f"  ({n} renamed)" if n else ""))
        sp = p.names_space(self.edits)
        if sp:
            for m, used, total in ((self.stage_meter, sp["stage_used"], sp["stage_bytes"]),
                                   (self.world_meter, sp["world_used"], sp["world_bytes"])):
                m.setMaximum(total)
                m.setValue(min(used, total))
                m.setFormat(f"{used} of {total} bytes" + ("  — too long!" if used > total else ""))
                m.setStyleSheet("QProgressBar::chunk { background: #d04040; }" if used > total else "")
        bad = self.problems()
        over = sp and (sp["stage_used"] > sp["stage_bytes"] or sp["world_used"] > sp["world_bytes"])
        nren = len(self.edits["stages"]) + len(self.edits["worlds"])
        if bad:
            self.summary.setText(f"<span style='color:#b03030'>Fix {len(bad)} name(s) first: "
                                 + "; ".join(f"{a}: {b}" for a, b in bad[:3]) + "</span>")
        elif over:
            self.summary.setText("<span style='color:#b03030'>The names no longer fit in the game's text space. "
                                 "Shorten some names.</span>")
        else:
            self.summary.setText(f"{nren} name(s) changed." if nren else "No names changed.")
        self.buttons.button(QtWidgets.QDialogButtonBox.Ok).setEnabled(not bad and not over)

    def _accept(self):
        if self.problems():
            return
        self.accept()

    def result_names(self) -> dict:
        return {"stages": dict(self.edits["stages"]), "worlds": dict(self.edits["worlds"])}
