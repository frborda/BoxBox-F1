"""Panel Pit lane: qué pilotos están AHORA en la calle de boxes, con qué
compuesto entraron, y dos relojes corriendo: tiempo total en la calle y
tiempo detenido (velocidad 0). Ordenado por orden de entrada. El que SALE
no desaparece de golpe: queda atenuado con sus datos congelados hasta que
cruza el fin del sector 2 o pasan 2 minutos, lo que ocurra primero."""
from __future__ import annotations

import numpy as np
from PySide6.QtCore import Qt, QRectF
from PySide6.QtGui import QColor, QFont, QPainter
from PySide6.QtWidgets import QHBoxLayout, QScrollArea, QVBoxLayout, QWidget

from ..hub import DataHub
from . import theme
from .driver_filter import DriverFilterButton

ROW_H = 26
OUT_LINGER_S = 120.0  # retención máxima del que salió (si no cruza el S2)


def _text_on(bg: QColor) -> QColor:
    lum = 0.299 * bg.redF() + 0.587 * bg.greenF() + 0.114 * bg.blueF()
    return QColor("#111318") if lum > 0.55 else QColor("#ffffff")


class _PitlaneCanvas(QWidget):
    def __init__(self, view: "PitlaneView"):
        super().__init__()
        self.view = view

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        self.view._paint(painter, self.width(), self.height())
        painter.end()


class PitlaneView(QWidget):
    def __init__(self, hub: DataHub, cfg: dict | None = None, parent=None):
        super().__init__(parent)
        self.hub = hub
        # (code, color, compuesto, t_calle, t_detenido, out) por piloto
        self.rows: list[tuple] = []
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        row.setContentsMargins(4, 2, 4, 0)
        row.addStretch(1)
        # filtro local de autos visibles (todos por defecto, persistido)
        self.filter_btn = DriverFilterButton(hub, cfg, "pitlane_hidden_cars")
        self.filter_btn.changed.connect(self.refresh)
        row.addWidget(self.filter_btn)
        lay.addLayout(row)
        self.canvas = _PitlaneCanvas(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setWidget(self.canvas)
        lay.addWidget(scroll)

    def clear_data(self) -> None:
        self.rows = []
        self.canvas.update()

    def _passed_s2(self, drv: str, t_out: float, b2: float) -> bool:
        """True si desde la salida el auto ya cruzó el fin del sector 2."""
        buf = self.hub.buffers.get(drv)
        if buf is None or not buf.n:
            return False
        t_col = buf.col("t")
        i0 = int(np.searchsorted(t_col, t_out))
        if i0 >= buf.n:
            return False
        lapcol = buf.col("lap")
        if int(lapcol[buf.n - 1]) > int(lapcol[i0]):
            return True  # cerró la vuelta: seguro pasó el fin del S2
        return bool((buf.col("dist_lap")[i0:] >= b2).any())

    def refresh(self) -> None:
        hub = self.hub
        now = hub.latest_t
        b2 = (hub.sector_bounds[1] if hub.sector_bounds is not None
              else 2.0 * hub.track_length / 3.0)
        rows = []
        for drv in hub.pit_lane:
            if drv in self.filter_btn.hidden:
                continue
            # visita en curso, o recién cerrada (queda atenuada un rato):
            # el replay publica la historia completa por adelantado, así
            # que las futuras no cuentan
            visit = hub.last_pit_visit(drv)
            if visit is None:
                continue
            lap, t_in, t_out = visit
            open_now = hub.pit_visit_open(visit)
            if not open_now:
                # salió: retener atenuado hasta fin de S2 o 2 minutos
                if t_out is None or now - float(t_out) > OUT_LINGER_S:
                    continue
                if self._passed_s2(drv, float(t_out), b2):
                    continue
            info = hub.drivers.get(drv)
            compound = ""
            tyre_map = hub.tyres.get(drv)
            if tyre_map:
                key = lap if lap in tyre_map else max(
                    (k for k in tyre_map if k <= lap), default=max(tyre_map))
                compound = tyre_map[key][0]
            end = now if open_now else float(t_out)
            rows.append((
                info.code if info else drv,
                info.color if info else "#9aa0a6",
                compound,
                max(0.0, end - t_in),
                hub.pit_stationary_time(drv, t_in, end),
                not open_now,  # atenuado: ya salió a pista
                t_in,
            ))
        rows.sort(key=lambda r: (r[5], r[6]))  # adentro primero
        self.rows = [r[:6] for r in rows]
        self.canvas.setMinimumHeight(max(len(self.rows) * ROW_H + 8, 40))
        self.canvas.update()

    def _paint(self, p: QPainter, width: int, height: int) -> None:
        f_small = QFont(self.font()); f_small.setPointSizeF(6.5)
        f_code = QFont(self.font()); f_code.setPointSizeF(7.5); f_code.setBold(True)
        f_val = QFont(self.font()); f_val.setPointSizeF(9.0); f_val.setBold(True)
        if not self.rows:
            p.setPen(QColor(theme.TEXT_MUTED))
            p.setFont(f_small)
            p.drawText(QRectF(0, 0, width, min(height, 40)),
                       Qt.AlignCenter, "— pit lane empty —")
            return
        for i, (code, color, compound, lane_s, stop_s, out) in enumerate(
                self.rows):
            y = i * ROW_H + 4
            p.setOpacity(0.45 if out else 1.0)
            if i % 2:
                p.fillRect(0, y - 2, width, ROW_H, QColor(theme.SURFACE_ALT))
            team = QColor(color)
            p.setPen(Qt.NoPen)
            p.setBrush(team)
            p.drawRoundedRect(QRectF(4, y + 1, 46, ROW_H - 6), 3, 3)
            p.setPen(_text_on(team))
            p.setFont(f_code)
            p.drawText(QRectF(4, y + 1, 46, ROW_H - 6), Qt.AlignCenter, code)
            x = 56
            # compuesto con el que entró
            if compound:
                cc = QColor(theme.COMPOUND_COLORS.get(compound.upper(), "#9aa0a6"))
                d = ROW_H - 10.0
                p.setPen(Qt.NoPen)
                p.setBrush(cc)
                p.drawEllipse(QRectF(x, y + 2, d, d))
                p.setPen(_text_on(cc))
                p.setFont(f_small)
                p.drawText(QRectF(x, y + 2, d, d), Qt.AlignCenter, compound[0])
            x += 24
            # relojes: calle de boxes y detenido
            for label, value in (("PIT", lane_s), ("STOP", stop_s)):
                p.setPen(QColor(theme.TEXT_MUTED))
                p.setFont(f_small)
                p.drawText(QRectF(x, y - 1, 34, ROW_H - 4),
                           Qt.AlignVCenter | Qt.AlignRight, label)
                p.setPen(QColor(theme.TEXT))
                p.setFont(f_val)
                p.drawText(QRectF(x + 38, y - 1, 52, ROW_H - 4),
                           Qt.AlignVCenter | Qt.AlignLeft, f"{value:.1f}s")
                x += 96
            if out:
                p.setPen(QColor(theme.TEXT_MUTED))
                p.setFont(f_small)
                p.drawText(QRectF(x, y - 1, 40, ROW_H - 4),
                           Qt.AlignVCenter | Qt.AlignLeft, "OUT")
        p.setOpacity(1.0)
