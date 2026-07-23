"""Fuente demo: telemetría sintética para probar la app sin red ni sesión."""
from __future__ import annotations

import math
import random
import time

import numpy as np

from ..models import DriverInfo, Sample
from .base import BaseSource

TRACK_LEN = 5280.0  # metros


def _make_track() -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Circuito cerrado sintético: puntos (x, y) y su distancia de arco,
    escalado para que el largo total sea exactamente TRACK_LEN."""
    th = np.linspace(0.0, 2.0 * np.pi, 720, endpoint=False)
    r = 1.0 + 0.28 * np.sin(2.0 * th + 0.7) + 0.10 * np.sin(3.0 * th + 1.9) + 0.06 * np.sin(5.0 * th)
    x = r * np.cos(th)
    y = 0.72 * r * np.sin(th)
    x = np.append(x, x[0])
    y = np.append(y, y[0])
    seg = np.hypot(np.diff(x), np.diff(y))
    scale = TRACK_LEN / float(seg.sum())
    x *= scale
    y *= scale
    d = np.concatenate(([0.0], np.cumsum(seg * scale)))
    return d, x, y


_TRACK_D, _TRACK_X, _TRACK_Y = _make_track()


def track_pos(dist: float) -> tuple[float, float]:
    d = dist % TRACK_LEN
    return float(np.interp(d, _TRACK_D, _TRACK_X)), float(np.interp(d, _TRACK_D, _TRACK_Y))

# Curvas del circuito ficticio: (posición en m, velocidad objetivo km/h, ancho en m)
_CORNERS = [
    (450.0, 120.0, 90.0),
    (980.0, 205.0, 120.0),
    (1560.0, 85.0, 80.0),
    (2210.0, 150.0, 110.0),
    (2900.0, 70.0, 70.0),
    (3420.0, 180.0, 130.0),
    (4150.0, 110.0, 90.0),
    (4780.0, 230.0, 140.0),
]

_DRIVERS = [
    DriverInfo("1", "VER", "Max Verstappen", "Red Bull", "#3671C6"),
    DriverInfo("4", "NOR", "Lando Norris", "McLaren", "#FF8000"),
    DriverInfo("16", "LEC", "Charles Leclerc", "Ferrari", "#E80020"),
    DriverInfo("44", "HAM", "Lewis Hamilton", "Mercedes", "#27F4D2"),
    DriverInfo("63", "RUS", "George Russell", "Mercedes", "#27F4D2"),
    DriverInfo("14", "ALO", "Fernando Alonso", "Aston Martin", "#229971"),
]


def _target_speed(dist: float, skill: float, wobble: float) -> float:
    """Velocidad objetivo en un punto del circuito (km/h)."""
    v = 328.0
    for pos, vmin, width in _CORNERS:
        # distancia circular a la curva
        d = min(abs(dist - pos), TRACK_LEN - abs(dist - pos))
        v -= (328.0 - vmin) * math.exp(-0.5 * (d / width) ** 2)
    return max(55.0, v * skill + wobble)


class _Car:
    def __init__(self, info: DriverInfo, rng: random.Random):
        self.info = info
        # posición de pista absoluta; la grilla queda antes de la línea de
        # meta, así los cruces de vuelta caen en múltiplos de TRACK_LEN
        self.pos = -rng.uniform(10.0, 250.0)
        self.speed = 0.0
        self.skill = rng.uniform(0.965, 1.0)
        self.phase = rng.uniform(0.0, math.tau)
        self.prev_lap = 0


class DemoSource(BaseSource):
    DT = 0.2  # paso de simulación en segundos (5 Hz, similar al feed real)

    def __init__(self, speed: float = 1.0, parent=None):
        super().__init__(parent)
        self.speed = max(0.1, speed)
        self._paused = False

    def set_speed(self, speed: float) -> None:
        self.speed = max(0.1, float(speed))

    def set_paused(self, paused: bool) -> None:
        self._paused = bool(paused)

    def run(self) -> None:
        rng = random.Random(42)
        cars = [_Car(info, rng) for info in _DRIVERS]
        self.driversDiscovered.emit({c.info.number: c.info for c in cars})
        self.trackLength.emit(TRACK_LEN)
        self.trackOutline.emit((_TRACK_X, _TRACK_Y))
        self.corners.emit([
            (f"T{i + 1}", pos, *track_pos(pos)) for i, (pos, _v, _w) in enumerate(_CORNERS)
        ])
        # datos sintéticos de estrategia: blandos 4 vueltas, luego medios; y
        # una amarilla + un safety car para probar el contexto de sesión
        tyre_plan = {
            lap: (("SOFT", lap) if lap <= 4 else ("MEDIUM", lap - 4))
            for lap in range(1, 300)
        }
        self.tyres.emit({car.info.number: dict(tyre_plan) for car in cars})
        self.trackStatus.emit([(60.0, 90.0, "2"), (200.0, 250.0, "4")])
        # la amarilla sintética aplica al sector de la curva T5 (2600-3200 m)
        self.sectorYellows.emit([(60.0, 90.0, 2600.0, 3200.0)])
        self.weather.emit([
            (0.0, 26.0, 41.0, 2.5, False, 48.0, 1013.0, 210.0),
            (150.0, 24.5, 37.0, 4.2, True, 74.0, 1009.5, 195.0),  # chaparrón
            (180.0, 25.5, 39.0, 3.1, False, 60.0, 1011.0, 205.0),
        ])
        self.sessionMeta.emit(
            {"type": "Race", "meeting": "Demo Grand Prix", "name": "Race"})
        self.lapCount.emit((1, 20))
        self.sessionClock.emit((0.0, 2.0 * 3600.0, True))
        self.raceControl.emit([
            {"t": 0.0, "lap": 1, "category": "Other", "flag": "GREEN",
             "scope": "Track", "sector": None, "mode": "", "driver": "",
             "message": "GREEN LIGHT - PIT EXIT OPEN"},
            {"t": 60.0, "lap": 2, "category": "Flag", "flag": "YELLOW",
             "scope": "Sector", "sector": 5, "mode": "", "driver": "",
             "message": "YELLOW IN TRACK SECTOR 5"},
            {"t": 90.0, "lap": 2, "category": "Flag", "flag": "CLEAR",
             "scope": "Sector", "sector": 5, "mode": "", "driver": "",
             "message": "CLEAR IN TRACK SECTOR 5"},
            {"t": 200.0, "lap": 3, "category": "SafetyCar", "flag": "",
             "scope": "Track", "sector": None, "mode": "SAFETY CAR",
             "driver": "", "message": "SAFETY CAR DEPLOYED"},
            {"t": 250.0, "lap": 4, "category": "SafetyCar", "flag": "GREEN",
             "scope": "Track", "sector": None, "mode": "", "driver": "",
             "message": "SAFETY CAR IN THIS LAP"},
        ])
        self._pit_log: dict[str, list] = {}
        self._lane_log: dict[str, list] = {}
        self._lead_lap = 1
        self.statusChanged.emit(f"Demo running ({len(cars)} cars, x{self.speed:g})")

        t_sim = 0.0
        acc = 0.0
        last_wall = time.monotonic()
        while self._running:
            time.sleep(0.05)
            now = time.monotonic()
            if not self._paused:
                acc += (now - last_wall) * self.speed
            last_wall = now
            samples: list[Sample] = []
            pos_batch: list[tuple] = []
            while acc >= self.DT:
                acc -= self.DT
                t_sim += self.DT
                for car in cars:
                    self._step(car, t_sim, rng, samples)
                    px, py = track_pos(car.pos)
                    pos_batch.append((car.info.number, t_sim, px, py))
            if samples:
                self.batch.emit(samples)
            if pos_batch:
                self.positions.emit(pos_batch)

    def _step(self, car: _Car, t: float, rng: random.Random, out: list[Sample]) -> None:
        wobble = 4.0 * math.sin(t / 17.0 + car.phase) + rng.uniform(-1.5, 1.5)
        target = _target_speed(car.pos % TRACK_LEN, car.skill, wobble)
        # aceleración limitada: frena más fuerte de lo que acelera
        dv = target - car.speed
        max_up = 28.0 * self.DT * (1.0 - car.speed / 360.0)
        max_down = -62.0 * self.DT
        dv = min(max(dv, max_down), max_up * 3.6)
        car.speed = max(0.0, car.speed + dv)
        car.pos += car.speed / 3.6 * self.DT
        lap = int(math.floor(car.pos / TRACK_LEN)) + 1
        dist_in_lap = car.pos - (lap - 1) * TRACK_LEN
        if lap != car.prev_lap:
            if lap == 5:  # todos paran en la vuelta 4 (cambio SOFT→MEDIUM)
                self._pit_log.setdefault(car.info.number, []).append((4, t))
                self.pits.emit({k: list(v) for k, v in self._pit_log.items()})
                # visita sintética a boxes terminando en el cruce (los autos
                # del demo no frenan: tiempo detenido 0)
                self._lane_log.setdefault(car.info.number, []).append(
                    [4, t - 21.5, t - 0.5])
                self.pitLane.emit({k: [list(v) for v in vs]
                                   for k, vs in self._lane_log.items()})
            car.prev_lap = lap
            if lap > self._lead_lap:
                self._lead_lap = lap
                self.lapCount.emit((lap, 20))

        accel = dv / self.DT
        throttle = 100.0 if accel > 1.0 else (30.0 if abs(accel) <= 1.0 else 0.0)
        brake = 100.0 if accel < -8.0 else 0.0
        gear = min(8, max(1, int(car.speed // 42) + 1))
        rpm = 4500.0 + 8000.0 * min(1.0, car.speed / 340.0) + rng.uniform(-150, 150)
        drs = 12 if (throttle == 100.0 and car.speed > 250) else 0
        out.append(
            Sample(
                driver=car.info.number,
                t=t,
                lap=lap,
                dist_lap=dist_in_lap,
                dist_total=car.pos,
                speed=car.speed,
                throttle=throttle,
                brake=brake,
                rpm=rpm,
                gear=gear,
                drs=drs,
            )
        )
