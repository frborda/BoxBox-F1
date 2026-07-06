"""Vista Qualy: comparación de la vuelta en curso contra una vuelta target.

Tres niveles, todos contra la misma target (cualquier vuelta cerrada de
cualquier piloto):

- Trazas del canal (velocidad, etc.): target punteada + vueltas en curso.
- Delta de tiempo acumulado en función de la distancia: cuánto viene ganando
  (−) o perdiendo (+) la vuelta en curso respecto de la target en cada metro.
- Tabla de deltas por sector y microsector, actualizada en vivo a medida que
  la vuelta en curso cruza cada tramo.
"""
from __future__ import annotations

import math

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame, QGridLayout, QHBoxLayout, QLabel, QVBoxLayout, QWidget,
)

from ..hub import DataHub
from ..timing import N_MICRO, SECTOR_STEP, TimingAnalyzer
from . import theme
from .charts import HoverProbe, QualyChart, series_pens
from .timing_view import _delta_bg, fmt_gap, fmt_laptime, fmt_secs


def _chip_css(delta: float | None) -> str:
    """Estilo de un chip de delta: verde = mejor, rojo = peor, gris = neutro."""
    base = "border-radius: 3px; padding: 1px 2px; font-size: 8pt;"
    if delta is None or not math.isfinite(delta):
        return base + f"color: {theme.TEXT_MUTED}; border: 1px solid {theme.BORDER};"
    color = _delta_bg(delta)
    if color is None:
        return base + f"color: {theme.TEXT}; background: #262a33;"
    rgba = f"rgba({color.red()},{color.green()},{color.blue()},{color.alpha()})"
    return base + f"color: {theme.TEXT}; background: {rgba};"


class _DeltaCard(QFrame):
    """Tarjeta de un piloto contra la target: delta total de la vuelta bien
    visible y una fila por sector — el chip del sector a la izquierda y sus
    8 microsectores alineados a la derecha, sin scroll horizontal."""

    MICRO_COLS = N_MICRO // 3  # µ por sector (una fila por sector)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet(
            f"_DeltaCard {{ border: 1px solid {theme.BORDER}; border-radius: 6px; }}"
        )
        # datos crudos de la última actualización (para tests y tooltips)
        self.last_delta = float("nan")
        self.sector_deltas = [float("nan")] * 3
        self.micro_filled = 0

        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 6)
        lay.setSpacing(4)
        head = QHBoxLayout()
        self.title = QLabel("—")
        self.total = QLabel("—")
        self.total.setTextFormat(Qt.RichText)
        head.addWidget(self.title)
        head.addStretch(1)
        head.addWidget(self.total)
        lay.addLayout(head)

        # fila s: [chip del sector | sus 8 microsectores alineados]
        self.grid = QGridLayout()
        self.grid.setSpacing(3)
        self.sectors = []
        self.micros = []
        for s in range(3):
            sec = QLabel(f"S{s + 1} —")
            sec.setAlignment(Qt.AlignCenter)
            sec.setMinimumWidth(72)
            self.grid.addWidget(sec, s, 0)
            self.sectors.append(sec)
        for i in range(N_MICRO):
            chip = QLabel("—")
            chip.setAlignment(Qt.AlignCenter)
            self.grid.addWidget(chip, i // self.MICRO_COLS, 1 + (i % self.MICRO_COLS))
            self.micros.append(chip)
        for c in range(1, self.MICRO_COLS + 1):
            self.grid.setColumnStretch(c, 1)
        lay.addLayout(self.grid)

    def show_empty(self, code_html: str) -> None:
        self.title.setText(code_html)
        self.total.setText(f'<span style="color:{theme.TEXT_MUTED}">Δ —</span>')
        self.last_delta = float("nan")
        self.sector_deltas = [float("nan")] * 3
        self.micro_filled = 0
        for k, chip in enumerate(self.sectors):
            chip.setText(f"S{k + 1} —")
            chip.setStyleSheet(_chip_css(None))
        for i, chip in enumerate(self.micros):
            chip.setText("—")
            chip.setStyleSheet(_chip_css(None))
            chip.setToolTip(f"µ{i + 1}")

    def update_deltas(self, code_html: str, delta_now: float,
                      sector_deltas: list[float], micro_deltas: np.ndarray,
                      micro_cur: np.ndarray, micro_ref: np.ndarray,
                      last_idx: int | None) -> None:
        self.title.setText(code_html)
        self.last_delta = delta_now
        self.sector_deltas = list(sector_deltas)
        if math.isfinite(delta_now):
            color = "#3ddc84" if delta_now < 0 else ("#ff6b5e" if delta_now > 0 else theme.TEXT)
            self.total.setText(
                f'<span style="color:{theme.TEXT_MUTED}; font-size:8pt">Δ now </span>'
                f'<b style="color:{color}; font-size:14pt">{fmt_gap(delta_now)}</b>'
            )
        else:
            self.total.setText(f'<span style="color:{theme.TEXT_MUTED}">Δ —</span>')
        for k, chip in enumerate(self.sectors):
            delta = sector_deltas[k]
            chip.setText(f"S{k + 1} {fmt_gap(delta)}")
            chip.setStyleSheet(_chip_css(delta))
        self.micro_filled = 0
        for i, chip in enumerate(self.micros):
            delta = float(micro_deltas[i])
            chip.setText(fmt_gap(delta) if math.isfinite(delta) else "—")
            css = _chip_css(delta if math.isfinite(delta) else None)
            if last_idx is not None and i == last_idx:
                css += f"border: 1px solid {theme.ACCENT}; font-weight: bold;"
            chip.setStyleSheet(css)
            if math.isfinite(delta):
                self.micro_filled += 1
                chip.setToolTip(
                    f"µ{i + 1}: {fmt_secs(float(micro_cur[i]))} s"
                    f" (target {fmt_secs(float(micro_ref[i]))})"
                )
            else:
                chip.setToolTip(f"µ{i + 1}")


class QualyView(QWidget):
    """Misma interfaz que los gráficos (refresh/set_selected/...) para el
    stack de modos; delega las trazas en QualyChart y agrega delta + tabla."""

    MAX_DELTA = 60.0  # s; por encima (out-laps vs target) no se dibuja

    def __init__(self, hub: DataHub, parent=None):
        super().__init__(parent)
        self.hub = hub
        self.analyzer = TimingAnalyzer(hub)
        self.selected: list[str] = []
        self.delta_curves: dict[str, pg.PlotDataItem] = {}
        self._ref: dict | None = None
        self._tick = 0

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.chart = QualyChart(hub)
        layout.addWidget(self.chart, stretch=5)

        self.caption = QLabel("Pick a driver and a target lap, then press Set")
        self.caption.setStyleSheet(f"color: {theme.TEXT_MUTED};")
        layout.addWidget(self.caption)

        self.delta_plot = pg.PlotWidget()
        self.delta_plot.setMenuEnabled(False)
        self.delta_plot.showGrid(x=True, y=True, alpha=theme.GRID_ALPHA)
        self.delta_plot.getAxis("left").setLabel("Δ vs target (s) · + worse / − better")
        self.delta_plot.getAxis("bottom").setLabel("Lap distance (m)")
        self.delta_plot.addItem(pg.InfiniteLine(
            pos=0.0, angle=0, pen=pg.mkPen(theme.TEXT_MUTED, width=1, style=Qt.DashLine)
        ))
        self.delta_plot.plotItem.setXLink(self.chart.plotItem)
        self._probe = HoverProbe(
            self.delta_plot, self._delta_hover,
            x_format=lambda x: f"{x:,.0f} m",
            y_format=lambda y: f"{y:+.2f} s",
        )
        layout.addWidget(self.delta_plot, stretch=3)

        cards_box = QWidget()
        self._cards_lay = QGridLayout(cards_box)
        self._cards_lay.setContentsMargins(0, 0, 0, 0)
        self._cards_lay.setSpacing(8)
        self.cards: dict[str, _DeltaCard] = {}
        self.more_note = QLabel("showing the first 4 drivers")
        self.more_note.setStyleSheet(f"color: {theme.TEXT_MUTED};")
        self.more_note.setVisible(False)
        layout.addWidget(cards_box, stretch=3)
        layout.addWidget(self.more_note)

        hub.driversChanged.connect(self._restyle)

    # ------------------------------------------------- interfaz de "chart"

    def set_channel(self, channel: str) -> None:
        self.chart.set_channel(channel)

    def set_peaks_enabled(self, on: bool) -> None:
        self.chart.set_peaks_enabled(on)

    def show_track_marker(self, dist: float | None) -> None:
        self.chart.show_track_marker(dist)

    def set_selected(self, drivers: list[str]) -> None:
        self.selected = list(drivers)
        self.chart.set_selected(drivers)
        for drv in list(self.delta_curves):
            if drv not in drivers:
                self.delta_plot.removeItem(self.delta_curves.pop(drv))
        for drv in drivers:
            if drv not in self.delta_curves:
                curve = pg.PlotDataItem(connect="finite")
                curve.setClipToView(True)
                self.delta_plot.addItem(curve)
                self.delta_curves[drv] = curve
        self._restyle()
        self._rebuild_cards()

    def _rebuild_cards(self) -> None:
        """Una tarjeta por piloto, hasta 4, en grilla de 2 columnas (así los
        microsectores mantienen ancho útil sin scroll)."""
        shown = self.selected[:4]
        for drv in list(self.cards):
            if drv not in shown:
                card = self.cards.pop(drv)
                self._cards_lay.removeWidget(card)
                card.deleteLater()
        for i, drv in enumerate(shown):
            card = self.cards.get(drv)
            if card is None:
                card = _DeltaCard()
                card.show_empty(self._card_title(drv, 0))
                self.cards[drv] = card
            self._cards_lay.addWidget(card, i // 2, i % 2)
        self.more_note.setVisible(len(self.selected) > 4)
        self._update_cards()

    def clear_data(self) -> None:
        self.chart.clear_data()
        self.analyzer.clear()
        self.set_reference(None)

    def clear_stream_data(self) -> None:
        """Limpieza para saltos de tiempo del replay: vacía las series pero
        conserva la vuelta target fijada (su snapshot está congelado)."""
        self.chart.clear_data()
        for curve in self.delta_curves.values():
            curve.setData([], [])
        for drv, card in self.cards.items():
            card.show_empty(self._card_title(drv, 0))

    def refresh(self) -> None:
        self.chart.refresh()
        self._tick += 1
        if self._tick % 3 == 0:  # el delta cambia al ritmo de las muestras
            self._update_delta()
        if self._tick % 15 == 0:  # tarjetas a ~2 Hz
            self._update_cards()

    # ----------------------------------------------------------- target

    def set_reference(self, driver: str | None, lap: int = 0) -> None:
        self.chart.set_reference(driver, lap)
        reference = self.chart.reference
        if reference is None:
            self._ref = None
            self.caption.setText("Pick a driver and a target lap, then press Set")
            for curve in self.delta_curves.values():
                curve.setData([], [])
            for drv, card in self.cards.items():
                card.show_empty(self._card_title(drv, 0))
            return
        drv, lap = reference
        marks = self.analyzer.lap_marks(drv, lap)
        buf = self.hub.buffers[drv]
        data = buf.lap_slice(lap)
        d = data["dist_lap"].astype(np.float64).copy()
        t = data["t"].astype(np.float64).copy()
        t0 = float(marks[0]) if marks is not None and math.isfinite(marks[0]) else float(t[0])
        self._ref = {"drv": drv, "lap": lap, "d": d, "elapsed": t - t0, "marks": marks}
        lap_time = self.analyzer.lap_time(drv, lap)
        caption = f"Target: {self._code_of(drv)} · Lap {lap} · {fmt_laptime(lap_time)}"
        if marks is not None:
            sectors = [
                float(marks[(k + 1) * SECTOR_STEP] - marks[k * SECTOR_STEP])
                for k in range(3)
            ]
            caption += " · S: " + " / ".join(fmt_secs(s) for s in sectors)
        caption += "   —   Δ: + losing / − gaining"
        self.caption.setText(caption)
        self._update_cards()

    # ------------------------------------------------------------ interno

    def _code_of(self, drv: str) -> str:
        info = self.hub.drivers.get(drv)
        return info.code if info else drv

    def _restyle(self) -> None:
        antialias = len(self.selected) <= 8
        for drv, pen in series_pens(self.hub, self.selected).items():
            curve = self.delta_curves.get(drv)
            if curve is not None:
                curve.opts["antialias"] = antialias
                curve.setPen(pen)

    def _delta_hover(self):
        out = []
        for drv in self.selected:
            curve = self.delta_curves.get(drv)
            if curve is None or not curve.isVisible():
                continue
            xd, yd = curve.getData()
            info = self.hub.drivers.get(drv)
            out.append((self._code_of(drv), info.color if info else "#9aa0a6", xd, yd))
        return out

    def _current_elapsed(self, drv: str):
        """(dist, elapsed, marks) de la vuelta en curso del piloto."""
        buf = self.hub.buffers.get(drv)
        if buf is None or not buf.n:
            return None
        cur = buf.current_lap()
        marks = self.analyzer.lap_marks(drv, cur)
        data = buf.lap_slice(cur)
        if not len(data["t"]):
            return None
        t = data["t"].astype(np.float64)
        t0 = float(marks[0]) if marks is not None and math.isfinite(marks[0]) else float(t[0])
        return data["dist_lap"].astype(np.float64), t - t0, marks, cur

    def _update_delta(self) -> None:
        if self._ref is None:
            return
        for drv in self.selected:
            curve = self.delta_curves[drv]
            cur_data = self._current_elapsed(drv)
            if cur_data is None:
                curve.setData([], [])
                continue
            d, elapsed, _marks, cur = cur_data
            ref_elapsed = np.interp(d, self._ref["d"], self._ref["elapsed"],
                                    left=np.nan, right=np.nan)
            delta = elapsed - ref_elapsed
            # en la vuelta 1 (largada) el delta recién vale desde el sector 1
            d_min = self.hub.track_length / 3.0 if cur == 1 else 20.0
            ok = np.isfinite(delta) & (np.abs(delta) < self.MAX_DELTA) & (d > d_min)
            curve.setData(d[ok], delta[ok])

    def _card_title(self, drv: str, lap: int) -> str:
        info = self.hub.drivers.get(drv)
        color = info.color if info else "#9aa0a6"
        code = self._code_of(drv)
        lap_txt = f" L{lap}" if lap else ""
        return (
            f'<span style="color:{color}">▍</span> <b>{code}</b>'
            f'<span style="color:{theme.TEXT_MUTED}">{lap_txt} vs target</span>'
        )

    def _update_cards(self) -> None:
        ref_marks = self._ref["marks"] if self._ref is not None else None
        for drv, card in self.cards.items():
            cur_data = self._current_elapsed(drv)
            marks = cur_data[2] if cur_data is not None else None
            cur_lap = cur_data[3] if cur_data is not None else 0
            title = self._card_title(drv, cur_lap)
            if (ref_marks is None or marks is None
                    or not math.isfinite(marks[0]) or not math.isfinite(ref_marks[0])):
                card.show_empty(title)
                continue
            both = np.isfinite(marks) & np.isfinite(ref_marks)
            idx = np.nonzero(both)[0]
            delta_now = float("nan")
            last_idx = None
            if len(idx) > 1:
                i = int(idx.max())
                delta_now = float((marks[i] - marks[0]) - (ref_marks[i] - ref_marks[0]))
                last_idx = i - 1 if i >= 1 else None  # µsector que termina en la marca i
            sector_deltas = []
            for k in range(3):
                a, b = k * SECTOR_STEP, (k + 1) * SECTOR_STEP
                if both[a] and both[b]:
                    sector_deltas.append(
                        float((marks[b] - marks[a]) - (ref_marks[b] - ref_marks[a]))
                    )
                else:
                    sector_deltas.append(float("nan"))
            micro_cur = np.diff(marks)
            micro_ref = np.diff(ref_marks)
            micro_deltas = micro_cur - micro_ref
            card.update_deltas(title, delta_now, sector_deltas,
                               micro_deltas, micro_cur, micro_ref, last_idx)
