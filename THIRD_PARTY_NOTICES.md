# Third-party software

## In this repository

* **Inter** (the interface font, `tredit/fonts/`): © The Inter Project Authors, SIL Open Font License 1.1
  (`tredit/fonts/OFL.txt`).

## In the packaged program (`Touch & Roll Stage Editor.exe`)

The program is built with PyInstaller and carries these, each under its own licence:

| Component | Licence | Source |
|---|---|---|
| Python 3.12 | Python Software Foundation License | https://www.python.org/ |
| Qt for Python (PySide6, shiboken6) and Qt 6 | GNU LGPL v3 | https://pypi.org/project/PySide6/, https://code.qt.io/ |
| PyOpenGL | BSD-style | https://pypi.org/project/PyOpenGL/ |
| numpy (with its bundled OpenBLAS) | BSD 3-Clause | https://numpy.org/ |
| PyInstaller bootloader | GPL v2 with an exception that allows distributing programs built with it | https://pyinstaller.org/ |

Qt is used unmodified, through PySide6. To use the editor with a different or modified Qt, install that
PySide6 and run the editor from its Python files (`run_editor.bat`), or rebuild the program with
`build_exe.bat`. All of the editor's source is in this repository.

Super Monkey Ball and Touch & Roll are trademarks of SEGA. Nothing from the game is included.
