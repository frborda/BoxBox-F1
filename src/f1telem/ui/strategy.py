"""Vista de estrategia: una barra horizontal por piloto con sus stints de
neumáticos coloreados por compuesto (estilo F1 TV), ordenada por posición
de pista. La línea vertical marca la vuelta actual del líder."""
from __future__ import annotations

from PySide6.QtCore import QEvent, Qt, QRectF
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QScrollArea, QToolTip, QVBoxLayout, QWidget

from ..hub import DataHub
from ..timing import TimingAnalyzer
from . import theme

ROW_H = 22
LEFT_W = 64  # caja de posición + sigla


def collect_stints(tyre_map: dict[int, tuple[str, int]]) -> list[tuple[str, int, int]]:
    """Mapa por vuelta {vuelta: (compuesto, edad)} -> [(compuesto, l0, l1)].
    Corta el stint cuando cambia el compuesto o la edad se reinicia."""
    stints: list[tuple[str, int, int]] = []
    prev_age = None
    for lap in sorted(tyre_map):
        compound, age = tyre_map[lap]
        if not compound:
            prev_age = None
            continue
        if (not stints or stints[-1][0] != compound
                or (prev_age is not None and age <= prev_age)
                or lap > stints[-1][2] + 1):
            stints.append((compound, lap, lap))
        else:
            stints[-1] = (compound, stints[-1][1], lap)
        prev_age = age
    return stints


class _StrategyCanvas(QWidget):
    def __init__(self, view: "StrategyView"):
        super().__init__()
        self.view = view

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        self.view._paint(painter, self.width())
        painter.end()

    def event(self, ev) -> bool:
        if ev.type() == QEvent.ToolTip:
            idx = int(ev.pos().y() // ROW_H)
            rows = self.view.rows
            if 0 <= idx < len(rows):
                _drv, code, _color, stints = rows[idx]
                lines = [code] + [
                    f"{comp.title()}: L{l0}–L{l1} ({l1 - l0 + 1} laps)"
                    for comp, l0, l1 in stints
                ]
                QToolTip.showText(ev.globalPos(), "\n".join(lines), self)
            else:
                QToolTip.hideText()
            return True
        return super().event(ev)


class StrategyView(QWidget):
    def __init__(self, hub: DataHub, parent=None):
        super().__init__(parent)
        self.hub = hub
        self.analyzer = TimingAnalyzer(hub)
        self.rows: list[tuple] = []  # (drv, code, color, [(comp, l0, l1)])
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.canvas = _StrategyCanvas(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setWidget(self.canvas)
        lay.addWidget(scroll)

    def clear_data(self) -> None:
        self.analyzer.clear()
        self.rows = []
        self.canvas.update()

    def refresh(self) -> None:
        hub = self.hub
        an = self.analyzer
        # orden por posición de pista (como la torre); sin muestras aún,
        # cualquier piloto con datos de neumáticos igual aparece
        pos = {}
        for drv in hub.tyres:
            pt = an.position_time(drv)
            if pt is not None:
                pos[drv] = float(pt[0][-1])
        ordered = sorted(hub.tyres, key=lambda d: pos.get(d, float("-inf")),
                         reverse=True)
        rows = []
        for drv in ordered:
            # sin spoilers: el plan futuro del replay no se muestra
            stints = collect_stints(hub.tyres_until_now(drv))
            if not stints:
                continue
            info = hub.drivers.get(drv)
            rows.append((drv, info.code if info else drv,
                         info.color if info else "#9aa0a6", stints))
        self.rows = rows
        self.canvas.setMinimumHeight(len(rows) * ROW_H + 16)
        self.canvas.update()

    # ------------------------------------------------------------- pintado

    def _lead_lap(self) -> int:
        cur, total = self.hub.lap_count
        if cur > 0:
            return cur
        return max((b.current_lap() for b in self.hub.buffers.values() if b.n),
                   default=0)

    def _paint(self, p: QPainter, width: int) -> None:
        if not self.rows:
            return
        lead = self._lead_lap()
        total = self.hub.lap_count[1]
        if not total:  # sin total oficial: hasta donde haya datos
            total = max(s[2] for _d, _c, _col, st in self.rows for s in st)
        total = max(total, lead, 1)
        bar_w = max(width - LEFT_W - 8, 50)
        x_of = lambda lap: LEFT_W + (lap / total) * bar_w  # fin de esa vuelta

        f_small = QFont(self.font()); f_small.setPointSizeF(6.5)
        f_code = QFont(self.font()); f_code.setPointSizeF(7.5); f_code.setBold(True)

        # ticks de vuelta cada 5/10 según el ancho disponible
        step = 5 if bar_w / total * 5 >= 22 else 10
        p.setFont(f_small)
        for lap in range(step, total + 1, step):
            x = x_of(lap)
            p.setPen(QPen(QColor(theme.BORDER), 1))
            p.drawLine(int(x), 12, int(x), len(self.rows) * ROW_H + 12)
            p.setPen(QColor(theme.TEXT_MUTED))
            p.drawText(QRectF(x - 14, 0, 28, 10), Qt.AlignCenter, str(lap))

        for i, (_drv, code, color, stints) in enumerate(self.rows):
            y = i * ROW_H + 12
            team = QColor(color)
            p.setPen(Qt.NoPen)
            p.setBrush(team)
            p.drawRoundedRect(QRectF(2, y + 3, LEFT_W - 8, ROW_H - 6), 3, 3)
            lum = 0.299 * team.redF() + 0.587 * team.greenF() + 0.114 * team.blueF()
            p.setPen(QColor("#111318") if lum > 0.55 else QColor("#ffffff"))
            p.setFont(f_code)
            p.drawText(QRectF(2, y + 3, LEFT_W - 8, ROW_H - 6),
                       Qt.AlignCenter, code)
            for comp, l0, l1 in stints:
                if l0 > total:
                    continue
                cx0 = x_of(l0 - 1)
                cx1 = x_of(min(l1, total))
                cc = QColor(theme.COMPOUND_COLORS.get(comp.upper(), "#9aa0a6"))
                p.setPen(QPen(QColor(theme.SURFACE), 1))
                p.setBrush(cc)
                p.drawRoundedRect(QRectF(cx0, y + 5, cx1 - cx0, ROW_H - 10), 2, 2)
                if cx1 - cx0 > 30:
                    p.setPen(QColor("#111318"))
                    p.setFont(f_small)
                    p.drawText(QRectF(cx0, y + 4, cx1 - cx0, ROW_H - 8),
                               Qt.AlignCenter, f"{comp[:1]} {l1 - l0 + 1}")

        if lead:
            x = x_of(lead)
            p.setPen(QPen(QColor(theme.ACCENT), 1, Qt.DashLine))
            p.drawLine(int(x), 10, int(x), len(self.rows) * ROW_H + 14)
