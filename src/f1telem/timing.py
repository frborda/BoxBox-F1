"""Análisis de tiempos derivado de las muestras: vueltas, sectores,
microsectores y gaps. Funciona igual para las tres fuentes.

Método: se interpola el instante en que cada auto cruza marcas de
distancia fijas dentro de la vuelta (24 divisiones; los sectores son
grupos de 8). Si el hub ya derivó los límites oficiales de sector
(`hub.sector_bounds`), las marcas se anclan a ellos: S1/S2/S3 pasan a ser
los sectores reales y cada µsector es 1/8 de su sector; si no, la vuelta
se divide en tercios iguales. Cuando el trazado con curvas ya se conoce,
los cortes internos se corren fuera de las zonas de frenaje/curva (la
interpolación asume velocidad constante entre muestras y es donde más
erra); los límites de sector nunca se tocan. Con telemetría a ~4-5 Hz la
precisión es de ~±0,1 s: sirve para comparar, no es el cronometraje
oficial. Como el µsector es un dato calculado y el sector es oficial, al
llegar cada tiempo oficial de sector los 8 µ de ese sector se re-escalan
proporcionalmente para que su suma cierre EXACTA contra el oficial (sin
oficial, la suma ya cierra contra el sector interpolado por construcción:
son diferencias del mismo arreglo de marcas).

El "gap" entre dos autos es la diferencia de tiempo al pasar por la misma
posición de pista (vuelta × largo + metro de vuelta): positivo = detrás
de la referencia.
"""
from __future__ import annotations

import numpy as np

from .hub import DataHub

N_MICRO = 24          # microsectores por vuelta (divisible por 3)
SECTOR_STEP = N_MICRO // 3

# zona conflictiva alrededor del vértice de cada curva: el frenaje arranca
# antes y la tracción se recupera después; un corte de µsector ahí cae en
# plena variación de velocidad (donde la interpolación lineal más erra) y
# es confuso de leer. Los cortes internos se corren al borde de la zona.
BRAKE_BEFORE = 100.0   # m antes del vértice (solo hasta derivar el frenaje
                       # real por curva del canal de freno: hub.brake_dists)
EXIT_AFTER = 60.0      # m después del vértice
APEX_GUARD = 30.0      # zona mínima antes del vértice (curvas sin frenaje)
ZONE_BRIDGE = 80.0     # m mínimos de pista limpia entre dos curvas para
                       # aceptar un corte entre ellas: con menos (chicanas,
                       # eses) las zonas se fusionan y el corte no cae en
                       # el cambio de sentido
MAX_SHIFT_FRAC = 0.35  # tope de corrimiento (fracción del ancho del µ):
                       # garantiza orden y un ancho mínimo de 0,3 µ


class TimingAnalyzer:
    def __init__(self, hub: DataHub):
        self.hub = hub
        self._marks_cache: dict[tuple[str, int], np.ndarray] = {}
        self._pos_cache: dict[str, tuple[int, float, tuple]] = {}
        self._geo_used: tuple = (0.0, None, (), None, None)
        self._dists_cache: np.ndarray | None = None
        self._scale_cache: dict[tuple[str, int], np.ndarray] = {}
        # índices de los límites de sector dentro de las marcas (la cantidad
        # de µ por sector puede variar con la config del panel Microsectors)
        self._sector_idx: tuple[int, int] = (SECTOR_STEP, 2 * SECTOR_STEP)

    def clear(self) -> None:
        self._marks_cache.clear()
        self._pos_cache.clear()
        self._scale_cache.clear()
        self._dists_cache = None

    def _mark_dists(self) -> np.ndarray:
        """Distancias de las marcas. Límites de sector: los oficiales cuando
        el hub ya los derivó (si no, tercios) — siempre presentes. Cortes
        internos: los del panel Microsectors si hay config para el circuito
        (cantidad libre por sector), si no 8 µ iguales por sector corridos
        fuera de las zonas de frenaje/curva."""
        self._check_track_len()
        if self._dists_cache is not None:
            return self._dists_cache
        L = self.hub.track_length
        edges = (0.0, L / 3.0, 2.0 * L / 3.0, L)
        bounds = self.hub.sector_bounds
        if bounds is not None and 0.0 < bounds[0] < bounds[1] < L:
            edges = (0.0, bounds[0], bounds[1], L)
        custom = self.hub.custom_micro
        if custom:
            interior = [float(d) for d in custom
                        if 1.0 < d < L - 1.0
                        and min(abs(d - e) for e in edges) > 1.0]
            marks = np.unique(np.array([*edges, *interior]))
        else:
            marks = np.concatenate([
                np.linspace(edges[k], edges[k + 1], SECTOR_STEP + 1)[:-1]
                for k in range(3)
            ] + [[L]])
            marks = self._snap_marks(marks, L)
        self._sector_idx = (int(np.searchsorted(marks, edges[1])),
                            int(np.searchsorted(marks, edges[2])))
        self._dists_cache = marks
        return marks

    def n_micro(self) -> int:
        return len(self._mark_dists()) - 1

    def sector_slices(self) -> tuple[tuple[int, int], ...]:
        """Rango (inicio, fin) de los µ de cada sector, sobre los µ de la
        vuelta (fin exclusivo). Con la config por defecto: (0,8)(8,16)(16,24)."""
        n = self.n_micro()  # asegura _sector_idx actualizado
        i1, i2 = self._sector_idx
        return ((0, i1), (i1, i2), (i2, n))

    def _snap_marks(self, marks: np.ndarray, L: float) -> np.ndarray:
        """Corre cada corte interno que cae en la zona de una curva
        ([−BRAKE_BEFORE, +EXIT_AFTER] del vértice) al borde más cercano de
        la zona: queda antes del comienzo del frenaje o pasada la salida.
        Los límites de sector (marcas 0/8/16/24) son oficiales y no se
        tocan; una marca nunca se mueve más de MAX_SHIFT_FRAC de su µ (si
        la zona es más ancha que eso, se queda: corte de compromiso). Sin
        curvas conocidas no hace nada."""
        corners = sorted(d for _lbl, d, _x, _y in self.hub.corners
                         if 0.0 < d < L)
        if not corners:
            return marks
        # inicio de zona por curva: el frenaje real medido si ya se derivó
        # (depende de la velocidad de llegada), si no el fallback fijo
        brake_dists = self.hub.brake_dists
        zones: list[list[float]] = []
        for c in corners:
            if brake_dists is not None:
                before = max(brake_dists.get(round(c, 1), 0.0), APEX_GUARD)
            else:
                before = BRAKE_BEFORE
            z0, z1 = max(c - before, 0.0), min(c + EXIT_AFTER, L)
            # fusionar también con hueco chico entre medio: en curva y
            # contracurva el corte no debe caer en el cambio de sentido
            if zones and z0 - zones[-1][1] < ZONE_BRIDGE:
                zones[-1][1] = max(zones[-1][1], z1)
            else:
                zones.append([z0, z1])
        out = marks.copy()
        for i in range(1, N_MICRO):
            if i % SECTOR_STEP == 0:
                continue  # límite de sector: intocable
            m = float(marks[i])
            max_shift = MAX_SHIFT_FRAC * float(marks[i + 1] - marks[i - 1]) / 2.0
            for z0, z1 in zones:
                if z0 < m < z1:
                    back, fwd = m - z0, z1 - m
                    if back <= fwd and back <= max_shift:
                        out[i] = z0
                    elif fwd <= max_shift:
                        out[i] = z1
                    break
        return out

    def _check_track_len(self) -> None:
        bd = self.hub.brake_dists
        cm = self.hub.custom_micro
        geo = (self.hub.track_length, self.hub.sector_bounds,
               tuple(round(float(d), 1) for _l, d, _x, _y in self.hub.corners),
               None if bd is None else tuple(sorted(bd.items())),
               None if cm is None else tuple(round(float(d), 1) for d in cm))
        if (abs(geo[0] - self._geo_used[0]) > 1.0
                or geo[1:] != self._geo_used[1:]):
            self._marks_cache.clear()
            self._pos_cache.clear()
            self._scale_cache.clear()
            self._dists_cache = None
            self._geo_used = geo

    # ------------------------------------------------------- marcas por vuelta

    def lap_marks(self, drv: str, lap: int) -> np.ndarray | None:
        """Tiempo de cruce de cada marca de la vuelta (NaN si aún no cruzada).

        En fuentes en vivo (marco de vuelta con latencia del feed) los
        cruces de meta se re-anclan con el S1 oficial apenas llega: el cruce
        real es t(b1) − S1, independiente de la latencia del stream.
        """
        marks = self._frame_marks(drv, lap)
        if marks is None or not self.hub.live_frames:
            return marks
        t0 = self._s1_crossing(drv, lap)
        t1 = self._s1_crossing(drv, lap + 1)
        if t0 is None and t1 is None:
            return marks
        marks = marks.copy()
        if t0 is not None:
            rest = marks[1:][np.isfinite(marks[1:])]
            if not len(rest) or t0 < float(rest[0]):
                marks[0] = t0
        if t1 is not None:
            rest = marks[:-1][np.isfinite(marks[:-1])]
            if not len(rest) or t1 > float(rest[-1]):
                marks[-1] = t1
        return marks

    def _s1_crossing(self, drv: str, lap: int) -> float | None:
        """Cruce de meta re-anclado: instante en que el marco cruza b1 menos
        el S1 oficial. Requiere los límites oficiales ya derivados."""
        if self.hub.sector_bounds is None:
            return None
        off = self.hub.official_times.get((drv, lap))
        if off is None or off[0] != off[0]:
            return None
        frame = self._frame_marks(drv, lap)
        if frame is None:
            return None
        t_b1 = float(frame[self._sector_idx[0]])
        if t_b1 != t_b1:
            return None
        return t_b1 - float(off[0])

    def _frame_marks(self, drv: str, lap: int) -> np.ndarray | None:
        """Marcas en el marco de distancia de la vuelta (sin re-anclaje).

        Los extremos se anclan sintetizando el instante del cruce de meta.
        Sin esto, una vuelta cuya distancia integrada difiere del largo
        mediano (vueltas tras pasar por boxes, deriva de integración) quedaba
        sin marca inicial o final y su tiempo/sectores daban NaN.
        """
        self._check_track_len()
        cached = self._marks_cache.get((drv, lap))
        if cached is not None:
            return cached
        buf = self.hub.buffers.get(drv)
        if buf is None or not buf.n or lap < 1:
            return None
        lapcol = buf.col("lap")
        i0 = int(np.searchsorted(lapcol, lap, side="left"))
        i1 = int(np.searchsorted(lapcol, lap, side="right"))
        if i0 >= i1:
            return None
        L = self.hub.track_length
        d = buf.col("dist_lap")[i0:i1].astype(np.float64)
        t = buf.col("t")[i0:i1].astype(np.float64)
        completed = lap < buf.current_lap()

        # conexión a mitad de sesión (vivo): la primera vuelta observada no
        # arranca en la meta — su marco está corrido por el punto de entrada
        # y cronometrarla ensuciaría Last/Best con una vuelta parcial
        if i0 == 0 and self.hub.live_frames:
            off = self.hub.provisional_start_offset(drv)
            if off is None or abs(off) > 150.0:
                return None

        # marco propio de la vuelta: las muestras vecinas se proyectan vía
        # dist_total (comparte la integración con dist_lap) y, si la vuelta
        # siguiente ya existe, las marcas se escalan al largo REALMENTE
        # integrado (base_next − base). Sin esto se asumía que cada vuelta
        # integró exactamente L y el error de integración (±15 m) sesgaba
        # los cruces de meta hasta ~0,1 s o dejaba vueltas sin marca final.
        base = float(buf.col("dist_total")[i0]) - float(d[0])
        scale = 1.0
        if i1 < buf.n and int(lapcol[i1]) == lap + 1:
            base_next = (float(buf.col("dist_total")[i1])
                         - float(buf.col("dist_lap")[i1]))
            lap_len = base_next - base
            if 0.5 * L < lap_len < 1.5 * L:
                scale = lap_len / L

        # ancla inicial: cruce de meta en dist 0
        if d[0] > 0.0:
            anchor = None
            if i0 > 0 and int(lapcol[i0 - 1]) == lap - 1:
                d_prev = float(buf.col("dist_total")[i0 - 1]) - base
                t_prev = float(buf.col("t")[i0 - 1])
                if d_prev < 0.0:
                    anchor = (d_prev, t_prev)
                else:
                    # el marco arranca antes de la muestra previa (raro):
                    # el cruce cayó entre ambas muestras
                    anchor = (0.0, (t_prev + float(t[0])) / 2.0)
            elif d[0] < L * 0.05:
                # sin vuelta previa: retro-extrapolar por velocidad
                v0 = max(float(buf.col("speed")[i0]), 5.0) / 3.6
                anchor = (0.0, float(t[0]) - min(d[0] / v0, 5.0))
            if anchor is not None:
                d = np.concatenate(([anchor[0]], d))
                t = np.concatenate(([anchor[1]], t))

        # ancla final: cruce de meta en dist L·scale (solo vueltas cerradas)
        if d[-1] < L * scale:
            anchor = None
            if i1 < buf.n and int(lapcol[i1]) == lap + 1:
                # la primera muestra de la vuelta siguiente proyectada a este
                # marco queda en lap_len + su avance: cubre L·scale (el mm
                # extra evita que el redondeo de L·scale caiga fuera y NaN)
                anchor = (float(buf.col("dist_total")[i1]) - base + 1e-3,
                          float(buf.col("t")[i1]))
            elif completed and (L - d[-1]) < L * 0.05:
                v1 = max(float(buf.col("speed")[i1 - 1]), 5.0) / 3.6
                anchor = (L + 1e-6, float(t[-1]) + min((L - d[-1]) / v1, 5.0))
            if anchor is not None and anchor[0] > d[-1]:
                d = np.append(d, anchor[0])
                t = np.append(t, anchor[1])

        marks = np.interp(self._mark_dists() * scale, d, t,
                          left=np.nan, right=np.nan)
        if completed:  # vuelta cerrada: inmutable, cachear
            self._marks_cache[(drv, lap)] = marks
        return marks

    # ------------------------------------------------------------ tiempos

    def lap_time(self, drv: str, lap: int) -> float:
        """Tiempo de vuelta: el oficial del feed si ya llegó, si no el
        interpolado de la telemetría."""
        off = self.hub.official_times.get((drv, lap))
        if off is not None and off[3] == off[3]:
            return float(off[3])
        marks = self.lap_marks(drv, lap)
        if marks is None:
            return float("nan")
        return float(marks[-1] - marks[0])

    def sector_times(self, drv: str, lap: int) -> list[float]:
        """Sectores de la vuelta: cada uno el oficial si ya llegó, si no el
        interpolado."""
        marks = self.lap_marks(drv, lap)
        if marks is None:
            times = [float("nan")] * 3
        else:
            i1, i2 = self._sector_idx
            pts = (0, i1, i2, len(marks) - 1)
            times = [
                float(marks[pts[k + 1]] - marks[pts[k]]) for k in range(3)
            ]
        off = self.hub.official_times.get((drv, lap))
        if off is not None:
            for k in range(3):
                if off[k] == off[k]:
                    times[k] = float(off[k])
        return times

    def _sector_scales(self, drv: str, lap: int) -> np.ndarray:
        """Factor por sector que lleva los 8 µ interpolados a sumar EXACTO
        el tiempo oficial del sector cuando ya llegó (1.0 sin oficial): el
        µsector es un dato calculado y el sector es oficial, así que el
        error de interpolación se reparte proporcionalmente entre los µ."""
        cached = self._scale_cache.get((drv, lap))
        if cached is not None:
            return cached
        scales = np.ones(3)
        off = self.hub.official_times.get((drv, lap))
        if off is not None:
            marks = self.lap_marks(drv, lap)
            if marks is not None:
                i1, i2 = self._sector_idx
                pts = (0, i1, i2, len(marks) - 1)
                for k in range(3):
                    if off[k] != off[k]:
                        continue
                    interp = float(marks[pts[k + 1]] - marks[pts[k]])
                    if np.isfinite(interp) and interp > 0.0:
                        scales[k] = float(off[k]) / interp
            buf = self.hub.buffers.get(drv)
            if (buf is not None and lap < buf.current_lap()
                    and all(o == o for o in off[:3])):
                # vuelta cerrada con los 3 oficiales: inmutable, cachear
                self._scale_cache[(drv, lap)] = scales
        return scales

    def _scale_per_micro(self, scales: np.ndarray) -> np.ndarray:
        """Expande los 3 factores de sector a un factor por µ."""
        counts = [b - a for a, b in self.sector_slices()]
        return np.repeat(scales, counts)

    def micro_times(self, drv: str, lap: int) -> np.ndarray | None:
        """µsectores de la vuelta, re-escalados sector a sector para que
        los µ de cada sector sumen exacto el sector oficial si ya llegó."""
        marks = self.lap_marks(drv, lap)
        if marks is None:
            return None
        return np.diff(marks) * self._scale_per_micro(
            self._sector_scales(drv, lap))

    def _latest_segments(self, drv: str, idx: np.ndarray) -> tuple[np.ndarray, np.ndarray] | None:
        """Últimos segmentos rodantes entre las marcas `idx` (índices de
        marca de los cortes): cada segmento sale de la vuelta en curso si ya
        fue cruzado (cálculo en tiempo real) y, si no, de exactamente una
        vuelta atrás. Devuelve (tiempos, vuelta_de_origen)."""
        buf = self.hub.buffers.get(drv)
        if buf is None or not buf.n:
            return None
        cur = buf.current_lap()
        if cur < 1:
            return None

        def segments(lap: int) -> np.ndarray | None:
            marks = self.lap_marks(drv, lap)
            if marks is None:
                return None
            pts = marks[idx]
            return pts[1:] - pts[:-1]

        n = len(idx) - 1
        seg_cur = segments(cur)
        seg_prev = segments(cur - 1) if cur >= 2 else None
        if seg_cur is None and seg_prev is None:
            return None
        if seg_cur is None:
            seg_cur = np.full(n, np.nan)
        if seg_prev is None:
            seg_prev = np.full(n, np.nan)
        use_cur = np.isfinite(seg_cur)
        times = np.where(use_cur, seg_cur, seg_prev)
        laps = np.where(use_cur, cur, cur - 1)
        return times, laps

    def latest_micro_times(self, drv: str) -> tuple[np.ndarray, np.ndarray] | None:
        """µ rodantes, re-escalados al sector oficial de su vuelta de origen
        (cada µ puede venir de la vuelta en curso o de la anterior)."""
        data = self._latest_segments(drv, np.arange(self.n_micro() + 1))
        if data is None:
            return None
        times, laps = data
        times = times.copy()
        for lap in np.unique(laps):
            scales = self._sector_scales(drv, int(lap))
            if (scales != 1.0).any():
                sel = laps == lap
                times[sel] *= self._scale_per_micro(scales)[sel]
        return times, laps

    def latest_sector_times(self, drv: str) -> tuple[np.ndarray, np.ndarray] | None:
        """Sectores rodantes; cada valor se reemplaza por el oficial de su
        vuelta de origen apenas el feed lo publica (segundos después del
        cruce), quedando el interpolado solo para lo aún no cronometrado."""
        n = self.n_micro()
        i1, i2 = self._sector_idx
        data = self._latest_segments(drv, np.array([0, i1, i2, n]))
        if data is None:
            return None
        times, laps = data
        times = times.copy()
        for k in range(3):
            off = self.hub.official_times.get((drv, int(laps[k])))
            if off is not None and off[k] == off[k]:
                times[k] = off[k]
        return times, laps

    def latest_corner_speeds(self, drv: str, dists) -> tuple[np.ndarray, np.ndarray] | None:
        """Velocidad mínima en cada curva (ventana ±60 m alrededor del
        vértice), rodante: vuelta en curso donde ya la pasó, si no la
        anterior. Devuelve (velocidades, vuelta_de_origen)."""
        buf = self.hub.buffers.get(drv)
        if buf is None or not buf.n:
            return None
        cur = buf.current_lap()
        if cur < 1:
            return None
        n_corners = len(dists)
        speeds = np.full(n_corners, np.nan)
        laps = np.full(n_corners, cur)
        lapcol = buf.col("lap")
        half = 60.0
        for lap in (cur, cur - 1):
            if lap < 1 or not np.isnan(speeds).any():
                break
            i0 = int(np.searchsorted(lapcol, lap, side="left"))
            i1 = int(np.searchsorted(lapcol, lap, side="right"))
            if i1 - i0 < 2:
                continue
            d = buf.col("dist_lap")[i0:i1]
            v = buf.col("speed")[i0:i1]
            d_max = float(d[-1])
            for j, dc in enumerate(dists):
                if not np.isnan(speeds[j]):
                    continue
                if d_max < float(dc) + 20.0:  # todavía no pasó la curva
                    continue
                a, b = np.searchsorted(d, [float(dc) - half, float(dc) + half])
                if b - a >= 2:
                    speeds[j] = float(v[a:b].min())
                    laps[j] = lap
        return speeds, laps

    def last_completed_lap(self, drv: str) -> int | None:
        buf = self.hub.buffers.get(drv)
        if buf is None:
            return None
        laps = buf.completed_laps()
        return laps[-1] if laps else None

    def best_lap(self, drv: str) -> tuple[int, float] | None:
        buf = self.hub.buffers.get(drv)
        if buf is None:
            return None
        best: tuple[int, float] | None = None
        for lap in buf.completed_laps():
            lt = self.lap_time(drv, lap)
            if np.isfinite(lt) and (best is None or lt < best[1]):
                best = (lap, lt)
        return best

    # ---------------------------------------------------------------- gaps

    def position_time(self, drv: str) -> tuple[np.ndarray, np.ndarray] | None:
        """Posición de pista (m) y tiempo, cacheados por cantidad de muestras.

        Cada vuelta se ancla a SU cruce de meta (el final de una vuelta
        siempre es la línea): pos = (vuelta−1)·L + dist − (largo_real − L).
        Sin esto, las vueltas que no arrancan en la línea — la vuelta 1 de
        carrera (offset de grilla) o las out-laps (salida de boxes) —
        desplazaban la posición y el gap no daba 0 con autos igualados en
        pista. El largo real de cada vuelta cerrada se estima interpolando
        el paso entre las dos muestras del cruce; la vuelta en curso usa
        offset 0 (se corrige sola al cerrarse).
        """
        buf = self.hub.buffers.get(drv)
        if buf is None or buf.n < 2:
            return None
        L = self.hub.track_length
        # primer tramo observado sin cruce de meta (vuelta 1 en curso o
        # conexión a mitad de sesión): offset estimado por proyección sobre
        # el trazado (cuando llega el primer cruce lo reemplaza el exacto)
        cur_off = 0.0
        lap_all = buf.col("lap")
        if int(lap_all[-1]) == int(lap_all[0]):
            provisional = self.hub.provisional_start_offset(drv)
            if provisional is not None:
                cur_off = provisional
        cached = self._pos_cache.get(drv)
        if (cached is not None and cached[0] == buf.n
                and abs(cached[1] - L) < 1.0 and abs(cached[2] - cur_off) < 1.0):
            return cached[3]
        n = buf.n
        lap = buf.col("lap").astype(np.float64)
        d = buf.col("dist_lap").astype(np.float64)
        t = buf.col("t").astype(np.float64)
        lap_int = buf.col("lap")
        ends = np.flatnonzero(np.diff(lap_int) > 0)  # última muestra de cada vuelta cerrada
        if len(ends):
            starts = ends + 1
            v = buf.col("speed").astype(np.float64)
            dt = np.clip(t[starts] - t[ends], 0.0, 5.0)
            step = (v[ends] + v[starts]) / 2.0 / 3.6 * dt
            lap_lens = d[ends] + np.maximum(step - d[starts], 0.0)
            seg_offsets = np.concatenate((lap_lens - L, [cur_off]))
            seg_lens = np.diff(np.concatenate(([0], starts, [n])))
            offset = np.repeat(seg_offsets, seg_lens)
        else:
            offset = cur_off
        pos = (lap - 1.0) * L + np.minimum(d - offset, L)
        pos = np.maximum.accumulate(pos)  # np.interp exige X creciente
        result = (pos, t)
        self._pos_cache[drv] = (n, L, cur_off, result)
        return result

    def real_positions_ready(self, drv: str) -> bool:
        """True cuando la posición de pista del piloto es real: ya se observó
        un cruce de meta en los datos (ancla exacta), o hay offset provisional
        proyectado (vuelta 1 en la grilla o conexión a mitad de sesión)."""
        buf = self.hub.buffers.get(drv)
        if buf is None or not buf.n:
            return False
        lapcol = buf.col("lap")
        if int(lapcol[-1]) > int(lapcol[0]):
            return True
        return self.hub.provisional_start_offset(drv) is not None

    @staticmethod
    def _decimate(p: np.ndarray, t: np.ndarray, cap: int = 12000):
        """Reduce puntos manteniendo el último (para gaps con sesiones largas)."""
        n = len(p)
        if n <= cap:
            return p, t
        idx = np.arange(0, n, n // cap + 1)
        if idx[-1] != n - 1:
            idx = np.append(idx, n - 1)
        return p[idx], t[idx]

    def gap_series(self, drv: str, ref: str, max_points: int = 2500):
        """Serie (posición_en_pista, gap) de drv contra ref.

        gap > 0: drv va detrás. El eje X es distancia ((vuelta−1)·L + metro
        de vuelta): cada rama usa la posición del auto en que se midió.
        """
        # el gap se calcula solo con posiciones reales: en la vuelta 1 en
        # curso hace falta el offset de grilla estimado (proyección sobre el
        # trazado); la serie arranca en el fin del S1 (filtro x >= L/3)
        if not self.real_positions_ready(drv) or not self.real_positions_ready(ref):
            return None
        a = self.position_time(drv)
        r = self.position_time(ref)
        if a is None or r is None:
            return None
        pa, ta = self._decimate(*a)
        pr, tr = self._decimate(*r)
        # drv detrás: cuánto después pasó por donde ya pasó la referencia
        g_behind = ta - np.interp(pa, pr, tr, left=np.nan, right=np.nan)
        # drv delante: cuánto antes pasó por donde recién pasa la referencia
        g_ahead = np.interp(pr, pa, ta, left=np.nan, right=np.nan) - tr
        x = np.concatenate((pa, pr))
        y = np.concatenate((g_behind, g_ahead))
        # en la largada el gap no es representativo: se calcula recién a
        # partir del sector 1 de la vuelta 1 (posición L/3)
        ok = np.isfinite(y) & (x >= self.hub.track_length / 3.0)
        x, y = x[ok], y[ok]
        if not len(x):
            return None
        order = np.argsort(x, kind="stable")
        x, y = x[order], y[order]
        # suavizado ligero: el ruido de integración entre autos (~±15 m)
        # mete oscilaciones de ~±0,3 s muestra a muestra
        if len(y) >= 15:
            kernel = np.ones(7) / 7.0
            y = np.convolve(y, kernel, mode="same")
            y[:3] = y[3]
            y[-3:] = y[-4]
        if len(x) > max_points:
            stride = len(x) // max_points + 1
            x, y = x[::stride], y[::stride]
        return x, y
