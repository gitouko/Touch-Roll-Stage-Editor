# Changelog

## 1.0.0 (2026-11-08)

First release.

* **Objects:**
  * move, add, delete and duplicate the start, goals, bumpers, bananas and bunches;
  * turn goals and bumpers on all three axes;
  * red goals.
* **Stage tools:**
  * platforms, invisible floors and decorations, with any of the stage's own textures;
  * removing collision in an area;
  * widening a stage's drawing range.
* **New stages:** replace a stage with an empty one (one platform and the start), restorable at any time.
* **Models:** import .obj files from Blender, Wings3D and other programs. Each object is its own piece, with
  its materials and, if you like, its own UV map.
* **Moving parts:** a keyframe editor (curve types, length), with the collision following the animation.
* **Names:** rename worlds and stages on the world select.
* **Views:** a top view and a side view, and a 3D view with the stage's own textures and animations.
* **Help:**
  * a start page;
  * Edit > Settings for everything (40 settings);
  * the built-in guide (F1);
  * Help > Packages, which installs missing packages;
  * automatic backups of your edits.
* **Saving:**
  * every save writes a new ROM copy, checks it against the editor and writes a `.patch.json` record;
  * File > Import edits loads such a record again.

Goal and bumper sizes are shown but locked: the game ignores them for the finish line and the bounce.
