"""Clima: valores actuales y gráfico de evolución.

El gráfico usa como eje X la vuelta que lleva el líder cuando la sesión es
una carrera, o los minutos desde el inicio de la tanda si no lo es. Arriba
las temperaturas (aire y pista, con la lluvia sombreada), abajo el viento.
"""
from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QGridLayout, QLabel, QVBoxLayout, QWidget

from ..hub import DataHub
from . import theme

AIR_COLOR = "#58a6ff"
TRACK_COLOR = "#ff9f1a"
WIND_COLOR = "#9aa0a6"
RAIN_COLOR = QColor(0, 130, 220, 40)


class WeatherNowPanel(QWidget):
    """Última lectura de clima en números grandes."""

    def __init__(self, hub: DataHub, parent=None):
        super().__init__(parent)
        self.hub = hub
        grid = QGridLayout(self)
        grid.setContentsMargins(8, 4, 8, 6)
        self._values: dict[str, QLabel] = {}
        for col, (key, title) in enumerate(
                (("air", "Air"), ("track", "Track"),
                 ("wind", "Wind"), ("hum", "Hum"),
                 ("press", "Press"), ("rain", "Rain"))):
            head = QLabel(title)
            head.setStyleSheet(f"color: {theme.TEXT_MUTED}; font-size: 8pt;")
            value = QLabel("—")
            value.setStyleSheet(
                f"color: {theme.TEXT}; font-size: 13pt; font-weight: bold;")
            grid.addWidget(head, 0, col)
            grid.addWidget(value, 1, col)
            self._values[key] = value

    def clear_data(self) -> None:
        for label in self._values.values():
            label.setText("—")

    def refresh(self) -> None:
        row = self.hub.weather_at(self.hub.latest_t)
        if row is None:
            return
        _t, air, track, wind, rain = row[:5]
        # extras (humedad %, presión mbar, dirección °): NaN si la fuente
        # no los trae — la celda queda en "—"
        hum, press, wdir = (list(row[5:8]) + [float("nan")] * 3)[:3]
        self._values["air"].setText(f"{air:.1f}°")
        self._values["track"].setText(f"{track:.1f}°")
        wind_txt = f"{wind:.1f} m/s"
        if wdir == wdir:
            wind_txt += f" · {wdir:.0f}°"
        self._values["wind"].setText(wind_txt)
        self._values["hum"].setText(f"{hum:.0f}%" if hum == hum else "—")
        self._values["press"].setText(
            f"{press:.0f} mb" if press == press else "—")
        rain_label = self._values["rain"]
        rain_label.setText("YES" if rain else "no")
        rain_label.setStyleSheet(
            "color: #58a6ff; font-size: 13pt; font-weight: bold;" if rain
            else f"color: {theme.TEXT_MUTED}; font-size: 13pt; font-weight: bold;")


class WeatherChart(QWidget):
    """Evolución de temperaturas y viento a lo largo de la sesión."""

    def __init__(self, hub: DataHub, parent=None):
        super().__init__(parent)
        self.hub = hub
        self._sig: tuple | None = None
        self._rain_items: list = []

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.glw = pg.GraphicsLayoutWidget()
        lay.addWidget(self.glw)
        self.p_temp = self.glw.addPlot(row=0, col=0)
        self.p_wind = self.glw.addPlot(row=1, col=0)
        self.glw.ci.layout.setRowStretchFactor(0, 3)
        self.glw.ci.layout.setRowStretchFactor(1, 1)
        self.p_wind.setXLink(self.p_temp)
        for plot in (self.p_temp, self.p_wind):
            plot.showGrid(x=True, y=True, alpha=theme.GRID_ALPHA)
            plot.getViewBox().setDefaultPadding(0.05)
        legend = self.p_temp.addLegend(offset=(4, 4), labelTextSize="7pt")
        self.c_air = self.p_temp.plot(
            pen=pg.mkPen(AIR_COLOR, width=2), name="Air")
        self.c_track = self.p_temp.plot(
            pen=pg.mkPen(TRACK_COLOR, width=2), name="Track")
        self.c_wind = self.p_wind.plot(pen=pg.mkPen(WIND_COLOR, width=2))
        self.p_temp.setLabel("left", "°C")
        self.p_wind.setLabel("left", "m/s")
        legend.setBrush(pg.mkBrush(theme.SURFACE))

    def clear_data(self) -> None:
        self._sig = None
        self.c_air.setData([], [])
        self.c_track.setData([], [])
        self.c_wind.setData([], [])
        for item in self._rain_items:
            self.p_temp.removeItem(item)
        self._rain_items = []

    def refresh(self) -> None:
        # sin spoilers: la evolución llega solo hasta el timeline actual
        rows = [r for r in self.hub.weather if r[0] <= self.hub.latest_t]
        if not rows:
            return
        is_race = (str(self.hub.session_meta.get("type", "")).lower() == "race"
                   or self.hub.lap_count[1] > 0)
        sig = (len(rows), is_race, int(self.hub.latest_t // 30))
        if sig == self._sig:
            return
        self._sig = sig

        t = np.array([r[0] for r in rows])
        if is_race:
            x = self.hub.leader_laps_at(t)
            self.p_wind.setLabel("bottom", "Leader lap")
        else:
            x = t / 60.0
            self.p_wind.setLabel("bottom", "Session time (min)")
        # el eje debe ser monótono para las bandas de lluvia; con pocas
        # muestras al inicio la vuelta del líder puede repetirse
        x = np.maximum.accumulate(x)
        self.c_air.setData(x, np.array([r[1] for r in rows]))
        self.c_track.setData(x, np.array([r[2] for r in rows]))
        self.c_wind.setData(x, np.array([r[3] for r in rows]))

        for item in self._rain_items:
            self.p_temp.removeItem(item)
        self._rain_items = []
        start = None
        rain_flags = [bool(r[4]) for r in rows]
        for i, rain in enumerate(rain_flags):
            if rain and start is None:
                start = float(x[i])
            elif not rain and start is not None:
                self._add_rain(start, float(x[i]))
                start = None
        if start is not None:
            self._add_rain(start, float(x[-1]))

    def _add_rain(self, x0: float, x1: float) -> None:
        item = pg.LinearRegionItem(values=(x0, max(x1, x0 + 1e-6)),
                                   movable=False,
                                   brush=pg.mkBrush(RAIN_COLOR),
                                   pen=pg.mkPen(None))
        item.setZValue(-20)
        self.p_temp.addItem(item)
        self._rain_items.append(item)
