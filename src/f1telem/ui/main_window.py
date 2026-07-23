"""Ventana principal: fuentes, selección de pilotos, modos y estado."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from PySide6.QtCore import (
    QEvent, Qt, QRect, QThread, QTimer, QPointF, QRectF, Signal,
)
from PySide6.QtGui import QColor, QCursor, QPainter, QPixmap, QPolygonF, QIcon
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFormLayout, QGridLayout, QGroupBox,
    QHBoxLayout, QInputDialog, QLabel, QListWidget, QListWidgetItem,
    QMainWindow, QMenu, QMessageBox, QPushButton, QScrollArea, QSizePolicy,
    QSlider, QSpinBox, QToolButton, QToolTip, QVBoxLayout, QWidget,
    QWidgetAction,
)

from .. import __version__, config
from ..analysis import AnalysisEngine
from ..hub import DataHub
from ..models import CHANNELS, CHANNEL_ORDER
from ..openf1 import OpenF1Client
from ..sources import BaseSource, CaptureSource, DemoSource, LiveSource, ReplaySource
from . import theme
from .analysis_panels import (
    ANALYSIS_SECTIONS, AccelPanel, AnalysisLauncher, DeployCoastPanel,
    EnergyBalancePanel, GForcePanel, GGDiagramPanel, GripDegPanel,
)
from .charts import RollingChart, WrapChart
from .data_table import DataTableView
from .docks import Detachable
from .dominance_map import DominanceMapView
from .driver_filter import DriverSelectButton
from .lap_compare import LapCompareView
from .lap_wheel import LapWheelView
from .micro_config import MicroConfigView
from .notifications import (
    NotificationCenter, NotificationSettingsDialog, NotificationsPanel,
)
from .pit_strategy import PitStrategyView
from .pitlane import PitlaneView
from .pitlane_map import PitlaneMapView
from .qualy_view import QualyView
from .race_control import RaceControlPanel
from .session_strip import SessionStrip
from .strategy import StrategyView
from .timing_view import TimingView
from .tower import TimingTower
from .trace_chart import TraceChart
from .track_map import TrackMapView
from .tyre_stints import TyreStintsView
from .update_dialog import run_check
from .weather import WeatherChart, WeatherNowPanel

# ventana X en vueltas para las vistas con ventana móvil (0 = toda la sesión)
X_WINDOWS = [
    ("½ lap", 0.5),
    ("1 lap", 1.0),
    ("2 laps", 2.0),
    ("3 laps", 3.0),
    ("5 laps", 5.0),
    ("10 laps", 10.0),
    ("20 laps", 20.0),
    ("All", 0.0),
]
SPEEDS = [1.0, 2.0, 5.0, 10.0, 25.0]
SESSIONS = ["R", "Q", "SQ", "S", "FP1", "FP2", "FP3"]


class ScheduleLoader(QThread):
    """Carga el calendario del año con FastF1 en segundo plano."""

    loaded = Signal(int, list)  # año, [(ronda, nombre del evento)]

    def __init__(self, year: int, parent=None):
        super().__init__(parent)
        self.year = year

    def run(self) -> None:
        try:
            import fastf1

            cache = config.cache_dir()
            cache.mkdir(parents=True, exist_ok=True)
            fastf1.Cache.enable_cache(str(cache))
            schedule = fastf1.get_event_schedule(self.year, include_testing=False)
            events = [
                (int(row["RoundNumber"]), str(row["EventName"]))
                for _, row in schedule.iterrows()
            ]
            self.loaded.emit(self.year, events)
        except Exception:
            self.loaded.emit(self.year, [])  # sin red se puede tipear igual


class LapRuler(QWidget):
    """Regla sobre la línea de tiempo: una marca por vuelta, numeradas de
    forma adaptativa; clic en una marca salta al inicio de esa vuelta."""

    def __init__(self, seek_callback, parent=None):
        super().__init__(parent)
        self._marks: list[tuple[int, float]] = []
        self._pits: list[tuple[float, str]] = []      # (t, color)
        self._status: list[tuple[float, float, str]] = []
        self._rain: list[tuple[float, float]] = []
        self._moments: list[tuple[float, str]] = []   # (t, texto) sobrepasos
        # capas visibles (▦ de la línea de tiempo): ausente = visible
        self._layers: dict[str, bool] = {}
        self._t0 = 0.0
        self._t1 = 1.0
        self._seek = seek_callback
        self.setFixedHeight(18)
        self.setCursor(Qt.PointingHandCursor)
        self.setMouseTracking(True)  # preview de vuelta/estado al pasar

    def set_marks(self, marks) -> None:
        self._marks = list(marks)
        self.update()

    def set_pits(self, pits) -> None:
        self._pits = list(pits)
        self.update()

    def set_status(self, periods) -> None:
        self._status = list(periods)
        self.update()

    def set_rain(self, periods) -> None:
        self._rain = list(periods)
        self.update()

    def set_moments(self, moments) -> None:
        """Momentos de la carrera (sobrepasos detectados): triángulo
        clickeable; solo lo ya visto, nunca el futuro."""
        self._moments = list(moments)
        self.update()

    def set_layer(self, key: str, on: bool) -> None:
        """Muestra/oculta un tipo de referencia (laps, pits, flags, rain,
        overtakes); también apaga su hover y su click."""
        self._layers[key] = bool(on)
        self.update()

    def _on(self, key: str) -> bool:
        return self._layers.get(key, True)

    def set_range(self, t0: float, t1: float) -> None:
        if (t0, t1) != (self._t0, self._t1):
            self._t0, self._t1 = t0, t1
            self.update()

    def _x_of(self, t: float) -> float:
        span = max(self._t1 - self._t0, 1e-9)
        return (t - self._t0) / span * self.width()

    def paintEvent(self, event) -> None:
        if not (self._marks or self._status or self._pits or self._rain):
            return
        painter = QPainter(self)
        font = painter.font()
        font.setPointSizeF(7.0)
        painter.setFont(font)
        # franja fina de lluvia (azul), arriba de las bandas de bandera
        for t0, t1 in (self._rain if self._on("rain") else ()):
            x0 = self._x_of(t0)
            x1 = self._x_of(min(t1, self._t1))
            painter.fillRect(QRectF(x0, 8, max(x1 - x0, 2.0), 2.5), QColor(0, 130, 220, 170))
        # bandas de bandera/SC de fondo
        for t0, t1, code in (self._status if self._on("flags") else ()):
            style = theme.TRACK_STATUS.get(code)
            if style is None:
                continue
            x0 = self._x_of(t0)
            x1 = self._x_of(min(t1, self._t1))
            color = QColor(style[1])
            color.setAlpha(90)
            painter.fillRect(QRectF(x0, 10, max(x1 - x0, 2.0), 8), color)
        # numerar cada k vueltas para que las etiquetas no se pisen
        avg_px = self.width() / max(len(self._marks), 1)
        step = 1
        while avg_px * step < 26.0:
            step += 1
        for lap, t in (self._marks if self._on("laps") else ()):
            x = self._x_of(t)
            if x < 0 or x > self.width():
                continue
            painter.setPen(QColor(theme.BORDER))
            painter.drawLine(int(x), 10, int(x), 18)
            if (lap - 1) % step == 0:
                painter.setPen(QColor(theme.TEXT_MUTED))
                painter.drawText(QRectF(x - 16, 0, 32, 10), Qt.AlignCenter, f"L{lap}")
        # rombos de paradas en boxes (color del piloto)
        painter.setPen(Qt.NoPen)
        for t, color in (self._pits if self._on("pits") else ()):
            x = self._x_of(t)
            if x < 0 or x > self.width():
                continue
            painter.setBrush(QColor(color))
            painter.drawPolygon(QPolygonF([
                QPointF(x, 10.5), QPointF(x + 3, 14), QPointF(x, 17.5), QPointF(x - 3, 14),
            ]))
        # triángulos de sobrepasos ("momentos" de la carrera)
        painter.setBrush(QColor("#2fbf71"))
        for t, _label in (self._moments if self._on("overtakes") else ()):
            x = self._x_of(t)
            if x < 0 or x > self.width():
                continue
            painter.drawPolygon(QPolygonF([
                QPointF(x - 3.2, 8.0), QPointF(x + 3.2, 8.0), QPointF(x, 2.0),
            ]))
        painter.end()

    def _t_at(self, x: float) -> float:
        return self._t0 + x / max(self.width(), 1) * (self._t1 - self._t0)

    def _status_at(self, t: float):
        if not self._on("flags"):
            return None
        for t0, t1, code in self._status:
            if t0 <= t <= t1 and code in theme.TRACK_STATUS:
                return t0, theme.TRACK_STATUS[code][0]
        return None

    def hint_at(self, t: float) -> str:
        """Texto de preview: vuelta, tiempo de sesión y estado de pista."""
        lap_txt = ""
        laps = [(lap, mt) for lap, mt in self._marks if mt <= t]
        if laps:
            lap_txt = f"L{laps[-1][0]} · "
        rel = max(0.0, t - self._t0)
        hint = f"{lap_txt}{int(rel // 60)}:{int(rel % 60):02d}"
        moment = self._moment_near(t)
        if moment is not None:
            hint += f" · {moment[1]}"
        status = self._status_at(t)
        if status is not None:
            hint += f" · {status[1]}"
        return hint

    def _moment_near(self, t: float) -> tuple[float, str] | None:
        if not self._on("overtakes"):
            return None
        span = max(self._t1 - self._t0, 1e-9)
        close = [(abs(mt - t), mt, txt) for mt, txt in self._moments
                 if abs(mt - t) <= span * 0.012]
        if not close:
            return None
        _d, mt, txt = min(close)
        return mt, txt

    def _target_for_click(self, t_click: float) -> float | None:
        """Un momento (sobrepaso) cercano manda: salta unos segundos antes;
        dentro de una banda de bandera/SC se salta al INICIO del incidente;
        si no, al inicio de la vuelta más cercana."""
        moment = self._moment_near(t_click)
        if moment is not None:
            return max(self._t0, moment[0] - 5.0)
        status = self._status_at(t_click)
        if status is not None:
            return max(status[0], self._t0)
        if not self._marks:
            return None
        _lap, t = min(self._marks, key=lambda m: abs(m[1] - t_click))
        return t

    def mouseMoveEvent(self, event) -> None:
        if self._marks or self._status:
            t = self._t_at(event.position().x())
            QToolTip.showText(event.globalPosition().toPoint(),
                              self.hint_at(t), self)
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event) -> None:
        target = self._target_for_click(self._t_at(event.position().x()))
        if target is not None:
            self._seek(target)


class _ViewHost(QWidget):
    """Vista + su propia barra de controles: en el modelo todo-ventanas cada
    gráfico lleva canal / ventana X propios (dos ventanas Race pueden mostrar
    canales distintos)."""

    def __init__(self, view: QWidget, controls: list, right: QWidget | None = None,
                 parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        row = QHBoxLayout()
        row.setContentsMargins(4, 2, 4, 0)
        for label, widget in controls:
            row.addWidget(QLabel(label + ":"))
            row.addWidget(widget)
            row.addSpacing(10)
        row.addStretch(1)
        if right is not None:  # p.ej. el selector 👥 de autos del gráfico
            row.addWidget(right)
        lay.addLayout(row)
        lay.addWidget(view, stretch=1)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("BoxBox-F1 — Control hub")
        self.resize(400, 900)

        self.cfg = config.load_config()
        # perfiles de fábrica en la primera corrida (solo visibilidad: las
        # ventanas se abren en cascada y el usuario las acomoda y re-guarda)
        if not self.cfg.get("layouts"):
            base = {"tower": True, "map": True, "session": True}
            self.cfg["layouts"] = {
                "Race": {"visible": {**base, "race_chart": True,
                                     "race_trace": True}, "float": {}},
                "Quali": {"visible": {**base, "quali_view": True,
                                      "times_gap": True}, "float": {}},
                "Strategy": {"visible": {**base, "race_trace": True,
                                         "strategy": True, "pitlane": True,
                                         "notifications": True}, "float": {}},
            }
        self.hub = DataHub(self)
        # enriquecimiento OpenF1 (histórico gratuito): grilla oficial,
        # paradas oficiales y fotos de pilotos
        self.openf1 = OpenF1Client(self)
        self.openf1.gridReady.connect(self.hub.on_grid)
        self.openf1.officialPitsReady.connect(self.hub.on_official_pits)
        self.openf1.headshotsReady.connect(self.hub.on_headshots)
        self.source: BaseSource | None = None
        self._progress: tuple[float, float, float] | None = None
        self._source_status = "Not connected."

        self.chart_rolling = RollingChart(self.hub)
        self.chart_wrap = WrapChart(self.hub)
        self.chart_qualy = QualyView(self.hub)
        self.chart_timing = TimingView(self.hub, self.cfg)
        self.chart_trace = TraceChart(self.hub, self.cfg)
        self.charts = [self.chart_rolling, self.chart_wrap, self.chart_qualy,
                       self.chart_timing, self.chart_trace]
        # selector local 👥 por gráfico: nace del panel Drivers (cada cambio
        # ahí lo vuelve a pisar) y se retoca por ventana sin afectar al resto
        self._panel_sel: list[str] = []
        self._chart_sel_btns: list[DriverSelectButton] = []
        for chart in self.charts:
            btn = DriverSelectButton(self.hub)
            btn.changed.connect(
                lambda c=chart, b=btn: c.set_selected(b.selection()))
            self._chart_sel_btns.append(btn)
        self.chart_trace.add_control(self._chart_sel_btns[4])
        self.track_map = TrackMapView(self.hub, self.cfg)
        self.dominance_view = DominanceMapView(self.hub, self.cfg)
        self.lap_wheel = LapWheelView(self.hub, self.cfg)
        self.tower = TimingTower(self.hub, self.cfg)
        self.session_strip = SessionStrip(self.hub)
        self.race_control_view = RaceControlPanel(self.hub)
        self.strategy_view = StrategyView(self.hub)
        self.tyre_stints_view = TyreStintsView(self.hub)
        self.micro_config_view = MicroConfigView(self.hub, self.cfg)
        self._micro_cfg_key: str | None = None
        self.pitlane_view = PitlaneView(self.hub, self.cfg)
        self.pitlane_map_view = PitlaneMapView(self.hub, self.cfg)
        self.pit_strategy_view = PitStrategyView(self.hub, self.cfg)
        self.notifier = NotificationCenter(self.hub, self.cfg, self)
        self.notifications_view = NotificationsPanel(self.notifier, self.cfg)
        self.weather_now = WeatherNowPanel(self.hub)
        self.weather_chart = WeatherChart(self.hub)
        # sección Analysis: motor de canales derivados + paneles (energía y
        # dinámica), lanzados desde su propia ventana-hub
        self.analysis_engine = AnalysisEngine(self.hub)
        self.analysis_views = {
            "an_deploy": DeployCoastPanel(self.hub, self.analysis_engine),
            "an_battery": EnergyBalancePanel(self.hub, self.analysis_engine),
            "an_gg": GGDiagramPanel(self.hub, self.analysis_engine),
            "an_gforce": GForcePanel(self.hub, self.analysis_engine),
            "an_accel": AccelPanel(self.hub, self.analysis_engine),
            "an_grip": GripDegPanel(self.hub, self.analysis_engine),
        }
        self.analysis_launcher = AnalysisLauncher(self._catalog_toggled)
        self.lap_compare_view = LapCompareView(self.hub, self.cfg)
        # las tablas de timing en su propia ventana (adopta las pestañas
        # clásicas de Times/Gap y suma la vista piloto×vuelta)
        self.data_table_view = DataTableView(self.hub, self.cfg,
                                             self.chart_timing)
        self._tick_n = 0
        self._moments_n = 0
        # espera de la fuente Capture: sondear hasta que el capturador
        # empiece a escribir datos y conectar solo (definido antes de la UI:
        # _source_kind_changed consulta _cap_waiting durante el cableado)
        self._cap_timer = QTimer(self)
        self._cap_timer.setInterval(700)
        self._cap_timer.timeout.connect(self._poll_capture_ready)
        self._cap_waiting = False
        self._cap_baseline: dict = {}
        # watchdog conectado: si el capturador empieza a escribir OTRO
        # archivo (p. ej. arranca una importación) y el actual queda quieto,
        # el visualizador lo sigue solo — sin esto, la primera apertura
        # automática podía quedar mirando la captura en vivo vacía
        self._cap_follow_timer = QTimer(self)
        self._cap_follow_timer.setInterval(2000)
        self._cap_follow_timer.timeout.connect(self._poll_capture_follow)
        self._follow_cand: tuple | None = None

        self._build_ui()
        self._wire()

        self._timer = QTimer(self)
        self._timer.setInterval(33)  # 30 fps: el paneo suavizado lo necesita
        self._timer.timeout.connect(self._tick)
        self._timer.start()

        if (bool(self.cfg.get("updates", {}).get("check_on_startup", True))
                and not os.environ.get("F1TELEM_NO_UPDATE_CHECK")):
            QTimer.singleShot(
                3000, lambda: run_check(self, self.cfg, silent=True)
            )

        # restaurar la geometría guardada del hub; sin estado previo, un
        # alto por defecto ACOTADO a la pantalla (el contenido escrolea)
        ui = self.cfg.get("ui", {})
        geom = ui.get("win_geom")
        if isinstance(geom, list) and len(geom) == 4:
            self.setGeometry(*[int(v) for v in geom])
        else:
            screen = self.screen()
            avail_h = (screen.availableGeometry().height()
                       if screen is not None else 800)
            self.resize(400, min(820, avail_h - 60))

    # ------------------------------------------------------------------- UI

    # Catálogo de ventanas: título + descripción visibles en el hub, para
    # que el usuario sepa exactamente qué es cada una. El hub solo gestiona
    # datos, perfiles y ajustes: TODAS las vistas viven en su propia ventana.
    PANEL_CATALOG = [
        ("race_chart", "Race chart",
         "Rolling telemetry vs track position (same corner = same vertical)"),
        ("race2_chart", "Race 2",
         "Fixed 0→L lap axis; each car overwrites its previous lap"),
        ("quali_view", "Lap Compare - Live",
         "Current laps vs a target lap: traces, cumulative delta and cards"),
        ("lap_compare", "Lap Compare",
         "Pick driver→lap sets from laps already completed and chart any "
         "channel; one set is the target for the delta"),
        ("times_gap", "Times / Gap",
         "Gap chart vs a reference driver"),
        ("data_table", "Data tables",
         "Every timing table in one place — per driver AND per lap: time, "
         "the 3 sectors, tyre compound/age, AVG5/AVG10 — plus the classic "
         "Summary, By lap, Microsectors, Corners and Degradation tabs; "
         "pick drivers and a lap range"),
        ("race_trace", "Race trace",
         "Gap evolution per microsector vs a selectable reference"),
        ("tower", "Timing tower",
         "Broadcast-style tower: positions, gaps, tyres, sectors "
         "(👥 picks the cars shown)"),
        ("map", "Track map",
         "Live car positions and trails on the circuit outline "
         "(👥 picks the cars shown)"),
        ("lap_wheel", "Lap wheel",
         "Circular lap view: cars by lap position (north = start line), "
         "sectors, corners and the pit-drop ghost (👥 picks the cars shown)"),
        ("dominance", "Track dominance",
         "Each µsector painted in the fastest driver's colour — pick the "
         "drivers to compare (👥) and the lap range"),
        ("session", "Session status",
         "Flag, lap counter, session clock and latest race control message"),
        ("drivers", "Drivers",
         "Driver selection: which cars are compared in the charts (the map, "
         "lap wheel and tower pick their own cars with 👥)"),
        ("timeline", "Timeline",
         "Race progress: scrubber, lap ruler, pause and LIVE (replay/capture)"),
        ("strategy", "Tyre strategy",
         "Stint bars per driver, colored by compound"),
        ("tyre_stints", "Tyre stints",
         "One chip per stint: compound, laps and a green N for a fresh set"),
        ("micro_config", "Microsectors Editor",
         "Move/add/remove the µ cuts on the map and table; saved per "
         "circuit and year (sector boundaries stay official)"),
        ("pitlane", "Pit lane",
         "Who is in the pits right now, entry compound and live clocks"),
        ("pitlane_map", "Pit lane map",
         "The whole pit lane left→right with its two lanes: cars roll "
         "down the fast lane, drop to the inner lane while stationary "
         "(mechanics on all four wheels) and rejoin the track"),
        ("pit_strategy", "Pit strategy",
         "Pit window loss (Ventana de Box) and rejoin projections"),
        ("race_control", "Race control",
         "Chronological log of official messages"),
        ("weather", "Weather",
         "Current air/track temperature, wind and rain"),
        ("weather_chart", "Weather evolution",
         "Temperatures and wind over the session (X = leader lap)"),
        ("notifications", "Notifications",
         "Event popups and log: pits, fastest lap, flags, penalties"),
        ("analysis", "Analysis",
         "Analysis toolbox: energy (clipping, lift & coast, battery) and "
         "dynamics (G forces, friction circle, grip) — each tool opens in "
         "its own window"),
    ]
    _CHART_IDS = ("race_chart", "race2_chart", "quali_view", "times_gap",
                  "race_trace")
    # botones destacados del catálogo (fila propia arriba, con acento)
    _FEATURED = ("drivers", "timeline")
    _FEATURED_LOOK = {"drivers": "👥", "timeline": "⏱"}
    _FEATURED_COLORS = {"drivers": "#2fbf71", "timeline": theme.ACCENT}
    # secciones de "Windows": lo afín en contenido/función queda junto
    PANEL_GROUPS = [
        ("Comparison charts",
         ("race_chart", "race2_chart", "quali_view", "lap_compare",
          "times_gap", "race_trace")),
        ("Track view",
         ("map", "lap_wheel", "dominance", "micro_config")),
        ("Timing & race",
         ("tower", "data_table", "session", "race_control",
          "notifications")),
        ("Pits & strategy",
         ("strategy", "tyre_stints", "pitlane", "pitlane_map",
          "pit_strategy")),
        ("Conditions & analysis",
         ("weather", "weather_chart", "analysis")),
    ]
    # arranque limpio: solo el hub (fuente + pilotos + catálogo); las
    # ventanas se abren a demanda o aplicando un perfil
    _DEFAULT_OPEN = frozenset()

    def _build_ui(self) -> None:
        root = QWidget()
        column = QVBoxLayout(root)
        column.setContentsMargins(8, 8, 8, 8)
        column.setSpacing(8)

        # --- fuente de datos + línea de tiempo ---
        src_box = QGroupBox("Data source")
        src_lay = QVBoxLayout(src_box)
        self.source_combo = QComboBox()
        # el visualizador consume histórico o captura; el vivo lo maneja el
        # capturador (exe propio). Demo y Live directo quedan como fuentes de
        # desarrollo detrás de F1TELEM_DEV_SOURCES=1.
        self.source_combo.addItem("Replay (FastF1 historical)", "replay")
        self.source_combo.addItem("Capture (live / imported)", "capture")
        if os.environ.get("F1TELEM_DEV_SOURCES"):
            self.source_combo.addItem("Live (F1 Live Timing)", "live")
            self.source_combo.addItem("Demo (synthetic)", "demo")
        src_lay.addWidget(self.source_combo)

        rep = self.cfg["replay"]
        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        self.year_spin = QSpinBox()
        self.year_spin.setRange(2018, 2030)
        self.year_spin.setValue(int(rep.get("year", 2025)))
        self.gp_combo = QComboBox()
        self.gp_combo.setEditable(True)
        self.gp_combo.setInsertPolicy(QComboBox.NoInsert)
        # los nombres largos de GP no deben imponer el ancho del hub
        self.gp_combo.setSizeAdjustPolicy(
            QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.gp_combo.setMinimumContentsLength(10)
        self.gp_combo.setEditText(str(rep.get("gp", "Bahrain")))
        self.gp_combo.lineEdit().setPlaceholderText("Loading calendar…")
        self.session_combo = QComboBox(); self.session_combo.addItems(SESSIONS)
        self.session_combo.setCurrentText(str(rep.get("session", "R")))
        self.speed_combo = QComboBox()
        for s in SPEEDS:
            self.speed_combo.addItem(f"x{s:g}", s)
        idx = SPEEDS.index(rep.get("speed", 5.0)) if rep.get("speed", 5.0) in SPEEDS else 2
        self.speed_combo.setCurrentIndex(idx)
        form.addRow("Year", self.year_spin)
        form.addRow("GP", self.gp_combo)
        form.addRow("Session", self.session_combo)
        form.addRow("Speed", self.speed_combo)
        self._replay_form = form
        src_lay.addLayout(form)

        self.connect_btn = QPushButton("Connect")
        src_lay.addWidget(self.connect_btn)
        # línea de tiempo del replay/captura (solo visible cuando aplica):
        # fila de controles arriba (pausa, LIVE, saltos ±, reloj, capas) y
        # la barra (regla + slider) abajo A TODO EL ANCHO del panel
        self.seek_row = QWidget()
        seek_col = QVBoxLayout(self.seek_row)
        seek_col.setContentsMargins(0, 0, 0, 0)
        seek_col.setSpacing(2)
        ctl = QHBoxLayout()
        ctl.setSpacing(2)
        self.pause_btn = QPushButton("⏸")
        self.pause_btn.setCheckable(True)
        self.pause_btn.setFixedWidth(30)
        self.pause_btn.setToolTip("Pause / resume playback")
        ctl.addWidget(self.pause_btn)
        self.live_btn = QPushButton("LIVE")
        self.live_btn.setFixedWidth(48)
        self.live_btn.setToolTip("Jump to the latest captured data")
        self.live_btn.setVisible(False)
        ctl.addWidget(self.live_btn)
        ctl.addStretch(1)
        # saltos relativos agrupados y centrados sobre la barra
        self.seek_back_btns: list[QToolButton] = []
        self.seek_fwd_btns: list[QToolButton] = []
        for secs, label in ((-900, "−15m"), (-300, "−5m"), (-60, "−1m"),
                            (-30, "−30s"), (-10, "−10s"), (-5, "−5s")):
            btn = QToolButton()
            btn.setText(label)
            btn.setAutoRaise(True)
            btn.setFixedWidth(36)
            btn.setStyleSheet("font-size: 7pt;")
            btn.setToolTip(f"Jump {label}")
            btn.clicked.connect(lambda _=False, s=secs: self._seek_relative(s))
            ctl.addWidget(btn)
            self.seek_back_btns.append(btn)
        ctl.addSpacing(12)
        for secs, label in ((5, "+5s"), (10, "+10s"), (30, "+30s"),
                            (60, "+1m"), (300, "+5m"), (900, "+15m")):
            btn = QToolButton()
            btn.setText(label)
            btn.setAutoRaise(True)
            btn.setFixedWidth(36)
            btn.setStyleSheet("font-size: 7pt;")
            btn.setToolTip(f"Jump {label}")
            btn.clicked.connect(lambda _=False, s=secs: self._seek_relative(s))
            ctl.addWidget(btn)
            self.seek_fwd_btns.append(btn)
        ctl.addStretch(1)
        self.seek_slider = QSlider(Qt.Horizontal)
        self.seek_slider.setRange(0, 1000)
        self.lap_ruler = LapRuler(self._seek_to_time)
        mid = QVBoxLayout()
        mid.setSpacing(0)
        mid.addWidget(self.lap_ruler)
        mid.addWidget(self.seek_slider)
        self.time_label = QLabel("0:00 / 0:00")
        ctl.addWidget(self.time_label)
        # capas de referencias de la línea de tiempo (persistidas)
        self.timeline_layers_btn = QToolButton()
        self.timeline_layers_btn.setText("▦")
        self.timeline_layers_btn.setAutoRaise(True)
        self.timeline_layers_btn.setToolTip(
            "Choose which reference marks the timeline shows")
        self.timeline_layers_btn.setPopupMode(QToolButton.InstantPopup)
        self.timeline_layers_btn.setStyleSheet(
            "QToolButton::menu-indicator { image: none; }")
        tl_menu = QMenu(self)
        tl_box = QWidget()
        tl_lay = QVBoxLayout(tl_box)
        tl_lay.setContentsMargins(6, 4, 6, 4)
        tl_lay.setSpacing(2)
        tl_stored = self.cfg.get("ui", {}).get("timeline_layers", {})
        for key, label in (("laps", "Lap marks"), ("pits", "Pit stops"),
                           ("flags", "Flags / SC bands"), ("rain", "Rain"),
                           ("overtakes", "Overtakes")):
            chk = QCheckBox(label)
            on = tl_stored.get(key, True) is not False
            chk.setChecked(on)
            self.lap_ruler.set_layer(key, on)
            chk.toggled.connect(
                lambda v, k=key: self._timeline_layer_toggled(k, v))
            tl_lay.addWidget(chk)
        tl_action = QWidgetAction(tl_menu)
        tl_action.setDefaultWidget(tl_box)
        tl_menu.addAction(tl_action)
        self.timeline_layers_btn.setMenu(tl_menu)
        ctl.addWidget(self.timeline_layers_btn)
        seek_col.addLayout(ctl)
        seek_col.addLayout(mid, stretch=1)
        # la línea de tiempo es un panel más del catálogo (ventana propia);
        # acá solo se deshabilita hasta que la fuente sea replay/captura
        self.seek_row.setEnabled(False)
        self._timeline_on = False
        # estado vivo del capturador (heartbeat + crecimiento del archivo)
        self.capturer_status = QLabel("Capturer: not running")
        self.capturer_status.setStyleSheet(
            f"color: {theme.TEXT_MUTED}; font-size: 7.5pt;")
        src_lay.addWidget(self.capturer_status)
        self._caprate: tuple | None = None
        column.addWidget(src_box)

        # --- catálogo de ventanas: grilla de botones conmutables, arriba de
        # todo y sin scroll (resaltado = ventana abierta; descripción al
        # pasar el mouse y en el renglón de ayuda) ---
        panels_box = QGroupBox("Windows")
        panels_lay = QVBoxLayout(panels_box)
        panels_lay.setSpacing(4)
        self._catalog_checks: dict[str, QPushButton] = {}
        self._catalog_desc: dict[str, str] = {}
        titles = {pid: (title, desc)
                  for pid, title, desc in self.PANEL_CATALOG}

        def _make_btn(pid: str) -> QPushButton:
            title, desc = titles[pid]
            btn = QPushButton(title)
            btn.setCheckable(True)
            btn.setToolTip(desc)
            # los botones no imponen el ancho del hub: con la ventana
            # angosta se recortan (el tooltip siempre tiene el nombre)
            btn.setMinimumWidth(56)
            btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            btn.toggled.connect(
                lambda on, p=pid: self._catalog_toggled(p, on))
            btn.installEventFilter(self)
            self._catalog_checks[pid] = btn
            self._catalog_desc[pid] = desc
            return btn

        # destacados: Drivers y Timeline con ícono y color propios
        featured_row = QHBoxLayout()
        featured_row.setSpacing(4)
        for pid in self._FEATURED:
            color = self._FEATURED_COLORS.get(pid, theme.ACCENT)
            btn = _make_btn(pid)
            btn.setMinimumWidth(80)
            btn.setText(f"{self._FEATURED_LOOK.get(pid, '')} "
                        f"{titles[pid][0]}")
            btn.setStyleSheet(
                "QPushButton { text-align: left; padding: 7px 10px;"
                f" font-weight: bold; font-size: 9pt; color: {color};"
                f" border: 2px solid {color}; border-radius: 5px; }}"
                f"QPushButton:checked {{ background: {color};"
                " color: #ffffff; }")
            featured_row.addWidget(btn)
        panels_lay.addLayout(featured_row)

        # secciones por afinidad; lo que no tenga grupo cae en "Other".
        # Las columnas de cada grilla se adaptan al ancho de la ventana
        # (1 a 4) en _relayout_catalog
        self._catalog_sections: list[tuple[QGridLayout, list]] = []
        self._catalog_cols = 2
        groups = list(self.PANEL_GROUPS)
        placed = set(self._FEATURED) | {
            p for _name, pids in groups for p in pids}
        leftovers = tuple(pid for pid, _t, _d in self.PANEL_CATALOG
                          if pid not in placed)
        if leftovers:
            groups.append(("Other", leftovers))
        for name, pids in groups:
            pids = [p for p in pids if p in titles]
            if not pids:
                continue
            head = QLabel(name.upper())
            head.setStyleSheet(
                f"color: {theme.TEXT_MUTED}; font-size: 7pt;"
                " letter-spacing: 1px; margin-top: 3px;")
            panels_lay.addWidget(head)
            grid = QGridLayout()
            grid.setSpacing(4)
            btns = []
            for i, pid in enumerate(pids):
                btn = _make_btn(pid)
                btn.setStyleSheet(
                    "QPushButton { text-align: left; padding: 4px 8px; }"
                    f"QPushButton:checked {{ background: {theme.ACCENT};"
                    " color: #ffffff; font-weight: bold; }}")
                grid.addWidget(btn, i // 2, i % 2)
                btns.append(btn)
            self._catalog_sections.append((grid, btns))
            panels_lay.addLayout(grid)
        self.catalog_hint = QLabel(
            "Click a window to open or close it — every view opens in its "
            "own window.")
        self.catalog_hint.setWordWrap(True)
        self.catalog_hint.setStyleSheet(
            f"color: {theme.TEXT_MUTED}; font-size: 7.5pt;")
        self.catalog_hint.setMinimumHeight(30)
        panels_lay.addWidget(self.catalog_hint)
        btn_row = QHBoxLayout()
        self.open_all_btn = QPushButton("Open all")
        self.open_all_btn.clicked.connect(lambda: self._set_all_panels(True))
        self.close_all_btn = QPushButton("Close all")
        self.close_all_btn.clicked.connect(lambda: self._set_all_panels(False))
        btn_row.addWidget(self.open_all_btn)
        btn_row.addWidget(self.close_all_btn)
        btn_row.addStretch(1)
        panels_lay.addLayout(btn_row)
        column.addWidget(panels_box, stretch=1)

        # --- pilotos (ventana propia, destacada en el catálogo) ---
        self._drivers_box = QWidget()
        drv_lay = QVBoxLayout(self._drivers_box)
        drv_lay.setContentsMargins(4, 2, 4, 4)
        self.all_check = QCheckBox("Select all")
        drv_lay.addWidget(self.all_check)
        self.driver_list = QListWidget()
        self.driver_list.setMinimumHeight(120)
        drv_lay.addWidget(self.driver_list, stretch=1)

        # --- perfiles de ventanas ---
        prof_box = QGroupBox("Window profiles")
        prof_lay = QVBoxLayout(prof_box)
        prow = QHBoxLayout()
        self.profile_combo = QComboBox()
        prow.addWidget(self.profile_combo, stretch=1)
        self.profile_apply_btn = QPushButton("Apply")
        prow.addWidget(self.profile_apply_btn)
        prof_lay.addLayout(prow)
        prow2 = QHBoxLayout()
        self.profile_save_btn = QPushButton("Save current as…")
        self.profile_delete_btn = QPushButton("Delete")
        prow2.addWidget(self.profile_save_btn)
        prow2.addWidget(self.profile_delete_btn)
        prow2.addStretch(1)
        prof_lay.addLayout(prow2)
        column.addWidget(prof_box)

        # --- ajustes generales ---
        set_box = QGroupBox("Settings")
        set_lay = QVBoxLayout(set_box)
        self.trails_check = QCheckBox("Map trails")
        self.trails_check.setChecked(bool(self.cfg["ui"].get("show_trails", True)))
        self.peaks_check = QCheckBox("Peak values (max/min)")
        self.peaks_check.setChecked(bool(self.cfg["ui"].get("show_peaks", False)))
        self.refine_check = QCheckBox("Corner model refinement")
        self.refine_check.setToolTip(
            "Analysis panels only: reconstruct corner minimum speeds with "
            "per-corner profiles learned from every lap of the session "
            "(and previous ones on this circuit). Fixes the 4-5 Hz "
            "sampling bias when no tick lands on the apex. Atypical laps "
            "(mistakes) keep their raw data. Official timing is never "
            "touched.")
        self.refine_check.setChecked(
            bool(self.cfg["ui"].get("refine_corners", False)))
        self.analysis_engine.set_refine(self.refine_check.isChecked())
        self.refine_check.toggled.connect(self._refine_toggled)
        self.snap_check = QCheckBox("Magnetic windows")
        self.snap_check.setToolTip(
            "Windows stick to each other and to screen edges when moved or "
            "resized close — no pixel-hunting to build tiled layouts. "
            "Arrow keys on a window fine-tune by 1 px (Ctrl = resize).")
        self.snap_check.setChecked(
            bool(self.cfg["ui"].get("snap_windows", True)))
        from .docks import set_snap_enabled
        set_snap_enabled(self.snap_check.isChecked())
        self.snap_check.toggled.connect(self._snap_toggled)
        set_lay.addWidget(self.trails_check)
        set_lay.addWidget(self.peaks_check)
        set_lay.addWidget(self.refine_check)
        set_lay.addWidget(self.snap_check)
        self.notif_settings_btn = QPushButton("Notifications…")
        self.notif_settings_btn.setToolTip(
            "Choose which events to announce and whether popups show")
        self.notif_settings_btn.clicked.connect(
            lambda: NotificationSettingsDialog(self.cfg, self).exec())
        set_lay.addWidget(self.notif_settings_btn)
        column.addWidget(set_box)

        # --- vistas: cada una en su propia ventana, con controles propios ---
        self.race_channel_combo = self._make_channel_combo(
            "race_channel", self.chart_rolling)
        self.race_window_combo = self._make_window_combo(
            "carrera_window_laps", 1.0, self.chart_rolling)
        self.race2_channel_combo = self._make_channel_combo(
            "race2_channel", self.chart_wrap)
        self.quali_channel_combo = self._make_channel_combo(
            "quali_channel", self.chart_qualy)
        self.gap_window_combo = self._make_window_combo(
            "gap_window_laps", 0.0, self.chart_timing)
        hosts = {
            "race_chart": _ViewHost(self.chart_rolling, [
                ("Channel", self.race_channel_combo),
                ("X window", self.race_window_combo)], self._chart_sel_btns[0]),
            "race2_chart": _ViewHost(self.chart_wrap, [
                ("Channel", self.race2_channel_combo)], self._chart_sel_btns[1]),
            "quali_view": _ViewHost(self.chart_qualy, [
                ("Channel", self.quali_channel_combo)], self._chart_sel_btns[2]),
            "times_gap": _ViewHost(self.chart_timing, [
                ("X window", self.gap_window_combo)], self._chart_sel_btns[3]),
            "race_trace": self.chart_trace,  # ya lleva sus controles
            "tower": self.tower,
            "map": self.track_map,
            "dominance": self.dominance_view,
            "lap_wheel": self.lap_wheel,
            "session": self.session_strip,
            "lap_compare": self.lap_compare_view,
            "data_table": self.data_table_view,
            "drivers": self._drivers_box,
            "timeline": self.seek_row,
            "strategy": self.strategy_view,
            "tyre_stints": self.tyre_stints_view,
            "micro_config": self.micro_config_view,
            "pitlane": self.pitlane_view,
            "pitlane_map": self.pitlane_map_view,
            "pit_strategy": self.pit_strategy_view,
            "race_control": self.race_control_view,
            "weather": self.weather_now,
            "weather_chart": self.weather_chart,
            "notifications": self.notifications_view,
            "analysis": self.analysis_launcher,
        }
        self._panels: dict[str, Detachable] = {}
        for pid, title, _desc in self.PANEL_CATALOG:
            self._panels[pid] = Detachable(pid, title, hosts[pid],
                                           window_only=True)
        # paneles de análisis: fuera del catálogo (se abren desde la
        # ventana Analysis), pero con la misma mecánica de ventana propia
        for section, items in ANALYSIS_SECTIONS:
            for pid, title, _desc in items:
                self._panels[pid] = Detachable(
                    pid, f"{section} · {title}", self.analysis_views[pid],
                    window_only=True)
        # subpaneles internos (siguen acoplados dentro de su ventana, con la
        # opción de flotarlos aparte desde su propio botón ⧉)
        self._panels["quali_cards"] = self.chart_qualy.cards_panel
        self._cascade_n = 0

        # el hub escrolea: su alto mínimo dejó de ser la suma de todas las
        # secciones (en pantallas chicas superaba el escritorio)
        hub_scroll = QScrollArea()
        hub_scroll.setWidgetResizable(True)
        hub_scroll.setFrameShape(QScrollArea.NoFrame)
        hub_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        hub_scroll.setWidget(root)
        self.setCentralWidget(hub_scroll)
        self.setMinimumSize(260, 320)
        self.status_label = QLabel(self._source_status)
        self.statusBar().addWidget(self.status_label, 1)
        self.meta_label = QLabel("")
        self.statusBar().addPermanentWidget(self.meta_label)
        self.version_btn = QPushButton(f"v{__version__}")
        self.version_btn.setFlat(True)
        self.version_btn.setCursor(Qt.PointingHandCursor)
        self.version_btn.setToolTip("Check for updates")
        self.statusBar().addPermanentWidget(self.version_btn)

    def _make_channel_combo(self, cfg_key: str, chart) -> QComboBox:
        """Combo de canal propio de una vista (persistido por vista)."""
        combo = QComboBox()
        for ch in CHANNEL_ORDER:
            combo.addItem(CHANNELS[ch][0], ch)
        idx = combo.findData(str(self.cfg["ui"].get(cfg_key, "speed")))
        if idx >= 0:
            combo.setCurrentIndex(idx)
        chart.set_channel(combo.currentData())

        def apply(_i: int = 0) -> None:
            chart.set_channel(combo.currentData())
            self.cfg.setdefault("ui", {})[cfg_key] = combo.currentData()
            config.save_config(self.cfg)

        combo.currentIndexChanged.connect(apply)
        return combo

    def _make_window_combo(self, cfg_key: str, default: float,
                           chart) -> QComboBox:
        """Combo de ventana X en vueltas propio de una vista."""
        combo = QComboBox()
        for label, laps in X_WINDOWS:
            combo.addItem(label, laps)
        idx = combo.findData(float(self.cfg["ui"].get(cfg_key, default)))
        if idx >= 0:
            combo.setCurrentIndex(idx)
        chart.set_window_laps(float(combo.currentData()))

        def apply(_i: int = 0) -> None:
            chart.set_window_laps(float(combo.currentData()))
            self.cfg.setdefault("ui", {})[cfg_key] = float(combo.currentData())
            config.save_config(self.cfg)

        combo.currentIndexChanged.connect(apply)
        return combo

    def _wire(self) -> None:
        self.connect_btn.clicked.connect(self._toggle_connection)
        self.driver_list.itemChanged.connect(self._selection_changed)
        self.hub.driversChanged.connect(self._rebuild_driver_list)
        # correlación mouse gráfico <-> mapa
        for chart in (self.chart_rolling, self.chart_wrap,
                      self.chart_qualy.chart, self.chart_timing):
            chart.hover_dist_cb = self._on_chart_hover
        self.track_map.hover_dist_cb = self._on_map_hover
        for view in self.analysis_views.values():
            view.hover_dist_cb = self._on_analysis_hover
        self.lap_compare_view.hover_dist_cb = self._on_analysis_hover
        self.trails_check.toggled.connect(self._trails_toggled)
        self.track_map.set_trails_enabled(self.trails_check.isChecked())
        self.peaks_check.toggled.connect(self._peaks_toggled)
        for panel in self._panels.values():
            panel.stateChanged.connect(self._on_panel_state)
        self._restore_panels()
        self.all_check.toggled.connect(self._select_all_toggled)
        self.seek_slider.sliderReleased.connect(self._seek_released)
        self.seek_slider.sliderMoved.connect(self._seek_preview)
        self.pause_btn.toggled.connect(self._pause_toggled)
        self.live_btn.clicked.connect(self._go_live)
        self.speed_combo.currentIndexChanged.connect(self._speed_changed)
        if self.peaks_check.isChecked():
            self._peaks_toggled(True)
        self.year_spin.valueChanged.connect(self._load_schedule)
        self._load_schedule()
        self.source_combo.currentIndexChanged.connect(self._source_kind_changed)
        self._source_kind_changed(self.source_combo.currentIndex())
        self.version_btn.clicked.connect(
            lambda: run_check(self, self.cfg, silent=False)
        )
        self.profile_apply_btn.clicked.connect(self._apply_selected_profile)
        self.profile_save_btn.clicked.connect(self._save_layout_profile)
        self.profile_delete_btn.clicked.connect(self._delete_selected_profile)
        self._reload_profiles()

    def _idle_connect_label(self) -> str:
        """Con la fuente Capture el botón dice qué hace de verdad: abrir el
        capturador (y conectar solo cuando fluyan datos)."""
        return ("Open capturer" if self.source_combo.currentData() == "capture"
                else "Connect")

    def _source_kind_changed(self, _index: int) -> None:
        """Year/GP/Session solo aplican a Replay; Speed no aplica a Live."""
        kind = self.source_combo.currentData()
        for row in (0, 1, 2):
            self._replay_form.setRowVisible(row, kind == "replay")
        self._replay_form.setRowVisible(3, kind != "live")
        if self.source is None and not self._cap_waiting:
            self.connect_btn.setText(self._idle_connect_label())

    # ------------------------------------------------------------- conexión

    def _toggle_connection(self) -> None:
        if self._cap_waiting:
            self._end_capture_wait()
            self._on_source_status("Cancelled — not connected.")
        elif self.source is not None:
            self._disconnect()
        else:
            self._connect()

    def _connect(self) -> None:
        kind = self.source_combo.currentData()
        speed = float(self.speed_combo.currentData())
        if kind == "demo":
            source = DemoSource(speed=speed)
        elif kind == "replay":
            self._save_replay_cfg()
            source = ReplaySource(
                year=self.year_spin.value(),
                gp=self._selected_gp() or "Bahrain",
                session=self.session_combo.currentText(),
                speed=speed,
            )
        elif kind == "live":
            source = LiveSource()
        else:
            # Capture: el visualizador gestiona el capturador — si ya hay un
            # archivo creciendo se conecta ya; si no, abre el capturador
            # (cuando hace falta) y conecta solo al empezar a fluir datos
            self._begin_capture_wait()
            return
        self._start_source(source)

    # ------------------------------------------ fuente Capture (capturador)

    def _active_capture(self):
        """Archivo de captura escribiéndose ahora mismo (mtime fresco)."""
        now = time.time()
        try:
            candidates = [
                p for p in config.recordings_dir().glob("*.jsonl")
                if now - p.stat().st_mtime < 5.0 and p.stat().st_size > 0
            ]
        except OSError:
            return None
        return max(candidates, key=lambda p: p.stat().st_mtime, default=None)

    def _begin_capture_wait(self) -> None:
        rec = config.recordings_dir()
        rec.mkdir(parents=True, exist_ok=True)
        active = self._active_capture()
        if active is not None:
            self._start_source(CaptureSource(
                active, speed=float(self.speed_combo.currentData())))
            return
        launched = False
        running = config.capture_running()
        if not running and not os.environ.get("F1TELEM_NO_CAPTURE_SPAWN"):
            launched = self._launch_capturer()
        elif running:
            # puede estar escondido en la bandeja: pedirle que se muestre
            try:
                config.capture_show_path().write_text("1", encoding="utf-8")
            except OSError:
                pass
        self._cap_baseline = {}
        for p in rec.glob("*.jsonl"):
            try:
                self._cap_baseline[p] = p.stat().st_size
            except OSError:
                pass
        self._cap_waiting = True
        self.connect_btn.setText("Cancel")
        self.source_combo.setEnabled(False)
        self._on_source_status(
            ("Opening the capturer — this takes a few seconds... start a "
             "live capture or an import there and the data will connect by "
             "itself." if launched else
             "Capturer already running (check the tray) — waiting for data: "
             "start a live capture or an import there..."))
        self._cap_timer.start()

    def _launch_capturer(self) -> bool:
        try:
            if getattr(sys, "frozen", False):
                exe = Path(sys.executable).parent / "capture" / "BoxBox-F1-Capture.exe"
                if not exe.exists():
                    return False
                subprocess.Popen([str(exe)], cwd=str(exe.parent))
            else:
                env = dict(os.environ)
                src_dir = Path(__file__).resolve().parents[2]
                env["PYTHONPATH"] = (str(src_dir) + os.pathsep
                                     + env.get("PYTHONPATH", ""))
                subprocess.Popen([sys.executable, "-m", "f1telem", "--capture"],
                                 env=env)
            return True
        except OSError:
            return False

    def _poll_capture_ready(self) -> None:
        """Conecta apenas algún archivo de captura crece (o aparece); con
        varios creciendo gana el modificado más recientemente."""
        best = None
        try:
            for p in config.recordings_dir().glob("*.jsonl"):
                stat = p.stat()
                if stat.st_size > self._cap_baseline.get(p, 0):
                    if best is None or stat.st_mtime > best[1]:
                        best = (p, stat.st_mtime)
        except OSError:
            return
        if best is not None:
            self._end_capture_wait()
            self._start_source(CaptureSource(
                best[0], speed=float(self.speed_combo.currentData())))

    def _poll_capture_follow(self) -> None:
        """Con la fuente Capture conectada: si aparece otro archivo con
        crecimiento sostenido (import o captura nueva) mientras el actual
        está quieto, cambiar a ese archivo automáticamente. El crecimiento
        debe verse en 3 sondeos seguidos: los heartbeats sueltos del stream
        fuera de sesión no cuentan."""
        source = self.source
        if not isinstance(source, CaptureSource):
            self._cap_follow_timer.stop()
            return
        current = Path(source.path)
        active = self._active_capture()
        if active is None or str(active) == str(current):
            self._follow_cand = None
            return
        try:
            size = active.stat().st_size
            cur_stale = time.time() - current.stat().st_mtime > 5.0
        except OSError:
            return
        cand = self._follow_cand
        hits = cand[2] + 1 if (cand is not None and cand[0] == str(active)
                               and size > cand[1]) else 0
        self._follow_cand = (str(active), size, hits)
        if hits < 2 or not cur_stale:
            return
        self._follow_cand = None
        speed = float(self.speed_combo.currentData())
        self._disconnect()
        self._start_source(CaptureSource(active, speed=speed))
        self._on_source_status(f"Following new capture: {active.name}")

    def _end_capture_wait(self) -> None:
        self._cap_timer.stop()
        self._cap_waiting = False
        self._cap_baseline = {}
        self.connect_btn.setText(self._idle_connect_label())
        self.source_combo.setEnabled(True)

    def _start_source(self, source: BaseSource) -> None:
        self.hub.reset()
        for chart in self.charts:
            chart.clear_data()
        self.chart_qualy.set_reference(None)
        self.track_map.clear_data()
        self.lap_wheel.clear_data()
        self._follow_cand = None
        if isinstance(source, CaptureSource):
            self._cap_follow_timer.start()
        else:
            self._cap_follow_timer.stop()

        source.batch.connect(self._on_batch)
        source.positions.connect(self.hub.on_positions)
        source.corners.connect(self.hub.on_corners)
        source.tyres.connect(self.hub.on_tyres)
        source.pits.connect(self._on_pits)
        source.trackStatus.connect(self._on_track_status)
        source.weather.connect(self._on_weather)
        source.sectorYellows.connect(self.hub.on_sector_yellows)
        source.sectorTimes.connect(self.hub.on_sector_times)
        source.segmentStatus.connect(self.hub.on_segments)
        source.pitLane.connect(self.hub.on_pit_lane)
        source.raceControl.connect(self.hub.on_race_control)
        source.sessionClock.connect(self.hub.on_session_clock)
        source.lapCount.connect(self.hub.on_lap_count)
        source.sessionMeta.connect(self.hub.on_session_meta)
        source.retirements.connect(self.hub.on_retirements)
        # config de microsectores por circuito+año: cargarla apenas la meta
        # identifica al fin de semana (después de que el hub la guardó)
        self._micro_cfg_key = None
        source.sessionMeta.connect(self._load_micro_cfg)
        # enriquecimiento OpenF1: se dispara con la misma meta de sesión
        self.openf1.reset()
        self.openf1.set_live(isinstance(source, (LiveSource, CaptureSource)))
        source.sessionMeta.connect(self._load_openf1)
        # Live/Capture: el marco de vuelta llega con la latencia del feed y
        # se re-ancla con los S1 oficiales
        self.hub.live_frames = isinstance(source, (LiveSource, CaptureSource))
        self.lap_ruler.set_rain([])
        self.lap_ruler.set_moments([])
        self._moments_n = 0
        self._progress = None
        self.lap_ruler.set_marks([])
        self.lap_ruler.set_pits([])
        self.lap_ruler.set_status([])
        self.pause_btn.setChecked(False)
        self.tower.clear_data()
        self.session_strip.clear_data()
        self.race_control_view.clear_data()
        self.strategy_view.clear_data()
        self.tyre_stints_view.clear_data()
        self.micro_config_view.clear_data()
        self.dominance_view.clear_data()
        self.pitlane_view.clear_data()
        self.pitlane_map_view.clear_data()
        self.pit_strategy_view.clear_data()
        self.notifier.reset()
        self.notifications_view.clear_data()
        self.weather_now.clear_data()
        self.weather_chart.clear_data()
        self.analysis_engine.save_profiles()  # el entrenamiento no se pierde
        self.analysis_engine.reset()
        for view in self.analysis_views.values():
            view.clear_data()
        self.lap_compare_view.clear_data()
        self.data_table_view.clear_data()
        if isinstance(source, (ReplaySource, CaptureSource)):
            source.progress.connect(self._on_progress)
            source.seekReset.connect(self._on_seek_reset)
            source.lapMarks.connect(self.lap_ruler.set_marks)
            self._set_timeline_available(True)
        else:
            self._set_timeline_available(False)
        self.live_btn.setVisible(isinstance(source, CaptureSource))
        if isinstance(source, CaptureSource):
            source.liveChanged.connect(self._on_live_changed)
            self._on_live_changed(True)
        source.driversDiscovered.connect(self.hub.on_drivers)
        source.trackLength.connect(self.hub.on_track_length)
        source.trackOutline.connect(self.hub.on_outline)
        source.statusChanged.connect(self._on_source_status)
        source.failed.connect(self._on_source_failed)
        source.finished.connect(self._on_source_finished)
        self.source = source
        self.connect_btn.setText("Disconnect")
        self.source_combo.setEnabled(False)
        source.start()

    def _disconnect(self) -> None:
        self._cap_follow_timer.stop()
        self.analysis_engine.save_profiles()
        if self.source is None:
            return
        source, self.source = self.source, None
        source.stop()
        source.wait(8000)
        source.deleteLater()
        self.connect_btn.setText(self._idle_connect_label())
        self.source_combo.setEnabled(True)
        self._set_timeline_available(False)
        self.live_btn.setVisible(False)
        self._on_source_status("Disconnected. Received data remains available.")

    def _on_source_finished(self) -> None:
        if self.source is not None and self.source.isFinished():
            # la fuente terminó sola (error fatal en la carga, etc.)
            self._cap_follow_timer.stop()
            source, self.source = self.source, None
            source.deleteLater()
            self.connect_btn.setText(self._idle_connect_label())
            self.source_combo.setEnabled(True)
            self._set_timeline_available(False)
            self.live_btn.setVisible(False)

    # ------------------------------------------------- línea de tiempo

    @staticmethod
    def _fmt_mmss(seconds: float) -> str:
        seconds = max(0.0, seconds)
        return f"{int(seconds // 60)}:{int(seconds % 60):02d}"

    def _seek_to_time(self, t: float) -> None:
        if isinstance(self.source, (ReplaySource, CaptureSource)):
            self.source.request_seek(t)

    def _seek_relative(self, delta_s: float) -> None:
        """Salto relativo desde la posición actual (botones ±5s…±15m),
        acotado al rango de la sesión."""
        if self._progress is None:
            return
        t0, t, t1 = self._progress
        self._seek_to_time(min(max(t + delta_s, t0), t1))

    def _go_live(self) -> None:
        if isinstance(self.source, CaptureSource):
            self.pause_btn.setChecked(False)
            self.source.go_live()

    def _on_live_changed(self, on: bool) -> None:
        self.live_btn.setStyleSheet(
            "background: #e10600; color: white; font-weight: bold;" if on else ""
        )

    def _on_chart_hover(self, dist) -> None:
        """Hover en un gráfico -> anillo en el punto de pista del mapa."""
        if self.track_map.isVisible():
            self.track_map.set_probe_dist(dist)
        self._hover_to_analysis(dist)

    def _on_map_hover(self, dist) -> None:
        """Hover en el mapa -> línea de referencia en el gráfico activo."""
        for pid, chart in zip(self._CHART_IDS, self.charts):
            if self._panels[pid].is_panel_visible():
                chart.show_track_marker(dist)
        self._hover_to_analysis(dist)

    def _hover_to_analysis(self, dist) -> None:
        for view in (*self.analysis_views.values(), self.lap_compare_view):
            if view.isVisible():
                view.set_hover_dist(dist)

    def _on_analysis_hover(self, dist) -> None:
        """Hover en un mapa/gráfico de análisis -> el mismo punto de pista
        en TODO el resto: mapa principal, gráficos y demás paneles."""
        if self.track_map.isVisible():
            self.track_map.set_probe_dist(dist)
        for pid, chart in zip(self._CHART_IDS, self.charts):
            if self._panels[pid].is_panel_visible():
                chart.show_track_marker(dist)
        self._hover_to_analysis(dist)

    def _on_pits(self, data) -> None:
        self.hub.on_pits(data)
        marks = []
        for drv, stops in self.hub.pits.items():
            info = self.hub.drivers.get(drv)
            color = info.color if info else "#9aa0a6"
            marks.extend((t, color) for _lap, t in stops)
        self.lap_ruler.set_pits(marks)

    def _on_track_status(self, periods) -> None:
        self.hub.on_track_status(periods)
        self.lap_ruler.set_status(periods)

    def _on_weather(self, rows) -> None:
        self.hub.on_weather(rows)
        # períodos de lluvia para la línea de tiempo
        periods = []
        start = None
        for row in self.hub.weather:
            t, rain = row[0], row[4]
            if rain and start is None:
                start = t
            elif not rain and start is not None:
                periods.append((start, t))
                start = None
        if start is not None:
            periods.append((start, float("inf")))
        self.lap_ruler.set_rain(periods)

    def _on_progress(self, t0: float, t: float, t1: float) -> None:
        self._progress = (t0, t, t1)
        self.lap_ruler.set_range(t0, t1)
        if self.seek_slider.isSliderDown():
            return
        span = max(t1 - t0, 1e-9)
        self.seek_slider.blockSignals(True)
        self.seek_slider.setValue(int((t - t0) / span * 1000))
        self.seek_slider.blockSignals(False)
        self.time_label.setText(f"{self._fmt_mmss(t - t0)} / {self._fmt_mmss(t1 - t0)}")

    def _seek_preview(self, value: int) -> None:
        """Tooltip con vuelta/tiempo/estado mientras se arrastra el scrub."""
        if self._progress is None:
            return
        t0, _t, t1 = self._progress
        t = t0 + value / 1000.0 * (t1 - t0)
        QToolTip.showText(QCursor.pos(), self.lap_ruler.hint_at(t),
                          self.seek_slider)

    def _seek_released(self) -> None:
        if self._progress is None or not isinstance(self.source, (ReplaySource, CaptureSource)):
            return
        t0, _t, t1 = self._progress
        self.source.request_seek(t0 + self.seek_slider.value() / 1000.0 * (t1 - t0))

    def _on_seek_reset(self) -> None:
        """Limpia las muestras sin perder pilotos, selección ni la target."""
        self.hub.clear_samples()
        self.chart_rolling.clear_data()
        self.chart_wrap.clear_data()
        self.chart_timing.clear_data()
        self.chart_trace.clear_data()
        self.track_map.clear_data()
        self.tower.clear_data()
        self.dominance_view.clear_data()
        self.chart_qualy.clear_stream_data()

    def _on_batch(self, samples: list) -> None:
        self.hub.on_batch(samples)

    def _on_source_status(self, text: str) -> None:
        self._source_status = text
        self.status_label.setText(text)

    def _on_source_failed(self, text: str) -> None:
        self._on_source_status(text)
        QMessageBox.warning(self, "BoxBox-F1", text)

    # ------------------------------------------------- calendario del año

    def _selected_gp(self) -> str:
        """Nombre del evento elegido (o el texto tipeado a mano)."""
        idx = self.gp_combo.currentIndex()
        if idx >= 0 and self.gp_combo.currentText() == self.gp_combo.itemText(idx):
            return str(self.gp_combo.itemData(idx))
        return self.gp_combo.currentText().strip()

    def _load_schedule(self) -> None:
        if os.environ.get("F1TELEM_NO_SCHEDULE"):
            return  # tests: sin red, el hilo lento bloquearía el teardown
        self._sched_loader = ScheduleLoader(self.year_spin.value(), self)
        self._sched_loader.loaded.connect(self._on_schedule)
        self._sched_loader.start()

    def _on_schedule(self, year: int, events: list) -> None:
        if year != self.year_spin.value():
            return  # respuesta vieja: el usuario ya cambió el año
        current = self._selected_gp()
        self.gp_combo.blockSignals(True)
        self.gp_combo.clear()
        for rnd, name in events:
            self.gp_combo.addItem(f"{rnd:02d} · {name}", name)
        idx = self.gp_combo.findData(current)
        if idx >= 0:
            self.gp_combo.setCurrentIndex(idx)
        else:
            self.gp_combo.setEditText(current)
        self.gp_combo.blockSignals(False)

    def _save_replay_cfg(self) -> None:
        self.cfg["replay"] = {
            "year": self.year_spin.value(),
            "gp": self._selected_gp(),
            "session": self.session_combo.currentText(),
            "speed": float(self.speed_combo.currentData()),
        }
        config.save_config(self.cfg)

    # -------------------------------------------------------------- pilotos

    def _rebuild_driver_list(self) -> None:
        checked = {
            self.driver_list.item(i).data(Qt.UserRole)
            for i in range(self.driver_list.count())
            if self.driver_list.item(i).checkState() == Qt.Checked
        }
        self.driver_list.blockSignals(True)
        self.driver_list.clear()
        drivers = sorted(self.hub.drivers.values(), key=lambda d: d.label.upper())
        for info in drivers:
            item = QListWidgetItem(info.label)
            item.setData(Qt.UserRole, info.number)
            item.setToolTip(self._driver_tooltip(info))
            pix = QPixmap(12, 12)
            pix.fill(QColor(info.color))
            item.setIcon(QIcon(pix))
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if info.number in checked else Qt.Unchecked)
            self.driver_list.addItem(item)
        self.driver_list.blockSignals(False)
        self._selection_changed()

    def _driver_tooltip(self, info) -> str:
        """Nombre y equipo; con la foto de OpenF1/CDN si ya se descargó."""
        import html as _html
        from pathlib import Path

        text = _html.escape(
            " · ".join(p for p in (info.name or info.code, info.team) if p))
        photo = self.hub.headshots.get(info.number)
        if photo:
            try:
                return (f"<img src='{Path(photo).as_uri()}' width='96'>"
                        f"<br>{text}")
            except ValueError:
                pass
        return text

    def _selected_drivers(self) -> list[str]:
        return [
            self.driver_list.item(i).data(Qt.UserRole)
            for i in range(self.driver_list.count())
            if self.driver_list.item(i).checkState() == Qt.Checked
        ]

    def _select_all_toggled(self, on: bool) -> None:
        self.driver_list.blockSignals(True)
        for i in range(self.driver_list.count()):
            self.driver_list.item(i).setCheckState(Qt.Checked if on else Qt.Unchecked)
        self.driver_list.blockSignals(False)
        self._selection_changed()

    def _selection_changed(self, *_args) -> None:
        selected = self._selected_drivers()
        # el panel Drivers pisa la selección local (👥) de cada gráfico —
        # solo ante un cambio real de selección, así los retoques locales
        # sobreviven a driversChanged sin cambios (altas, colores)
        if selected != self._panel_sel:
            self._panel_sel = list(selected)
            for chart, btn in zip(self.charts, self._chart_sel_btns):
                btn.set_selection(selected)
                chart.set_selected(selected)
        # reflejar el estado sin disparar el toggle
        self.all_check.blockSignals(True)
        self.all_check.setChecked(
            self.driver_list.count() > 0 and len(selected) == self.driver_list.count()
        )
        self.all_check.blockSignals(False)

    def _timeline_layer_toggled(self, key: str, on: bool) -> None:
        """Capas de la línea de tiempo (vueltas, pits, banderas, lluvia,
        sobrepasos): mostrar/ocultar persistente."""
        self.lap_ruler.set_layer(key, on)
        layers = self.cfg.setdefault("ui", {}).setdefault(
            "timeline_layers", {})
        if on:
            layers.pop(key, None)
        else:
            layers[key] = False
        config.save_config(self.cfg)

    def _load_openf1(self, meta) -> None:
        """Pide el enriquecimiento OpenF1 con la meta de sesión (el cliente
        es idempotente y se rinde solo si no hay red o no aplica)."""
        if isinstance(meta, dict):
            self.openf1.request(meta)

    def _load_micro_cfg(self, *_meta) -> None:
        """Config de microsectores del circuito+año: se carga UNA vez por
        fin de semana, sin importar qué tanda se cargue."""
        key = self.hub.circuit_key()
        if key is None or key == self._micro_cfg_key:
            return
        self._micro_cfg_key = key
        stored = self.cfg.get("microsectors", {}).get(key)
        self.hub.custom_micro = (
            [float(d) for d in stored]
            if isinstance(stored, list) and stored else None)

    def _trails_toggled(self, on: bool) -> None:
        self.track_map.set_trails_enabled(on)
        self.cfg.setdefault("ui", {})["show_trails"] = on
        config.save_config(self.cfg)

    def _refine_toggled(self, on: bool) -> None:
        self.analysis_engine.set_refine(on)
        self.cfg.setdefault("ui", {})["refine_corners"] = bool(on)
        config.save_config(self.cfg)

    def _snap_toggled(self, on: bool) -> None:
        from .docks import set_snap_enabled

        set_snap_enabled(on)
        self.cfg.setdefault("ui", {})["snap_windows"] = on
        config.save_config(self.cfg)

    # ------------------------------------------------ ventanas (catálogo)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if hasattr(self, "_catalog_sections"):
            self._relayout_catalog()

    def _relayout_catalog(self, w: int | None = None) -> None:
        """Columnas de la sección Windows según el ancho del hub: 1-4."""
        w = self.width() if w is None else w
        cols = 1 if w < 310 else 2 if w < 470 else 3 if w < 650 else 4
        if cols == self._catalog_cols:
            return
        self._catalog_cols = cols
        for grid, btns in self._catalog_sections:
            for btn in btns:
                grid.removeWidget(btn)
            for i, btn in enumerate(btns):
                grid.addWidget(btn, i // cols, i % cols)

    def eventFilter(self, obj, event) -> bool:
        # hover sobre un botón del catálogo -> descripción en el renglón de
        # ayuda (además del tooltip)
        if event.type() == QEvent.Enter:
            for pid, btn in getattr(self, "_catalog_checks", {}).items():
                if btn is obj:
                    self.catalog_hint.setText(
                        f"{btn.text()}: {self._catalog_desc[pid]}")
                    break
        return super().eventFilter(obj, event)

    def _catalog_toggled(self, pid: str, on: bool) -> None:
        panel = self._panels[pid]
        if on and panel._win is None:
            panel.detach(self._next_cascade_rect(pid))
        else:
            panel.set_panel_visible(on)

    def _set_all_panels(self, on: bool) -> None:
        for pid, _title, _desc in self.PANEL_CATALOG:
            self._catalog_toggled(pid, on)

    def _next_cascade_rect(self, pid: str) -> QRect:
        """Posición por defecto de una ventana nueva: cascada a la derecha
        del hub, con tamaño según el tipo de vista."""
        base = self.geometry()
        wide = (pid in self._CHART_IDS or pid.startswith("an_")
                or pid in ("strategy", "weather_chart", "timeline",
                           "lap_compare", "data_table"))
        width = 860 if wide else (320 if pid == "drivers" else 420)
        height = (620 if wide else 520)
        if pid == "session":
            height = 110
        elif pid == "timeline":
            height = 132
        elif pid == "pitlane_map":
            width, height = 860, 150
        elif pid == "lap_wheel":
            width, height = 520, 580
        elif pid == "analysis":
            width, height = 300, 460
        offset = 34 * (self._cascade_n % 8)
        self._cascade_n += 1
        return QRect(base.right() + 16 + offset, base.y() + offset,
                     width, height)

    def _sync_catalog(self) -> None:
        for pid, box in self._catalog_checks.items():
            box.blockSignals(True)
            box.setChecked(self._panels[pid].is_panel_visible())
            box.blockSignals(False)
        self.analysis_launcher.sync(
            lambda pid: self._panels[pid].is_panel_visible())

    def _on_panel_state(self) -> None:
        """Persiste el estado de cada ventana (geometría, fijado, visible)."""
        panels_cfg = self.cfg.setdefault("panels", {})
        float_cfg = panels_cfg.setdefault("float", {})
        visible = panels_cfg.setdefault("visible", {})
        for pid, panel in self._panels.items():
            if getattr(panel, "window_only", False):
                if panel._win is not None:
                    float_cfg[pid] = panel.save_state()
                visible[pid] = panel.is_panel_visible()
            elif panel.floating:  # subpaneles internos (tablas, tarjetas)
                float_cfg[pid] = panel.save_state()
            else:
                float_cfg.pop(pid, None)
        config.save_config(self.cfg)
        self._sync_catalog()

    def _restore_panels(self) -> None:
        """Reabre las ventanas tal cual quedaron (geometría, fijado); sin
        estado previo, abre el conjunto por defecto en cascada."""
        panels_cfg = self.cfg.get("panels", {})
        float_cfg = panels_cfg.get("float", {})
        visible = panels_cfg.get("visible", {})
        # plan primero, aplicar después: cada detach dispara _on_panel_state,
        # que reescribe estos mismos dicts mientras se itera
        plan = []
        analysis_ids = [(pid, title, desc)
                        for _s, items in ANALYSIS_SECTIONS
                        for pid, title, desc in items]
        for pid, _title, _desc in self.PANEL_CATALOG + analysis_ids:
            state = float_cfg.get(pid)
            plan.append((pid,
                         dict(state) if isinstance(state, dict) else None,
                         bool(visible.get(pid, pid in self._DEFAULT_OPEN))))
        subs = [(pid, dict(float_cfg[pid]))
                for pid in ("quali_cards",)
                if isinstance(float_cfg.get(pid), dict)
                and float_cfg[pid].get("floating")]
        for pid, state, open_it in plan:
            panel = self._panels[pid]
            if state is not None and state.get("geom"):
                panel.restore_state({**state, "floating": True,
                                     "visible": open_it})
            elif open_it:
                panel.detach(self._next_cascade_rect(pid))
        # subpaneles internos flotados aparte (tablas de tiempos, tarjetas)
        for pid, state in subs:
            self._panels[pid].restore_state(state)
        self._sync_catalog()

    # -------------------------------------------------- perfiles de layout

    def _reload_profiles(self) -> None:
        current = self.profile_combo.currentText()
        self.profile_combo.blockSignals(True)
        self.profile_combo.clear()
        for name in sorted(self.cfg.get("layouts", {})):
            self.profile_combo.addItem(name)
        idx = self.profile_combo.findText(current)
        if idx >= 0:
            self.profile_combo.setCurrentIndex(idx)
        self.profile_combo.blockSignals(False)

    def _save_layout_profile(self) -> None:
        name, ok = QInputDialog.getText(
            self, "Save window profile",
            "Profile name (e.g. \"Race 3 monitors\"):")
        name = name.strip()
        if not ok or not name:
            return
        profile = {
            "visible": {pid: panel.is_panel_visible()
                        for pid, panel in self._panels.items()},
            "float": {pid: panel.save_state()
                      for pid, panel in self._panels.items()
                      if panel._win is not None or panel.floating},
            "win_max": self.isMaximized(),
        }
        g = self.normalGeometry()
        profile["win_geom"] = [g.x(), g.y(), g.width(), g.height()]
        self.cfg.setdefault("layouts", {})[name] = profile
        config.save_config(self.cfg)
        self._reload_profiles()
        self.profile_combo.setCurrentIndex(self.profile_combo.findText(name))

    def _apply_selected_profile(self) -> None:
        self._apply_layout_profile(self.profile_combo.currentText())

    def _delete_selected_profile(self) -> None:
        self._delete_layout_profile(self.profile_combo.currentText())

    def _apply_layout_profile(self, name: str) -> None:
        profile = self.cfg.get("layouts", {}).get(name)
        if not isinstance(profile, dict):
            return
        float_states = profile.get("float", {})
        visible = profile.get("visible", {})
        for pid, _title, _desc in self.PANEL_CATALOG:
            panel = self._panels[pid]
            state = float_states.get(pid)
            open_it = bool(visible.get(pid, False))
            if isinstance(state, dict) and state.get("geom"):
                panel.restore_state({**state, "floating": True,
                                     "visible": open_it})
            elif open_it and panel._win is None:
                panel.detach(self._next_cascade_rect(pid))
            else:
                panel.apply_visible(open_it)
        geom = profile.get("win_geom")
        if isinstance(geom, list) and len(geom) == 4:
            self.setGeometry(*[int(v) for v in geom])
        self._on_panel_state()  # el perfil aplicado pasa a ser el estado actual

    def _delete_layout_profile(self, name: str) -> None:
        layouts = self.cfg.get("layouts", {})
        if name in layouts:
            del layouts[name]
            config.save_config(self.cfg)
            self._reload_profiles()

    def _set_timeline_available(self, on: bool) -> None:
        """La línea de tiempo aplica solo a replay/captura: la ventana la
        abre/cierra el usuario desde el catálogo; acá solo se habilita."""
        self._timeline_on = on
        self.seek_row.setEnabled(on)

    def _update_capturer_status(self) -> None:
        """Línea viva en el hub: qué está haciendo el capturador (latido +
        crecimiento del archivo activo, con tasa en MB/min)."""
        if not config.capture_running():
            self.capturer_status.setText("Capturer: not running")
            self._caprate = None
            return
        active = self._active_capture()
        if active is None:
            self.capturer_status.setText(
                "Capturer: running (idle — no data flowing)")
            self._caprate = None
            return
        try:
            size = active.stat().st_size
        except OSError:
            return
        now = time.time()
        rate_txt = ""
        if self._caprate is not None and self._caprate[0] == str(active):
            dt = now - self._caprate[2]
            if dt >= 3.0:
                rate = (size - self._caprate[1]) / dt / (1024 * 1024) * 60.0
                rate_txt = f" · {rate:.1f} MB/min"
                self._caprate = (str(active), size, now)
        else:
            self._caprate = (str(active), size, now)
        kind = ("importing" if active.name == "import_live.jsonl"
                else "capturing")
        self.capturer_status.setText(
            f"Capturer: {kind} → {active.name}{rate_txt}")

    def _pause_toggled(self, on: bool) -> None:
        if self.source is not None:
            self.source.set_paused(on)
        self.pause_btn.setText("▶" if on else "⏸")

    def _speed_changed(self) -> None:
        if self.source is not None:
            self.source.set_speed(float(self.speed_combo.currentData()))

    def _peaks_toggled(self, on: bool) -> None:
        for chart in self.charts:
            chart.set_peaks_enabled(on)
        self.cfg.setdefault("ui", {})["show_peaks"] = on
        config.save_config(self.cfg)

    # ----------------------------------------------------------------- tick

    def _safe_refresh(self, refresh) -> None:
        """Aísla el refresh de una vista: si explota, las demás siguen y el
        error queda en %LOCALAPPDATA%\\f1telem\\ui-errors.log (el exe no
        tiene consola). La vista se recupera sola en el próximo tick."""
        try:
            refresh()
        except Exception:
            import time as _t
            import traceback
            traceback.print_exc()
            try:
                path = config.data_dir() / "ui-errors.log"
                if path.exists() and path.stat().st_size > 512 * 1024:
                    path.unlink()
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write(_t.strftime("[%Y-%m-%d %H:%M:%S] ")
                             + traceback.format_exc() + "\n")
            except OSError:
                pass

    def _tick(self) -> None:
        self._tick_n += 1
        if self._tick_n % 30 == 0:  # 1 Hz: límites oficiales de sector
            self._safe_refresh(self.hub.maybe_derive_sector_bounds)
            self._safe_refresh(self.hub.maybe_derive_brake_zones)
        # cada gráfico refresca solo si su ventana está abierta; un refresh
        # que explote jamás debe matar el tick (todo lo que refresca después
        # quedaría "vacío" para siempre, p.ej. tras un salto de timeline):
        # cada vista se aísla y el error queda en ui-errors.log
        for pid, chart in zip(self._CHART_IDS, self.charts):
            if self._panels[pid].is_panel_visible():
                self._safe_refresh(chart.refresh)
        if self.track_map.isVisible():
            self._safe_refresh(self.track_map.refresh)
        if self.lap_wheel.isVisible():
            self._safe_refresh(self.lap_wheel.refresh)
        # el mapa de la calle de boxes anima entre lotes: refresco pleno
        if self.pitlane_map_view.isVisible():
            self._safe_refresh(self.pitlane_map_view.refresh)
        if self.tower.isVisible() and self._tick_n % 15 == 0:
            self._safe_refresh(self.tower.refresh)
        if self._tick_n % 30 == 0:
            self._safe_refresh(self._update_capturer_status)
        if self._tick_n % 15 == 0:
            # el gestor de notificaciones corre siempre (los popups no
            # dependen de que su panel esté visible) y jamás frena el tick
            try:
                self.notifier.check()
            except Exception:
                import traceback
                traceback.print_exc()
            if len(self.notifier.moments) != self._moments_n:
                self._moments_n = len(self.notifier.moments)
                self.lap_ruler.set_moments(
                    [(m["t"], m["text"]) for m in self.notifier.moments])
            if self.session_strip.isVisible():
                self._safe_refresh(self.session_strip.refresh)
            for view in (self.race_control_view, self.strategy_view,
                         self.tyre_stints_view, self.micro_config_view,
                         self.dominance_view,
                         self.pitlane_view, self.pit_strategy_view,
                         self.weather_now, self.weather_chart,
                         self.notifications_view, self.lap_compare_view,
                         self.data_table_view,
                         *self.analysis_views.values()):
                if view.isVisible():
                    self._safe_refresh(view.refresh)
            # sub-paneles flotados aparte con su ventana madre cerrada
            cards = self.chart_qualy.cards_panel
            if (not self._panels["quali_view"].is_panel_visible()
                    and cards.floating and cards.is_panel_visible()):
                self.chart_qualy._update_cards()
        meta = f"Lap: {self.hub.track_length:,.0f} m  ·  Samples: {self.hub.total_samples:,}"
        weather = self.hub.weather_at(self.hub.latest_t)
        if weather is not None:
            _t, air, track, wind, rain = weather[:5]
            meta = (f"Air {air:.0f}°  ·  Track {track:.0f}°  ·  Wind {wind:.1f} m/s"
                    + ("  ·  RAIN" if rain else "") + "  |  " + meta)
        self.meta_label.setText(meta)

    def closeEvent(self, event) -> None:
        self._cap_timer.stop()
        # el cargador de calendario es hijo de la ventana: destruirlo con el
        # hilo corriendo aborta el proceso al salir
        loader = getattr(self, "_sched_loader", None)
        if loader is not None and loader.isRunning():
            loader.wait(5000)
        self._disconnect()  # (guarda también los perfiles entrenados)
        self._on_panel_state()  # persiste la geometría de cada ventana
        for panel in self._panels.values():
            panel.close_float()
        self._timer.stop()
        ui = self.cfg.setdefault("ui", {})
        g = self.normalGeometry()
        ui["win_geom"] = [g.x(), g.y(), g.width(), g.height()]
        config.save_config(self.cfg)
        super().closeEvent(event)
