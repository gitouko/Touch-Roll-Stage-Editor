# How this editor was made: AI disclosure

**The code in this repository was written by an AI.** Claude, a model made by Anthropic and used through
Claude Code, wrote the editor (`tredit/`), the file-format core (`trcore/`), the built-in guide, the build
scripts and this documentation.

The project's owner (GitHub: gitouko) directed the work:

* set the requirements and decided what the editor should and shouldn't do;
* play-tested every change to the game in an emulator (BizHawk);
* decided what shipped.

## How features were checked

The game's file formats were worked out by Claude, from the game's files and its code. A feature that
changes the game was **locked in the editor until an in-game test confirmed it**. For each test:

* Claude built a test ROM with the editor's own code;
* the owner played it and reported what happened;
* memory dumps from the emulator were compared with the edited files.

Before this release there were eleven such tests and two acceptance tests of earlier editor versions. There
was also a full functional test of every button, menu item, key and setting, and a final 3-stage acceptance
test.

That process caught a mistake before release. In the acceptance test, goals made bigger could not be
finished. Claude traced it to the game's own finish-line code, which ignores a goal's size, so goal and
bumper sizes are locked in 1.0.0 (see the README).

## What that means for you

* The editor can still have mistakes. Your ROM is only read, every save goes to a new copy, and each copy
  comes with a `.patch.json` listing exactly what changed, so nothing is lost if something goes wrong.
* Bug reports are welcome in this repository's Issues.
* No part of the game is included: no ROM, no game files, no textures and no icon from the game.
