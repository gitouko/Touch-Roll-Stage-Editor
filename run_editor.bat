@echo off
setlocal EnableExtensions
cd /d "%~dp0"
rem Touch & Roll Stage Editor. Your ROM is opened read-only; saving always writes a separate copy.
rem Needs Python 3.10+ and the packages in tredit\requirements.txt; this script offers to install them.

py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" 2>nul
if errorlevel 1 (
  echo The editor needs Python 3.10 or newer ^(required by PySide6^).
  echo Your "py -3" is:
  py -3 --version
  echo Install a newer Python from https://www.python.org/downloads/ and run this again.
  pause
  exit /b 2
)

py -3 -c "import sys, PySide6; v = tuple(int(x) for x in PySide6.__version__.split('.')[:2]); sys.exit(0 if v >= (6, 5) else 1)" 2>nul
if errorlevel 1 (
  echo The editor needs PySide6 6.5 or newer ^(Qt for Python^), which is not installed yet or too old.
  choice /M "Install it now with pip (about 100 MB download)"
  if errorlevel 2 (
    echo Not installed. You can install it later with:  py -3 -m pip install --user --upgrade "PySide6>=6.5"
    pause
    exit /b 2
  )
  py -3 -m pip install --user --upgrade "PySide6>=6.5"
  if errorlevel 1 (
    echo pip could not install PySide6.
    pause
    exit /b 2
  )
)

py -3 -c "import OpenGL.GL, numpy" 2>nul
if errorlevel 1 (
  echo The 3D view needs PyOpenGL and numpy ^(about 20 MB^). The rest of the editor works without them.
  choice /M "Install them now with pip"
  if errorlevel 2 (
    echo Skipped. You can install them later with:  py -3 -m pip install --user PyOpenGL numpy
  ) else (
    py -3 -m pip install --user "PyOpenGL>=3.1" "numpy>=1.21"
    if errorlevel 1 echo pip could not install them; the 3D view will tell you what is missing.
  )
)

rem Start the editor without a console window (pyw), so only the editor and its icon show in the taskbar.
where pyw >nul 2>nul
if errorlevel 1 (
  py -3 -m tredit %*
  if errorlevel 1 pause
) else (
  start "" pyw -3 -m tredit %*
)
