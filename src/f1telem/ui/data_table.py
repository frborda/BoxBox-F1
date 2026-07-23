"""Data tables: TODOS los datos de timing en modo tabla, en su propia
ventana (antes vivían dentro de Times / Gap).

- Pestaña "All data" (propia): una fila por piloto y por vuelta con el
  tiempo, los 3 parciales, compuesto y edad del neumático, AVG5/AVG10
  rodantes a esa vuelta y flags (P = paró, OUT = vuelta de salida).
- El resto de las pestañas son las clásicas (Summary, By lap,
  Microsectors, Official µ, Corners, Degradation), adoptadas de Times/Gap
  y ahora gobernadas por los selectores de este panel.

Selector de pilotos (👥, todos por defecto) y de rango de vueltas: aplican
a todas las pestañas. Sin spoilers: solo vueltas completadas al timeline.
"""
from __future__ import annotations

import math

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QSpinBox, QTableWidget, QVBoxLayout, QWidget,
)

from ..hub import DataHub
from . import theme
from .driver_filter import DriverSelectButton
from .timing_view import (
    _BEST_COLOR, _COMPOUND_BG, _cell, fmt_laptime, fmt_secs,
)

ALL_COLS = ["Driver", "Lap", "Time", "S1", "S2", "S3", "Tyre", "Age",
            "AVG5", "AVG10", "Flags"]


class DataTableView(QWidget):
    changed = Signal()

    def __init__(self, hub: DataHub, cfg: dict | None = None,
                 timing_view=None, parent=None):
        super().__init__(parent)
        self.hub = hub
        self.tv = timing_view
        self.analyzer = timing_view.analyzer
        self._auto = True
        self._sig: tuple | None = None

        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 2, 4, 4)
        lay.setSpacing(4)
        row = QHBoxLayout()
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
            spin.valueChanged.connect(self._filter_changed)
            row.addWidget(spin)
        note = QLabel("Both selectors apply to every tab")
        note.setStyleSheet(f"color: {theme.TEXT_MUTED}; font-size: 7.5pt;")
        row.addWidget(note)
        row.addStretch(1)
        lay.addLayout(row)

        # pestaña propia "All data": piloto × vuelta con todo
        self.table = QTableWidget(0, len(ALL_COLS))
        self.table.setHorizontalHeaderLabels(ALL_COLS)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionMode(QTableWidget.NoSelection)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeaderItem(8).setToolTip(
            "Rolling average of the 5 laps up to this one (pit in/out "
            "laps excluded)")
        self.table.horizontalHeaderItem(10).setToolTip(
            "P = pitted on this lap · OUT = out lap")
        # adopta las pestañas clásicas de Times/Gap y antepone la propia
        self.tabs = timing_view.tabs
        self.tabs.insertTab(0, self.table, "All data")
        self.tabs.setCurrentIndex(0)
        lay.addWidget(self.tabs, stretch=1)
        self._auto_fill()

    # ---------------------------------------------------------- selección

    def _user_touched(self) -> None:
        self._auto = False
        self._filter_changed()

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

    def _filter_changed(self, *_a) -> None:
        self._push_filter()
        self._sig = None
        self.refresh()
        self.changed.emit()

    def _push_filter(self) -> None:
        """Los selectores de este panel gobiernan también las pestañas
        clásicas (vía TimingView.table_filter)."""
        lo, hi = self.lap_range()
        sel = self.drivers()
        new = (set(sel) if sel else None, lo, hi)
        if new != self.tv.table_filter:
            self.tv.table_filter = new
            self.tv._laps_signature = None

    # ------------------------------------------------------------ refresco

    def clear_data(self) -> None:
        self._sig = None
        self.table.setRowCount(0)

    def _laps_for(self, drv: str) -> list[int]:
        lo, hi = self.lap_range()
        buf = self.hub.buffers.get(drv)
        laps = buf.completed_laps() if buf is not None and buf.n else []
        return [l for l in laps if l >= lo and (hi is None or l <= hi)]

    def _avg_at(self, drv: str, lap: int, n: int) -> float:
        """Promedio de las n vueltas hasta `lap` inclusive, sin vueltas de
        entrada/salida de boxes (mismo criterio que la torre)."""
        pit_laps = {p_lap for p_lap, _t in self.hub.pits.get(drv, [])}
        times = []
        for l in range(lap, 0, -1):
            if l in pit_laps or (l - 1) in pit_laps:
                continue
            t = self.analyzer.lap_time(drv, l)
            if math.isfinite(t):
                times.append(t)
            if len(times) >= n:
                break
        return (sum(times) / len(times)) if times else float("nan")

    def refresh(self) -> None:
        self._push_filter()
        drivers = self.drivers()
        per_drv = {d: self._laps_for(d) for d in drivers}
        sig = (tuple(drivers), self.lap_range(),
               tuple((d, len(v)) for d, v in sorted(per_drv.items())))
        if sig != self._sig:
            self._sig = sig
            self._rebuild_all_data(per_drv)
        # la pestaña clásica visible se refresca con el filtro ya aplicado
        self.tv.refresh_tables()

    def _rebuild_all_data(self, per_drv: dict[str, list[int]]) -> None:
        an = self.analyzer
        # orden por posición en pista, como el resto de las tablas
        ordered = [d for d in self.tv._by_track_position()
                   if d in per_drv] or sorted(per_drv)
        rows = [(d, lap) for d in ordered for lap in per_drv.get(d, [])]
        # violeta del mejor absoluto del rango; negrita al mejor del piloto
        finite = {}
        for d, lap in rows:
            t = an.lap_time(d, lap)
            if math.isfinite(t):
                finite[(d, lap)] = t
        session_best = min(finite.values(), default=math.inf)
        drv_best = {}
        for (d, _l), t in finite.items():
            drv_best[d] = min(drv_best.get(d, math.inf), t)

        self.table.setRowCount(len(rows))
        for r, (drv, lap) in enumerate(rows):
            t = finite.get((drv, lap), float("nan"))
            is_sb = math.isfinite(t) and abs(t - session_best) < 1e-9
            is_pb = math.isfinite(t) and abs(t - drv_best.get(drv, -1)) < 1e-9
            self.table.setItem(r, 0, _cell(self._code_of(drv), bold=True))
            self.table.setItem(r, 1, _cell(f"L{lap}"))
            self.table.setItem(r, 2, _cell(
                fmt_laptime(t), fg=_BEST_COLOR if is_sb else None,
                bold=is_sb or is_pb))
            sectors = an.sector_times(drv, lap)
            for k in range(3):
                self.table.setItem(r, 3 + k, _cell(fmt_secs(sectors[k])))
            tyre = self.hub.tyres.get(drv, {}).get(lap)
            if tyre and tyre[0]:
                item = _cell(tyre[0].title())
                bg = _COMPOUND_BG.get(tyre[0].upper())
                if bg is not None:
                    item.setBackground(bg)
                self.table.setItem(r, 6, item)
                self.table.setItem(r, 7, _cell(str(tyre[1])))
            else:
                self.table.setItem(r, 6, _cell("—"))
                self.table.setItem(r, 7, _cell("—"))
            self.table.setItem(r, 8, _cell(
                fmt_laptime(self._avg_at(drv, lap, 5))))
            self.table.setItem(r, 9, _cell(
                fmt_laptime(self._avg_at(drv, lap, 10))))
            pit_laps = {p_lap for p_lap, _t in self.hub.pits.get(drv, [])}
            flags = []
            if lap in pit_laps:
                flags.append("P")
            if (lap - 1) in pit_laps:
                flags.append("OUT")
            self.table.setItem(r, 10, _cell(" ".join(flags)))

    def _code_of(self, drv: str) -> str:
        info = self.hub.drivers.get(drv)
        return info.code if info else drv
