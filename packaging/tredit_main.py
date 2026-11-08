"""Start-up script of the packaged editor (PyInstaller builds "Touch & Roll Stage Editor.exe" from it)."""
import sys

from tredit.app import main

if __name__ == "__main__":
    sys.exit(main())
