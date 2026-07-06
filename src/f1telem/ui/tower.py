"""Torre de tiempos tipo F1 TV: todos los autos ordenados por posición en
pista, con gap al líder, intervalo con el de adelante, última vuelta y
mejor vuelta (verde = mejor personal, violeta = mejor de la sesión).
"""
from __future__ import annotations

import math

import numpy as np
from PySide6.QtWidgets import QHBoxLayout, QLabel, QTableWidget, QVBoxLayout, QWidget

from ..hub import DataHub
from ..timing import TimingAnalyzer
from . import theme
from .timing_view import _cell, fmt_laptime

_GREEN = "#2fbf71"   # mejor vuelta personal
_PURPLE = "#c9a1ff"  # mejor vuelta de la sesión


class TimingTower(QWidget):
    def __init__(self, hub: DataHub, parent=None):
        super().__init__(parent)
        self.hub = hub
        self.analyzer = TimingAnalyzer(hub)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        head = QHBoxLayout()
        title = QLabel("Timing tower")
        title.setStyleSheet(f"color: {theme.TEXT_MUTED}; font-weight: bold;")
        head.addWidget(title)
        head.addStretch(1)
        self.flag_label = QLabel("")
        self.flag_label.setVisible(False)
        head.addWidget(self.flag_label)
        lay.addLayout(head)
        self.table = QTableWidget(0, 9)
        self.table.setHorizontalHeaderLabels(
            ["P", "Driver", "Gap", "Int", "Last", "Best", "Pit", "AVG5", "AVG10"]
        )
        for c in (7, 8):
            self.table.horizontalHeaderItem(c).setToolTip(
                "Average of the last 5/10 completed laps (excluding pit in/out laps)"
            )
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(20)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionMode(QTableWidget.NoSelection)
        self.table.setAlternatingRowColors(True)
        for c, w in enumerate((22, 44, 52, 46, 58, 58, 26, 58, 58)):
            self.table.setColumnWidth(c, w)
        lay.addWidget(self.table)

    def clear_data(self) -> None:
        self.analyzer.clear()
        self.table.setRowCount(0)
        self.flag_label.setVisible(False)

    def _update_flag(self, t_now: float) -> None:
        """Banner de bandera/SC si hay un período activo en este instante."""
        for t0, t1, code in self.hub.track_status:
            if t0 <= t_now <= t1 and code in theme.TRACK_STATUS:
                text, color = theme.TRACK_STATUS[code]
                self.flag_label.setText(text)
                self.flag_label.setStyleSheet(
                    f"background: {color}; color: #111318; font-weight: bold;"
                    "padding: 1px 6px; border-radius: 3px;"
                )
                self.flag_label.setVisible(True)
                return
        self.flag_label.setVisible(False)

    def _catch_laps(self, drv: str, ahead: str, pts: dict, L: float) -> float | None:
        """Vueltas para alcanzar al de adelante, según la tendencia del gap
        en las últimas ~3 vueltas (None si no se está acercando)."""
        pos_d, t_d = pts[drv]
        pos_a, t_a = pts[ahead]
        p1 = float(pos_d[-1])
        p0 = p1 - 3.0 * L
        if p0 <= max(float(pos_d[0]), float(pos_a[0])):
            return None

        def gap_at(p: float) -> float:
            return float(np.interp(p, pos_d, t_d)) - float(np.interp(p, pos_a, t_a))

        g1 = gap_at(p1)
        rate = (gap_at(p0) - g1) / 3.0  # segundos ganados por vuelta
        if g1 <= 0.0 or rate < 0.05:
            return None
        return g1 / rate

    def _avg_lap(self, drv: str, n: int) -> float:
        """Promedio de las últimas n vueltas cerradas, sin vueltas de boxes."""
        an = self.analyzer
        pit_laps = {p_lap for p_lap, _t in self.hub.pits.get(drv, [])}
        times = []
        buf = self.hub.buffers.get(drv)
        if buf is None:
            return float("nan")
        for lap in reversed(buf.completed_laps()):
            if lap in pit_laps or (lap - 1) in pit_laps:
                continue
            lap_time = an.lap_time(drv, lap)
            if math.isfinite(lap_time):
                times.append(lap_time)
            if len(times) >= n:
                break
        if len(times) < 2:
            return float("nan")
        return float(sum(times) / len(times))

    def refresh(self) -> None:
        an = self.analyzer
        pts = {}
        for drv, buf in self.hub.buffers.items():
            if buf.n:
                pt = an.position_time(drv)
                if pt is not None:
                    pts[drv] = pt
        ordered = sorted(pts, key=lambda d: float(pts[d][0][-1]), reverse=True)
        self.table.setRowCount(len(ordered))
        if not ordered:
            return
        L = self.hub.track_length
        leader = ordered[0]
        pos_leader, t_leader = pts[leader]
        an = self.analyzer
        # mejores vueltas (cacheadas en el analizador por vuelta cerrada)
        bests = {drv: an.best_lap(drv) for drv in ordered}
        session_best = min(
            (b[1] for b in bests.values() if b is not None), default=math.inf
        )
        self._update_flag(max(float(pts[d][1][-1]) for d in ordered))
        prev_gap: float | None = 0.0
        for i, drv in enumerate(ordered):
            info = self.hub.drivers.get(drv)
            code = info.code if info else drv
            color = info.color if info else "#9aa0a6"
            pos_now = float(pts[drv][0][-1])
            t_now = float(pts[drv][1][-1])
            # el gap arranca al terminar el S1 de la vuelta 1 y solo con
            # posiciones reales (offset de grilla estimado o vuelta cerrada)
            ready = (an.real_positions_ready(drv)
                     and an.real_positions_ready(leader)
                     and pos_now >= L / 3.0)
            if i == 0:
                gap_txt, int_txt = "leader", "—"
                gap_val: float | None = 0.0
            elif not ready:
                gap_txt, int_txt, gap_val = "—", "—", None
            else:
                behind = float(pos_leader[-1]) - pos_now
                if behind >= L:
                    gap_txt, gap_val = f"+{int(behind // L)}L", None
                else:
                    gap_val = t_now - float(np.interp(pos_now, pos_leader, t_leader))
                    gap_txt = f"+{gap_val:.1f}"
                int_txt = (f"+{gap_val - prev_gap:.1f}"
                           if gap_val is not None and prev_gap is not None else "—")
            prev_gap = gap_val
            # última vuelta cerrada, coloreada si es mejor personal/sesión
            last_lap = an.last_completed_lap(drv)
            last_time = an.lap_time(drv, last_lap) if last_lap else float("nan")
            best = bests.get(drv)
            last_fg = None
            if math.isfinite(last_time):
                if last_time <= session_best + 1e-9:
                    last_fg = _PURPLE
                elif best is not None and last_time <= best[1] + 1e-9:
                    last_fg = _GREEN
            best_fg = None
            best_txt = "—"
            if best is not None:
                best_txt = fmt_laptime(best[1])
                if best[1] <= session_best + 1e-9:
                    best_fg = _PURPLE
            n_pits = len([1 for lap, _t in self.hub.pits.get(drv, [])
                          if lap <= self.hub.buffers[drv].current_lap()])
            self.table.setItem(i, 0, _cell(str(i + 1), bold=True))
            self.table.setItem(i, 1, _cell(code, fg=color, bold=True))
            self.table.setItem(i, 2, _cell(gap_txt))
            self.table.setItem(i, 3, _cell(int_txt))
            self.table.setItem(i, 4, _cell(fmt_laptime(last_time), fg=last_fg,
                                           bold=last_fg is not None))
            self.table.setItem(i, 5, _cell(best_txt, fg=best_fg,
                                           bold=best_fg is not None))
            self.table.setItem(i, 6, _cell(str(n_pits) if n_pits else "—"))
            self.table.setItem(i, 7, _cell(fmt_laptime(self._avg_lap(drv, 5))))
            self.table.setItem(i, 8, _cell(fmt_laptime(self._avg_lap(drv, 10))))
