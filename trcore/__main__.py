"""Allows ``py -3 -m trcore ...``."""
import sys

from .cli import main

sys.exit(main())
