"""Modo Tiempos / Gap: gráfico de gap contra un piloto de referencia
(X = tiempo de sesión, Y = segundos) y tablas comparativas de tiempos
por vuelta, sector y microsector.
"""
from __future__ import annotations

import math
import time

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QComboBox, QHBoxLayout, QLabel, QTableWidget, QTableWidgetItem,
    QTabWidget, QVBoxLayout, QWidget,
)

from ..hub import DataHub
from ..timing import N_MICRO, TimingAnalyzer
from . import theme
from .charts import DoubleClickHider, EdgeSmoother, HoverProbe, legend_set_dim, series_pens

_BEST_COLOR = "#c9a1ff"  # mejor tiempo (violeta, convención F1)

# estados de los microsectores oficiales (feed TimingData → Segments)
_SEG_COLORS = {
    2051: QColor(178, 132, 232),  # violeta: mejor de la sesión
    2049: QColor(46, 158, 91),    # verde: mejor personal
    2048: QColor(214, 190, 60),   # amarillo: completado sin mejora
    2064: QColor(74, 127, 212),   # azul: pit lane
}
_SEG_UNKNOWN = QColor(120, 120, 120, 110)   # código no mapeado
_SEG_EMPTY = QColor(255, 255, 255, 16)      # aún no cruzado

# tinte de fondo por compuesto de neumático (convención Pirelli)
_COMPOUND_BG = {
    "SOFT": QColor(225, 6, 0, 52),
    "MEDIUM": QColor(255, 209, 46, 45),
    "HARD": QColor(240, 240, 240, 36),
    "INTERMEDIATE": QColor(67, 176, 42, 48),
    "WET": QColor(0, 103, 173, 58),
}


def fmt_laptime(sec: float) -> str:
    if not math.isfinite(sec) or sec <= 0:
        return "—"
    m, s = divmod(sec, 60.0)
    return f"{int(m)}:{s:06.3f}"


def fmt_secs(sec: float) -> str:
    return f"{sec:.3f}" if math.isfinite(sec) else "—"


def fmt_gap(sec: float) -> str:
    return f"{sec:+.3f}" if math.isfinite(sec) else "—"


def _delta_bg(delta: float) -> QColor | None:
    """Fondo diverging para deltas: verde = más rápido, rojo = más lento.
    El valor numérico siempre está en la celda (el color es redundante)."""
    if not math.isfinite(delta) or abs(delta) < 0.03:
        return None
    a = min(abs(delta) / 0.5, 1.0)
    alpha = int(40 + 130 * a)
    return QColor(38, 148, 92, alpha) if delta < 0 else QColor(206, 82, 70, alpha)


def _cell(text: str, bg: QColor | None = None, fg: str | None = None,
          bold: bool = False) -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    item.setFlags(Qt.ItemIsEnabled)
    item.setTextAlignment(Qt.AlignCenter)
    if bg is not None:
        item.setBackground(bg)
    if fg is not None:
        item.setForeground(QColor(fg))
    if bold:
        font = item.font()
        font.setBold(True)
        item.setFont(font)
    return item


class DistanceAxis(pg.AxisItem):
    """Eje de posición en pista: distancia total y 'V<vuelta> +<metros>'."""

    def __init__(self, hub: DataHub, **kwargs):
        super().__init__(**kwargs)
        self._hub = hub

    def tickStrings(self, values, scale, spacing):
        L = self._hub.track_length
        out = []
        for v in values:
            if v < 0 or L <= 0:
                out.append(f"{v:,.0f} m")
                continue
            lap = int(v // L) + 1
            rem = v - (lap - 1) * L
            out.append(f"{v:,.0f} m\nL{lap} +{rem:,.0f}")
        return out


class TimingView(QWidget):
    """Implementa la misma interfaz que los gráficos (refresh/set_selected/
    set_channel/clear_data) para enchufarse al stack de modos."""

    def __init__(self, hub: DataHub, cfg: dict | None = None, parent=None):
        super().__init__(parent)
        self.hub = hub
        self.cfg = cfg
        self.analyzer = TimingAnalyzer(hub)
        self.selected: list[str] = []
        self.curves: dict[str, pg.PlotDataItem] = {}
        self._last_gap: dict[str, float] = {}
        self._edges: dict[str, float] = {}
        self._laps_signature = None
        self._lap_lines: dict[int, pg.InfiniteLine] = {}
        self._lap_lines_L = 0.0
        self._legend_ref: str | None = None
        self._smoother = EdgeSmoother()
        self._status_items: list[pg.LinearRegionItem] = []
        self._status_sig = None
        self._tick = 0
        self._window = float((cfg or {}).get("ui", {}).get("gap_window_laps", 0.0))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        top = QHBoxLayout()
        top.addWidget(QLabel("Reference:"))
        self.ref_combo = QComboBox()
        self.ref_combo.setMinimumWidth(160)
        top.addWidget(self.ref_combo)
        top.addSpacing(16)
        top.addStretch(1)
        note = QLabel("live µsectors · ~±0.1 s (?)")
        note.setStyleSheet(f"color: {theme.TEXT_MUTED};")
        note.setToolTip(
            "Sectors and microsectors by distance (24 equal splits of the lap), computed\n"
            "live over the current lap; dimmed values come from exactly one lap ago.\n"
            "Accuracy ~±0.1 s — not official timing."
        )
        top.addWidget(note)
        self._note = note
        self._note_official = False
        layout.addLayout(top)

        self.plot = pg.PlotWidget(axisItems={"bottom": DistanceAxis(hub, orientation="bottom")})
        self.plot.setMenuEnabled(False)
        self.plot.showGrid(x=True, y=True, alpha=theme.GRID_ALPHA)
        self.plot.getAxis("bottom").setLabel("Track position (total distance · lap +meters)")
        self.plot.getAxis("bottom").setHeight(46)  # ticks de dos líneas
        self.plot.getAxis("left").setLabel("Gap to reference (s) · + behind / − ahead")
        self.legend = self.plot.addLegend(offset=(10, 10), labelTextColor=theme.TEXT)
        self.zero_line = pg.InfiniteLine(
            pos=0.0, angle=0, pen=pg.mkPen(theme.TEXT_MUTED, width=1, style=Qt.DashLine)
        )
        self.plot.addItem(self.zero_line)
        # con ventana activa, el rango Y se ajusta solo a los datos visibles
        self.plot.getViewBox().setAutoVisible(y=True)
        self._probe = HoverProbe(
            self.plot, self._hover_series,
            x_format=self._fmt_pos,
            y_format=lambda y: f"{y:+.3f} s",
            hover_cb=self._probe_hover,
        )
        self.hover_dist_cb = None
        self._track_marker = pg.InfiniteLine(
            angle=90, movable=False,
            pen=pg.mkPen(theme.ACCENT, width=1, style=Qt.DashLine),
        )
        self._track_marker.setZValue(45)
        self._track_marker.setVisible(False)
        self.plot.addItem(self._track_marker, ignoreBounds=True)
        self.hider = DoubleClickHider(
            self.plot, lambda: dict(self.curves), self._on_hidden_changed
        )
        layout.addWidget(self.plot, stretch=3)

        self.tabs = QTabWidget()
        self.summary_table = QTableWidget(0, 8)
        self.summary_table.setHorizontalHeaderLabels(
            ["Driver", "Lap", "Last", "Best", "S1", "S2", "S3", "Gap"]
        )
        self.summary_table.verticalHeader().setVisible(True)  # posición en pista
        self.laps_table = QTableWidget(0, 0)
        self.micro_table = QTableWidget(0, N_MICRO)
        self.micro_table.setHorizontalHeaderLabels([f"µ{i + 1}" for i in range(N_MICRO)])
        for table in (self.summary_table, self.laps_table, self.micro_table):
            table.setEditTriggers(QTableWidget.NoEditTriggers)
            table.setSelectionMode(QTableWidget.NoSelection)
            table.setAlternatingRowColors(True)
        for c in range(N_MICRO):
            self.micro_table.setColumnWidth(c, 60)  # "+0.123" entra sin cortar
        self.segments_table = QTableWidget(0, 0)
        self.segments_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.segments_table.setSelectionMode(QTableWidget.NoSelection)
        self.segments_table.setToolTip(
            "Official mini-sector status from F1 live timing (the colored dashes of\n"
            "the official app): purple = session best, green = personal best,\n"
            "yellow = completed without improving, blue = pit lane.\n"
            "Only the Live and Capture sources carry this feed."
        )
        self._seg_layout: tuple | None = None
        self.corners_table = QTableWidget(0, 0)
        self.corners_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.corners_table.setSelectionMode(QTableWidget.NoSelection)
        self.corners_table.setAlternatingRowColors(True)
        self.corners_table.setToolTip(
            "Minimum speed (km/h) at each circuit corner (±60 m around the apex);\n"
            "color vs the reference: green = faster through. Dimmed = previous lap."
        )
        self.deg_plot = pg.PlotWidget()
        self.deg_plot.setMenuEnabled(False)
        self.deg_plot.showGrid(x=True, y=True, alpha=theme.GRID_ALPHA)
        self.deg_plot.getAxis("bottom").setLabel("Tyre age (laps)")
        self.deg_plot.getAxis("left").setLabel("Lap time (s)")
        self.deg_legend = self.deg_plot.addLegend(offset=(10, 10), labelTextColor=theme.TEXT)
        self._deg_curves: list = []
        self.stint_table = QTableWidget(0, 6)
        self.stint_table.setHorizontalHeaderLabels(
            ["Driver", "Stint", "Comp.", "Laps", "Avg pace", "Deg (s/lap)"]
        )
        self.stint_table.horizontalHeaderItem(5).setToolTip(
            "Slope of the linear fit of lap time vs tyre age"
        )
        self.stint_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.stint_table.setSelectionMode(QTableWidget.NoSelection)
        self.stint_table.setAlternatingRowColors(True)
        self.stint_table.verticalHeader().setVisible(False)
        deg_container = QWidget()
        deg_lay = QVBoxLayout(deg_container)
        deg_lay.setContentsMargins(0, 0, 0, 0)
        deg_lay.addWidget(self.deg_plot, stretch=3)
        deg_lay.addWidget(self.stint_table, stretch=2)
        self._deg_container = deg_container
        self.tabs.addTab(self.summary_table, "Summary")
        self.tabs.addTab(self.laps_table, "By lap")
        self.tabs.addTab(self.micro_table, "Microsectors")
        self.tabs.addTab(self.segments_table, "Official µ")
        self.tabs.addTab(self.corners_table, "Corners")
        self.tabs.addTab(self._deg_container, "Degradation")
        # las tablas viven en su PROPIO panel del catálogo (Data tables),
        # que adopta self.tabs; acá queda solo el gráfico de gap. El panel
        # externo fija este filtro: (pilotos|None, vuelta_desde, hasta|None)
        self.table_filter: tuple = (None, 1, None)

        hub.driversChanged.connect(self._on_drivers_changed)

    # ------------------------------------------------- interfaz de "chart"

    def set_channel(self, channel: str) -> None:  # el gap no depende del canal
        pass

    def set_peaks_enabled(self, on: bool) -> None:  # sin picos en el gap
        pass

    def set_selected(self, drivers: list[str]) -> None:
        self.selected = list(drivers)
        for drv in list(self.curves):
            if drv not in drivers:
                curve = self.curves.pop(drv)
                self.legend.removeItem(curve)
                self.plot.removeItem(curve)
        for drv in list(self._edges):
            if drv not in drivers:
                self._edges.pop(drv, None)
        for drv in drivers:
            if drv not in self.curves:
                curve = pg.PlotDataItem(connect="finite")
                curve.setClipToView(True)
                curve.setDownsampling(auto=True, method="peak")
                self.plot.addItem(curve)
                self.legend.addItem(curve, self._code_of(drv))
                self.curves[drv] = curve
        self._restyle()
        self._rebuild_ref_combo()
        self.hider.prune(self.curves.keys())
        self._laps_signature = None
        self._legend_ref = None  # la leyenda se rearma: volver a marcar la ref

    def clear_data(self) -> None:
        self.analyzer.clear()
        self._last_gap.clear()
        self._edges.clear()
        self._smoother.reset()
        self._laps_signature = None
        self._legend_ref = None
        for line in self._lap_lines.values():
            self.plot.removeItem(line)
        self._lap_lines.clear()
        for item in self._status_items:
            self.plot.removeItem(item)
        self._status_items = []
        self._status_sig = None
        for curve in self._deg_curves:
            self.deg_legend.removeItem(curve)
            self.deg_plot.removeItem(curve)
        self._deg_curves = []
        for curve in self.curves.values():
            curve.setData([], [])
        self.summary_table.setRowCount(0)
        self.laps_table.setRowCount(0)
        self.laps_table.setColumnCount(0)
        self.micro_table.setRowCount(0)
        self.segments_table.setRowCount(0)
        self.segments_table.setColumnCount(0)
        self._seg_layout = None

    def refresh(self) -> None:
        if not self.selected:
            return
        ref = self.ref_combo.currentData() or self.selected[0]
        self._tick += 1
        # las series de gap se recalculan escalonadas (a 30 fps no hace falta
        # cada tick); con muchos pilotos la cadencia por serie baja más
        stagger = 15 if len(self.selected) > 6 else 3
        for i, drv in enumerate(self.selected):
            curve = self.curves[drv]
            if drv == ref:
                pt = self.analyzer.position_time(drv)
                if pt is not None:
                    pos = pt[0]
                    curve.setData([float(pos[0]), float(pos[-1])], [0.0, 0.0])
                    self._edges[drv] = float(pos[-1])
                self._last_gap[drv] = 0.0
                continue
            if (self._tick + i) % stagger:
                continue
            series = self.analyzer.gap_series(drv, ref)
            if series is None:
                curve.setData([], [])
                self._last_gap[drv] = float("nan")
                self._edges.pop(drv, None)
                continue
            x, y = series
            curve.setData(x, y)
            self._last_gap[drv] = float(y[-1])
            self._edges[drv] = float(x[-1])
        edge = None
        for drv in self.selected:  # la ventana sigue solo a las series visibles
            if drv in self._edges and self.curves[drv].isVisible():
                edge = max(edge or -math.inf, self._edges[drv])
        if self._window > 0 and edge is not None:
            edge = self._smoother.update(edge, time.monotonic())
            width = self._window * self.hub.track_length
            self.plot.setXRange(edge - width, edge, padding=0)
        if ref != self._legend_ref:
            self._legend_ref = ref
            self._mark_legend_ref(ref)
        self._update_lap_lines()
        if self._tick % 15 == 0:  # tablas a 2 Hz, solo la pestaña visible
            self._update_status_regions(ref)
            self.refresh_tables(ref)

    def refresh_tables(self, ref: str | None = None) -> None:
        """Refresca la pestaña de tablas visible (las tablas viven en el
        panel Data tables; su filtro puede poblarlas aunque el gráfico no
        tenga selección propia)."""
        population = self._by_track_position()
        if not population:
            return
        if ref is None:
            ref = self.ref_combo.currentData() or (
                self.selected[0] if self.selected else population[0])
        self._update_note()
        # despacho por widget (no por índice): el panel Data tables puede
        # anteponer pestañas propias sin romper el mapeo
        tab = self.tabs.currentWidget()
        if tab is self.summary_table:
            self._update_summary(ref)
        elif tab is self.laps_table:
            self._update_laps_table()
        elif tab is self.micro_table:
            self._update_micro(ref)
        elif tab is self.segments_table:
            self._update_segments()
        elif tab is self.corners_table:
            self._update_corners(ref)
        elif tab is self._deg_container:
            self._update_degradation()

    # -------------------------------------------------------------- interno

    def set_window_laps(self, laps: float) -> None:
        """Ventana X en vueltas (0 = toda la sesión); la maneja el selector
        global de la ventana principal."""
        self._window = max(0.0, float(laps))
        if self._window <= 0:
            self.plot.enableAutoRange(axis="x")

    def _probe_hover(self, x: float | None) -> None:
        if self.hover_dist_cb is None:
            return
        L = max(self.hub.track_length, 1e-9)
        self.hover_dist_cb(None if x is None else float(x) % L)

    def show_track_marker(self, dist: float | None) -> None:
        if dist is None:
            self._track_marker.setVisible(False)
            return
        L = self.hub.track_length
        if L <= 0:
            self._track_marker.setVisible(False)
            return
        x0, x1 = self.plot.getViewBox().viewRange()[0]
        x = (math.floor((x0 - float(dist)) / L) + 1) * L + float(dist)
        if x0 <= x <= x1:
            self._track_marker.setPos(x)
            self._track_marker.setVisible(True)
        else:
            self._track_marker.setVisible(False)

    def _fmt_pos(self, x: float) -> str:
        L = self.hub.track_length
        if x < 0 or L <= 0:
            return f"{x:,.0f} m"
        lap = int(x // L) + 1
        rem = x - (lap - 1) * L
        return f"{x:,.0f} m · L{lap} +{rem:,.0f} m"

    def _by_track_position(self) -> list[str]:
        """Pilotos de las TABLAS ordenados por posición en pista (1ro
        primero). Con filtro del panel Data tables, esa selección manda;
        sin él, la selección del gráfico."""
        flt = self.table_filter[0]
        drivers = ([d for d in flt if d in self.hub.buffers]
                   if flt else self.selected)

        def pos(drv: str) -> float:
            pt = self.analyzer.position_time(drv)
            return float(pt[0][-1]) if pt is not None else -math.inf

        return sorted(drivers, key=pos, reverse=True)

    def _mark_legend_ref(self, ref: str) -> None:
        for sample, label in self.legend.items:
            curve = getattr(sample, "item", None)
            for drv, c in self.curves.items():
                if c is curve:
                    label.setText(self._code_of(drv) + (" (ref)" if drv == ref else ""))
                    break

    def _update_lap_lines(self) -> None:
        """Línea vertical en cada corte de vuelta (múltiplos del largo)."""
        L = self.hub.track_length
        if abs(L - self._lap_lines_L) > 1.0:  # cambió el largo: reposicionar todo
            for line in self._lap_lines.values():
                self.plot.removeItem(line)
            self._lap_lines.clear()
            self._lap_lines_L = L
        x0, x1 = self.plot.getViewBox().viewRange()[0]
        want: set[int] = set()
        if L > 0 and (x1 - x0) / L <= 150:
            want = set(range(max(1, math.ceil(x0 / L)), int(x1 // L) + 1))
        for k in list(self._lap_lines):
            if k not in want:
                self.plot.removeItem(self._lap_lines.pop(k))
        for k in want:
            if k not in self._lap_lines:
                line = pg.InfiniteLine(
                    pos=k * L, angle=90, movable=False,
                    pen=pg.mkPen(theme.BORDER, width=1),
                    label=f"L{k + 1}",
                    labelOpts={"position": 0.96, "color": theme.TEXT_MUTED,
                               "fill": pg.mkBrush(theme.SURFACE + "c0")},
                )
                line.setZValue(-5)
                self.plot.addItem(line, ignoreBounds=True)
                self._lap_lines[k] = line

    def _code_of(self, drv: str) -> str:
        info = self.hub.drivers.get(drv)
        return info.code if info else drv

    def _hover_series(self):
        ref = self.ref_combo.currentData() or (self.selected[0] if self.selected else None)
        out = []
        for drv in self.selected:
            curve = self.curves.get(drv)
            if curve is None or not curve.isVisible():
                continue
            xd, yd = curve.getData()
            info = self.hub.drivers.get(drv)
            label = self._code_of(drv) + (" (ref)" if drv == ref else "")
            out.append((label, info.color if info else "#9aa0a6", xd, yd))
        return out

    def _on_hidden_changed(self, hidden: set) -> None:
        for drv, curve in self.curves.items():
            legend_set_dim(self.legend, curve, drv in hidden)

    def _on_drivers_changed(self) -> None:
        self._restyle()
        self._rebuild_ref_combo()

    def _restyle(self) -> None:
        # con muchas series el antialiasing domina el costo de pintado
        antialias = len(self.selected) <= 8
        for drv, pen in series_pens(self.hub, self.selected).items():
            if drv in self.curves:
                self.curves[drv].opts["antialias"] = antialias
                self.curves[drv].setPen(pen)

    def _rebuild_ref_combo(self) -> None:
        current = self.ref_combo.currentData()
        self.ref_combo.blockSignals(True)
        self.ref_combo.clear()
        for drv in self.selected:
            info = self.hub.drivers.get(drv)
            self.ref_combo.addItem(info.label if info else drv, drv)
        idx = self.ref_combo.findData(current)
        if idx >= 0:
            self.ref_combo.setCurrentIndex(idx)
        self.ref_combo.blockSignals(False)

    def _update_summary(self, ref: str) -> None:
        an = self.analyzer
        rows = []
        best_overall = math.inf
        ordered = self._by_track_position()
        # rango de vueltas del panel Data tables: Last/Best/S1-3 DENTRO
        # del rango; sin rango, el comportamiento clásico (rodante)
        lo, hi = int(self.table_filter[1]), self.table_filter[2]
        ranged = lo > 1 or hi is not None
        for drv in ordered:
            buf = self.hub.buffers.get(drv)
            cur_lap = buf.current_lap() if buf else 0
            if ranged:
                laps = [l for l in (buf.completed_laps() if buf else [])
                        if l >= lo and (hi is None or l <= hi)]
                last = laps[-1] if laps else 0
                last_time = an.lap_time(drv, last) if last else float("nan")
                best = None
                sec_best = [math.inf] * 3
                for l in laps:
                    lt = an.lap_time(drv, l)
                    if math.isfinite(lt) and (best is None or lt < best[1]):
                        best = (l, lt)
                    st = an.sector_times(drv, l)
                    for k in range(3):
                        if math.isfinite(float(st[k])):
                            sec_best[k] = min(sec_best[k], float(st[k]))
                sectors = (
                    [t if math.isfinite(t) else float("nan")
                     for t in sec_best],
                    None,  # mejores del rango: sin atenuar por vuelta
                )
            else:
                last = an.last_completed_lap(drv)
                last_time = an.lap_time(drv, last) if last else float("nan")
                best = an.best_lap(drv)
                sectors = an.latest_sector_times(drv)  # rodantes
            if best and best[1] < best_overall:
                best_overall = best[1]
            rows.append((drv, cur_lap, last_time, best, sectors))

        self.summary_table.setRowCount(len(rows))
        for r, (drv, cur_lap, last_time, best, sectors) in enumerate(rows):
            self.summary_table.setItem(r, 0, _cell(self._code_of(drv), bold=True))
            self.summary_table.setItem(r, 1, _cell(str(cur_lap)))
            self.summary_table.setItem(r, 2, _cell(fmt_laptime(last_time)))
            if best:
                is_best = abs(best[1] - best_overall) < 1e-9
                self.summary_table.setItem(
                    r, 3, _cell(f"{fmt_laptime(best[1])} (L{best[0]})",
                                fg=_BEST_COLOR if is_best else None, bold=is_best)
                )
            else:
                self.summary_table.setItem(r, 3, _cell("—"))
            for k in range(3):
                if sectors is None:
                    self.summary_table.setItem(r, 4 + k, _cell("—"))
                else:
                    times, laps = sectors
                    # laps None = mejores del rango (no hay "vuelta vieja")
                    dim = laps is not None and int(laps[k]) != cur_lap
                    self.summary_table.setItem(
                        r, 4 + k,
                        _cell(fmt_secs(float(times[k])),
                              fg=theme.TEXT_MUTED if dim else None),
                    )
            gap = self._last_gap.get(drv, float("nan"))
            self.summary_table.setItem(
                r, 7, _cell("ref" if drv == ref else fmt_gap(gap), bold=(drv == ref))
            )
        self.summary_table.setVerticalHeaderLabels(
            [f"P{i + 1}" for i in range(len(rows))]
        )

    def _update_laps_table(self) -> None:
        an = self.analyzer
        ordered = self._by_track_position()
        completed = {drv: an.last_completed_lap(drv) or 0 for drv in ordered}
        signature = (tuple(ordered), tuple(sorted(completed.items())),
                     round(self.hub.track_length), self.table_filter[1:])
        if signature == self._laps_signature:
            return
        self._laps_signature = signature
        lap_lo = max(1, int(self.table_filter[1]))
        max_lap = max(completed.values(), default=0)
        if self.table_filter[2] is not None:
            max_lap = min(max_lap, int(self.table_filter[2]))
        n_rows = max(0, max_lap - lap_lo + 1)
        self.laps_table.setColumnCount(len(ordered))
        self.laps_table.setHorizontalHeaderLabels([self._code_of(d) for d in ordered])
        self.laps_table.setRowCount(n_rows)
        self.laps_table.setVerticalHeaderLabels(
            [f"L{n}" for n in range(lap_lo, max_lap + 1)])
        for lap in range(lap_lo, max_lap + 1):
            times = [an.lap_time(drv, lap) if lap <= completed[drv] else float("nan")
                     for drv in ordered]
            finite = [t for t in times if math.isfinite(t)]
            row_best = min(finite) if finite else math.inf
            for c, t in enumerate(times):
                drv = ordered[c]
                is_best = math.isfinite(t) and abs(t - row_best) < 1e-9 and len(finite) > 1
                text = fmt_laptime(t)
                tyre = self.hub.tyres.get(drv, {}).get(lap)
                if tyre and tyre[1] and math.isfinite(t):
                    text += f" ({tyre[1]})"  # edad del neumático en vueltas
                pit = any(p_lap == lap for p_lap, _t in self.hub.pits.get(drv, []))
                if pit and math.isfinite(t):
                    text += " P"
                item = _cell(text, fg=_BEST_COLOR if is_best else None, bold=is_best)
                tooltip = []
                if tyre and tyre[0]:
                    bg = _COMPOUND_BG.get(tyre[0].upper())
                    if bg is not None:
                        item.setBackground(bg)
                    tooltip.append(f"{tyre[0]} · {tyre[1]} laps old")
                if pit:
                    tooltip.append("Pit stop on this lap")
                if tooltip:
                    item.setToolTip("\n".join(tooltip))
                self.laps_table.setItem(lap - lap_lo, c, item)
        self.laps_table.scrollToBottom()

    def _update_status_regions(self, ref: str) -> None:
        """Bandas de bandera/SC en el gráfico de gap (X = posición): cada
        período de tiempo se traduce a posición vía la referencia."""
        periods = self.hub.track_status
        pt = self.analyzer.position_time(ref) if periods else None
        if not periods or pt is None:
            for item in self._status_items:
                self.plot.removeItem(item)
            self._status_items = []
            self._status_sig = None
            return
        pos_r, t_r = pt
        t_max = float(t_r[-1])
        sig = (tuple(periods), ref, int(t_max // 5))
        if sig == self._status_sig:
            return
        self._status_sig = sig
        for item in self._status_items:
            self.plot.removeItem(item)
        self._status_items = []
        for t0, t1, code in periods:
            if t0 > t_max:
                continue
            x0 = float(np.interp(t0, t_r, pos_r))
            x1 = float(np.interp(min(t1, t_max), t_r, pos_r))
            color = QColor(theme.TRACK_STATUS.get(code, ("", "#888888"))[1])
            color.setAlpha(30)
            item = pg.LinearRegionItem(values=(x0, x1), movable=False,
                                       brush=pg.mkBrush(color), pen=pg.mkPen(None))
            item.setZValue(-20)
            self.plot.addItem(item)
            self._status_items.append(item)

    _COMPOUND_PEN = {
        "SOFT": "#e10600", "MEDIUM": "#ffd12e", "HARD": "#f0f0f0",
        "INTERMEDIATE": "#43b02a", "WET": "#0067ad",
    }
    _DEG_SYMBOLS = ["o", "s", "t", "d", "+", "x"]

    def _collect_stints(self, drv: str):
        """Stints del piloto: (compuesto, vueltas [l0, l1], puntos válidos
        (edad, tiempo) sin vueltas de entrada/salida de boxes)."""
        tyre_map = self.hub.tyres.get(drv)
        buf = self.hub.buffers.get(drv)
        if not tyre_map or buf is None or not buf.n:
            return []
        an = self.analyzer
        pit_laps = {p_lap for p_lap, _t in self.hub.pits.get(drv, [])}
        # rango de vueltas del panel Data tables: la degradación se
        # analiza SOLO dentro del rango (stints recortados a él)
        lo, hi = int(self.table_filter[1]), self.table_filter[2]
        laps = [l for l in buf.completed_laps()
                if l >= lo and (hi is None or l <= hi)]
        stints: list[dict] = []
        prev_age = None
        for lap in laps:
            compound, age = tyre_map.get(lap, ("", 0))
            if not compound:
                prev_age = None
                continue
            if prev_age is None or age <= prev_age or (stints and stints[-1]["comp"] != compound):
                stints.append({"comp": compound, "laps": [lap, lap], "pts": []})
            prev_age = age
            stints[-1]["laps"][1] = lap
            if lap in pit_laps or (lap - 1) in pit_laps:
                continue  # vueltas de entrada/salida distorsionan
            lap_time = an.lap_time(drv, lap)
            if math.isfinite(lap_time):
                stints[-1]["pts"].append((age, lap_time))
        return stints

    def _update_degradation(self) -> None:
        """Tiempo de vuelta vs edad del neumático: una serie por stint
        coloreada por compuesto, más la tabla resumen con ritmo promedio y
        pendiente de degradación (ajuste lineal, s/vuelta)."""
        for curve in self._deg_curves:
            self.deg_legend.removeItem(curve)
            self.deg_plot.removeItem(curve)
        self._deg_curves = []
        rows = []
        for di, drv in enumerate(self._by_track_position()):
            symbol = self._DEG_SYMBOLS[di % len(self._DEG_SYMBOLS)]
            for si, stint in enumerate(self._collect_stints(drv), start=1):
                points = stint["pts"]
                if len(points) < 2:
                    continue
                xs = [p[0] for p in points]
                ys = [p[1] for p in points]
                color = self._COMPOUND_PEN.get(stint["comp"].upper(), "#9aa0a6")
                curve = self.deg_plot.plot(
                    xs, ys, pen=pg.mkPen(color, width=1.5),
                    symbol=symbol, symbolSize=5,
                    symbolBrush=pg.mkBrush(color), symbolPen=pg.mkPen(None),
                    name=f"{self._code_of(drv)} · {stint['comp']}",
                )
                self._deg_curves.append(curve)
                slope = float("nan")
                if len(points) >= 3 and len(set(xs)) >= 2:
                    slope = float(np.polyfit(xs, ys, 1)[0])
                rows.append((drv, si, stint["comp"], stint["laps"],
                             float(np.mean(ys)), slope))
        self.stint_table.setRowCount(len(rows))
        for r, (drv, si, compound, laps_range, avg, slope) in enumerate(rows):
            info = self.hub.drivers.get(drv)
            color = info.color if info else "#9aa0a6"
            self.stint_table.setItem(r, 0, _cell(self._code_of(drv), fg=color, bold=True))
            self.stint_table.setItem(r, 1, _cell(str(si)))
            self.stint_table.setItem(r, 2, _cell(compound.capitalize()))
            self.stint_table.setItem(r, 3, _cell(f"L{laps_range[0]}-L{laps_range[1]}"))
            self.stint_table.setItem(r, 4, _cell(fmt_laptime(avg)))
            self.stint_table.setItem(
                r, 5, _cell(fmt_gap(slope), bg=_delta_bg(slope * 2.0))
            )

    def _update_corners(self, ref: str) -> None:
        """Velocidad mínima por curva real del circuito (rodante), coloreada
        contra la referencia: verde = pasa más rápido que la ref."""
        corners = self.hub.corners
        table = self.corners_table
        if not corners:
            table.setRowCount(0)
            table.setColumnCount(0)
            return
        labels = [c[0] for c in corners]
        dists = np.array([c[1] for c in corners], dtype=float)
        if table.columnCount() != len(labels):
            table.setColumnCount(len(labels))
            table.setHorizontalHeaderLabels(labels)
            for c in range(len(labels)):
                table.setColumnWidth(c, 56)
        an = self.analyzer
        ordered = self._by_track_position()
        ref_data = an.latest_corner_speeds(ref, dists)
        table.setRowCount(len(ordered))
        vlabels = []
        for r, drv in enumerate(ordered):
            buf = self.hub.buffers.get(drv)
            cur_lap = buf.current_lap() if buf else 0
            vlabels.append(f"{self._code_of(drv)} L{cur_lap}" if cur_lap else self._code_of(drv))
            data = an.latest_corner_speeds(drv, dists)
            for c in range(len(labels)):
                if data is None or not math.isfinite(data[0][c]):
                    table.setItem(r, c, _cell("—"))
                    continue
                v = float(data[0][c])
                dim = int(data[1][c]) != cur_lap
                bg = None
                if drv != ref and ref_data is not None and math.isfinite(ref_data[0][c]):
                    # 10 km/h de diferencia satura el color
                    bg = _delta_bg((float(ref_data[0][c]) - v) / 20.0)
                table.setItem(r, c, _cell(f"{v:.0f}", bg=bg,
                                          fg=theme.TEXT_MUTED if dim else None))
        table.setVerticalHeaderLabels(vlabels)

    def _update_micro(self, ref: str) -> None:
        """µsectores rodantes: cada celda es el último cruce de ese tramo —
        de la vuelta en curso si ya pasó (en tiempo real), si no de una
        vuelta atrás (atenuada). El último µsector completado de cada piloto
        se resalta en color acento, negrita y subrayado."""
        an = self.analyzer
        n_micro = an.n_micro()
        if self.micro_table.columnCount() != n_micro:
            # la cantidad de µ es configurable (panel Microsectors)
            self.micro_table.setColumnCount(n_micro)
            self.micro_table.setHorizontalHeaderLabels(
                [f"µ{i + 1}" for i in range(n_micro)])
            for c in range(n_micro):
                self.micro_table.setColumnWidth(c, 60)
        ref_data = an.latest_micro_times(ref)
        ref_micro = ref_data[0] if ref_data is not None else None
        ordered = self._by_track_position()
        self.micro_table.setRowCount(len(ordered))
        labels = []
        for r, drv in enumerate(ordered):
            buf = self.hub.buffers.get(drv)
            cur_lap = buf.current_lap() if buf else 0
            labels.append(f"{self._code_of(drv)} L{cur_lap}" if cur_lap else self._code_of(drv))
            data = an.latest_micro_times(drv)
            last_idx = None
            if data is not None:
                from_cur = np.nonzero(data[1] == cur_lap)[0]
                if len(from_cur):
                    last_idx = int(from_cur.max())
                elif bool(np.isfinite(data[0]).any()):
                    last_idx = n_micro - 1  # recién cruzó la meta: último µ previo
            for c in range(n_micro):
                if data is None or not math.isfinite(data[0][c]):
                    self.micro_table.setItem(r, c, _cell("—"))
                    continue
                micro, laps = data
                val = float(micro[c])
                dim = int(laps[c]) != cur_lap
                fg = theme.TEXT_MUTED if dim else None
                if drv == ref or ref_micro is None or not math.isfinite(ref_micro[c]):
                    item = _cell(fmt_secs(val), fg=fg)
                else:
                    delta = val - float(ref_micro[c])
                    item = _cell(fmt_gap(delta), bg=_delta_bg(delta), fg=fg)
                if c == last_idx:
                    font = item.font()
                    font.setBold(True)
                    font.setUnderline(True)
                    item.setFont(font)
                    item.setForeground(QColor(theme.ACCENT))
                    item.setToolTip("Most recent completed microsector")
                self.micro_table.setItem(r, c, item)
        self.micro_table.setVerticalHeaderLabels(labels)

    def _update_note(self) -> None:
        """La nota refleja si los sectores ya están anclados a los
        oficiales y si hay un rango de vueltas activo (panel Data
        tables)."""
        official = self.hub.sector_bounds is not None
        lo_hi = (int(self.table_filter[1]), self.table_filter[2])
        if (official, lo_hi) == self._note_official:
            return
        self._note_official = (official, lo_hi)
        if official:
            b1, b2 = self.hub.sector_bounds
            self._note.setText("official sectors (?)")
            self._note.setToolTip(
                "S1/S2/S3 are anchored to the REAL sector boundaries, located by\n"
                "crossing the official sector times with the telemetry\n"
                f"(S1 ends at {b1:,.0f} m, S2 at {b2:,.0f} m). As soon as the feed\n"
                "publishes each official sector/lap time (seconds after the\n"
                "crossing), tables show that exact value; interpolated ~±0.1 s\n"
                "values only cover what is not timed yet (the rolling current\n"
                "lap and microsectors). Dimmed = from exactly one lap ago."
            )
        else:
            self._note.setText("live µsectors · ~±0.1 s (?)")
            self._note.setToolTip(
                "Sectors and microsectors by distance (24 equal splits of the lap), computed\n"
                "live over the current lap; dimmed values come from exactly one lap ago.\n"
                "Accuracy ~±0.1 s — not official timing."
            )
        lo, hi = int(self.table_filter[1]), self.table_filter[2]
        if lo > 1 or hi is not None:
            # el rango del panel Data tables aplica donde las vueltas son
            # el dominio; las pestañas de estado actual quedan al margen
            self._note.setText(
                self._note.text()
                + f"  ·  laps {lo}–{hi if hi is not None else 'now'} "
                  "(All data, By lap, Summary, Degradation)")

    def _update_segments(self) -> None:
        """Microsectores oficiales del feed (rayitas de colores de la app de
        F1): una celda coloreada por segmento, filas en orden de pista."""
        table = self.segments_table
        counts = self.hub.segment_counts
        if not counts:
            table.setRowCount(0)
            table.setColumnCount(0)
            return
        secs = sorted(counts)
        columns = [(s, i) for s in secs for i in range(counts[s])]
        if self._seg_layout != tuple(columns):
            self._seg_layout = tuple(columns)
            table.setColumnCount(len(columns))
            table.setHorizontalHeaderLabels(
                [f"S{s + 1}" if i == 0 else "" for s, i in columns]
            )
            for c in range(len(columns)):
                table.setColumnWidth(c, 18)
        ordered = self._by_track_position()
        table.setRowCount(len(ordered))
        labels = []
        for r, drv in enumerate(ordered):
            buf = self.hub.buffers.get(drv)
            cur_lap = buf.current_lap() if buf else 0
            labels.append(f"{self._code_of(drv)} L{cur_lap}" if cur_lap else self._code_of(drv))
            state = self.hub.segments.get(drv, {})
            for c, key in enumerate(columns):
                status = int(state.get(key, 0))
                if status == 0:
                    bg = _SEG_EMPTY
                else:
                    bg = _SEG_COLORS.get(status, _SEG_UNKNOWN)
                item = _cell("", bg=bg)
                item.setToolTip(f"S{key[0] + 1} µ{key[1] + 1} · status {status}")
                table.setItem(r, c, item)
        table.setVerticalHeaderLabels(labels)
