"""Motor de análisis derivado: fuerzas G, zonas del circuito (curvas y
rectas), detección de clipping/derate de batería, lift & coast y métricas
de energía por vuelta. Todo se deriva de los canales ya presentes en el
hub (velocidad, acelerador, freno binario, marcha, DRS, posición) más la
geometría del trazado; ningún dato viene de afuera.

Física y límites honestos (feed a ~4-5 Hz, velocidad en km/h enteros):
- G longitudinal = dv/dt suavizado; picos de frenada reales ~4-5 G.
- G lateral = v²·κ con la curvatura κ del TRAZADO (mucho más estable que
  derivar las posiciones del auto); con signo (izquierda/derecha).
- Clipping/derate: a fondo, sin cambio de marcha ni transición de DRS, la
  aceleración se desploma a ~0 ANTES del final de la recta => se quedó sin
  deploy. Se mide en metros de recta "aplanada".
- Lift & coast: sin acelerador y sin freno a alta velocidad; se atribuye a
  la curva siguiente. Es gestión de combustible/energía.
- Los índices de batería son PROXIES cualitativos (como la barra ERS de la
  TV): no hay canal real de SOC en el feed.

Anti-spoiler por construcción: los buffers del hub solo contienen datos
hasta la posición actual del timeline.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

# --- geometría ---
CORNER_RADIUS = 600.0   # radio menor a esto => curva
ZONE_MIN_LEN = 25.0     # largo mínimo de una zona de curva (m)
ZONE_MERGE_GAP = 60.0   # huecos menores unen curvas (chicanes)
# --- detecciones ---
WOT = 98.0              # % de acelerador que cuenta como "a fondo"
DERATE_A_MAX = 0.03     # g: aceleración menor a esto a fondo = derate
DERATE_V_MIN = 55.0     # m/s (~200 km/h): solo rectas rápidas
DERATE_MIN_RUN = 3      # muestras seguidas: una sola con a≈0 es ruido
# el tramo final de la recta solo cuenta si el derate NACIÓ antes: un
# aplanado que empieza recién al final puede ser velocidad final natural
DERATE_TAIL_FRAC = 0.2
COAST_V_MIN = 25.0      # m/s: coast solo a velocidad de vuelta lanzada
# lift & coast REAL = acelerador soltado del todo (0%) y sin freno; el 5%
# tolera el ruido del sensor sin confundir un acelerador de mantenimiento
COAST_THROTTLE = 5.0
BRAKE_ON = 50.0         # el freno del feed es binario 0/100


@dataclass
class Zone:
    kind: str    # "corner" | "straight"
    d0: float    # metros de vuelta (inicio)
    d1: float    # metros de vuelta (fin)
    label: str


@dataclass
class LapMetrics:
    pit: bool = False        # vuelta de entrada/salida de boxes
    caution: bool = False    # hubo SC/VSC/amarilla general en la vuelta
    wot_frac: float = 0.0    # fracción de la vuelta a fondo (por distancia)
    straight_m: float = 0.0  # metros recorridos en zonas de recta
    wot_straight_m: float = 0.0  # metros a fondo en recta (base del deploy)
    deploy_m: float = 0.0    # metros a fondo en recta SIN derate
    derate_total: float = 0.0
    coast_total: float = 0.0
    brake_e: float = 0.0     # Σ caída de v² frenando (∝ energía, proxy)
    coast_e: float = 0.0     # Σ caída de v² en coast (cosecha en overrun)
    derate_m: dict = field(default_factory=dict)   # {zona: metros}
    derate_start: dict = field(default_factory=dict)  # {zona: m de inicio}
    derate_end: dict = field(default_factory=dict)    # {zona: m de fin}
    coast_m: dict = field(default_factory=dict)    # {zona curva: metros}
    coast_n: dict = field(default_factory=dict)    # {zona curva: eventos}
    corners: dict = field(default_factory=dict)    # {zona: (vmin, alat_max)}


def _smooth(arr: np.ndarray, w: int) -> np.ndarray:
    if w < 2 or len(arr) < w:
        return arr.astype(float)
    kernel = np.ones(w) / w
    pad = np.pad(arr.astype(float), (w // 2, w - 1 - w // 2), mode="edge")
    return np.convolve(pad, kernel, mode="valid")


def signed_curvature(xs: np.ndarray, ys: np.ndarray,
                     dist: np.ndarray) -> np.ndarray:
    """κ con signo a lo largo del trazado (parametrizado por arco). El
    trazado es un lazo cerrado: se calcula con envoltura para que la meta
    no genere una curva falsa por el borde del array."""
    pad = min(12, len(xs) - 1)
    span = float(dist[-1] - dist[0]) + float(
        np.median(np.diff(dist)))  # paso de cierre del lazo
    xs_w = np.concatenate([xs[-pad:], xs, xs[:pad]])
    ys_w = np.concatenate([ys[-pad:], ys, ys[:pad]])
    d_w = np.concatenate([dist[-pad:] - span, dist, dist[:pad] + span])
    dx = np.gradient(xs_w, d_w)
    dy = np.gradient(ys_w, d_w)
    ddx = np.gradient(dx, d_w)
    ddy = np.gradient(dy, d_w)
    denom = np.maximum((dx * dx + dy * dy) ** 1.5, 1e-12)
    spacing = max(float(np.median(np.diff(dist))), 1e-6)
    w = max(3, int(round(25.0 / spacing)) | 1)
    return _smooth((dx * ddy - dy * ddx) / denom, w)[pad:pad + len(xs)]


def convex_hull(x: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Envolvente convexa (cadena monótona de Andrew) de la nube de puntos:
    el contorno del círculo de fricción que se compara entre pilotos."""
    pts = sorted(set(zip(x.tolist(), y.tolist())))
    if len(pts) < 3:
        arr = np.array(pts) if pts else np.zeros((0, 2))
        return arr[:, 0] if len(arr) else np.array([]), \
            arr[:, 1] if len(arr) else np.array([])

    def cross(o, a, b):
        return ((a[0] - o[0]) * (b[1] - o[1])
                - (a[1] - o[1]) * (b[0] - o[0]))

    lower: list = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper: list = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    hull = lower[:-1] + upper[:-1]
    hull.append(hull[0])  # cerrar el contorno
    arr = np.array(hull)
    return arr[:, 0], arr[:, 1]


def fit_trend(x: np.ndarray, y: np.ndarray,
              kind: str) -> tuple[np.ndarray, np.ndarray] | None:
    """Curva de tendencia sobre una nube de puntos: lineal, cuadrática o
    exponencial (y = A·e^{Bx}, con el signo dominante de y). None si no
    hay puntos suficientes o el tipo no aplica."""
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    ok = np.isfinite(x) & np.isfinite(y)
    x, y = x[ok], y[ok]
    # 5 puntos alcanzan para una tendencia por vuelta (stints cortos)
    if len(x) < 5 or float(x.max() - x.min()) < 1e-6:
        return None
    xs = np.linspace(float(x.min()), float(x.max()), 80)
    if kind == "linear":
        ys = np.polyval(np.polyfit(x, y, 1), xs)
    elif kind == "quadratic":
        ys = np.polyval(np.polyfit(x, y, 2), xs)
    elif kind == "exponential":
        sign = 1.0 if float(np.median(y)) >= 0 else -1.0
        mag = sign * y
        good = mag > 1e-3
        if int(good.sum()) < 5:
            return None
        coef = np.polyfit(x[good], np.log(mag[good]), 1)
        ys = sign * np.exp(np.polyval(coef, xs))
    else:
        return None
    return xs, ys


def _min_run(mask: np.ndarray, n: int) -> np.ndarray:
    """Filtra rachas de True más cortas que n muestras (una muestra suelta
    con a≈0 no es un derate: exigir continuidad)."""
    if n <= 1 or not mask.any():
        return mask
    out = np.zeros_like(mask)
    idx = np.flatnonzero(np.diff(np.concatenate(([0], mask.view(np.int8),
                                                 [0]))))
    for start, end in zip(idx[::2], idx[1::2]):
        if end - start >= n:
            out[start:end] = True
    return out


def find_zones(dist: np.ndarray, kappa: np.ndarray, track_len: float,
               corners: list) -> list[Zone]:
    """Divide la vuelta en curvas (|κ| alto sostenido) y rectas. Toda
    curva OFICIAL genera zona aunque sea un viraje suave que la curvatura
    no supere el umbral (el usuario piensa en T1…Tn). Las curvas se
    etiquetan con los vértices oficiales que caen dentro; las rectas, por
    la curva a la que llegan."""
    mask = np.abs(kappa) > (1.0 / CORNER_RADIUS)
    # tramos de curva: runs de True con largo mínimo
    runs: list[list[float]] = []
    start = None
    for i, m in enumerate(mask):
        if m and start is None:
            start = i
        elif not m and start is not None:
            runs.append([float(dist[start]), float(dist[i - 1])])
            start = None
    if start is not None:
        runs.append([float(dist[start]), float(dist[-1])])
    runs = [r for r in runs if r[1] - r[0] >= ZONE_MIN_LEN]
    # vértices oficiales fuera de todo run: carvar una zona alrededor
    apex_ds = sorted(float(d) for _lbl, d, _x, _y in corners)
    for d in apex_ds:
        if not any(r[0] - 20.0 <= d <= r[1] + 20.0 for r in runs):
            runs.append([max(0.0, d - 45.0), min(track_len, d + 45.0)])
    runs.sort()
    # unir chicanes (huecos cortos entre curvas)
    merged: list[list[float]] = []
    for run in runs:
        if merged and run[0] - merged[-1][1] < ZONE_MERGE_GAP:
            merged[-1][1] = max(merged[-1][1], run[1])
        else:
            merged.append(run)

    apexes = sorted((float(d), str(lbl)) for lbl, d, _x, _y in corners)
    zones: list[Zone] = []
    for d0, d1 in merged:
        inside = [lbl for d, lbl in apexes if d0 - 20.0 <= d <= d1 + 20.0]
        label = (inside[0] if len(inside) == 1
                 else f"{inside[0]}-{inside[-1]}" if inside
                 else f"C@{d0:.0f}m")
        zones.append(Zone("corner", d0, d1, label))
    # rectas: los complementos (la que contiene la meta se marca "main")
    straights: list[Zone] = []
    bounds = [0.0] + [b for z in zones for b in (z.d0, z.d1)] + [track_len]
    for i in range(0, len(bounds), 2):
        s0, s1 = bounds[i], bounds[i + 1]
        if s1 - s0 < 60.0:
            continue
        nxt = next((z.label for z in zones if z.d0 >= s1 - 1.0),
                   zones[0].label if zones else "T1")
        main = " (main)" if s0 <= 1.0 or s1 >= track_len - 1.0 else ""
        straights.append(Zone("straight", s0, s1, f"Str → {nxt}{main}"))
    out = zones + straights
    out.sort(key=lambda z: z.d0)
    return out


PROFILE_BIN_M = 2.0       # resolución del perfil aprendido por curva
PROFILE_MIN_PASSES = 30   # pasadas mínimas para activar el modelo
PROFILE_RESIDUAL = 0.06   # desvío relativo máx.: más que esto = pasada
                          # atípica (error real del piloto), se conserva
                          # el dato crudo


class CornerProfiles:
    """Perfiles de velocidad por curva aprendidos del ENSAMBLE de vueltas.

    El feed muestrea a ~4-5 Hz: una vuelta individual puede no pisar el
    apex y su Vmin queda sobreestimada. Pero las fases de muestreo
    difieren entre pasadas: cientos de vueltas cubren la curva densamente.
    El modelo acumula v(d) en bins finos y, para una vuelta puntual,
    ajusta el perfil por escala a sus muestras → Vmin reconstruida aunque
    ningún tick haya caído en el mínimo. Si la pasada no se parece al
    perfil (residuo alto: trompo, error), se conserva el dato crudo."""

    def __init__(self, min_passes: int = PROFILE_MIN_PASSES):
        self.min_passes = min_passes
        self.zones: dict[int, dict] = {}
        self.version = 0  # crece cuando una curva alcanza el mínimo

    def reset(self) -> None:
        self.zones.clear()
        self.version += 1

    def _zone_state(self, zi: int, zone: Zone) -> dict:
        state = self.zones.get(zi)
        n_bins = max(2, int((zone.d1 - zone.d0) / PROFILE_BIN_M))
        if state is None or len(state["sum"]) != n_bins:
            state = self.zones[zi] = {
                "label": zone.label, "d0": zone.d0, "d1": zone.d1,
                "sum": np.zeros(n_bins), "cnt": np.zeros(n_bins),
                "passes": 0, "seen": set(),
            }
        return state

    def add_pass(self, zi: int, zone: Zone, dists: np.ndarray,
                 speeds: np.ndarray, key=None) -> None:
        """Acumula UNA pasada; `key` (piloto, vuelta) evita re-entrenar la
        misma vuelta cuando las métricas cacheadas se recalculan."""
        if len(dists) < 3:
            return
        state = self._zone_state(zi, zone)
        if key is not None:
            if key in state["seen"]:
                return
            state["seen"].add(key)
        idx = np.clip(((dists - zone.d0) / PROFILE_BIN_M).astype(int),
                      0, len(state["sum"]) - 1)
        np.add.at(state["sum"], idx, speeds)
        np.add.at(state["cnt"], idx, 1.0)
        state["passes"] += 1
        if state["passes"] == self.min_passes:
            self.version += 1  # recién ahora el modelo rige: recalcular

    def ready(self, zi: int) -> bool:
        state = self.zones.get(zi)
        return state is not None and state["passes"] >= self.min_passes

    def profile(self, zi: int) -> tuple[np.ndarray, np.ndarray] | None:
        state = self.zones.get(zi)
        if state is None:
            return None
        cnt = state["cnt"]
        ok = cnt > 0
        if ok.sum() < 3:
            return None
        centers = (state["d0"] + (np.arange(len(cnt)) + 0.5)
                   * PROFILE_BIN_M)
        mean = np.where(ok, state["sum"] / np.maximum(cnt, 1.0), np.nan)
        return centers[ok], mean[ok]

    def refine_vmin(self, zi: int, dists: np.ndarray,
                    speeds: np.ndarray) -> float | None:
        """Vmin reconstruida de UNA pasada: escala el perfil a sus
        muestras. None si el modelo no está listo o la pasada es atípica."""
        if not self.ready(zi) or len(dists) < 3:
            return None
        prof = self.profile(zi)
        if prof is None:
            return None
        centers, mean = prof
        model_at = np.interp(dists, centers, mean)
        good = model_at > 1.0
        if good.sum() < 3:
            return None
        scale = float(np.median(speeds[good] / model_at[good]))
        resid = np.abs(speeds[good] - scale * model_at[good]) \
            / np.maximum(speeds[good], 1.0)
        if float(resid.max()) > PROFILE_RESIDUAL:
            return None
        return scale * float(np.nanmin(mean))

    # ------------------------------------------------------- persistencia

    def to_json(self) -> dict:
        return {str(zi): {
            "label": s["label"], "d0": s["d0"], "d1": s["d1"],
            "sum": [round(float(x), 2) for x in s["sum"]],
            "cnt": [int(x) for x in s["cnt"]],
            "passes": int(s["passes"]),
        } for zi, s in self.zones.items() if s["passes"] > 0}

    def load_json(self, data: dict, zones: list[Zone]) -> int:
        """Adopta perfiles guardados que sigan calzando con las zonas
        actuales (por etiqueta y arranque ±30 m). Devuelve cuántos."""
        adopted = 0
        by_label = {z.label: (i, z) for i, z in enumerate(zones)
                    if z.kind == "corner"}
        for saved in data.values():
            hit = by_label.get(saved.get("label"))
            if hit is None:
                continue
            zi, zone = hit
            if abs(float(saved.get("d0", -1e9)) - zone.d0) > 30.0:
                continue
            state = self._zone_state(zi, zone)
            arr_sum = np.asarray(saved.get("sum", []), dtype=float)
            arr_cnt = np.asarray(saved.get("cnt", []), dtype=float)
            if len(arr_sum) != len(state["sum"]):
                continue
            state["sum"] += arr_sum
            state["cnt"] += arr_cnt
            state["passes"] += int(saved.get("passes", 0))
            adopted += 1
        if adopted:
            self.version += 1
        return adopted


class AnalysisEngine:
    """Canales derivados por auto + métricas por vuelta, con caché (los
    buffers solo crecen; una vuelta cerrada no cambia)."""

    def __init__(self, hub):
        self.hub = hub
        self._zones: list[Zone] = []
        self._zones_sig: tuple | None = None
        self._kappa: tuple | None = None  # (dist, κ) del trazado
        self._zone_kmax: dict[int, float] = {}
        self._chan: dict[str, tuple[int, dict]] = {}
        self._laps: dict[tuple[str, int], LapMetrics] = {}
        # refinamiento por perfiles de curva (toggle en Settings): el
        # entrenamiento acumula SIEMPRE; el flag decide si se aplica
        self.profiles = CornerProfiles()
        self.refine = False
        self._profiles_seen = 0
        self._profiles_loaded_key: str | None = None

    def reset(self) -> None:
        self._zones = []
        self._zones_sig = None
        self._kappa = None
        self._zone_kmax.clear()
        self._chan.clear()
        self._laps.clear()
        self.profiles.reset()
        self._profiles_loaded_key = None

    def set_refine(self, on: bool) -> None:
        """Aplica/retira el refinamiento: las métricas cacheadas se
        recalculan con el nuevo modo."""
        on = bool(on)
        if on != self.refine:
            self.refine = on
            self._laps.clear()

    # -------------------------------------------- persistencia del modelo

    def _profiles_path(self):
        from . import config

        key = self.hub.circuit_key()
        if not key:
            return None
        folder = config.cache_dir() / "profiles"
        folder.mkdir(parents=True, exist_ok=True)
        return folder / f"{key}.json"

    def _maybe_load_profiles(self) -> int:
        """Carga el entrenamiento previo del circuito una vez, cuando la
        geometría ya está (reintenta mientras no haya curvas)."""
        import json

        key = self.hub.circuit_key()
        if not key or key == self._profiles_loaded_key:
            return 0
        if not any(z.kind == "corner" for z in self._zones):
            return 0
        self._profiles_loaded_key = key
        path = self._profiles_path()
        if path is None or not path.exists():
            return 0
        try:
            data = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            return 0
        return self.profiles.load_json(data, self._zones)

    def save_profiles(self) -> None:
        import json

        path = self._profiles_path()
        data = self.profiles.to_json()
        if path is None or not data:
            return
        try:
            path.write_text(json.dumps(data), "utf-8")
        except OSError:
            pass

    # ------------------------------------------------------------ geometría

    def _ensure_geometry(self) -> bool:
        mapping = self.hub.outline_dist_map()
        if mapping is None:
            return False
        dist, xs, ys = mapping
        sig = (len(dist), round(self.hub.track_length, 1),
               len(self.hub.corners))
        if sig == self._zones_sig:
            return True
        self._zones_sig = sig
        # las coordenadas del trazado vienen en unidades crudas del feed
        # (Position.z usa décimas de metro): normalizar κ a 1/metro con el
        # largo de arco real vs el largo oficial de la vuelta — sin esto
        # solo las horquillas superaban el umbral de curva y la G lateral
        # quedaba subestimada en esa misma proporción
        raw_len = float(np.hypot(np.diff(xs), np.diff(ys)).sum())
        unit = raw_len / max(self.hub.track_length, 1.0)
        kappa = signed_curvature(xs, ys, dist) * max(unit, 1e-9)
        self._kappa = (dist, kappa)
        self._zones = find_zones(dist, kappa, self.hub.track_length,
                                 self.hub.corners)
        self._zone_kmax = {}
        for zi, zone in enumerate(self._zones):
            if zone.kind == "corner":
                sel = (dist >= zone.d0) & (dist <= zone.d1)
                self._zone_kmax[zi] = (float(np.abs(kappa[sel]).max())
                                       if sel.any() else 0.0)
        self._laps.clear()   # las zonas cambiaron: métricas de cero
        self._chan.clear()
        return True

    def zones(self) -> list[Zone]:
        self._ensure_geometry()
        self._maybe_load_profiles()
        # el modelo de curvas recién activado (por entrenamiento o carga)
        # debe verse en las métricas ya cacheadas
        if self.refine and self.profiles.version != self._profiles_seen:
            self._profiles_seen = self.profiles.version
            self._laps.clear()
        return self._zones

    def zone_mask(self, dists: np.ndarray, selector) -> np.ndarray:
        """Máscara de muestras dentro de las zonas elegidas. selector:
        ("all", None) | ("kind", "corner"|"straight") | ("zone", índice) |
        ("multi", índices)."""
        zones = self.zones()
        kind, arg = selector
        if kind == "all" or not zones:
            return np.ones(len(dists), dtype=bool)
        if kind == "zone":
            zones = [zones[int(arg)]] if 0 <= int(arg) < len(zones) else []
        elif kind == "multi":
            zones = [zones[i] for i in arg if 0 <= int(i) < len(zones)]
        else:
            zones = [z for z in zones if z.kind == arg]
        mask = np.zeros(len(dists), dtype=bool)
        for z in zones:
            mask |= (dists >= z.d0) & (dists <= z.d1)
        return mask

    # ------------------------------------------------------------- canales

    def channels(self, drv: str) -> dict | None:
        """Arrays alineados con el buffer del auto: v (m/s), a_lon y a_lat
        en g (lateral con signo), más los crudos y máscaras de guarda."""
        buf = self.hub.buffers.get(drv)
        if buf is None or buf.n < 8 or not self._ensure_geometry():
            return None
        cached = self._chan.get(drv)
        if cached is not None and cached[0] == buf.n:
            return cached[1]
        n = buf.n
        t = buf.col("t").astype(float)
        # dt mínimo de 20 ms: timestamps duplicados o casi (lotes del feed)
        # dispararían gradientes absurdos que contaminan la detección
        dt = np.maximum(np.diff(t), 0.02)
        t = np.concatenate(([t[0]], t[0] + np.cumsum(dt)))
        # limpieza de picos imposibles del feed antes de derivar
        from .timing import TimingAnalyzer as _TA

        raw_ms = _TA._despike(buf.col("speed").astype(float)) / 3.6
        # v suavizada para derivar (ruido de km/h enteros); la CRUDA queda
        # aparte: el suavizado aplana los mínimos del apex y sesgaría la
        # Vmin y el entrenamiento de perfiles
        v = _smooth(raw_ms, 3)
        # a = dv/dt suavizada: la velocidad viene en km/h enteros a ~4-5 Hz;
        # sin doble suavizado el ruido de cuantización llega a ±0.05 g
        a_lon = _smooth(np.clip(np.gradient(v, t) / 9.81, -8.0, 8.0), 3)
        dist = np.clip(buf.col("dist_lap").astype(float), 0.0,
                       self.hub.track_length)
        kd, kappa = self._kappa
        a_lat = np.clip(v * v * np.interp(dist, kd, kappa) / 9.81, -8.0, 8.0)
        gear = buf.col("gear")
        throttle = buf.col("throttle").astype(float)
        drs_open = np.isin(buf.col("drs"), (10, 12, 14))
        # guardas: el corte de torque de un cambio, la transición de DRS y
        # el instante de soltar/pisar el acelerador producen la misma firma
        # que un derate (a≈0 a fondo) — excluir el entorno de cada uno
        guard = np.zeros(n, dtype=bool)
        for series, offs in ((gear, (-1, 0, 1)),
                             (drs_open, (-1, 0, 1, 2)),
                             (throttle >= WOT, (-1, 0, 1))):
            change = np.flatnonzero(np.diff(series.astype(np.int8)) != 0)
            for off in offs:
                guard[np.clip(change + off, 0, n - 1)] = True
        chan = {
            "t": t, "lap": buf.col("lap"), "dist": dist, "v": v,
            "v_raw": raw_ms,
            "a_lon": a_lon, "a_lat": a_lat,
            "throttle": throttle,
            "brake": buf.col("brake").astype(float),
            "guard": guard,
        }
        self._chan[drv] = (n, chan)
        return chan

    # -------------------------------------------------- métricas por vuelta

    def completed_laps(self, drv: str) -> list[int]:
        buf = self.hub.buffers.get(drv)
        return buf.completed_laps() if buf is not None and buf.n else []

    def lap_metrics(self, drv: str, lap: int) -> LapMetrics | None:
        key = (drv, int(lap))
        cached = self._laps.get(key)
        if cached is not None:
            return cached
        chan = self.channels(drv)
        if chan is None:
            return None
        lapcol = chan["lap"]
        i0 = int(np.searchsorted(lapcol, lap, side="left"))
        i1 = int(np.searchsorted(lapcol, lap, side="right"))
        if i1 - i0 < 10:
            return None
        sl = {k: arr[i0:i1] for k, arr in chan.items()}
        m = LapMetrics()
        t0, t1 = float(sl["t"][0]), float(sl["t"][-1])
        # vuelta de boxes / bajo neutralización: excluir de los promedios
        for visit in self.hub.pit_lane.get(drv, []):
            v_in = float(visit[1])
            v_out = float(visit[2]) if visit[2] is not None else v_in
            if v_in <= t1 and v_out >= t0:
                m.pit = True
        for s0, s1, code in self.hub.track_status:
            if code != "1" and s0 <= t1 and s1 >= t0:
                m.caution = True

        ds = np.diff(sl["dist"])
        ds = np.where((ds < 0) | (ds > 200.0), 0.0, ds)  # cruce de meta
        mid = sl["dist"][:-1]
        wot = sl["throttle"][:-1] >= WOT
        m.wot_frac = float(ds[wot].sum() / max(ds.sum(), 1e-9))

        v = sl["v"]
        dke = np.maximum(v[:-1] ** 2 - v[1:] ** 2, 0.0)
        braking = sl["brake"][:-1] >= BRAKE_ON
        m.brake_e = float(dke[braking].sum())
        coastable = ((sl["throttle"][:-1] < COAST_THROTTLE) & ~braking
                     & (v[:-1] > COAST_V_MIN))
        m.coast_e = float(dke[coastable].sum())

        zones = self.zones()
        corner_idx = [i for i, z in enumerate(zones) if z.kind == "corner"]
        for zi, zone in enumerate(zones):
            in_zone = (mid >= zone.d0) & (mid <= zone.d1)
            if not in_zone.any():
                continue
            if zone.kind == "straight":
                m.straight_m += float(ds[in_zone].sum())
                wot_m = float(ds[in_zone & wot].sum())
                m.wot_straight_m += wot_m
                usable = (in_zone & wot & ~sl["guard"][:-1]
                          & (v[:-1] > DERATE_V_MIN))
                raw = usable & (sl["a_lon"][:-1] <= DERATE_A_MAX)
                raw = _min_run(raw, DERATE_MIN_RUN)
                # rachas que NACEN en el tramo final de la recta pueden ser
                # la velocidad final natural: se descartan; las que nacen
                # antes y se extienden hasta el final son clipping real
                tail = zone.d1 - (zone.d1 - zone.d0) * DERATE_TAIL_FRAC
                derate = np.zeros_like(raw)
                edges = np.flatnonzero(np.diff(np.concatenate(
                    ([0], raw.view(np.int8), [0]))))
                for start, end in zip(edges[::2], edges[1::2]):
                    if mid[start] < tail:
                        derate[start:end] = True
                meters = float(ds[derate].sum())
                m.deploy_m += wot_m - float(ds[in_zone & wot & derate].sum())
                if meters > 0.0:
                    m.derate_m[zi] = meters
                    m.derate_start[zi] = float(mid[derate].min())
                    m.derate_end[zi] = float(mid[derate].max())
                    m.derate_total += meters
            elif int(in_zone.sum()) >= 3:
                d_pass = mid[in_zone]
                # velocidad CRUDA: el suavizado aplana el valle del apex
                v_pass = sl["v_raw"][:-1][in_zone]
                vmin = float(v_pass.min())
                alat = float(np.abs(sl["a_lat"][:-1][in_zone]).max())
                # entrenamiento del perfil de la curva (siempre acumula;
                # la clave piloto+vuelta evita duplicar en recomputes)
                self.profiles.add_pass(zi, zone, d_pass, v_pass,
                                       key=(drv, lap))
                if self.refine:
                    # submuestreo: si ningún tick pisó el apex, el perfil
                    # aprendido reconstruye la Vmin real de esta pasada
                    refined = self.profiles.refine_vmin(zi, d_pass, v_pass)
                    if refined is not None and refined < vmin:
                        vmin = float(refined)
                        kmax = self._zone_kmax.get(zi, 0.0)
                        alat = max(alat, vmin * vmin * kmax / 9.81)
                m.corners[zi] = (vmin, alat)

        # ---- lift & coast POR EVENTO: desde que suelta el acelerador
        # hasta que aplica el freno (o entra a la curva sin frenar), hacia
        # atrás desde cada ancla — la definición exacta del fenómeno; las
        # levantadas sueltas con re-aceleración (tráfico, error) no cuentan
        anchors: list[tuple[int, int | None]] = []  # (índice, zona curva)
        rise = np.flatnonzero(np.diff(np.concatenate(
            ([0], braking.view(np.int8)))) == 1)
        # re-aplicar el freno sin haber vuelto a fondo en el medio es la
        # MISMA frenada (doble mordida, coast entre frenos): un solo evento
        kept: list[int] = []
        for e in rise:
            if kept and not wot[kept[-1]:e].any():
                continue
            kept.append(int(e))
        rise = kept
        starts = np.array([zones[i].d0 for i in corner_idx]) \
            if corner_idx else np.array([])
        for e in rise:
            if v[e] <= COAST_V_MIN:
                continue  # frenadas lentas (boxes, trompos): fuera
            zi = None
            if corner_idx:
                # la frenada apunta a la primera curva que arranca delante
                # (30 m de tolerancia si el freno llega ya pisando la
                # curva); pasada la última, envuelve a la primera (meta)
                k = int(np.searchsorted(starts, mid[e] - 30.0, side="left"))
                zi = corner_idx[k % len(corner_idx)]
            anchors.append((int(e), zi))
        # entrada a curva SIN freno (curvas de lift puro): el primer punto
        # dentro de la curva, si no hubo freno en las muestras previas
        for zi in corner_idx:
            zone = zones[zi]
            inside = np.flatnonzero((mid >= zone.d0) & (mid <= zone.d1))
            if not len(inside):
                continue
            k = int(inside[0])
            if not braking[max(0, k - 8):k + 1].any() \
                    and sl["throttle"][k] < COAST_THROTTLE:
                anchors.append((k, zi))
        for e, zi in anchors:
            j = e
            while j > 0 and coastable[j - 1]:
                j -= 1
            meters = float(ds[j:e].sum())
            if meters <= 0.0 or zi is None:
                continue
            m.coast_m[zi] = m.coast_m.get(zi, 0.0) + meters
            m.coast_n[zi] = m.coast_n.get(zi, 0) + 1
        m.coast_total = float(sum(m.coast_m.values()))
        self._laps[key] = m
        return m
