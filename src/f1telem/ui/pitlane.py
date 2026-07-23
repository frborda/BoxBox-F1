"""Panel Pit lane: historial vivo de la calle de boxes. Los que están
ADENTRO ahora van arriba a opacidad plena; los que ya salieron quedan
atenuados con un indicador de hace cuántas vueltas se fueron, y NO se
quitan — la fila de un piloto solo se renueva cuando vuelve a entrar. Una
línea separa ambos grupos; dentro de cada uno, el ingreso más reciente
primero.

Cada fila resalta los DOS compuestos (con el que entró → el que puso al
salir; el de salida puede llegar con retraso del origen y se completa
solo). El tiempo detenido usa la telemetría, pero si OpenF1 publica la
detención oficial de esa parada la reemplaza (marcada con ✓). Un auto
clavado a velocidad 0 por más de 30 s (abandono, reparación, o en el box
en práctica/quali) se resalta en rojo."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from PySide6.QtCore import Qt, QRectF
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QHBoxLayout, QScrollArea, QVBoxLayout, QWidget

from ..hub import DataHub
from . import theme
from .driver_filter import DriverFilterButton

ROW_H = 26
STALL_S = 30.0  # detenido a velocidad 0 este tiempo = resaltado en rojo
STALL_BG = QColor(255, 80, 80, 30)
STALL_FG = QColor("#ff6b5e")


@dataclass
class PitRow:
    drv: str
    code: str
    color: str
    tyre_in: str            # compuesto al entrar ("" desconocido)
    tyre_out: str           # compuesto al salir ("" aún sin dato)
    lane_s: float           # s en la calle (congelado al salir)
    stop_s: float           # s detenido (oficial si stop_official)
    stop_official: bool     # True: corregido con el dato oficial de OpenF1
    inside: bool            # está en la calle ahora
    stalled: bool           # ≥ STALL_S clavado a velocidad 0 (adentro)
    laps_ago: int | None    # vueltas desde que salió (None sin dato)
    t_in: float


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
        self.rows: list[PitRow] = []
        self.sep_index = 0  # filas "adentro" (la línea separadora va después)
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
        self.sep_index = 0
        self.canvas.update()

    def _lap_at(self, drv: str, t: float) -> int | None:
        """Vuelta del auto en el instante t (None sin muestras)."""
        buf = self.hub.buffers.get(drv)
        if buf is None or not buf.n:
            return None
        i = int(np.searchsorted(buf.col("t"), t, side="right")) - 1
        return int(buf.col("lap")[max(i, 0)])

    def refresh(self) -> None:
        hub = self.hub
        now = hub.latest_t
        rows: list[PitRow] = []
        for drv in hub.pit_lane:
            if drv in self.filter_btn.hidden:
                continue
            # última visita ya iniciada a latest_t (el replay publica la
            # historia por adelantado: las futuras no cuentan). La fila
            # persiste hasta que el piloto vuelva a entrar: la visita nueva
            # simplemente la reemplaza
            visit = hub.last_pit_visit(drv)
            if visit is None:
                continue
            lap, t_in, t_out = visit
            lap = int(lap)
            inside = hub.pit_visit_open(visit)
            end = now if inside else float(t_out)

            # compuestos: con el que entró y el del stint que ARRANCA
            # después de la vuelta de entrada (llega con retraso del
            # origen: la fila se completa sola en un refresh posterior).
            # tyres_until_now respeta el timeline (sin spoilers en replay)
            tmap = hub.tyres_until_now(drv)
            tyre_in, tyre_out = "", ""
            if tmap:
                key = lap if lap in tmap else max(
                    (k for k in tmap if k <= lap), default=min(tmap))
                tyre_in = tmap[key][0]
                out_key = min((k for k in tmap if k > lap), default=None)
                if out_key is not None:
                    tyre_out = tmap[out_key][0]

            stop_s = hub.pit_stationary_time(drv, float(t_in), end)
            stop_official = False
            if not inside:
                # dato oficial de la detención (OpenF1): corrige al medido
                official = hub.official_stop(drv, lap)
                if official is not None and official[1] == official[1]:
                    stop_s = float(official[1])
                    stop_official = True

            stalled = False
            if inside:
                stopped = now - max(hub.last_move_t.get(drv, 0.0),
                                    float(t_in))
                stalled = stopped >= STALL_S

            laps_ago: int | None = None
            if not inside:
                buf = hub.buffers.get(drv)
                exit_lap = self._lap_at(drv, float(t_out))
                if buf is not None and buf.n and exit_lap is not None:
                    laps_ago = max(0, buf.current_lap() - exit_lap)

            info = hub.drivers.get(drv)
            rows.append(PitRow(
                drv=drv,
                code=info.code if info else drv,
                color=info.color if info else "#9aa0a6",
                tyre_in=tyre_in,
                tyre_out=tyre_out,
                lane_s=max(0.0, end - float(t_in)),
                stop_s=stop_s,
                stop_official=stop_official,
                inside=inside,
                stalled=stalled,
                laps_ago=laps_ago,
                t_in=float(t_in),
            ))
        # adentro arriba; en cada grupo, el ingreso más reciente primero
        rows.sort(key=lambda r: (not r.inside, -r.t_in))
        self.rows = rows
        self.sep_index = sum(1 for r in rows if r.inside)
        self.canvas.setMinimumHeight(max(len(rows) * ROW_H + 10, 40))
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
        for i, row in enumerate(self.rows):
            y = i * ROW_H + 4
            p.setOpacity(1.0 if row.inside else 0.45)
            if i % 2:
                p.fillRect(0, y - 2, width, ROW_H, QColor(theme.SURFACE_ALT))
            if row.stalled:
                # clavado a velocidad 0: abandono / reparación / en el box
                p.fillRect(0, y - 2, width, ROW_H, STALL_BG)
            team = QColor(row.color)
            p.setPen(Qt.NoPen)
            p.setBrush(team)
            p.drawRoundedRect(QRectF(4, y + 1, 46, ROW_H - 6), 3, 3)
            p.setPen(_text_on(team))
            p.setFont(f_code)
            p.drawText(QRectF(4, y + 1, 46, ROW_H - 6), Qt.AlignCenter, row.code)

            # compuestos resaltados: entró → salió (el de salida puede
            # tardar en llegar: mientras tanto un aro con "?")
            d = ROW_H - 10.0
            x = 56
            for tyre, known in ((row.tyre_in, bool(row.tyre_in)),
                                (row.tyre_out, bool(row.tyre_out))):
                if known:
                    cc = QColor(theme.COMPOUND_COLORS.get(
                        tyre.upper(), "#9aa0a6"))
                    p.setPen(Qt.NoPen)
                    p.setBrush(cc)
                    p.drawEllipse(QRectF(x, y + 2, d, d))
                    p.setPen(_text_on(cc))
                    p.setFont(f_small)
                    p.drawText(QRectF(x, y + 2, d, d), Qt.AlignCenter, tyre[0])
                else:
                    p.setPen(QPen(QColor(theme.TEXT_MUTED), 1))
                    p.setBrush(Qt.NoBrush)
                    p.drawEllipse(QRectF(x, y + 2, d, d))
                    p.setPen(QColor(theme.TEXT_MUTED))
                    p.setFont(f_small)
                    p.drawText(QRectF(x, y + 2, d, d), Qt.AlignCenter, "?")
                if x == 56:  # flecha entre ambos
                    p.setPen(QColor(theme.TEXT_MUTED))
                    p.setFont(f_small)
                    p.drawText(QRectF(x + d, y + 2, 14, d),
                               Qt.AlignCenter, "→")
                    x += d + 14
            x = 56 + d + 14 + d + 8

            # relojes: calle de boxes y detenido (oficial de OpenF1 si llegó)
            for label, value, official in (
                    ("PIT", row.lane_s, False),
                    ("STOP✓" if row.stop_official else "STOP",
                     row.stop_s, row.stop_official)):
                p.setPen(QColor(theme.ACCENT) if official
                         else QColor(theme.TEXT_MUTED))
                p.setFont(f_small)
                p.drawText(QRectF(x, y - 1, 36, ROW_H - 4),
                           Qt.AlignVCenter | Qt.AlignRight, label)
                stall_val = row.stalled and label.startswith("STOP")
                p.setPen(STALL_FG if stall_val else QColor(theme.TEXT))
                p.setFont(f_val)
                p.drawText(QRectF(x + 40, y - 1, 52, ROW_H - 4),
                           Qt.AlignVCenter | Qt.AlignLeft, f"{value:.1f}s")
                x += 98
            # estado a la derecha: clavado (adentro) o salida hace N vueltas
            p.setFont(f_small)
            if row.stalled:
                p.setPen(STALL_FG)
                p.drawText(QRectF(x, y - 1, 80, ROW_H - 4),
                           Qt.AlignVCenter | Qt.AlignLeft, "⚠ STOPPED 30s+")
            elif not row.inside:
                p.setPen(QColor(theme.TEXT_MUTED))
                tag = "OUT" if row.laps_ago is None else f"OUT · {row.laps_ago}L"
                p.drawText(QRectF(x, y - 1, 80, ROW_H - 4),
                           Qt.AlignVCenter | Qt.AlignLeft, tag)
        p.setOpacity(1.0)
        # línea separadora entre los que están adentro y los que salieron
        if 0 < self.sep_index < len(self.rows):
            y_sep = self.sep_index * ROW_H + 2.5
            p.setPen(QPen(QColor(theme.TEXT_MUTED), 1, Qt.DashLine))
            p.drawLine(4, int(y_sep), width - 4, int(y_sep))
