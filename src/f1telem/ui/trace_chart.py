"""Race trace: evolución del gap de cada piloto contra una referencia
elegible (el líder por defecto o cualquier piloto), con un punto por
microsector — se ve el efecto de cada curva, no solo el corte por vuelta.

X = vueltas completadas (continuo), Y = diferencia en segundos contra la
referencia (positivo = detrás; el eje va invertido para que perder tiempo
sea "caer" en el gráfico, como en los race trace clásicos). El rango de X
(últimas N vueltas) y de Y (±segundos) son configurables. Los períodos de
SC/VSC/bandera se sombrean con el color del estado.
"""
from __future__ import annotations

import time

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QComboBox, QDoubleSpinBox, QHBoxLayout, QLabel, QSpinBox, QVBoxLayout,
    QWidget,
)

from .. import config
from ..hub import DataHub
from ..timing import TimingAnalyzer
from . import theme
from .charts import HoverProbe


class TraceChart(QWidget):
    def __init__(self, hub: DataHub, cfg: dict | None = None, parent=None):
        super().__init__(parent)
        self.hub = hub
        self.cfg = cfg if cfg is not None else {}
        self.analyzer = TimingAnalyzer(hub)
        self._selected: list[str] = []
        self._curves: dict[str, pg.PlotDataItem] = {}
        self._status_items: list = []
        self._status_sig: tuple | None = None
        self._last_compute = 0.0
        self._dirty = True

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        controls = QHBoxLayout()
        controls.setContentsMargins(4, 2, 4, 0)
        controls.addWidget(QLabel("Reference:"))
        self.ref_combo = QComboBox()
        self.ref_combo.addItem("Leader (auto)", None)
        controls.addWidget(self.ref_combo)
        controls.addSpacing(12)
        controls.addWidget(QLabel("X:"))
        self.x_spin = QSpinBox()
        self.x_spin.setRange(0, 200)
        self.x_spin.setSuffix(" laps")
        self.x_spin.setSpecialValueText("All laps")
        self.x_spin.setValue(int(self.cfg.get("ui", {}).get("trace_x_laps", 0)))
        controls.addWidget(self.x_spin)
        controls.addSpacing(12)
        controls.addWidget(QLabel("Y:"))
        self.y_spin = QDoubleSpinBox()
        self.y_spin.setRange(0.0, 600.0)
        self.y_spin.setDecimals(1)
        self.y_spin.setSingleStep(5.0)
        self.y_spin.setPrefix("±")
        self.y_spin.setSuffix(" s")
        self.y_spin.setSpecialValueText("Auto")
        self.y_spin.setValue(float(self.cfg.get("ui", {}).get("trace_y_secs", 0.0)))
        controls.addWidget(self.y_spin)
        controls.addStretch(1)
        self._controls = controls
        lay.addLayout(controls)

        self.plot = pg.PlotWidget()
        self.plot.showGrid(x=True, y=True, alpha=theme.GRID_ALPHA)
        self.plot.getViewBox().invertY(True)
        self.plot.setLabel("bottom", "Lap")
        self.plot.setLabel("left", "Gap to reference (s)")
        self.zero_line = pg.InfiniteLine(
            pos=0.0, angle=0,
            pen=pg.mkPen(theme.TEXT_MUTED, width=1, style=Qt.DashLine))
        self.plot.addItem(self.zero_line)
        lay.addWidget(self.plot, stretch=1)
        # tooltip de cursor: gap de cada piloto en el X del mouse, en
        # segundos con 1 decimal, ordenado como la carrera en ese punto
        self._probe = HoverProbe(
            self.plot, self._probe_series,
            x_format=lambda x: f"Lap {x:.2f}",
            y_format=lambda y: f"{y:+.1f} s",
            sort_rows=True,
        )

        self.hub.driversChanged.connect(self._rebuild_ref_combo)
        self.ref_combo.currentIndexChanged.connect(self._control_changed)
        self.x_spin.valueChanged.connect(self._range_changed)
        self.y_spin.valueChanged.connect(self._range_changed)

    def _probe_series(self):
        rows = []
        for drv, curve in self._curves.items():
            if not curve.isVisible():
                continue
            xd, yd = curve.getData()
            if xd is None or not len(xd):
                continue
            info = self.hub.drivers.get(drv)
            rows.append((info.code if info else drv,
                         info.color if info else "#9aa0a6", xd, yd))
        return rows

    # ------------------------------------------------------------ controles

    def _rebuild_ref_combo(self) -> None:
        current = self.ref_combo.currentData()
        self.ref_combo.blockSignals(True)
        self.ref_combo.clear()
        self.ref_combo.addItem("Leader (auto)", None)
        drivers = sorted(self.hub.drivers.values(), key=lambda d: d.label.upper())
        for info in drivers:
            self.ref_combo.addItem(info.label, info.number)
        if current is not None:
            idx = self.ref_combo.findData(current)
            if idx >= 0:
                self.ref_combo.setCurrentIndex(idx)
        self.ref_combo.blockSignals(False)

    def _control_changed(self, *_a) -> None:
        self._dirty = True
        self.refresh()

    def _range_changed(self, *_a) -> None:
        ui = self.cfg.setdefault("ui", {})
        ui["trace_x_laps"] = int(self.x_spin.value())
        ui["trace_y_secs"] = float(self.y_spin.value())
        config.save_config(self.cfg)
        self._control_changed()

    # ---------------------------------------------- interfaz común de vistas

    def add_control(self, widget: QWidget) -> None:
        """Suma un control a la barrita propia (p.ej. el selector 👥)."""
        self._controls.addWidget(widget)

    def set_selected(self, drivers: list[str]) -> None:
        self._selected = list(drivers)
        for drv, curve in self._curves.items():
            curve.setVisible(drv in self._selected)
        self._dirty = True

    def set_channel(self, channel: str) -> None:
        pass  # el race trace siempre grafica gap

    def set_peaks_enabled(self, on: bool) -> None:
        pass

    def show_track_marker(self, dist) -> None:
        pass  # el eje X es vueltas, no metros de pista

    def clear_data(self) -> None:
        self.analyzer.clear()
        for curve in self._curves.values():
            self.plot.removeItem(curve)
        self._curves.clear()
        for item in self._status_items:
            self.plot.removeItem(item)
        self._status_items = []
        self._status_sig = None
        self._dirty = True

    # -------------------------------------------------------------- refresco

    def _checkpoint_step(self) -> float:
        counts = self.hub.segment_counts
        n = (sum(counts.values()) if len(counts) == 3
             else self.analyzer.n_micro())
        return self.hub.track_length / max(n, 3)

    def refresh(self) -> None:
        now = time.monotonic()
        if not self._dirty and now - self._last_compute < 0.4:
            return
        self._last_compute = now
        self._dirty = False
        hub = self.hub
        an = self.analyzer
        L = hub.track_length
        if L <= 0 or not hub.buffers:
            return

        wanted = set(self._selected)
        ref_choice = self.ref_combo.currentData()
        if ref_choice:
            wanted.add(ref_choice)
        pts = {}
        for drv in wanted:
            pt = an.position_time(drv)
            if pt is not None and len(pt[0]) >= 2:
                pts[drv] = pt
        if not pts:
            return
        ref = ref_choice if ref_choice in pts else max(
            pts, key=lambda d: float(pts[d][0][-1]))
        pos_r, t_r = pts[ref]
        p_lo, p_hi = float(pos_r[0]), float(pos_r[-1])

        step = self._checkpoint_step()
        n_laps = int(self.x_spin.value())
        if n_laps > 0:
            p_lo = max(p_lo, p_hi - n_laps * L)
        k0 = int(np.ceil(p_lo / step))
        k1 = int(np.floor(p_hi / step))
        if k1 <= k0:
            return
        base_p = np.arange(k0, k1 + 1, dtype=float) * step
        t_ref = np.interp(base_p, pos_r, t_r)

        for drv in self._selected:
            pt = pts.get(drv)
            curve = self._curves.get(drv)
            if pt is None:
                if curve is not None:
                    curve.setData([], [])
                continue
            pos_d, t_d = pt
            mask = (base_p >= float(pos_d[0])) & (base_p <= float(pos_d[-1]))
            x = base_p[mask] / L
            y = np.interp(base_p[mask], pos_d, t_d) - t_ref[mask]
            if curve is None:
                info = hub.drivers.get(drv)
                color = info.color if info else "#9aa0a6"
                curve = self.plot.plot(
                    pen=pg.mkPen(color, width=theme.LINE_WIDTH))
                self._curves[drv] = curve
            curve.setData(x, y)
            curve.setVisible(True)
        for drv, curve in self._curves.items():
            if drv not in self._selected:
                curve.setVisible(False)

        vb = self.plot.getViewBox()
        if n_laps > 0:
            vb.setXRange(p_lo / L, p_hi / L, padding=0.02)
        else:
            vb.enableAutoRange(axis=vb.XAxis)
        y_range = float(self.y_spin.value())
        if y_range > 0:
            vb.setYRange(-y_range, y_range, padding=0.0)
        else:
            vb.enableAutoRange(axis=vb.YAxis)

        self._update_status_regions(pos_r, t_r, L)

    def _update_status_regions(self, pos_r, t_r, L: float) -> None:
        """Bandas de SC/VSC/bandera: el período temporal se traduce a
        vueltas vía la posición de la referencia."""
        periods = self.hub.track_status
        t_max = float(t_r[-1])
        sig = (tuple(periods), int(t_max // 5))
        if sig == self._status_sig:
            return
        self._status_sig = sig
        for item in self._status_items:
            self.plot.removeItem(item)
        self._status_items = []
        for t0, t1, code in periods:
            if t0 > t_max:
                continue
            x0 = float(np.interp(t0, t_r, pos_r)) / L
            x1 = float(np.interp(min(t1, t_max), t_r, pos_r)) / L
            color = QColor(theme.TRACK_STATUS.get(code, ("", "#888888"))[1])
            color.setAlpha(28)
            item = pg.LinearRegionItem(values=(x0, x1), movable=False,
                                       brush=pg.mkBrush(color),
                                       pen=pg.mkPen(None))
            item.setZValue(-20)
            self.plot.addItem(item)
            self._status_items.append(item)
