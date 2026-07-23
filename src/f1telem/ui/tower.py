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
    QCheckBox, QComboBox, QHBoxLayout, QLabel, QMenu, QScrollArea,
    QToolButton, QToolTip, QVBoxLayout, QWidget, QWidgetAction,
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

def quali_drops(n: int) -> tuple[int, int]:
    """Eliminados en Q1 y Q2 según el tamaño de la grilla. La invariante
    del formato es que Q3 SIEMPRE corre con 10 autos: con 22 (2026) son
    6+6, con 20 eran 5+5; una grilla impar reparte el resto (21 → 5+6).
    Grillas chicas (tests/demos): proporcional."""
    if n > 12:
        d1 = (n - 10) // 2
        return d1, (n - 10) - d1
    d = max(1, n // 4)
    return d, d


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
    delta: int | None          # posiciones ganadas (+) desde la grilla oficial
                               # (OpenF1) o, sin ella, el primer orden observado
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
    grid: int | None = None     # posición de grilla oficial (OpenF1)
    pit_stop_off: float = float("nan")  # parada oficial (s detenido, OpenF1)
    free_stop: bool = False     # parada "gratis": el de atrás está a más
                                # de una Ventana de Box + 1 s
    out_tag: str = ""           # quali: eliminado ("OUT Q1" / "OUT SQ2")
    cut_txt: str = ""           # quali: cuánto le falta para salvarse
    drop: bool = False          # quali: hoy queda eliminado (zona roja)
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
        # clasificación por tandas (Q1-Q3): cachés y estado de la tanda
        self._lap_end_cache: dict[tuple[str, int], float] = {}
        self._quali_phase = 0
        self._quali_hdr: str | None = None
        self.quali_seps: list[tuple[int, str]] = []
        self.quali_cut_row: int | None = None

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
        # ordenamiento de las filas: "Position" = posición de carrera en
        # race/sprint y vuelta rápida en el resto; o una métrica a demanda
        self.sort_combo = QComboBox()
        for label, key in (("Position", "position"), ("Last lap", "last"),
                           ("Best lap", "best"), ("AVG5", "a5"),
                           ("AVG10", "a10"), ("S1", "s1"), ("S2", "s2"),
                           ("S3", "s3")):
            self.sort_combo.addItem(label, key)
        self.sort_combo.setToolTip(
            "Row order. Position = race order in races/sprints, fastest "
            "lap in practice/quali; the rest sort by that metric (best "
            "sector = personal best)")
        self.sort_combo.setFixedHeight(18)
        self.sort_combo.setStyleSheet("font-size: 7pt;")
        stored_sort = (cfg or {}).get("ui", {}).get("tower_sort", "position")
        idx_sort = self.sort_combo.findData(stored_sort)
        if idx_sort >= 0:
            self.sort_combo.setCurrentIndex(idx_sort)
        self.sort_combo.currentIndexChanged.connect(self._sort_changed)
        head.addWidget(self.sort_combo)
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

    def _sort_changed(self, _i: int = 0) -> None:
        if self.cfg is not None:
            self.cfg.setdefault("ui", {})["tower_sort"] = \
                self.sort_combo.currentData()
            config.save_config(self.cfg)
        self.refresh()

    def _race_like(self) -> bool:
        """Carrera o sprint: el orden natural es el de pista; en práctica
        y clasificación, la vuelta rápida."""
        meta = self.hub.session_meta
        name = str(meta.get("name", "")).strip().lower()
        typ = str(meta.get("type", "")).strip().lower()
        return typ == "race" or name in ("race", "sprint")

    def _sort_value(self, drv: str, mode: str) -> float:
        an = self.analyzer
        if mode == "last":
            lap = an.last_completed_lap(drv)
            v = an.lap_time(drv, lap) if lap else float("nan")
        elif mode == "best":
            b = an.best_lap(drv)
            v = b[1] if b is not None else float("nan")
        elif mode in ("a5", "a10"):
            v = self._avg_lap(drv, 5 if mode == "a5" else 10)
        else:  # s1 / s2 / s3: mejor sector personal acumulado
            arr = self._best_sec.get(drv)
            k = {"s1": 0, "s2": 1, "s3": 2}.get(mode, 0)
            v = float(arr[k]) if arr is not None else float("nan")
        return v if v == v and math.isfinite(v) else float("inf")

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
        self._lap_end_cache.clear()
        self._quali_phase = 0
        self._quali_hdr = None
        self.quali_seps = []
        self.quali_cut_row = None
        self.lap_label.setText("")
        self.flag_label.setVisible(False)
        self.wx_label.setText("")
        self.canvas.update()

    def _update_header(self, t_now: float, leader_lap: int) -> None:
        if self._quali_hdr:
            self.lap_label.setText(self._quali_hdr)
        else:
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
            _t, air, track, _wind, rain = weather[:5]
            self.wx_label.setText(
                f"Air {air:.0f}° · Trk {track:.0f}°" + (" · RAIN" if rain else "")
            )

    # -------------------------------------------- clasificación por tandas

    def _lap_end(self, drv: str, lap: int) -> float | None:
        """Instante de cierre de una vuelta (cacheado: es inmutable)."""
        key = (drv, lap)
        cached = self._lap_end_cache.get(key)
        if cached is not None:
            return cached
        marks = self.analyzer.lap_marks(drv, lap)
        if marks is None:
            return None
        value = float(marks[-1])
        self._lap_end_cache[key] = value
        return value

    def _phase_best(self, drv: str, t0: float,
                    t1: float) -> tuple[int, float] | None:
        """Mejor vuelta CERRADA dentro de la ventana de una tanda (y nunca
        después del timeline: sin spoilers ni con datos por delante)."""
        buf = self.hub.buffers.get(drv)
        if buf is None or not buf.n:
            return None
        t1 = min(t1, self.hub.latest_t + 1.0)
        best: tuple[int, float] | None = None
        for lap in buf.completed_laps():
            end = self._lap_end(drv, lap)
            if end is None or not (t0 <= end < t1):
                continue
            lt = self.analyzer.lap_time(drv, lap)
            if math.isfinite(lt) and (best is None or lt < best[1]):
                best = (lap, lt)
        return best

    def _quali_reset_folds(self, phase_start: float) -> None:
        """Al cruzar de tanda, los mejores (violeta/verde de sectores y µ)
        arrancan de cero: solo cuentan las vueltas de la tanda actual."""
        n_mu = self.analyzer.n_micro()
        self._best_micro.clear()
        self._sess_micro = np.full(n_mu, np.inf)
        self._sess_micro_by = [None] * n_mu
        self._best_sec.clear()
        self._sess_sec = np.full(3, np.inf)
        self._sess_sec_by = [None] * 3
        self._folded.clear()
        for drv, buf in self.hub.buffers.items():
            last_prev = 0
            for lap in (buf.completed_laps() if buf.n else []):
                end = self._lap_end(drv, lap)
                if end is not None and end < phase_start:
                    last_prev = lap
            self._folded[drv] = last_prev

    def _quali_model(self, pts: dict, metric: str | None, phase: int,
                     ws: list[float]) -> dict:
        """Bloques de la torre en clasificación: vivos ordenados por el
        mejor de la tanda ACTUAL, luego eliminados de Q2 y de Q1 (con sus
        tiempos congelados). Un eliminado jamás supera a un vivo. Con una
        métrica elegida, ordena por ella DENTRO de cada bloque."""
        hub = self.hub
        drivers = list(pts)
        n = len(drivers)
        drops = quali_drops(n)  # (eliminados en Q1, en Q2): Q3 = 10 autos
        name = str(hub.session_meta.get("name", "")).lower()
        prefix = "SQ" if ("sprint" in name or "shootout" in name) else "Q"
        pb: dict[int, dict] = {}
        for k in range(1, phase + 1):
            pb[k] = {}
            for drv in drivers:
                b = self._phase_best(drv, ws[k - 1], ws[k])
                if b is not None:
                    pb[k][drv] = b

        def rank_key(k):
            return lambda d: (pb[k].get(d, (0, math.inf))[1], d)

        alive = list(drivers)
        out_phase: dict[str, int] = {}
        out_best: dict[str, tuple | None] = {}
        for k in range(1, phase):  # tandas ya completadas: eliminaciones
            ranked = sorted(alive, key=rank_key(k))
            surv = max(1, len(ranked) - drops[k - 1])
            for d in ranked[surv:]:
                out_phase[d] = k
                out_best[d] = pb[k].get(d)
            alive = ranked[:surv]
        # bloque activo: mejor de la tanda actual (los sin tiempo, abajo,
        # ordenados por su ranking de la tanda anterior)
        if phase > 1:
            prev_order = sorted(alive, key=rank_key(phase - 1))
            prev_rank = {d: i for i, d in enumerate(prev_order)}
        else:
            prev_rank = {d: i for i, d in enumerate(sorted(alive))}
        if metric is None:
            active = sorted(alive, key=lambda d: (
                pb[phase].get(d, (0, math.inf))[1], prev_rank.get(d, 99)))
        else:
            active = sorted(alive, key=lambda d: self._sort_value(d, metric))
        groups = [active]
        seps: list[tuple[int, str]] = []
        for k in range(phase - 1, 0, -1):
            grp = [d for d, p in out_phase.items() if p == k]
            if metric is None:
                grp.sort(key=lambda d: (out_best.get(d) or (0, math.inf))[1])
            else:
                grp.sort(key=lambda d: self._sort_value(d, metric))
            if grp:
                seps.append((sum(len(g) for g in groups),
                             f"ELIMINATED {prefix}{k}"))
                groups.append(grp)
        ordered = [d for g in groups for d in g]
        best_disp = {
            d: (out_best.get(d) if d in out_phase else pb[phase].get(d))
            for d in ordered}
        sb = min(((b[1], b[0], d) for d in active
                  for b in (pb[phase].get(d),) if b is not None),
                 default=None)
        p1_best = pb[phase].get(active[0])[1] if (
            active and active[0] in pb[phase]) else None
        # zona de eliminación (Q1/Q2 en curso, orden por posición)
        cut_row = None
        cut_txts: dict[str, str] = {}
        drop_set: set[str] = set()
        if phase < 3 and metric is None and len(active) > drops[phase - 1]:
            surv_now = len(active) - drops[phase - 1]
            cut_row = surv_now
            ref_b = pb[phase].get(active[surv_now - 1])
            for d in active[surv_now:]:
                drop_set.add(d)
                own = pb[phase].get(d)
                if own is None:
                    cut_txts[d] = "NO TIME"
                elif ref_b is not None:
                    cut_txts[d] = f"CUT +{own[1] - ref_b[1]:.3f}"
        out_tag = {d: f"OUT {prefix}{k}" for d, k in out_phase.items()}
        n_thru = (max(1, len(active) - drops[phase - 1]) if phase < 3
                  else len(active))
        hdr = (f"{prefix}{phase} · top {n_thru} through" if phase < 3
               else f"{prefix}3")
        return dict(phase=phase, ordered=ordered, best=best_disp,
                    out_tag=out_tag, seps=seps, cut_row=cut_row,
                    cut_txts=cut_txts, drop=drop_set, sb=sb,
                    p1_best=p1_best, n_active=len(active), hdr=hdr)

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
        # clasificación (Q1-Q3): detectar la tanda y resetear los mejores
        # acumulados al cruzar de tanda (el reloj rápido arranca de cero)
        quali = None
        qphase, ws = 0, []
        raw_mode = self.sort_combo.currentData() or "position"
        if self.hub.is_quali():
            bounds = self.hub.quali_phase_bounds()
            qphase = min(len(bounds) + 1, 3)
            ws = [0.0] + list(bounds)[:2]
            while len(ws) < 4:
                ws.append(float("inf"))
            if qphase != self._quali_phase:
                self._quali_phase = qphase
                self._quali_reset_folds(ws[qphase - 1])
        for drv in ordered:
            self._fold_bests(drv)  # antes del orden: S1-3 usan los mejores
        if qphase:
            metric = None if raw_mode == "position" else raw_mode
            quali = self._quali_model(pts, metric, qphase, ws)
            ordered = quali["ordered"]
            ready_set = set(ordered)
            active_set = set(ordered)
            self.quali_seps = list(quali["seps"])
            self.quali_cut_row = quali["cut_row"]
            self._quali_hdr = quali["hdr"]
            mode = raw_mode
        else:
            self._quali_hdr = None
            self.quali_seps = []
            self.quali_cut_row = None
            # ordenamiento elegido: "position" = pista en race/sprint y
            # vuelta rápida en el resto; métricas ascendente (mejor arriba)
            mode = raw_mode
            if mode == "position" and not self._race_like():
                mode = "best"
            if mode != "position":
                ordered.sort(key=lambda d: self._sort_value(d, mode))
            # sin ancla de meta ni proyección la posición no es real; los
            # fuera de carrera van últimos con tag RET
            ready_set = {d for d in ordered if an.real_positions_ready(d)}
            active_set = {d for d in ordered if self.hub.is_active(d)}
            ordered = ([d for d in ordered
                        if d in ready_set and d in active_set]
                       + [d for d in ordered
                          if d not in ready_set and d in active_set]
                       + [d for d in ordered if d not in active_set])
        if (quali is None and mode == "position" and not self._order0
                and len(ready_set) == len(ordered)):
            # solo el orden de pista sirve de línea base del Δ posición
            self._order0 = {drv: i + 1 for i, drv in enumerate(ordered)}
        L = self.hub.track_length
        leader = ordered[0]
        pos_leader, t_leader = pts[leader]
        bests = (quali["best"] if quali is not None
                 else {drv: an.best_lap(drv) for drv in ordered})
        # mejor absoluto con dueño (desempate por vuelta y auto: el violeta
        # de vuelta es de UN solo piloto); en quali, el de la tanda actual
        # entre los que siguen vivos
        sb = (quali["sb"] if quali is not None else
              min(((b[1], b[0], drv) for drv, b in bests.items()
                   if b is not None), default=None))
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
        row_gaps: list[float | None] = []
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
            if quali is not None:
                # en clasificación los gaps son deltas de MEJORES tiempos
                # (de la tanda que corresponda a cada bloque), no de pista
                catch = None
                b_own = quali["best"].get(drv)
                gap_txt = int_txt = "—"
                if i == 0:
                    gap_txt, int_txt = "leader", "—"
                elif b_own is not None:
                    if (i < quali["n_active"]
                            and quali["p1_best"] is not None):
                        gap_txt = f"+{b_own[1] - quali['p1_best']:.3f}"
                    starts_group = any(idx == i for idx, _l in quali["seps"])
                    b_above = quali["best"].get(ordered[i - 1])
                    if b_above is not None and not starts_group:
                        int_txt = f"+{b_own[1] - b_above[1]:.3f}"

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
            pit_stop_off = float("nan")
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
            if pit_lap and not pit_open:
                # parada oficial (OpenF1): contraste del s detenido medido
                official = self.hub.official_stop(drv, pit_lap)
                if official is not None:
                    pit_stop_off = float(official[1])

            # Δ posición: contra la grilla oficial si OpenF1 la trajo;
            # si no, contra el primer orden observado en pista (en quali
            # no aplica: la posición es la clasificación por tandas)
            base0 = (None if quali is not None
                     else self.hub.grid.get(drv, self._order0.get(drv)))
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
                grid=self.hub.grid.get(drv),
                pit_stop_off=pit_stop_off,
                out_tag=(quali["out_tag"].get(drv, "")
                         if quali is not None else ""),
                cut_txt=(quali["cut_txts"].get(drv, "")
                         if quali is not None else ""),
                drop=quali is not None and drv in quali["drop"],
                wave=wave,
                wave_now=wave_now,
            ))
            row_gaps.append(gap_val if drv in active_set else None)
        # parada "gratis": el que viene atrás está a más de una Ventana de
        # Box (+1 s de margen) — puede parar sin perder la posición. Solo
        # tiene sentido en carrera (hay vueltas totales o tipo Race)
        is_race = (str(self.hub.session_meta.get("type", "")).lower()
                   == "race" or self.hub.lap_count[1] > 0)
        if is_race and mode == "position":
            # con un orden por métrica los vecinos de fila no son los de
            # pista: el tag FREE no aplica
            window = float((self.cfg or {}).get("strategy", {})
                           .get("pit_window", 20.0)) + 1.0
            for i, row in enumerate(rows):
                if row.retired or row_gaps[i] is None:
                    continue
                if i + 1 >= len(rows):
                    row.free_stop = True  # nadie atrás: no pierde nada
                elif row_gaps[i + 1] is not None:
                    row.free_stop = (row_gaps[i + 1] - row_gaps[i]) > window
        # valores de la referencia para pintar deltas (antes del filtro 👥:
        # la ref puede estar oculta y los deltas siguen valiendo)
        self._ref_vals = next((r for r in rows if r.drv == self.ref_drv), None)
        # el filtro 👥 solo saca filas de la vista: posiciones, gaps e INT se
        # calculan siempre sobre todos los autos (P y gaps reales). Los
        # separadores de tandas se re-indexan a las filas visibles
        if self.filter_btn.hidden:
            hidden = self.filter_btn.hidden

            def _visible_before(idx: int) -> int:
                return sum(1 for r in rows[:idx] if r.drv not in hidden)

            self.quali_seps = [(_visible_before(idx), label)
                               for idx, label in self.quali_seps]
            if self.quali_cut_row is not None:
                self.quali_cut_row = _visible_before(self.quali_cut_row)
            rows = [r for r in rows if r.drv not in hidden]
        self.rows = rows
        self.canvas.setMinimumHeight(len(rows) * self.row_h)
        self.canvas.update()

    def _row_tooltip(self, row: TowerRow) -> str:
        head = f"{row.code} — P{row.pos}"
        if row.grid:
            head += f" · grid P{row.grid}"
        parts = [head,
                 f"Pits: {row.pits}",
                 f"AVG5: {fmt_laptime(row.avg5)} · AVG10: {fmt_laptime(row.avg10)}"]
        if row.tyre:
            parts.insert(1, f"Tyre: {row.tyre.title()} · {row.tyre_age} laps")
        if row.pit_lap:
            lane = (f"{row.pit_lane_s:.1f}s"
                    if row.pit_lane_s == row.pit_lane_s else "—")
            stop = (f"{row.pit_stop_s:.1f}s"
                    if row.pit_stop_s == row.pit_stop_s else "—")
            if row.pit_stop_off == row.pit_stop_off:
                stop += f" (official {row.pit_stop_off:.1f}s)"
            parts.append(
                f"In pit lane NOW — {lane} (stopped {stop})" if row.pit_open
                else f"Last pit: L{row.pit_lap} · lane {lane} · stopped {stop}")
        if row.catch is not None:
            parts.append(f"Catching the car ahead in ~{row.catch:.1f} laps")
        if row.free_stop:
            parts.append("FREE stop: can pit and keep position "
                         "(gap behind > pit window + 1 s)")
        parts.append("Click: compare everyone against this driver "
                     "(click again to clear)")
        photo = self.hub.headshots.get(row.drv)
        if photo:
            # con foto el tooltip pasa a rich text (QToolTip lo detecta)
            import html as _html
            from pathlib import Path

            try:
                uri = Path(photo).as_uri()
            except ValueError:
                uri = ""
            if uri:
                return (f"<img src='{uri}' width='84'><br>"
                        + "<br>".join(_html.escape(p) for p in parts))
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
            # eliminados de quali: fila atenuada con su tiempo congelado
            p.setOpacity(0.55 if row.out_tag else 1.0)
            if i % 2:
                p.fillRect(0, y, width, row_h, QColor(theme.SURFACE_ALT))
            if row.drop:
                # zona de eliminación: hoy queda afuera
                p.fillRect(0, y, width, row_h, QColor(255, 80, 80, 24))
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

            # píldoras LAST y BEST con su letra al estilo A5/A10; con
            # referencia elegida, delta contra ella
            pill_specs = () if not self._col("pills") else (
                    ("L", row.last, row.last_kind, top,
                     ref_vals.last if comparing else float("nan")),
                    ("B", row.best, row.best_kind, bot,
                     ref_vals.best if comparing else float("nan")))
            for letter, value, kind, y_pill, ref_val in pill_specs:
                p.setFont(f_small)
                p.setPen(QColor(theme.TEXT_MUTED))
                p.drawText(QRectF(x, y_pill, 10 * s, line_h),
                           Qt.AlignVCenter | Qt.AlignLeft, letter)
                rect = QRectF(x + 11 * s, y_pill + 1, 66 * s, line_h - 1)
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
                x += 81 * s

            # INT (arriba, con contador de pits) y gap al líder (abajo); en
            # boxes o en la vuelta de salida un tag PIT/OUT reemplaza al
            # INT, y un auto fuera de carrera lleva RET
            show_int = self._col("interval")
            if show_int and (row.retired or row.pit_open or row.pit_out
                             or row.out_tag):
                tag, bg = (("RET", QColor("#b3404a")) if row.retired
                           else ("PIT", QColor("#d6be3c")) if row.pit_open
                           else ("OUT", QColor("#2fbf71")) if row.pit_out
                           else (row.out_tag, QColor("#5a5f6a")))
                rect_tag = QRectF(x, top + 1,
                                  (50 if row.out_tag else 36) * s,
                                  line_h - 1)
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
                elif row.cut_txt:
                    # zona de eliminación: cuánto necesita para salvarse
                    p.setPen(QColor("#ff6b5e"))
                    p.drawText(QRectF(x, bot, 52 * s, line_h),
                               Qt.AlignVCenter | Qt.AlignLeft, row.cut_txt)
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
                elif row.free_stop and not (row.pit_open or row.pit_out):
                    # parada gratis: puede parar sin perder la posición
                    p.setFont(f_small)
                    p.setPen(QColor("#2fbf71"))
                    p.drawText(QRectF(x, bot, 52 * s, line_h),
                               Qt.AlignVCenter | Qt.AlignRight, "FREE")
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
        p.setOpacity(1.0)
        # clasificación: separadores de bloque rotulados y línea de corte
        f_sep = QFont(base)
        f_sep.setPointSizeF(6.0 * s)
        for idx, label in self.quali_seps:
            y = idx * row_h
            p.setPen(QPen(QColor(theme.TEXT_MUTED), 2))
            p.drawLine(0, y, width, y)
            p.setFont(f_sep)
            p.drawText(QRectF(width - 130.0, y + 1, 126.0, 10 * s),
                       Qt.AlignRight | Qt.AlignTop, label)
        if (self.quali_cut_row is not None
                and 0 < self.quali_cut_row < len(self.rows)):
            y = self.quali_cut_row * row_h
            pen = QPen(QColor("#ff6b5e"), 2, Qt.DashLine)
            p.setPen(pen)
            p.drawLine(0, y, width, y)
