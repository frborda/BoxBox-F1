"""Los tres modos de gráfico.

- RollingChart ("Carrera"): X = distancia acumulada; ventana deslizante de
  exactamente una vuelta que termina en el auto más adelantado.
- WrapChart ("Carrera 2"): X fijo de 0 al largo de la vuelta; cada serie
  sobrescribe ("come") su propia línea de la vuelta anterior al avanzar.
- QualyChart ("Qualy"): vuelta actual de los pilotos seleccionados contra
  una vuelta de referencia (cualquier vuelta cerrada de cualquier piloto).
"""
from __future__ import annotations

import math
import time

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QEvent, QObject, Qt

from ..hub import DataHub
from ..models import CHANNELS
from . import theme

_DASH_STYLES = [Qt.SolidLine, Qt.DashLine, Qt.DotLine, Qt.DashDotLine]


def series_pens(hub: DataHub, drivers: list[str]) -> dict[str, "pg.QtGui.QPen"]:
    """Color fijo por equipo; compañeros de equipo se distinguen por el
    estilo de trazo (codificación secundaria, no solo color)."""
    used: dict[str, int] = {}
    pens = {}
    for drv in drivers:
        info = hub.drivers.get(drv)
        color = info.color if info else "#9aa0a6"
        style = _DASH_STYLES[used.get(color, 0) % len(_DASH_STYLES)]
        used[color] = used.get(color, 0) + 1
        pens[drv] = pg.mkPen(color, width=theme.LINE_WIDTH, style=style)
    return pens


class EdgeSmoother:
    """Suaviza un valor que avanza a saltos (borde de ventana, punta de serie).

    Los datos llegan en lotes (~4-5 Hz); entre lotes se extrapola linealmente
    con la velocidad aprendida para que a 30 fps el movimiento sea continuo.
    `reset_drop`: retroceso del objetivo que se interpreta como reinicio
    (nueva sesión, o cruce de meta en ejes que se reinician por vuelta).
    """

    def __init__(self, reset_drop: float = 1e4):
        self._reset_drop = reset_drop
        self.reset()

    def reset(self) -> None:
        self._target: float | None = None
        self._t = 0.0
        self._rate = 0.0

    def update(self, target: float, now: float) -> float:
        if self._target is None or target < self._target - self._reset_drop:
            self._target, self._t, self._rate = target, now, 0.0
            return target
        if target > self._target:
            dt = now - self._t
            if dt > 1e-3:
                instant = (target - self._target) / dt
                self._rate = instant if self._rate <= 0 else 0.6 * self._rate + 0.4 * instant
            self._target, self._t = target, now
        # extrapolar como máximo ~0,6 s para no inventar futuro de más
        return self._target + self._rate * min(now - self._t, 0.6)


def find_significant_peaks(x: np.ndarray, y: np.ndarray, max_labels: int = 14):
    """Picos significativos de una serie: máximos locales (final de recta) y
    mínimos (vértice de curva), filtrados por prominencia y separación.
    Devuelve [(x, y, es_máximo)] ordenado por prominencia descendente."""
    from scipy.signal import find_peaks

    ok = np.isfinite(y)
    if int(ok.sum()) < 16:
        return []
    xv = np.asarray(x, dtype=float)[ok]
    yv = np.asarray(y, dtype=float)[ok]
    y_range = float(yv.max() - yv.min())
    if y_range <= 0:
        return []
    prominence = y_range * 0.08
    dx = float(np.median(np.diff(xv))) if len(xv) > 1 else 1.0
    distance = max(1, int(120.0 / dx)) if dx > 0 else 1  # ≥120 m entre picos
    found = []
    for is_max, arr in ((True, yv), (False, -yv)):
        idx, props = find_peaks(arr, prominence=prominence, distance=distance)
        for i, prom in zip(idx, props["prominences"]):
            found.append((float(prom), float(xv[i]), float(yv[i]), is_max))
    found.sort(reverse=True)
    return [(px, py, is_max) for _prom, px, py, is_max in found[:max_labels]]


def legend_set_dim(legend: pg.LegendItem, curve, dim: bool) -> None:
    """Atenúa la entrada de leyenda de una serie oculta."""
    for sample, label in legend.items:
        if getattr(sample, "item", None) is curve:
            opacity = 0.35 if dim else 1.0
            sample.setOpacity(opacity)
            label.setOpacity(opacity)
            break


class DoubleClickHider(QObject):
    """Doble click sobre una línea la oculta; doble click en zona vacía
    vuelve a mostrar todas las ocultas."""

    HIT_PX = 12.0  # radio de acierto en píxeles

    def __init__(self, plot: pg.PlotWidget, curves_provider, on_changed=None):
        super().__init__(plot)
        self.plot = plot
        self.provider = curves_provider  # () -> dict[clave, PlotDataItem]
        self.on_changed = on_changed
        self.hidden: set = set()
        plot.viewport().installEventFilter(self)

    def eventFilter(self, obj, event) -> bool:
        if event.type() == QEvent.MouseButtonDblClick and event.button() == Qt.LeftButton:
            self.handle_double_click(self.plot.mapToScene(event.position().toPoint()))
            return True
        return False

    def handle_double_click(self, scene_pos) -> None:
        key = self._nearest(scene_pos)
        if key is not None:
            self.hidden.add(key)
        elif self.hidden:
            self.hidden.clear()
        else:
            return
        self.apply()

    def prune(self, valid_keys) -> None:
        self.hidden &= set(valid_keys)
        self.apply()

    def apply(self) -> None:
        for key, curve in self.provider().items():
            curve.setVisible(key not in self.hidden)
        if self.on_changed:
            self.on_changed(self.hidden)

    def _nearest(self, scene_pos):
        vb = self.plot.getViewBox()
        if not vb.sceneBoundingRect().contains(scene_pos):
            return None
        p = vb.mapSceneToView(scene_pos)
        psx, psy = vb.viewPixelSize()
        psx = max(abs(psx), 1e-12)
        psy = max(abs(psy), 1e-12)
        best, best_d = None, self.HIT_PX
        for key, curve in self.provider().items():
            if not curve.isVisible():
                continue
            xd, yd = curve.getData()
            if xd is None or not len(xd):
                continue
            ok = np.isfinite(yd)
            if not ok.any():
                continue
            d2 = ((xd[ok] - p.x()) / psx) ** 2 + ((yd[ok] - p.y()) / psy) ** 2
            dmin = math.sqrt(float(d2.min()))
            if dmin < best_d:
                best_d, best = dmin, key
        return best


class HoverProbe(QObject):
    """Crosshair + tooltip: al mover el mouse sobre el gráfico muestra el
    valor de todas las series visibles interpolado en ese X."""

    def __init__(self, plot: pg.PlotWidget, series_provider, x_format, y_format,
                 hover_cb=None):
        super().__init__(plot)
        self.plot = plot
        self.provider = series_provider  # () -> [(etiqueta, color, x_arr, y_arr)]
        self.x_format = x_format
        self.y_format = y_format
        self.hover_cb = hover_cb  # recibe la X del mouse (None al salir)
        self.rows: list[tuple[str, float]] = []  # última lectura (para tests)

        self.vline = pg.InfiniteLine(
            angle=90, movable=False,
            pen=pg.mkPen(theme.TEXT_MUTED, width=1, style=Qt.DashLine),
        )
        self.vline.setZValue(1e6 - 1)
        self.vline.setVisible(False)
        plot.addItem(self.vline, ignoreBounds=True)
        self.label = pg.TextItem(
            anchor=(0, 1), fill=pg.mkBrush("#191c23ee"), border=pg.mkPen(theme.BORDER)
        )
        self.label.setZValue(1e6)
        self.label.setVisible(False)
        plot.addItem(self.label, ignoreBounds=True)

        plot.scene().sigMouseMoved.connect(self._on_move)
        plot.viewport().installEventFilter(self)

    def eventFilter(self, obj, event) -> bool:
        if event.type() == QEvent.Leave:
            self._hide()
        return False

    def _hide(self) -> None:
        self.vline.setVisible(False)
        self.label.setVisible(False)
        if self.hover_cb is not None:
            self.hover_cb(None)

    def _on_move(self, scene_pos) -> None:
        vb = self.plot.getViewBox()
        if not vb.sceneBoundingRect().contains(scene_pos):
            self._hide()
            return
        view_pos = vb.mapSceneToView(scene_pos)
        x = float(view_pos.x())
        if self.hover_cb is not None:
            self.hover_cb(x)
        rows: list[tuple[str, str, float]] = []
        for label, color, xd, yd in self.provider():
            if xd is None or len(xd) < 2 or not (xd[0] <= x <= xd[-1]):
                continue
            y = float(np.interp(x, xd, yd))
            if math.isfinite(y):
                rows.append((label, color, y))
        self.rows = [(lbl, y) for lbl, _c, y in rows]
        if not rows:
            self._hide()
            return
        parts = [f'<div style="color:{theme.TEXT_MUTED}">{self.x_format(x)}</div>']
        parts += [
            f'<div><span style="color:{color}">▍</span>'
            f'<span style="color:{theme.TEXT}"> {label} </span>'
            f'<b style="color:{theme.TEXT}">{self.y_format(y)}</b></div>'
            for label, color, y in rows
        ]
        self.label.setHtml("".join(parts))
        (x0, x1), (y0, y1) = vb.viewRange()
        anchor_x = 1.0 if x > (x0 + x1) / 2 else 0.0
        anchor_y = 0.0 if view_pos.y() > (y0 + y1) / 2 else 1.0
        self.label.setAnchor((anchor_x, anchor_y))
        self.label.setPos(x, float(view_pos.y()))
        self.vline.setPos(x)
        self.vline.setVisible(True)
        self.label.setVisible(True)


class BaseChart(pg.PlotWidget):
    x_label = "Distance (m)"

    def __init__(self, hub: DataHub, parent=None):
        super().__init__(parent)
        self.hub = hub
        self.channel = "speed"
        self.selected: list[str] = []
        self.curves: dict[str, pg.PlotDataItem] = {}
        self.labels: dict[str, pg.TextItem] = {}

        self.setMenuEnabled(False)
        self.showGrid(x=True, y=True, alpha=theme.GRID_ALPHA)
        self.getAxis("bottom").setLabel(self.x_label)
        self.legend = self.addLegend(offset=(10, 10), labelTextColor=theme.TEXT)
        self._apply_channel_axis()
        self._probe = HoverProbe(
            self, self._hover_series,
            x_format=lambda x: f"{x:,.0f} m",
            y_format=self._format_value,
            hover_cb=self._probe_hover,
        )
        # correlación con el mapa: la ventana principal setea el callback y
        # el marcador vertical se posiciona desde el hover sobre el mapa
        self.hover_dist_cb = None
        self._track_marker = pg.InfiniteLine(
            angle=90, movable=False,
            pen=pg.mkPen(theme.ACCENT, width=1, style=Qt.DashLine),
        )
        self._track_marker.setZValue(45)
        self._track_marker.setVisible(False)
        self.addItem(self._track_marker, ignoreBounds=True)
        self.hider = DoubleClickHider(self, self._clickable_curves, self._on_hidden_changed)
        self._peaks_enabled = False
        self._peak_pool: list[pg.TextItem] = []
        # punta suavizada por serie: entre lotes de muestras se extrapola el
        # último tramo para que la línea crezca continua, no a tirones
        self._tips: dict[str, pg.PlotDataItem] = {}
        self._tip_sm: dict[str, EdgeSmoother] = {}
        self._last_state: dict[str, tuple[float, float, float]] = {}  # x, y, pendiente
        hub.driversChanged.connect(self._restyle_pens)

    # ------------------------------------------------------------- selección

    def set_selected(self, drivers: list[str]) -> None:
        self.selected = list(drivers)
        for drv in list(self.curves):
            if drv not in drivers:
                self._remove_driver(drv)
        for drv in drivers:
            if drv not in self.curves:
                self._add_driver(drv)
        self._restyle_pens()
        self.hider.prune(self._clickable_curves().keys())
        self.on_selection_changed()

    def _add_driver(self, drv: str) -> None:
        curve = pg.PlotDataItem(connect="finite")
        curve.setClipToView(True)
        curve.setDownsampling(auto=True, method="peak")
        self.addItem(curve)
        self.legend.addItem(curve, self._name_of(drv))
        self.curves[drv] = curve
        label = pg.TextItem(self._code_of(drv), color=theme.TEXT, anchor=(0.0, 0.5),
                            fill=pg.mkBrush(theme.SURFACE + "c0"))
        label.setVisible(False)
        self.addItem(label, ignoreBounds=True)
        self.labels[drv] = label

    def _remove_driver(self, drv: str) -> None:
        curve = self.curves.pop(drv)
        self.legend.removeItem(curve)
        self.removeItem(curve)
        self.removeItem(self.labels.pop(drv))
        tip = self._tips.pop(drv, None)
        if tip is not None:
            self.removeItem(tip)
        self._tip_sm.pop(drv, None)
        self._last_state.pop(drv, None)

    def _code_of(self, drv: str) -> str:
        info = self.hub.drivers.get(drv)
        return info.code if info else drv

    def _name_of(self, drv: str) -> str:
        return self._code_of(drv)

    def _restyle_pens(self) -> None:
        # con muchas series el antialiasing domina el costo de pintado
        antialias = len(self.selected) <= 8
        for drv, pen in series_pens(self.hub, self.selected).items():
            if drv in self.curves:
                self.curves[drv].opts["antialias"] = antialias
                self.curves[drv].setPen(pen)
                self.labels[drv].setText(self._code_of(drv))
            tip = self._tips.get(drv)
            if tip is not None:
                tip.opts["antialias"] = antialias
                tip.setPen(pen)

    def _place_label(self, drv: str, x: float, y: float) -> None:
        if drv in self.hider.hidden:
            return
        label = self.labels.get(drv)
        if label is not None and math.isfinite(x) and math.isfinite(y):
            label.setPos(x, y)
            label.setVisible(True)

    # ------------------------------------- correlación gráfico <-> mapa

    def _probe_hover(self, x: float | None) -> None:
        if self.hover_dist_cb is not None:
            self.hover_dist_cb(None if x is None else self.dist_at(float(x)))

    def dist_at(self, x: float) -> float:
        """Metro de vuelta correspondiente a una X del gráfico."""
        return min(max(x, 0.0), self.hub.track_length)

    def _marker_x(self, dist: float) -> float | None:
        """X del gráfico donde marcar un metro de vuelta (None si no cae
        dentro de la vista)."""
        return dist

    def show_track_marker(self, dist: float | None) -> None:
        if dist is None:
            self._track_marker.setVisible(False)
            return
        x = self._marker_x(float(dist))
        if x is None:
            self._track_marker.setVisible(False)
            return
        self._track_marker.setPos(x)
        self._track_marker.setVisible(True)

    # ------------------------------------------------- punta suavizada

    TIP_RESET_DROP = 1e4  # los ejes que se reinician por vuelta usan menos

    def _render_tip(self, drv: str, x_cap: float | None = None):
        """Barre el último tramo REAL de la serie de forma continua.

        La punta interpola entre las dos últimas muestras (la curva principal
        termina en la anteúltima): nunca inventa por delante del dato, así el
        último valor jamás se corrige — a cambio, el dibujo corre ~1 muestra
        (~0,25 s) detrás del dato más nuevo, imperceptible.
        Devuelve la punta mostrada (para etiqueta y borde de ventana).
        """
        state = self._last_state.get(drv)
        curve = self.curves.get(drv)
        if state is None or curve is None:
            return None
        x_prev, y_prev, x_new, y_new = state
        tip = self._tips.get(drv)
        if tip is None:
            tip = pg.PlotDataItem()
            tip.setPen(curve.opts.get("pen"))
            self.addItem(tip, ignoreBounds=True)
            self._tips[drv] = tip
        if not curve.isVisible():
            tip.setData([], [])
            return None
        sm = self._tip_sm.get(drv)
        if sm is None:
            sm = self._tip_sm[drv] = EdgeSmoother(reset_drop=self.TIP_RESET_DROP)
        x_head = sm.update(x_new, time.monotonic())
        if x_cap is not None:
            x_head = min(x_head, x_cap)
        x_head = min(max(x_head, x_prev), x_new)  # solo dentro del tramo real
        if x_new > x_prev:
            frac = (x_head - x_prev) / (x_new - x_prev)
            y_head = y_prev + (y_new - y_prev) * frac
        else:
            y_head = y_new
        if x_head > x_prev:
            tip.setData([x_prev, x_head], [y_prev, y_head])
        else:
            tip.setData([], [])
        return (x_head, y_head)

    def _store_segment(self, drv: str, x: np.ndarray, y: np.ndarray) -> None:
        """Guarda el último tramo real para que la punta lo barra."""
        if len(x) >= 2:
            self._last_state[drv] = (float(x[-2]), float(y[-2]), float(x[-1]), float(y[-1]))
        elif len(x) == 1:
            self._last_state[drv] = (float(x[-1]), float(y[-1]), float(x[-1]), float(y[-1]))

    # ----------------------------------------- ocultar series (doble click)

    def _clickable_curves(self) -> dict:
        return dict(self.curves)

    def _on_hidden_changed(self, hidden: set) -> None:
        for drv, label in self.labels.items():
            if drv in hidden:
                label.setVisible(False)
        for drv, tip in self._tips.items():
            if drv in hidden:
                tip.setData([], [])
        for key, curve in self._clickable_curves().items():
            legend_set_dim(self.legend, curve, key in hidden)

    # --------------------------------------------------- valores en picos

    MAX_PEAK_LABELS = 150  # tope global del pool de etiquetas

    def set_peaks_enabled(self, on: bool) -> None:
        self._peaks_enabled = on
        if not on:
            for item in self._peak_pool:
                item.setVisible(False)

    def _update_peak_labels(self) -> None:
        """Marca máximos (final de recta) y mínimos (vértice de curva) de
        cada serie visible con su valor en texto junto al pico."""
        used = 0
        if self._peaks_enabled:
            for _label, color, xd, yd in self._hover_series():
                if xd is None or len(xd) < 16:
                    continue
                for px, py, is_max in find_significant_peaks(xd, yd):
                    if used >= self.MAX_PEAK_LABELS:
                        break
                    if used >= len(self._peak_pool):
                        item = pg.TextItem(fill=pg.mkBrush(theme.SURFACE + "a0"))
                        item.setZValue(50)
                        self.addItem(item, ignoreBounds=True)
                        self._peak_pool.append(item)
                    item = self._peak_pool[used]
                    item.setHtml(
                        f'<span style="font-size:8pt;color:{color}">{py:.0f}</span>'
                    )
                    # máximos: texto arriba del pico; mínimos: debajo
                    item.setAnchor((0.5, 1.0) if is_max else (0.5, 0.0))
                    item.setPos(px, py)
                    item.setVisible(True)
                    used += 1
        for item in self._peak_pool[used:]:
            item.setVisible(False)

    # ------------------------------------------------------ tooltip (hover)

    _UNITS = {"speed": " km/h", "throttle": " %", "brake": " %", "rpm": "", "gear": ""}

    def _format_value(self, y: float) -> str:
        return f"{y:.0f}{self._UNITS.get(self.channel, '')}"

    def _hover_series(self) -> list[tuple[str, str, np.ndarray, np.ndarray]]:
        out = []
        for drv in self.selected:
            curve = self.curves.get(drv)
            if curve is None or not curve.isVisible():
                continue
            xd, yd = curve.getData()
            info = self.hub.drivers.get(drv)
            out.append((self._code_of(drv), info.color if info else "#9aa0a6", xd, yd))
        return out

    # --------------------------------------------------------------- canales

    def set_channel(self, channel: str) -> None:
        if channel not in CHANNELS or channel == self.channel:
            return
        self.channel = channel
        self._apply_channel_axis()
        self.on_channel_changed()

    def _apply_channel_axis(self) -> None:
        label, yrange = CHANNELS[self.channel]
        self.getAxis("left").setLabel(label)
        if yrange:
            self.setYRange(*yrange, padding=0)
        else:
            self.enableAutoRange(axis="y")

    # ------------------------------------------------- puntos de extensión

    def refresh(self) -> None:  # llamado por timer de la GUI (~10 Hz)
        raise NotImplementedError

    def on_channel_changed(self) -> None:
        pass

    def on_selection_changed(self) -> None:
        pass

    def clear_data(self) -> None:
        for curve in self.curves.values():
            curve.setData([], [])
        for label in self.labels.values():
            label.setVisible(False)
        for tip in self._tips.values():
            tip.setData([], [])
        self._last_state.clear()
        for sm in self._tip_sm.values():
            sm.reset()


class RollingChart(BaseChart):
    """Modo Carrera: ventana deslizante configurable en vueltas (0 = todo).

    El eje X es la posición de pista: (vuelta − 1) × largo + metro dentro
    de la vuelta. Como el metro de vuelta se re-ancla en cada cruce de
    meta, la misma curva del circuito cae en el mismo X para todos los
    autos (misma frenada = misma vertical), sin la deriva que acumula la
    distancia integrada de cada auto.
    """

    x_label = "Track position (m)"

    # espacio libre a la derecha para que los últimos valores y la etiqueta
    # de cada serie queden visibles y no pegados al borde
    RIGHT_MARGIN_FRAC = 0.08

    # cota inicial de muestras a procesar por serie y refresco: cubre de
    # sobra una vuelta y mantiene el costo constante en sesiones largas
    TAIL_SAMPLES = 4096

    def __init__(self, hub: DataHub, parent=None):
        super().__init__(hub, parent)
        self.window_laps = 1.0
        self._seen_n: dict[str, int] = {}
        self._tick_n = 0

    def set_window_laps(self, laps: float) -> None:
        self.window_laps = max(0.0, float(laps))

    def clear_data(self) -> None:
        super().clear_data()
        self._seen_n.clear()

    def on_channel_changed(self) -> None:
        self._seen_n.clear()  # mismo largo de datos pero hay que redibujar Y

    def dist_at(self, x: float) -> float:
        L = self.hub.track_length
        return float(x) % L if L > 0 else 0.0

    def _marker_x(self, dist: float) -> float | None:
        # X es posición acumulada: elegir la vuelta que cae dentro de la vista
        L = self.hub.track_length
        if L <= 0:
            return None
        x0, x1 = self.getViewBox().viewRange()[0]
        x = (math.floor((x0 - dist) / L) + 1) * L + dist
        return x if x0 <= x <= x1 else None

    def refresh(self) -> None:
        L = self.hub.track_length
        window = L * self.window_laps
        # el redibujado de datos ocurre solo cuando hay muestras nuevas (y
        # con ventanas grandes, como mucho a 2 Hz); la punta de cada serie y
        # el paneo de la ventana se extrapolan suaves en todos los ticks
        big = self.window_laps <= 0 or self.window_laps > 3
        self._tick_n += 1
        allow_data = (not big) or (self._tick_n % 15 == 0)
        data_changed = False
        edge = 0.0
        for drv in self.selected:
            buf = self.hub.buffers.get(drv)
            curve = self.curves[drv]
            if buf is None or not buf.n:
                curve.setData([], [])
                continue
            if allow_data and buf.n != self._seen_n.get(drv):
                tail = buf.n if self.window_laps <= 0 else self.TAIL_SAMPLES
                while True:
                    j0 = max(0, buf.n - tail)
                    lap = buf.col("lap")[j0:].astype(np.float64)
                    d = np.minimum(buf.col("dist_lap")[j0:].astype(np.float64), L)
                    x = (lap - 1.0) * L + d
                    if j0 == 0 or self.window_laps <= 0:
                        break
                    if float(x[-1] - x[0]) >= window * 1.05:
                        break
                    tail *= 2  # la cola no cubre la ventana pedida: ampliar
                y = buf.col(self.channel)[j0:]
                i0 = 0
                if self.window_laps > 0:
                    i0 = int(np.searchsorted(x, x[-1] - window * 1.05))
                # la curva termina en la anteúltima muestra: el tramo final
                # lo barre la punta suavizada sin correcciones
                end = len(x) - 1 if len(x) - i0 >= 2 else len(x)
                curve.setData(x[i0:end], y[i0:end])
                self._store_segment(drv, x, y)
                self._seen_n[drv] = buf.n
                data_changed = True
            tip = self._render_tip(drv)
            if tip is None:
                continue
            self._place_label(drv, tip[0], tip[1])
            if curve.isVisible():  # la ventana sigue solo a los autos visibles
                edge = max(edge, tip[0])
        if edge > 0.0:
            if self.window_laps > 0:
                self.setXRange(edge - window, edge + window * self.RIGHT_MARGIN_FRAC, padding=0)
            else:
                self.setXRange(0.0, edge + L * self.RIGHT_MARGIN_FRAC, padding=0)
        if data_changed:
            self._update_peak_labels()


class WrapChart(BaseChart):
    """Modo Carrera 2: X fijo [0, vuelta]; cada serie come su vuelta anterior."""

    BIN = 4.0        # metros por bin
    GAP_M = 120.0    # hueco visible delante del cabezal
    TIP_RESET_DROP = 500.0  # el eje X se reinicia en cada vuelta

    def __init__(self, hub: DataHub, parent=None):
        super().__init__(hub, parent)
        self._n_bins = 0
        self._xs = np.zeros(0)
        self._bins: dict[str, np.ndarray] = {}
        self._head: dict[str, int | None] = {}
        self._cursor: dict[str, int] = {}
        self._render_bin: dict[str, int] = {}
        hub.trackLengthChanged.connect(lambda _m: self._rebuild())

    def _rebuild(self) -> None:
        self._n_bins = max(10, int(math.ceil(self.hub.track_length / self.BIN)))
        self._xs = np.arange(self._n_bins) * self.BIN
        self._bins.clear()
        self._head.clear()
        self._cursor.clear()
        self._render_bin.clear()
        self.setXRange(0.0, self.hub.track_length, padding=0.01)

    def on_channel_changed(self) -> None:
        self._rebuild()

    def on_selection_changed(self) -> None:
        if not self._n_bins:
            self._rebuild()

    def _state_for(self, drv: str) -> np.ndarray:
        ys = self._bins.get(drv)
        if ys is None:
            ys = self._bins[drv] = np.full(self._n_bins, np.nan)
            self._head[drv] = None
            buf = self.hub.buffers.get(drv)
            # rellenar solo desde el inicio de la vuelta actual
            self._cursor[drv] = buf.lap_start_index(buf.current_lap()) if buf and buf.n else 0
        return ys

    def refresh(self) -> None:
        if not self._n_bins:
            self._rebuild()
        n_bins = self._n_bins
        gap_bins = max(1, int(self.GAP_M / self.BIN))
        data_changed = False
        for drv in self.selected:
            buf = self.hub.buffers.get(drv)
            curve = self.curves[drv]
            if buf is None or not buf.n:
                curve.setData([], [])
                continue
            ys = self._state_for(drv)
            cur = self._cursor[drv]
            new_data = buf.n > cur
            if new_data:
                dist = buf.col("dist_lap")[cur:]
                vals = buf.col(self.channel)[cur:]
                head = self._head[drv]
                for i in range(len(dist)):
                    b = int(max(0.0, dist[i]) // self.BIN) % n_bins
                    val = float(vals[i])
                    if head is None or b == head:
                        ys[b] = val
                    else:
                        self._fill(ys, head, b, val, n_bins)
                    head = b
                self._head[drv] = head
                self._cursor[drv] = buf.n
                data_changed = True
            head = self._head[drv]
            if head is None:
                curve.setData([], [])
                continue
            # cabezal de render suavizado: barre los bins nuevos de forma
            # continua (nunca por delante del dato); lo aún no barrido queda
            # oculto dentro del hueco
            sm = self._tip_sm.get(drv)
            if sm is None:
                sm = self._tip_sm[drv] = EdgeSmoother(reset_drop=self.TIP_RESET_DROP)
            x_render = min(sm.update(float(self._xs[head]), time.monotonic()),
                           float(self._xs[head]))
            b_render = min(n_bins - 1, max(0, int(x_render // self.BIN)))
            if not new_data and b_render == self._render_bin.get(drv):
                continue  # nada cambió a la vista
            self._render_bin[drv] = b_render
            display = ys.copy()
            gap_idx = (np.arange(b_render + 1, b_render + 1 + gap_bins)) % n_bins
            display[gap_idx] = np.nan
            curve.setData(self._xs, display, connect="finite")
            if math.isfinite(ys[b_render]):
                self._place_label(drv, float(self._xs[b_render]), float(ys[b_render]))
        if data_changed:
            self._update_peak_labels()

    @staticmethod
    def _fill(ys: np.ndarray, b0: int, b1: int, val: float, n_bins: int) -> None:
        """Rellena (b0, b1] interpolando linealmente, con vuelta circular."""
        start = float(ys[b0]) if math.isfinite(ys[b0]) else val
        count = (b1 - b0) % n_bins
        if count <= 0 or count > n_bins // 2:
            # salto hacia atrás o glitch: escribir solo el punto nuevo
            ys[b1] = val
            return
        vals = np.linspace(start, val, count + 1)[1:]
        idx = (np.arange(b0 + 1, b0 + 1 + count)) % n_bins
        ys[idx] = vals


class QualyChart(BaseChart):
    """Modo Qualy: vuelta actual en vivo contra una vuelta de referencia."""

    x_label = "Lap distance (m)"
    TIP_RESET_DROP = 500.0  # el eje X se reinicia en cada vuelta

    def __init__(self, hub: DataHub, parent=None):
        super().__init__(hub, parent)
        self.reference: tuple[str, int] | None = None
        self._ref_data: dict[str, np.ndarray] | None = None
        self._seen_n: dict[str, int] = {}
        self._ref_curve = pg.PlotDataItem(connect="finite")
        self.addItem(self._ref_curve)
        hub.trackLengthChanged.connect(lambda _m: self._apply_x_range())
        self._apply_x_range()

    def _apply_x_range(self) -> None:
        self.setXRange(0.0, self.hub.track_length, padding=0.01)

    def set_reference(self, driver: str | None, lap: int = 0) -> None:
        if self._ref_data is not None:
            self.legend.removeItem(self._ref_curve)
        self._ref_curve.setData([], [])
        self.reference = None
        self._ref_data = None
        if driver is None:
            return
        buf = self.hub.buffers.get(driver)
        if buf is None:
            return
        data = buf.lap_slice(lap)
        if not len(data["t"]):
            return
        self.reference = (driver, lap)
        self.hider.hidden.discard("__ref__")
        self._ref_curve.setVisible(True)
        # copia: el buffer sigue creciendo pero la referencia queda congelada
        self._ref_data = {k: v.copy() for k, v in data.items()}
        info = self.hub.drivers.get(driver)
        color = info.color if info else "#9aa0a6"
        self._ref_curve.setPen(pg.mkPen(color, width=theme.LINE_WIDTH, style=Qt.DashLine))
        self.legend.addItem(self._ref_curve, f"{self._code_of(driver)} L{lap} (ref)")
        self._draw_reference()

    def _draw_reference(self) -> None:
        if self._ref_data is None:
            return
        mask = self._ref_data["dist_lap"] <= self.hub.track_length * 1.02
        self._ref_curve.setData(self._ref_data["dist_lap"][mask],
                                self._ref_data[self.channel][mask])

    def _hover_series(self) -> list[tuple[str, str, np.ndarray, np.ndarray]]:
        out = super()._hover_series()
        if self.reference is not None and self._ref_curve.isVisible():
            drv, lap = self.reference
            xd, yd = self._ref_curve.getData()
            info = self.hub.drivers.get(drv)
            out.append((f"{self._code_of(drv)} L{lap} ref",
                        info.color if info else "#9aa0a6", xd, yd))
        return out

    def _clickable_curves(self) -> dict:
        curves = dict(self.curves)
        if self.reference is not None:
            curves["__ref__"] = self._ref_curve
        return curves

    def on_channel_changed(self) -> None:
        self._draw_reference()
        self._seen_n.clear()

    def clear_data(self) -> None:
        super().clear_data()
        self._seen_n.clear()

    def refresh(self) -> None:
        data_changed = False
        for drv in self.selected:
            buf = self.hub.buffers.get(drv)
            curve = self.curves[drv]
            if buf is None or not buf.n:
                curve.setData([], [])
                continue
            if buf.n != self._seen_n.get(drv):
                self._seen_n[drv] = buf.n
                data = buf.lap_slice(buf.current_lap())
                if not len(data["t"]):
                    curve.setData([], [])
                    continue
                x = data["dist_lap"]
                y = data[self.channel]
                end = len(x) - 1 if len(x) >= 2 else len(x)
                curve.setData(x[:end], y[:end])
                data_changed = True
                self._store_segment(drv, x, y)
            tip = self._render_tip(drv, x_cap=self.hub.track_length)
            if tip is not None:
                self._place_label(drv, tip[0], tip[1])
        if data_changed:
            self._update_peak_labels()
