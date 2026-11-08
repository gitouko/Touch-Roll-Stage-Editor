"""trcore — read-only research core for Super Monkey Ball: Touch & Roll stage data.

This package only READS game assets. It never modifies, recompresses or injects
files. Every layout label it produces is a research hypothesis with an evidence
ID (see docs/research_notes/stc_format_spec.md); none is confirmed in-game.

Requires Python 3.8+ and the standard library only.
"""

__version__ = "0.1.0"
TOOL_NAME = "trcore"
