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
from PySide6.QtCore import QEvent, QPointF, Qt, QRectF
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QCheckBox, QHBoxLayout, QLabel, QMenu, QScrollArea, QToolButton,
    QToolTip, QVBoxLayout, QWidget, QWidgetAction,
)

from .. import config
from ..hub import DataHub
from ..timing import N_MICRO, TimingAnalyzer
from . import theme
from .driver_filter import DriverFilterButton
from .timing_view import fmt_gap, fmt_laptime, fmt_secs

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

# columnas de datos que el usuario puede mostrar/ocultar (▦, persistido)
_COLUMNS = [
    ("tyre", "Tyre"),
    ("delta_pos", "Δ position"),
    ("speed", "Speed"),
    ("pills", "LAST / BEST"),
    ("interval", "INT / gap / PIT-RET tags"),
    ("wave", "Delta graph"),
    ("avgs", "AVG5 / AVG10"),
    ("pit", "Last pit stop"),
    ("micro", "Mini-sectors"),
]

ROW_H = 38
WAVE_BINS = 240  # bins de la onda delta (una vuelta = 240 casilleros)
_WAVE_FRS = (np.arange(WAVE_BINS) + 0.5) / WAVE_BINS


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
    pit_out: bool = False       # vuelta de salida (cerró boxes hace <= 1 vuelta)
    retired: bool = False       # fuera de carrera (abandono / clavado)
    ref_gap_txt: str = ""       # gap contra la referencia elegida ("—" en la ref)
    stew: str = ""              # chip de comisarios: ⚠ / +5s / DT / SG
    wave: object = None         # onda delta de la última vuelta (frac, ±s)
    wave_now: float = 0.0       # fracción de vuelta de la posición actual


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

    def mousePressEvent(self, ev) -> None:
        # click en una fila: ese auto pasa a ser la referencia de los deltas
        if ev.button() == Qt.LeftButton:
            i = int(ev.position().y() // self.tower.row_h)
            rows = self.tower.rows
            if 0 <= i < len(rows):
                self.tower.set_reference(rows[i].drv)
                return
        super().mousePressEvent(ev)

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
        self._marks_sig: tuple | None = None
        # referencia elegida por click: los deltas se muestran contra ella
        self.ref_drv: str | None = None
        self._ref_vals: TowerRow | None = None
        # delta gráfico (lógica JRT): por rival, la vuelta actual del
        # cursor (NaN sin pintar), la vuelta anterior atenuada, el REL de
        # referencia capturado al inicio de la vuelta, el último REL/pos
        # del cursor y la vuelta del cursor
        self._wave_store: dict[str, np.ndarray] = {}
        self._wave_prev: dict[str, np.ndarray | None] = {}
        self._wave_ref0: dict[str, float | None] = {}
        self._wave_rel: dict[str, float] = {}
        self._wave_pos: dict[str, float] = {}
        self._wave_lap: dict[str, int] = {}
        self._wave_target: str | None = None
        self._folded: dict[str, int] = {}
        self._best_micro: dict[str, np.ndarray] = {}
        self._sess_micro = np.full(N_MICRO, np.inf)
        self._sess_micro_by: list[str | None] = [None] * N_MICRO
        self._best_sec: dict[str, np.ndarray] = {}
        self._sess_sec = np.full(3, np.inf)
        self._sess_sec_by: list[str | None] = [None] * 3

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
        # columnas visibles (▦): mucha info no siempre hace falta
        stored_cols = (cfg or {}).get("ui", {}).get("tower_cols", {})
        self._cols: dict[str, bool] = {
            k: False for k, v in stored_cols.items() if v is False}
        self.cols_btn = QToolButton()
        self.cols_btn.setText("▦")
        self.cols_btn.setAutoRaise(True)
        self.cols_btn.setFixedSize(22, 16)
        self.cols_btn.setToolTip("Choose which data columns the tower shows")
        self.cols_btn.setPopupMode(QToolButton.InstantPopup)
        self.cols_btn.setStyleSheet(
            "QToolButton::menu-indicator { image: none; }")
        cols_menu = QMenu(self)
        cols_box = QWidget()
        cols_lay = QVBoxLayout(cols_box)
        cols_lay.setContentsMargins(6, 4, 6, 4)
        cols_lay.setSpacing(2)
        self._col_checks: dict[str, QCheckBox] = {}
        for key, label in _COLUMNS:
            chk = QCheckBox(label)
            chk.setChecked(self._col(key))
            chk.toggled.connect(lambda on, k=key: self.set_column(k, on))
            cols_lay.addWidget(chk)
            self._col_checks[key] = chk
        cols_action = QWidgetAction(cols_menu)
        cols_action.setDefaultWidget(cols_box)
        cols_menu.addAction(cols_action)
        self.cols_btn.setMenu(cols_menu)
        head.addWidget(self.cols_btn)
        # filtro local de autos visibles (independiente del panel Drivers)
        self.filter_btn = DriverFilterButton(hub, cfg, "tower_hidden_cars")
        self.filter_btn.changed.connect(self.refresh)
        head.addWidget(self.filter_btn)
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

    def _col(self, key: str) -> bool:
        return self._cols.get(key, True)

    def set_column(self, key: str, on: bool) -> None:
        """Muestra/oculta una columna de datos (persistido en config)."""
        if on:
            self._cols.pop(key, None)
        else:
            self._cols[key] = False
        if self.cfg is not None:
            self.cfg.setdefault("ui", {})["tower_cols"] = dict(self._cols)
            config.save_config(self.cfg)
        chk = self._col_checks.get(key)
        if chk is not None and chk.isChecked() != on:
            chk.blockSignals(True)
            chk.setChecked(on)
            chk.blockSignals(False)
        self.canvas.update()

    def set_reference(self, drv: str | None) -> None:
        """Referencia de la torre: gap, LAST/BEST, sectores y promedios se
        muestran como delta contra este auto; el mismo click la saca."""
        self.ref_drv = None if drv == self.ref_drv else drv
        self.refresh()

    def clear_data(self) -> None:
        self.analyzer.clear()
        self.rows = []
        # ref_drv se CONSERVA: un seek de la línea de tiempo limpia los
        # datos pero no debe robarle al usuario la referencia elegida.
        # _marks_sig se invalida: los arrays recién creados tienen el tamaño
        # DEFAULT y el guard del refresh debe re-dimensionarlos a la config
        # de µ activa (sin esto, con µ personalizados cada refresh moría
        # con un ValueError de shapes tras cualquier seek)
        self._marks_sig = None
        self._ref_vals = None
        self._clear_waves()
        self._order0.clear()
        self._folded.clear()
        self._best_micro.clear()
        self._sess_micro = np.full(N_MICRO, np.inf)
        self._sess_micro_by = [None] * N_MICRO
        self._best_sec.clear()
        self._sess_sec = np.full(3, np.inf)
        self._sess_sec_by = [None] * 3
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
        best_m = self._best_micro.setdefault(
            drv, np.full(len(self._sess_micro), np.inf))
        best_s = self._best_sec.setdefault(drv, np.full(3, np.inf))
        for lap in buf.completed_laps():
            if lap <= done:
                continue
            micro = an.micro_times(drv, lap)
            # cinturón: si una vuelta cacheada viniera con otra cantidad de
            # µ que los acumuladores, se ignora (jamás romper el refresh)
            if micro is not None and len(micro) == len(best_m):
                vals = np.where(np.isfinite(micro), micro, np.inf)
                np.minimum(best_m, vals, out=best_m)
                # dueño del mejor absoluto: solo una mejora ESTRICTA lo
                # roba (el empate no) — un único violeta por µ en la tanda
                for i in np.flatnonzero(vals < self._sess_micro):
                    self._sess_micro_by[i] = drv
                np.minimum(self._sess_micro, vals, out=self._sess_micro)
            sec = np.array(an.sector_times(drv, lap))
            vals = np.where(np.isfinite(sec), sec, np.inf)
            np.minimum(best_s, vals, out=best_s)
            for k in np.flatnonzero(vals < self._sess_sec):
                self._sess_sec_by[k] = drv
            np.minimum(self._sess_sec, vals, out=self._sess_sec)
            self._folded[drv] = lap

    @staticmethod
    def _wave_color(v: float) -> QColor:
        """Escala de color JRT del delta gráfico: gris neutro → rojo
        (perdiendo) o verde (ganando) hasta 1 s; de 1 a 2 s vira a magenta
        / cian; satura en ±2 s."""
        def mix(c0: QColor, c1: QColor, t: float) -> QColor:
            return QColor(int(c0.red() + (c1.red() - c0.red()) * t),
                          int(c0.green() + (c1.green() - c0.green()) * t),
                          int(c0.blue() + (c1.blue() - c0.blue()) * t))

        a = min(abs(v), 2.0)
        grey = QColor("#6a6f78")
        if v > 0:
            base, sat = QColor("#e04b3a"), QColor("#c852ff")
        else:
            base, sat = QColor("#2fbf71"), QColor("#35d0c8")
        if a <= 1.0:
            return mix(grey, base, a)
        return mix(base, sat, a - 1.0)

    @staticmethod
    def _ref_color(sign: float) -> QColor:
        """Convención de signo y color con referencia elegida, SIEMPRE
        desde su punto de vista: POSITIVO/rojo = la referencia PIERDE
        contra el auto de la fila; NEGATIVO/verde = le GANA."""
        if sign > 0:
            return QColor("#ff6b5e")
        if sign < 0:
            return QColor("#2fbf71")
        return QColor(theme.TEXT)

    def _kind_of(self, value: float, personal: float, session: float,
                 mine: bool) -> int:
        """Violeta (3) SOLO para el dueño del mejor absoluto de la tanda:
        un único violeta por vuelta/sector/µ; los empates no lo roban."""
        if not math.isfinite(value):
            return 0
        if mine and value <= session + 1e-9:
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
        slices = self.analyzer.sector_slices()
        data = self.analyzer.latest_micro_times(drv)
        if data is None:
            return [[0] * max(b - a, 1) for a, b in slices]
        times, laps = data
        best = self._best_micro.get(drv)
        segs = []
        for k, (a, b) in enumerate(slices):
            group = []
            for i in range(a, b):
                if int(laps[i]) != cur_lap or not math.isfinite(times[i]):
                    group.append(0)  # de la vuelta anterior: apagado
                else:
                    group.append(self._kind_of(
                        float(times[i]),
                        float(best[i]) if best is not None else math.inf,
                        float(self._sess_micro[i]),
                        self._sess_micro_by[i] == drv,
                    ))
            segs.append(group)
        return segs

    def _clear_waves(self) -> None:
        for d in (self._wave_store, self._wave_prev, self._wave_ref0,
                  self._wave_rel, self._wave_pos, self._wave_lap):
            d.clear()

    @staticmethod
    def _rel_to_ref(pts: dict, drv: str, ref: str) -> float | None:
        """REL con signo JRT: positivo = el rival va ADELANTE de la
        referencia; medido en la posición del auto que va detrás."""
        pos_d, t_d = pts[drv]
        pos_r, t_r = pts[ref]
        pd, pr = float(pos_d[-1]), float(pos_r[-1])
        if pd >= pr:
            if float(pos_d[0]) <= pr <= pd:
                return float(t_r[-1]) - float(np.interp(pr, pos_d, t_d))
        elif float(pos_r[0]) <= pd <= float(pos_r[-1]):
            return -(float(t_d[-1]) - float(np.interp(pd, pos_r, t_r)))
        return None

    def _update_wave(self, an, pts: dict, drv: str, ref: str,
                     L: float) -> tuple:
        """Delta gráfico (lógica JRT): pinta, en el punto por donde pasa el
        auto que va DETRÁS de la pareja rival↔referencia (el cursor), la
        variación del REL desde el inicio de la vuelta del cursor
        (delta = REL_ahora − REL_al_inicio). Positivo (rojo, arriba) = la
        referencia viene PERDIENDO contra ese rival en esta vuelta;
        negativo (verde, abajo) = viene ganando. Reset por vuelta (la
        anterior queda atenuada hasta sobrescribirse), por sobrepaso con
        salto de signo > 1 s y por saltos de timeline."""
        pos_d = pts[drv][0]
        now_frac = (float(pos_d[-1]) % L) / L
        rel = self._rel_to_ref(pts, drv, ref)
        if rel is None:
            store = self._wave_store.get(drv)
            wave = ((_WAVE_FRS, store, self._wave_prev.get(drv))
                    if store is not None else None)
            return wave, now_frac
        cur_pos = min(float(pos_d[-1]), float(pts[ref][0][-1]))
        last_pos = self._wave_pos.get(drv)
        last_rel = self._wave_rel.get(drv)
        self._wave_rel[drv] = rel
        store = self._wave_store.get(drv)
        # sobrepaso real (cambio de signo con salto): borrar y re-arrancar
        if (store is not None and last_rel is not None
                and (rel > 0.0) != (last_rel > 0.0)
                and abs(rel - last_rel) > 1.0):
            store = None
        # salto de timeline hacia atrás: el cursor retrocedió
        if (store is not None and last_pos is not None
                and cur_pos < last_pos - 50.0):
            store = None
        lap_now = int(cur_pos // L)
        if store is None:
            store = np.full(WAVE_BINS, np.nan)
            self._wave_store[drv] = store
            self._wave_prev[drv] = None
            self._wave_lap[drv] = lap_now
            self._wave_ref0[drv] = rel if rel != 0.0 else None
            self._wave_pos[drv] = cur_pos
            return (_WAVE_FRS, store, None), now_frac
        if lap_now != self._wave_lap.get(drv):
            # nueva vuelta del cursor: la actual pasa a "anterior"
            # (atenuada) y se recaptura la referencia — arranque en cero
            self._wave_prev[drv] = store.copy()
            store[:] = np.nan
            self._wave_lap[drv] = lap_now
            self._wave_ref0[drv] = rel if rel != 0.0 else None
            last_pos = lap_now * L
        ref0 = self._wave_ref0.get(drv)
        if ref0 is None:  # referencia aún inválida (largada): recapturar
            self._wave_ref0[drv] = rel if rel != 0.0 else None
            self._wave_pos[drv] = cur_pos
            return (_WAVE_FRS, store, self._wave_prev.get(drv)), now_frac
        start = cur_pos if last_pos is None else max(last_pos, cur_pos - L)
        b0 = int(math.floor(start / L * WAVE_BINS))
        b1 = int(math.floor(cur_pos / L * WAVE_BINS))
        if b1 > b0:
            bins = np.arange(b0 + 1, b1 + 1)
            store[bins % WAVE_BINS] = rel - ref0
        self._wave_pos[drv] = cur_pos
        return (_WAVE_FRS, store, self._wave_prev.get(drv)), now_frac

    @staticmethod
    def _gap_to(pts: dict, drv: str, ref: str, L: float) -> str:
        """Gap firmado de drv contra ref, con el signo desde la REFERENCIA
        (convención JRT): + = ese auto va ADELANTE (la referencia pierde),
        − = va detrás (favorable); '±nL' con una vuelta o más."""
        pos_d, t_d = pts[drv]
        pos_r, t_r = pts[ref]
        pd, pr = float(pos_d[-1]), float(pos_r[-1])
        behind = pr - pd
        if abs(behind) >= L:
            laps = int(abs(behind) // L)
            return f"-{laps}L" if behind > 0 else f"+{laps}L"
        if behind >= 0.0:  # el rival va detrás: favorable → negativo
            if float(pos_r[0]) <= pd <= pr:
                return (f"{-(float(t_d[-1]) - float(np.interp(pd, pos_r, t_r))):+.1f}")
        elif float(pos_d[0]) <= pr <= pd:
            return f"{float(t_r[-1]) - float(np.interp(pr, pos_d, t_d)):+.1f}"
        return "—"

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
        # marcas de µ cambiadas (config del panel Microsectors, límites
        # oficiales recién derivados): los mejores acumulados se refoldean
        # desde cero con las marcas nuevas
        marks_sig = tuple(np.round(an._mark_dists(), 1))
        if marks_sig != self._marks_sig:
            self._marks_sig = marks_sig
            n_mu = an.n_micro()
            self._folded.clear()
            self._best_micro.clear()
            self._sess_micro = np.full(n_mu, np.inf)
            self._sess_micro_by = [None] * n_mu
            self._best_sec.clear()
            self._sess_sec = np.full(3, np.inf)
            self._sess_sec_by = [None] * 3
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
        # mitad de sesión): esos autos van al fondo con posición "–"; los
        # fuera de carrera (abandono / clavados) van últimos con tag RET
        ready_set = {d for d in ordered if an.real_positions_ready(d)}
        active_set = {d for d in ordered if self.hub.is_active(d)}
        ordered = ([d for d in ordered if d in ready_set and d in active_set]
                   + [d for d in ordered
                      if d not in ready_set and d in active_set]
                   + [d for d in ordered if d not in active_set])
        if not self._order0 and len(ready_set) == len(ordered):
            self._order0 = {drv: i + 1 for i, drv in enumerate(ordered)}
        L = self.hub.track_length
        leader = ordered[0]
        pos_leader, t_leader = pts[leader]
        for drv in ordered:
            self._fold_bests(drv)
        bests = {drv: an.best_lap(drv) for drv in ordered}
        # mejor absoluto con dueño (desempate por vuelta y auto: el violeta
        # de vuelta es de UN solo piloto en toda la tanda)
        sb = min(((b[1], b[0], drv) for drv, b in bests.items() if b is not None),
                 default=None)
        session_best = sb[0] if sb else math.inf
        session_best_by = sb[2] if sb else None
        t_now_max = max(float(pts[d][1][-1]) for d in ordered)
        leader_buf = self.hub.buffers.get(leader)
        self._update_header(t_now_max, leader_buf.current_lap() if leader_buf else 0)

        # referencia por click: gap de cada auto contra ella
        ref_ok = self.ref_drv if self.ref_drv in pts else None
        # blanco de la onda delta: la referencia, o el puntero de la carrera
        wave_ref = ref_ok if ref_ok is not None else leader
        if wave_ref != self._wave_target:  # cambió el blanco: ondas de cero
            self._wave_target = wave_ref
            self._clear_waves()
        stew_flags = self.hub.stewards_flags()
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
            if drv not in active_set:
                gap_txt, int_txt, gap_val = "—", "—", None
            elif i == 0:
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

            ref_gap_txt = ""
            if ref_ok is not None:
                if drv == ref_ok or drv not in active_set:
                    ref_gap_txt = "—"
                else:
                    ref_gap_txt = self._gap_to(pts, drv, ref_ok, L)

            # onda delta: pintado incremental delante de la línea blanca
            wave, wave_now = None, 0.0
            if (wave_ref is not None and drv != wave_ref
                    and drv in active_set):
                wave, wave_now = self._update_wave(an, pts, drv, wave_ref, L)

            last_lap = an.last_completed_lap(drv)
            last_time = an.lap_time(drv, last_lap) if last_lap else float("nan")
            best = bests.get(drv)
            last_kind = 0
            if math.isfinite(last_time):
                if drv == session_best_by and last_time <= session_best + 1e-9:
                    last_kind = 3
                elif best is not None and last_time <= best[1] + 1e-9:
                    last_kind = 2
                else:
                    last_kind = 1
            best_kind = 0
            best_time = float("nan")
            if best is not None:
                best_time = best[1]
                best_kind = 3 if drv == session_best_by else 1

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
                        self._sess_sec_by[k] == drv,
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
            pit_out = False
            visit = self.hub.last_pit_visit(drv)
            if visit is not None:
                v_lap, t_in, t_out = visit
                pit_lap = int(v_lap)
                pit_open = self.hub.pit_visit_open(visit)
                end = self.hub.latest_t if pit_open else float(t_out)
                pit_lane_s = max(0.0, end - float(t_in))
                pit_stop_s = self.hub.pit_stationary_time(drv, float(t_in), end)
                # vuelta de salida: la visita cerró en esta vuelta o la anterior
                pit_out = not pit_open and cur_lap <= pit_lap + 1
            else:
                stops = self.hub.pit_stops_done(drv)
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
                pits=len(self.hub.pit_stops_done(drv)),
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
                pit_out=pit_out,
                retired=drv not in active_set,
                ref_gap_txt=ref_gap_txt,
                stew=stew_flags.get(drv, ""),
                wave=wave,
                wave_now=wave_now,
            ))
        # valores de la referencia para pintar deltas (antes del filtro 👥:
        # la ref puede estar oculta y los deltas siguen valiendo)
        self._ref_vals = next((r for r in rows if r.drv == self.ref_drv), None)
        # el filtro 👥 solo saca filas de la vista: posiciones, gaps e INT se
        # calculan siempre sobre todos los autos (P y gaps reales)
        if self.filter_btn.hidden:
            rows = [r for r in rows if r.drv not in self.filter_btn.hidden]
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
        parts.append("Click: compare everyone against this driver "
                     "(click again to clear)")
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
            if row.drv == self.ref_drv:
                hl = QColor(theme.ACCENT)
                hl.setAlpha(30)
                p.fillRect(0, y, width, row_h, hl)
            ref_vals = self._ref_vals
            comparing = (self.ref_drv is not None and ref_vals is not None
                         and row.drv != self.ref_drv)
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
            if self._col("tyre") and row.tyre:
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
            if self._col("tyre"):
                x += 20 * s

            # Δ posición desde el inicio (columna centrada)
            if self._col("delta_pos") and width >= 270 * s:
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
            if self._col("speed") and width >= 320 * s:
                p.setPen(QColor(theme.TEXT))
                p.setFont(f_val)
                p.drawText(QRectF(x, top, 34 * s, line_h + 2), Qt.AlignCenter,
                           f"{row.speed:.0f}")
                p.setPen(QColor(theme.TEXT_MUTED))
                p.setFont(f_small)
                p.drawText(QRectF(x, bot, 34 * s, line_h), Qt.AlignCenter, "km/h")
                x += 38 * s

            # píldoras LAST y BEST; con referencia elegida, delta contra ella
            pill_specs = () if not self._col("pills") else (
                    (row.last, row.last_kind, top,
                     ref_vals.last if comparing else float("nan")),
                    (row.best, row.best_kind, bot,
                     ref_vals.best if comparing else float("nan")))
            for value, kind, y_pill, ref_val in pill_specs:
                rect = QRectF(x, y_pill + 1, 66 * s, line_h - 1)
                if comparing:
                    # signo desde la referencia: + = la ref pierde (la fila
                    # tiene mejor tiempo), − = la ref gana
                    delta = ref_val - value
                    if math.isfinite(delta):
                        p.setPen(self._ref_color(delta))
                        txt = fmt_gap(delta)
                    else:
                        p.setPen(QColor(theme.TEXT_MUTED))
                        txt = "—"
                    p.setFont(f_val)
                    p.drawText(rect, Qt.AlignCenter, txt)
                    continue
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
            if self._col("pills"):
                x += 70 * s

            # INT (arriba, con contador de pits) y gap al líder (abajo); en
            # boxes o en la vuelta de salida un tag PIT/OUT reemplaza al
            # INT, y un auto fuera de carrera lleva RET
            show_int = self._col("interval")
            if show_int and (row.retired or row.pit_open or row.pit_out):
                tag, bg = (("RET", QColor("#b3404a")) if row.retired
                           else ("PIT", QColor("#d6be3c")) if row.pit_open
                           else ("OUT", QColor("#2fbf71")))
                rect_tag = QRectF(x, top + 1, 36 * s, line_h - 1)
                p.setPen(Qt.NoPen)
                p.setBrush(bg)
                p.drawRoundedRect(rect_tag, 4, 4)
                p.setPen(_text_on(bg))
                p.setFont(f_val)
                p.drawText(rect_tag, Qt.AlignCenter, tag)
            elif show_int:
                p.setFont(f_val)
                p.setPen(QColor(theme.TEXT))
                p.drawText(QRectF(x, top, 52 * s, line_h),
                           Qt.AlignVCenter | Qt.AlignLeft,
                           row.int_txt if row.pos > 1 else "INT —")
            if show_int and row.pits:
                p.setFont(f_small)
                p.setPen(QColor("#d6be3c"))
                p.drawText(QRectF(x, top, 52 * s, line_h),
                           Qt.AlignVCenter | Qt.AlignRight, f"P{row.pits}")
            if show_int:
                p.setFont(f_small)
                if self.ref_drv is not None:
                    # gap contra la referencia, coloreado desde SU punto de
                    # vista: rojo = esa fila va adelante (la ref pierde)
                    txt_g = row.ref_gap_txt or "—"
                    if row.drv == self.ref_drv:
                        p.setPen(QColor(theme.ACCENT))
                    elif txt_g.startswith("-"):
                        p.setPen(self._ref_color(-1.0))
                    elif txt_g.startswith("+"):
                        p.setPen(self._ref_color(1.0))
                    else:
                        p.setPen(QColor(theme.TEXT_MUTED))
                    p.drawText(QRectF(x, bot, 52 * s, line_h),
                               Qt.AlignVCenter | Qt.AlignLeft,
                               "REF " + txt_g)
                else:
                    p.setPen(QColor(theme.TEXT_MUTED))
                    p.drawText(QRectF(x, bot, 52 * s, line_h),
                               Qt.AlignVCenter | Qt.AlignLeft,
                               "LDR " + (row.gap_txt if row.pos > 1 else "—"))
                if row.stew:
                    # chip de comisarios: investigación / sanción pendiente
                    p.setFont(f_small)
                    p.setPen(QColor("#d6be3c") if row.stew == "⚠"
                             else QColor("#ff6b5e"))
                    p.drawText(QRectF(x, bot, 52 * s, line_h),
                               Qt.AlignVCenter | Qt.AlignRight, row.stew)
                x += 54 * s

            # onda delta de la última vuelta contra la referencia (o el
            # líder): X = la vuelta completa, línea blanca = posición
            # actual; rojo arriba = perdiendo, verde abajo = ganando,
            # magenta = fuera de la escala de ±2 s
            if self._col("wave") and width >= 560 * s:
                w_col = 80 * s
                mid_y = y + row_h / 2.0
                amp = (row_h / 2.0 - 3.0)  # px que representan 2 s
                p.setPen(QPen(QColor(theme.BORDER), 1))
                p.drawLine(QPointF(x, mid_y), QPointF(x + w_col, mid_y))
                if row.wave is not None:
                    frs, cur_w, prev_w = row.wave
                    # deflexión completa = ±1 s; el color sigue hasta ±2 s
                    # (magenta = perdiendo mucho, cian = ganando mucho); la
                    # vuelta anterior queda atenuada hasta sobrescribirse
                    for i in range(len(cur_w)):
                        v = float(cur_w[i])
                        dim = False
                        if v != v and prev_w is not None:
                            v = float(prev_w[i])
                            dim = True
                        if v != v:
                            continue
                        color = self._wave_color(v)
                        if dim:
                            color.setAlpha(70)
                        p.setPen(QPen(color, 1))
                        h_px = max(min(v, 1.0), -1.0) * amp
                        px = x + float(frs[i]) * w_col
                        p.drawLine(QPointF(px, mid_y),
                                   QPointF(px, mid_y - h_px))
                    px_now = x + row.wave_now * w_col
                    p.setPen(QPen(QColor("#ffffff"), 1))
                    p.drawLine(QPointF(px_now, y + 3),
                               QPointF(px_now, y + row_h - 3))
                x += w_col + 4 * s

            # promedios de las últimas 5/10 vueltas (sin vueltas de boxes)
            if self._col("avgs") and width >= 400 * s:
                p.setFont(f_small)
                for label, value, y_avg, ref_val in (
                        ("A5", row.avg5, top,
                         ref_vals.avg5 if comparing else float("nan")),
                        ("A10", row.avg10, bot,
                         ref_vals.avg10 if comparing else float("nan"))):
                    p.setPen(QColor(theme.TEXT_MUTED))
                    p.drawText(QRectF(x, y_avg, 18 * s, line_h),
                               Qt.AlignVCenter | Qt.AlignLeft, label)
                    if comparing:
                        delta = ref_val - value  # signo desde la referencia
                        ok = math.isfinite(delta)
                        p.setPen(self._ref_color(delta) if ok
                                 else QColor(theme.TEXT_MUTED))
                        txt = fmt_gap(delta) if ok else "—"
                    else:
                        p.setPen(QColor(theme.TEXT))
                        txt = fmt_laptime(value)
                    p.drawText(QRectF(x + 20 * s, y_avg, 44 * s, line_h),
                               Qt.AlignVCenter | Qt.AlignLeft, txt)
                x += 68 * s

            # última pasada por boxes: vuelta + tiempo en calle (arriba) y
            # tiempo detenido (abajo); la calle en amarillo si está adentro
            if self._col("pit") and width >= 470 * s:
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
            if self._col("micro") and width - x >= 96 * s and row.segs:
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
                        if comparing and k < len(ref_vals.sectors):
                            # delta de sector, con el signo desde la ref
                            delta = ref_vals.sectors[k][0] - val
                            ok = math.isfinite(delta)
                            p.setPen(self._ref_color(delta) if ok
                                     else QColor(theme.TEXT_MUTED))
                            txt = fmt_gap(delta) if ok else "—"
                        else:
                            p.setPen(QColor(theme.TEXT_MUTED)
                                     if dim or kind == 0
                                     else _KIND_COLORS[max(kind, 1)])
                            txt = fmt_secs(val)
                        p.setFont(f_small)
                        p.drawText(QRectF(sx - 2, bot, gx - sx + 4, line_h),
                                   Qt.AlignCenter, txt)
                    sx = gx + gap_px
            # separador
            p.setPen(QPen(QColor(theme.BORDER), 1))
            p.drawLine(0, y + row_h - 1, width, y + row_h - 1)
