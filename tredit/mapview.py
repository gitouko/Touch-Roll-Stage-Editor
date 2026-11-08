"""2D map views: top-down (x right, -z up = the start's forward direction) and two side views.

The stage outline is the stage's own model read from the ROM (trcore.stagemodel),
drawn at the stage's rest pose. Object markers are drawn on top; dragging a
marker reports the new position to the main window, which commits it through
the project (and then re-syncs every view from the re-parsed stage bytes).
0.3: the stage's collision can be overlaid, and added geometry (platforms, invisible
floors, decorations, no-collision areas) is drawn as rectangles that can be dragged.
"""
from __future__ import annotations

import math
from typing import Callable, Dict, List, Optional

from PySide6 import QtCore, QtGui, QtWidgets

from trcore.nitro.nsbmd import triangle_normal

Qt = QtCore.Qt

# projection name -> (scene x from world, scene y from world, which world axes a drag may change, label)
PROJECTIONS = {
    "top": (lambda p: p[0], lambda p: p[2], (0, 2), "Top (x right, forward up)"),
    "right": (lambda p: -p[2], lambda p: -p[1], (1,), "Side from the right (forward to the right)"),
    "behind": (lambda p: p[0], lambda p: -p[1], (1,), "Side from behind (x right)"),
}

COLORS = {
    "start": QtGui.QColor(60, 220, 90),
    "goal": QtGui.QColor(70, 160, 255),
    "goal_red": QtGui.QColor(240, 60, 60),
    "bumper": QtGui.QColor(230, 70, 230),
    "banana": QtGui.QColor(255, 220, 40),
}
MARKER_PX = {"start": 9, "goal": 9, "bumper": 8, "banana": 5}
APPEARANCE = {"marker_scale": 1.0, "coll_fixed": QtGui.QColor(90, 230, 255), "coll_moving": QtGui.QColor(230, 150, 255)}


def marker_px(kind: str) -> float:
    return MARKER_PX[kind] * APPEARANCE["marker_scale"]


def to_scene(proj: str, p) -> QtCore.QPointF:
    fx, fy, _axes, _l = PROJECTIONS[proj]
    return QtCore.QPointF(fx(p), fy(p))


class OutlineItem(QtWidgets.QGraphicsItem):
    """All model triangles, painted far-to-near with height / facing shading."""

    def __init__(self, triangles, proj: str):
        super().__init__()
        self.proj = proj
        self.setCacheMode(QtWidgets.QGraphicsItem.DeviceCoordinateCache)
        self.setZValue(-10)
        self.polys: List[tuple] = []
        if not triangles:
            self._rect = QtCore.QRectF()
            return
        ys = [v[1] for t in triangles for v in t[:3]]
        y0, y1 = min(ys), max(ys)
        span = (y1 - y0) or 1.0
        if proj == "top":
            depth = lambda t: max(v[1] for v in t[:3])
            view = (0.0, 1.0, 0.0)
        elif proj == "right":
            depth = lambda t: max(v[0] for v in t[:3])
            view = (1.0, 0.0, 0.0)
        else:
            depth = lambda t: max(v[2] for v in t[:3])
            view = (0.0, 0.0, 1.0)
        for t in sorted(triangles, key=depth):
            n = triangle_normal(t)
            facing = abs(n[0] * view[0] + n[1] * view[1] + n[2] * view[2])
            h = (sum(v[1] for v in t[:3]) / 3 - y0) / span
            if proj == "top":
                if abs(n[1]) >= 0.3:           # floor / slope: lighter when higher
                    c = QtGui.QColor.fromRgbF(0.22 + 0.45 * h, 0.30 + 0.42 * h, 0.42 + 0.40 * h)
                else:                          # wall seen from above
                    c = QtGui.QColor(95, 70, 72)
            else:                              # side views: lit by how directly a face looks at the viewer
                k = 0.55 + 0.45 * facing
                c = QtGui.QColor.fromRgbF(min(1.0, (0.40 + 0.25 * h) * k), min(1.0, (0.46 + 0.22 * h) * k),
                                          min(1.0, (0.62 + 0.18 * h) * k))
            poly = QtGui.QPolygonF([to_scene(proj, v) for v in t[:3]])
            self.polys.append((poly, c))
        r = QtCore.QRectF()
        for poly, _c in self.polys:
            r = r.united(poly.boundingRect())
        self._rect = r.adjusted(-1, -1, 1, 1)

    def boundingRect(self):
        return self._rect

    def paint(self, painter, option, widget=None):
        edge = QtGui.QPen(QtGui.QColor(20, 24, 34, 160) if self.proj == "top" else QtGui.QColor(150, 165, 200, 200), 0)
        edge.setCosmetic(True)
        painter.setPen(edge)
        for poly, c in self.polys:
            painter.setBrush(c)
            painter.drawPolygon(poly)


class Marker(QtWidgets.QGraphicsItem):
    """One stage object. Fixed pixel size; draggable along the view's editable axes."""

    def __init__(self, view: "MapView", obj: dict):
        super().__init__()
        self.view = view
        self.obj = obj
        self.setFlag(QtWidgets.QGraphicsItem.ItemIgnoresTransformations, True)
        self.setFlag(QtWidgets.QGraphicsItem.ItemIsSelectable, True)
        movable = "position" in obj["editable"]
        self.setFlag(QtWidgets.QGraphicsItem.ItemIsMovable, movable)
        self.setFlag(QtWidgets.QGraphicsItem.ItemSendsGeometryChanges, True)
        self.setAcceptHoverEvents(True)
        self.setZValue({"banana": 1, "bumper": 2, "goal": 3, "start": 4}[obj["kind"]])
        self.dragging = False
        self.press_pos = None
        self.update_from(obj)

    def update_from(self, obj: dict):
        self.obj = obj
        self.setPos(to_scene(self.view.proj, obj["position"]))
        self.setToolTip(self.view.describe(obj))
        self.update()

    def refresh_size(self):
        """After Settings > Map > Size of object markers changed."""
        self.prepareGeometryChange()
        self.update()

    def boundingRect(self):
        r = marker_px(self.obj["kind"]) + 14
        return QtCore.QRectF(-r, -r, 2 * r, 2 * r)

    def _heading_vector(self):
        """Screen direction of the object's facing in this view (top view only)."""
        h = self.obj.get("heading")
        if h is None or self.view.proj != "top":
            return None
        a = math.radians(h)
        # facing (-sin a, 0, -cos a) in world; top view: screen x = x, screen y = z
        return QtCore.QPointF(-math.sin(a), -math.cos(a))

    def paint(self, painter, option, widget=None):
        o = self.obj
        k = o["kind"]
        r = marker_px(k)
        painter.setRenderHint(QtGui.QPainter.Antialiasing, True)
        col = COLORS["goal_red"] if (k == "goal" and o.get("red")) else COLORS[k]
        outline = QtGui.QPen(QtGui.QColor(15, 15, 20), 1.5)
        if o["in_moving_group"]:
            outline.setStyle(Qt.DashLine)
        painter.setPen(outline)
        painter.setBrush(col)
        hv = self._heading_vector()
        if k == "start":
            painter.drawEllipse(QtCore.QPointF(0, 0), r, r)
        elif k == "goal":
            if hv is not None:
                # bar across the facing direction
                px, py = -hv.y(), hv.x()
                bar = QtGui.QPolygonF([QtCore.QPointF(px * r * 1.4 + hv.x() * 3, py * r * 1.4 + hv.y() * 3),
                                       QtCore.QPointF(-px * r * 1.4 + hv.x() * 3, -py * r * 1.4 + hv.y() * 3),
                                       QtCore.QPointF(-px * r * 1.4 - hv.x() * 3, -py * r * 1.4 - hv.y() * 3),
                                       QtCore.QPointF(px * r * 1.4 - hv.x() * 3, py * r * 1.4 - hv.y() * 3)])
                painter.drawPolygon(bar)
            else:
                painter.drawRect(QtCore.QRectF(-r, -r * 0.6, 2 * r, 1.2 * r))
        elif k == "bumper":
            painter.drawEllipse(QtCore.QPointF(0, 0), r, r)
            painter.setBrush(QtGui.QColor(255, 255, 255, 160))
            painter.drawEllipse(QtCore.QPointF(0, 0), r * 0.4, r * 0.4)
        else:
            if o.get("bunch"):
                for dx, dy in ((-3, 2), (3, 2), (0, -3)):
                    painter.drawEllipse(QtCore.QPointF(dx, dy), r * 0.9, r * 0.9)
            else:
                painter.drawEllipse(QtCore.QPointF(0, 0), r, r)
        if hv is not None:
            pen = QtGui.QPen(QtGui.QColor(255, 255, 255), 2)
            painter.setPen(pen)
            tip = QtCore.QPointF(hv.x() * (r + 11), hv.y() * (r + 11))
            painter.drawLine(QtCore.QPointF(0, 0), tip)
            side = QtCore.QPointF(-hv.y() * 4, hv.x() * 4)
            back = QtCore.QPointF(hv.x() * (r + 5), hv.y() * (r + 5))
            painter.setBrush(QtGui.QColor(255, 255, 255))
            painter.drawPolygon(QtGui.QPolygonF([tip, back + side, back - side]))
        if o.get("collision_warning"):
            warn = QtGui.QPen(QtGui.QColor(255, 140, 0), 2.5, Qt.DashLine)
            painter.setPen(warn)
            painter.setBrush(Qt.NoBrush)
            painter.drawEllipse(QtCore.QPointF(0, 0), r + 4, r + 4)
        elif o.get("changed"):
            painter.setPen(QtGui.QPen(QtGui.QColor(255, 255, 255), 1.5))
            painter.setBrush(Qt.NoBrush)
            painter.drawEllipse(QtCore.QPointF(0, 0), r + 4, r + 4)
        if self.isSelected():
            pen = QtGui.QPen(QtGui.QColor(255, 160, 0), 2.5)
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            painter.drawEllipse(QtCore.QPointF(0, 0), r + 7, r + 7)

    def itemChange(self, change, value):
        if change == QtWidgets.QGraphicsItem.ItemPositionChange and self.dragging:
            return self.view.constrain(self, value)
        return super().itemChange(change, value)

    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            self.view.select_requested(self.obj["id"])
            self.press_pos = self.pos()
            self.dragging = bool(self.flags() & QtWidgets.QGraphicsItem.ItemIsMovable)
        super().mousePressEvent(ev)

    def mouseReleaseEvent(self, ev):
        super().mouseReleaseEvent(ev)
        if self.dragging:
            self.dragging = False
            if self.press_pos is not None and self.pos() != self.press_pos:
                self.view.drag_finished(self)


GEO_STYLE = {
    # title-independent look by (visible, solid) / remove
    "platform": (QtGui.QColor(90, 200, 110, 120), QtGui.QColor(60, 230, 90), Qt.SolidLine),
    "invisible": (QtGui.QColor(80, 200, 230, 50), QtGui.QColor(90, 220, 255), Qt.DashLine),
    "decoration": (QtGui.QColor(240, 160, 60, 70), QtGui.QColor(250, 170, 60), Qt.DotLine),
    "remove": (QtGui.QColor(230, 60, 60, 60), QtGui.QColor(255, 80, 80), Qt.DashLine),
}


def geo_style_key(g: dict) -> str:
    if g["type"] == "remove":
        return "remove"
    if g["visible"] and g["solid"]:
        return "platform"
    return "invisible" if g["solid"] else "decoration"


def geo_outline(proj: str, g: dict) -> Optional[QtGui.QPolygonF]:
    """Outline (convex hull of the corners) of a turned platform or an imported model in a projection."""
    if not g.get("turned") or not g.get("polygons"):
        return None
    pts = sorted({(round(q.x(), 4), round(q.y(), 4)) for poly in g["polygons"]
                  for q in (to_scene(proj, c) for c in poly)})
    if len(pts) < 3:
        return None

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])
    lower, upper = [], []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return QtGui.QPolygonF([QtCore.QPointF(x, y) for x, y in lower[:-1] + upper[:-1]])


def geo_rect(proj: str, g: dict) -> Optional[QtCore.QRectF]:
    """Scene rectangle of a geometry item in a projection (None if it has no extent there)."""
    outline = geo_outline(proj, g)
    if outline is not None:
        r = outline.boundingRect()
        if r.height() < 0.04:
            r.adjust(0, -0.02, 0, 0.02)
        if r.width() < 0.04:
            r.adjust(-0.02, 0, 0.02, 0)
        return r
    if proj == "top":
        return QtCore.QRectF(QtCore.QPointF(g["x"][0], g["z"][0]), QtCore.QPointF(g["x"][1], g["z"][1])).normalized()
    if g["type"] == "remove":
        if not g.get("y"):
            return None
        y0, y1 = g["y"]
    else:
        y0, y1 = g["top"] - g["thickness"], g["top"]
    if y1 - y0 < 0.02:
        y0, y1 = y1 - 0.02, y1 + 0.02          # a flat panel: a thin bar
    h0, h1 = (-g["z"][1], -g["z"][0]) if proj == "right" else (g["x"][0], g["x"][1])
    return QtCore.QRectF(QtCore.QPointF(h0, -y1), QtCore.QPointF(h1, -y0)).normalized()


class GeoItem(QtWidgets.QGraphicsItem):
    """One added geometry item. Top view: drag moves it in x/z; side views: drag changes its height."""

    def __init__(self, view: "MapView", g: dict, rect: QtCore.QRectF):
        super().__init__()
        self.view = view
        self.g = g
        self.rect = rect
        self.outline = geo_outline(view.proj, g)
        self.setFlag(QtWidgets.QGraphicsItem.ItemIsSelectable, True)
        self.setFlag(QtWidgets.QGraphicsItem.ItemIsMovable, True)
        self.setFlag(QtWidgets.QGraphicsItem.ItemSendsGeometryChanges, True)
        self.setZValue(0.5)
        self.dragging = False
        self.press_pos = None
        self.setToolTip(view.describe_geo(g))

    def boundingRect(self):
        return self.rect.adjusted(-0.5, -0.5, 0.5, 0.5)

    def paint(self, painter, option, widget=None):
        fill, line, style = GEO_STYLE[geo_style_key(self.g)]
        pen = QtGui.QPen(QtGui.QColor(255, 160, 0) if self.isSelected() else line, 3 if self.isSelected() else 2,
                         style)
        pen.setCosmetic(True)
        painter.setPen(pen)
        brush = QtGui.QBrush(fill, Qt.BDiagPattern if self.g["type"] == "remove" else Qt.SolidPattern)
        painter.setBrush(brush)
        if self.outline is not None:
            painter.drawPolygon(self.outline)
        else:
            painter.drawRect(self.rect)

    def itemChange(self, change, value):
        if change == QtWidgets.QGraphicsItem.ItemPositionChange and self.dragging:
            if self.view.proj == "top":
                return QtCore.QPointF(self.view._snap(value.x()), self.view._snap(value.y()))
            return QtCore.QPointF(0.0, self.view._snap(value.y()))
        return super().itemChange(change, value)

    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            self.view.select_requested(self.g["id"])
            self.press_pos = self.pos()
            self.dragging = True
        super().mousePressEvent(ev)

    def mouseReleaseEvent(self, ev):
        super().mouseReleaseEvent(ev)
        if self.dragging:
            self.dragging = False
            d = self.pos() - (self.press_pos or QtCore.QPointF())
            if abs(d.x()) > 1e-9 or abs(d.y()) > 1e-9:
                if self.view.proj == "top":
                    self.view.on_geo_move(self.g["id"], [d.x(), 0.0, d.y()])
                else:
                    self.view.on_geo_move(self.g["id"], [0.0, -d.y(), 0.0])


class CollisionOverlay(QtWidgets.QGraphicsItem):
    """The stage's collision faces as they now stand (moving parts at rest)."""

    def __init__(self, faces, proj: str):
        super().__init__()
        self.proj = proj
        self.setZValue(-5)
        self.setCacheMode(QtWidgets.QGraphicsItem.DeviceCoordinateCache)
        self.items = []
        r = QtCore.QRectF()
        for corners, normal, group in faces:
            poly = QtGui.QPolygonF([to_scene(proj, c) for c in corners])
            up = proj == "top" and normal[1] > 0.3
            self.items.append((poly, up, group))
            r = r.united(poly.boundingRect())
        self._rect = r.adjusted(-1, -1, 1, 1)

    def boundingRect(self):
        return self._rect

    def paint(self, painter, option, widget=None):
        for poly, up, group in self.items:
            col = QtGui.QColor(APPEARANCE["coll_fixed"] if group == 0 else APPEARANCE["coll_moving"])
            pen = QtGui.QPen(col, 0)
            pen.setCosmetic(True)
            painter.setPen(pen)
            if up:
                fill = QtGui.QColor(col)
                fill.setAlpha(45)
                painter.setBrush(fill)
            else:
                painter.setBrush(Qt.NoBrush)
            painter.drawPolygon(poly)


class MapView(QtWidgets.QGraphicsView):
    def __init__(self, proj: str, describe: Callable[[dict], str], parent=None):
        super().__init__(parent)
        self.proj = proj
        self.describe = describe
        self.setScene(QtWidgets.QGraphicsScene(self))
        self.setRenderHint(QtGui.QPainter.Antialiasing, False)
        self.setViewportUpdateMode(QtWidgets.QGraphicsView.SmartViewportUpdate)
        self.setTransformationAnchor(QtWidgets.QGraphicsView.AnchorUnderMouse)
        self.setDragMode(QtWidgets.QGraphicsView.NoDrag)
        self.setBackgroundBrush(QtGui.QColor(18, 20, 28))
        self.markers: Dict[str, Marker] = {}
        self.geo_items: Dict[str, GeoItem] = {}
        self.collision_item: Optional[CollisionOverlay] = None
        self.outline: Optional[OutlineItem] = None
        self.describe_geo: Callable[[dict], str] = lambda g: g.get("label", "")
        self.on_geo_move: Callable[[str, list], None] = lambda gid, d: None
        self.on_rect: Callable[[float, float, float, float], None] = lambda x0, z0, x1, z1: None
        self.rect_mode = False
        self._rubber = None
        self._rubber_start = None
        self.bounds_item = None
        self.snap = 0.0
        self.on_select: Callable[[Optional[str]], None] = lambda oid: None
        self.on_move: Callable[[str, list], None] = lambda oid, pos: None
        self.on_hover: Callable[[Optional[list]], None] = lambda p: None
        self.on_place: Callable[[float, float], None] = lambda x, z: None
        self.place_mode = False
        self.floor_height: Callable[[float, float, float], Optional[float]] = lambda x, z, y: None
        self.follow_floor = True
        self._pan = None
        self.show_grid = True
        self.setMouseTracking(True)

    # ---- content
    def set_stage(self, triangles, objects: List[dict], bounds=None):
        sc = self.scene()
        sc.clear()
        self.markers.clear()
        self.geo_items.clear()
        self.collision_item = None
        self._rubber = None
        self.outline = OutlineItem(triangles, self.proj) if triangles else None
        if self.outline:
            sc.addItem(self.outline)
        self.bounds_item = None
        if bounds:
            lo, hi = bounds
            a, b = to_scene(self.proj, lo), to_scene(self.proj, hi)
            pen = QtGui.QPen(QtGui.QColor(120, 120, 140), 0, Qt.DashLine)
            pen.setCosmetic(True)
            self.bounds_item = sc.addRect(QtCore.QRectF(a, b).normalized(), pen)
            self.bounds_item.setZValue(-20)
        for o in objects:
            m = Marker(self, o)
            sc.addItem(m)
            self.markers[o["id"]] = m
        # content = the outline, the bounds box and the objects' positions (markers have a fixed pixel size,
        # so their own bounding rectangles are not in scene units)
        rect = self.outline.boundingRect() if self.outline else QtCore.QRectF()
        if self.bounds_item is not None and bounds:
            rect = rect.united(self.bounds_item.rect())
        for m in self.markers.values():
            rect = rect.united(QtCore.QRectF(m.pos() - QtCore.QPointF(1, 1), QtCore.QSizeF(2, 2)))
        sc.setSceneRect(rect.adjusted(-rect.width() - 10, -rect.height() - 10, rect.width() + 10, rect.height() + 10))
        self._content_rect = rect.adjusted(-2, -2, 2, 2)
        self.fit()

    def update_objects(self, objects: List[dict]):
        for o in objects:
            m = self.markers.get(o["id"])
            if m is not None:
                m.update_from(o)

    def sync_objects(self, objects: List[dict]):
        """Add markers for new objects, drop markers of deleted ones, update the rest."""
        ids = {o["id"] for o in objects}
        for oid in [k for k in self.markers if k not in ids]:
            self.scene().removeItem(self.markers.pop(oid))
        for o in objects:
            m = self.markers.get(o["id"])
            if m is None:
                m = Marker(self, o)
                self.scene().addItem(m)
                self.markers[o["id"]] = m
            else:
                m.update_from(o)

    def sync_geometry(self, items: List[dict]):
        for gid in list(self.geo_items):
            self.scene().removeItem(self.geo_items.pop(gid))
        for g in items:
            r = geo_rect(self.proj, g)
            if r is None:
                continue
            it = GeoItem(self, g, r)
            self.scene().addItem(it)
            self.geo_items[g["id"]] = it
            cr = getattr(self, "_content_rect", None)
            if cr is not None and not cr.contains(r):          # geometry beyond the stage: keep it reachable
                self._content_rect = cr.united(r.adjusted(-1, -1, 1, 1))
                sr = self.scene().sceneRect()
                if not sr.contains(self._content_rect):
                    w, h = self._content_rect.width(), self._content_rect.height()
                    self.scene().setSceneRect(sr.united(self._content_rect.adjusted(-w, -h, w, h)))

    def set_collision(self, faces: Optional[list]):
        if self.collision_item is not None:
            self.scene().removeItem(self.collision_item)
            self.collision_item = None
        if faces:
            self.collision_item = CollisionOverlay(faces, self.proj)
            self.scene().addItem(self.collision_item)

    def set_rect_mode(self, on: bool):
        self.rect_mode = on
        self.viewport().setCursor(Qt.CrossCursor if on else Qt.ArrowCursor)

    def set_place_mode(self, on: bool):
        self.place_mode = on
        self.viewport().setCursor(Qt.CrossCursor if on else Qt.ArrowCursor)

    def set_selected(self, oid: Optional[str]):
        for k, m in self.markers.items():
            m.setSelected(k == oid)
            m.update()
        for k, g in self.geo_items.items():
            g.setSelected(k == oid)
            g.update()

    def fit(self):
        if getattr(self, "_content_rect", None) and not self._content_rect.isEmpty():
            self.resetTransform()
            self.fitInView(self._content_rect, Qt.KeepAspectRatio)

    # ---- dragging
    def select_requested(self, oid):
        self.on_select(oid)

    def _snap(self, v: float) -> float:
        return round(v / self.snap) * self.snap if self.snap else v

    def constrain(self, marker: Marker, value: QtCore.QPointF) -> QtCore.QPointF:
        start = to_scene(self.proj, marker.obj["position"])
        if self.proj == "top":
            return QtCore.QPointF(self._snap(value.x()), self._snap(value.y()))
        return QtCore.QPointF(start.x(), self._snap(value.y()))      # side views: height only

    def drag_finished(self, marker: Marker):
        p = list(marker.obj["position"])
        s = marker.pos()
        if self.proj == "top":
            nx, nz = s.x(), s.y()
            if self.follow_floor:
                f0 = self.floor_height(p[0], p[2], p[1])
                f1 = self.floor_height(nx, nz, p[1] + 3.0)
                if f0 is not None and f1 is not None:
                    p[1] = f1 + max(0.0, p[1] - f0)
            p[0], p[2] = nx, nz
        else:
            p[1] = -s.y()
        self.on_move(marker.obj["id"], p)

    # ---- navigation
    def wheelEvent(self, ev):
        f = 1.0015 ** ev.angleDelta().y()
        cur = self.transform().m11()
        if 0.5 < cur * f < 4000:
            self.scale(f, f)

    def mousePressEvent(self, ev):
        if ev.button() in (Qt.RightButton, Qt.MiddleButton):
            self._pan = ev.position()
            self.setCursor(Qt.ClosedHandCursor)
            return
        if self.place_mode and ev.button() == Qt.LeftButton and self.proj == "top":
            s = self.mapToScene(ev.position().toPoint())
            self.on_place(s.x(), s.y())
            return
        if self.rect_mode and ev.button() == Qt.LeftButton and self.proj == "top":
            s = self.mapToScene(ev.position().toPoint())
            self._rubber_start = QtCore.QPointF(self._snap(s.x()), self._snap(s.y()))
            pen = QtGui.QPen(QtGui.QColor(255, 200, 80), 0, Qt.DashLine)
            pen.setCosmetic(True)
            self._rubber = self.scene().addRect(QtCore.QRectF(self._rubber_start, self._rubber_start), pen)
            self._rubber.setZValue(10)
            return
        item = self.itemAt(ev.position().toPoint())
        if item is None or not isinstance(item, (Marker, GeoItem)):
            self.on_select(None)
        super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev):
        if self._pan is not None:
            d = ev.position() - self._pan
            self._pan = ev.position()
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - int(d.x()))
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - int(d.y()))
            return
        s = self.mapToScene(ev.position().toPoint())
        if self._rubber is not None and self._rubber_start is not None:
            cur = QtCore.QPointF(self._snap(s.x()), self._snap(s.y()))
            self._rubber.setRect(QtCore.QRectF(self._rubber_start, cur).normalized())
        self.on_hover([s.x(), s.y()])
        super().mouseMoveEvent(ev)

    def mouseReleaseEvent(self, ev):
        if self._pan is not None and ev.button() in (Qt.RightButton, Qt.MiddleButton):
            self._pan = None
            self.unsetCursor()
            return
        if self._rubber is not None and ev.button() == Qt.LeftButton:
            r = self._rubber.rect()
            self.scene().removeItem(self._rubber)
            self._rubber = None
            self._rubber_start = None
            self.finish_rect(r)
            return
        super().mouseReleaseEvent(ev)

    def finish_rect(self, r: QtCore.QRectF):
        """A rectangle was drawn (scene units = world x/z). A click without dragging gives a square of
        click_size (Settings; 4 by default)."""
        if r.width() < 0.25 or r.height() < 0.25:
            c = r.center()
            k = float(getattr(self, "click_size", 4.0))
            r = QtCore.QRectF(c.x() - k / 2, c.y() - k / 2, k, k)
        self.on_rect(r.left(), r.top(), r.right(), r.bottom())

    def drawBackground(self, painter, rect):
        super().drawBackground(painter, rect)
        if not self.show_grid:
            return
        scale = self.transform().m11()
        for step, alpha in ((1.0, 28), (10.0, 60)):
            if step * scale < 6:
                continue
            pen = QtGui.QPen(QtGui.QColor(150, 160, 190, alpha), 0)
            pen.setCosmetic(True)
            painter.setPen(pen)
            x = math.floor(rect.left() / step) * step
            while x <= rect.right():
                painter.drawLine(QtCore.QLineF(x, rect.top(), x, rect.bottom()))
                x += step
            y = math.floor(rect.top() / step) * step
            while y <= rect.bottom():
                painter.drawLine(QtCore.QLineF(rect.left(), y, rect.right(), y))
                y += step
        pen = QtGui.QPen(QtGui.QColor(200, 120, 120, 90), 0)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.drawLine(QtCore.QLineF(rect.left(), 0, rect.right(), 0))
        painter.drawLine(QtCore.QLineF(0, rect.top(), 0, rect.bottom()))
