"""Rueda de vuelta: vista circular de la posición de cada auto EN LA VUELTA.

El norte es la meta (inicio/fin de vuelta), el sur la mitad; el ángulo de
cada auto es su fracción de vuelta, girando en sentido horario. El anillo
lleva las referencias de los sectores (S1/S2/S3, límites oficiales cuando
existen) y las curvas del circuito. Al elegir un piloto en "Pit sim" se
dibuja un fantasma en el punto donde caería si entrara a boxes ahora,
usando la Ventana de Box calculada/fijada en el panel Pit strategy.
"""
from __future__ import annotations

import math

import numpy as np
from PySide6.QtCore import Qt, QPointF, QRectF
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from ..hub import DataHub
from ..timing import TimingAnalyzer
from . import theme
from .charts import EdgeSmoother
from .pit_strategy import current_gaps, pit_lane_bounds, project_rejoin

RING_COLOR = "#3a3f4a"      # mismo gris que el trazado del track map
YELLOW_COLOR = "#ffd12e"    # amarillas por sector, como en el mapa
PIT_COLOR = "#4a7fd4"       # tramo de la calle de boxes (azul pit)


def _text_on(bg: QColor) -> QColor:
    lum = 0.299 * bg.redF() + 0.587 * bg.greenF() + 0.114 * bg.blueF()
    return QColor("#111318") if lum > 0.55 else QColor("#ffffff")


class _WheelCanvas(QWidget):
    def __init__(self, view: "LapWheelView"):
        super().__init__()
        self.view = view
        self.setMinimumSize(260, 260)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        self.view._paint(painter, self.width(), self.height())
        painter.end()


class LapWheelView(QWidget):
    def __init__(self, hub: DataHub, cfg: dict | None = None, parent=None):
        super().__init__(parent)
        self.hub = hub
        self.cfg = cfg if cfg is not None else {}
        self.analyzer = TimingAnalyzer(hub)
        self._sm: dict[str, EdgeSmoother] = {}
        self._ghost_sm: EdgeSmoother | None = None
        self._ghost_drv: str | None = None
        # estado del último refresco (también para tests)
        self._dots: dict[str, tuple[float, str, str]] = {}  # {drv: (ángulo, code, color)}
        self._ghost: tuple[str, float] | None = None        # (drv, ángulo)
        # anillo interno: [(detrás, adelante, segundos, áng_detrás, barrido)]
        self._intervals: list[tuple[str, str, float, float, float]] = []
        # tramo de boxes (áng_entrada, barrido); cache por visitas cerradas
        self._pit_arc: tuple[float, float] | None = None
        self._pit_arc_n = -1

        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 2, 4, 4)
        lay.setSpacing(2)
        row = QHBoxLayout()
        row.addWidget(QLabel("Pit sim:"))
        self.sim_combo = QComboBox()
        self.sim_combo.addItem("None", None)
        row.addWidget(self.sim_combo)
        self.result_label = QLabel("")
        self.result_label.setStyleSheet(f"color: {theme.TEXT_MUTED};")
        row.addWidget(self.result_label, stretch=1)
        lay.addLayout(row)
        self.canvas = _WheelCanvas(self)
        lay.addWidget(self.canvas, stretch=1)

        hub.driversChanged.connect(self._rebuild_combo)

    def _rebuild_combo(self) -> None:
        current = self.sim_combo.currentData()
        self.sim_combo.blockSignals(True)
        self.sim_combo.clear()
        self.sim_combo.addItem("None", None)
        for info in sorted(self.hub.drivers.values(),
                           key=lambda d: d.label.upper()):
            self.sim_combo.addItem(info.label, info.number)
        if current is not None:
            idx = self.sim_combo.findData(current)
            if idx >= 0:
                self.sim_combo.setCurrentIndex(idx)
        self.sim_combo.blockSignals(False)

    def clear_data(self) -> None:
        self.analyzer.clear()
        self._sm.clear()
        self._ghost_sm = None
        self._ghost_drv = None
        self._dots = {}
        self._ghost = None
        self._intervals = []
        self._pit_arc = None
        self._pit_arc_n = -1
        self.result_label.setText("")
        self.canvas.update()

    def pit_window_value(self) -> float:
        return float(self.cfg.get("strategy", {}).get("pit_window", 20.0))

    # ------------------------------------------------------------ refresco

    def refresh(self) -> None:
        import time as _time

        hub = self.hub
        L = max(hub.track_length, 1.0)
        now = _time.monotonic()
        dots: dict[str, tuple[float, str, str]] = {}
        pts: dict[str, tuple] = {}
        for drv, buf in hub.buffers.items():
            pt = self.analyzer.position_time(drv)
            if pt is None or len(pt[0]) < 2:
                continue
            pts[drv] = pt
            sm = self._sm.get(drv)
            if sm is None:
                sm = self._sm[drv] = EdgeSmoother(reset_drop=2.0 * L)
            pos = sm.update(float(pt[0][-1]), now)
            info = hub.drivers.get(drv)
            dots[drv] = ((pos % L) / L * 360.0,
                         info.code if info else drv,
                         info.color if info else "#9aa0a6")
        self._dots = dots

        # anillo interno: intervalos entre autos consecutivos (en orden de
        # carrera); el arco une al de atrás con el de adelante y gira con
        # los ángulos suavizados, el valor se recalcula con cada refresco
        ordered = sorted(pts, key=lambda d: float(pts[d][0][-1]), reverse=True)
        intervals: list[tuple[str, str, float, float, float]] = []
        for i in range(len(ordered) - 1):
            ahead, behind = ordered[i], ordered[i + 1]
            pos_a, t_a = pts[ahead]
            pos_b, t_b = pts[behind]
            p_b = float(pos_b[-1])
            if not (float(pos_a[0]) <= p_b <= float(pos_a[-1])):
                continue  # el de adelante aún no tiene historia en ese punto
            secs = float(t_b[-1]) - float(np.interp(p_b, pos_a, t_a))
            if secs < 0.0 or behind not in dots or ahead not in dots:
                continue
            b_ang = dots[behind][0]
            span = (dots[ahead][0] - b_ang) % 360.0
            intervals.append((behind, ahead, secs, b_ang, span))
        self._intervals = intervals

        # tramo de la calle de boxes (recalcular solo con visitas nuevas)
        n_closed = sum(1 for visits in hub.pit_lane.values()
                       for v in visits if v[2] is not None)
        if n_closed != self._pit_arc_n:
            self._pit_arc_n = n_closed
            bounds = pit_lane_bounds(hub)
            if bounds is None:
                self._pit_arc = None
            else:
                entry, exit_ = bounds
                a_in = (entry % L) / L * 360.0
                span = ((exit_ - entry) % L) / L * 360.0
                self._pit_arc = (a_in, span if span > 0.5 else 0.5)

        self._ghost = None
        sim = self.sim_combo.currentData()
        if sim != self._ghost_drv:
            self._ghost_drv = sim
            self._ghost_sm = None  # piloto nuevo: re-enganchar sin arrastre
        if sim and sim in pts:
            window = self.pit_window_value()
            pos, t = pts[sim]
            t_ghost = float(t[-1]) - window
            if t_ghost > float(t[0]):
                raw = float(np.interp(t_ghost, t, pos))
                # el fantasma avanza con el mismo motor de reproducción que
                # los autos: fluido aunque el feed llegue en ráfagas
                if self._ghost_sm is None:
                    self._ghost_sm = EdgeSmoother(reset_drop=2.0 * L)
                ghost_pos = self._ghost_sm.update(raw, now)
                self._ghost = (sim, (ghost_pos % L) / L * 360.0)
            ordered, gaps = current_gaps(hub, self.analyzer)
            proj = project_rejoin(gaps, sim, window)
            info = hub.drivers.get(sim)
            code = info.code if info else sim
            if proj is None:
                self.result_label.setText(f"{code}: no projection (lapped)")
            else:
                new_pos, (behind_drv, margin), _b = proj
                binfo = hub.drivers.get(behind_drv or "")
                behind_txt = ("" if behind_drv is None else
                              f" · {margin:.1f}s behind "
                              f"{binfo.code if binfo else behind_drv}")
                self.result_label.setText(
                    f"{code} pits now ({window:.1f}s): → P{new_pos}{behind_txt}")
        elif not sim:
            self.result_label.setText("")
        self.canvas.update()

    # ------------------------------------------------------------- pintado

    @staticmethod
    def _xy(cx: float, cy: float, radius: float, angle: float) -> QPointF:
        rad = math.radians(angle)
        return QPointF(cx + radius * math.sin(rad), cy - radius * math.cos(rad))

    def _sector_bounds_deg(self) -> tuple[float, float]:
        L = max(self.hub.track_length, 1.0)
        bounds = self.hub.sector_bounds
        if bounds is not None and 0.0 < bounds[0] < bounds[1] < L:
            return bounds[0] / L * 360.0, bounds[1] / L * 360.0
        return 120.0, 240.0  # tercios hasta derivar los límites oficiales

    def _active_yellows_deg(self) -> list[tuple[float, float]]:
        """Amarillas por sector vigentes, como (ángulo0, ángulo1)."""
        L = max(self.hub.track_length, 1.0)
        t = self.hub.latest_t
        return [((d0 % L) / L * 360.0, (d1 % L) / L * 360.0)
                for t0, t1, d0, d1 in self.hub.sector_yellows
                if t0 <= t <= t1]

    def _paint(self, p: QPainter, width: int, height: int) -> None:
        cx, cy = width / 2.0, height / 2.0 + 4
        radius = min(width, height) / 2.0 - 40
        if radius < 60:
            return
        L = max(self.hub.track_length, 1.0)
        f_corner = QFont(self.font()); f_corner.setPointSizeF(7.5)
        f_small = QFont(self.font()); f_small.setPointSizeF(7.5)
        f_sector = QFont(self.font()); f_sector.setPointSizeF(8.0)
        f_sector.setBold(True)
        f_code = QFont(self.font()); f_code.setPointSizeF(9.0); f_code.setBold(True)

        # anillo gris único (mismo tono que el trazado del track map)
        rect = QRectF(cx - radius, cy - radius, 2 * radius, 2 * radius)
        p.setPen(QPen(QColor(RING_COLOR), 5))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(rect)

        # amarillas por sector vigentes: arco amarillo sobre el anillo
        pen_y = QPen(QColor(YELLOW_COLOR), 6)
        p.setPen(pen_y)
        for a0, a1 in self._active_yellows_deg():
            span = (a1 - a0) % 360.0
            if span <= 0.0:
                span = 360.0
            p.drawArc(rect, int((90.0 - a0 - span) * 16), int(span * 16))

        # delimitación de sectores: rayas radiales bien marcadas + etiquetas
        b1, b2 = self._sector_bounds_deg()
        p.setPen(QPen(QColor(theme.TEXT), 2.5))
        for angle in (b1, b2):
            p.drawLine(self._xy(cx, cy, radius - 12, angle),
                       self._xy(cx, cy, radius + 12, angle))
        p.setFont(f_sector)
        p.setPen(QColor(theme.TEXT_MUTED))
        for (a0, a1), label in zip(((0.0, b1), (b1, b2), (b2, 360.0)),
                                   ("S1", "S2", "S3")):
            mid = self._xy(cx, cy, radius - 22, (a0 + a1) / 2.0)
            p.drawText(QRectF(mid.x() - 14, mid.y() - 8, 28, 16),
                       Qt.AlignCenter, label)

        # tramo de la calle de boxes: arco punteado apenas por dentro
        if self._pit_arc is not None:
            a_in, span = self._pit_arc
            r_pit = radius - 10
            rect_pit = QRectF(cx - r_pit, cy - r_pit, 2 * r_pit, 2 * r_pit)
            pen_pit = QPen(QColor(PIT_COLOR), 3)
            pen_pit.setStyle(Qt.DashLine)
            p.setPen(pen_pit)
            p.drawArc(rect_pit, int((90.0 - a_in - span) * 16), int(span * 16))
            mid_pit = self._xy(cx, cy, r_pit - 11, a_in + span / 2.0)
            p.setFont(f_small)
            p.setPen(QColor(PIT_COLOR))
            p.drawText(QRectF(mid_pit.x() - 15, mid_pit.y() - 7, 30, 14),
                       Qt.AlignCenter, "PIT")

        # meta (norte): tick fuerte + S/F
        p.setPen(QPen(QColor(theme.TEXT), 3))
        p.drawLine(self._xy(cx, cy, radius - 10, 0.0),
                   self._xy(cx, cy, radius + 10, 0.0))
        p.setPen(QColor(theme.TEXT))
        p.setFont(f_small)
        p.drawText(QRectF(cx - 16, cy - radius - 26, 32, 14),
                   Qt.AlignCenter, "S/F")

        # curvas del circuito: tick corto + etiqueta por fuera
        p.setFont(f_corner)
        for label, dist, _x, _y in self.hub.corners:
            angle = (float(dist) % L) / L * 360.0
            p.setPen(QPen(QColor(theme.TEXT_MUTED), 1))
            p.drawLine(self._xy(cx, cy, radius - 5, angle),
                       self._xy(cx, cy, radius + 5, angle))
            tip = self._xy(cx, cy, radius + 15, angle)
            p.drawText(QRectF(tip.x() - 15, tip.y() - 7, 30, 14),
                       Qt.AlignCenter, label)

        # anillo interno: arcos entre autos consecutivos con su diferencia
        # en segundos (el arco cubre la pista que los separa y gira con
        # ellos; el valor se ajusta en tiempo real)
        r_gap = radius - 44
        if r_gap > 40 and self._intervals:
            rect_gap = QRectF(cx - r_gap, cy - r_gap, 2 * r_gap, 2 * r_gap)
            f_gap = QFont(self.font())
            f_gap.setPointSizeF(7.5)
            f_gap.setBold(True)
            for i, (behind, _ahead, secs, b_ang, span) in enumerate(
                    self._intervals):
                team = QColor(self._dots[behind][2])
                arc_color = QColor(team)
                arc_color.setAlpha(170)
                pen_gap = QPen(arc_color, 3)
                pen_gap.setCapStyle(Qt.FlatCap)
                p.setPen(pen_gap)
                draw_span = max(span - 4.0, 0.5)  # aire junto a cada auto
                p.drawArc(rect_gap,
                          int((90.0 - b_ang - 2.0 - draw_span) * 16),
                          int(draw_span * 16))
                # etiqueta al medio del arco, radios alternados para que las
                # cifras no se pisen con autos amontonados
                mid = b_ang + span / 2.0
                r_lab = r_gap - 13 - (i % 2) * 15
                lab = self._xy(cx, cy, r_lab, mid)
                p.setFont(f_gap)
                p.setPen(team)
                p.drawText(QRectF(lab.x() - 22, lab.y() - 8, 44, 16),
                           Qt.AlignCenter, f"{secs:.1f}")

        # fantasma de la parada (debajo de los autos)
        if self._ghost is not None:
            _drv, angle = self._ghost
            pt = self._xy(cx, cy, radius, angle)
            pen = QPen(QColor(theme.ACCENT))
            pen.setWidthF(2.0)
            pen.setStyle(Qt.DashLine)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(pt, 8.0, 8.0)
            p.drawLine(self._xy(cx, cy, radius - 13, angle),
                       self._xy(cx, cy, radius + 13, angle))
            tip = self._xy(cx, cy, radius - 26, angle)
            p.setFont(f_small)
            p.drawText(QRectF(tip.x() - 15, tip.y() - 7, 30, 14),
                       Qt.AlignCenter, "PIT")

        # autos: punto sobre el anillo + sigla por fuera
        sim = self.sim_combo.currentData()
        for drv, (angle, code, color) in self._dots.items():
            team = QColor(color)
            pt = self._xy(cx, cy, radius, angle)
            p.setPen(QPen(QColor(theme.SURFACE), 1.5))
            p.setBrush(team)
            r_dot = 8.5 if drv == sim else 6.0
            p.drawEllipse(pt, r_dot, r_dot)
            label_pt = self._xy(cx, cy, radius + 30, angle)
            p.setFont(f_code)
            p.setPen(team)
            p.drawText(QRectF(label_pt.x() - 20, label_pt.y() - 8, 40, 16),
                       Qt.AlignCenter, code)
