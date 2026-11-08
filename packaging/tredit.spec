# PyInstaller spec: the editor as one Windows program, "Touch & Roll Stage Editor.exe".
# Build with build_exe.bat (or: py -3 -m PyInstaller --noconfirm --clean packaging/tredit.spec).
# The program carries the guide, the Inter font and, when present, about.txt and the icon made from your own
# ROM (tredit/icon.png / icon.ico, which are not in the repository).
import os

from PyInstaller.utils.hooks import collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
T = os.path.join(ROOT, "tredit")
datas = [(os.path.join(T, "guide"), "tredit/guide"), (os.path.join(T, "fonts"), "tredit/fonts")]
for name in ("about.txt", "icon.png", "icon.ico"):
    if os.path.isfile(os.path.join(T, name)):
        datas.append((os.path.join(T, name), "tredit"))
hidden = ["OpenGL.GL"] + collect_submodules("OpenGL.arrays") + collect_submodules("OpenGL.platform")
icon = os.path.join(T, "icon.ico")

a = Analysis([os.path.join(SPECPATH, "tredit_main.py")], pathex=[ROOT], datas=datas, hiddenimports=hidden,
             excludes=["tkinter", "matplotlib", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets",
                       "PySide6.QtQml", "PySide6.QtQuick", "PySide6.Qt3DCore", "PySide6.QtMultimedia"],
             noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, name="Touch & Roll Stage Editor", console=False,
          icon=icon if os.path.isfile(icon) else None, upx=False, version=None)
