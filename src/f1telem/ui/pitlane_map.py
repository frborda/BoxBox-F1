"""Pit lane map: la calle de boxes completa desplegada de izquierda
(entrada) a derecha (salida a pista), con sus DOS carriles — el de
circulación arriba y el de detención abajo, con la tira de garajes debajo.
Cada auto en la calle avanza por el carril de circulación según su
posición real medida; al clavarse a 0 km/h baja al carril de detención con
los mecánicos en las 4 ruedas, y al reanudar vuelve arriba hasta el final
de la calle.

Las 4 ruedas van pintadas con el compuesto de ENTRADA; cuando el auto
reanuda tras la detención cambian al compuesto de salida (si el dato ya
llegó del origen). Etiquetas: sigla + tiempo total en la calle; desde la
detención se agrega el tiempo detenido, que queda congelado hasta que el
auto abandona la calle. El movimiento se reproduce con ~un lote de retraso
(EdgeSmoother) para que sea continuo entre ticks.

El filtro 👥 (persistido) permite ocultar autos; todos visibles por
defecto. Diseñado apaisado: pocos píxeles de alto y todo el ancho para el
largo de la calle.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import numpy as np
from PySide6.QtCore import Qt, QRectF
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QVBoxLayout, QWidget

from ..hub import DataHub
from . import theme
from .charts import EdgeSmoother
from .driver_filter import DriverFilterButton
from .pit_strategy import pit_lane_bounds

STOP_KMH = 1.0        # velocidad que cuenta como detenido
DEFAULT_SPAN = 0.045  # largo de la calle como fracción de vuelta (sin dato)

ASPHALT_FAST = QColor("#272b34")
ASPHALT_STOP = QColor("#21252d")
LANE_EDGE = QColor("#3a3f4a")
ENTRY_COLOR = QColor("#d6be3c")   # línea de entrada (límite de velocidad)
EXIT_COLOR = QColor("#2fbf71")    # semáforo/línea de salida
CHEVRON = QColor(255, 255, 255, 34)
GARAGE = QColor("#2c313b")
CREW = QColor("#d6be3c")


@dataclass
class CarGlyph:
    drv: str
    code: str
    color: str          # color de equipo
    frac: float         # 0 = entrada de la calle, 1 = salida
    stopped: bool       # clavado a 0 ahora (carril de detención)
    lane_s: float       # total en la calle (corriendo)
    stop_s: float       # total detenido (congelado tras reanudar)
    tyre: str           # compuesto de las ruedas AHORA ("" desconocido)


class _PitMapCanvas(QWidget):
    def __init__(self, view: "PitlaneMapView"):
        super().__init__()
        self.view = view
        self.setMinimumHeight(100)

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        self.view._paint(p, self.width(), self.height())
        p.end()


class PitlaneMapView(QWidget):
    # geometría vertical (px)
    LBL_H = 13
    LANE_H = 20
    GARAGE_H = 7
    MARGIN_X = 34

    def __init__(self, hub: DataHub, cfg: dict | None = None, parent=None):
        super().__init__(parent)
        self.hub = hub
        self.cars: list[CarGlyph] = []
        # animación: cada auto se reproduce con ~un lote de retraso sobre
        # sus propios datos (mismo esquema que el track map) — sin esto la
        # posición salta de tick en tick
        self.smooth = True
        self._now = time.monotonic  # inyectable para tests deterministas
        self._tsm: dict[str, EdgeSmoother] = {}
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.canvas = _PitMapCanvas(self)
        lay.addWidget(self.canvas)
        # filtro de autos visibles (todos por defecto, persistido),
        # superpuesto arriba a la derecha como en el track map
        self.filter_btn = DriverFilterButton(hub, cfg,
                                             "pitlane_map_hidden_cars", self)
        self.filter_btn.changed.connect(self.refresh)
        self.setMinimumSize(520, 104)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        btn = getattr(self, "filter_btn", None)
        if btn is not None:
            btn.move(self.width() - btn.width() - 6, 2)
            btn.raise_()

    def clear_data(self) -> None:
        self.cars = []
        self._tsm.clear()
        self.canvas.update()

    # ------------------------------------------------------------- datos

    def _tyres(self, drv: str, lap: int) -> tuple[str, str]:
        """(compuesto de entrada, de salida). Para la salida se permite
        mirar el stint de la vuelta SIGUIENTE a la visita en curso: es la
        goma que le están poniendo ahora (conocimiento presente, no
        spoiler); los stints posteriores siguen ocultos."""
        tmap = {k: v for k, v in self.hub.tyres.get(drv, {}).items()
                if k <= lap + 1}
        if not tmap:
            return "", ""
        key = lap if lap in tmap else max(
            (k for k in tmap if k <= lap), default=min(tmap))
        tyre_in = tmap[key][0]
        out_key = min((k for k in tmap if k > lap), default=None)
        return tyre_in, (tmap[out_key][0] if out_key is not None else "")

    def refresh(self) -> None:
        hub = self.hub
        now = hub.latest_t
        L = max(hub.track_length, 1.0)
        bounds = pit_lane_bounds(hub)
        wall = self._now()
        cars: list[CarGlyph] = []
        for drv in hub.pit_lane:
            if drv in self.filter_btn.hidden:
                continue
            visit = hub.last_pit_visit(drv)
            if visit is None:
                continue
            lap, t_in, t_out = visit
            buf = hub.buffers.get(drv)
            if buf is None or not buf.n:
                continue
            t_col = buf.col("t")
            # reloj de reproducción: sigue los datos con ~un lote de
            # retraso, así el movimiento y los relojes son continuos
            if self.smooth:
                sm = self._tsm.get(drv)
                if sm is None:
                    sm = self._tsm[drv] = EdgeSmoother(reset_drop=30.0)
                t_render = sm.update(float(t_col[-1]), wall)
            else:
                t_render = float(t_col[-1])
            t_render = min(t_render, now)
            if t_render < float(t_in):
                continue  # su entrada se reproduce el tick que viene
            if t_out is not None and t_render >= float(t_out):
                self._tsm.pop(drv, None)  # próxima visita, reloj de cero
                continue  # su salida ya se terminó de reproducir
            dist_now = float(np.interp(t_render, t_col,
                                       buf.col("dist_lap")))
            speed = float(np.interp(t_render, t_col, buf.col("speed")))
            if bounds is not None:
                d_in, d_out = bounds
                span = (d_out - d_in) % L or DEFAULT_SPAN * L
            else:
                # sin paradas cerradas aún: anclar en la entrada propia
                if t_col[0] <= t_in <= t_col[-1]:
                    d_in = float(np.interp(t_in, t_col,
                                           buf.col("dist_lap")))
                else:
                    d_in = dist_now
                span = DEFAULT_SPAN * L
            rel = (dist_now - d_in) % L
            if rel > span * 3.0:
                rel = 0.0  # todavía llegando a la entrada
            frac = min(max(rel / span, 0.0), 1.0)

            # relojes y estado, todos al reloj retrasado del propio auto
            lane_s = max(0.0, t_render - float(t_in))
            stop_s = hub.pit_stationary_time(drv, float(t_in), t_render)
            stopped = speed <= STOP_KMH and lane_s > 1.0
            tyre_in, tyre_out = self._tyres(drv, int(lap))
            # ruedas: entrada hasta reanudar tras la detención; después,
            # el compuesto nuevo (si ya se conoce)
            after_stop = (not stopped) and stop_s > 1.0
            tyre = (tyre_out or tyre_in) if after_stop else tyre_in
            info = hub.drivers.get(drv)
            cars.append(CarGlyph(
                drv=drv,
                code=info.code if info else drv,
                color=info.color if info else "#9aa0a6",
                frac=frac,
                stopped=stopped,
                lane_s=lane_s,
                stop_s=stop_s,
                tyre=tyre,
            ))
        cars.sort(key=lambda c: c.frac)
        self.cars = cars
        self.canvas.update()

    # ------------------------------------------------------------ pintado

    def _x_of(self, frac: float, width: int) -> float:
        usable = width - 2 * self.MARGIN_X
        return self.MARGIN_X + frac * usable

    def _draw_car(self, p: QPainter, x: float, cy: float, car: CarGlyph,
                  f_code: QFont) -> None:
        tyre_c = QColor(theme.COMPOUND_COLORS.get(
            car.tyre.upper(), "#9aa0a6")) if car.tyre else QColor("#6a7078")
        # 4 ruedas al color del compuesto (sobresalen del cuerpo)
        p.setPen(Qt.NoPen)
        p.setBrush(tyre_c)
        for dx in (-9.0, 5.0):
            for dy in (-7.0, 3.5):
                p.drawRoundedRect(QRectF(x + dx, cy + dy, 5.0, 3.5), 1, 1)
        # cuerpo con la sigla
        team = QColor(car.color)
        p.setBrush(team)
        p.drawRoundedRect(QRectF(x - 13.0, cy - 5.0, 26.0, 10.0), 3, 3)
        lum = (0.299 * team.redF() + 0.587 * team.greenF()
               + 0.114 * team.blueF())
        p.setPen(QColor("#111318") if lum > 0.55 else QColor("#ffffff"))
        p.setFont(f_code)
        p.drawText(QRectF(x - 13.0, cy - 5.5, 26.0, 11.0),
                   Qt.AlignCenter, car.code)
        if car.stopped:
            # mecánicos trabajando en las 4 ruedas + gato adelante/atrás
            p.setPen(Qt.NoPen)
            p.setBrush(CREW)
            for dx in (-13.5, 12.0):
                for dy in (-8.5, 5.5):
                    p.drawEllipse(QRectF(x + dx, cy + dy, 3.2, 3.2))
            p.setBrush(QColor(theme.TEXT_MUTED))
            p.drawRect(QRectF(x - 17.5, cy - 1.5, 3.0, 3.0))
            p.drawRect(QRectF(x + 14.5, cy - 1.5, 3.0, 3.0))

    def _paint(self, p: QPainter, width: int, height: int) -> None:
        f_small = QFont(self.font()); f_small.setPointSizeF(6.5)
        f_tiny = QFont(self.font()); f_tiny.setPointSizeF(5.0)
        f_code = QFont(self.font()); f_code.setPointSizeF(6.0)
        f_code.setBold(True)
        y_fast = self.LBL_H + 2
        y_stop = y_fast + self.LANE_H + 3
        y_garage = y_stop + self.LANE_H + 1
        y_lbl = y_garage + self.GARAGE_H + 1
        cy_fast = y_fast + self.LANE_H / 2.0
        cy_stop = y_stop + self.LANE_H / 2.0
        x0, x1 = self.MARGIN_X - 16.0, width - self.MARGIN_X + 16.0

        # --- carriles de asfalto con borde ---
        p.setPen(QPen(LANE_EDGE, 1))
        p.setBrush(ASPHALT_FAST)
        p.drawRoundedRect(QRectF(x0, y_fast, x1 - x0, self.LANE_H), 5, 5)
        p.setBrush(ASPHALT_STOP)
        p.drawRoundedRect(QRectF(x0, y_stop, x1 - x0, self.LANE_H), 5, 5)
        # divisoria punteada entre circulación y detención
        p.setPen(QPen(QColor(255, 255, 255, 60), 1, Qt.DashLine))
        y_div = (y_fast + self.LANE_H + y_stop) / 2.0
        p.drawLine(int(x0 + 6), int(y_div), int(x1 - 6), int(y_div))

        # --- chevrones de dirección en el carril rápido ---
        p.setPen(QPen(CHEVRON, 2))
        step = 52
        for cx in range(int(x0 + 40), int(x1 - 46), step):
            p.drawLine(cx, int(cy_fast - 5), cx + 7, int(cy_fast))
            p.drawLine(cx + 7, int(cy_fast), cx, int(cy_fast + 5))

        # --- bahías de detención + tira de garajes ---
        slot_w = 46.0
        n_slots = max(4, int((x1 - x0 - 60) / slot_w))
        gx0 = x0 + 34.0
        p.setFont(f_tiny)
        for i in range(n_slots):
            sx = gx0 + i * slot_w
            if sx + slot_w > x1 - 8:
                break
            # marca de bahía en el carril de detención
            p.setPen(QPen(QColor(255, 255, 255, 26), 1))
            p.drawLine(int(sx), int(y_stop + 3),
                       int(sx), int(y_stop + self.LANE_H - 3))
            # garaje: boca oscura con pared divisoria
            p.setPen(QPen(LANE_EDGE, 1))
            p.setBrush(GARAGE)
            p.drawRect(QRectF(sx + 3, y_garage, slot_w - 6, self.GARAGE_H))
        p.setPen(QColor(theme.TEXT_MUTED))
        p.setFont(f_small)
        p.drawText(QRectF(x0, y_garage - 1, 34, self.GARAGE_H + 3),
                   Qt.AlignVCenter | Qt.AlignLeft, " PITS")

        # --- entrada: línea amarilla + cartel de límite "80" ---
        p.setPen(QPen(ENTRY_COLOR, 2))
        p.drawLine(int(x0 + 3), int(y_fast + 2),
                   int(x0 + 3), int(y_stop + self.LANE_H - 2))
        p.setPen(QColor(theme.TEXT_MUTED))
        p.setFont(f_small)
        p.drawText(QRectF(x0 + 8, y_fast, 40, self.LANE_H),
                   Qt.AlignVCenter | Qt.AlignLeft, "IN")
        sign = QRectF(x0 + 22.0, cy_fast - 5.5, 11.0, 11.0)
        p.setPen(QPen(QColor("#e8eaed"), 1.2))
        p.setBrush(QColor("#2a2e37"))
        p.drawEllipse(sign)
        p.setFont(f_tiny)
        p.setPen(QColor("#e8eaed"))
        p.drawText(sign, Qt.AlignCenter, "80")

        # --- salida: damero + línea verde ---
        cell = 3.0
        for r in range(int(self.LANE_H / cell)):
            for c in range(2):
                if (r + c) % 2 == 0:
                    p.fillRect(QRectF(x1 - 10 + c * cell,
                                      y_fast + 1 + r * cell, cell, cell),
                               QColor(255, 255, 255, 90))
        p.setPen(QPen(EXIT_COLOR, 2))
        p.drawLine(int(x1 - 3), int(y_fast + 2),
                   int(x1 - 3), int(y_fast + self.LANE_H - 2))
        p.setPen(QColor(EXIT_COLOR))
        p.setFont(f_small)
        p.drawText(QRectF(x1 - 62, y_fast - self.LBL_H, 60, self.LBL_H),
                   Qt.AlignVCenter | Qt.AlignRight, "TRACK →")

        if not self.cars:
            p.setPen(QColor(theme.TEXT_MUTED))
            p.setFont(f_small)
            p.drawText(QRectF(0, y_fast, width, self.LANE_H),
                       Qt.AlignCenter, "— pit lane empty —")
            return
        labels: dict[bool, list[tuple[float, str]]] = {False: [], True: []}
        for car in self.cars:
            x = self._x_of(car.frac, width)
            cy = cy_stop if car.stopped else cy_fast
            self._draw_car(p, x, cy, car, f_code)
            # etiqueta: sigla + tiempo en calle; con detención, también el
            # tiempo detenido (congelado hasta salir de la calle)
            text = f"{car.code} {car.lane_s:.0f}s"
            if car.stopped or car.stop_s > 1.0:
                text += f" ▪{car.stop_s:.1f}s"
            labels[car.stopped].append((x, text))
        # filas de texto (arriba: circulando; abajo: detenidos) con
        # de-conflicto: cada etiqueta empieza después de la anterior
        p.setFont(f_small)
        for stopped, items in labels.items():
            items.sort()
            y = y_lbl if stopped else 0
            p.setPen(QColor("#d6be3c") if stopped else QColor(theme.TEXT))
            last_end = 2.0
            for x, text in items:
                tw = p.fontMetrics().horizontalAdvance(text) + 4
                tx = min(max(x - tw / 2.0, last_end), width - tw - 2.0)
                tx = max(tx, last_end)
                p.drawText(QRectF(tx, y, tw, self.LBL_H),
                           Qt.AlignCenter, text)
                last_end = tx + tw + 4.0
