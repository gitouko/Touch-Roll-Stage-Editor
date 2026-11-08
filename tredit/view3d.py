"""3D view of the stage (OpenGL through PySide6's QOpenGLWidget and PyOpenGL).

Draws the stage model with its own textures, the objects and the added geometry; orbit, pan and zoom;
click to select and drag on the floor to move; a time slider and play button pose the moving parts by
the stage's animation, with their collision overlay. If OpenGL or PyOpenGL is missing the tab shows how
to get it and the top and side views keep working.
"""
from __future__ import annotations

import math
from typing import Callable, Dict, Optional

from PySide6 import QtCore, QtWidgets

from .scene3d import OBJECT_COLOURS, StageScene, gl_matrix, object_matrix, object_mesh, shade_for

Qt = QtCore.Qt

try:
    from PySide6.QtOpenGLWidgets import QOpenGLWidget
except ImportError:                                  # pragma: no cover - very old PySide6
    QOpenGLWidget = None

GL_IMPORT_ERROR = None
try:
    import numpy as np
    from OpenGL import GL
except Exception as exc:                             # noqa: BLE001 - any import problem means "no 3D"
    GL = None
    np = None
    GL_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"

INSTALL_HINT = ("The 3D view needs PyOpenGL and numpy. Install them with\n\n"
                "    py -3 -m pip install --user PyOpenGL numpy\n\n"
                "and restart the editor. The top and side views work without them.")
BACKGROUND = (0.25, 0.30, 0.45)
FOV = 50.0
# set from Edit > Settings by the main window
VIEW = {"fov": FOV, "background": BACKGROUND, "orbit_speed": 1.0, "invert_orbit": False, "smooth": False}


def available() -> Optional[str]:
    """None if the 3D view can be used, otherwise why not."""
    if QOpenGLWidget is None:
        return "this PySide6 has no QOpenGLWidget"
    if GL is None:
        return f"PyOpenGL / numpy not available ({GL_IMPORT_ERROR})"
    return None


def _look_at(eye, target, up=(0.0, 1.0, 0.0)):
    f = [t - e for t, e in zip(target, eye)]
    fl = math.sqrt(sum(c * c for c in f)) or 1.0
    f = [c / fl for c in f]
    s = [f[1] * up[2] - f[2] * up[1], f[2] * up[0] - f[0] * up[2], f[0] * up[1] - f[1] * up[0]]
    sl = math.sqrt(sum(c * c for c in s)) or 1.0
    s = [c / sl for c in s]
    u = [s[1] * f[2] - s[2] * f[1], s[2] * f[0] - s[0] * f[2], s[0] * f[1] - s[1] * f[0]]
    return s, u, f


class Camera:
    """Orbit camera: target point, yaw (0 = looking towards -z), pitch (positive = from above), distance."""

    def __init__(self):
        self.target = [0.0, 0.0, 0.0]
        self.yaw = 0.0
        self.pitch = 30.0
        self.distance = 30.0

    def eye(self):
        y, p = math.radians(self.yaw), math.radians(self.pitch)
        fwd = (-math.sin(y) * math.cos(p), -math.sin(p), -math.cos(y) * math.cos(p))
        return [t - self.distance * c for t, c in zip(self.target, fwd)]

    def basis(self):
        return _look_at(self.eye(), self.target)

    def view_matrix(self):
        s, u, f = self.basis()
        e = self.eye()
        return [s[0], u[0], -f[0], 0.0, s[1], u[1], -f[1], 0.0, s[2], u[2], -f[2], 0.0,
                -sum(a * b for a, b in zip(s, e)), -sum(a * b for a, b in zip(u, e)),
                sum(a * b for a, b in zip(f, e)), 1.0]

    def project(self, p, w, h):
        """Screen (x, y, depth) of a world point, or None if behind the camera."""
        s, u, f = self.basis()
        e = self.eye()
        d = [p[i] - e[i] for i in range(3)]
        z = sum(a * b for a, b in zip(d, f))
        if z < 0.05:
            return None
        fl = (h / 2) / math.tan(math.radians(VIEW["fov"]) / 2)
        return (w / 2 + fl * sum(a * b for a, b in zip(d, s)) / z, h / 2 - fl * sum(a * b for a, b in zip(d, u)) / z, z)

    def ray(self, x, y, w, h):
        s, u, f = self.basis()
        fl = (h / 2) / math.tan(math.radians(VIEW["fov"]) / 2)
        dx, dy = (x - w / 2) / fl, -(y - h / 2) / fl
        d = [f[i] + dx * s[i] + dy * u[i] for i in range(3)]
        n = math.sqrt(sum(c * c for c in d))
        return self.eye(), [c / n for c in d]


def ray_plane_y(origin, direction, y):
    if abs(direction[1]) < 1e-6:
        return None
    t = (y - origin[1]) / direction[1]
    if t <= 0:
        return None
    return [origin[i] + t * direction[i] for i in range(3)]


class StageGL(QOpenGLWidget if QOpenGLWidget is not None else QtWidgets.QWidget):
    """The OpenGL canvas. The owner (View3D) feeds it a StageScene and callbacks."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMinimumSize(320, 240)
        self.scene: Optional[StageScene] = None
        self.camera = Camera()
        self.frame = 0.0
        self.show_collision = True
        self.selected: Optional[str] = None
        self.gl_error: Optional[str] = None
        self.gl_info = ""
        self.snap = 0.0
        self.on_select: Callable[[Optional[str]], None] = lambda oid: None
        self.on_move: Callable[[str, list], None] = lambda oid, pos: None
        self.on_geo_move: Callable[[str, list], None] = lambda gid, d: None
        self.on_error: Callable[[str], None] = lambda msg: None
        self._textures: Dict[int, int] = {}
        self._arrays: Dict[int, tuple] = {}
        self._coll_arrays = None
        self._pending_upload = False
        self._press = None
        self._drag = None              # (kind, id, plane y, start point, original position)
        self._ghost = None
        self._pose = {}

    # ------------------------------------------------------------ scene
    def set_scene(self, scene: Optional[StageScene], keep_camera=False):
        self.scene = scene
        self.frame = min(self.frame, (scene.frames - 1) if scene else 0)
        self._pose = scene.pose(self.frame) if scene else {}
        self._pending_upload = True
        if scene and not keep_camera:
            self.view_start()
        self.update()

    def set_frame(self, f: float):
        self.frame = f
        self._pose = self.scene.pose(f) if self.scene else {}
        self.update()

    def view_start(self):
        """Behind the start, looking the way it faces (as at the beginning of a try)."""
        sc = self.scene
        if not sc:
            return
        start = next((o for o in sc.objects if o["kind"] == "start"), None)
        lo, hi = sc.bounds
        if start is None:
            return self.view_fit()
        p = start["position"]
        self.camera.yaw = start.get("heading") or 0.0
        self.camera.pitch = 22.0
        self.camera.distance = 9.0
        a = math.radians(self.camera.yaw)
        self.camera.target = [p[0] - 3.0 * math.sin(a), p[1] + 0.5, p[2] - 3.0 * math.cos(a)]
        self.update()

    def view_fit(self):
        sc = self.scene
        if not sc:
            return
        lo, hi = sc.bounds
        self.camera.target = [(a + b) / 2 for a, b in zip(lo, hi)]
        size = max(hi[0] - lo[0], hi[2] - lo[2], hi[1] - lo[1], 4.0)
        self.camera.distance = size * 1.45
        self.camera.pitch = 45.0
        self.update()

    def view_top(self):
        self.view_fit()
        self.camera.pitch = 89.0
        start = next((o for o in (self.scene.objects if self.scene else []) if o["kind"] == "start"), None)
        self.camera.yaw = (start.get("heading") or 0.0) if start else 0.0
        self.update()

    # ------------------------------------------------------------ GL
    def initializeGL(self):
        try:
            self.gl_info = f"{GL.glGetString(GL.GL_RENDERER).decode(errors='replace')} / " \
                           f"OpenGL {GL.glGetString(GL.GL_VERSION).decode(errors='replace')}"
            GL.glEnable(GL.GL_DEPTH_TEST)
            GL.glClearColor(*VIEW["background"], 1.0)
            GL.glPixelStorei(GL.GL_UNPACK_ALIGNMENT, 1)
            GL.glAlphaFunc(GL.GL_GREATER, 0.4)
            GL.glBlendFunc(GL.GL_SRC_ALPHA, GL.GL_ONE_MINUS_SRC_ALPHA)
            GL.glFrontFace(GL.GL_CCW)
        except Exception as exc:                     # noqa: BLE001
            self.gl_error = f"OpenGL could not start: {exc}"
            self.on_error(self.gl_error)

    def _upload(self):
        for tid in self._textures.values():
            GL.glDeleteTextures([tid])
        self._textures, self._arrays, self._coll_arrays = {}, {}, None
        sc = self.scene
        self._pending_upload = False
        if not sc:
            return
        mirrored = getattr(GL, "GL_MIRRORED_REPEAT", 0x8370)
        for mi, mat in sc.materials.items():
            if not mat.image:
                continue
            w, h, px = mat.image
            tid = GL.glGenTextures(1)
            GL.glBindTexture(GL.GL_TEXTURE_2D, tid)
            filt = GL.GL_LINEAR if VIEW["smooth"] else GL.GL_NEAREST
            GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MIN_FILTER, filt)
            GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MAG_FILTER, filt)
            for pname, rep, flip in ((GL.GL_TEXTURE_WRAP_S, mat.repeat_s, mat.flip_s),
                                     (GL.GL_TEXTURE_WRAP_T, mat.repeat_t, mat.flip_t)):
                GL.glTexParameteri(GL.GL_TEXTURE_2D, pname,
                                   (mirrored if flip else GL.GL_REPEAT) if rep else GL.GL_CLAMP_TO_EDGE)
            GL.glTexImage2D(GL.GL_TEXTURE_2D, 0, GL.GL_RGBA, w, h, 0, GL.GL_RGBA, GL.GL_UNSIGNED_BYTE, px)
            self._textures[mi] = tid
        for i, b in enumerate(sc.batches):
            mat = sc.materials.get(b.material)
            textured = b.material in self._textures
            base = (1.0, 1.0, 1.0) if textured else (mat.colour if mat else (0.8, 0.8, 0.8))
            a = mat.alpha if mat else 1.0
            shades = np.asarray(b.shades, dtype=np.float32)
            cols = np.empty((b.count, 4), dtype=np.float32)
            for k in range(3):
                cols[:, k] = shades * base[k]
            cols[:, 3] = a
            self._arrays[i] = (np.asarray(b.positions, dtype=np.float32).reshape(-1, 3),
                               np.asarray(b.texcoords, dtype=np.float32).reshape(-1, 2), cols)
        groups: Dict[int, list] = {}
        for corners, normal, g in sc.faces:
            groups.setdefault(g, []).append((corners, normal))
        coll = {}
        for g, faces in groups.items():
            tris, cols, lines = [], [], []
            for corners, normal in faces:
                up = normal[1] if normal else 0.0
                c = (0.25, 0.95, 0.45) if up > 0.7 else (1.0, 0.55, 0.2) if up > -0.2 else (0.55, 0.55, 0.9)
                for k in range(1, len(corners) - 1):
                    for p in (corners[0], corners[k], corners[k + 1]):
                        tris.append(p)
                        cols.append(c + (0.22 if up > 0.7 else 0.14,))
                for k in range(len(corners)):
                    lines += [corners[k], corners[(k + 1) % len(corners)]]
            coll[g] = (np.asarray(tris, dtype=np.float32), np.asarray(cols, dtype=np.float32),
                       np.asarray(lines, dtype=np.float32))
        self._coll_arrays = coll

    def resizeGL(self, w, h):
        GL.glViewport(0, 0, w, h)

    def _projection(self):
        w, h = max(1, self.width()), max(1, self.height())
        sc = self.scene
        far = 4000.0
        if sc:
            lo, hi = sc.bounds
            far = max(500.0, 4 * max(abs(v) for v in lo + hi) + self.camera.distance * 4)
        near = max(0.05, min(1.0, self.camera.distance / 200))
        top = near * math.tan(math.radians(VIEW["fov"]) / 2)
        GL.glMatrixMode(GL.GL_PROJECTION)
        GL.glLoadIdentity()
        GL.glFrustum(-top * w / h, top * w / h, -top, top, near, far)
        GL.glMatrixMode(GL.GL_MODELVIEW)
        GL.glLoadMatrixf(self.camera.view_matrix())

    def paintGL(self):
        if self.gl_error or GL is None:
            return
        try:
            if self._pending_upload:
                self._upload()
            GL.glClearColor(*VIEW["background"], 1.0)
            GL.glClear(GL.GL_COLOR_BUFFER_BIT | GL.GL_DEPTH_BUFFER_BIT)
            if not self.scene:
                return
            self._projection()
            self._draw_model()
            self._draw_geometry()
            if self.show_collision:
                self._draw_collision()
            self._draw_objects()
        except Exception as exc:                     # noqa: BLE001
            self.gl_error = f"OpenGL drawing failed: {exc}"
            self.on_error(self.gl_error)

    def _draw_model(self):
        sc = self.scene
        GL.glEnableClientState(GL.GL_VERTEX_ARRAY)
        GL.glEnableClientState(GL.GL_COLOR_ARRAY)
        GL.glEnable(GL.GL_ALPHA_TEST)
        for alpha_pass in (False, True):
            GL.glAlphaFunc(GL.GL_GREATER, 0.02 if alpha_pass else 0.4)
            for i, b in enumerate(sc.batches):
                mat = sc.materials.get(b.material)
                translucent = bool(mat and mat.alpha < 0.999)
                if translucent != alpha_pass or i not in self._arrays:
                    continue
                pos, tc, cols = self._arrays[i]
                if translucent:
                    GL.glEnable(GL.GL_BLEND)
                    GL.glDepthMask(GL.GL_FALSE)
                if mat and mat.cull != "none":
                    GL.glEnable(GL.GL_CULL_FACE)
                    GL.glCullFace(GL.GL_BACK if mat.cull == "back" else GL.GL_FRONT)
                else:
                    GL.glDisable(GL.GL_CULL_FACE)
                tid = self._textures.get(b.material)
                if tid:
                    GL.glEnable(GL.GL_TEXTURE_2D)
                    GL.glBindTexture(GL.GL_TEXTURE_2D, tid)
                    GL.glTexEnvi(GL.GL_TEXTURE_ENV, GL.GL_TEXTURE_ENV_MODE, GL.GL_MODULATE)
                    GL.glEnableClientState(GL.GL_TEXTURE_COORD_ARRAY)
                    GL.glTexCoordPointer(2, GL.GL_FLOAT, 0, tc)
                else:
                    GL.glDisable(GL.GL_TEXTURE_2D)
                    GL.glDisableClientState(GL.GL_TEXTURE_COORD_ARRAY)
                GL.glPushMatrix()
                GL.glMultMatrixf(gl_matrix(sc.batch_matrix(b, self._pose)))
                GL.glVertexPointer(3, GL.GL_FLOAT, 0, pos)
                GL.glColorPointer(4, GL.GL_FLOAT, 0, cols)
                GL.glDrawArrays(GL.GL_TRIANGLES, 0, b.count)
                GL.glPopMatrix()
                if translucent:
                    GL.glDisable(GL.GL_BLEND)
                    GL.glDepthMask(GL.GL_TRUE)
        GL.glDisableClientState(GL.GL_TEXTURE_COORD_ARRAY)
        GL.glDisable(GL.GL_TEXTURE_2D)
        GL.glDisable(GL.GL_ALPHA_TEST)
        GL.glDisable(GL.GL_CULL_FACE)

    def _draw_collision(self):
        if not self._coll_arrays:
            return
        GL.glEnable(GL.GL_BLEND)
        GL.glDepthMask(GL.GL_FALSE)
        GL.glEnable(GL.GL_POLYGON_OFFSET_FILL)
        GL.glPolygonOffset(-1.0, -1.0)
        for g, (tris, cols, lines) in self._coll_arrays.items():
            d = self.scene.group_delta(g, self._pose) if g else None
            GL.glPushMatrix()
            if d:
                GL.glMultMatrixf(gl_matrix(d))
            if len(tris):
                GL.glEnableClientState(GL.GL_COLOR_ARRAY)
                GL.glVertexPointer(3, GL.GL_FLOAT, 0, tris)
                GL.glColorPointer(4, GL.GL_FLOAT, 0, cols)
                GL.glDrawArrays(GL.GL_TRIANGLES, 0, len(tris))
            if len(lines):
                GL.glDisableClientState(GL.GL_COLOR_ARRAY)
                GL.glColor4f(1.0, 1.0, 1.0, 0.35 if not g else 0.7)
                GL.glVertexPointer(3, GL.GL_FLOAT, 0, lines)
                GL.glDrawArrays(GL.GL_LINES, 0, len(lines))
            GL.glPopMatrix()
        GL.glEnableClientState(GL.GL_COLOR_ARRAY)
        GL.glDisable(GL.GL_POLYGON_OFFSET_FILL)
        GL.glDepthMask(GL.GL_TRUE)
        GL.glDisable(GL.GL_BLEND)

    def _draw_geometry(self):
        GL.glDisableClientState(GL.GL_COLOR_ARRAY)
        GL.glEnable(GL.GL_BLEND)
        for g in self.scene.geometry:
            sel = g["id"] == self.selected
            if self._drag and self._drag[1] == g["id"] and self._ghost is not None:
                dx, dz = self._ghost
            else:
                dx = dz = 0.0
            if g.get("turned") and g.get("polygons"):          # turned platform or imported model
                col = (0.24, 0.9, 0.35) if g.get("visible") and g.get("solid") else \
                    (0.35, 0.86, 1.0) if g.get("solid") else (0.98, 0.67, 0.24)
                if sel:
                    col = (1.0, 1.0, 1.0)
                GL.glColor4f(*col, 0.9 if sel else 0.7)
                GL.glLineWidth(2.0 if sel else 1.0)
                GL.glBegin(GL.GL_LINES)
                for poly in g["polygons"]:
                    for k in range(len(poly)):
                        a, b = poly[k], poly[(k + 1) % len(poly)]
                        GL.glVertex3f(a[0] + dx, a[1], a[2] + dz)
                        GL.glVertex3f(b[0] + dx, b[1], b[2] + dz)
                GL.glEnd()
                continue
            x0, x1 = g["x"][0] + dx, g["x"][1] + dx
            z0, z1 = g["z"][0] + dz, g["z"][1] + dz
            if g["type"] == "remove":
                y0, y1 = (g["y"] if g.get("y") else (-1.0, 3.0))
                col = (1.0, 0.3, 0.3)
            else:
                y1 = g["top"]
                y0 = y1 - max(g.get("thickness", 0.0), 0.0)
                col = (0.24, 0.9, 0.35) if g.get("visible") and g.get("solid") else \
                    (0.35, 0.86, 1.0) if g.get("solid") else (0.98, 0.67, 0.24)
            if sel:
                col = (1.0, 1.0, 1.0)
            GL.glColor4f(*col, 0.9 if sel else 0.75)
            GL.glLineWidth(2.0 if sel else 1.0)
            c = [(x0, y0, z0), (x1, y0, z0), (x1, y0, z1), (x0, y0, z1),
                 (x0, y1, z0), (x1, y1, z0), (x1, y1, z1), (x0, y1, z1)]
            edges = [(0, 1), (1, 2), (2, 3), (3, 0), (4, 5), (5, 6), (6, 7), (7, 4), (0, 4), (1, 5), (2, 6), (3, 7)]
            GL.glBegin(GL.GL_LINES)
            for a, b in edges:
                GL.glVertex3f(*c[a])
                GL.glVertex3f(*c[b])
            GL.glEnd()
            if g["type"] == "remove" or not g.get("visible"):
                GL.glColor4f(*col, 0.18)
                GL.glDepthMask(GL.GL_FALSE)
                GL.glBegin(GL.GL_QUADS)
                for q in ((4, 5, 6, 7), (0, 3, 2, 1)):
                    for k in q:
                        GL.glVertex3f(*c[k])
                GL.glEnd()
                GL.glDepthMask(GL.GL_TRUE)
        GL.glLineWidth(1.0)
        GL.glDisable(GL.GL_BLEND)
        GL.glEnableClientState(GL.GL_COLOR_ARRAY)

    def _draw_objects(self):
        GL.glDisableClientState(GL.GL_COLOR_ARRAY)
        GL.glDisableClientState(GL.GL_TEXTURE_COORD_ARRAY)
        sc = self.scene
        for o in sc.objects:
            pos = self._object_pos(o)
            col = OBJECT_COLOURS["goal_red" if o["kind"] == "goal" and o.get("red") else o["kind"]]
            tris = object_mesh(o)
            m = object_matrix(o, pos)
            GL.glPushMatrix()
            GL.glMultMatrixf(gl_matrix(m))
            GL.glBegin(GL.GL_TRIANGLES)
            for k in range(0, len(tris), 3):
                a, b, c = tris[k], tris[k + 1], tris[k + 2]
                n = _tri_normal(m, a, b, c)
                s = shade_for(n)
                GL.glColor4f(col[0] * s, col[1] * s, col[2] * s, 1.0)
                GL.glVertex3f(*a)
                GL.glVertex3f(*b)
                GL.glVertex3f(*c)
            GL.glEnd()
            if o["id"] == self.selected or o.get("changed"):
                GL.glColor4f(1.0, 1.0, 1.0, 1.0)
                GL.glLineWidth(2.5 if o["id"] == self.selected else 1.0)
                r = 0.9 if o["kind"] in ("goal",) else 0.6
                hgt = 2.0 if o["kind"] == "goal" else 0.9
                GL.glBegin(GL.GL_LINE_LOOP)
                for k in range(24):
                    a = 2 * math.pi * k / 24
                    GL.glVertex3f(r * math.cos(a) * (1.6 if o["kind"] == "goal" else 1.0), 0.03, r * math.sin(a))
                GL.glEnd()
                if o["id"] == self.selected:
                    GL.glBegin(GL.GL_LINES)
                    GL.glVertex3f(0.0, 0.0, 0.0)
                    GL.glVertex3f(0.0, hgt, 0.0)
                    GL.glEnd()
                GL.glLineWidth(1.0)
            GL.glPopMatrix()
        GL.glEnableClientState(GL.GL_COLOR_ARRAY)

    def _object_pos(self, o):
        if self._drag and self._drag[0] == "object" and self._drag[1] == o["id"] and self._ghost is not None:
            return self._ghost
        return self.scene.object_position(o, self._pose)

    # ------------------------------------------------------------ picking
    def pick(self, x, y) -> Optional[str]:
        sc = self.scene
        if not sc:
            return None
        w, h = self.width(), self.height()
        best = None
        for o in sc.objects:
            p = sc.object_position(o, self._pose)
            q = self.camera.project((p[0], p[1] + 0.25, p[2]), w, h)
            if q is None:
                continue
            d = math.hypot(q[0] - x, q[1] - y)
            if d < 16 and (best is None or (d, q[2]) < best[0]):
                best = ((d, q[2]), o["id"])
        if best:
            return best[1]
        origin, direction = self.camera.ray(x, y, w, h)
        hit = None
        for g in sc.geometry:
            if g.get("bounds"):
                gx, gy, gz = g["bounds"]
                yy = gy[1]
            else:
                gx, gz = g["x"], g["z"]
                yy = g["top"] if g["type"] == "platform" else (g["y"][1] if g.get("y") else 0.0)
            p = ray_plane_y(origin, direction, yy)
            if p and gx[0] <= p[0] <= gx[1] and gz[0] <= p[2] <= gz[1]:
                dist = sum((p[i] - origin[i]) ** 2 for i in range(3))
                if hit is None or dist < hit[0]:
                    hit = (dist, g["id"])
        return hit[1] if hit else None

    # ------------------------------------------------------------ mouse
    def mousePressEvent(self, ev):
        self.setFocus()
        pos = ev.position()
        self._press = (ev.button(), pos.x(), pos.y(), self.camera.yaw, self.camera.pitch, list(self.camera.target))
        self._drag = None
        self._ghost = None
        if ev.button() == Qt.LeftButton and self.scene:
            oid = self.pick(pos.x(), pos.y())
            if oid:
                self.on_select(oid)
                o = next((x for x in self.scene.objects if x["id"] == oid), None)
                if o is not None and "position" in o.get("editable", ()):
                    if not o.get("group"):
                        p = list(o["position"])
                        self._drag = ("object", oid, p[1], None, p)
                g = next((x for x in self.scene.geometry if x["id"] == oid), None)
                if g is not None:
                    yy = g["bounds"][1][1] if g.get("bounds") else \
                        g["top"] if g["type"] == "platform" else (g["y"][1] if g.get("y") else 0.0)
                    origin, direction = self.camera.ray(pos.x(), pos.y(), self.width(), self.height())
                    start = ray_plane_y(origin, direction, yy)
                    if start:
                        self._drag = ("geometry", oid, yy, start, None)
            else:
                self._press = self._press + ("orbit",)
                if ev.modifiers() & Qt.ControlModifier:
                    self.on_select(None)

    def mouseMoveEvent(self, ev):
        if not self._press:
            return
        pos = ev.position()
        btn, x0, y0, yaw0, pitch0, tgt0 = self._press[:6]
        dx, dy = pos.x() - x0, pos.y() - y0
        if self._drag and btn == Qt.LeftButton:
            origin, direction = self.camera.ray(pos.x(), pos.y(), self.width(), self.height())
            p = ray_plane_y(origin, direction, self._drag[2])
            if p:
                if self._drag[0] == "object":
                    q = [self._snap(p[0]), self._drag[2], self._snap(p[2])]
                    self._ghost = q
                else:
                    s = self._drag[3]
                    self._ghost = (self._snap(p[0] - s[0]), self._snap(p[2] - s[2]))
            self.update()
            return
        if btn == Qt.LeftButton:
            k = 0.4 * VIEW["orbit_speed"]
            self.camera.yaw = yaw0 - dx * k
            self.camera.pitch = max(-89.0, min(89.0, pitch0 + (-dy if VIEW["invert_orbit"] else dy) * k))
        elif btn in (Qt.RightButton, Qt.MiddleButton):
            s, u, _f = self.camera.basis()
            k = self.camera.distance / max(1, self.height()) * 1.2
            self.camera.target = [tgt0[i] - (dx * s[i] - dy * u[i]) * k for i in range(3)]
        self.update()

    def mouseReleaseEvent(self, ev):
        drag, ghost = self._drag, self._ghost
        self._press, self._drag, self._ghost = None, None, None
        if drag and ghost is not None:
            if drag[0] == "object" and any(abs(a - b) > 1e-6 for a, b in zip(ghost, drag[4])):
                self.on_move(drag[1], [ghost[0], drag[4][1], ghost[2]])
            elif drag[0] == "geometry" and (abs(ghost[0]) > 1e-6 or abs(ghost[1]) > 1e-6):
                self.on_geo_move(drag[1], [ghost[0], 0.0, ghost[1]])
        self.update()

    def wheelEvent(self, ev):
        k = 0.85 if ev.angleDelta().y() > 0 else 1 / 0.85
        self.camera.distance = max(0.5, min(5000.0, self.camera.distance * k))
        self.update()

    def keyPressEvent(self, ev):
        k = ev.key()
        if k == Qt.Key_F:
            self.view_fit()
        elif k == Qt.Key_Home:
            self.view_start()
        elif k == Qt.Key_T:
            self.view_top()
        else:
            super().keyPressEvent(ev)

    def _snap(self, v):
        return round(v / self.snap) * self.snap if self.snap else round(v * 1000) / 1000


def _tri_normal(m, a, b, c):
    r = m[0]

    def rot(v):
        return tuple(v[0] * r[0][j] + v[1] * r[1][j] + v[2] * r[2][j] for j in range(3))
    a, b, c = rot(a), rot(b), rot(c)
    u = [b[i] - a[i] for i in range(3)]
    v = [c[i] - a[i] for i in range(3)]
    n = (u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0])
    ln = math.sqrt(sum(x * x for x in n)) or 1.0
    return tuple(x / ln for x in n)


class View3D(QtWidgets.QWidget):
    """The 3D tab: a toolbar (camera buttons, animation controls) above the OpenGL canvas."""

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        bar = QtWidgets.QHBoxLayout()
        bar.setContentsMargins(4, 2, 4, 2)
        self.btn_start = QtWidgets.QPushButton("Start view")
        self.btn_fit = QtWidgets.QPushButton("Whole stage")
        self.btn_top = QtWidgets.QPushButton("From above")
        for b, tip in ((self.btn_start, "Behind the start, facing its direction (Home)"),
                       (self.btn_fit, "Fit the whole stage (F)"), (self.btn_top, "Look straight down (T)")):
            b.setToolTip(tip)
            bar.addWidget(b)
        bar.addSpacing(12)
        self.btn_keys = QtWidgets.QToolButton()
        self.btn_keys.setText("Keyframes…")
        self.btn_keys.setToolTip("Edit how the moving parts move (Ctrl+K)")
        self.btn_play = QtWidgets.QToolButton()
        self.btn_play.setText("▶ Play")
        self.btn_play.setCheckable(True)
        self.btn_play.setToolTip("Play the stage's animation (moving parts and their collision)")
        self.slider = QtWidgets.QSlider(Qt.Horizontal)
        self.slider.setMinimum(0)
        self.frame_label = QtWidgets.QLabel("")
        self.frame_label.setMinimumWidth(170)
        bar.addWidget(self.btn_play)
        bar.addWidget(self.slider, 1)
        bar.addWidget(self.frame_label)
        bar.addWidget(self.btn_keys)
        lay.addLayout(bar)
        self.stack = QtWidgets.QStackedWidget()
        why = available()
        self.message = QtWidgets.QLabel()
        self.message.setAlignment(Qt.AlignCenter)
        self.message.setWordWrap(True)
        self.message.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.gl: Optional[StageGL] = None
        if why is None:
            self.gl = StageGL()
            self.gl.on_error = self._gl_failed
            self.stack.addWidget(self.gl)
        else:
            self.message.setText(f"3D view unavailable: {why}\n\n{INSTALL_HINT}")
        page = QtWidgets.QWidget()
        pl = QtWidgets.QVBoxLayout(page)
        pl.addStretch(1)
        pl.addWidget(self.message)
        self.install_button = QtWidgets.QPushButton("Install PyOpenGL and numpy…")
        self.install_button.setToolTip("Help > Packages…: installs them with pip; restart the editor afterwards")
        from .packages import frozen
        self.install_button.setVisible(why is not None and GL is None and not frozen())
        self.install_button.clicked.connect(self._install_packages)
        row = QtWidgets.QHBoxLayout()
        row.addStretch(1)
        row.addWidget(self.install_button)
        row.addStretch(1)
        pl.addLayout(row)
        pl.addStretch(1)
        self.message_page = page
        self.stack.addWidget(page)
        lay.addWidget(self.stack, 1)
        hint = QtWidgets.QLabel("Left-drag: turn the view (or move what you clicked) · right-drag: pan · "
                                "wheel: zoom · Home / F / T: start view / whole stage / from above")
        hint.setStyleSheet("color: gray; font-size: 8pt;")
        lay.addWidget(hint)
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(16)
        self.timer.timeout.connect(self._tick)
        self.btn_play.toggled.connect(self._play_toggled)
        self.slider.valueChanged.connect(self._slider_moved)
        if self.gl:
            self.btn_start.clicked.connect(self.gl.view_start)
            self.btn_fit.clicked.connect(self.gl.view_fit)
            self.btn_top.clicked.connect(self.gl.view_top)
        self._elapsed = QtCore.QElapsedTimer()
        self._frame = 0.0
        self.on_frame: Callable[[float], None] = lambda f: None
        self.set_scene(None)

    # ---- wiring
    def _gl_failed(self, msg):
        self.message.setText(f"{msg}\n\nThe top and side views still work. Updating your graphics driver "
                             f"usually fixes this.")
        self.stack.setCurrentWidget(self.message_page)
        self.btn_play.setChecked(False)

    def set_callbacks(self, on_select, on_move, on_geo_move):
        if self.gl:
            self.gl.on_select, self.gl.on_move, self.gl.on_geo_move = on_select, on_move, on_geo_move

    def set_scene(self, scene: Optional[StageScene], keep_camera=False):
        frames = scene.frames if scene else 1
        animated = bool(scene and scene.anim and scene.animated_nodes)
        self.slider.blockSignals(True)
        self.slider.setMaximum(max(0, frames - 1))
        self._frame = min(self._frame, frames - 1)
        self.slider.setValue(int(self._frame))
        self.slider.blockSignals(False)
        for w in (self.btn_play, self.slider):
            w.setEnabled(animated)
        if self.gl:
            self.gl.set_scene(scene, keep_camera)
            self.gl.set_frame(self._frame)
            if scene and scene.errors:
                self.gl.setToolTip("\n".join(scene.errors))
        if not animated:
            self.btn_play.setChecked(False)
            self.frame_label.setText("no moving parts" if scene else "")
        else:
            self._update_label()

    def animation_changed(self):
        """The scene's animation was replaced (keyframe edit): update the slider and the pose."""
        sc = self.gl.scene if self.gl else None
        frames = sc.frames if sc else 1
        animated = bool(sc and sc.anim and sc.animated_nodes)
        self.slider.blockSignals(True)
        self.slider.setMaximum(max(0, frames - 1))
        self._frame = min(self._frame, max(0, frames - 1))
        self.slider.setValue(int(self._frame))
        self.slider.blockSignals(False)
        for w in (self.btn_play, self.slider):
            w.setEnabled(animated)
        if animated:
            self._update_label()
        if self.gl:
            self.gl.set_frame(self._frame)

    def set_frame(self, f: float):
        """Show frame f (from the keyframe editor)."""
        sc = self.gl.scene if self.gl else None
        n = sc.frames if sc else 1
        self._frame = max(0.0, min(float(f), n - 1))
        self.slider.blockSignals(True)
        self.slider.setValue(int(self._frame))
        self.slider.blockSignals(False)
        if self.gl:
            self.gl.set_frame(self._frame)
        if sc and sc.anim:
            self._update_label()

    @property
    def frame(self) -> float:
        return self._frame

    def set_selected(self, oid):
        if self.gl:
            self.gl.selected = oid
            self.gl.update()

    def set_show_collision(self, on):
        if self.gl:
            self.gl.show_collision = on
            self.gl.update()

    def set_snap(self, v):
        if self.gl:
            self.gl.snap = v

    def _install_packages(self):
        from .packages import PackagesDialog
        PackagesDialog(self, only=["PyOpenGL", "numpy"]).exec()

    def appearance_changed(self, reupload: bool = False):
        """Settings that change the look (field of view, background, texture smoothing)."""
        if self.gl:
            if reupload:
                self.gl._pending_upload = True
            self.gl.update()

    # ---- animation
    def _update_label(self):
        sc = self.gl.scene if self.gl else None
        n = sc.frames if sc else 1
        self.frame_label.setText(f"frame {int(self._frame)} / {n - 1}  ({self._frame / 60:.2f} s of {(n - 1) / 60:.2f})")

    def _play_toggled(self, on):
        self.btn_play.setText("❚❚ Pause" if on else "▶ Play")
        if on:
            self._elapsed.start()
            self.timer.start()
        else:
            self.timer.stop()

    def _tick(self):
        sc = self.gl.scene if self.gl else None
        if not sc or sc.frames <= 1:
            return
        dt = self._elapsed.restart() / 1000.0
        self._frame = (self._frame + dt * 60.0) % (sc.frames - 1)
        self.slider.blockSignals(True)
        self.slider.setValue(int(self._frame))
        self.slider.blockSignals(False)
        self.gl.set_frame(self._frame)
        self._update_label()
        self.on_frame(self._frame)

    def _slider_moved(self, v):
        self._frame = float(v)
        if self.gl:
            self.gl.set_frame(self._frame)
        self._update_label()
        self.on_frame(self._frame)

    def stop(self):
        self.btn_play.setChecked(False)
