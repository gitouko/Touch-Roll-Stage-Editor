@echo off
rem Builds the editor as one program: dist\Touch ^& Roll Stage Editor.exe
rem Needs Python 3.10+ (the "py" launcher). Your ROM is not needed and nothing from it is packed,
rem except the icon if you made tredit\icon.ico from your own ROM.
setlocal
cd /d "%~dp0"
echo Installing the build tools (PyInstaller, PySide6, PyOpenGL, numpy)...
py -3 -m pip install --user --upgrade pyinstaller PySide6 PyOpenGL numpy || goto :fail
echo Building...
py -3 -m PyInstaller --noconfirm --clean packaging\tredit.spec || goto :fail
echo Checking the program...
start "" /wait "dist\Touch & Roll Stage Editor.exe" --self-test
if errorlevel 1 goto :fail
echo.
echo Done: dist\Touch ^& Roll Stage Editor.exe
pause
exit /b 0
:fail
echo.
echo The build did not finish. The messages above say why.
pause
exit /b 1
