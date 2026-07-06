"""Mapa del circuito: trazado de la pista con la posición actual de los
pilotos seleccionados (punto + estela + código), sincronizado con el mismo
reloj que los gráficos.

El trazado lo provee la fuente (demo: sintético; replay: posiciones de la
vuelta más rápida). En vivo, el hub lo arma siguiendo a un auto en
movimiento hasta que cierra una vuelta; mientras tanto las estelas ya
dibujan la pista.
"""
from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QEvent, QPointF
from PySide6.QtGui import QBrush, QColor, QLinearGradient, QPen

from ..hub import DataHub
from . import theme
from .charts import series_pens

TRAIL_SEC = 5.0  # largo de la estela detrás de cada auto (en tiempo de sesión)


class TrackMapView(pg.PlotWidget):
    def __init__(self, hub: DataHub, parent=None):
        super().__init__(parent)
        self.hub = hub
        self.selected: list[str] = []
        self.trails: dict[str, pg.PlotDataItem] = {}
        self.labels: dict[str, pg.TextItem] = {}
        # posición mostrada por auto, con easing hacia la última muestra para
        # que los puntos se muevan fluido a 30 fps en vez de a saltos
        self._disp: dict[str, tuple[float, float]] = {}
        self._trails_enabled = True
        self._outline_len = -1
        self._corner_items: list[pg.TextItem] = []
        self._corner_count = -1

        self.setMenuEnabled(False)
        self.setAspectLocked(True)
        self.getPlotItem().hideAxis("left")
        self.getPlotItem().hideAxis("bottom")

        self.outline_curve = pg.PlotDataItem(pen=pg.mkPen("#3a3f4a", width=5))
        self.addItem(self.outline_curve)
        # tramo en amarillo mientras hay bandera amarilla en ese sector
        self.yellow_curve = pg.PlotDataItem(
            pen=pg.mkPen("#ffd12e", width=6), connect="finite"
        )
        self.yellow_curve.setZValue(1)
        self._yellow_sig = None
        self.addItem(self.yellow_curve)
        self.start_marker = pg.ScatterPlotItem(
            symbol="s", size=9, brush=pg.mkBrush(theme.TEXT), pen=pg.mkPen(None)
        )
        self.addItem(self.start_marker)
        self.dots = pg.ScatterPlotItem(size=11, pxMode=True)
        self.dots.setZValue(10)
        self.addItem(self.dots)

        # correlación con los gráficos: anillo en el punto de pista hovereado
        self.hover_dist_cb = None
        self.probe_marker = pg.ScatterPlotItem(
            symbol="o", size=16, pxMode=True,
            brush=pg.mkBrush(0, 0, 0, 0), pen=pg.mkPen(theme.ACCENT, width=2),
        )
        self.probe_marker.setZValue(20)
        self.probe_marker.setVisible(False)
        self.addItem(self.probe_marker)
        self._dist_map = None  # (dist, xs, ys) del trazado
        self._dist_map_len = -1
        self.scene().sigMouseMoved.connect(self._on_scene_move)
        self.viewport().installEventFilter(self)

        hub.driversChanged.connect(self._restyle)

    def eventFilter(self, obj, event) -> bool:
        if event.type() == QEvent.Leave:
            self.set_probe_dist(None)
            if self.hover_dist_cb is not None:
                self.hover_dist_cb(None)
        return False

    # ------------------------------------- correlación gráfico <-> mapa

    def _ensure_dist_map(self):
        """Mapeo distancia de vuelta -> (x, y): lo mantiene el hub (lo usan
        también los offsets de grilla de la vuelta 1)."""
        return self.hub.outline_dist_map()

    def set_probe_dist(self, dist: float | None) -> None:
        """Marca en el mapa el metro de vuelta hovereado en un gráfico."""
        mapping = self._ensure_dist_map() if dist is not None else None
        if dist is None or mapping is None:
            self.probe_marker.setVisible(False)
            return
        d_arr, xs, ys = mapping
        dist = min(max(float(dist), 0.0), float(d_arr[-1]))
        px = float(np.interp(dist, d_arr, xs))
        py = float(np.interp(dist, d_arr, ys))
        self.probe_marker.setData([px], [py])
        self.probe_marker.setVisible(True)

    def _on_scene_move(self, scene_pos) -> None:
        if self.hover_dist_cb is None:
            return
        vb = self.getPlotItem().vb
        mapping = self._ensure_dist_map()
        if mapping is None or not vb.sceneBoundingRect().contains(scene_pos):
            return
        p = vb.mapSceneToView(scene_pos)
        d_arr, xs, ys = mapping
        psx, psy = vb.viewPixelSize()
        psx = max(abs(psx), 1e-12)
        psy = max(abs(psy), 1e-12)
        d2 = ((xs - p.x()) / psx) ** 2 + ((ys - p.y()) / psy) ** 2
        i = int(np.argmin(d2))
        if float(d2[i]) ** 0.5 <= 25.0:  # a ≤25 px del trazado
            dist = float(d_arr[i])
            self.set_probe_dist(dist)
            self.hover_dist_cb(dist)
        else:
            self.set_probe_dist(None)
            self.hover_dist_cb(None)

    def set_trails_enabled(self, on: bool) -> None:
        self._trails_enabled = bool(on)
        if not on:
            for trail in self.trails.values():
                trail.setData([], [])

    def set_selected(self, drivers: list[str]) -> None:
        self.selected = list(drivers)
        for drv in list(self.trails):
            if drv not in drivers:
                self.removeItem(self.trails.pop(drv))
                self.removeItem(self.labels.pop(drv))
        for drv in drivers:
            if drv not in self.trails:
                trail = pg.PlotDataItem()
                self.addItem(trail)
                self.trails[drv] = trail
                label = pg.TextItem(
                    self._code_of(drv), color=theme.TEXT, anchor=(-0.25, 0.5),
                    fill=pg.mkBrush(theme.SURFACE + "c0"),
                )
                label.setZValue(11)
                label.setVisible(False)
                self.addItem(label, ignoreBounds=True)
                self.labels[drv] = label
        self._restyle()

    def clear_data(self) -> None:
        self._outline_len = -1
        self._disp.clear()
        for item in self._corner_items:
            self.removeItem(item)
        self._corner_items = []
        self._corner_count = -1
        self._dist_map = None
        self._dist_map_len = -1
        self.probe_marker.setVisible(False)
        self.yellow_curve.setData([], [])
        self._yellow_sig = None
        self.outline_curve.setData([], [])
        self.start_marker.setData([], [])
        self.dots.setData([])
        for trail in self.trails.values():
            trail.setData([], [])
        for label in self.labels.values():
            label.setVisible(False)

    def _update_yellow_sectors(self) -> None:
        """Pinta de amarillo los tramos con bandera amarilla activa ahora."""
        t = self.hub.latest_t
        active = tuple(
            (d0, d1) for t0, t1, d0, d1 in self.hub.sector_yellows if t0 <= t <= t1
        )
        if active == self._yellow_sig:
            return
        self._yellow_sig = active
        mapping = self._ensure_dist_map()
        if not active or mapping is None:
            self.yellow_curve.setData([], [])
            return
        d_arr, xs, ys = mapping
        mask = np.zeros(len(d_arr), dtype=bool)
        for d0, d1 in active:
            if d0 <= d1:
                mask |= (d_arr >= d0) & (d_arr <= d1)
            else:  # el sector cruza la línea de meta
                mask |= (d_arr >= d0) | (d_arr <= d1)
        self.yellow_curve.setData(xs, np.where(mask, ys, np.nan), connect="finite")

    def refresh(self) -> None:
        self._update_yellow_sectors()
        outline = self.hub.outline
        if outline is not None and len(outline[0]) != self._outline_len:
            self._outline_len = len(outline[0])
            self.outline_curve.setData(outline[0], outline[1])
            self.start_marker.setData([float(outline[0][0])], [float(outline[1][0])])
        if len(self.hub.corners) != self._corner_count:
            self._corner_count = len(self.hub.corners)
            for item in self._corner_items:
                self.removeItem(item)
            self._corner_items = []
            for label, _dist, cx, cy in self.hub.corners:
                item = pg.TextItem(label, color=theme.TEXT_MUTED, anchor=(0.5, 0.5))
                item.setPos(float(cx), float(cy))
                item.setZValue(5)
                self.addItem(item, ignoreBounds=True)
                self._corner_items.append(item)

        pens = series_pens(self.hub, self.selected)
        spots = []
        for drv in self.selected:
            pb = self.hub.positions.get(drv)
            trail = self.trails[drv]
            label = self.labels[drv]
            if pb is None or not len(pb):
                trail.setData([], [])
                label.setVisible(False)
                continue
            t = np.fromiter(pb.t, dtype=np.float64)
            x = np.fromiter(pb.x, dtype=np.float64)
            y = np.fromiter(pb.y, dtype=np.float64)
            info = self.hub.drivers.get(drv)
            color = info.color if info else "#9aa0a6"
            if self._trails_enabled:
                i0 = int(np.searchsorted(t, t[-1] - TRAIL_SEC))
                xs_t, ys_t = x[i0:], y[i0:]
                trail.setData(xs_t, ys_t)
                if len(xs_t) >= 2:
                    # degradado: transparente en la cola, pleno en el auto
                    grad = QLinearGradient(
                        QPointF(float(xs_t[0]), float(ys_t[0])),
                        QPointF(float(xs_t[-1]), float(ys_t[-1])),
                    )
                    tail = QColor(color)
                    tail.setAlpha(0)
                    head_c = QColor(color)
                    head_c.setAlpha(230)
                    grad.setColorAt(0.0, tail)
                    grad.setColorAt(1.0, head_c)
                    pen = QPen(QBrush(grad), 2.0)
                    pen.setCosmetic(True)
                    base = pens.get(drv)
                    if base is not None:
                        pen.setStyle(base.style())  # compañeros: trazo distinto
                    trail.setPen(pen)
            # easing hacia la última muestra: movimiento fluido entre lotes
            target = (float(x[-1]), float(y[-1]))
            prev = self._disp.get(drv, target)
            pos = (prev[0] + (target[0] - prev[0]) * 0.35,
                   prev[1] + (target[1] - prev[1]) * 0.35)
            self._disp[drv] = pos
            spots.append({
                "pos": pos,
                "brush": pg.mkBrush(color),
                "pen": pg.mkPen(theme.TEXT, width=1),
            })
            label.setText(self._code_of(drv))
            label.setPos(pos[0], pos[1])
            label.setVisible(True)
        self.dots.setData(spots)

    def _code_of(self, drv: str) -> str:
        info = self.hub.drivers.get(drv)
        return info.code if info else drv

    def _restyle(self) -> None:
        antialias = len(self.selected) <= 8
        for drv, pen in series_pens(self.hub, self.selected).items():
            trail = self.trails.get(drv)
            if trail is not None:
                pen = pg.mkPen(pen)
                pen.setWidthF(2.0)
                trail.opts["antialias"] = antialias
                trail.setPen(pen)
