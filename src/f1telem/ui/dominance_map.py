"""Track dominance: cada µsector del trazado pintado con el color del
piloto más rápido en ese tramo, comparando los pilotos elegidos (todos por
defecto, filtro 👥 propio) sobre un rango de vueltas configurable (0 =
extremo abierto). Usa los µ re-escalados a los sectores oficiales y los
cortes del panel Microsectors; compañeros de equipo se distinguen por el
estilo de trazo (como en los gráficos). Solo vueltas ya completadas: nada
posterior al timeline."""
from __future__ import annotations

import time

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QHBoxLayout, QLabel, QSpinBox, QVBoxLayout, QWidget

from ..hub import DataHub
from ..timing import TimingAnalyzer
from . import theme
from .charts import series_pens
from .driver_filter import DriverFilterButton

MAX_LAPS = 60  # tope de vueltas consideradas por piloto (costo acotado)


class DominanceMapView(QWidget):
    def __init__(self, hub: DataHub, cfg: dict | None = None, parent=None):
        super().__init__(parent)
        self.hub = hub
        self.analyzer = TimingAnalyzer(hub)
        self._sig: tuple | None = None
        self._last_try = 0.0
        self._seg_items: list[pg.PlotDataItem] = []
        self._label_items: list[pg.TextItem] = []
        self.counts: dict[str, int] = {}  # µ dominados por piloto (tests)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 2, 4, 4)
        lay.setSpacing(2)
        row = QHBoxLayout()
        row.addWidget(QLabel("Laps:"))
        self.from_spin = QSpinBox()
        self.from_spin.setRange(0, 999)
        self.from_spin.setSpecialValueText("start")
        self.from_spin.setToolTip("First lap considered (start = from lap 1)")
        row.addWidget(self.from_spin)
        row.addWidget(QLabel("–"))
        self.to_spin = QSpinBox()
        self.to_spin.setRange(0, 999)
        self.to_spin.setSpecialValueText("now")
        self.to_spin.setToolTip("Last lap considered (now = keeps growing)")
        row.addWidget(self.to_spin)
        self.legend = QLabel("")
        self.legend.setTextFormat(Qt.RichText)
        row.addWidget(self.legend, stretch=1)
        self.filter_btn = DriverFilterButton(hub, cfg, "dom_hidden_cars")
        self.filter_btn.changed.connect(self._invalidate)
        row.addWidget(self.filter_btn)
        lay.addLayout(row)
        self.from_spin.valueChanged.connect(self._invalidate)
        self.to_spin.valueChanged.connect(self._invalidate)

        self.plot = pg.PlotWidget()
        self.plot.setMenuEnabled(False)
        self.plot.setAspectLocked(True)
        self.plot.getPlotItem().hideAxis("left")
        self.plot.getPlotItem().hideAxis("bottom")
        self.base_curve = pg.PlotDataItem(pen=pg.mkPen("#3a3f4a", width=5))
        self.plot.addItem(self.base_curve)
        lay.addWidget(self.plot, stretch=1)

    def _invalidate(self, *_a) -> None:
        self._sig = None
        self._last_try = 0.0
        self.refresh()

    def clear_data(self) -> None:
        self.analyzer.clear()
        self._sig = None
        self._last_try = 0.0
        self.counts = {}
        for item in self._seg_items + self._label_items:
            self.plot.removeItem(item)
        self._seg_items = []
        self._label_items = []
        self.legend.setText("")

    # ------------------------------------------------------------ refresco

    def _lap_range(self) -> tuple[int, float]:
        l0 = max(int(self.from_spin.value()), 1)
        l1 = int(self.to_spin.value())
        return l0, (float("inf") if l1 == 0 else float(l1))

    def refresh(self) -> None:
        now = time.monotonic()
        if now - self._last_try < 1.0:
            return
        self._last_try = now
        hub = self.hub
        an = self.analyzer
        mapping = hub.outline_dist_map()
        if mapping is None:
            return
        marks = an._mark_dists()
        n_micro = len(marks) - 1
        l0, l1 = self._lap_range()
        visible = [d for d in hub.drivers if self.filter_btn.is_visible(d)]
        laps_sig = tuple(
            (d, len(hub.buffers[d].completed_laps()))
            for d in visible if d in hub.buffers and hub.buffers[d].n)
        sig = (tuple(np.round(marks, 1)), l0, l1, laps_sig,
               len(mapping[0]))
        if sig == self._sig:
            return
        self._sig = sig

        # mejor µ por piloto en el rango (solo vueltas ya completadas)
        best: dict[str, np.ndarray] = {}
        for drv in visible:
            buf = hub.buffers.get(drv)
            if buf is None or not buf.n:
                continue
            laps = [lap for lap in buf.completed_laps()
                    if l0 <= lap <= l1][-MAX_LAPS:]
            rows = []
            for lap in laps:
                micro = an.micro_times(drv, lap)
                if micro is not None and len(micro) == n_micro:
                    rows.append(micro)
            if rows:
                best[drv] = np.nanmin(np.vstack(rows), axis=0)

        d_arr, xs, ys = mapping
        pens = series_pens(hub, list(best)) if best else {}
        for item in self._seg_items + self._label_items:
            self.plot.removeItem(item)
        self._seg_items = []
        self._label_items = []
        counts: dict[str, int] = {}
        self.base_curve.setData(xs, ys)
        winners: list[str | None] = []
        for k in range(n_micro):
            winner, wtime = None, float("inf")
            for drv, arr in best.items():
                v = float(arr[k])
                if v == v and v < wtime:
                    winner, wtime = drv, v
            winners.append(winner)
            if winner is None:
                continue
            counts[winner] = counts.get(winner, 0) + 1
            mask = (d_arr >= marks[k]) & (d_arr <= marks[k + 1])
            if int(mask.sum()) < 2:
                continue
            info = hub.drivers.get(winner)
            pen = pg.mkPen(info.color if info else "#9aa0a6", width=5)
            base_pen = pens.get(winner)
            if base_pen is not None:
                pen.setStyle(base_pen.style())  # compañeros: trazo distinto
            item = pg.PlotDataItem(xs[mask], ys[mask], pen=pen)
            item.setZValue(5)
            self.plot.addItem(item)
            self._seg_items.append(item)
        self.counts = counts

        # iniciales del dominador por ZONA (µ contiguos del mismo piloto),
        # desplazadas hacia afuera del trazado para no pisarlo; las zonas de
        # un solo µ quedan sin etiqueta (el color alcanza)
        zones: list[list] = []
        for k, w in enumerate(winners):
            if w is None:
                continue
            if zones and zones[-1][0] == w and zones[-1][2] == k - 1:
                zones[-1][2] = k
            else:
                zones.append([w, k, k])
        if (len(zones) >= 2 and zones[0][0] == zones[-1][0]
                and zones[0][1] == 0 and zones[-1][2] == n_micro - 1):
            # la zona cruza la línea de meta: fusionarla
            zones[0][1] = zones[-1][1] - n_micro
            zones.pop()
        total = float(marks[-1])
        cx0, cy0 = float(np.mean(xs)), float(np.mean(ys))
        off = 0.07 * max(float(xs.max() - xs.min()),
                         float(ys.max() - ys.min()))
        f_lbl = QFont(self.font())
        f_lbl.setPointSizeF(8.0)
        f_lbl.setBold(True)
        for w, k0, k1 in zones:
            if k1 - k0 + 1 < 2:
                continue
            d_start = float(marks[k0 % n_micro])
            d_end = total if (k1 + 1) >= n_micro and (k1 + 1) % n_micro == 0 \
                else float(marks[(k1 + 1) % n_micro])
            span = (d_end - d_start) % total or total
            mid = (d_start + span / 2.0) % total
            px = float(np.interp(mid, d_arr, xs))
            py = float(np.interp(mid, d_arr, ys))
            vx, vy = px - cx0, py - cy0
            norm = float(np.hypot(vx, vy)) or 1.0
            info = hub.drivers.get(w)
            label = pg.TextItem(info.code if info else w,
                                color=info.color if info else "#9aa0a6",
                                anchor=(0.5, 0.5))
            label.setFont(f_lbl)
            label.setPos(px + vx / norm * off, py + vy / norm * off)
            self.plot.addItem(label)
            self._label_items.append(label)

        parts = []
        for drv, n in sorted(counts.items(), key=lambda kv: -kv[1]):
            info = hub.drivers.get(drv)
            code = info.code if info else drv
            color = info.color if info else "#9aa0a6"
            parts.append(f'<span style="color:{color}"><b>{code}</b> {n}</span>')
        self.legend.setText(" · ".join(parts) if parts
                            else f'<span style="color:{theme.TEXT_MUTED}">'
                                 f'no timed laps in range</span>')
