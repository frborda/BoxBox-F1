"""Paneles de análisis (sección Analysis del Control Hub).

Todos comparten la misma botonera: pilotos (👥), rango de vueltas, modo
"Per lap"/"Total" y un selector MÚLTIPLE de zonas (todas las curvas y
rectas detectadas del trazado). Analizan siempre del timeline para atrás.

Sincronización: el hover en cualquier mapa o gráfico-por-distancia de
estos paneles marca el mismo punto de pista en el resto de la app (track
map y gráficos de comparación) y viceversa, vía hover_dist_cb /
set_hover_dist.

Los ejes X e Y se reescalan de forma independiente con la mecánica nativa
de pyqtgraph: arrastrar o scrollear SOBRE un eje escala solo ese eje.
"""
from __future__ import annotations

import math

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
    QListWidget, QListWidgetItem, QMenu, QPushButton, QSpinBox,
    QTableWidget, QTableWidgetItem, QToolButton, QVBoxLayout, QWidget,
    QWidgetAction,
)

from ..analysis import AnalysisEngine, Zone, convex_hull, fit_trend
from ..hub import DataHub
from . import theme
from .driver_filter import DriverSelectButton

# catálogo de la sección Analysis: (pid, título, descripción)
ANALYSIS_SECTIONS = [
    ("Energy", [
        ("an_deploy", "Deploy & Coast",
         "Battery clipping zones and lift & coast on the map, plus meters "
         "per lap (estimated from telemetry)"),
        ("an_battery", "Battery balance",
         "Estimated battery index, charge and discharge per lap; driver "
         "energy-management comparison"),
    ]),
    ("Dynamics", [
        ("an_gg", "Friction circle",
         "g-g diagram: lateral vs longitudinal G per driver, with the "
         "envelope used to compare drivers"),
        ("an_gforce", "G forces",
         "Lateral and longitudinal G along the lap, with corner shading "
         "and an intensity map"),
        ("an_accel", "Acceleration",
         "Speed vs acceleration (positive longitudinal G only) — the "
         "traction/deploy envelope by speed, with optional trend lines"),
        ("an_grip", "Grip degradation",
         "Peak lateral G and minimum speed per corner across laps — "
         "measured tyre drop-off"),
    ]),
]

AXIS_HINT = ("Drag or scroll ON an axis to rescale X or Y independently; "
             "drag the plot to pan, right-drag to zoom")
DERATE_COLOR = QColor("#ff5252")
COAST_COLOR = QColor("#37d0ee")


def _trend_row(on_change) -> tuple[QHBoxLayout, QCheckBox, QComboBox]:
    """Control estándar de líneas de tendencia: tilde + tipo (lineal /
    cuadrática / exponencial). `on_change` se dispara con cualquier cambio."""
    row = QHBoxLayout()
    check = QCheckBox("Trend lines")
    check.toggled.connect(on_change)
    row.addWidget(check)
    combo = QComboBox()
    for label, kind in (("Linear", "linear"), ("Quadratic", "quadratic"),
                        ("Exponential", "exponential")):
        combo.addItem(label, kind)
    combo.currentIndexChanged.connect(on_change)
    row.addWidget(combo)
    row.addStretch(1)
    return row, check, combo


class AnalysisLauncher(QWidget):
    """Contenido de la ventana Analysis: accesos por sección a cada panel
    de análisis (botón resaltado = ventana abierta)."""

    def __init__(self, toggle_cb, parent=None):
        super().__init__(parent)
        self.buttons: dict[str, QPushButton] = {}
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(8)
        for section, items in ANALYSIS_SECTIONS:
            box = QGroupBox(section)
            box_lay = QVBoxLayout(box)
            box_lay.setSpacing(4)
            for pid, title, desc in items:
                btn = QPushButton(title)
                btn.setCheckable(True)
                btn.setToolTip(desc)
                btn.setStyleSheet(
                    "QPushButton { text-align: left; padding: 5px 8px; }"
                    f"QPushButton:checked {{ background: {theme.ACCENT};"
                    " color: #ffffff; font-weight: bold; }}")
                btn.toggled.connect(
                    lambda on, p=pid: toggle_cb(p, on))
                box_lay.addWidget(btn)
                self.buttons[pid] = btn
            lay.addWidget(box)
        note = QLabel(
            "Every analysis looks backwards from the current timeline "
            "position. Battery figures are estimates derived from "
            "telemetry (no real SOC channel exists in the feed).")
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {theme.TEXT_MUTED}; font-size: 7.5pt;")
        lay.addWidget(note)
        lay.addStretch(1)

    def sync(self, is_visible) -> None:
        for pid, btn in self.buttons.items():
            btn.blockSignals(True)
            btn.setChecked(bool(is_visible(pid)))
            btn.blockSignals(False)


class ZoneSelectButton(QToolButton):
    """Selector MÚLTIPLE de tramos: lista todas las curvas y rectas del
    trazado con un checkbox cada una, más atajos Todas/Curvas/Rectas.
    Sin nada tildado (estado inicial) equivale a "todas"."""

    changed = Signal()

    def __init__(self, corners_only: bool = False, parent=None):
        super().__init__(parent)
        self.corners_only = corners_only
        self._zones: list[Zone] = []
        self._chosen: set[str] | None = None  # labels elegidas; None = todas
        self.setAutoRaise(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setPopupMode(QToolButton.InstantPopup)
        self.setStyleSheet("QToolButton::menu-indicator { image: none; }")
        self.setText("Zones")
        self.setToolTip("Which track zones the analysis covers — tick "
                        "several corners/straights to combine them")
        menu = QMenu(self)
        box = QWidget()
        lay = QVBoxLayout(box)
        lay.setContentsMargins(6, 4, 6, 4)
        lay.setSpacing(2)
        quick = QHBoxLayout()
        for text, kind in (("All", None), ("Corners", "corner"),
                           ("Straights", "straight")):
            if corners_only and kind == "straight":
                continue
            btn = QPushButton(text)
            btn.setFixedHeight(20)
            btn.clicked.connect(lambda _=False, k=kind: self._quick(k))
            quick.addWidget(btn)
        lay.addLayout(quick)
        self.list = QListWidget()
        self.list.setMinimumSize(170, 260)
        self.list.itemChanged.connect(self._item_changed)
        lay.addWidget(self.list)
        action = QWidgetAction(menu)
        action.setDefaultWidget(box)
        menu.addAction(action)
        self.setMenu(menu)

    def _quick(self, kind: str | None) -> None:
        self._chosen = (None if kind is None else
                        {z.label for z in self._zones if z.kind == kind})
        self._rebuild()
        self.changed.emit()

    def _item_changed(self, item: QListWidgetItem) -> None:
        chosen = {self.list.item(i).text()
                  for i in range(self.list.count())
                  if self.list.item(i).checkState() == Qt.Checked}
        visible = {self.list.item(i).text()
                   for i in range(self.list.count())}
        self._chosen = None if chosen == visible or not chosen else chosen
        self._sync_label()
        self.changed.emit()

    def update_zones(self, zones: list[Zone]) -> None:
        listed = [z for z in zones
                  if not (self.corners_only and z.kind != "corner")]
        if [z.label for z in listed] == [self.list.item(i).text()
                                         for i in range(self.list.count())]:
            self._zones = zones
            return
        self._zones = zones
        self.list.blockSignals(True)
        self.list.clear()
        for z in listed:
            item = QListWidgetItem(z.label)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            on = self._chosen is None or z.label in self._chosen
            item.setCheckState(Qt.Checked if on else Qt.Unchecked)
            self.list.addItem(item)
        self.list.blockSignals(False)
        self._sync_label()

    def _rebuild(self) -> None:
        self.list.blockSignals(True)
        for i in range(self.list.count()):
            item = self.list.item(i)
            on = self._chosen is None or item.text() in self._chosen
            item.setCheckState(Qt.Checked if on else Qt.Unchecked)
        self.list.blockSignals(False)
        self._sync_label()

    def _sync_label(self) -> None:
        total = self.list.count()
        if self._chosen is None:
            self.setText("Zones" if not self.corners_only else "Corners")
        else:
            self.setText(f"Zones {len(self._chosen)}/{total}")
        self.adjustSize()

    def selector(self) -> tuple:
        """("all"|"kind"|"multi", arg) para engine.zone_mask."""
        if self._chosen is None:
            return (("kind", "corner") if self.corners_only
                    else ("all", None))
        idx = frozenset(i for i, z in enumerate(self._zones)
                        if z.label in self._chosen)
        return ("multi", idx)

    def allowed(self, zones: list[Zone]) -> set[int]:
        """Índices de zona habilitados por la selección actual."""
        if self._chosen is None:
            if self.corners_only:
                return {i for i, z in enumerate(zones)
                        if z.kind == "corner"}
            return set(range(len(zones)))
        return {i for i, z in enumerate(zones) if z.label in self._chosen}

    def sig(self) -> tuple:
        return (None if self._chosen is None
                else tuple(sorted(self._chosen)))


class AnalysisControls(QWidget):
    """Botonera común: pilotos, rango de vueltas, modo y zonas."""

    changed = Signal()

    def __init__(self, hub: DataHub, engine: AnalysisEngine,
                 corners_only: bool = False, parent=None):
        super().__init__(parent)
        self.hub = hub
        self.engine = engine
        self._auto = True       # sin toque del usuario: todos los autos
        row = QHBoxLayout(self)
        row.setContentsMargins(4, 2, 4, 2)
        row.setSpacing(6)
        self.sel_btn = DriverSelectButton(hub)
        self.sel_btn.changed.connect(self._user_touched)
        hub.driversChanged.connect(self._auto_fill)
        row.addWidget(self.sel_btn)
        lbl = QLabel("Laps")
        lbl.setStyleSheet(f"color: {theme.TEXT_MUTED};")
        row.addWidget(lbl)
        self.from_spin = QSpinBox()
        self.from_spin.setRange(1, 999)
        self.from_spin.setToolTip("First lap included")
        self.to_spin = QSpinBox()
        self.to_spin.setRange(0, 999)
        self.to_spin.setSpecialValueText("now")
        self.to_spin.setToolTip("Last lap included (\"now\" = timeline)")
        for spin in (self.from_spin, self.to_spin):
            spin.valueChanged.connect(lambda _v: self.changed.emit())
            row.addWidget(spin)
        # "Per lap": series vuelta a vuelta; "Total": el rango agregado
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("Per lap", "lap")
        self.mode_combo.addItem("Total", "total")
        self.mode_combo.setToolTip(
            "Per lap: one value per lap. Total: the whole lap range "
            "aggregated (averages / trend)")
        self.mode_combo.currentIndexChanged.connect(
            lambda _i: self.changed.emit())
        row.addWidget(self.mode_combo)
        self.zone_btn = ZoneSelectButton(corners_only)
        self.zone_btn.changed.connect(self.changed.emit)
        row.addWidget(self.zone_btn)
        row.addStretch(1)
        self._auto_fill()

    def _user_touched(self) -> None:
        self._auto = False
        self.changed.emit()

    def _auto_fill(self) -> None:
        if self._auto:
            self.sel_btn.set_selection(sorted(self.hub.drivers))

    def drivers(self) -> list[str]:
        if self._auto and not self.sel_btn.selection():
            self._auto_fill()
        return self.sel_btn.selection()

    def lap_range(self) -> tuple[int, int | None]:
        hi = self.to_spin.value()
        return self.from_spin.value(), (hi if hi > 0 else None)

    def mode(self) -> str:
        return str(self.mode_combo.currentData())

    def selector(self) -> tuple:
        return self.zone_btn.selector()

    def update_zones(self, zones: list[Zone]) -> None:
        self.zone_btn.update_zones(zones)

    def sig(self) -> tuple:
        return (tuple(sorted(self.drivers())), self.lap_range(),
                self.mode(), self.zone_btn.sig())


class AnalysisPanelBase(QWidget):
    """Esqueleto común: botonera + cuerpo + tabla de datos, refresh con
    firma para no recalcular si nada cambió, y sincronización de hover
    (hover_dist_cb hacia afuera, set_hover_dist hacia adentro)."""

    corners_only = False

    def __init__(self, hub: DataHub, engine: AnalysisEngine, parent=None):
        super().__init__(parent)
        self.hub = hub
        self.engine = engine
        self._sig: tuple | None = None
        self.hover_dist_cb = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        self.controls = AnalysisControls(hub, engine, self.corners_only)
        self.controls.changed.connect(self._dirty)
        lay.addWidget(self.controls)
        self.body = QVBoxLayout()
        lay.addLayout(self.body, stretch=1)
        self.table = QTableWidget()
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setMaximumHeight(150)
        self.table.setStyleSheet("font-size: 7.5pt;")
        lay.addWidget(self.table)
        self.summary = QLabel("")
        self.summary.setWordWrap(True)
        self.summary.setStyleSheet(
            f"color: {theme.TEXT_MUTED}; font-size: 7.5pt; padding: 0 4px;")
        lay.addWidget(self.summary)
        self._build()
        self.setMinimumSize(560, 430)

    # ganchos de subclase
    def _build(self) -> None: ...
    def _clear(self) -> None: ...
    def _refresh_impl(self, zones: list[Zone]) -> None: ...

    def set_hover_dist(self, dist: float | None) -> None:
        """Marca el punto de pista hovereado en otro panel (si aplica)."""

    def _dirty(self) -> None:
        self._sig = None
        if self.isVisible():
            self.refresh()

    def clear_data(self) -> None:
        self._sig = None
        self.summary.setText("")
        self.table.setRowCount(0)
        self.set_hover_dist(None)
        self._clear()

    def refresh(self) -> None:
        zones = self.engine.zones()
        self.controls.update_zones(zones)
        total_n = sum(self.hub.buffers[d].n for d in self.controls.drivers()
                      if d in self.hub.buffers)
        sig = (self.controls.sig(), len(zones), total_n)
        if sig == self._sig:
            return
        self._sig = sig
        self._refresh_impl(zones)

    # ------------------------------------------------------------ helpers

    def _laps_for(self, drv: str) -> list[int]:
        lo, hi = self.controls.lap_range()
        return [l for l in self.engine.completed_laps(drv)
                if l >= lo and (hi is None or l <= hi)]

    def _color(self, drv: str) -> str:
        info = self.hub.drivers.get(drv)
        return info.color if info else "#9aa0a6"

    def _code(self, drv: str) -> str:
        info = self.hub.drivers.get(drv)
        return info.code if info else drv

    def _outline_slice(self, d0: float, d1: float):
        """Tramo [d0, d1] del trazado del circuito."""
        mapping = self.hub.outline_dist_map()
        if mapping is None or d1 <= d0:
            return None
        dist, xs, ys = mapping
        mask = (dist >= d0) & (dist <= d1)
        if mask.sum() < 2:
            return None
        return xs[mask], ys[mask]

    def _fill_table(self, headers: list[str], rows: list[list]) -> None:
        self.table.setColumnCount(len(headers))
        self.table.setHorizontalHeaderLabels(headers)
        self.table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            for c, value in enumerate(row):
                item = QTableWidgetItem(str(value))
                item.setTextAlignment(Qt.AlignCenter)
                self.table.setItem(r, c, item)
        self.table.horizontalHeader().setSectionResizeMode(
            QHeaderView.Stretch)

    def _driver_bars(self, plot, values: dict[str, float]) -> None:
        """Barras por piloto (modo Total) con los códigos en el eje X."""
        drivers = list(values)
        xs = np.arange(len(drivers), dtype=float)
        brushes = [pg.mkBrush(self._color(d)) for d in drivers]
        plot.addItem(pg.BarGraphItem(
            x=xs, height=[values[d] for d in drivers], width=0.6,
            brushes=brushes, pen=pg.mkPen(None)))
        plot.getAxis("bottom").setTicks(
            [[(float(i), self._code(d)) for i, d in enumerate(drivers)]])

    def _reset_ticks(self, plot, label: str) -> None:
        plot.getAxis("bottom").setTicks(None)
        plot.setLabel("bottom", label)

    # mapa con marcador de hover + reporte del hover propio ---------------

    def _init_map(self, widget: pg.PlotWidget) -> tuple:
        widget.setMenuEnabled(False)
        widget.setAspectLocked(True)
        widget.getPlotItem().hideAxis("left")
        widget.getPlotItem().hideAxis("bottom")
        outline = pg.PlotDataItem(pen=pg.mkPen("#3a3f4a", width=4))
        widget.addItem(outline)
        probe = pg.ScatterPlotItem(
            symbol="o", size=14, pxMode=True,
            brush=pg.mkBrush(0, 0, 0, 0),
            pen=pg.mkPen(theme.ACCENT, width=2))
        probe.setZValue(30)
        probe.setVisible(False)
        widget.addItem(probe)
        widget.scene().sigMouseMoved.connect(
            lambda pos, w=widget: self._map_mouse(w, pos))
        return outline, probe

    def _map_mouse(self, widget, scene_pos) -> None:
        if self.hover_dist_cb is None:
            return
        vb = widget.getPlotItem().vb
        mapping = self.hub.outline_dist_map()
        if mapping is None or not vb.sceneBoundingRect().contains(scene_pos):
            return
        p = vb.mapSceneToView(scene_pos)
        dist, xs, ys = mapping
        psx, psy = vb.viewPixelSize()
        d2 = (((xs - p.x()) / max(abs(psx), 1e-12)) ** 2
              + ((ys - p.y()) / max(abs(psy), 1e-12)) ** 2)
        i = int(np.argmin(d2))
        self.hover_dist_cb(float(dist[i])
                           if float(d2[i]) ** 0.5 <= 25.0 else None)

    def _place_probe(self, probe, dist: float | None) -> None:
        mapping = self.hub.outline_dist_map()
        if dist is None or mapping is None:
            probe.setVisible(False)
            return
        d_arr, xs, ys = mapping
        dist = min(max(float(dist), 0.0), float(d_arr[-1]))
        probe.setData([float(np.interp(dist, d_arr, xs))],
                      [float(np.interp(dist, d_arr, ys))])
        probe.setVisible(True)


class DeployCoastPanel(AnalysisPanelBase):
    """Clipping (derate) y lift & coast: mapa de zonas con referencias +
    metros por vuelta (o el total del rango) por piloto."""

    def _build(self) -> None:
        row = QHBoxLayout()
        map_col = QVBoxLayout()
        self.map = pg.PlotWidget()
        self.map_outline, self.map_probe = self._init_map(self.map)
        self._overlay: list = []
        map_col.addWidget(self.map, stretch=1)
        # tildes-leyenda: mostrar/ocultar cada representación en el mapa
        leg_row = QHBoxLayout()
        self.derate_check = QCheckBox("▮ Derate")
        self.derate_check.setStyleSheet(
            f"color: {DERATE_COLOR.name()}; font-size: 7.5pt;")
        self.derate_check.setToolTip(
            "Show the measured derate spans on the track line")
        self.coast_check = QCheckBox("▮ Lift && coast")
        self.coast_check.setStyleSheet(
            f"color: {COAST_COLOR.name()}; font-size: 7.5pt;")
        self.coast_check.setToolTip(
            "Show the lift & coast approaches on the track line")
        for chk in (self.derate_check, self.coast_check):
            chk.setChecked(True)
            chk.toggled.connect(self._dirty)
            leg_row.addWidget(chk)
        note = QLabel("▢ start · Tn = corners")
        note.setStyleSheet(
            f"color: {theme.TEXT_MUTED}; font-size: 7.5pt;")
        leg_row.addWidget(note)
        leg_row.addStretch(1)
        map_col.addLayout(leg_row)
        row.addLayout(map_col, stretch=5)
        charts_col = QVBoxLayout()
        t_row, self.trend_check, self.trend_combo = _trend_row(self._dirty)
        charts_col.addLayout(t_row)
        self.glw = pg.GraphicsLayoutWidget()
        self.glw.setToolTip(AXIS_HINT)
        self.p_derate = self.glw.addPlot(row=0, col=0)
        self.p_coast = self.glw.addPlot(row=1, col=0)
        self.p_coast.setXLink(self.p_derate)
        self.p_derate.setLabel("left", "Derate m")
        self.p_coast.setLabel("left", "Coast m")
        self.p_coast.setLabel("bottom", "Lap")
        for plot in (self.p_derate, self.p_coast):
            plot.showGrid(x=True, y=True, alpha=theme.GRID_ALPHA)
        charts_col.addWidget(self.glw, stretch=1)
        row.addLayout(charts_col, stretch=4)
        self.body.addLayout(row)

    def _clear(self) -> None:
        for item in self._overlay:
            self.map.removeItem(item)
        self._overlay = []
        for plot in (self.p_derate, self.p_coast):
            plot.clear()
        self.map_outline.setData([], [])

    def set_hover_dist(self, dist: float | None) -> None:
        self._place_probe(self.map_probe, dist)

    def _refresh_impl(self, zones: list[Zone]) -> None:
        for item in self._overlay:
            self.map.removeItem(item)
        self._overlay = []
        for plot in (self.p_derate, self.p_coast):
            plot.clear()
        mapping = self.hub.outline_dist_map()
        if mapping is not None:
            _d, xs, ys = mapping
            self.map_outline.setData(xs, ys)
            # referencias del mapa: meta y vértices oficiales
            start = pg.ScatterPlotItem(
                [float(xs[0])], [float(ys[0])], symbol="s", size=9,
                brush=pg.mkBrush(theme.TEXT), pen=pg.mkPen(None))
            self.map.addItem(start)
            self._overlay.append(start)
            for label, _dist, cx, cy in self.hub.corners:
                item = pg.TextItem(label, color=theme.TEXT_MUTED,
                                   anchor=(0.5, 0.5))
                item.setPos(float(cx), float(cy))
                self.map.addItem(item)
                self._overlay.append(item)

        allowed = self.controls.zone_btn.allowed(zones)
        mode = self.controls.mode()
        derate_zone: dict[int, list] = {}
        derate_start: dict[int, list] = {}
        derate_end: dict[int, list] = {}
        coast_zone: dict[int, list] = {}
        table_rows: list[list] = []
        totals: dict[str, tuple] = {}
        lines = []
        for drv in self.controls.drivers():
            laps, der, coa = [], [], []
            clean = 0
            for lap in self._laps_for(drv):
                m = self.engine.lap_metrics(drv, lap)
                if m is None:
                    continue
                if not (m.pit or m.caution):
                    clean += 1
                    for zi, meters in m.derate_m.items():
                        if zi in allowed:
                            derate_zone.setdefault(zi, []).append(meters)
                            derate_start.setdefault(zi, []).append(
                                m.derate_start[zi])
                            derate_end.setdefault(zi, []).append(
                                m.derate_end[zi])
                    for zi, meters in m.coast_m.items():
                        if zi in allowed:
                            coast_zone.setdefault(zi, []).append(meters)
                d_lap = sum(v for zi, v in m.derate_m.items()
                            if zi in allowed)
                c_lap = sum(v for zi, v in m.coast_m.items()
                            if zi in allowed)
                laps.append(lap)
                der.append(d_lap)
                coa.append(c_lap)
                if mode == "lap":
                    deploy = (100.0 * m.deploy_m / m.wot_straight_m
                              if m.wot_straight_m > 0 else float("nan"))
                    flag = "PIT" if m.pit else ("SC" if m.caution else "")
                    table_rows.append(
                        [self._code(drv), lap, f"{d_lap:.0f}",
                         f"{c_lap:.0f}",
                         "—" if deploy != deploy else f"{deploy:.0f}%",
                         flag])
            if laps:
                if mode == "lap":
                    pen = pg.mkPen(self._color(drv), width=2)
                    self.p_derate.plot(
                        laps, der, pen=pen, symbol="o", symbolSize=4,
                        symbolBrush=self._color(drv), symbolPen=None)
                    self.p_coast.plot(
                        laps, coa, pen=pen, symbol="o", symbolSize=4,
                        symbolBrush=self._color(drv), symbolPen=None)
                    if self.trend_check.isChecked():
                        kind = self.trend_combo.currentData()
                        for plot, ys in ((self.p_derate, der),
                                         (self.p_coast, coa)):
                            fit = fit_trend(np.array(laps, float),
                                            np.array(ys, float), kind)
                            if fit is not None:
                                plot.plot(fit[0], fit[1],
                                          pen=pg.mkPen(self._color(drv),
                                                       width=2,
                                                       style=Qt.DashLine))
                totals[drv] = (len(laps), clean, sum(der), sum(coa))
                lines.append(
                    f"{self._code(drv)}: {sum(der):.0f} m derate · "
                    f"{sum(coa):.0f} m coast · {len(laps)} laps")

        if mode == "total":
            self._driver_bars(
                self.p_derate,
                {d: t[2] / max(t[0], 1) for d, t in totals.items()})
            self._driver_bars(
                self.p_coast,
                {d: t[3] / max(t[0], 1) for d, t in totals.items()})
            self.p_derate.setLabel("bottom", "")
            self.p_coast.setLabel("bottom", "Driver (avg m/lap)")
            self._fill_table(
                ["Driver", "Laps", "Derate m/lap", "Coast m/lap",
                 "Derate total", "Coast total"],
                [[self._code(d), t[0], f"{t[2] / max(t[0], 1):.0f}",
                  f"{t[3] / max(t[0], 1):.0f}", f"{t[2]:.0f}",
                  f"{t[3]:.0f}"] for d, t in totals.items()])
        else:
            self._reset_ticks(self.p_derate, "")
            self._reset_ticks(self.p_coast, "Lap")
            self._fill_table(
                ["Driver", "Lap", "Derate m", "Coast m", "Deploy %", ""],
                table_rows)

        # mapa: ambos SOBRE el trazado — derate en rojo grueso (su
        # extensión medida inicio→fin) y lift & coast en cian más fino
        # dibujado encima, así una superposición se lee (núcleo cian sobre
        # halo rojo). Cada tilde muestra/oculta su representación
        if self.derate_check.isChecked():
            for zi, meters in derate_zone.items():
                zone = zones[zi]
                start_d = float(np.mean(derate_start[zi]))
                end_d = float(np.mean(derate_end[zi]))
                sl = self._outline_slice(start_d,
                                         max(end_d, start_d + 10.0))
                if sl is None:
                    continue
                avg = float(np.mean(meters))
                intensity = min(1.0, (avg / max(zone.d1 - zone.d0, 1.0))
                                * 3.0)
                color = QColor(DERATE_COLOR)
                color.setAlpha(90 + int(160 * intensity))
                item = pg.PlotDataItem(sl[0], sl[1],
                                       pen=pg.mkPen(color, width=7))
                self.map.addItem(item)
                self._overlay.append(item)
                self._map_note(sl[0][-1], sl[1][-1],
                               f"{avg:.0f}m ×{len(meters)}", DERATE_COLOR)
        if self.coast_check.isChecked():
            for zi, meters in coast_zone.items():
                zone = zones[zi]
                avg = float(np.mean(meters))
                sl = self._outline_slice(zone.d0 - avg, zone.d0)
                if sl is None:
                    continue
                color = QColor(COAST_COLOR)
                color.setAlpha(120 + int(135 * min(1.0, avg / 250.0)))
                item = pg.PlotDataItem(sl[0], sl[1],
                                       pen=pg.mkPen(color, width=4))
                item.setZValue(5)  # encima del derate
                self.map.addItem(item)
                self._overlay.append(item)
                self._map_note(sl[0][0], sl[1][0],
                               f"{avg:.0f}m ×{len(meters)}", COAST_COLOR)
        self.summary.setText(
            "  |  ".join(lines) if lines else
            "No laps in range (map/averages use clean laps only: "
            "pit and SC/yellow laps are excluded)")

    def _map_note(self, x: float, y: float, text: str,
                  color: QColor) -> None:
        note = pg.TextItem(text, color=color, anchor=(0.5, 1.1))
        note.setPos(float(x), float(y))
        self.map.addItem(note)
        self._overlay.append(note)


class EnergyBalancePanel(AnalysisPanelBase):
    """Índice de batería estimado + carga/descarga por vuelta y
    comparativa de gestión entre pilotos."""

    def _build(self) -> None:
        row, self.trend_check, self.trend_combo = _trend_row(self._dirty)
        self.body.addLayout(row)
        self.glw = pg.GraphicsLayoutWidget()
        self.glw.setToolTip(AXIS_HINT)
        self.p_batt = self.glw.addPlot(row=0, col=0)
        self.p_charge = self.glw.addPlot(row=1, col=0)
        self.p_disch = self.glw.addPlot(row=2, col=0)
        self.p_batt.setLabel("left", "Battery %*")
        self.p_charge.setLabel("left", "Charge")
        self.p_disch.setLabel("left", "Discharge")
        self.p_disch.setLabel("bottom", "Lap")
        for plot in (self.p_charge, self.p_disch):
            plot.setXLink(self.p_batt)
        for plot in (self.p_batt, self.p_charge, self.p_disch):
            plot.showGrid(x=True, y=True, alpha=theme.GRID_ALPHA)
        self.body.addWidget(self.glw)

    def _clear(self) -> None:
        for plot in (self.p_batt, self.p_charge, self.p_disch):
            plot.clear()

    def _series(self, drv: str):
        laps = self._laps_for(drv)
        metrics = [(l, self.engine.lap_metrics(drv, l)) for l in laps]
        clean = [(l, m) for l, m in metrics
                 if m is not None and not (m.pit or m.caution)]
        if not clean:
            return None
        raw = np.array([m.brake_e + 0.5 * m.coast_e for _l, m in clean])
        scale = max(float(np.percentile(raw, 95)), 1e-9)
        charge = np.clip(raw / scale * 100.0, 0.0, 120.0)
        disch = np.array([
            100.0 * m.deploy_m / max(m.wot_straight_m, 1.0)
            for _l, m in clean])
        batt = np.clip(50.0 + np.cumsum(charge - disch) * 0.12, 2, 100)
        return [l for l, _m in clean], clean, charge, disch, batt

    def _refresh_impl(self, zones: list[Zone]) -> None:
        self._clear()
        mode = self.controls.mode()
        lines = []
        table_rows: list[list] = []
        bars_b: dict[str, float] = {}
        bars_c: dict[str, float] = {}
        bars_d: dict[str, float] = {}
        for drv in self.controls.drivers():
            series = self._series(drv)
            if series is None:
                continue
            x, clean, charge, disch, batt = series
            if mode == "lap":
                pen = pg.mkPen(self._color(drv), width=2)
                self.p_batt.plot(x, batt, pen=pen)
                self.p_charge.plot(x, charge, pen=pen, symbol="o",
                                   symbolSize=3,
                                   symbolBrush=self._color(drv),
                                   symbolPen=None)
                self.p_disch.plot(x, disch, pen=pen, symbol="o",
                                  symbolSize=3,
                                  symbolBrush=self._color(drv),
                                  symbolPen=None)
                if self.trend_check.isChecked():
                    kind = self.trend_combo.currentData()
                    for plot, ys in ((self.p_batt, batt),
                                     (self.p_charge, charge),
                                     (self.p_disch, disch)):
                        fit = fit_trend(np.array(x, float),
                                        np.asarray(ys, float), kind)
                        if fit is not None:
                            plot.plot(fit[0], fit[1],
                                      pen=pg.mkPen(self._color(drv),
                                                   width=2,
                                                   style=Qt.DashLine))
                for i, l in enumerate(x):
                    table_rows.append(
                        [self._code(drv), l, f"{charge[i]:.0f}",
                         f"{disch[i]:.0f}", f"{batt[i]:.0f}"])
            else:
                bars_b[drv] = float(np.mean(batt))
                bars_c[drv] = float(np.mean(charge))
                bars_d[drv] = float(np.mean(disch))
                table_rows.append(
                    [self._code(drv), len(x), f"{np.mean(charge):.0f}",
                     f"{np.mean(disch):.0f}", f"{np.mean(batt):.0f}"])
            coast_avg = float(np.mean([m.coast_total for _l, m in clean]))
            derate_avg = float(np.mean([m.derate_total for _l, m in clean]))
            lines.append(
                f"{self._code(drv)}: coast {coast_avg:.0f} m/lap · derate "
                f"{derate_avg:.0f} m/lap · deploy "
                f"{float(np.mean(disch)):.0f}%")
        if mode == "total" and bars_b:
            self._driver_bars(self.p_batt, bars_b)
            self._driver_bars(self.p_charge, bars_c)
            self._driver_bars(self.p_disch, bars_d)
            self.p_disch.setLabel("bottom", "Driver (avg)")
            self._fill_table(
                ["Driver", "Laps", "Avg charge", "Avg discharge",
                 "Avg battery*"], table_rows)
        else:
            for plot, label in ((self.p_batt, ""), (self.p_charge, ""),
                                (self.p_disch, "Lap")):
                self._reset_ticks(plot, label)
            self._fill_table(
                ["Driver", "Lap", "Charge", "Discharge", "Battery*"],
                table_rows)
        self.summary.setText(
            ("*estimated from telemetry — no real SOC channel.   "
             + "  |  ".join(lines)) if lines else
            "No clean laps in range yet")


class GGDiagramPanel(AnalysisPanelBase):
    """Círculo de fricción: nube de puntos y/o la envolvente que delimita
    la nube (la que se compara entre pilotos)."""

    def _build(self) -> None:
        opts = QHBoxLayout()
        self.points_check = QCheckBox("Points")
        self.points_check.setChecked(True)
        self.hull_check = QCheckBox("Envelope")
        self.hull_check.setChecked(True)
        self.square_check = QCheckBox("1:1")
        self.square_check.setChecked(True)
        self.square_check.setToolTip(
            "Lock the aspect ratio (uncheck to rescale X and Y freely)")
        for chk in (self.points_check, self.hull_check, self.square_check):
            chk.toggled.connect(self._options_changed)
            opts.addWidget(chk)
        opts.addStretch(1)
        self.body.addLayout(opts)
        self.plot = pg.PlotWidget()
        self.plot.setMenuEnabled(False)
        self.plot.setAspectLocked(True)
        self.plot.setToolTip(AXIS_HINT)
        self.plot.setLabel("bottom", "Lateral G (− left / + right)")
        self.plot.setLabel("left", "Longitudinal G")
        self.plot.showGrid(x=True, y=True, alpha=theme.GRID_ALPHA)
        self.plot.setXRange(-6.5, 6.5)
        self.plot.setYRange(-6.5, 6.5)
        th = np.linspace(0, 2 * math.pi, 120)
        for r in (2.0, 4.0, 6.0):
            self.plot.addItem(pg.PlotDataItem(
                r * np.cos(th), r * np.sin(th),
                pen=pg.mkPen("#3a3f4a", width=1, style=Qt.DashLine)))
        self.body.addWidget(self.plot)
        self._items: list = []

    def _options_changed(self) -> None:
        self.plot.setAspectLocked(self.square_check.isChecked())
        self._sig = None
        self.refresh()

    def _clear(self) -> None:
        for item in self._items:
            self.plot.removeItem(item)
        self._items = []

    def _masked(self, drv: str):
        chan = self.engine.channels(drv)
        if chan is None:
            return None
        lo, hi = self.controls.lap_range()
        lap = chan["lap"]
        mask = (lap >= lo) & (chan["v"] > 15.0) & ~chan["guard"]
        if hi is not None:
            mask &= lap <= hi
        mask &= lap < self.hub.buffers[drv].current_lap()
        mask &= self.engine.zone_mask(chan["dist"],
                                      self.controls.selector())
        return chan, mask

    def _refresh_impl(self, zones: list[Zone]) -> None:
        self._clear()
        lines = []
        table_rows: list[list] = []
        per_lap_table = self.controls.mode() == "lap"
        for drv in self.controls.drivers():
            got = self._masked(drv)
            if got is None:
                continue
            chan, mask = got
            n = int(mask.sum())
            if n < 10:
                continue
            x = chan["a_lat"][mask]
            y = chan["a_lon"][mask]
            if self.points_check.isChecked():
                stride = max(1, n // 6000)
                color = QColor(self._color(drv))
                color.setAlpha(90)
                scatter = pg.ScatterPlotItem(
                    x=x[::stride], y=y[::stride], size=4,
                    brush=pg.mkBrush(color), pen=None)
                self.plot.addItem(scatter)
                self._items.append(scatter)
            if self.hull_check.isChecked():
                hx, hy = convex_hull(np.round(x, 2), np.round(y, 2))
                if len(hx) >= 4:
                    hull = pg.PlotDataItem(
                        hx, hy, pen=pg.mkPen(self._color(drv), width=2))
                    self.plot.addItem(hull)
                    self._items.append(hull)
            lines.append(
                f"{self._code(drv)}: {np.abs(x).max():.1f}G lat · "
                f"{max(0.0, -y.min()):.1f}G braking · "
                f"{max(0.0, y.max()):.1f}G accel")
            if per_lap_table:
                laps_arr = chan["lap"][mask]
                for l in np.unique(laps_arr):
                    sel = laps_arr == l
                    table_rows.append(
                        [self._code(drv), int(l),
                         f"{np.abs(x[sel]).max():.2f}",
                         f"{max(0.0, -y[sel].min()):.2f}",
                         f"{max(0.0, y[sel].max()):.2f}"])
            else:
                table_rows.append(
                    [self._code(drv), "range",
                     f"{np.abs(x).max():.2f}",
                     f"{max(0.0, -y.min()):.2f}",
                     f"{max(0.0, y.max()):.2f}"])
        self._fill_table(
            ["Driver", "Lap", "Peak lat G", "Peak brake G",
             "Peak accel G"], table_rows)
        self.summary.setText("  |  ".join(lines) if lines else
                             "No complete laps in range yet")


class GForcePanel(AnalysisPanelBase):
    """G lateral y longitudinal a lo largo de la vuelta (por vuelta o el
    promedio del rango) + mapa de intensidad, sincronizados por hover."""

    BIN_M = 8.0

    def _build(self) -> None:
        row = QHBoxLayout()
        self.glw = pg.GraphicsLayoutWidget()
        self.glw.setToolTip(AXIS_HINT)
        self.p_lat = self.glw.addPlot(row=0, col=0)
        self.p_lon = self.glw.addPlot(row=1, col=0)
        self.p_lon.setXLink(self.p_lat)
        self.p_lat.setLabel("left", "Lateral G")
        self.p_lon.setLabel("left", "Longitudinal G")
        self.p_lon.setLabel("bottom", "Lap distance (m)")
        for plot in (self.p_lat, self.p_lon):
            plot.showGrid(x=True, y=True, alpha=theme.GRID_ALPHA)
        self._hover_lines = []
        for plot in (self.p_lat, self.p_lon):
            line = pg.InfiniteLine(angle=90,
                                   pen=pg.mkPen(theme.ACCENT, width=1))
            line.setVisible(False)
            plot.addItem(line, ignoreBounds=True)
            self._hover_lines.append(line)
        self.glw.scene().sigMouseMoved.connect(self._chart_mouse)
        row.addWidget(self.glw, stretch=5)
        self.map = pg.PlotWidget()
        self.map_outline, self.map_probe = self._init_map(self.map)
        self._heat: list = []
        row.addWidget(self.map, stretch=3)
        self.body.addLayout(row)
        self._regions: list = []

    def _chart_mouse(self, scene_pos) -> None:
        if self.hover_dist_cb is None:
            return
        vb = self.p_lat.vb
        if not vb.sceneBoundingRect().contains(scene_pos):
            vb = self.p_lon.vb
            if not vb.sceneBoundingRect().contains(scene_pos):
                self.hover_dist_cb(None)
                return
        x = float(vb.mapSceneToView(scene_pos).x())
        self.hover_dist_cb(x if 0.0 <= x <= self.hub.track_length else None)

    def set_hover_dist(self, dist: float | None) -> None:
        self._place_probe(self.map_probe, dist)
        for line in self._hover_lines:
            if dist is None:
                line.setVisible(False)
            else:
                line.setValue(float(dist))
                line.setVisible(True)

    def _clear(self) -> None:
        for plot in (self.p_lat, self.p_lon):
            plot.clear()
        self._regions = []
        for line in self._hover_lines:
            line.setVisible(False)
        for item in self._heat:
            self.map.removeItem(item)
        self._heat = []
        self.map_outline.setData([], [])

    @staticmethod
    def _heat_color(x: float) -> QColor:
        """0 → gris azulado, 0.5 → amarillo, 1 → rojo."""
        x = min(max(x, 0.0), 1.0)
        if x < 0.5:
            f = x / 0.5
            return QColor(int(90 + f * 165), int(105 + f * 95), 110)
        f = (x - 0.5) / 0.5
        return QColor(255, int(200 - f * 150), int(110 - f * 80))

    def _refresh_impl(self, zones: list[Zone]) -> None:
        self._clear()
        # las líneas de hover se limpiaron con plot.clear(): volver a
        # agregarlas para que el probe siga funcionando
        for plot, line in zip((self.p_lat, self.p_lon), self._hover_lines):
            plot.addItem(line, ignoreBounds=True)
        L = self.hub.track_length
        n_bins = max(int(L / self.BIN_M), 16)
        lo, hi = self.controls.lap_range()
        per_lap = self.controls.mode() == "lap"
        edges = np.linspace(0.0, L, n_bins + 1)
        centers = (edges[:-1] + edges[1:]) / 2.0
        zone_bins = self.engine.zone_mask(centers, self.controls.selector())
        heat_sum = np.zeros(n_bins)
        heat_cnt = np.zeros(n_bins)
        table_rows: list[list] = []
        for zone in zones:
            if zone.kind != "corner":
                continue
            region = pg.LinearRegionItem(values=(zone.d0, zone.d1),
                                         movable=False,
                                         brush=pg.mkBrush(60, 65, 75, 60),
                                         pen=pg.mkPen(None))
            region.setZValue(-20)
            self.p_lat.addItem(region)
            self._regions.append(region)
        for drv in self.controls.drivers():
            chan = self.engine.channels(drv)
            if chan is None:
                continue
            lap = chan["lap"]
            cur = self.hub.buffers[drv].current_lap()
            base_mask = (lap >= lo) & (lap < cur)
            if hi is not None:
                base_mask &= lap <= hi
            if base_mask.sum() < 20:
                continue

            def binned(mask):
                idx = np.clip(np.digitize(chan["dist"][mask], edges) - 1,
                              0, n_bins - 1)
                lat = np.zeros(n_bins)
                lon = np.zeros(n_bins)
                cnt = np.zeros(n_bins)
                np.add.at(lat, idx, chan["a_lat"][mask])
                np.add.at(lon, idx, chan["a_lon"][mask])
                np.add.at(cnt, idx, 1.0)
                ok = (cnt > 0) & zone_bins
                return ok, lat, lon, cnt, idx

            ok, lat, lon, cnt, idx = binned(base_mask)
            if per_lap:
                # trazas finas por vuelta + promedio en trazo grueso
                thin = QColor(self._color(drv))
                thin.setAlpha(70)
                for l in np.unique(lap[base_mask]):
                    ok_l, lat_l, lon_l, cnt_l, _i = binned(
                        base_mask & (lap == l))
                    self.p_lat.plot(centers[ok_l], lat_l[ok_l] / cnt_l[ok_l],
                                    pen=pg.mkPen(thin, width=1))
                    self.p_lon.plot(centers[ok_l], lon_l[ok_l] / cnt_l[ok_l],
                                    pen=pg.mkPen(thin, width=1))
            pen = pg.mkPen(self._color(drv), width=2)
            self.p_lat.plot(centers[ok], lat[ok] / cnt[ok], pen=pen)
            self.p_lon.plot(centers[ok], lon[ok] / cnt[ok], pen=pen)
            np.add.at(heat_sum, idx, np.abs(chan["a_lat"][base_mask]))
            heat_cnt += cnt
            zmask = self.engine.zone_mask(chan["dist"],
                                          self.controls.selector())
            sel = base_mask & zmask
            if sel.any():
                table_rows.append(
                    [self._code(drv),
                     f"{np.abs(chan['a_lat'][sel]).max():.2f}",
                     f"{max(0.0, -chan['a_lon'][sel].min()):.2f}",
                     f"{max(0.0, chan['a_lon'][sel].max()):.2f}",
                     f"{np.abs(chan['a_lat'][sel]).mean():.2f}"])
        self._fill_table(
            ["Driver", "Peak lat G", "Peak brake G", "Peak accel G",
             "Mean |lat| G"], table_rows)
        mapping = self.hub.outline_dist_map()
        if mapping is not None and heat_cnt.sum() > 0:
            dist, xs, ys = mapping
            self.map_outline.setData(xs, ys)
            mean = np.divide(heat_sum, np.maximum(heat_cnt, 1.0))
            peak = max(float(mean.max()), 1e-6)
            vals = np.interp(dist, centers, mean / peak)
            chunk = max(4, len(dist) // 90)
            for i in range(0, len(dist) - 1, chunk):
                j = min(i + chunk + 1, len(dist))
                item = pg.PlotDataItem(
                    xs[i:j], ys[i:j],
                    pen=pg.mkPen(self._heat_color(float(vals[i:j].mean())),
                                 width=5))
                self.map.addItem(item)
                self._heat.append(item)
            self.summary.setText(
                f"Map: mean |lateral G| over the selection — peak "
                f"{peak:.1f}G. Hover any chart or map to cross-locate.")
        else:
            self.summary.setText("No complete laps in range yet")


class AccelPanel(AnalysisPanelBase):
    """Aceleración: velocidad (X) contra G longitudinal POSITIVA (Y) —
    solo tracción, sin frenada. El envelope cae con la velocidad (límite
    de potencia + arrastre) y el derate lo muerde en el tramo alto; las
    líneas de tendencia opcionales (lineal/cuadrática/exponencial) se
    ajustan por piloto sobre esa nube."""

    def _build(self) -> None:
        opts = QHBoxLayout()
        self.trend_check = QCheckBox("Trend lines")
        self.trend_check.setToolTip(
            "Fit one curve to the braking cloud and one to the traction "
            "cloud, per driver")
        self.trend_check.toggled.connect(self._options_changed)
        opts.addWidget(self.trend_check)
        self.trend_combo = QComboBox()
        for label, kind in (("Linear", "linear"),
                            ("Quadratic", "quadratic"),
                            ("Exponential", "exponential")):
            self.trend_combo.addItem(label, kind)
        self.trend_combo.currentIndexChanged.connect(self._options_changed)
        opts.addWidget(self.trend_combo)
        opts.addStretch(1)
        self.body.addLayout(opts)
        self.plot = pg.PlotWidget()
        self.plot.setMenuEnabled(False)
        self.plot.setToolTip(AXIS_HINT)
        self.plot.setLabel("bottom", "Speed (km/h)")
        self.plot.setLabel("left", "Acceleration (longitudinal G)")
        self.plot.showGrid(x=True, y=True, alpha=theme.GRID_ALPHA)
        self.plot.addItem(pg.InfiniteLine(
            angle=0, pen=pg.mkPen("#3a3f4a", width=1, style=Qt.DashLine)))
        self.body.addWidget(self.plot)
        self._items: list = []

    def _options_changed(self, *_a) -> None:
        self._sig = None
        self.refresh()

    def _clear(self) -> None:
        for item in self._items:
            self.plot.removeItem(item)
        self._items = []

    def refresh(self) -> None:  # las opciones propias entran en la firma
        zones = self.engine.zones()
        self.controls.update_zones(zones)
        total_n = sum(self.hub.buffers[d].n for d in self.controls.drivers()
                      if d in self.hub.buffers)
        sig = (self.controls.sig(), len(zones), total_n,
               self.trend_check.isChecked(), self.trend_combo.currentData())
        if sig == self._sig:
            return
        self._sig = sig
        self._refresh_impl(zones)

    def _refresh_impl(self, zones: list[Zone]) -> None:
        self._clear()
        lo, hi = self.controls.lap_range()
        per_lap = self.controls.mode() == "lap"
        lines = []
        table_rows: list[list] = []
        for drv in self.controls.drivers():
            chan = self.engine.channels(drv)
            if chan is None:
                continue
            lap = chan["lap"]
            mask = (lap >= lo) & (chan["v"] > 5.0) & ~chan["guard"]
            if hi is not None:
                mask &= lap <= hi
            mask &= lap < self.hub.buffers[drv].current_lap()
            mask &= self.engine.zone_mask(chan["dist"],
                                          self.controls.selector())
            # SOLO aceleración: la frenada queda fuera de este panel
            mask &= chan["a_lon"] > 0.0
            n = int(mask.sum())
            if n < 20:
                continue
            x = chan["v"][mask] * 3.6
            y = chan["a_lon"][mask]
            stride = max(1, n // 6000)
            color = QColor(self._color(drv))
            color.setAlpha(80)
            scatter = pg.ScatterPlotItem(
                x=x[::stride], y=y[::stride], size=4,
                brush=pg.mkBrush(color), pen=None)
            self.plot.addItem(scatter)
            self._items.append(scatter)
            if self.trend_check.isChecked():
                fit = fit_trend(x, y, self.trend_combo.currentData())
                if fit is not None:
                    curve = pg.PlotDataItem(
                        fit[0], fit[1],
                        pen=pg.mkPen(self._color(drv), width=2,
                                     style=Qt.DashLine))
                    self.plot.addItem(curve)
                    self._items.append(curve)
            i_pk = int(np.argmax(y))
            lines.append(
                f"{self._code(drv)}: {float(y.max()):.1f}G accel @ "
                f"{float(x[i_pk]):.0f} km/h")
            if per_lap:
                laps_arr = lap[mask]
                for l in np.unique(laps_arr):
                    sel = laps_arr == l
                    table_rows.append(
                        [self._code(drv), int(l),
                         f"{float(y[sel].max()):.2f}",
                         f"{float(x[sel][np.argmax(y[sel])]):.0f}",
                         f"{float(y[sel].mean()):.2f}"])
            else:
                table_rows.append(
                    [self._code(drv), "range", f"{float(y.max()):.2f}",
                     f"{float(x[i_pk]):.0f}", f"{float(y.mean()):.2f}"])
        self._fill_table(
            ["Driver", "Lap", "Peak accel G", "V @ peak", "Mean accel G"],
            table_rows)
        self.summary.setText("  |  ".join(lines) if lines else
                             "No complete laps in range yet")


class GripDegPanel(AnalysisPanelBase):
    """Degradación medida: pico de G lateral y velocidad mínima por curva,
    vuelta a vuelta (vueltas de boxes y neutralizadas excluidas)."""

    corners_only = True

    def _build(self) -> None:
        row, self.trend_check, self.trend_combo = _trend_row(self._dirty)
        self.body.addLayout(row)
        self.glw = pg.GraphicsLayoutWidget()
        self.glw.setToolTip(AXIS_HINT)
        self.p_g = self.glw.addPlot(row=0, col=0)
        self.p_v = self.glw.addPlot(row=1, col=0)
        self.p_v.setXLink(self.p_g)
        self.p_g.setLabel("left", "Peak lateral G")
        self.p_v.setLabel("left", "V min (km/h)")
        self.p_v.setLabel("bottom", "Lap")
        for plot in (self.p_g, self.p_v):
            plot.showGrid(x=True, y=True, alpha=theme.GRID_ALPHA)
        self.body.addWidget(self.glw)

    def _clear(self) -> None:
        for plot in (self.p_g, self.p_v):
            plot.clear()

    def _add_trends(self, xs, g_vals, v_vals, color) -> None:
        kind = self.trend_combo.currentData()
        for plot, ys in ((self.p_g, g_vals), (self.p_v, v_vals)):
            fit = fit_trend(np.array(xs, float), np.array(ys, float), kind)
            if fit is not None:
                plot.plot(fit[0], fit[1],
                          pen=pg.mkPen(color, width=2, style=Qt.DashLine))

    def _refresh_impl(self, zones: list[Zone]) -> None:
        self._clear()
        allowed = self.controls.zone_btn.allowed(zones)
        corner_ids = [i for i, z in enumerate(zones)
                      if z.kind == "corner" and i in allowed]
        mode = self.controls.mode()
        lines = []
        table_rows: list[list] = []
        bars_g: dict[str, float] = {}
        bars_v: dict[str, float] = {}
        for drv in self.controls.drivers():
            xs, g_vals, v_vals = [], [], []
            for lap in self._laps_for(drv):
                m = self.engine.lap_metrics(drv, lap)
                if m is None or m.pit or m.caution:
                    continue
                gs = [m.corners[zi][1] for zi in corner_ids
                      if zi in m.corners]
                vs = [m.corners[zi][0] for zi in corner_ids
                      if zi in m.corners]
                if not gs:
                    continue
                xs.append(lap)
                g_vals.append(float(np.mean(gs)))
                v_vals.append(float(np.mean(vs)) * 3.6)
                if mode == "lap":
                    table_rows.append(
                        [self._code(drv), lap, f"{g_vals[-1]:.2f}",
                         f"{v_vals[-1]:.0f}"])
            if not xs:
                continue
            slope = (float(np.polyfit(xs, g_vals, 1)[0])
                     if len(xs) >= 4 else float("nan"))
            if mode == "lap":
                pen = pg.mkPen(self._color(drv), width=2)
                self.p_g.plot(xs, g_vals, pen=pen, symbol="o", symbolSize=4,
                              symbolBrush=self._color(drv), symbolPen=None)
                self.p_v.plot(xs, v_vals, pen=pen, symbol="o", symbolSize=4,
                              symbolBrush=self._color(drv), symbolPen=None)
                if self.trend_check.isChecked():
                    self._add_trends(xs, g_vals, v_vals, self._color(drv))
            else:
                bars_g[drv] = float(np.mean(g_vals))
                bars_v[drv] = float(np.mean(v_vals))
                table_rows.append(
                    [self._code(drv), len(xs), f"{np.mean(g_vals):.2f}",
                     f"{np.mean(v_vals):.0f}",
                     "—" if slope != slope else f"{slope:+.3f}",
                     f"{g_vals[-1] - g_vals[0]:+.2f}"])
            if slope == slope:
                lines.append(f"{self._code(drv)}: {slope:+.3f} g/lap")
        if mode == "total" and bars_g:
            self._driver_bars(self.p_g, bars_g)
            self._driver_bars(self.p_v, bars_v)
            self.p_v.setLabel("bottom", "Driver (avg)")
            self._fill_table(
                ["Driver", "Laps", "Avg peak G", "Avg Vmin",
                 "Slope g/lap", "ΔG first→last"], table_rows)
        else:
            self._reset_ticks(self.p_g, "")
            self._reset_ticks(self.p_v, "Lap")
            self._fill_table(
                ["Driver", "Lap", "Peak lat G", "Vmin km/h"], table_rows)
        label = ("selected corners" if len(corner_ids) != sum(
            1 for z in zones if z.kind == "corner") else "all corners")
        self.summary.setText(
            f"Grip trend ({label}): " + "  |  ".join(lines) if lines
            else "Need 4+ clean laps per driver for a trend")
