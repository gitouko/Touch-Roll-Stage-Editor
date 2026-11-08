# Touch & Roll Stage Editor

A stage editor for **Super Monkey Ball: Touch & Roll** (Nintendo DS, USA version). Open your own copy of
the game, change its stages, and save the result as a new ROM to play in an emulator or on hardware.

It is mainly an **object placement tool**: you move, add, turn and delete the start, goals, bumpers and
bananas. On top of that come a few stage tools:

* platforms, invisible floors and decorations;
* removing collision;
* a 3D view with the stage's own textures;
* the moving parts and their keyframes;
* brand new empty stages;
* the world select's names;
* importing models made in Blender, Wings3D or any other 3D program as .obj files.

It is **not 3D modelling software**: detailed shapes are built in a real 3D program and imported.

Your ROM is only read. Every save writes a separate copy, checks it against what you see in the editor,
and writes a small `.patch.json` next to it that lists exactly what changed.

The full guide is inside the editor: **Help > Guide** (F1).

**This repository contains no part of the game.** You need your own ROM.

## Before anything else, how This Editor was made

I want to make this clear from the beginning instead of having people find out about it later. This Editor was genuinely vibe-coded with human-effort. AI was used to help write and develop the program, and I'm not going to hide that or pretend the entire codebase was handwritten by me. But at the same time, This Editor wasn't something I just thought of one afternoon and asked an AI to finish in a single prompt. I've spent the past few days playtesting every modification of the main game to see if it lines up, done by a REAL HUMAN, and fully understand the concept on how the game is breakdown. I just want people to know where the project came from and how much I wanted to see it become a real program, and possibly establish a major step in the Super Monkey Ball Community.

## Download

Get `Touch & Roll Stage Editor.exe` from the [latest release](../../releases/latest). It is one file and
carries everything it needs, so there's nothing to install. The program isn't code-signed, so Windows
SmartScreen may ask you to confirm the first time you start it (More info > Run anyway).

## What you need

* **Your own ROM** of Super Monkey Ball: Touch & Roll, USA version.
* **Windows 10 or 11.** The editor also runs on Linux and macOS from the Python files.

To run it from the Python files (`run_editor.bat`) instead of the .exe, you also need:

| What | Version | Used for | Needed? |
|---|---|---|---|
| [Python](https://www.python.org/downloads/) | 3.10 or newer | running the editor | yes |
| PySide6 (Qt for Python) | 6.5 or newer | the editor's windows | yes |
| PyOpenGL | 3.1 or newer | the 3D view | for the 3D view |
| numpy | 1.21 or newer | the 3D view | for the 3D view |
| [BizHawk](https://tasvideos.org/BizHawk) | 2.9 or newer | File > Play saved copy in BizHawk (F5) | no |

Without PyOpenGL and numpy, everything works except the 3D view, which then says what is missing.

## Installing the packages

You can let the editor do it, or do it yourself.

* **Let the editor do it.** Double-click `run_editor.bat`. It checks your Python and offers to install
  PySide6, then PyOpenGL and numpy, with pip.
  * Later, **Help > Packages…** in the editor shows what is installed and installs anything missing.
  * The 3D view has an **Install PyOpenGL and numpy…** button when they are missing.
  * Restart the editor after installing.
* **Do it yourself.** In a command prompt in this folder:

  ```
  py -3 -m pip install --user -r tredit/requirements.txt
  ```

  On Linux or macOS use `python3` instead of `py -3`.

pip downloads about 100 MB for PySide6 and about 20 MB for the 3D view's packages, and installs them for
your user only.

## Starting the editor

* Packaged program: double-click `Touch & Roll Stage Editor.exe`.
* From the Python files: double-click `run_editor.bat` (Windows), or run `python3 -m tredit` in this folder.

The start page has a big button to open your ROM, your recent ROMs and the guide.

## Making the packaged program yourself

Double-click `build_exe.bat`. It does three things:

1. installs PyInstaller and the packages above;
2. builds `dist\Touch & Roll Stage Editor.exe`;
3. checks that the program has its guide, fonts and 3D view.

If `tredit\icon.ico` is there (an icon made from your own ROM), the program uses it. Keep that icon to
yourself: it comes from the game, so it is never part of what is shared. The released program has no icon
of its own for that reason.

## What it can't do (yet)

* **Goal and bumper sizes are shown but locked.** The game draws a sized goal or bumper at its size, but its
  finish line and bounce ignore the size: a goal taller than about 1.2 can't be finished at all. So the
  editor keeps every goal and bumper at the size stored in the game.
* **Your own textures** (from image files) are planned for version 1.1. Until then, platforms and imported
  models use the stage's own textures.
* Only the **USA** version of the game is supported.

## How it was made

The format notes and in-game tests
that `trcore/README.md` mentions are there.

## Licence

MIT (see [LICENSE](LICENSE)). The interface font, Inter, is under the SIL Open Font License 1.1
(`tredit/fonts/OFL.txt`). The packaged program also contains Python, Qt for Python, PyOpenGL and numpy
under their own licences (see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)).

Super Monkey Ball and Touch & Roll are trademarks of SEGA. This project isn't affiliated with or endorsed by
SEGA or Nintendo.
