"""Panel de dirección de carrera: log cronológico de los mensajes oficiales
(banderas, SC/VSC, investigaciones y sanciones), coloreados por bandera."""
from __future__ import annotations

from PySide6.QtGui import QColor
from PySide6.QtWidgets import QListWidget, QListWidgetItem, QVBoxLayout, QWidget

from ..hub import DataHub
from . import theme


def _fmt_t(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


class RaceControlPanel(QWidget):
    def __init__(self, hub: DataHub, parent=None):
        super().__init__(parent)
        self.hub = hub
        self._count = 0
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.list = QListWidget()
        self.list.setWordWrap(True)
        self.list.setStyleSheet("QListWidget { font-size: 8pt; }")
        lay.addWidget(self.list)

    def clear_data(self) -> None:
        self.list.clear()
        self._count = 0

    def refresh(self) -> None:
        # sin spoilers: solo mensajes hasta el instante actual del timeline
        # (el replay publica la lista completa por adelantado)
        rows = [r for r in self.hub.race_control
                if float(r.get("t", 0.0)) <= self.hub.latest_t]
        if len(rows) < self._count:  # seek atrás / historia re-emitida
            self.clear_data()
        if len(rows) == self._count:
            return
        for msg in rows[self._count:]:
            lap = msg.get("lap")
            head = _fmt_t(float(msg.get("t", 0.0)))
            if lap:
                head += f" · L{lap}"
            item = QListWidgetItem(f"[{head}]  {msg.get('message', '')}")
            flag = str(msg.get("flag", "")).upper()
            color = theme.FLAG_COLORS.get(flag)
            if color is None and msg.get("mode"):
                color = "#ff9f1a"  # SC / VSC
            item.setForeground(QColor(color or theme.TEXT))
            self.list.addItem(item)
        self._count = len(rows)
        self.list.scrollToBottom()
