"""Strategy Board: qué hacer con cada auto AHORA. Una fila por auto con
su chip de acción (BOX NOW en SC/VSC, COVER a la parada rival, FREE STOP,
BOX FOR AIR, BOX SOON, WATCH, STAY), el porqué corto, la reinserción
proyectada y las amenazas de undercut. El tooltip de cada fila muestra el
RAZONAMIENTO COMPLETO del motor (valores medidos, estimaciones marcadas,
alternativas descartadas), y abajo corre el log de cambios de veredicto —
todo también persistido en strategy-log.jsonl para afinar fases futuras.
"""
from __future__ import annotations

import time

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QLabel, QListWidget, QTableWidget, QTableWidgetItem, QVBoxLayout,
    QWidget,
)

from ..hub import DataHub
from ..strategy_engine import StrategyEngine, neutralization
from ..timing import TimingAnalyzer
from . import theme

_ACTION_COLORS = {
    "BOX NOW": "#e10600",
    "COVER": "#ff8c00",
    "FREE STOP": "#2fbf71",
    "BOX FOR AIR": "#37d0ee",
    "BOX SOON": "#d6be3c",
    "WATCH": "#d6be3c",
    "IN PIT": "#6a7078",
    "STAY": "",
}

_COLS = ["P", "Car", "Tyre", "Action", "Why", "Rejoin now", "Threats"]


class StrategyBoardView(QWidget):
    def __init__(self, hub: DataHub, cfg: dict | None = None, parent=None):
        super().__init__(parent)
        self.hub = hub
        self.cfg = cfg if cfg is not None else {}
        self.analyzer = TimingAnalyzer(hub)
        self.engine = StrategyEngine(hub, self.analyzer)
        self._last_eval = 0.0
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 2, 4, 4)
        lay.setSpacing(4)
        # banner de neutralización: la ventana de decisión más corta de la
        # carrera merece el cartel más grande del panel
        self.banner = QLabel("")
        self.banner.setAlignment(Qt.AlignCenter)
        self.banner.setVisible(False)
        lay.addWidget(self.banner)
        self.table = QTableWidget(0, len(_COLS))
        self.table.setHorizontalHeaderLabels(_COLS)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionMode(QTableWidget.NoSelection)
        self.table.setAlternatingRowColors(True)
        self.table.setStyleSheet("QTableWidget { font-size: 8pt; }")
        for col, width in ((0, 26), (1, 44), (2, 52), (3, 92)):
            self.table.setColumnWidth(col, width)
        self.table.horizontalHeader().setStretchLastSection(True)
        lay.addWidget(self.table, stretch=3)
        head = QLabel("Decision log (every change, fully traced in "
                      "strategy-log.jsonl)")
        head.setStyleSheet(f"color: {theme.TEXT_MUTED}; font-size: 7.5pt;")
        lay.addWidget(head)
        self.log_list = QListWidget()
        self.log_list.setStyleSheet("font-size: 7.5pt;")
        lay.addWidget(self.log_list, stretch=1)
        self.setMinimumSize(560, 360)

    def clear_data(self) -> None:
        self.engine.reset()
        self.table.setRowCount(0)
        self.log_list.clear()
        self.banner.setVisible(False)
        self._last_eval = 0.0

    def refresh(self) -> None:
        now = time.monotonic()
        if now - self._last_eval < 1.0:
            return
        self._last_eval = now
        # la Ventana de Box vigente la gobierna el panel Pit strategy
        self.engine.pit_window = float(
            self.cfg.get("strategy", {}).get("pit_window", 20.0))
        advices = self.engine.evaluate()
        neutral = neutralization(self.hub)
        if neutral:
            factor = 0.45 if neutral == "SC" else 0.55
            self.banner.setText(
                f"{neutral} — CHEAP STOP WINDOW (pit loss ×{factor:.2f})")
            self.banner.setStyleSheet(
                "background: #d6be3c; color: #111318; font-weight: bold;"
                "font-size: 11pt; padding: 4px; border-radius: 4px;")
            self.banner.setVisible(True)
        else:
            self.banner.setVisible(False)

        order = [d for d in advices]
        self.table.setRowCount(len(order))
        for r, drv in enumerate(order):
            adv = advices[drv]
            info = self.hub.drivers.get(drv)
            stint = adv.factors.get("stint", {})
            tyre = (f"{(stint.get('compound') or '?')[:1]}"
                    f"{stint.get('age', 0)}")
            cells = [str(adv.factors.get("pos", r + 1)),
                     info.code if info else drv, tyre, adv.action,
                     adv.reason, adv.rejoin_txt,
                     " · ".join(adv.threats) if adv.threats else "—"]
            tooltip = "\n".join(adv.trace)
            for c, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setToolTip(tooltip)
                if c == 1 and info is not None:
                    item.setForeground(QColor(info.color))
                if c == 3:
                    color = _ACTION_COLORS.get(
                        adv.action.split()[0] if adv.action.startswith(
                            "COVER") else adv.action, "")
                    if color:
                        item.setForeground(QColor(color))
                    font = item.font()
                    font.setBold(adv.urgency >= 1)
                    item.setFont(font)
                self.table.setItem(r, c, item)
        # log de cambios (el motor lo mantiene; acá solo se refleja)
        if self.log_list.count() != len(self.engine.log):
            self.log_list.clear()
            for t, lap, code, action, reason in self.engine.log:
                mins = int(t // 60)
                self.log_list.addItem(
                    f"L{lap} · {mins}:{t % 60:04.1f} · {code}: {action}"
                    f" — {reason}")
