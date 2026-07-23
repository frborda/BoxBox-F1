"""Lap Compare (histórico): comparación de vueltas YA COMPLETADAS.

Se arman sets piloto→vuelta (cuantos se quieran, del mismo piloto o de
varios) y se grafica el canal elegido (velocidad, rpm, marcha, etc.) a lo
largo de la vuelta, más el delta de tiempo acumulado contra el set marcado
como TARGET. A diferencia de "Lap Compare - Live", acá no entra el stream
en vivo: son vueltas cerradas, así que el gráfico es estable.

Sin spoilers por construcción: solo se listan vueltas ya completadas a la
posición actual del timeline.
"""
from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QPushButton, QVBoxLayout, QWidget,
)
from PySide6.QtGui import QColor, QIcon, QPixmap

from ..hub import DataHub
from ..models import CHANNELS, CHANNEL_ORDER
from ..timing import TimingAnalyzer
from . import theme
from .timing_view import fmt_laptime

# estilos de trazo para sets repetidos del mismo piloto
_STYLES = (Qt.SolidLine, Qt.DashLine, Qt.DotLine, Qt.DashDotLine)
GRID_N = 400  # puntos de la grilla de distancia para el delta


class LapCompareView(QWidget):
    def __init__(self, hub: DataHub, cfg: dict | None = None, parent=None):
        super().__init__(parent)
        self.hub = hub
        self.analyzer = TimingAnalyzer(hub)
        self.entries: list[tuple[str, int]] = []   # (piloto, vuelta)
        self.target = 0                            # índice en entries
        self._sig: tuple | None = None
        # correlación con el mapa: hover propio hacia afuera, marca ajena
        # hacia adentro (set_hover_dist)
        self.hover_dist_cb = None

        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 2, 4, 4)
        lay.setSpacing(4)
        row = QHBoxLayout()
        row.addWidget(QLabel("Driver"))
        self.driver_combo = QComboBox()
        self.driver_combo.currentIndexChanged.connect(self._refresh_laps)
        row.addWidget(self.driver_combo)
        row.addWidget(QLabel("Lap"))
        self.lap_combo = QComboBox()
        self.lap_combo.setMinimumWidth(130)
        row.addWidget(self.lap_combo)
        self.add_btn = QPushButton("+ Add")
        self.add_btn.setToolTip("Add this driver→lap set to the chart")
        self.add_btn.clicked.connect(self._add_clicked)
        row.addWidget(self.add_btn)
        row.addSpacing(12)
        row.addWidget(QLabel("Channel"))
        self.channel_combo = QComboBox()
        for ch in CHANNEL_ORDER:
            self.channel_combo.addItem(CHANNELS[ch][0], ch)
        self.channel_combo.currentIndexChanged.connect(self._redraw_needed)
        row.addWidget(self.channel_combo)
        row.addStretch(1)
        lay.addLayout(row)

        row2 = QHBoxLayout()
        self.sets_list = QListWidget()
        self.sets_list.setMaximumHeight(84)
        self.sets_list.itemDoubleClicked.connect(
            lambda _i: self._target_clicked())
        row2.addWidget(self.sets_list, stretch=1)
        btns = QVBoxLayout()
        self.target_btn = QPushButton("🎯 Set target")
        self.target_btn.setToolTip(
            "The selected set becomes the delta reference (double-click "
            "a set does the same)")
        self.target_btn.clicked.connect(self._target_clicked)
        btns.addWidget(self.target_btn)
        self.remove_btn = QPushButton("✕ Remove")
        self.remove_btn.clicked.connect(self._remove_clicked)
        btns.addWidget(self.remove_btn)
        btns.addStretch(1)
        row2.addLayout(btns)
        lay.addLayout(row2)

        self.glw = pg.GraphicsLayoutWidget()
        self.p_chan = self.glw.addPlot(row=0, col=0)
        self.p_delta = self.glw.addPlot(row=1, col=0)
        self.p_delta.setXLink(self.p_chan)
        self.glw.ci.layout.setRowStretchFactor(0, 3)
        self.glw.ci.layout.setRowStretchFactor(1, 2)
        self.p_delta.setLabel("left", "Δ vs target (s)")
        self.p_delta.setLabel("bottom", "Lap distance (m)")
        for plot in (self.p_chan, self.p_delta):
            plot.showGrid(x=True, y=True, alpha=theme.GRID_ALPHA)
        self.p_delta.addItem(pg.InfiniteLine(
            angle=0, pen=pg.mkPen(theme.TEXT_MUTED, width=1,
                                  style=Qt.DashLine)))
        # marcador del punto de pista (sincronizado con mapa y análisis)
        self._hover_lines = []
        for plot in (self.p_chan, self.p_delta):
            line = pg.InfiniteLine(angle=90,
                                   pen=pg.mkPen(theme.ACCENT, width=1))
            line.setVisible(False)
            plot.addItem(line, ignoreBounds=True)
            self._hover_lines.append(line)
        self.glw.scene().sigMouseMoved.connect(self._chart_mouse)
        lay.addWidget(self.glw, stretch=1)
        self.hint = QLabel("Add driver→lap sets to compare completed laps; "
                           "the 🎯 set is the delta reference")
        self.hint.setStyleSheet(
            f"color: {theme.TEXT_MUTED}; font-size: 7.5pt;")
        lay.addWidget(self.hint)

        hub.driversChanged.connect(self._rebuild_drivers)
        self._rebuild_drivers()

    # ------------------------------------------------------------ combos

    def _rebuild_drivers(self) -> None:
        current = self.driver_combo.currentData()
        self.driver_combo.blockSignals(True)
        self.driver_combo.clear()
        for info in sorted(self.hub.drivers.values(),
                           key=lambda d: d.label.upper()):
            pix = QPixmap(10, 10)
            pix.fill(QColor(info.color))
            self.driver_combo.addItem(QIcon(pix), info.code, info.number)
        idx = self.driver_combo.findData(current)
        self.driver_combo.setCurrentIndex(max(idx, 0))
        self.driver_combo.blockSignals(False)
        self._refresh_laps()

    def _refresh_laps(self, *_a) -> None:
        drv = self.driver_combo.currentData()
        current = self.lap_combo.currentData()
        self.lap_combo.blockSignals(True)
        self.lap_combo.clear()
        if drv is not None:
            buf = self.hub.buffers.get(drv)
            for lap in (buf.completed_laps() if buf is not None else []):
                t = self.analyzer.lap_time(drv, lap)
                label = (f"Lap {lap} — {fmt_laptime(t)}" if t == t
                         else f"Lap {lap}")
                self.lap_combo.addItem(label, lap)
        idx = self.lap_combo.findData(current)
        # por defecto la última vuelta cerrada (la más probable a comparar)
        self.lap_combo.setCurrentIndex(
            idx if idx >= 0 else self.lap_combo.count() - 1)
        self.lap_combo.blockSignals(False)

    # ------------------------------------------------------------- sets

    def _add_clicked(self) -> None:
        drv = self.driver_combo.currentData()
        lap = self.lap_combo.currentData()
        if drv is None or lap is None:
            return
        entry = (str(drv), int(lap))
        if entry in self.entries:
            return
        self.entries.append(entry)
        if len(self.entries) == 1:
            self.target = 0
        self._rebuild_sets()
        self._redraw_needed()

    def _target_clicked(self) -> None:
        row = self.sets_list.currentRow()
        if 0 <= row < len(self.entries):
            self.target = row
            self._rebuild_sets()
            self._redraw_needed()

    def _remove_clicked(self) -> None:
        row = self.sets_list.currentRow()
        if not (0 <= row < len(self.entries)):
            return
        self.entries.pop(row)
        if self.target >= len(self.entries):
            self.target = max(0, len(self.entries) - 1)
        elif row < self.target:
            self.target -= 1
        self._rebuild_sets()
        self._redraw_needed()

    def _rebuild_sets(self) -> None:
        self.sets_list.clear()
        for i, (drv, lap) in enumerate(self.entries):
            info = self.hub.drivers.get(drv)
            code = info.code if info else drv
            t = self.analyzer.lap_time(drv, lap)
            label = f"{code} · L{lap}" + (f" · {fmt_laptime(t)}"
                                          if t == t else "")
            if i == self.target:
                label = "🎯 " + label
            item = QListWidgetItem(label)
            pix = QPixmap(10, 10)
            pix.fill(QColor(info.color if info else "#9aa0a6"))
            item.setIcon(QIcon(pix))
            self.sets_list.addItem(item)

    # -------------------------------------------- correlación con el mapa

    def _chart_mouse(self, scene_pos) -> None:
        if self.hover_dist_cb is None:
            return
        vb = self.p_chan.vb
        if not vb.sceneBoundingRect().contains(scene_pos):
            vb = self.p_delta.vb
            if not vb.sceneBoundingRect().contains(scene_pos):
                self.hover_dist_cb(None)
                return
        x = float(vb.mapSceneToView(scene_pos).x())
        self.hover_dist_cb(x if 0.0 <= x <= self.hub.track_length else None)

    def set_hover_dist(self, dist: float | None) -> None:
        """Marca el metro de vuelta hovereado en otro panel/mapa."""
        for line in self._hover_lines:
            if dist is None:
                line.setVisible(False)
            else:
                line.setValue(float(dist))
                line.setVisible(True)

    # ------------------------------------------------------------ dibujo

    def _redraw_needed(self, *_a) -> None:
        self._sig = None
        self.refresh()

    def clear_data(self) -> None:
        self.entries = []
        self.target = 0
        self._sig = None
        self.sets_list.clear()
        self.p_chan.clearPlots()
        self.p_delta.clearPlots()
        self._refresh_laps()

    def _lap_arrays(self, drv: str, lap: int):
        """(dist monótona, t relativo al inicio, canal) de una vuelta
        cerrada; None si (ya) no está disponible (seek hacia atrás)."""
        buf = self.hub.buffers.get(drv)
        if buf is None or lap not in buf.completed_laps():
            return None
        sl = buf.lap_slice(lap)
        if len(sl["t"]) < 8:
            return None
        dist = np.maximum.accumulate(
            np.clip(sl["dist_lap"].astype(float), 0.0,
                    self.hub.track_length))
        t = sl["t"].astype(float)
        chan = self.channel_combo.currentData() or "speed"
        return dist, t - t[0], sl[chan].astype(float)

    def refresh(self) -> None:
        # los combos siguen el avance de la sesión; el gráfico solo se
        # rearma si cambió algo (las vueltas cerradas son datos finales)
        self._refresh_laps()
        avail = tuple(self._lap_arrays(d, l) is not None
                      for d, l in self.entries)
        sig = (tuple(self.entries), self.target,
               self.channel_combo.currentData(), avail)
        if sig == self._sig:
            return
        self._sig = sig
        self.p_chan.clearPlots()
        self.p_delta.clearPlots()
        chan_key = self.channel_combo.currentData() or "speed"
        label, y_range = CHANNELS[chan_key]
        self.p_chan.setLabel("left", label)
        if y_range is not None:
            self.p_chan.setYRange(*y_range)

        tgt = (self._lap_arrays(*self.entries[self.target])
               if 0 <= self.target < len(self.entries) else None)
        grid = np.linspace(0.0, self.hub.track_length, GRID_N)
        tgt_rel = (np.interp(grid, tgt[0], tgt[1])
                   if tgt is not None else None)
        style_count: dict[str, int] = {}
        for i, (drv, lap) in enumerate(self.entries):
            arrays = self._lap_arrays(drv, lap)
            if arrays is None:
                continue
            dist, t_rel, values = arrays
            info = self.hub.drivers.get(drv)
            color = info.color if info else "#9aa0a6"
            n_prev = style_count.get(drv, 0)
            style_count[drv] = n_prev + 1
            pen = pg.mkPen(color, width=2.5 if i == self.target else 1.6,
                           style=_STYLES[n_prev % len(_STYLES)])
            self.p_chan.plot(dist, values, pen=pen)
            if tgt_rel is not None and i != self.target:
                delta = np.interp(grid, dist, t_rel) - tgt_rel
                self.p_delta.plot(grid, delta, pen=pen)