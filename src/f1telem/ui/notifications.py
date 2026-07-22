"""Gestor de notificaciones: detecta eventos de la sesión y los anuncia con
popups (toasts arriba de todo, se cierran solos) y un panel con el log y un
interruptor por categoría.

Eventos: entrada/salida de boxes, vuelta rápida de la sesión, auto detenido
en pista (velocidad 0 fuera de boxes), bandera amarilla, safety car, virtual
safety car, bandera roja y sanciones de los comisarios.

Al conectar una fuente, la primera pasada solo toma una línea base del
estado (el replay emite toda la historia de golpe: eso no debe disparar una
catarata de popups); a partir de ahí, solo las transiciones nuevas notifican.
Las claves de identidad deduplican tras los saltos de la línea de tiempo.
"""
from __future__ import annotations

import math
import time

from PySide6.QtCore import QObject, Qt, QTimer
from PySide6.QtGui import QColor, QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QFrame, QGridLayout, QGroupBox,
    QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton,
    QVBoxLayout, QWidget,
)

from .. import config
from ..hub import DataHub
from ..timing import TimingAnalyzer
from . import theme
from .timing_view import fmt_laptime

# (clave, etiqueta del panel, color)
KINDS = [
    ("pit_in", "Pit in", "#4c8dff"),
    ("pit_out", "Pit out", "#4c8dff"),
    ("fast_lap", "Fastest lap", "#c852ff"),
    ("stopped", "Car stopped on track", "#ff6b6b"),
    ("yellow", "Yellow flag", "#ffd12e"),
    ("sc", "Safety car", "#ff9f1a"),
    ("vsc", "Virtual safety car", "#ffd12e"),
    ("red", "Red flag", "#ff4d4d"),
    ("penalty", "Penalties", "#ff6b6b"),
    ("overtake", "Overtakes", "#2fbf71"),
]
_COLOR = {key: color for key, _label, color in KINDS}
_STATUS_EVENTS = {
    "2": ("yellow", "YELLOW FLAG"),
    "4": ("sc", "SAFETY CAR DEPLOYED"),
    "5": ("red", "RED FLAG"),
    "6": ("vsc", "VIRTUAL SAFETY CAR"),
}
_PENALTY_WORDS = ("PENALTY", "REPRIMAND", "DISQUALIF")


class _Toast(QWidget):
    """Popup que se apila abajo a la derecha y se cierra solo."""

    _stack: list["_Toast"] = []

    def __init__(self, color: str, text: str):
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint
                         | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_DeleteOnClose, True)
        self.setObjectName("toast")
        self.setStyleSheet(
            f"QWidget#toast {{ background: {theme.SURFACE_ALT};"
            f" border: 1px solid {theme.BORDER}; }}")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 10, 0)
        bar = QFrame()
        bar.setFixedWidth(4)
        bar.setStyleSheet(f"background: {color};")
        lay.addWidget(bar)
        label = QLabel(text)
        label.setWordWrap(True)
        label.setStyleSheet(f"color: {theme.TEXT}; padding: 8px 2px;")
        lay.addWidget(label, stretch=1)
        self.setFixedWidth(330)
        self.adjustSize()

    @classmethod
    def show_toast(cls, color: str, text: str) -> None:
        try:
            while len(cls._stack) >= 6:  # no invadir la pantalla
                cls._stack[0].close()
            toast = cls(color, text)
            cls._stack.append(toast)
            toast.show()
            cls._reposition()
            QTimer.singleShot(6000, toast.close)
        except Exception:
            pass  # una notificación jamás debe tirar la app

    def closeEvent(self, event) -> None:
        if self in _Toast._stack:
            _Toast._stack.remove(self)
            _Toast._reposition()
        super().closeEvent(event)

    @classmethod
    def _reposition(cls) -> None:
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            return
        geo = screen.availableGeometry()
        y = geo.bottom() - 10
        for toast in reversed(cls._stack):
            y -= toast.height() + 8
            toast.move(geo.right() - toast.width() - 12, y)


class NotificationCenter(QObject):
    def __init__(self, hub: DataHub, cfg: dict, parent=None):
        super().__init__(parent)
        self.hub = hub
        self.cfg = cfg
        self.analyzer = TimingAnalyzer(hub)
        self.log: list[tuple[str, str, str, str]] = []  # (hora, kind, color, texto)
        self._primed = False
        self._seen: set = set()
        self._lane_state: dict[str, tuple[int, bool]] = {}
        self._laps_seen: dict[str, int] = {}
        self._best = math.inf
        self._status = "1"
        self._rcm_n = 0
        self._moving: set[str] = set()
        self._stop_since: dict[str, float] = {}
        self._stop_fired: set[str] = set()
        # sobrepasos: "momentos" para la línea de tiempo
        self.moments: list[dict] = []  # {"t", "lap", "text"}
        self._order_prev: list[str] = []
        self._swaps_pending: dict[tuple[str, str], float] = {}
        self._ov_t = 0.0  # último instante chequeado (detecta saltos)

    def reset(self) -> None:
        self.analyzer.clear()
        self.log = []
        self._primed = False
        self._seen.clear()
        self._lane_state.clear()
        self._laps_seen.clear()
        self._best = math.inf
        self._status = "1"
        self._rcm_n = 0
        self._moving.clear()
        self._stop_since.clear()
        self._stop_fired.clear()
        self.moments = []
        self._order_prev = []
        self._swaps_pending.clear()
        self._ov_t = 0.0

    # ------------------------------------------------------------- emisión

    def _enabled(self, kind: str) -> bool:
        kinds = self.cfg.get("notifications", {}).get("kinds", {})
        return bool(kinds.get(kind, True))

    def popups_on(self) -> bool:
        return bool(self.cfg.get("notifications", {}).get("popups", True))

    def _code(self, drv: str) -> str:
        info = self.hub.drivers.get(drv)
        return info.code if info else drv

    def _emit(self, kind: str, text: str, key=None) -> None:
        if key is not None:
            if key in self._seen:
                return
            self._seen.add(key)
        if not self._enabled(kind):
            return
        self.log.append((time.strftime("%H:%M:%S"), kind, _COLOR[kind], text))
        if len(self.log) > 600:
            del self.log[:200]
        if self.popups_on():
            _Toast.show_toast(_COLOR[kind], text)

    # ------------------------------------------------------------ detección

    def check(self) -> None:
        prime = not self._primed
        self._check_pit(prime)
        self._check_fast_lap(prime)
        self._check_stopped(prime)
        self._check_status(prime)
        self._check_penalties(prime)
        self._check_overtakes(prime)
        self._primed = True

    def _check_pit(self, prime: bool) -> None:
        now = self.hub.latest_t
        for drv, visits in self.hub.pit_lane.items():
            n = len(visits)
            open_now = bool(visits) and visits[-1][2] is None
            prev_n, prev_open = self._lane_state.get(drv, (0, False))
            self._lane_state[drv] = (n, open_now)
            if prime or not visits:
                continue
            code = self._code(drv)
            if n > prev_n:
                for visit in visits[prev_n:]:
                    lap, t_in, t_out = visit
                    # la historia que llega de golpe tras el prime (replay
                    # con toda la sesión, conexión a mitad de tanda) no se
                    # anuncia: solo lo cercano al presente
                    if not (-30.0 <= now - float(t_in) <= 120.0):
                        continue
                    self._emit("pit_in", f"{code} — pit in (L{lap})",
                               key=("pi", drv, round(float(t_in), 1)))
                    if t_out is not None:
                        self._emit_pit_out(drv, code, visit)
            elif prev_open and not open_now:
                self._emit_pit_out(drv, code, visits[-1])

    def _emit_pit_out(self, drv: str, code: str, visit) -> None:
        _lap, t_in, t_out = visit
        lane = max(0.0, float(t_out) - float(t_in))
        stop = self.hub.pit_stationary_time(drv, float(t_in), float(t_out))
        self._emit("pit_out",
                   f"{code} — pit out: {lane:.1f}s in lane · {stop:.1f}s stopped",
                   key=("po", drv, round(float(t_in), 1)))

    def _check_fast_lap(self, prime: bool) -> None:
        for drv, buf in self.hub.buffers.items():
            laps = buf.completed_laps()
            if not laps:
                continue
            last_seen = self._laps_seen.get(drv, 0)
            for lap in laps:
                if lap <= last_seen:
                    continue
                lap_time = self.analyzer.lap_time(drv, lap)
                if lap_time == lap_time and lap_time < self._best:
                    self._best = lap_time
                    if not prime:
                        self._emit(
                            "fast_lap",
                            f"Fastest lap: {self._code(drv)} "
                            f"{fmt_laptime(lap_time)} (L{lap})",
                            key=("fl", drv, lap))
            self._laps_seen[drv] = max(last_seen, laps[-1])

    def _check_stopped(self, prime: bool) -> None:
        for drv, buf in self.hub.buffers.items():
            if not buf.n:
                continue
            speed = float(buf.col("speed")[-1])
            t = float(buf.col("t")[-1])
            if speed > 100.0:
                self._moving.add(drv)  # armado: ya rodó de verdad
            if speed > 30.0:
                self._stop_since.pop(drv, None)
                self._stop_fired.discard(drv)
                continue
            if speed > 0.5:
                continue
            since = self._stop_since.setdefault(drv, t)
            visit = self.hub.last_pit_visit(drv)
            in_pit = visit is not None and self.hub.pit_visit_open(visit)
            if (not prime and drv in self._moving and not in_pit
                    and drv not in self._stop_fired and t - since >= 3.0):
                self._stop_fired.add(drv)
                self._emit("stopped",
                           f"Car stopped on track: {self._code(drv)}",
                           key=("st", drv, round(since, 1)))

    def _check_status(self, prime: bool) -> None:
        code_now, t_start = "1", 0.0
        for t0, t1, code in self.hub.track_status:
            if t0 <= self.hub.latest_t <= t1:
                code_now, t_start = code, t0
                break
        prev, self._status = self._status, code_now
        if prime or code_now == prev:
            return
        event = _STATUS_EVENTS.get(code_now)
        if event is not None:
            kind, text = event
            self._emit(kind, text, key=("ts", code_now, round(t_start, 1)))

    def _pit_involved(self, drv: str, now: float) -> bool:
        """True si el auto está en boxes o acaba de entrar/salir: los
        cambios de orden del ciclo de paradas no son sobrepasos."""
        for _lap, t_in, t_out in self.hub.pit_lane.get(drv, []):
            end = float(t_out) + 25.0 if t_out is not None else float("inf")
            if float(t_in) - 5.0 <= now <= end:
                return True
        return False

    def _check_overtakes(self, prime: bool) -> None:
        """Cambios de orden EN PISTA (fuera de boxes) sostenidos dos chequeos
        seguidos: notificación + "momento" clickeable en la línea de tiempo."""
        hub = self.hub
        if hub.lap_count[1] <= 0 and "race" not in str(
                hub.session_meta.get("type", "")).lower():
            return  # la posición de pista solo ordena en carrera
        pts = {}
        for drv in hub.buffers:
            if not hub.is_active(drv):
                continue
            pt = self.analyzer.position_time(drv)
            if (pt is not None and len(pt[0]) >= 2
                    and self.analyzer.real_positions_ready(drv)):
                pts[drv] = float(pt[0][-1])
        order = sorted(pts, key=lambda d: pts[d], reverse=True)
        prev, self._order_prev = self._order_prev, order
        now_t = hub.latest_t
        jumped = abs(now_t - self._ov_t) > 30.0  # salto de timeline
        self._ov_t = now_t
        if prime or not prev or jumped:
            # tras un seek el orden "anterior" es de otro instante: los
            # cruces aparentes serían sobrepasos fantasma
            self._swaps_pending.clear()
            return
        prev_idx = {d: k for k, d in enumerate(prev)}
        now_idx = {d: k for k, d in enumerate(order)}
        now = hub.latest_t
        swapped: set[tuple[str, str]] = set()
        for winner, wi in now_idx.items():
            pw = prev_idx.get(winner)
            if pw is None:
                continue
            for loser, li in now_idx.items():
                pl = prev_idx.get(loser)
                if pl is None or not (li > wi and pl < pw):
                    continue  # no se cruzaron entre chequeos
                if (self._pit_involved(winner, now)
                        or self._pit_involved(loser, now)):
                    continue
                swapped.add((winner, loser))
        # confirmación diferida: un cruce cuenta si el orden nuevo SIGUE en
        # pie un chequeo después (el ruido de posición entre autos casi
        # empatados se revierte solo y no debe disparar sobrepasos fantasma)
        for (winner, loser), t0 in list(self._swaps_pending.items()):
            del self._swaps_pending[(winner, loser)]
            wi, li = now_idx.get(winner), now_idx.get(loser)
            if wi is None or li is None or wi >= li:
                continue  # se revirtió: era ruido
            buf = hub.buffers.get(winner)
            lap = buf.current_lap() if buf is not None and buf.n else 0
            text = (f"{self._code(winner)} overtakes "
                    f"{self._code(loser)} for P{wi + 1} (L{lap})")
            key = ("ov", winner, loser, lap)
            if key not in self._seen:
                self.moments.append(
                    {"t": float(t0), "lap": lap, "text": f"L{lap}: {text}"})
                if len(self.moments) > 500:
                    del self.moments[:100]
            self._emit("overtake", text, key=key)
        for pair in swapped:
            self._swaps_pending.setdefault(pair, now)

    def _check_penalties(self, prime: bool) -> None:
        rows = self.hub.race_control
        start = 0 if len(rows) < self._rcm_n else self._rcm_n
        for msg in rows[start:]:
            text = str(msg.get("message", ""))
            upper = text.upper()
            if not prime and any(word in upper for word in _PENALTY_WORDS):
                self._emit("penalty", text,
                           key=("pen", upper[:80], msg.get("lap")))
        self._rcm_n = len(rows)


class NotificationSettingsDialog(QDialog):
    """Configuración de notificaciones: popups sí/no y qué categorías
    anunciar. Los cambios se aplican y persisten al instante."""

    def __init__(self, cfg: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Notification settings")
        self.cfg = cfg
        ncfg = cfg.setdefault("notifications", {})
        kinds_cfg = ncfg.setdefault("kinds", {})

        lay = QVBoxLayout(self)
        self.popups_check = QCheckBox("Show popup toasts (bottom-right)")
        self.popups_check.setChecked(bool(ncfg.get("popups", True)))
        self.popups_check.toggled.connect(self._popups_toggled)
        lay.addWidget(self.popups_check)
        box = QGroupBox("Notify about")
        grid = QGridLayout(box)
        grid.setVerticalSpacing(4)
        self.kind_checks: dict[str, QCheckBox] = {}
        for i, (key, label, color) in enumerate(KINDS):
            check = QCheckBox(label)
            check.setStyleSheet(f"color: {color};")
            check.setChecked(bool(kinds_cfg.get(key, True)))
            check.toggled.connect(lambda on, k=key: self._kind_toggled(k, on))
            self.kind_checks[key] = check
            grid.addWidget(check, i // 2, i % 2)
        lay.addWidget(box)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        buttons.clicked.connect(lambda _b: self.accept())
        lay.addWidget(buttons)

    def _popups_toggled(self, on: bool) -> None:
        self.cfg.setdefault("notifications", {})["popups"] = on
        config.save_config(self.cfg)

    def _kind_toggled(self, kind: str, on: bool) -> None:
        kinds = self.cfg.setdefault("notifications", {}).setdefault("kinds", {})
        kinds[kind] = on
        config.save_config(self.cfg)


class NotificationsPanel(QWidget):
    """Log de notificaciones, con acceso a la configuración."""

    def __init__(self, center: NotificationCenter, cfg: dict, parent=None):
        super().__init__(parent)
        self.center = center
        self.cfg = cfg
        self._count = 0

        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 2, 4, 4)
        lay.setSpacing(2)
        row = QHBoxLayout()
        self.configure_btn = QPushButton("Configure…")
        self.configure_btn.setToolTip(
            "Choose which events to announce and whether popups show")
        self.configure_btn.clicked.connect(self._configure)
        row.addWidget(self.configure_btn)
        row.addStretch(1)
        lay.addLayout(row)
        self.list = QListWidget()
        self.list.setWordWrap(True)
        self.list.setStyleSheet("QListWidget { font-size: 8pt; }")
        lay.addWidget(self.list, stretch=1)

    def _configure(self) -> None:
        NotificationSettingsDialog(self.cfg, self).exec()

    def clear_data(self) -> None:
        self.list.clear()
        self._count = 0

    def refresh(self) -> None:
        log = self.center.log
        if len(log) < self._count:
            self.clear_data()
        if len(log) == self._count:
            return
        for stamp, _kind, color, text in log[self._count:]:
            item = QListWidgetItem(f"[{stamp}] {text}")
            item.setForeground(QColor(color))
            self.list.addItem(item)
        self._count = len(log)
        self.list.scrollToBottom()
