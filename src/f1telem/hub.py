"""DataHub: almacena en memoria las series por piloto.

Todo el acceso ocurre en el hilo de la GUI: las fuentes emiten lotes por
señales Qt (conexión en cola) y los gráficos leen vistas numpy en cada
refresco, así no hacen falta locks.
"""
from __future__ import annotations

import re
import time
from collections import deque

import numpy as np
from PySide6.QtCore import QObject, Signal

from .models import DriverInfo, Sample

_FIELDS: list[tuple[str, type]] = [
    ("t", np.float64),
    ("lap", np.int32),
    ("dist_lap", np.float32),
    ("dist_total", np.float64),
    ("speed", np.float32),
    ("throttle", np.float32),
    ("brake", np.float32),
    ("rpm", np.float32),
    ("gear", np.int16),
    ("drs", np.int16),
]


class SeriesBuffer:
    """Arrays numpy crecientes con toda la telemetría de un auto."""

    def __init__(self, capacity: int = 4096):
        self.n = 0
        self._arr = {name: np.zeros(capacity, dtype=dt) for name, dt in _FIELDS}

    def _grow(self, need: int) -> None:
        cap = len(self._arr["t"])
        while cap < need:
            cap *= 2
        for key, old in self._arr.items():
            new = np.zeros(cap, dtype=old.dtype)
            new[: self.n] = old[: self.n]
            self._arr[key] = new

    def append(self, samples: list[Sample]) -> None:
        need = self.n + len(samples)
        if need > len(self._arr["t"]):
            self._grow(need)
        arr = self._arr
        i = self.n
        for s in samples:
            arr["t"][i] = s.t
            arr["lap"][i] = s.lap
            arr["dist_lap"][i] = s.dist_lap
            arr["dist_total"][i] = s.dist_total
            arr["speed"][i] = s.speed
            arr["throttle"][i] = s.throttle
            arr["brake"][i] = s.brake
            arr["rpm"][i] = s.rpm
            arr["gear"][i] = s.gear
            arr["drs"][i] = s.drs
            i += 1
        self.n = i

    def col(self, name: str) -> np.ndarray:
        return self._arr[name][: self.n]

    def current_lap(self) -> int:
        return int(self._arr["lap"][self.n - 1]) if self.n else 0

    def completed_laps(self) -> list[int]:
        """Vueltas con datos ya cerradas (todas menos la actual)."""
        if not self.n:
            return []
        laps = np.unique(self.col("lap"))
        cur = self.current_lap()
        return [int(l) for l in laps if l != cur and l > 0]

    def lap_slice(self, lap: int) -> dict[str, np.ndarray]:
        """Vistas de todos los campos para una vuelta (columna lap es no
        decreciente, así que searchsorted alcanza)."""
        lapcol = self.col("lap")
        i0 = int(np.searchsorted(lapcol, lap, side="left"))
        i1 = int(np.searchsorted(lapcol, lap, side="right"))
        return {name: self._arr[name][i0:i1] for name, _ in _FIELDS}

    def lap_start_index(self, lap: int) -> int:
        return int(np.searchsorted(self.col("lap"), lap, side="left"))


class PosBuffer:
    """Últimas posiciones (x, y) de un auto para el mapa del circuito.
    Solo hace falta cubrir la estela (~25 s); el trazado del circuito se
    acumula aparte, así el buffer se mantiene chico y barato de convertir."""

    __slots__ = ("t", "x", "y")

    def __init__(self, maxlen: int = 300):
        self.t: deque = deque(maxlen=maxlen)
        self.x: deque = deque(maxlen=maxlen)
        self.y: deque = deque(maxlen=maxlen)

    def append(self, t: float, x: float, y: float) -> None:
        self.t.append(t)
        self.x.append(x)
        self.y.append(y)

    def __len__(self) -> int:
        return len(self.t)


class DataHub(QObject):
    driversChanged = Signal()
    trackLengthChanged = Signal(float)

    DEFAULT_TRACK_LEN = 5000.0

    def __init__(self, parent=None):
        super().__init__(parent)
        self.drivers: dict[str, DriverInfo] = {}
        self.buffers: dict[str, SeriesBuffer] = {}
        self.positions: dict[str, PosBuffer] = {}
        self.outline: tuple[np.ndarray, np.ndarray] | None = None
        self.corners: list[tuple[str, float, float, float]] = []
        self.tyres: dict[str, dict[int, tuple[str, int]]] = {}
        self.pits: dict[str, list[tuple[int, float]]] = {}
        self.track_status: list[tuple[float, float, str]] = []
        self.weather: list[tuple[float, float, float, float, bool]] = []
        self.sector_yellows: list[tuple[float, float, float, float]] = []
        # visitas a la calle de boxes: {driver: [[vuelta, t_in, t_out|None]]}
        self.pit_lane: dict[str, list[list]] = {}
        # dirección de carrera, reloj de sesión y contador de vueltas
        self.race_control: list[dict] = []
        self.session_clock: tuple | None = None  # (t_rel, restante_s, extrapolando)
        self.lap_count: tuple[int, int] = (0, 0)
        self.session_meta: dict = {}
        # límites oficiales de sector (fin de S1, fin de S2) en metros de
        # vuelta, derivados de los tiempos de sector del feed / FastF1
        self.sector_bounds: tuple[float, float] | None = None
        # tiempos oficiales por (driver, vuelta): [S1, S2, S3, vuelta]
        self.official_times: dict[tuple[str, int], list[float]] = {}
        # True cuando el marco de vuelta viene del feed con latencia
        # (Live/Capture): habilita el re-anclaje con el S1 oficial
        self.live_frames = False
        self._bounds_next_try = 0.0
        self._bounds_done = False
        # frenaje observado por curva: {vértice_m: m de frenaje antes del
        # vértice} derivado del canal de freno (la zona real depende de qué
        # tan rápido se llega y cuánto hay que frenar). None hasta derivar;
        # congelado después (las marcas de µsector deben quedar estables).
        self.brake_dists: dict[float, float] | None = None
        self._brakes_next_try = 0.0
        # cortes de µsector personalizados (panel Microsectors), en metros de
        # vuelta SIN contar los límites de sector (esos siempre están). None
        # = colocación automática. Persisten por circuito+año en config.
        self.custom_micro: list[float] | None = None
        # autos fuera de carrera: retirados oficiales (Retired del feed) y
        # último instante con el auto EN MOVIMIENTO (para detectar abandonos
        # y autos clavados aunque la fuente no publique el retiro)
        self.retired: set[str] = set()
        self.last_move_t: dict[str, float] = {}
        self._stew_cache: tuple | None = None
        # estado de los microsectores oficiales: {driver: {(sector, µ): estado}}
        self.segments: dict[str, dict[tuple[int, int], int]] = {}
        self.segment_counts: dict[int, int] = {}
        self.latest_t = 0.0
        self._dist_map = None
        self._dist_map_len = -1
        self._start_offsets: dict[str, float] = {}
        # corrección de marco pendiente por auto (conexión a mitad de vuelta):
        # (offset, vuelta de entrada) — se aplica a las muestras que sigan
        # llegando en el marco viejo hasta el primer cruce real de meta
        self._dist_fix: dict[str, tuple[float, int]] = {}
        self.track_length = self.DEFAULT_TRACK_LEN
        self.total_samples = 0
        self._track_exact = False
        self._lap_len_obs: list[float] = []
        self._outline_drv: str | None = None
        self._outline_pts: tuple[list, list] | None = None
        self._outline_start_lap = 0

    def clear_samples(self) -> None:
        """Vacía las muestras conservando pilotos, trazado y largo de vuelta
        (para saltos de tiempo del replay: la selección no se pierde). Los
        límites de sector ya derivados también se conservan; el estado de los
        microsectores oficiales se rearma con la historia re-emitida."""
        self.buffers.clear()
        self.positions.clear()
        self.total_samples = 0
        self.latest_t = 0.0
        self._lap_len_obs.clear()
        self._start_offsets.clear()
        self._dist_fix.clear()
        self.last_move_t.clear()
        self.segments.clear()

    def on_corners(self, corners) -> None:
        self.corners = list(corners)

    def circuit_key(self) -> str | None:
        """Clave por circuito+año para persistir la config de microsectores:
        la misma todo el fin de semana, sin importar la tanda cargada."""
        meeting = str(self.session_meta.get("meeting") or "").strip().lower()
        if not meeting:
            return None
        slug = re.sub(r"[^a-z0-9]+", "-", meeting).strip("-")
        year = str(self.session_meta.get("year") or "").strip()
        return f"{year}-{slug}" if year else slug

    def on_tyres(self, data) -> None:
        self.tyres = dict(data)

    def on_pits(self, data) -> None:
        self.pits = {drv: list(stops) for drv, stops in data.items()}

    def on_track_status(self, periods) -> None:
        self.track_status = list(periods)

    def on_weather(self, rows) -> None:
        self.weather = sorted(rows)

    def on_sector_yellows(self, periods) -> None:
        self.sector_yellows = list(periods)

    def on_pit_lane(self, data) -> None:
        self.pit_lane = {drv: [list(v) for v in visits]
                         for drv, visits in data.items()}

    # margen para el feed de timing apenas adelantado a la telemetría (vivo)
    PIT_AHEAD_S = 3.0

    _CAR_RE = re.compile(r"CARS? (\d+)|\b(\d+) \([A-Z]{3}\)")

    def stewards_flags(self) -> dict[str, str]:
        """Chip de comisarios por auto según dirección de carrera HASTA el
        timeline: '⚠' investigación abierta; '+5s'/'+10s'/'DT'/'SG' sanción
        pendiente (se limpia con PENALTY SERVED; la investigación con NO
        FURTHER... o al decidirse la sanción)."""
        sig = (len(self.race_control), round(self.latest_t, 1))
        if self._stew_cache is not None and self._stew_cache[0] == sig:
            return self._stew_cache[1]
        inv: dict[str, bool] = {}
        pen: dict[str, str] = {}
        for msg in self.race_control:
            if float(msg.get("t", 0.0)) > self.latest_t:
                continue  # sin spoilers
            text = str(msg.get("message", "")).upper()
            cars = {a or b for a, b in self._CAR_RE.findall(text)}
            if not cars:
                continue
            for num in cars:
                if "NO FURTHER" in text:
                    inv[num] = False
                elif "UNDER INVESTIGATION" in text or "NOTED" in text:
                    inv[num] = True
                if "TIME PENALTY" in text:
                    m = re.search(r"(\d+) SECOND", text)
                    pen[num] = f"+{m.group(1)}s" if m else "PEN"
                    inv[num] = False
                elif "DRIVE THROUGH" in text:
                    pen[num] = "DT"
                    inv[num] = False
                elif "STOP AND GO" in text or "STOP/GO" in text \
                        or "STOP & GO" in text:
                    pen[num] = "SG"
                    inv[num] = False
                if "SERVED" in text:
                    pen.pop(num, None)
        out: dict[str, str] = {}
        for num in set(inv) | set(pen):
            chip = pen.get(num) or ("⚠" if inv.get(num) else "")
            if chip:
                out[num] = chip
        self._stew_cache = (sig, out)
        return out

    def tyres_until_now(self, drv: str) -> dict[int, tuple[str, int]]:
        """Neumáticos por vuelta SOLO hasta la vuelta en curso del auto: el
        replay/demo publican el plan completo por adelantado y ningún panel
        debe adelantarse al timeline. Sin muestras del auto se usa la vuelta
        del líder (fila visible sin espiar el futuro)."""
        tyre_map = self.tyres.get(drv)
        if not tyre_map:
            return {}
        buf = self.buffers.get(drv)
        cur = buf.current_lap() if buf is not None and buf.n else 0
        if cur <= 0:
            cur = max((b.current_lap() for b in self.buffers.values() if b.n),
                      default=0)
        if cur <= 0:
            return {}
        return {lap: v for lap, v in tyre_map.items() if lap <= cur}

    def on_retirements(self, nums) -> None:
        self.retired = {str(n) for n in nums}

    def is_active(self, drv: str, stale_s: float = 45.0) -> bool:
        """Auto en competencia: no retirado oficialmente y con telemetría en
        movimiento. Un auto clavado o sin datos frescos por stale_s queda
        inactivo (abandono, auto en el garage); en boxes no caduca (paradas
        largas, banderas rojas), y antes del primer cruce de meta tampoco
        (la grilla está detenida)."""
        if drv in self.retired:
            return False
        buf = self.buffers.get(drv)
        if buf is None or not buf.n:
            return False
        if buf.current_lap() <= 1:
            return True
        visit = self.last_pit_visit(drv)
        if visit is not None and self.pit_visit_open(visit):
            return True
        return self.latest_t - self.last_move_t.get(drv, 0.0) <= stale_s

    def last_pit_visit(self, drv: str) -> list | None:
        """Última visita a boxes ya iniciada a latest_t. El replay publica
        la historia completa de la sesión por adelantado: las visitas
        futuras todavía no cuentan."""
        visits = self.pit_lane.get(drv)
        if not visits:
            return None
        limit = self.latest_t + self.PIT_AHEAD_S
        for visit in reversed(visits):
            if visit[1] <= limit:
                return visit
        return None

    def pit_visit_open(self, visit: list) -> bool:
        """True si la visita sigue en curso a latest_t: sin salida
        registrada, o con la salida aún en el futuro (replay)."""
        return visit[2] is None or self.latest_t < visit[2]

    def pit_stops_done(self, drv: str) -> list[tuple[int, float]]:
        """Paradas ya ocurridas a latest_t (el replay publica todas)."""
        limit = self.latest_t + self.PIT_AHEAD_S
        return [s for s in self.pits.get(drv, []) if s[1] <= limit]

    def pit_stationary_time(self, drv: str, t0: float, t1: float) -> float:
        """Segundos detenido (velocidad ~0) entre t0 y t1, de la telemetría."""
        buf = self.buffers.get(drv)
        if buf is None or buf.n < 2:
            return 0.0
        t = buf.col("t")
        i0 = int(np.searchsorted(t, t0))
        i1 = int(np.searchsorted(t, t1))
        if i1 - i0 < 2:
            return 0.0
        dt = np.diff(t[i0:i1])
        stopped = (buf.col("speed")[i0 + 1:i1] <= 0.5) & (dt <= 5.0)
        return float(dt[stopped].sum())

    def on_race_control(self, rows) -> None:
        self.race_control = list(rows)

    def on_session_clock(self, value) -> None:
        self.session_clock = tuple(value) if value else None

    def on_lap_count(self, value) -> None:
        try:
            self.lap_count = (int(value[0]), int(value[1]))
        except (TypeError, ValueError, IndexError):
            pass

    def on_session_meta(self, meta) -> None:
        if isinstance(meta, dict):
            self.session_meta.update(meta)

    def clock_remaining(self) -> float | None:
        """Tiempo restante de sesión al instante más reciente de datos
        (extrapola desde la última lectura del reloj si corresponde)."""
        if self.session_clock is None:
            return None
        t_ref, remaining, extrapolating = self.session_clock
        if not extrapolating:
            return remaining
        return max(0.0, remaining - max(0.0, self.latest_t - t_ref))

    def leader_laps_at(self, ts) -> np.ndarray:
        """Vuelta (continua: vuelta + fracción) del auto más adelantado en
        cada instante de `ts` — eje X del gráfico de clima en carrera."""
        ts = np.asarray(ts, dtype=float)
        out = np.zeros(len(ts))
        L = max(self.track_length, 1.0)
        for buf in self.buffers.values():
            if not buf.n:
                continue
            t_col = buf.col("t")
            idx = np.clip(np.searchsorted(t_col, ts, side="right") - 1, 0, buf.n - 1)
            laps = (buf.col("lap")[idx].astype(float)
                    + np.clip(buf.col("dist_lap")[idx] / L, 0.0, 1.0))
            laps[ts < float(t_col[0])] = 0.0
            np.maximum(out, laps, out=out)
        return out

    def on_sector_times(self, batch) -> None:
        """Tiempos oficiales: [(driver, vuelta, índice, segundos)] con
        índice 0-2 = S1-S3 y 3 = vuelta. Gana el primer valor (una
        atribución tardía dudosa no pisa un dato ya correcto)."""
        for drv, lap, idx, secs in batch:
            if idx not in (0, 1, 2, 3):
                continue
            rec = self.official_times.setdefault(
                (str(drv), int(lap)), [float("nan")] * 4
            )
            if rec[idx] != rec[idx]:  # NaN: aún sin valor
                rec[idx] = float(secs)

    def on_segments(self, batch) -> None:
        """Estados de microsectores oficiales: [(driver, sector, µ, estado)]."""
        for drv, sec, seg, status in batch:
            sec, seg = int(sec), int(seg)
            self.segments.setdefault(str(drv), {})[(sec, seg)] = int(status)
            if seg + 1 > self.segment_counts.get(sec, 0):
                self.segment_counts[sec] = seg + 1

    # ---- límites oficiales de sector ----

    BOUNDS_MIN_OBS = 6     # observaciones mínimas por límite para publicarlo
    BOUNDS_DONE_OBS = 400  # con estas observaciones se considera convergido

    def _apply_pending_dist_fix(self, drv: str, buf: SeriesBuffer,
                                start: int) -> None:
        """Corrige las muestras recién llegadas mientras la vuelta de entrada
        siga en el marco del punto de conexión; al primer cruce real el
        decodificador ya ancla en la meta y la corrección se retira."""
        fix = self._dist_fix.get(drv)
        if fix is None:
            return
        offset, join_lap = fix
        lapcol = buf.col("lap")
        end = buf.n
        if int(lapcol[-1]) > join_lap:
            end = int(np.searchsorted(lapcol, join_lap, side="right"))
            del self._dist_fix[drv]
        if end > start:
            buf._arr["dist_lap"][start:end] -= offset

    def _lap_t0(self, buf: SeriesBuffer, lap: int) -> float | None:
        """Instante del cruce de meta que abre la vuelta (interpolado entre
        la última muestra de la vuelta previa y la primera de esta)."""
        lapcol = buf.col("lap")
        i0 = int(np.searchsorted(lapcol, lap, side="left"))
        if i0 <= 0 or i0 >= buf.n or int(lapcol[i0 - 1]) != lap - 1:
            return None
        d_prev = float(buf.col("dist_lap")[i0 - 1]) - self.track_length
        d_next = float(buf.col("dist_lap")[i0])
        t_prev = float(buf.col("t")[i0 - 1])
        t_next = float(buf.col("t")[i0])
        if d_prev >= 0.0 or d_next <= d_prev or t_next < t_prev:
            # la vuelta previa integró de más: el cruce cayó entre muestras
            return (t_prev + t_next) / 2.0
        return t_prev + (0.0 - d_prev) / (d_next - d_prev) * (t_next - t_prev)

    def maybe_derive_sector_bounds(self) -> None:
        """Ubica los límites reales de S1/S2 en metros: interpola dónde
        estaba cada auto en el instante en que cerró el sector (ancla = cruce
        de meta + tiempo oficial de sector) y toma la mediana entre vueltas y
        pilotos. Llamar periódicamente; recalcula como mucho cada 5 s y deja
        de hacerlo al converger."""
        if self._bounds_done or not self.official_times:
            return
        now = time.monotonic()
        if now < self._bounds_next_try:
            return
        self._bounds_next_try = now + 5.0
        L = self.track_length
        b1_obs: list[float] = []
        b2_obs: list[float] = []
        for (drv, lap), (s1, s2, _s3, _lt) in self.official_times.items():
            if lap < 2:
                continue  # vuelta 1: largada desde la grilla, sin ancla limpia
            buf = self.buffers.get(drv)
            if buf is None or not buf.n:
                continue
            t0 = self._lap_t0(buf, lap)
            if t0 is None:
                continue
            sl = buf.lap_slice(lap)
            t_arr = sl["t"]
            d_arr = sl["dist_lap"]
            if len(t_arr) < 4:
                continue
            if s1 == s1:  # not NaN
                tc = t0 + s1
                if t_arr[0] <= tc <= t_arr[-1]:
                    d1 = float(np.interp(tc, t_arr, d_arr))
                    if 0.10 * L < d1 < 0.55 * L:
                        b1_obs.append(d1)
                if s2 == s2:
                    tc = t0 + s1 + s2
                    if t_arr[0] <= tc <= t_arr[-1]:
                        d2 = float(np.interp(tc, t_arr, d_arr))
                        if 0.40 * L < d2 < 0.92 * L:
                            b2_obs.append(d2)
        if len(b1_obs) < self.BOUNDS_MIN_OBS or len(b2_obs) < self.BOUNDS_MIN_OBS:
            return
        b1 = float(np.median(b1_obs))
        b2 = float(np.median(b2_obs))
        if not (0.0 < b1 < b2 - 0.05 * L and b2 < L):
            return
        if len(b1_obs) + len(b2_obs) >= self.BOUNDS_DONE_OBS:
            self._bounds_done = True
        if (self.sector_bounds is None
                or abs(b1 - self.sector_bounds[0]) > 2.0
                or abs(b2 - self.sector_bounds[1]) > 2.0):
            self.sector_bounds = (b1, b2)

    # detección del frenaje por curva (canal brake de la telemetría)
    BRAKE_WINDOW = 450.0   # m antes del vértice donde buscar el frenaje
    BRAKE_ON = 20.0        # % de pedal que cuenta como frenando
    BRAKES_MIN_OBS = 4     # observaciones mínimas por curva

    def maybe_derive_brake_zones(self) -> None:
        """Mide dónde ARRANCA el frenaje de cada curva: primera muestra con
        pedal > BRAKE_ON % acercándose al vértice, mediana entre vueltas y
        pilotos. Una sola derivación por sesión (con 2+ autos con 2+ vueltas
        cerradas): las marcas de µsector deben quedar estables. Curvas sin
        frenadas observadas (viraje a fondo) quedan sin zona de frenaje."""
        if self.brake_dists is not None or not self.corners:
            return
        now = time.monotonic()
        if now < self._brakes_next_try:
            return
        self._brakes_next_try = now + 5.0
        apexes = [float(d) for _l, d, _x, _y in self.corners]
        obs: dict[int, list[float]] = {}
        cars_ready = 0
        for buf in self.buffers.values():
            laps = buf.completed_laps()[-6:] if buf.n else []
            if len(laps) < 2:
                continue
            cars_ready += 1
            for lap in laps:
                sl = buf.lap_slice(lap)
                d_arr = sl["dist_lap"]
                b_arr = sl["brake"]
                if len(d_arr) < 8:
                    continue
                for j, c in enumerate(apexes):
                    a = int(np.searchsorted(d_arr, c - self.BRAKE_WINDOW))
                    b = int(np.searchsorted(d_arr, c))
                    if b - a < 3:
                        continue
                    braking = np.flatnonzero(b_arr[a:b] >= self.BRAKE_ON)
                    if len(braking):
                        obs.setdefault(j, []).append(
                            c - float(d_arr[a + int(braking[0])]))
        if cars_ready < 2:
            return
        self.brake_dists = {
            round(apexes[j], 1): float(np.median(dists))
            for j, dists in obs.items()
            if len(dists) >= self.BRAKES_MIN_OBS
        }

    def weather_at(self, t: float):
        """Última lectura de clima anterior o igual a t (None si no hay)."""
        current = None
        for row in self.weather:
            if row[0] <= t:
                current = row
            else:
                break
        return current

    def reset(self) -> None:
        self.drivers.clear()
        self.buffers.clear()
        self.positions.clear()
        self.corners = []
        self.tyres = {}
        self.pits = {}
        self.track_status = []
        self.weather = []
        self.sector_yellows = []
        self.pit_lane = {}
        self.race_control = []
        self.session_clock = None
        self.lap_count = (0, 0)
        self.session_meta = {}
        self.sector_bounds = None
        self.official_times.clear()
        self.live_frames = False
        self._bounds_next_try = 0.0
        self._bounds_done = False
        self.brake_dists = None
        self._brakes_next_try = 0.0
        self.custom_micro = None
        self.retired = set()
        self.last_move_t.clear()
        self._stew_cache = None
        self.segments.clear()
        self.segment_counts.clear()
        self.latest_t = 0.0
        self.outline = None
        self._dist_map = None
        self._dist_map_len = -1
        self._start_offsets.clear()
        self._outline_drv = None
        self._outline_pts = None
        self.track_length = self.DEFAULT_TRACK_LEN
        self.total_samples = 0
        self._track_exact = False
        self._lap_len_obs.clear()
        self.driversChanged.emit()
        self.trackLengthChanged.emit(self.track_length)

    # ---- slots conectados a las fuentes ----

    def on_drivers(self, infos: dict[str, DriverInfo]) -> None:
        changed = False
        for num, info in infos.items():
            old = self.drivers.get(num)
            if old is None or (info.code, info.color, info.name) != (old.code, old.color, old.name):
                self.drivers[num] = info
                changed = True
        if changed:
            self.driversChanged.emit()

    def on_track_length(self, meters: float) -> None:
        """Longitud exacta informada por la fuente (replay)."""
        if meters > 100:
            self._track_exact = True
            if abs(meters - self.track_length) > 1:
                self.track_length = float(meters)
                self.trackLengthChanged.emit(self.track_length)

    def on_batch(self, samples: list[Sample]) -> None:
        by_driver: dict[str, list[Sample]] = {}
        for s in samples:
            by_driver.setdefault(s.driver, []).append(s)
        for drv, group in by_driver.items():
            buf = self.buffers.get(drv)
            if buf is None:
                buf = self.buffers[drv] = SeriesBuffer()
                if drv not in self.drivers:
                    self.drivers[drv] = DriverInfo(number=drv, code=drv)
                    self.driversChanged.emit()
            if not self._track_exact:
                self._observe_lap_length(buf, group)
            n0 = buf.n
            buf.append(group)
            self._apply_pending_dist_fix(drv, buf, n0)
            moved = [s.t for s in group if s.speed > 3.0]
            if moved:
                self.last_move_t[drv] = max(
                    self.last_move_t.get(drv, 0.0), moved[-1])
        if samples:
            self.latest_t = max(self.latest_t, samples[-1].t)
        self.total_samples += len(samples)

    def on_outline(self, arrays) -> None:
        """Trazado del circuito informado por la fuente (demo/replay)."""
        xs, ys = arrays
        if len(xs) > 10:
            self.outline = (np.asarray(xs, dtype=float), np.asarray(ys, dtype=float))
            self._outline_pts = None
            self._dist_map = None
            self._dist_map_len = -1

    def outline_dist_map(self):
        """Mapea distancia de vuelta -> (x, y) sobre el trazado (arco
        acumulado escalado al largo de vuelta)."""
        if self.outline is None or len(self.outline[0]) < 3:
            return None
        if self._dist_map is None or self._dist_map_len != len(self.outline[0]):
            xs, ys = self.outline
            seg = np.hypot(np.diff(xs), np.diff(ys))
            total = float(seg.sum())
            if total <= 0:
                return None
            dist = np.concatenate(([0.0], np.cumsum(seg))) / total * self.track_length
            self._dist_map = (dist, np.asarray(xs, float), np.asarray(ys, float))
            self._dist_map_len = len(xs)
        return self._dist_map

    def provisional_start_offset(self, drv: str) -> float | None:
        """Offset provisional del primer tramo observado (aún sin cruce de
        meta en los datos): proyecta la última posición (x, y) sobre el
        trazado para conocer el metro físico real y lo compara con la
        distancia integrada. Cubre la vuelta 1 (offset de grilla) y la
        conexión a mitad de sesión, donde cada auto arranca con distancia 0
        en cualquier punto de la pista (sin esto la torre ordenaba mal hasta
        que todos cruzaban la meta)."""
        cached = self._start_offsets.get(drv)
        if cached is not None:
            return cached
        buf = self.buffers.get(drv)
        pb = self.positions.get(drv)
        mapping = self.outline_dist_map()
        if buf is None or not buf.n or pb is None or not len(pb) or mapping is None:
            return None
        lapcol = buf.col("lap")
        if int(lapcol[-1]) > int(lapcol[0]):
            return None  # ya se observó un cruce: el ancla es real
        if float(buf.col("dist_lap")[-1]) < 300.0:
            return None  # muy poca integración para comparar
        # distancia recorrida en el instante de la última posición conocida
        driven = float(np.interp(pb.t[-1], buf.col("t"), buf.col("dist_lap")))
        if driven < 250.0:
            return None
        dist_arr, xs, ys = mapping
        d2 = (xs - pb.x[-1]) ** 2 + (ys - pb.y[-1]) ** 2
        phys = float(dist_arr[int(np.argmin(d2))])
        offset = driven - phys
        if int(lapcol[-1]) == 1:
            # largada: el auto parte de la grilla, el offset es chico y >= 0
            if offset < -100.0 or offset > 1000.0:
                return None  # proyección dudosa
            offset = max(offset, 0.0)
        else:
            if abs(offset) > 1.5 * self.track_length:
                return None  # proyección dudosa
            # conexión a mitad de vuelta: REESCRIBIR el tramo no anclado para
            # que dist_lap mida desde la meta real del circuito (el eje X de
            # los gráficos usa esa columna; sin esto el 0 quedaba en el punto
            # de conexión). Las muestras que sigan llegando en el marco viejo
            # se corrigen en on_batch hasta el primer cruce real.
            buf._arr["dist_lap"][:buf.n] -= offset
            self._dist_fix[drv] = (offset, int(lapcol[-1]))
            offset = 0.0
        self._start_offsets[drv] = offset
        return offset

    def on_positions(self, batch: list) -> None:
        """Lote de posiciones (driver, t, x, y) para el mapa."""
        for drv, t, x, y in batch:
            pb = self.positions.get(drv)
            if pb is None:
                pb = self.positions[drv] = PosBuffer()
            pb.append(t, x, y)
        if self.outline is None:
            self._autobuild_outline(batch)

    def _autobuild_outline(self, batch: list) -> None:
        """Sin trazado provisto (fuente en vivo): se arma siguiendo a un auto
        en movimiento hasta que cierra una vuelta completa."""
        if self._outline_drv is None:
            for drv, *_ in batch:
                buf = self.buffers.get(drv)
                if buf is not None and buf.n and float(buf.col("speed")[-1]) > 50.0:
                    self._outline_drv = drv
                    self._outline_pts = ([], [])
                    self._outline_start_lap = buf.current_lap()
                    break
            if self._outline_drv is None:
                return
        xs, ys = self._outline_pts
        for drv, _t, x, y in batch:
            if drv == self._outline_drv:
                xs.append(x)
                ys.append(y)
        buf = self.buffers.get(self._outline_drv)
        lap_done = buf is not None and buf.current_lap() >= self._outline_start_lap + 2
        if (lap_done and len(xs) > 50) or len(xs) > 8000:
            self.outline = (np.asarray(xs, dtype=float), np.asarray(ys, dtype=float))
            self._outline_pts = None

    # ---- estimación de longitud de vuelta (fuentes en vivo) ----

    def _observe_lap_length(self, buf: SeriesBuffer, group: list[Sample]) -> None:
        if buf.n:
            last_lap = int(buf.col("lap")[-1])
            last_d = float(buf.col("dist_lap")[-1])
        else:
            last_lap, last_d = group[0].lap, 0.0
        added = False
        for s in group:
            if s.lap > last_lap and last_d > 1000.0:
                self._lap_len_obs.append(last_d)
                added = True
            last_lap, last_d = s.lap, s.dist_lap
        if added:
            est = float(np.median(self._lap_len_obs[-30:]))
            if abs(est - self.track_length) > 20.0:
                self.track_length = est
                self.trackLengthChanged.emit(est)
