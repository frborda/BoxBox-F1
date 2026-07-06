"""Modelos de datos compartidos por fuentes, hub y gráficos."""
from __future__ import annotations

from dataclasses import dataclass

# Canales graficables: clave -> (etiqueta eje Y, rango Y fijo o None para auto)
CHANNELS: dict[str, tuple[str, tuple[float, float] | None]] = {
    "speed": ("Speed (km/h)", None),
    "throttle": ("Throttle (%)", (0.0, 105.0)),
    "brake": ("Brake (%)", (0.0, 105.0)),
    "rpm": ("RPM", None),
    "gear": ("Gear", (0.0, 8.5)),
}

CHANNEL_ORDER = ["speed", "throttle", "brake", "rpm", "gear"]


@dataclass(slots=True)
class Sample:
    """Una muestra de telemetría de un auto en un instante."""

    driver: str        # número de auto como texto, p.ej. "1", "44"
    t: float           # tiempo de sesión en segundos
    lap: int           # número de vuelta (1-based; 0 = antes de la primera)
    dist_lap: float    # metros recorridos dentro de la vuelta actual
    dist_total: float  # metros acumulados en toda la sesión
    speed: float       # km/h
    throttle: float    # 0-100
    brake: float       # 0-100
    rpm: float
    gear: int
    drs: int           # valor crudo del canal DRS


@dataclass(slots=True)
class DriverInfo:
    number: str
    code: str          # TLA, p.ej. "VER"
    name: str = ""
    team: str = ""
    color: str = "#9aa0a6"

    @property
    def label(self) -> str:
        base = f"{self.code or self.number}"
        return f"{base} · {self.name}" if self.name else base
