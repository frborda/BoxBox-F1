"""Ventana principal: fuentes, selección de pilotos, modos y estado."""
from __future__ import annotations

import os

from PySide6.QtCore import Qt, QThread, QTimer, QPointF, QRectF, Signal
from PySide6.QtGui import QColor, QPainter, QPixmap, QPolygonF, QIcon
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFormLayout, QGroupBox,
    QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMainWindow,
    QMessageBox, QPushButton, QSlider, QSpinBox, QSplitter, QStackedWidget,
    QVBoxLayout, QWidget,
)

from .. import __version__, config
from ..hub import DataHub
from ..models import CHANNELS, CHANNEL_ORDER
from ..sources import BaseSource, CaptureSource, DemoSource, LiveSource, ReplaySource
from . import theme
from .charts import RollingChart, WrapChart
from .qualy_view import QualyView
from .timing_view import TimingView, fmt_laptime
from .tower import TimingTower
from .track_map import TrackMapView
from .update_dialog import run_check

MODES = [
    ("Race (rolling window)", 0),
    ("Race 2 (fixed track)", 1),
    ("Quali (comparison)", 2),
    ("Times / Gap", 3),
]

# ventana X en vueltas para los modos sin ventana fija (0 = toda la sesión)
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
        self._t0 = 0.0
        self._t1 = 1.0
        self._seek = seek_callback
        self.setFixedHeight(18)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("Click: jump to the start of that lap\n"
                        "Diamonds: pit stops · Bands: flags/SC")

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
        for t0, t1 in self._rain:
            x0 = self._x_of(t0)
            x1 = self._x_of(min(t1, self._t1))
            painter.fillRect(QRectF(x0, 8, max(x1 - x0, 2.0), 2.5), QColor(0, 130, 220, 170))
        # bandas de bandera/SC de fondo
        for t0, t1, code in self._status:
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
        for lap, t in self._marks:
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
        for t, color in self._pits:
            x = self._x_of(t)
            if x < 0 or x > self.width():
                continue
            painter.setBrush(QColor(color))
            painter.drawPolygon(QPolygonF([
                QPointF(x, 10.5), QPointF(x + 3, 14), QPointF(x, 17.5), QPointF(x - 3, 14),
            ]))
        painter.end()

    def mousePressEvent(self, event) -> None:
        if not self._marks:
            return
        t_click = self._t0 + event.position().x() / max(self.width(), 1) * (self._t1 - self._t0)
        _lap, t = min(self._marks, key=lambda m: abs(m[1] - t_click))
        self._seek(t)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("F1 Live Telemetry")
        self.resize(1360, 820)

        self.cfg = config.load_config()
        self.hub = DataHub(self)
        self.source: BaseSource | None = None
        self._progress: tuple[float, float, float] | None = None
        self._source_status = "Not connected."

        self.chart_rolling = RollingChart(self.hub)
        self.chart_wrap = WrapChart(self.hub)
        self.chart_qualy = QualyView(self.hub)
        self.chart_timing = TimingView(self.hub, self.cfg)
        self.charts = [self.chart_rolling, self.chart_wrap, self.chart_qualy, self.chart_timing]
        self.track_map = TrackMapView(self.hub)
        self.tower = TimingTower(self.hub)
        self._tick_n = 0

        self._build_ui()
        self._wire()

        self._timer = QTimer(self)
        self._timer.setInterval(33)  # 30 fps: el paneo suavizado lo necesita
        self._timer.timeout.connect(self._tick)
        self._timer.start()
        self._laps_timer = QTimer(self)
        self._laps_timer.setInterval(2000)
        self._laps_timer.timeout.connect(self._refresh_ref_laps)
        self._laps_timer.start()

        if (bool(self.cfg.get("updates", {}).get("check_on_startup", True))
                and not os.environ.get("F1TELEM_NO_UPDATE_CHECK")):
            QTimer.singleShot(
                3000, lambda: run_check(self, self.cfg, silent=True)
            )

    # ------------------------------------------------------------------- UI

    def _build_ui(self) -> None:
        root = QWidget()
        layout = QHBoxLayout(root)
        layout.setContentsMargins(8, 8, 8, 8)

        side = QVBoxLayout()
        side.setSpacing(8)

        # --- fuente ---
        src_box = QGroupBox("Data source")
        src_lay = QVBoxLayout(src_box)
        self.source_combo = QComboBox()
        self.source_combo.addItems([
            "Demo (synthetic)", "Replay (FastF1 historical)",
            "Live (F1 Live Timing)", "Capture (recorded live)",
        ])
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
        side.addWidget(src_box)

        # --- pilotos ---
        drv_box = QGroupBox("Drivers (chart series)")
        drv_lay = QVBoxLayout(drv_box)
        self.all_check = QCheckBox("Select all")
        drv_lay.addWidget(self.all_check)
        self.driver_list = QListWidget()
        self.driver_list.setMinimumHeight(180)
        drv_lay.addWidget(self.driver_list)
        side.addWidget(drv_box, stretch=1)

        # --- modo y canal ---
        mode_box = QGroupBox("Mode")
        mode_lay = QFormLayout(mode_box)
        self.mode_combo = QComboBox()
        for name, _ in MODES:
            self.mode_combo.addItem(name)
        self.channel_combo = QComboBox()
        for ch in CHANNEL_ORDER:
            self.channel_combo.addItem(CHANNELS[ch][0], ch)
        self.window_combo = QComboBox()
        for label, laps in X_WINDOWS:
            self.window_combo.addItem(label, laps)
        self.map_check = QCheckBox("Show track map")
        self.map_check.setChecked(bool(self.cfg["ui"].get("show_map", True)))
        self.trails_check = QCheckBox("Map trails")
        self.trails_check.setChecked(bool(self.cfg["ui"].get("show_trails", True)))
        self.tower_check = QCheckBox("Timing tower")
        self.tower_check.setChecked(bool(self.cfg["ui"].get("show_tower", True)))
        self.peaks_check = QCheckBox("Peak values (max/min)")
        self.peaks_check.setChecked(bool(self.cfg["ui"].get("show_peaks", False)))
        mode_lay.addRow("Mode", self.mode_combo)
        mode_lay.addRow("Channel", self.channel_combo)
        mode_lay.addRow("X window", self.window_combo)
        mode_lay.addRow(self.map_check)
        mode_lay.addRow(self.trails_check)
        mode_lay.addRow(self.tower_check)
        mode_lay.addRow(self.peaks_check)
        side.addWidget(mode_box)

        # --- referencia de qualy ---
        self.ref_box = QGroupBox("Quali: target lap")
        ref_lay = QFormLayout(self.ref_box)
        self.ref_driver_combo = QComboBox()
        self.ref_lap_combo = QComboBox()
        ref_btns = QHBoxLayout()
        self.ref_set_btn = QPushButton("Set")
        self.ref_clear_btn = QPushButton("Clear")
        ref_btns.addWidget(self.ref_set_btn)
        ref_btns.addWidget(self.ref_clear_btn)
        ref_lay.addRow("Driver", self.ref_driver_combo)
        ref_lay.addRow("Lap", self.ref_lap_combo)
        ref_lay.addRow(ref_btns)
        self.ref_box.setVisible(False)
        side.addWidget(self.ref_box)

        side_widget = QWidget()
        side_widget.setLayout(side)
        side_widget.setFixedWidth(300)
        layout.addWidget(side_widget)

        self.stack = QStackedWidget()
        for chart in self.charts:
            self.stack.addWidget(chart)
        # panel derecho: torre arriba, mapa debajo
        self.right_split = QSplitter(Qt.Vertical)
        self.right_split.addWidget(self.tower)
        self.right_split.addWidget(self.track_map)
        self.right_split.setStretchFactor(0, 1)
        self.right_split.setStretchFactor(1, 1)
        self.right_split.setMinimumWidth(300)
        self.splitter = QSplitter(Qt.Horizontal)
        self.splitter.addWidget(self.stack)
        self.splitter.addWidget(self.right_split)
        self.splitter.setStretchFactor(0, 3)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setCollapsible(1, False)
        self.splitter.setSizes([950, 340])
        self.track_map.setVisible(self.map_check.isChecked())
        self.tower.setVisible(self.tower_check.isChecked())
        self.right_split.setVisible(
            self.map_check.isChecked() or self.tower_check.isChecked()
        )

        # línea de tiempo del replay: avanzar/retroceder en toda la tanda
        center = QWidget()
        center_lay = QVBoxLayout(center)
        center_lay.setContentsMargins(0, 0, 0, 0)
        center_lay.addWidget(self.splitter, stretch=1)
        self.seek_row = QWidget()
        seek_lay = QHBoxLayout(self.seek_row)
        seek_lay.setContentsMargins(0, 0, 0, 0)
        self.pause_btn = QPushButton("⏸")
        self.pause_btn.setCheckable(True)
        self.pause_btn.setFixedWidth(30)
        self.pause_btn.setToolTip("Pause / resume playback")
        seek_lay.addWidget(self.pause_btn)
        self.live_btn = QPushButton("LIVE")
        self.live_btn.setFixedWidth(48)
        self.live_btn.setToolTip("Jump to the latest captured data")
        self.live_btn.setVisible(False)
        seek_lay.addWidget(self.live_btn)
        seek_lay.addWidget(QLabel("Time:"))
        self.seek_slider = QSlider(Qt.Horizontal)
        self.seek_slider.setRange(0, 1000)
        self.lap_ruler = LapRuler(self._seek_to_time)
        mid = QVBoxLayout()
        mid.setSpacing(0)
        mid.addWidget(self.lap_ruler)
        mid.addWidget(self.seek_slider)
        seek_lay.addLayout(mid, stretch=1)
        self.time_label = QLabel("0:00 / 0:00")
        seek_lay.addWidget(self.time_label)
        self.seek_row.setVisible(False)
        center_lay.addWidget(self.seek_row)
        layout.addWidget(center, stretch=1)

        self.setCentralWidget(root)
        self.status_label = QLabel(self._source_status)
        self.statusBar().addWidget(self.status_label, 1)
        self.meta_label = QLabel("")
        self.statusBar().addPermanentWidget(self.meta_label)
        self.version_btn = QPushButton(f"v{__version__}")
        self.version_btn.setFlat(True)
        self.version_btn.setCursor(Qt.PointingHandCursor)
        self.version_btn.setToolTip("Check for updates")
        self.statusBar().addPermanentWidget(self.version_btn)

    def _wire(self) -> None:
        self.connect_btn.clicked.connect(self._toggle_connection)
        self.mode_combo.currentIndexChanged.connect(self._mode_changed)
        self.channel_combo.currentIndexChanged.connect(self._channel_changed)
        self.driver_list.itemChanged.connect(self._selection_changed)
        self.hub.driversChanged.connect(self._rebuild_driver_list)
        self.ref_set_btn.clicked.connect(self._set_reference)
        self.ref_clear_btn.clicked.connect(lambda: self.chart_qualy.set_reference(None))
        self.ref_driver_combo.currentIndexChanged.connect(self._refresh_ref_laps)
        # correlación mouse gráfico <-> mapa
        for chart in (self.chart_rolling, self.chart_wrap,
                      self.chart_qualy.chart, self.chart_timing):
            chart.hover_dist_cb = self._on_chart_hover
        self.track_map.hover_dist_cb = self._on_map_hover
        self.map_check.toggled.connect(self._map_toggled)
        self.trails_check.toggled.connect(self._trails_toggled)
        self.track_map.set_trails_enabled(self.trails_check.isChecked())
        self.peaks_check.toggled.connect(self._peaks_toggled)
        self.all_check.toggled.connect(self._select_all_toggled)
        self.window_combo.currentIndexChanged.connect(self._window_changed)
        self.seek_slider.sliderReleased.connect(self._seek_released)
        self.pause_btn.toggled.connect(self._pause_toggled)
        self.live_btn.clicked.connect(self._go_live)
        self.speed_combo.currentIndexChanged.connect(self._speed_changed)
        self.tower_check.toggled.connect(self._tower_toggled)
        if self.peaks_check.isChecked():
            self._peaks_toggled(True)
        # aplicar las ventanas guardadas y reflejar la del modo activo
        self.chart_rolling.set_window_laps(
            float(self.cfg["ui"].get("carrera_window_laps", 1.0))
        )
        self._sync_window_combo()
        self.year_spin.valueChanged.connect(self._load_schedule)
        self._load_schedule()
        self.source_combo.currentIndexChanged.connect(self._source_kind_changed)
        self._source_kind_changed(self.source_combo.currentIndex())
        self.version_btn.clicked.connect(
            lambda: run_check(self, self.cfg, silent=False)
        )

    def _source_kind_changed(self, kind: int) -> None:
        """Year/GP/Session solo aplican a Replay; Speed no aplica a Live."""
        for row in (0, 1, 2):
            self._replay_form.setRowVisible(row, kind == 1)
        self._replay_form.setRowVisible(3, kind != 2)

    # ------------------------------------------------------------- conexión

    def _toggle_connection(self) -> None:
        if self.source is not None:
            self._disconnect()
        else:
            self._connect()

    def _connect(self) -> None:
        kind = self.source_combo.currentIndex()
        speed = float(self.speed_combo.currentData())
        if kind == 0:
            source = DemoSource(speed=speed)
        elif kind == 1:
            self._save_replay_cfg()
            source = ReplaySource(
                year=self.year_spin.value(),
                gp=self._selected_gp() or "Bahrain",
                session=self.session_combo.currentText(),
                speed=speed,
            )
        elif kind == 2:
            source = LiveSource()
        else:
            # Capture: seguir el archivo más reciente grabado por el capturador
            captures = sorted(config.recordings_dir().glob("*.jsonl"),
                              key=lambda p: p.stat().st_mtime)
            if not captures:
                QMessageBox.warning(
                    self, "F1 Live Telemetry",
                    "No capture files found.\nRun the capturer first: "
                    "F1LiveTelemetry.exe --capture",
                )
                return
            source = CaptureSource(captures[-1], speed=speed)

        self.hub.reset()
        for chart in self.charts:
            chart.clear_data()
        self.chart_qualy.set_reference(None)
        self.track_map.clear_data()

        source.batch.connect(self._on_batch)
        source.positions.connect(self.hub.on_positions)
        source.corners.connect(self.hub.on_corners)
        source.tyres.connect(self.hub.on_tyres)
        source.pits.connect(self._on_pits)
        source.trackStatus.connect(self._on_track_status)
        source.weather.connect(self._on_weather)
        source.sectorYellows.connect(self.hub.on_sector_yellows)
        self.lap_ruler.set_rain([])
        self._progress = None
        self.lap_ruler.set_marks([])
        self.lap_ruler.set_pits([])
        self.lap_ruler.set_status([])
        self.pause_btn.setChecked(False)
        self.tower.clear_data()
        if isinstance(source, (ReplaySource, CaptureSource)):
            source.progress.connect(self._on_progress)
            source.seekReset.connect(self._on_seek_reset)
            source.lapMarks.connect(self.lap_ruler.set_marks)
            self.seek_row.setVisible(True)
        else:
            self.seek_row.setVisible(False)
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
        if self.source is None:
            return
        source, self.source = self.source, None
        source.stop()
        source.wait(8000)
        source.deleteLater()
        self.connect_btn.setText("Connect")
        self.source_combo.setEnabled(True)
        self.seek_row.setVisible(False)
        self.live_btn.setVisible(False)
        self._on_source_status("Disconnected. Received data remains available.")

    def _on_source_finished(self) -> None:
        if self.source is not None and self.source.isFinished():
            # la fuente terminó sola (error fatal en la carga, etc.)
            source, self.source = self.source, None
            source.deleteLater()
            self.connect_btn.setText("Connect")
            self.source_combo.setEnabled(True)
            self.seek_row.setVisible(False)
            self.live_btn.setVisible(False)

    # ------------------------------------------------- línea de tiempo

    @staticmethod
    def _fmt_mmss(seconds: float) -> str:
        seconds = max(0.0, seconds)
        return f"{int(seconds // 60)}:{int(seconds % 60):02d}"

    def _seek_to_time(self, t: float) -> None:
        if isinstance(self.source, (ReplaySource, CaptureSource)):
            self.source.request_seek(t)

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

    def _on_map_hover(self, dist) -> None:
        """Hover en el mapa -> línea de referencia en el gráfico activo."""
        self.charts[self.stack.currentIndex()].show_track_marker(dist)

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
        for t, _air, _track, _wind, rain in self.hub.weather:
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
        self.track_map.clear_data()
        self.tower.clear_data()
        self.chart_qualy.clear_stream_data()

    def _on_batch(self, samples: list) -> None:
        self.hub.on_batch(samples)

    def _on_source_status(self, text: str) -> None:
        self._source_status = text
        self.status_label.setText(text)

    def _on_source_failed(self, text: str) -> None:
        self._on_source_status(text)
        QMessageBox.warning(self, "F1 Live Telemetry", text)

    # ------------------------------------------------- calendario del año

    def _selected_gp(self) -> str:
        """Nombre del evento elegido (o el texto tipeado a mano)."""
        idx = self.gp_combo.currentIndex()
        if idx >= 0 and self.gp_combo.currentText() == self.gp_combo.itemText(idx):
            return str(self.gp_combo.itemData(idx))
        return self.gp_combo.currentText().strip()

    def _load_schedule(self) -> None:
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
            pix = QPixmap(12, 12)
            pix.fill(QColor(info.color))
            item.setIcon(QIcon(pix))
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if info.number in checked else Qt.Unchecked)
            self.driver_list.addItem(item)
        self.driver_list.blockSignals(False)

        ref_current = self.ref_driver_combo.currentData()
        self.ref_driver_combo.blockSignals(True)
        self.ref_driver_combo.clear()
        for info in drivers:
            self.ref_driver_combo.addItem(info.label, info.number)
        if ref_current is not None:
            idx = self.ref_driver_combo.findData(ref_current)
            if idx >= 0:
                self.ref_driver_combo.setCurrentIndex(idx)
        self.ref_driver_combo.blockSignals(False)
        self._selection_changed()

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
        for chart in self.charts:
            chart.set_selected(selected)
        self.track_map.set_selected(selected)
        # reflejar el estado sin disparar el toggle
        self.all_check.blockSignals(True)
        self.all_check.setChecked(
            self.driver_list.count() > 0 and len(selected) == self.driver_list.count()
        )
        self.all_check.blockSignals(False)

    # ----------------------------------------------------------- modo/canal

    def _mode_changed(self, index: int) -> None:
        self.stack.setCurrentIndex(index)
        self.ref_box.setVisible(index == 2)
        if index == 2:
            self._refresh_ref_laps()
        self._sync_window_combo()

    # ------------------------------------------------------- ventana X

    _WINDOW_CFG_KEY = {0: "carrera_window_laps", 3: "gap_window_laps"}

    def _sync_window_combo(self) -> None:
        """El combo refleja y edita la ventana del modo activo; en los modos
        de pista fija (Carrera 2, Qualy) queda deshabilitado."""
        mode = self.mode_combo.currentIndex()
        key = self._WINDOW_CFG_KEY.get(mode)
        self.window_combo.blockSignals(True)
        if key is None:
            self.window_combo.setEnabled(False)
            self.window_combo.setToolTip("This mode has a fixed 1-lap window")
        else:
            self.window_combo.setEnabled(True)
            self.window_combo.setToolTip("")
            default = 1.0 if mode == 0 else 0.0
            value = float(self.cfg["ui"].get(key, default))
            idx = self.window_combo.findData(value)
            if idx >= 0:
                self.window_combo.setCurrentIndex(idx)
        self.window_combo.blockSignals(False)

    def _window_changed(self) -> None:
        mode = self.mode_combo.currentIndex()
        key = self._WINDOW_CFG_KEY.get(mode)
        if key is None:
            return
        laps = float(self.window_combo.currentData())
        self.cfg.setdefault("ui", {})[key] = laps
        config.save_config(self.cfg)
        if mode == 0:
            self.chart_rolling.set_window_laps(laps)
        else:
            self.chart_timing.set_window_laps(laps)

    def _channel_changed(self) -> None:
        channel = self.channel_combo.currentData()
        for chart in self.charts:
            chart.set_channel(channel)

    # ---------------------------------------------------------------- qualy

    def _refresh_ref_laps(self) -> None:
        if not self.ref_box.isVisible():
            return
        drv = self.ref_driver_combo.currentData()
        if drv is None:
            return
        buf = self.hub.buffers.get(drv)
        laps = buf.completed_laps() if buf else []
        current = self.ref_lap_combo.currentData()
        if laps == [self.ref_lap_combo.itemData(i) for i in range(self.ref_lap_combo.count())]:
            return
        self.ref_lap_combo.blockSignals(True)
        self.ref_lap_combo.clear()
        for lap in laps:
            lap_time = self.chart_timing.analyzer.lap_time(drv, lap)
            self.ref_lap_combo.addItem(f"Lap {lap} — {fmt_laptime(lap_time)}", lap)
        if current is not None:
            idx = self.ref_lap_combo.findData(current)
            if idx >= 0:
                self.ref_lap_combo.setCurrentIndex(idx)
        self.ref_lap_combo.blockSignals(False)

    def _set_reference(self) -> None:
        drv = self.ref_driver_combo.currentData()
        lap = self.ref_lap_combo.currentData()
        if drv is None or lap is None:
            QMessageBox.information(
                self, "F1 Live Telemetry",
                "That driver has no completed laps to use as target yet.",
            )
            return
        self.chart_qualy.set_reference(drv, int(lap))

    def _map_toggled(self, on: bool) -> None:
        self.track_map.setVisible(on)
        self.cfg.setdefault("ui", {})["show_map"] = on
        config.save_config(self.cfg)
        self._sync_right_panel()

    def _trails_toggled(self, on: bool) -> None:
        self.track_map.set_trails_enabled(on)
        self.cfg.setdefault("ui", {})["show_trails"] = on
        config.save_config(self.cfg)

    def _tower_toggled(self, on: bool) -> None:
        self.tower.setVisible(on)
        self.cfg.setdefault("ui", {})["show_tower"] = on
        config.save_config(self.cfg)
        self._sync_right_panel()

    def _sync_right_panel(self) -> None:
        self.right_split.setVisible(
            self.map_check.isChecked() or self.tower_check.isChecked()
        )

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

    def _tick(self) -> None:
        self._tick_n += 1
        chart = self.charts[self.stack.currentIndex()]
        chart.refresh()
        if self.track_map.isVisible():
            self.track_map.refresh()
        if self.tower.isVisible() and self._tick_n % 15 == 0:
            self.tower.refresh()
        meta = f"Lap: {self.hub.track_length:,.0f} m  ·  Samples: {self.hub.total_samples:,}"
        weather = self.hub.weather_at(self.hub.latest_t)
        if weather is not None:
            _t, air, track, wind, rain = weather
            meta = (f"Air {air:.0f}°  ·  Track {track:.0f}°  ·  Wind {wind:.1f} m/s"
                    + ("  ·  RAIN" if rain else "") + "  |  " + meta)
        self.meta_label.setText(meta)

    def closeEvent(self, event) -> None:
        self._disconnect()
        config.save_config(self.cfg)
        super().closeEvent(event)
