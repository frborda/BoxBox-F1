"""Ventana de Box (pit window) y proyecciones de rejoin/undercut.

La pérdida real por parar NO es el tiempo de pit lane: el auto también
pierde frenando antes y acelerando después. Acá se mide una "ventana de
box" EN PISTA — desde 2 microsectores antes de la entrada a boxes hasta 2
microsectores después de la salida — y se compara cuánto tarda en
atravesarla un auto que paró contra la referencia limpia:

- referencia: promedio de las últimas 3 vueltas limpias (pista verde, sin
  entrar a boxes) de cada auto del top 5 de la carrera;
- el auto que paró: su cruce de la ventana con la detención NORMALIZADA a
  3 s (si estuvo detenido más o menos que eso, se compensa).

El valor puede editarse a mano y trabarse (tilde) para que el cálculo
automático no lo pise.
"""
from __future__ import annotations

import math
import time

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox, QDoubleSpinBox, QHBoxLayout, QLabel, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from .. import config
from ..hub import DataHub
from ..timing import TimingAnalyzer
from . import theme

STOP_NORM = 3.0     # s: detención estándar a 0 km/h
MICRO_MARGIN = 2    # microsectores antes de la entrada y después de la salida
REF_LAPS = 3        # vueltas limpias por auto de referencia
REF_TOP = 5         # autos de referencia (top de la carrera)


def clean_at(hub: DataHub, t0: float, t1: float) -> bool:
    """True si [t0, t1] no pisa ningún período de bandera/SC."""
    for a, b, _code in hub.track_status:
        if a <= t1 and t0 <= b:
            return False
    return True


def pit_lane_bounds(hub: DataHub) -> tuple[float, float] | None:
    """(entrada, salida) medianas de la calle de boxes en metros de vuelta,
    derivadas de las visitas ya observadas (None hasta la primera parada)."""
    d_in: list[float] = []
    d_out: list[float] = []
    for drv, visits in hub.pit_lane.items():
        buf = hub.buffers.get(drv)
        if buf is None or buf.n < 2:
            continue
        t_col = buf.col("t")
        d_col = buf.col("dist_lap")
        for _lap, t_in, t_out in visits:
            if t_out is None:
                continue
            if t_col[0] <= t_in <= t_col[-1]:
                d_in.append(float(np.interp(t_in, t_col, d_col)))
            if t_col[0] <= t_out <= t_col[-1]:
                d_out.append(float(np.interp(t_out, t_col, d_col)))
    if not d_in or not d_out:
        return None
    return float(np.median(d_in)), float(np.median(d_out))


def current_gaps(hub: DataHub, analyzer: TimingAnalyzer
                 ) -> tuple[list[str], dict[str, float | None]]:
    """Orden actual de pista y gap al líder por auto (None si va doblado)."""
    pts = {}
    for drv in hub.buffers:
        pt = analyzer.position_time(drv)
        if pt is not None and len(pt[0]) >= 2:
            pts[drv] = pt
    ordered = sorted(pts, key=lambda d: float(pts[d][0][-1]), reverse=True)
    gaps: dict[str, float | None] = {}
    if not ordered:
        return [], gaps
    L = hub.track_length
    pos_l, t_l = pts[ordered[0]]
    for drv in ordered:
        pos, t = pts[drv]
        behind = float(pos_l[-1]) - float(pos[-1])
        if behind >= L:
            gaps[drv] = None
        else:
            gaps[drv] = (float(t[-1])
                         - float(np.interp(float(pos[-1]), pos_l, t_l)))
    return ordered, gaps


def project_rejoin(gaps: dict[str, float | None], drv: str,
                   window: float) -> tuple[int, tuple, tuple] | None:
    """Posición y márgenes tras parar AHORA con la Ventana de Box dada.

    `gaps` = gap al líder de cada auto (None = doblado / sin dato). Devuelve
    (posición proyectada, (auto adelante, margen), (auto atrás, margen));
    los extremos usan (None, nan).
    """
    own = gaps.get(drv)
    if own is None:
        return None
    after = own + window
    others = sorted(
        ((g, d) for d, g in gaps.items() if d != drv and g is not None))
    ahead = [(g, d) for g, d in others if g <= after]
    behind = [(g, d) for g, d in others if g > after]
    pos = len(ahead) + 1
    ahead_info = ((ahead[-1][1], after - ahead[-1][0]) if ahead
                  else (None, float("nan")))
    behind_info = ((behind[0][1], behind[0][0] - after) if behind
                   else (None, float("nan")))
    return pos, ahead_info, behind_info


class PitWindowEstimator:
    """Cálculo automático de la Ventana de Box a partir de las paradas ya
    observadas en la carrera."""

    def __init__(self, hub: DataHub, analyzer: TimingAnalyzer):
        self.hub = hub
        self.analyzer = analyzer

    # ------------------------------------------------------------ geometría

    def window_bounds(self) -> tuple[float, float] | None:
        """(w_start, span) en metros: la ventana arranca 2 µsectores antes
        de la entrada mediana a boxes y termina 2 después de la salida."""
        hub = self.hub
        bounds = pit_lane_bounds(hub)
        if bounds is None:
            return None
        entry, exit_ = bounds
        L = hub.track_length
        marks = self.analyzer._mark_dists()[:-1]  # 24 marcas 0..L
        n = len(marks)
        k_in = int(np.searchsorted(marks, entry, side="right")) - 1
        k_out = int(np.searchsorted(marks, exit_, side="left")) % n
        w_start = float(marks[(k_in - MICRO_MARGIN) % n])
        w_end = float(marks[(k_out + MICRO_MARGIN) % n])
        span = (w_end - w_start) % L
        if span <= 0.0:
            span = L
        return w_start, span

    # ------------------------------------------------------------ medición

    def _duration(self, pos: np.ndarray, t: np.ndarray,
                  p0: float, span: float) -> float | None:
        if pos[0] <= p0 and p0 + span <= pos[-1]:
            t0 = float(np.interp(p0, pos, t))
            t1 = float(np.interp(p0 + span, pos, t))
            if t1 > t0:
                return t1 - t0
        return None

    def _in_pit_between(self, drv: str, t0: float, t1: float) -> bool:
        for _lap, t_in, t_out in self.hub.pit_lane.get(drv, []):
            end = t_out if t_out is not None else float("inf")
            if t_in <= t1 and t0 <= end:
                return True
        return False

    def reference(self, w_start: float, span: float) -> tuple[float, int] | None:
        """Cruce limpio promedio: últimas REF_LAPS vueltas limpias (verde,
        sin boxes) de cada auto del top REF_TOP por posición de pista."""
        hub = self.hub
        L = hub.track_length
        pts = {}
        for drv in hub.buffers:
            pt = self.analyzer.position_time(drv)
            if pt is not None and len(pt[0]) >= 2:
                pts[drv] = pt
        top = sorted(pts, key=lambda d: float(pts[d][0][-1]),
                     reverse=True)[:REF_TOP]
        samples: list[float] = []
        for drv in top:
            pos, t = pts[drv]
            k_hi = int(math.floor((float(pos[-1]) - span - w_start) / L))
            k_lo = int(math.ceil((float(pos[0]) - w_start) / L))
            good = 0
            for k in range(k_hi, k_lo - 1, -1):  # del más reciente hacia atrás
                p0 = k * L + w_start
                dur = self._duration(pos, t, p0, span)
                if dur is None:
                    continue
                t0 = float(np.interp(p0, pos, t))
                t1 = t0 + dur
                if not clean_at(hub, t0, t1) or self._in_pit_between(drv, t0, t1):
                    continue
                samples.append(dur)
                good += 1
                if good >= REF_LAPS:
                    break
        if len(samples) < REF_LAPS:
            return None
        return float(np.mean(samples)), len(samples)

    def estimate(self) -> tuple[float, int, int] | None:
        """(Ventana de Box en s, muestras de paradas, muestras de referencia)
        — None hasta que haya al menos una parada cerrada medible."""
        bounds = self.window_bounds()
        if bounds is None:
            return None
        w_start, span = bounds
        ref = self.reference(w_start, span)
        if ref is None:
            return None
        ref_time, n_ref = ref
        hub = self.hub
        L = hub.track_length
        losses: list[float] = []
        for drv, visits in hub.pit_lane.items():
            pt = self.analyzer.position_time(drv)
            if pt is None or len(pt[0]) < 2:
                continue
            pos, t = pt
            for _lap, t_in, t_out in visits:
                if t_out is None:
                    continue
                if not (t[0] <= t_in <= t[-1]):
                    continue
                p_in = float(np.interp(t_in, t, pos))
                p0 = math.floor((p_in - w_start) / L) * L + w_start
                dur = self._duration(pos, t, p0, span)
                if dur is None:
                    continue
                stop = hub.pit_stationary_time(drv, float(t_in),
                                               float(t_out))
                losses.append(dur - ref_time - (stop - STOP_NORM))
        if not losses:
            return None
        return float(np.median(losses)), len(losses), n_ref


class PitStrategyView(QWidget):
    """Panel Pit strategy: Ventana de Box (auto/manual con traba) y la
    proyección de rejoin de cada auto si parara ahora (detención de 3 s)."""

    def __init__(self, hub: DataHub, cfg: dict | None = None, parent=None):
        super().__init__(parent)
        self.hub = hub
        self.cfg = cfg if cfg is not None else {}
        self.analyzer = TimingAnalyzer(hub)
        self.estimator = PitWindowEstimator(hub, self.analyzer)
        self._last_est = 0.0
        self._last_table = 0.0

        scfg = self.cfg.setdefault("strategy", {})
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 2, 4, 4)
        lay.setSpacing(4)
        row = QHBoxLayout()
        row.addWidget(QLabel("Pit window:"))
        self.window_spin = QDoubleSpinBox()
        self.window_spin.setRange(0.0, 120.0)
        self.window_spin.setDecimals(1)
        self.window_spin.setSingleStep(0.5)
        self.window_spin.setSuffix(" s")
        self.window_spin.setValue(float(scfg.get("pit_window", 20.0)))
        self.window_spin.valueChanged.connect(self._value_edited)
        row.addWidget(self.window_spin)
        self.lock_check = QCheckBox("Lock (manual)")
        self.lock_check.setToolTip(
            "Locked: the automatic estimate never overwrites the value")
        self.lock_check.setChecked(bool(scfg.get("pit_window_locked", False)))
        self.lock_check.toggled.connect(self._lock_toggled)
        row.addWidget(self.lock_check)
        self.auto_label = QLabel("auto: —")
        self.auto_label.setStyleSheet(f"color: {theme.TEXT_MUTED};")
        self.auto_label.setToolTip(
            "Box window measured on track (2 microsectors before pit entry "
            "to 2 after pit exit), stop normalized to 3 s, reference = last "
            "3 clean laps of the top 5")
        row.addWidget(self.auto_label)
        row.addStretch(1)
        lay.addLayout(row)

        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(
            ["P", "Driver", "Gap", "Net", "→ P", "Behind", "Margin"])
        self.table.horizontalHeaderItem(3).setToolTip(
            "Net position in the pit cycle: virtual order once every car "
            "with pending stops pays one Pit window (green = gains, red = "
            "loses vs today's track position)")
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setStyleSheet("QTableWidget { font-size: 8pt; }")
        for col, width in ((0, 28), (2, 56), (3, 40), (4, 36), (6, 64)):
            self.table.setColumnWidth(col, width)
        lay.addWidget(self.table, stretch=1)

    # ------------------------------------------------------------- config

    def _value_edited(self, value: float) -> None:
        self.cfg.setdefault("strategy", {})["pit_window"] = float(value)
        config.save_config(self.cfg)

    def _lock_toggled(self, on: bool) -> None:
        self.cfg.setdefault("strategy", {})["pit_window_locked"] = on
        config.save_config(self.cfg)

    def apply_auto(self, value: float, n_pits: int, n_ref: int) -> None:
        """Volcar el estimado al valor efectivo, salvo que esté trabado."""
        self.auto_label.setText(
            f"auto: {value:.1f}s ({n_pits} stops · {n_ref} ref laps)")
        if self.lock_check.isChecked():
            return
        if abs(self.window_spin.value() - value) >= 0.05:
            self.window_spin.blockSignals(True)
            self.window_spin.setValue(round(value, 1))
            self.window_spin.blockSignals(False)
            self.cfg.setdefault("strategy", {})["pit_window"] = round(value, 1)

    # ------------------------------------------------------------ refresco

    def clear_data(self) -> None:
        self.analyzer.clear()
        self.table.setRowCount(0)
        self.auto_label.setText("auto: —")
        self._last_est = 0.0

    def current_gaps(self) -> tuple[list[str], dict[str, float | None]]:
        return current_gaps(self.hub, self.analyzer)

    def refresh(self) -> None:
        now = time.monotonic()
        if now - self._last_est > 5.0:
            self._last_est = now
            est = self.estimator.estimate()
            if est is not None:
                self.apply_auto(*est)
        if now - self._last_table < 1.0:
            return
        self._last_table = now
        ordered, gaps = self.current_gaps()
        window = float(self.window_spin.value())
        # posición NETA en el ciclo de paradas: el orden en pista miente
        # mientras unos pararon y otros no — a cada auto le faltan
        # (máx_paradas − propias) paradas, cada una vale una Ventana de Box
        stops = {d: len(self.hub.pit_stops_done(d)) for d in ordered}
        max_stops = max(stops.values(), default=0)
        virtual: dict[str, float] = {}
        for d in ordered:
            g = gaps.get(d)
            if g is not None:
                virtual[d] = g + (max_stops - stops[d]) * window
        net_order = sorted(virtual, key=lambda d: virtual[d])
        net_pos = {d: k + 1 for k, d in enumerate(net_order)}
        self.table.setRowCount(len(ordered))
        for i, drv in enumerate(ordered):
            info = self.hub.drivers.get(drv)
            code = info.code if info else drv
            gap = gaps.get(drv)
            proj = project_rejoin(gaps, drv, window)
            net = net_pos.get(drv)
            cells = [str(i + 1), code,
                     "—" if gap is None else f"+{gap:.1f}",
                     f"P{net}" if net else "—",
                     "—", "—", "—"]
            if proj is not None:
                new_pos, (behind_drv, margin), _ahead = proj
                binfo = self.hub.drivers.get(behind_drv or "")
                cells[4] = f"P{new_pos}"
                cells[5] = (binfo.code if binfo else (behind_drv or "—"))
                cells[6] = "—" if margin != margin else f"+{margin:.1f}s"
            for c, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if c == 1 and info is not None:
                    item.setForeground(Qt.white)
                if c == 3 and net is not None:
                    if net < i + 1:
                        item.setForeground(QColor("#2fbf71"))
                    elif net > i + 1:
                        item.setForeground(QColor("#ff6b5e"))
                self.table.setItem(i, c, item)
