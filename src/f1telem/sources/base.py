"""Interfaz común de las fuentes de datos.

Cada fuente corre en su propio QThread y emite lotes de Sample por señal.
La GUI conecta estas señales al DataHub (conexión en cola automática).
"""
from __future__ import annotations

from PySide6.QtCore import QThread, Signal


class BaseSource(QThread):
    batch = Signal(list)              # list[Sample]
    positions = Signal(list)          # list[(driver, t, x, y)] para el mapa
    driversDiscovered = Signal(dict)  # dict[str, DriverInfo]
    statusChanged = Signal(str)
    trackLength = Signal(float)       # longitud exacta de vuelta en metros
    trackOutline = Signal(object)     # (xs, ys) trazado del circuito
    corners = Signal(object)          # [(etiqueta, dist, x, y)] curvas reales
    tyres = Signal(object)            # {driver: {vuelta: (compuesto, edad)}}
    pits = Signal(object)             # {driver: [(vuelta, t_entrada)]}
    trackStatus = Signal(object)      # [(t0, t1, código)] banderas/SC (t1=inf abierto)
    weather = Signal(object)          # [(t, t_aire, t_pista, viento_ms, lluvia)]
    sectorYellows = Signal(object)    # [(t0, t1, d0, d1)] amarillas por sector
    sectorTimes = Signal(object)      # [(driver, vuelta, sector 0|1, segundos)] oficiales
    segmentStatus = Signal(object)    # [(driver, sector, µsector, estado)] feed oficial
    pitLane = Signal(object)          # {driver: [[vuelta, t_in, t_out|None]]} visitas a boxes
    raceControl = Signal(object)      # [dict] mensajes de dirección de carrera (lista completa)
    sessionClock = Signal(object)     # (t_rel, restante_s, extrapolando) reloj de sesión
    lapCount = Signal(object)         # (vuelta_actual, total) en carreras
    sessionMeta = Signal(object)      # {"type", "meeting", "name"} de SessionInfo
    retirements = Signal(object)      # [driver, ...] retirados oficiales (Retired)
    failed = Signal(str)

    def set_speed(self, speed: float) -> None:
        """Cambio de velocidad en caliente (demo/replay)."""

    def set_paused(self, paused: bool) -> None:
        """Pausa/reanuda la reproducción (demo/replay)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._running = False

    def start(self, *args, **kwargs) -> None:  # type: ignore[override]
        self._running = True
        super().start(*args, **kwargs)

    def stop(self) -> None:
        self._running = False
