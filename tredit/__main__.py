"""python -m tredit [ROM.nds]"""
import sys


def _run():
    try:
        import PySide6  # noqa: F401
    except ImportError:
        print("The editor needs PySide6 (Qt for Python). Install it once with:\n"
              "    py -3 -m pip install PySide6\n"
              "(Python 3.10 or newer is required by PySide6.)")
        return 2
    from .app import main
    return main(sys.argv)


sys.exit(_run())
