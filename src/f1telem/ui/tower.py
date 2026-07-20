"""Torre de tiempos estilo broadcast: cada auto en una fila de dos líneas
con caja de posición y sigla en color de equipo, neumático, velocidad,
píldoras LAST/BEST (violeta = mejor de la sesión, verde = mejor personal),
INT/LDR y los microsectores como rayitas de colores con los tiempos de
sector debajo (en vivo usa los segmentos oficiales del feed; si no, se
calculan contra el mejor personal y de la sesión).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
from PySide6.QtCore import QEvent, Qt, QRectF
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QScrollArea, QToolButton, QToolTip, QVBoxLayout,
    QWidget,
)

from .. import config
from ..hub import DataHub
from ..timing import N_MICRO, SECTOR_STEP, TimingAnalyzer
from . import theme
from .timing_view import fmt_laptime, fmt_secs

# tipo de tiempo/segmento: 0 = sin dato, 1 = completado, 2 = mejor personal,
# 3 = mejor de la sesión, 4 = pit lane
_KIND_COLORS = {
    0: QColor(255, 255, 255, 26),
    1: QColor(214, 190, 60),
    2: QColor(46, 190, 108),
    3: QColor(200, 82, 255),
    4: QColor(74, 127, 212),
}
_OFFICIAL_KIND = {2048: 1, 2049: 2, 2051: 3, 2064: 4}
_CLEAR_BADGE = ("TRACK CLEAR", "#21a05a")

ROW_H = 38


@dataclass
class TowerRow:
    drv: str
    code: str
    color: str
    pos: int
    ready: bool                # posición de pista confiable (ancla o proyección)
    delta: int | None          # posiciones ganadas (+) desde el inicio
    speed: float
    gap_txt: str               # al líder
    int_txt: str               # al de adelante
    last: float
    last_kind: int
    best: float
    best_kind: int
    pits: int
    sectors: list = field(default_factory=list)  # [(tiempo, kind, dim)] x3
    segs: list = field(default_factory=list)     # [ [kind, ...] ] x3
    avg5: float = float("nan")
    avg10: float = float("nan")
    catch: float | None = None  # vueltas para alcanzar al de adelante
    tyre: str = ""              # compuesto actual (SOFT/MEDIUM/...)
    tyre_age: int = 0           # vueltas del juego actual
    pit_lap: int = 0            # vuelta de la última pasada por boxes
    pit_lane_s: float = float("nan")  # segundos en la calle en esa pasada
    pit_stop_s: float = float("nan")  # segundos detenido en esa pasada
    pit_open: bool = False      # está en la calle ahora mismo


def _text_on(bg: QColor) -> QColor:
    lum = 0.299 * bg.redF() + 0.587 * bg.greenF() + 0.114 * bg.blueF()
    return QColor("#111318") if lum > 0.55 else QColor("#ffffff")


class _TowerCanvas(QWidget):
    """Superficie de pintado de las filas (dentro del scroll)."""

    def __init__(self, tower: "TimingTower"):
        super().__init__()
        self.tower = tower

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        self.tower._paint_rows(painter, self.width())
        painter.end()

    def event(self, ev) -> bool:
        if ev.type() == QEvent.ToolTip:
            row = int(ev.pos().y() // self.tower.row_h)
            rows = self.tower.rows
            if 0 <= row < len(rows):
                QToolTip.showText(ev.globalPos(), self.tower._row_tooltip(rows[row]), self)
            else:
                QToolTip.hideText()
            return True
        return super().event(ev)


class TimingTower(QWidget):
    def __init__(self, hub: DataHub, cfg: dict | None = None, parent=None):
        super().__init__(parent)
        self.hub = hub
        self.cfg = cfg
        self.analyzer = TimingAnalyzer(hub)
        self.scale = float((cfg or {}).get("ui", {}).get("tower_scale", 1.0))
        self.scale = min(max(self.scale, 0.7), 1.8)
        self.rows: list[TowerRow] = []
        self._order0: dict[str, int] = {}
        self._folded: dict[str, int] = {}
        self._best_micro: dict[str, np.ndarray] = {}
        self._sess_micro = np.full(N_MICRO, np.inf)
        self._best_sec: dict[str, np.ndarray] = {}
        self._sess_sec = np.full(3, np.inf)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        head = QHBoxLayout()
        head.setContentsMargins(2, 0, 2, 0)
        self.lap_label = QLabel("")
        self.lap_label.setStyleSheet(f"color: {theme.TEXT}; font-weight: bold;")
        head.addWidget(self.lap_label)
        self.flag_label = QLabel("")
        self.flag_label.setVisible(False)
        head.addWidget(self.flag_label)
        head.addStretch(1)
        self.wx_label = QLabel("")
        self.wx_label.setStyleSheet(f"color: {theme.TEXT_MUTED};")
        head.addWidget(self.wx_label)
        for text, step in (("A−", -0.1), ("A+", 0.1)):
            btn = QToolButton()
            btn.setText(text)
            btn.setAutoRaise(True)
            btn.setFixedSize(22, 16)
            btn.setToolTip("Tower font size")
            btn.clicked.connect(lambda _=False, d=step: self._change_scale(d))
            head.addWidget(btn)
        lay.addLayout(head)

        self.canvas = _TowerCanvas(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setWidget(self.canvas)
        lay.addWidget(scroll)

    # ------------------------------------------------------------- estado

    @property
    def row_h(self) -> int:
        return int(ROW_H * self.scale)

    def _change_scale(self, delta: float) -> None:
        self.scale = min(max(self.scale + delta, 0.7), 1.8)
        if self.cfg is not None:
            self.cfg.setdefault("ui", {})["tower_scale"] = round(self.scale, 2)
            config.save_config(self.cfg)
        self.canvas.setMinimumHeight(len(self.rows) * self.row_h)
        self.canvas.update()

    def clear_data(self) -> None:
        self.analyzer.clear()
        self.rows = []
        self._order0.clear()
        self._folded.clear()
        self._best_micro.clear()
        self._sess_micro = np.full(N_MICRO, np.inf)
        self._best_sec.clear()
        self._sess_sec = np.full(3, np.inf)
        self.lap_label.setText("")
        self.flag_label.setVisible(False)
        self.wx_label.setText("")
        self.canvas.update()

    def _update_header(self, t_now: float, leader_lap: int) -> None:
        self.lap_label.setText(f"LAP {leader_lap}" if leader_lap else "")
        badge = _CLEAR_BADGE
        for t0, t1, code in self.hub.track_status:
            if t0 <= t_now <= t1 and code in theme.TRACK_STATUS:
                badge = theme.TRACK_STATUS[code]
                break
        text, color = badge
        self.flag_label.setText(text)
        self.flag_label.setStyleSheet(
            f"background: {color}; color: #111318; font-weight: bold;"
            "padding: 0px 6px; border-radius: 3px;"
        )
        self.flag_label.setVisible(True)
        weather = self.hub.weather_at(t_now)
        if weather is not None:
            _t, air, track, _wind, rain = weather
            self.wx_label.setText(
                f"Air {air:.0f}° · Trk {track:.0f}°" + (" · RAIN" if rain else "")
            )

    def _fold_bests(self, drv: str) -> None:
        """Acumula los mejores µsectores/sectores (personal y de la sesión)
        de las vueltas cerradas nuevas."""
        buf = self.hub.buffers.get(drv)
        if buf is None:
            return
        an = self.analyzer
        done = self._folded.get(drv, 0)
        best_m = self._best_micro.setdefault(drv, np.full(N_MICRO, np.inf))
        best_s = self._best_sec.setdefault(drv, np.full(3, np.inf))
        for lap in buf.completed_laps():
            if lap <= done:
                continue
            micro = an.micro_times(drv, lap)
            if micro is not None:
                ok = np.isfinite(micro)
                np.minimum(best_m, np.where(ok, micro, np.inf), out=best_m)
                np.minimum(self._sess_micro, np.where(ok, micro, np.inf),
                           out=self._sess_micro)
            sec = np.array(an.sector_times(drv, lap))
            ok = np.isfinite(sec)
            np.minimum(best_s, np.where(ok, sec, np.inf), out=best_s)
            np.minimum(self._sess_sec, np.where(ok, sec, np.inf), out=self._sess_sec)
            self._folded[drv] = lap

    def _kind_of(self, value: float, personal: float, session: float) -> int:
        if not math.isfinite(value):
            return 0
        if value <= session + 1e-9:
            return 3
        if value <= personal + 1e-9:
            return 2
        return 1

    def _segs_for(self, drv: str, cur_lap: int) -> list:
        """Rayitas: segmentos oficiales del feed si existen; si no, los 24
        µsectores calculados de la vuelta en curso."""
        counts = self.hub.segment_counts
        if counts:
            state = self.hub.segments.get(drv, {})
            return [
                [_OFFICIAL_KIND.get(int(state.get((sec, i), 0)), 0)
                 for i in range(counts[sec])]
                for sec in sorted(counts)
            ]
        data = self.analyzer.latest_micro_times(drv)
        if data is None:
            return [[0] * SECTOR_STEP for _ in range(3)]
        times, laps = data
        best = self._best_micro.get(drv)
        segs = []
        for k in range(3):
            group = []
            for i in range(k * SECTOR_STEP, (k + 1) * SECTOR_STEP):
                if int(laps[i]) != cur_lap or not math.isfinite(times[i]):
                    group.append(0)  # de la vuelta anterior: apagado
                else:
                    group.append(self._kind_of(
                        float(times[i]),
                        float(best[i]) if best is not None else math.inf,
                        float(self._sess_micro[i]),
                    ))
            segs.append(group)
        return segs

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

    # ------------------------------------------------------------ refresco

    def refresh(self) -> None:
        an = self.analyzer
        pts = {}
        for drv, buf in self.hub.buffers.items():
            if buf.n:
                pt = an.position_time(drv)
                if pt is not None:
                    pts[drv] = pt
        ordered = sorted(pts, key=lambda d: float(pts[d][0][-1]), reverse=True)
        if not ordered:
            self.rows = []
            self.canvas.update()
            return
        # sin ancla de meta ni proyección la posición no es real (conexión a
        # mitad de sesión): esos autos van al fondo con posición "–"
        ready_set = {d for d in ordered if an.real_positions_ready(d)}
        ordered = ([d for d in ordered if d in ready_set]
                   + [d for d in ordered if d not in ready_set])
        if not self._order0 and len(ready_set) == len(ordered):
            self._order0 = {drv: i + 1 for i, drv in enumerate(ordered)}
        L = self.hub.track_length
        leader = ordered[0]
        pos_leader, t_leader = pts[leader]
        for drv in ordered:
            self._fold_bests(drv)
        bests = {drv: an.best_lap(drv) for drv in ordered}
        session_best = min(
            (b[1] for b in bests.values() if b is not None), default=math.inf
        )
        t_now_max = max(float(pts[d][1][-1]) for d in ordered)
        leader_buf = self.hub.buffers.get(leader)
        self._update_header(t_now_max, leader_buf.current_lap() if leader_buf else 0)

        rows: list[TowerRow] = []
        prev_gap: float | None = 0.0
        for i, drv in enumerate(ordered):
            info = self.hub.drivers.get(drv)
            buf = self.hub.buffers[drv]
            cur_lap = buf.current_lap()
            pos_now = float(pts[drv][0][-1])
            t_now = float(pts[drv][1][-1])
            ready = (drv in ready_set and leader in ready_set
                     and pos_now >= L / 3.0)
            catch = None
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
                catch = self._catch_laps(drv, ordered[i - 1], pts, L)
            prev_gap = gap_val

            last_lap = an.last_completed_lap(drv)
            last_time = an.lap_time(drv, last_lap) if last_lap else float("nan")
            best = bests.get(drv)
            last_kind = 0
            if math.isfinite(last_time):
                if last_time <= session_best + 1e-9:
                    last_kind = 3
                elif best is not None and last_time <= best[1] + 1e-9:
                    last_kind = 2
                else:
                    last_kind = 1
            best_kind = 0
            best_time = float("nan")
            if best is not None:
                best_time = best[1]
                best_kind = 3 if best[1] <= session_best + 1e-9 else 1

            sec_data = an.latest_sector_times(drv)
            sectors = []
            best_s = self._best_sec.get(drv)
            for k in range(3):
                if sec_data is None or not math.isfinite(float(sec_data[0][k])):
                    sectors.append((float("nan"), 0, False))
                else:
                    val = float(sec_data[0][k])
                    dim = int(sec_data[1][k]) != cur_lap
                    kind = self._kind_of(
                        val,
                        float(best_s[k]) if best_s is not None else math.inf,
                        float(self._sess_sec[k]),
                    )
                    sectors.append((val, kind, dim))

            # neumático actual: la entrada de la vuelta en curso, o la última
            # conocida extendida (el mapa por vuelta puede venir con retraso)
            tyre, tyre_age = "", 0
            tyre_map = self.hub.tyres.get(drv)
            if tyre_map:
                lap_key = cur_lap if cur_lap in tyre_map else max(tyre_map)
                tyre, tyre_age = tyre_map[lap_key]
                if cur_lap > lap_key:
                    tyre_age += cur_lap - lap_key

            # última pasada por boxes: vuelta, tiempo en calle y detenido
            pit_lap, pit_lane_s, pit_stop_s = 0, float("nan"), float("nan")
            pit_open = False
            visit = self.hub.last_pit_visit(drv)
            if visit is not None:
                v_lap, t_in, t_out = visit
                pit_lap = int(v_lap)
                pit_open = t_out is None
                end = self.hub.latest_t if pit_open else float(t_out)
                pit_lane_s = max(0.0, end - float(t_in))
                pit_stop_s = self.hub.pit_stationary_time(drv, float(t_in), end)
            else:
                stops = self.hub.pits.get(drv)
                if stops:
                    pit_lap = int(stops[-1][0])

            base0 = self._order0.get(drv)
            row_ready = drv in ready_set
            rows.append(TowerRow(
                drv=drv,
                code=info.code if info else drv,
                color=info.color if info else "#9aa0a6",
                pos=i + 1,
                ready=row_ready,
                delta=(base0 - (i + 1)) if base0 is not None and row_ready else None,
                speed=float(buf.col("speed")[-1]),
                gap_txt=gap_txt,
                int_txt=int_txt,
                last=last_time,
                last_kind=last_kind,
                best=best_time,
                best_kind=best_kind,
                pits=len([1 for lap, _t in self.hub.pits.get(drv, [])
                          if lap <= cur_lap]),
                sectors=sectors,
                segs=self._segs_for(drv, cur_lap),
                avg5=self._avg_lap(drv, 5),
                avg10=self._avg_lap(drv, 10),
                catch=catch,
                tyre=tyre,
                tyre_age=tyre_age,
                pit_lap=pit_lap,
                pit_lane_s=pit_lane_s,
                pit_stop_s=pit_stop_s,
                pit_open=pit_open,
            ))
        self.rows = rows
        self.canvas.setMinimumHeight(len(rows) * self.row_h)
        self.canvas.update()

    def _row_tooltip(self, row: TowerRow) -> str:
        parts = [f"{row.code} — P{row.pos}",
                 f"Pits: {row.pits}",
                 f"AVG5: {fmt_laptime(row.avg5)} · AVG10: {fmt_laptime(row.avg10)}"]
        if row.tyre:
            parts.insert(1, f"Tyre: {row.tyre.title()} · {row.tyre_age} laps")
        if row.pit_lap:
            lane = (f"{row.pit_lane_s:.1f}s"
                    if row.pit_lane_s == row.pit_lane_s else "—")
            stop = (f"{row.pit_stop_s:.1f}s"
                    if row.pit_stop_s == row.pit_stop_s else "—")
            parts.append(
                f"In pit lane NOW — {lane} (stopped {stop})" if row.pit_open
                else f"Last pit: L{row.pit_lap} · lane {lane} · stopped {stop}")
        if row.catch is not None:
            parts.append(f"Catching the car ahead in ~{row.catch:.1f} laps")
        return "\n".join(parts)

    # ------------------------------------------------------------- pintado

    def _paint_rows(self, p: QPainter, width: int) -> None:
        s = self.scale
        row_h = self.row_h
        base = self.font()
        f_small = QFont(base); f_small.setPointSizeF(6.5 * s)
        f_val = QFont(base); f_val.setPointSizeF(8.0 * s); f_val.setBold(True)
        f_big = QFont(base); f_big.setPointSizeF(10.0 * s); f_big.setBold(True)

        for i, row in enumerate(self.rows):
            y = i * row_h
            if i % 2:
                p.fillRect(0, y, width, row_h, QColor(theme.SURFACE_ALT))
            team = QColor(row.color)
            on_team = _text_on(team)
            top, bot = y + 3 * s, y + row_h // 2 + 1  # líneas superior e inferior
            line_h = row_h // 2 - 4 * s
            x = 4 * s

            # caja de posición y sigla en color de equipo ("–" si la posición
            # aún no es confiable: conexión a mitad de sesión)
            p.setPen(Qt.NoPen)
            p.setBrush(team)
            p.drawRoundedRect(QRectF(x, y + 4 * s, 22 * s, row_h - 8 * s), 3, 3)
            p.setPen(on_team)
            p.setFont(f_big)
            p.drawText(QRectF(x, y + 4 * s, 22 * s, row_h - 8 * s),
                       Qt.AlignCenter, str(row.pos) if row.ready else "–")
            x += 24 * s
            p.setPen(Qt.NoPen)
            p.setBrush(team)
            p.drawRoundedRect(QRectF(x, y + 4 * s, 40 * s, row_h - 8 * s), 3, 3)
            p.setPen(on_team)
            p.setFont(f_val)
            p.drawText(QRectF(x, y + 4 * s, 40 * s, row_h - 8 * s),
                       Qt.AlignCenter, row.code)
            x += 44 * s

            # neumático actual: letra del compuesto (arriba) y edad (abajo)
            if row.tyre:
                tc = QColor(theme.COMPOUND_COLORS.get(row.tyre.upper(), "#9aa0a6"))
                d = min(13.0 * s, line_h)
                circle = QRectF(x + (18 * s - d) / 2, top + (line_h - d) / 2 + 1, d, d)
                p.setPen(Qt.NoPen)
                p.setBrush(tc)
                p.drawEllipse(circle)
                p.setPen(_text_on(tc))
                p.setFont(f_small)
                p.drawText(circle, Qt.AlignCenter, row.tyre[0])
                p.setPen(QColor(theme.TEXT_MUTED))
                p.drawText(QRectF(x, bot, 18 * s, line_h), Qt.AlignCenter,
                           str(row.tyre_age))
            x += 20 * s

            # Δ posición desde el inicio (columna centrada)
            if width >= 270 * s:
                p.setFont(f_small)
                if row.delta is None:
                    d_txt = ""
                elif row.delta == 0:
                    p.setPen(QColor(theme.TEXT_MUTED))
                    d_txt = "−0"
                elif row.delta > 0:
                    p.setPen(QColor("#2fbf71"))
                    d_txt = f"▲{row.delta}"
                else:
                    p.setPen(QColor("#ff6b6b"))
                    d_txt = f"▼{-row.delta}"
                p.drawText(QRectF(x, y, 26 * s, row_h), Qt.AlignCenter, d_txt)
                x += 30 * s

            # velocidad
            if width >= 320 * s:
                p.setPen(QColor(theme.TEXT))
                p.setFont(f_val)
                p.drawText(QRectF(x, top, 34 * s, line_h + 2), Qt.AlignCenter,
                           f"{row.speed:.0f}")
                p.setPen(QColor(theme.TEXT_MUTED))
                p.setFont(f_small)
                p.drawText(QRectF(x, bot, 34 * s, line_h), Qt.AlignCenter, "km/h")
                x += 38 * s

            # píldoras LAST y BEST
            for value, kind, y_pill in ((row.last, row.last_kind, top),
                                        (row.best, row.best_kind, bot)):
                rect = QRectF(x, y_pill + 1, 66 * s, line_h - 1)
                if kind >= 2:
                    bg = _KIND_COLORS[kind]
                    p.setPen(Qt.NoPen)
                    p.setBrush(bg)
                    p.drawRoundedRect(rect, 7 * s, 7 * s)
                    p.setPen(_text_on(bg))
                else:
                    p.setPen(QColor(theme.TEXT) if kind else QColor(theme.TEXT_MUTED))
                p.setFont(f_val)
                p.drawText(rect, Qt.AlignCenter, fmt_laptime(value))
            x += 70 * s

            # INT (arriba, con contador de pits) y gap al líder (abajo)
            p.setFont(f_val)
            p.setPen(QColor(theme.TEXT))
            p.drawText(QRectF(x, top, 52 * s, line_h), Qt.AlignVCenter | Qt.AlignLeft,
                       row.int_txt if row.pos > 1 else "INT —")
            if row.pits:
                p.setFont(f_small)
                p.setPen(QColor("#d6be3c"))
                p.drawText(QRectF(x, top, 52 * s, line_h),
                           Qt.AlignVCenter | Qt.AlignRight, f"P{row.pits}")
            p.setFont(f_small)
            p.setPen(QColor(theme.TEXT_MUTED))
            p.drawText(QRectF(x, bot, 52 * s, line_h), Qt.AlignVCenter | Qt.AlignLeft,
                       "LDR " + (row.gap_txt if row.pos > 1 else "—"))
            x += 54 * s

            # promedios de las últimas 5/10 vueltas (sin vueltas de boxes)
            if width >= 400 * s:
                p.setFont(f_small)
                for label, value, y_avg in (("A5", row.avg5, top),
                                            ("A10", row.avg10, bot)):
                    p.setPen(QColor(theme.TEXT_MUTED))
                    p.drawText(QRectF(x, y_avg, 18 * s, line_h),
                               Qt.AlignVCenter | Qt.AlignLeft, label)
                    p.setPen(QColor(theme.TEXT))
                    p.drawText(QRectF(x + 20 * s, y_avg, 44 * s, line_h),
                               Qt.AlignVCenter | Qt.AlignLeft, fmt_laptime(value))
                x += 68 * s

            # última pasada por boxes: vuelta + tiempo en calle (arriba) y
            # tiempo detenido (abajo); la calle en amarillo si está adentro
            if width >= 470 * s:
                if row.pit_lap:
                    p.setFont(f_small)
                    p.setPen(QColor(theme.TEXT_MUTED))
                    p.drawText(QRectF(x, top, 22 * s, line_h),
                               Qt.AlignVCenter | Qt.AlignLeft, f"L{row.pit_lap}")
                    p.setPen(QColor("#d6be3c") if row.pit_open else QColor(theme.TEXT))
                    lane_txt = (f"{row.pit_lane_s:.1f}s"
                                if row.pit_lane_s == row.pit_lane_s else "—")
                    p.drawText(QRectF(x + 22 * s, top, 42 * s, line_h),
                               Qt.AlignVCenter | Qt.AlignLeft, lane_txt)
                    p.setPen(QColor(theme.TEXT_MUTED))
                    p.drawText(QRectF(x, bot, 22 * s, line_h),
                               Qt.AlignVCenter | Qt.AlignLeft, "stp")
                    p.setPen(QColor(theme.TEXT))
                    stop_txt = (f"{row.pit_stop_s:.1f}s"
                                if row.pit_stop_s == row.pit_stop_s else "—")
                    p.drawText(QRectF(x + 22 * s, bot, 42 * s, line_h),
                               Qt.AlignVCenter | Qt.AlignLeft, stop_txt)
                x += 66 * s

            # microsectores (rayitas) + tiempos de sector debajo
            if width - x >= 96 * s and row.segs:
                avail = width - x - 4 * s
                n_total = sum(len(g) for g in row.segs) or 1
                gap_px = 3.0 * s
                dash_w = max(2.0, min(6.0 * s,
                                      (avail - gap_px * len(row.segs)) / n_total - 1.0))
                sx = x
                for k, group in enumerate(row.segs):
                    gx = sx
                    for kind in group:
                        p.setPen(Qt.NoPen)
                        p.setBrush(_KIND_COLORS.get(kind, _KIND_COLORS[0]))
                        p.drawRoundedRect(QRectF(gx, top + 2, dash_w, line_h - 4), 1, 1)
                        gx += dash_w + 1.0
                    if k < len(row.sectors):
                        val, kind, dim = row.sectors[k]
                        color = (QColor(theme.TEXT_MUTED) if dim or kind == 0
                                 else _KIND_COLORS[max(kind, 1)])
                        p.setPen(color)
                        p.setFont(f_small)
                        p.drawText(QRectF(sx - 2, bot, gx - sx + 4, line_h),
                                   Qt.AlignCenter, fmt_secs(val))
                    sx = gx + gap_px
            # separador
            p.setPen(QPen(QColor(theme.BORDER), 1))
            p.drawLine(0, y + row_h - 1, width, y + row_h - 1)
