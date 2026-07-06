"""Análisis de tiempos derivado de las muestras: vueltas, sectores,
microsectores y gaps. Funciona igual para las tres fuentes.

Método: se interpola el instante en que cada auto cruza marcas de
distancia fijas dentro de la vuelta (24 divisiones iguales; los sectores
son grupos de 8). Con telemetría a ~4-5 Hz la precisión es de ~±0,1 s:
sirve para comparar, no es el cronometraje oficial.

El "gap" entre dos autos es la diferencia de tiempo al pasar por la misma
posición de pista (vuelta × largo + metro de vuelta): positivo = detrás
de la referencia.
"""
from __future__ import annotations

import numpy as np

from .hub import DataHub

N_MICRO = 24          # microsectores por vuelta (divisible por 3)
SECTOR_STEP = N_MICRO // 3


class TimingAnalyzer:
    def __init__(self, hub: DataHub):
        self.hub = hub
        self._marks_cache: dict[tuple[str, int], np.ndarray] = {}
        self._pos_cache: dict[str, tuple[int, float, tuple]] = {}
        self._len_used = 0.0

    def clear(self) -> None:
        self._marks_cache.clear()
        self._pos_cache.clear()

    def _mark_dists(self) -> np.ndarray:
        return np.linspace(0.0, self.hub.track_length, N_MICRO + 1)

    def _check_track_len(self) -> None:
        if abs(self.hub.track_length - self._len_used) > 1.0:
            self._marks_cache.clear()
            self._pos_cache.clear()
            self._len_used = self.hub.track_length

    # ------------------------------------------------------- marcas por vuelta

    def lap_marks(self, drv: str, lap: int) -> np.ndarray | None:
        """Tiempo de cruce de cada marca de la vuelta (NaN si aún no cruzada).

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

        # ancla inicial: cruce de meta en dist 0
        if d[0] > 0.0:
            anchor = None
            if i0 > 0 and int(lapcol[i0 - 1]) == lap - 1:
                d_prev = float(buf.col("dist_lap")[i0 - 1]) - L
                t_prev = float(buf.col("t")[i0 - 1])
                if d_prev < 0.0:
                    anchor = (d_prev, t_prev)
                else:
                    # la vuelta previa integró de más: el cruce cayó entre
                    # ambas muestras (error acotado a medio período)
                    anchor = (0.0, (t_prev + float(t[0])) / 2.0)
            elif d[0] < L * 0.05:
                # sin vuelta previa: retro-extrapolar por velocidad
                v0 = max(float(buf.col("speed")[i0]), 5.0) / 3.6
                anchor = (0.0, float(t[0]) - min(d[0] / v0, 5.0))
            if anchor is not None:
                d = np.concatenate(([anchor[0]], d))
                t = np.concatenate(([anchor[1]], t))

        # ancla final: cruce de meta en dist L (solo vueltas ya cerradas)
        if d[-1] < L:
            anchor = None
            if i1 < buf.n and int(lapcol[i1]) == lap + 1:
                anchor = (float(buf.col("dist_lap")[i1]) + L, float(buf.col("t")[i1]))
            elif completed and (L - d[-1]) < L * 0.05:
                v1 = max(float(buf.col("speed")[i1 - 1]), 5.0) / 3.6
                anchor = (L + 1e-6, float(t[-1]) + min((L - d[-1]) / v1, 5.0))
            if anchor is not None and anchor[0] > d[-1]:
                d = np.append(d, anchor[0])
                t = np.append(t, anchor[1])

        marks = np.interp(self._mark_dists(), d, t, left=np.nan, right=np.nan)
        if completed:  # vuelta cerrada: inmutable, cachear
            self._marks_cache[(drv, lap)] = marks
        return marks

    # ------------------------------------------------------------ tiempos

    def lap_time(self, drv: str, lap: int) -> float:
        marks = self.lap_marks(drv, lap)
        if marks is None:
            return float("nan")
        return float(marks[-1] - marks[0])

    def sector_times(self, drv: str, lap: int) -> list[float]:
        marks = self.lap_marks(drv, lap)
        if marks is None:
            return [float("nan")] * 3
        return [
            float(marks[(k + 1) * SECTOR_STEP] - marks[k * SECTOR_STEP])
            for k in range(3)
        ]

    def micro_times(self, drv: str, lap: int) -> np.ndarray | None:
        marks = self.lap_marks(drv, lap)
        return None if marks is None else np.diff(marks)

    def _latest_segments(self, drv: str, step: int) -> tuple[np.ndarray, np.ndarray] | None:
        """Últimos segmentos rodantes de tamaño `step` marcas: cada segmento
        sale de la vuelta en curso si ya fue cruzado (cálculo en tiempo
        real) y, si no, de exactamente una vuelta atrás.

        Devuelve (tiempos, vuelta_de_origen) de largo N_MICRO // step.
        """
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
            pts = marks[::step]
            return pts[1:] - pts[:-1]

        n = N_MICRO // step
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
        return self._latest_segments(drv, 1)

    def latest_sector_times(self, drv: str) -> tuple[np.ndarray, np.ndarray] | None:
        return self._latest_segments(drv, SECTOR_STEP)

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
        # vuelta 1 en curso: offset de grilla estimado por proyección sobre
        # el trazado (cuando cierra la vuelta 1 lo reemplaza el exacto)
        cur_off = 0.0
        if int(buf.col("lap")[-1]) == 1:
            provisional = self.hub.provisional_lap1_offset(drv)
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
        """True cuando la posición de pista del piloto es real: vuelta 1
        cerrada, o vuelta 1 en curso con offset de grilla ya estimado."""
        buf = self.hub.buffers.get(drv)
        if buf is None or not buf.n:
            return False
        if buf.current_lap() >= 2:
            return True
        return self.hub.provisional_lap1_offset(drv) is not None

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
