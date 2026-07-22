"""Editor de microsectores: mover, agregar y quitar cortes, siempre
representados en datos (tabla) y en el mapa a la vez. Los límites de sector
son oficiales y no se editan; la cantidad de µ por sector es libre.

La config persiste por circuito+año (clave `hub.circuit_key()`, sección
"microsectors" de config.json): se carga sola en cualquier tanda del mismo
fin de semana. Sin config propia rigen los cortes automáticos (8 por
sector, esquivando frenajes y curvas); el primer retoque parte de ellos.
"""
from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QPushButton, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from .. import config
from ..hub import DataHub
from ..timing import TimingAnalyzer
from . import theme

GRAB_PX = 18.0   # radio de agarre de un corte en el mapa (píxeles)
MIN_GAP = 15.0   # separación mínima en metros entre cortes


class _MapCanvas(pg.PlotWidget):
    """Mapa del circuito con los cortes arrastrables sobre el trazado."""

    def __init__(self, view: "MicroConfigView"):
        super().__init__()
        self.view = view
        self._drag: int | None = None
        self.setMenuEnabled(False)
        self.setAspectLocked(True)
        self.getPlotItem().hideAxis("left")
        self.getPlotItem().hideAxis("bottom")
        self.outline_curve = pg.PlotDataItem(pen=pg.mkPen("#3a3f4a", width=4))
        self.addItem(self.outline_curve)
        # límites de sector: cuadrados acento, NO editables
        self.bound_dots = pg.ScatterPlotItem(
            symbol="s", size=11, pxMode=True,
            brush=pg.mkBrush(theme.ACCENT), pen=pg.mkPen(None))
        self.bound_dots.setZValue(9)
        self.addItem(self.bound_dots)
        self.cut_dots = pg.ScatterPlotItem(
            symbol="o", size=9, pxMode=True,
            brush=pg.mkBrush(theme.TEXT), pen=pg.mkPen("#111318", width=1))
        self.cut_dots.setZValue(10)
        self.addItem(self.cut_dots)
        self.sel_dot = pg.ScatterPlotItem(
            symbol="o", size=16, pxMode=True, brush=pg.mkBrush(0, 0, 0, 0),
            pen=pg.mkPen(theme.ACCENT, width=2))
        self.sel_dot.setZValue(11)
        self.sel_dot.setVisible(False)
        self.addItem(self.sel_dot)
        self._labels: list[pg.TextItem] = []

    # ------------------------------------------------------------ geometría

    def _view_pos(self, pos):
        vb = self.getPlotItem().vb
        sp = self.mapToScene(pos.toPoint())
        if not vb.sceneBoundingRect().contains(sp):
            return None
        return vb.mapSceneToView(sp)

    def _px_dist(self, p, x: float, y: float) -> float:
        vb = self.getPlotItem().vb
        psx, psy = vb.viewPixelSize()
        psx, psy = max(abs(psx), 1e-12), max(abs(psy), 1e-12)
        return float(np.hypot((x - p.x()) / psx, (y - p.y()) / psy))

    def _cut_at(self, pos) -> int | None:
        """Índice del corte a menos de GRAB_PX del mouse (o None)."""
        p = self._view_pos(pos)
        if p is None:
            return None
        best, best_px = None, GRAB_PX
        for i, (x, y) in enumerate(self.view.cut_xy):
            d = self._px_dist(p, x, y)
            if d < best_px:
                best, best_px = i, d
        return best

    def _dist_at(self, pos) -> float | None:
        """Metro de vuelta del punto del trazado más cercano al mouse."""
        mapping = self.view.hub.outline_dist_map()
        p = self._view_pos(pos)
        if mapping is None or p is None:
            return None
        d_arr, xs, ys = mapping
        vb = self.getPlotItem().vb
        psx, psy = vb.viewPixelSize()
        psx, psy = max(abs(psx), 1e-12), max(abs(psy), 1e-12)
        d2 = ((xs - p.x()) / psx) ** 2 + ((ys - p.y()) / psy) ** 2
        return float(d_arr[int(np.argmin(d2))])

    # ---------------------------------------------------------------- mouse

    def mousePressEvent(self, ev) -> None:
        if ev.button() == Qt.LeftButton:
            i = self._cut_at(ev.position())
            if i is not None:
                self._drag = i
                self.view.select_cut(i)
                ev.accept()
                return
        super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev) -> None:
        if self._drag is not None:
            dist = self._dist_at(ev.position())
            if dist is not None:
                self._drag = self.view.move_cut(self._drag, dist, save=False)
            ev.accept()
            return
        super().mouseMoveEvent(ev)

    def mouseReleaseEvent(self, ev) -> None:
        if self._drag is not None:
            self._drag = None
            self.view.commit()
            ev.accept()
            return
        super().mouseReleaseEvent(ev)

    # --------------------------------------------------------------- render

    def render_cuts(self) -> None:
        view = self.view
        outline = view.hub.outline
        if outline is not None:
            self.outline_curve.setData(outline[0], outline[1])
        for item in self._labels:
            self.removeItem(item)
        self._labels = []
        self.bound_dots.setData(
            [x for x, _y in view.bound_xy], [y for _x, y in view.bound_xy])
        self.cut_dots.setData(
            [x for x, _y in view.cut_xy], [y for _x, y in view.cut_xy])
        for i, (x, y) in enumerate(view.cut_xy):
            label = pg.TextItem(str(i + 1), color=theme.TEXT_MUTED,
                                anchor=(-0.3, 0.5))
            label.setPos(x, y)
            self.addItem(label, ignoreBounds=True)
            self._labels.append(label)
        sel = view.selected
        if sel is not None and sel < len(view.cut_xy):
            self.sel_dot.setData([view.cut_xy[sel][0]], [view.cut_xy[sel][1]])
            self.sel_dot.setVisible(True)
        else:
            self.sel_dot.setVisible(False)


class MicroConfigView(QWidget):
    def __init__(self, hub: DataHub, cfg: dict, parent=None):
        super().__init__(parent)
        self.hub = hub
        self.cfg = cfg
        self.analyzer = TimingAnalyzer(hub)
        self.selected: int | None = None
        self.cut_xy: list[tuple[float, float]] = []
        self.bound_xy: list[tuple[float, float]] = []
        self._sig: tuple | None = None

        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 2, 4, 4)
        lay.setSpacing(4)
        top = QHBoxLayout()
        self.info = QLabel("")
        self.info.setStyleSheet(f"color: {theme.TEXT_MUTED};")
        top.addWidget(self.info, stretch=1)
        self.lock_btn = QPushButton("🔒")
        self.lock_btn.setCheckable(True)
        self.lock_btn.setFixedWidth(36)
        self.lock_btn.setToolTip(
            "Lock the cuts exactly as they are: they stop following the "
            "automatic adjustments that arrive as cars run (measured "
            "braking zones, late circuit data) and are saved for this "
            "circuit+year. Unlock returns to automatic placement.")
        self.lock_btn.clicked.connect(self._lock_toggled)
        top.addWidget(self.lock_btn)
        self.add_btn = QPushButton("Add cut")
        self.add_btn.setToolTip("Insert a cut in the widest µ (drag it after)")
        self.add_btn.clicked.connect(self._add_cut)
        self.del_btn = QPushButton("Remove")
        self.del_btn.setToolTip("Remove the selected cut")
        self.del_btn.clicked.connect(self._remove_cut)
        self.reset_btn = QPushButton("Reset auto")
        self.reset_btn.setToolTip(
            "Back to automatic cuts (8 per sector, avoiding braking zones)")
        self.reset_btn.clicked.connect(self._reset_auto)
        for btn in (self.add_btn, self.del_btn, self.reset_btn):
            top.addWidget(btn)
        lay.addLayout(top)

        body = QHBoxLayout()
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Meters", "Sector"])
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.itemChanged.connect(self._table_edited)
        self.table.currentCellChanged.connect(self._table_selected)
        self.table.setMaximumWidth(230)
        body.addWidget(self.table)
        self.map = _MapCanvas(self)
        body.addWidget(self.map, stretch=1)
        lay.addLayout(body)

    # ------------------------------------------------------------- estado

    def _edges(self) -> tuple[float, float, float, float]:
        L = self.hub.track_length
        bounds = self.hub.sector_bounds
        if bounds is not None and 0.0 < bounds[0] < bounds[1] < L:
            return (0.0, bounds[0], bounds[1], L)
        return (0.0, L / 3.0, 2.0 * L / 3.0, L)

    def cuts(self) -> list[float]:
        """Cortes internos efectivos (custom si hay, si no los automáticos),
        sin los límites de sector."""
        marks = self.analyzer._mark_dists()
        i1, i2 = self.analyzer._sector_idx
        skip = {0, i1, i2, len(marks) - 1}
        return [float(marks[i]) for i in range(len(marks)) if i not in skip]

    def _sector_of(self, dist: float) -> int:
        edges = self._edges()
        if dist < edges[1]:
            return 1
        return 2 if dist < edges[2] else 3

    def _apply(self, cuts, save: bool = True) -> None:
        """Normaliza (orden, mínimos, lejos de los límites) y aplica: el hub
        es la fuente única y todas las vistas recalculan solas."""
        L = self.hub.track_length
        edges = self._edges()
        clean: list[float] = []
        for c in sorted(round(float(c), 1) for c in cuts):
            if not (MIN_GAP < c < L - MIN_GAP):
                continue
            if min(abs(c - e) for e in edges) < MIN_GAP:
                continue
            if clean and c - clean[-1] < MIN_GAP:
                continue
            clean.append(c)
        self.hub.custom_micro = clean
        if save:
            key = self.hub.circuit_key()
            if key:
                self.cfg.setdefault("microsectors", {})[key] = list(clean)
                config.save_config(self.cfg)
        self._sig = None
        self.refresh()

    def move_cut(self, idx: int, dist: float, save: bool = True) -> int:
        """Mueve un corte y devuelve su índice nuevo (el orden puede
        cambiar al cruzar otro corte)."""
        cuts = self.cuts()
        if not (0 <= idx < len(cuts)):
            return idx
        cuts[idx] = float(dist)
        self._apply(cuts, save=save)
        new = self.cuts()
        self.selected = (int(np.argmin(np.abs(np.array(new) - dist)))
                         if new else None)
        self._sync_selection()
        return self.selected if self.selected is not None else idx

    def commit(self) -> None:
        self._apply(self.cuts(), save=True)

    def select_cut(self, idx: int | None) -> None:
        self.selected = idx
        self._sync_selection()

    # ------------------------------------------------------------ acciones

    def _add_cut(self) -> None:
        marks = self.analyzer._mark_dists()
        gaps = np.diff(marks)
        i = int(np.argmax(gaps))
        new = float((marks[i] + marks[i + 1]) / 2.0)
        cuts = self.cuts() + [new]
        self._apply(cuts)
        self.select_cut(int(np.argmin(np.abs(np.array(self.cuts()) - new))))

    def _remove_cut(self) -> None:
        cuts = self.cuts()
        if self.selected is None or not (0 <= self.selected < len(cuts)):
            return
        cuts.pop(self.selected)
        self.selected = None
        self._apply(cuts)

    def _lock_toggled(self, on: bool) -> None:
        """Candado: congela los cortes tal como están AHORA (pasan a config
        propia, inmune a los ajustes automáticos que llegan con los autos
        girando); soltarlo vuelve a la colocación automática."""
        if on:
            self._apply(self.cuts())
        else:
            self._reset_auto()

    def _reset_auto(self) -> None:
        self.hub.custom_micro = None
        key = self.hub.circuit_key()
        if key and key in self.cfg.get("microsectors", {}):
            self.cfg["microsectors"].pop(key)
            config.save_config(self.cfg)
        self.selected = None
        self._sig = None
        self.refresh()

    def _table_edited(self, item: QTableWidgetItem) -> None:
        if item.column() != 0:
            return
        try:
            dist = float(str(item.text()).replace(",", "."))
        except ValueError:
            self._sig = None
            self.refresh()
            return
        self.move_cut(item.row(), dist)

    def _table_selected(self, row: int, _c: int, _pr: int, _pc: int) -> None:
        if 0 <= row < len(self.cut_xy):
            self.selected = row
            self.map.render_cuts()

    def _sync_selection(self) -> None:
        self.table.blockSignals(True)
        if self.selected is not None and self.selected < self.table.rowCount():
            self.table.selectRow(self.selected)
        else:
            self.table.clearSelection()
        self.table.blockSignals(False)
        self.map.render_cuts()

    def clear_data(self) -> None:
        self._sig = None

    # ------------------------------------------------------------ refresco

    def _project(self, dists) -> list[tuple[float, float]]:
        mapping = self.hub.outline_dist_map()
        if mapping is None:
            return []
        d_arr, xs, ys = mapping
        out = []
        for d in dists:
            d = min(max(float(d), 0.0), float(d_arr[-1]))
            out.append((float(np.interp(d, d_arr, xs)),
                        float(np.interp(d, d_arr, ys))))
        return out

    def refresh(self) -> None:
        cuts = self.cuts()
        edges = self._edges()
        key = self.hub.circuit_key()
        sig = (tuple(cuts), edges, key, self.hub.custom_micro is not None,
               self.hub.outline is not None
               and len(self.hub.outline[0]), self.selected)
        if sig == self._sig:
            return
        self._sig = sig

        counts = [b - a for a, b in self.analyzer.sector_slices()]
        locked = self.hub.custom_micro is not None
        mode = "🔒 fixed (saved)" if locked else "auto (follows the data)"
        self.info.setText(
            f"{key or 'no session'} · µ per sector: "
            f"{counts[0]}/{counts[1]}/{counts[2]} · {mode}")
        self.lock_btn.setChecked(locked)  # clicked no se dispara programático

        self.table.blockSignals(True)
        self.table.setRowCount(len(cuts))
        vlabels = []
        for r, c in enumerate(cuts):
            vlabels.append(f"µ cut {r + 1}")
            item = QTableWidgetItem(f"{c:.1f}")
            self.table.setItem(r, 0, item)
            sec = QTableWidgetItem(f"S{self._sector_of(c)}")
            sec.setFlags(sec.flags() & ~Qt.ItemIsEditable)
            self.table.setItem(r, 1, sec)
        self.table.setVerticalHeaderLabels(vlabels)
        self.table.blockSignals(False)

        self.cut_xy = self._project(cuts)
        self.bound_xy = self._project([edges[1], edges[2]])
        self.map.render_cuts()
