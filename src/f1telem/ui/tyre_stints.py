"""Panel Tyre Stints estilo broadcast: una fila por piloto (orden de
carrera) con un chip por stint — la letra del compuesto en su color, las
vueltas del stint ("18L") y una "N" verde si el juego era nuevo al
montarlo. Complementa al panel Tyre strategy (barras a escala de carrera):
acá cada stint se lee como lista, sin escala temporal."""
from __future__ import annotations

from PySide6.QtCore import QEvent, Qt, QRectF
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPen
from PySide6.QtWidgets import QScrollArea, QToolTip, QVBoxLayout, QWidget

from ..hub import DataHub
from ..timing import TimingAnalyzer
from . import theme
from .strategy import collect_stints

ROW_H = 30
LEFT_W = 58       # barrita de color + sigla
CHIP_H = 20
NEW_COLOR = "#2fbf71"


class _StintsCanvas(QWidget):
    def __init__(self, view: "TyreStintsView"):
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
                    f"{comp.title()}: L{l0}–L{l1} ({l1 - l0 + 1} laps, "
                    + ("new set" if new else "used set")
                    for comp, l0, l1, new in stints
                ]
                QToolTip.showText(ev.globalPos(), "\n".join(lines), self)
            else:
                QToolTip.hideText()
            return True
        return super().event(ev)


class TyreStintsView(QWidget):
    def __init__(self, hub: DataHub, parent=None):
        super().__init__(parent)
        self.hub = hub
        self.analyzer = TimingAnalyzer(hub)
        # (drv, code, color, [(comp, l0, l1, nuevo)])
        self.rows: list[tuple] = []
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.canvas = _StintsCanvas(self)
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
        pos = {}
        for drv in hub.tyres:
            pt = an.position_time(drv)
            if pt is not None:
                pos[drv] = float(pt[0][-1])
        ordered = sorted(hub.tyres, key=lambda d: pos.get(d, float("-inf")),
                         reverse=True)
        rows = []
        for drv in ordered:
            # sin spoilers: neumáticos solo hasta la vuelta en curso
            tyre_map = hub.tyres_until_now(drv)
            stints = collect_stints(tyre_map)
            if not stints:
                continue
            # juego nuevo: la edad en la primera vuelta del stint es 0/1
            # (con un juego usado la edad arranca más arriba)
            full = [(comp, l0, l1, tyre_map.get(l0, ("", 99))[1] <= 1)
                    for comp, l0, l1 in stints]
            info = hub.drivers.get(drv)
            rows.append((drv, info.code if info else drv,
                         info.color if info else "#9aa0a6", full))
        self.rows = rows
        self.canvas.setMinimumHeight(len(rows) * ROW_H + 8)
        self.canvas.update()

    # ------------------------------------------------------------- pintado

    def _paint(self, p: QPainter, width: int) -> None:
        f_code = QFont(self.font()); f_code.setPointSizeF(8.5); f_code.setBold(True)
        f_chip = QFont(self.font()); f_chip.setPointSizeF(7.5); f_chip.setBold(True)
        f_comp = QFont(self.font()); f_comp.setPointSizeF(7.0); f_comp.setBold(True)
        fm = QFontMetricsF(f_chip)

        for i, (_drv, code, color, stints) in enumerate(self.rows):
            y = i * ROW_H
            cy = y + ROW_H / 2.0
            # barrita en color de equipo + sigla
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(color))
            p.drawRoundedRect(QRectF(4, y + 5, 3, ROW_H - 10), 1.5, 1.5)
            p.setPen(QColor(theme.TEXT))
            p.setFont(f_code)
            p.drawText(QRectF(12, y, LEFT_W - 12, ROW_H),
                       Qt.AlignVCenter | Qt.AlignLeft, code)

            x = float(LEFT_W)
            for comp, l0, l1, new in stints:
                laps_txt = f"{l1 - l0 + 1}L"
                w_laps = fm.horizontalAdvance(laps_txt)
                w_new = fm.horizontalAdvance(" N") if new else 0.0
                chip_w = 6 + 14 + 5 + w_laps + w_new + 6
                if x + chip_w > width - 4:
                    break  # sin lugar: el tooltip lista todos los stints
                chip = QRectF(x, cy - CHIP_H / 2.0, chip_w, CHIP_H)
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(theme.SURFACE_ALT))
                p.drawRoundedRect(chip, CHIP_H / 2.0, CHIP_H / 2.0)
                # circulito del compuesto: anillo y letra en su color
                cc = QColor(theme.COMPOUND_COLORS.get(comp.upper(), "#9aa0a6"))
                circle = QRectF(x + 6, cy - 7, 14, 14)
                p.setPen(QPen(cc, 1.6))
                p.setBrush(Qt.NoBrush)
                p.drawEllipse(circle)
                p.setFont(f_comp)
                p.setPen(cc)
                p.drawText(circle, Qt.AlignCenter, comp[:1].upper())
                # vueltas del stint y "N" de juego nuevo
                p.setFont(f_chip)
                p.setPen(QColor(theme.TEXT))
                text_rect = QRectF(x + 6 + 14 + 5, y, w_laps, ROW_H)
                p.drawText(text_rect, Qt.AlignVCenter | Qt.AlignLeft, laps_txt)
                if new:
                    p.setPen(QColor(NEW_COLOR))
                    p.drawText(QRectF(text_rect.right(), y, w_new, ROW_H),
                               Qt.AlignVCenter | Qt.AlignLeft, " N")
                x += chip_w + 6
            p.setPen(QPen(QColor(theme.BORDER), 1))
            p.drawLine(0, y + ROW_H - 1, width, y + ROW_H - 1)
