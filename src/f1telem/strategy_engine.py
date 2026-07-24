"""Motor de estrategia en carrera (fase 1): un veredicto de acción por
auto, recalculado en vivo, con TRAZABILIDAD COMPLETA — cada decisión
registra qué consideró, qué midió, qué alternativas descartó y con qué
umbrales, para que cada indicador pueda perfeccionarse en fases futuras
sin arqueología.

Principios (pedidos explícitos):
- PASAR ES DIFÍCIL: reinsertarse a <2 s de otro auto equivale a quedar
  atrapado; el aire limpio vale más que la posición nominal.
- Ante SC/VSC la parada se abarata (la pérdida en pista se paga a ritmo
  neutralizado): el veredicto se recalcula al instante del deploy.
- Un rival directo que boxea abre una cuenta regresiva de respuesta.

Acciones (por prioridad): IN PIT · BOX NOW (SC/VSC barata) · COVER
(responder parada rival) · FREE STOP (parada gratis) · BOX FOR AIR
(atrapado en tráfico, rejoin limpio) · BOX SOON (goma al límite) ·
WATCH (amenaza de undercut) · STAY.

Fase 2 — medir lo que la fase 1 estimaba, y decidir con eso:
- SessionMeasures: pérdida REAL de una parada (verde), factor de
  abaratamiento SC/VSC medido (contra referencias que cruzaron el tramo
  de boxes DURANTE la neutralización) y ganancia de goma fresca medida
  del delta de ritmo alrededor de cada parada. Reemplazan a las
  estimaciones cuando hay muestras; cada traza cita la fuente.
- Tráfico también por POSICIÓN DE PISTA (mod vuelta): atrapa a los
  doblados, que por gap parecen lejos pero están justo ahí.
- Escáner de vuelta de parada (ahora..+5, gaps proyectados con la
  tendencia medida): green/yellow/red por vuelta candidata.
- COVER pondera la trampa del propio rejoin antes de pedir respuesta.

Fase 3 — proyectar hacia adelante:
- Compactación bajo SC: la proyección de rejoin ya no usa los gaps
  muestreados (optimistas mientras la fila se forma) sino quién es
  probable que se quede afuera (goma fresca / parada reciente) y te
  salte a ~1.2 s/auto de fila india; si todos boxean con vos, parar no
  cuesta orden relativo.
- Detector de CLIFF: ajuste cuadrático del stint — si la degradación
  acelera y ya cuesta caro por vuelta, BOX SOON urgente; el auto de
  atrás del que sufre el cliff recibe la ventana de ataque.
- Proyección a bandera: quedarse = deg × vueltas restantes × edad vs
  costo de la parada; si el neto favorece parar y quedan pocas vueltas,
  last call — cada vuelta esperada erosiona el neto.

Coherencia entre fases: el ENDGAME (≤ENDGAME_LAPS vueltas) apaga TODA
parada voluntaria — SC barata, cover, free stop, aire, cliff, last
call — porque la posición ya no se devuelve; BOX SOON por vida no
dispara si el stint sobrevive a la bandera; el escáner proyecta k≥1
con la ventana VERDE (la neutralización no dura); y amenaza, cover y
escáner comparten los mismos valores vigentes (medidos si los hay).
"""
from __future__ import annotations

import json
import math
import time
from collections import deque
from dataclasses import dataclass, field

import numpy as np

from . import config

# --- constantes de fase 1 (cada uso queda trazado) ---
VSC_FACTOR = 0.55        # la ventana de box se paga a este factor bajo VSC
SC_FACTOR = 0.45         # ídem bajo safety car
# rango REAL de amenaza: la ganancia acumulada de goma fresca (~2-3
# vueltas); la ventana NO entra — ambos autos la pagan al parar
UNDERCUT_RANGE = 4.0
FRESH_AGE_MIN = 5        # con goma más nueva que esto, parar no gana nada
ENDGAME_LAPS = 3         # vueltas finales: ya no se para ni bajo SC
AIR_MAX_LOSS = 2         # posiciones máximas a pagar por buscar aire
TRAFFIC_CLOSE = 2.0      # s: reinsertarse a menos de esto = atrapado
TRAFFIC_SPAN = 5.0       # s hacia atrás del rejoin que cuentan como zona
STUCK_GAP = 1.5          # s: pegado al de adelante = tráfico
STUCK_LAPS = 3           # vueltas seguidas pegado para declararlo atrapado
FRESH_ABSORB = 5         # vueltas de ventaja de goma para absorber undercut
STINT_LIFE_DROP = 1.2    # s/vuelta perdidos que marcan el fin útil del stint
PACK_SPACING = 1.2       # s/auto en la fila india detrás del SC
CLIFF_CURV = 0.015       # s/vuelta²: aceleración de deg que declara cliff
CLIFF_MARGINAL = 0.35    # s/vuelta perdidos AHORA para declarar cliff
FLAG_NET_MIN = 5.0       # s netos mínimos para el last-call a bandera
LAST_CALL_LAPS = 10      # vueltas finales donde aplica el last-call
COVER_WINDOW_S = 90.0    # s tras la parada rival en que se puede responder
LOG_MAX_BYTES = 2_000_000


@dataclass
class Advice:
    drv: str
    action: str              # STAY / BOX NOW / COVER / FREE STOP / ...
    reason: str              # frase corta para la fila
    urgency: int = 0         # 0 info · 1 atención · 2 urgente
    rejoin_txt: str = ""     # proyección si boxea ahora
    threats: list = field(default_factory=list)
    trace: list = field(default_factory=list)    # razonamiento completo
    factors: dict = field(default_factory=dict)  # valores crudos usados


def neutralization(hub) -> str | None:
    """SC / VSC activo en el instante del timeline (None si pista verde)."""
    t = hub.latest_t
    for t0, t1, code in hub.track_status:
        if t0 <= t <= t1:
            if str(code) == "4":
                return "SC"
            if str(code) in ("6", "7"):
                return "VSC"
    return None


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def neutral_between(hub, t0: float, t1: float) -> str | None:
    """Neutralización que pisa [t0, t1] (SC gana sobre VSC)."""
    found = None
    for a, b, code in hub.track_status:
        if a <= t1 and t0 <= b:
            if str(code) == "4":
                return "SC"
            if str(code) in ("6", "7"):
                found = "VSC"
    return found


class SessionMeasures:
    """Mediciones de la sesión (fase 2) a partir de las paradas REALES:

    - window: pérdida verde de una parada (cruce del tramo de boxes vs
      vueltas limpias de referencia, detención normalizada; reusa la
      maquinaria del PitWindowEstimator).
    - sc / vsc: factor de abaratamiento MEDIDO = pérdida de las paradas
      bajo neutralización / pérdida verde. La referencia de esas paradas
      son autos que cruzaron el MISMO tramo durante la MISMA
      neutralización (comparar contra ritmo verde inflaría la pérdida).
    - gain: rango de undercut medido = 2.5 × (ritmo viejo − ritmo nuevo)
      alrededor de cada parada, con vueltas limpias y sin in/out-lap.

    Cada valor lleva su cantidad de muestras; el motor sigue usando la
    estimación de fase 1 (y lo traza) mientras no haya suficientes."""

    def __init__(self, hub, analyzer):
        self.hub = hub
        self.analyzer = analyzer
        self._est = None
        self._stop_norm = 3.0
        self._seen = (-1, -1)
        self.window: tuple[float, int] | None = None
        self.sc: tuple[float, int] | None = None
        self.vsc: tuple[float, int] | None = None
        self.gain: tuple[float, int] | None = None

    def reset(self) -> None:
        self._seen = (-1, -1)
        self.window = self.sc = self.vsc = self.gain = None

    def update(self) -> None:
        """Recalcula solo si hay una parada cerrada nueva O avanzó la
        vuelta del líder. El conteo solo no alcanza: en replay las
        visitas llegan TODAS al cargar la sesión — sin mirar la vuelta,
        jamás se remediría a medida que crece la telemetría (las visitas
        fuera del rango de datos se descartan solas al medir)."""
        hub = self.hub
        closed = sum(1 for visits in hub.pit_lane.values()
                     for v in visits if v[2] is not None)
        lap = 0
        for b in hub.buffers.values():
            if b.n:
                cur = b.current_lap()
                if cur > lap:
                    lap = cur
        if (closed, lap) == self._seen:
            return
        self._seen = (closed, lap)
        if not closed:
            return
        if self._est is None:
            from .ui.pit_strategy import STOP_NORM, PitWindowEstimator
            self._est = PitWindowEstimator(self.hub, self.analyzer)
            self._stop_norm = float(STOP_NORM)
        self._measure_losses()
        self._measure_gain()

    def summary(self) -> str:
        parts = []
        if self.window:
            parts.append(f"pit loss {self.window[0]:.1f}s "
                         f"({self.window[1]} stops)")
        if self.sc:
            parts.append(f"SC ×{self.sc[0]:.2f} ({self.sc[1]})")
        if self.vsc:
            parts.append(f"VSC ×{self.vsc[0]:.2f} ({self.vsc[1]})")
        if self.gain:
            parts.append(f"fresh-tyre gain {self.gain[0]:.1f}s "
                         f"({self.gain[1]} stops)")
        return (" · ".join(parts) if parts else
                "no measurable stops yet — phase-1 estimates in use")

    # ---------------------------------------------------------- medición

    def _neutral_between(self, t0: float, t1: float) -> str | None:
        return neutral_between(self.hub, t0, t1)

    def _measure_losses(self) -> None:
        est = self._est
        bounds = est.window_bounds()
        if bounds is None:
            return
        w_start, span = bounds
        ref = est.reference(w_start, span)
        if ref is None:
            return
        ref_green, _n_ref = ref
        hub = self.hub
        L = hub.track_length
        buckets: dict[str, list[float]] = {"green": [], "SC": [], "VSC": []}
        for drv, visits in hub.pit_lane.items():
            pt = self.analyzer.position_time(drv)
            if pt is None or len(pt[0]) < 2:
                continue
            pos, t = pt
            for _lap, t_in, t_out in visits:
                if t_out is None or not (t[0] <= t_in <= t[-1]):
                    continue
                p_in = float(np.interp(t_in, t, pos))
                p0 = math.floor((p_in - w_start) / L) * L + w_start
                dur = est._duration(pos, t, p0, span)
                if dur is None:
                    continue
                stop = hub.pit_stationary_time(drv, float(t_in),
                                               float(t_out))
                neu = self._neutral_between(float(t_in), float(t_out))
                ref_t = (ref_green if neu is None else
                         self._ref_during(drv, w_start, span,
                                          float(t_in), float(t_out)))
                if ref_t is None:
                    continue
                buckets["green" if neu is None else neu].append(
                    dur - ref_t - (stop - self._stop_norm))
        if buckets["green"]:
            self.window = (float(np.median(buckets["green"])),
                           len(buckets["green"]))
        self._set_factor("sc", buckets["SC"])
        self._set_factor("vsc", buckets["VSC"])

    def _set_factor(self, name: str, losses: list[float]) -> None:
        if losses and self.window and self.window[0] > 3.0:
            f = _clamp(float(np.median(losses)) / self.window[0],
                       0.05, 1.2)
            setattr(self, name, (f, len(losses)))
        else:
            setattr(self, name, None)

    def _ref_during(self, drv: str, w_start: float, span: float,
                    t_in: float, t_out: float) -> float | None:
        """Cruce del tramo de boxes por autos que NO pararon, durante la
        misma neutralización (±60 s de la visita)."""
        hub = self.hub
        L = hub.track_length
        vals: list[float] = []
        for other in hub.buffers:
            if other == drv:
                continue
            pt = self.analyzer.position_time(other)
            if pt is None or len(pt[0]) < 2:
                continue
            pos, t = pt
            p_c = float(np.interp((t_in + t_out) / 2.0, t, pos))
            k0 = math.floor((p_c - w_start) / L)
            for k in (k0 - 1, k0, k0 + 1):
                p0 = k * L + w_start
                dur = self._est._duration(pos, t, p0, span)
                if dur is None:
                    continue
                t0 = float(np.interp(p0, pos, t))
                if t0 + dur < t_in - 60.0 or t0 > t_out + 60.0:
                    continue
                if self._est._in_pit_between(other, t0, t0 + dur):
                    continue
                if self._neutral_between(t0, t0 + dur) is None:
                    continue
                vals.append(dur)
        return float(np.median(vals)) if vals else None

    def _measure_gain(self) -> None:
        gains: list[float] = []
        for drv, visits in self.hub.pit_lane.items():
            for lap, _t_in, t_out in visits:
                if t_out is None:
                    continue
                lap = int(lap)
                old = self._clean_laps(drv, range(lap - 3, lap))
                new = self._clean_laps(drv, range(lap + 2, lap + 5))
                if len(old) >= 2 and len(new) >= 2:
                    # también las ganancias nulas o NEGATIVAS (compuesto
                    # más duro, pista que mejora): filtrarlas inflaría
                    # la mediana — sesgo de selección
                    gains.append(float(np.median(old) - np.median(new)))
        if gains:
            med = float(np.median(gains))
            self.gain = ((_clamp(2.5 * med, 1.0, 8.0), len(gains))
                         if med > 0.05 else None)

    def _clean_laps(self, drv: str, laps) -> list[float]:
        from .ui.pit_strategy import clean_at
        buf = self.hub.buffers.get(drv)
        if buf is None or not buf.n:
            return []
        out: list[float] = []
        for lap in laps:
            if lap < 1:
                continue
            lt = self.analyzer.lap_time(drv, lap)
            if lt != lt:
                continue
            tcol = buf.lap_slice(lap)["t"]
            if not len(tcol):
                continue
            t0, t1 = float(tcol[0]), float(tcol[-1])
            if not clean_at(self.hub, t0, t1):
                continue
            if self._est is not None \
                    and self._est._in_pit_between(drv, t0, t1):
                continue
            out.append(float(lt))
        return out


class StrategyEngine:
    """Evalúa la situación estratégica de cada auto. Reusa la medición de
    gaps/rejoin del panel Pit strategy y la degradación medida del stint.
    Cada cambio de veredicto se agrega al log en memoria y se persiste en
    strategy-log.jsonl (una línea JSON con TODOS los factores)."""

    def __init__(self, hub, analyzer):
        self.hub = hub
        self.analyzer = analyzer
        self.pit_window = 20.0   # lo actualiza la UI desde cfg strategy
        self.advices: dict[str, Advice] = {}
        self.log: deque = deque(maxlen=400)   # (t, lap, drv, action, reason)
        self._last_action: dict[str, str] = {}
        self._stuck_count: dict[str, int] = {}
        self._stuck_lap_seen: dict[str, int] = {}
        # snapshots de gaps para leer el gap PRE-parada de un rival (el
        # gap actual de un auto en boxes ya está inflado por la parada)
        self._gap_snaps: deque = deque(maxlen=180)  # (t, {drv: gap})
        self.measures = SessionMeasures(hub, analyzer)
        self._lap_s = 90.0   # vuelta del líder (pasada espacial y trends)
        self._log_path = None

    def reset(self) -> None:
        self.advices.clear()
        self.log.clear()
        self._last_action.clear()
        self._stuck_count.clear()
        self._stuck_lap_seen.clear()
        self._gap_snaps.clear()
        self.measures.reset()

    def _gap_before(self, t_ref: float, drv: str) -> float | None:
        """Gap al líder del auto en el último snapshot ANTERIOR a t_ref.
        Con tope de antigüedad: si el board estuvo cerrado y el snapshot
        más cercano es de hace varias vueltas, ese 'gap pre-parada' ya no
        describe nada — mejor None (la rama que lo usa se abstiene)."""
        for t, snap in reversed(self._gap_snaps):
            if t < t_ref:
                if t_ref - t > 2.0 * self._lap_s + 30.0:
                    return None
                return snap.get(drv)
        return None

    # ------------------------------------------------------------ insumos

    def _stint(self, drv: str) -> dict:
        """Stint actual medido: compuesto, edad, pendiente de degradación
        (s/vuelta, ajuste lineal sin vueltas de boxes) y vida útil restante
        estimada hasta perder STINT_LIFE_DROP s/vuelta."""
        hub = self.hub
        an = self.analyzer
        out = {"compound": "", "age": 0, "slope": float("nan"),
               "life": None, "laps_used": 0, "cliff": False,
               "curv": float("nan"), "marginal": float("nan")}
        buf = hub.buffers.get(drv)
        if buf is None or not buf.n:
            return out
        cur = buf.current_lap()
        tyres = hub.tyres_until_now(drv)
        if tyres:
            key = cur if cur in tyres else max(tyres)
            out["compound"], out["age"] = tyres[key]
            if cur > key:
                out["age"] += cur - key
        start = max(1, cur - out["age"])
        pit_laps = {p_lap for p_lap, _t in hub.pits.get(drv, [])}
        xs, ys = [], []
        for lap in range(start, cur):
            if lap in pit_laps or (lap - 1) in pit_laps:
                continue
            lt = an.lap_time(drv, lap)
            if lt == lt:
                xs.append(lap)
                ys.append(lt)
        out["laps_used"] = len(xs)
        if len(xs) >= 4:
            slope = float(np.polyfit(xs, ys, 1)[0])
            out["slope"] = slope
            if slope > 0.02:
                # vueltas hasta que la caída acumulada llegue al umbral
                lost_now = slope * out["age"]
                out["life"] = max(0, int((STINT_LIFE_DROP - lost_now)
                                         / slope))
        if len(xs) >= 6:
            # cliff: la degradación ACELERA (curvatura positiva) y ya
            # cuesta caro por vuelta — la recta del slope no lo ve venir
            a, b, _c = np.polyfit(xs, ys, 2)
            out["curv"] = float(a)
            out["marginal"] = float(2.0 * a * xs[-1] + b)
            if out["curv"] > CLIFF_CURV \
                    and out["marginal"] >= CLIFF_MARGINAL:
                out["cliff"] = True
        return out

    def _likely_stays_out(self, drv: str, stints: dict) -> bool:
        """Bajo SC casi todos boxean; se queda afuera el que no gana nada
        parando: goma fresca o parada recién hecha."""
        st = stints.get(drv, {})
        if st.get("age", 99) < FRESH_AGE_MIN:
            return True
        visit = self.hub.last_pit_visit(drv)
        if visit is not None:
            t_ref = visit[2] if visit[2] is not None else visit[1]
            if self.hub.latest_t - float(t_ref) <= 90.0:
                return True
        return False

    def _traffic_at(self, gaps: dict, drv: str, gap_after: float,
                    window_now: float | None = None,
                    spatial: bool = False) -> dict:
        """Densidad de tráfico alrededor del punto de reinserción. La
        pasada por gaps cubre a los autos en vuelta; con spatial=True se
        suma la pasada por POSICIÓN DE PISTA (mod vuelta), que atrapa
        también a los doblados: por gap parecen a +1 vuelta pero están
        físicamente donde caerá el rejoin."""
        ahead_close = []
        zone = []
        seen: set[str] = set()
        for d, g in gaps.items():
            if d == drv or g is None:
                continue
            delta = gap_after - g   # + = ese auto queda delante
            if 0.0 <= delta <= TRAFFIC_CLOSE:
                ahead_close.append((d, delta))
                seen.add(d)
            elif -TRAFFIC_SPAN <= delta < 0.0:
                zone.append((d, -delta))
                seen.add(d)
        lapped: set[str] = set()
        if spatial and window_now is not None:
            self._spatial_pass(drv, window_now, seen, ahead_close, zone,
                               lapped)
        ahead_close.sort(key=lambda e: e[1])
        zone.sort(key=lambda e: e[1])
        return {"ahead_close": ahead_close, "zone": zone,
                "clear": not ahead_close, "lapped": lapped}

    def _spatial_pass(self, drv: str, window_now: float, seen: set,
                      ahead_close: list, zone: list, lapped: set) -> None:
        """Un rival que hoy está x segundos detrás EN PISTA queda delante
        tras la parada si x < ventana vigente — valga lo que valga su gap
        acumulado (doblados incluidos)."""
        hub = self.hub
        L = hub.track_length
        v = L / self._lap_s if self._lap_s > 0 else 0.0
        buf = hub.buffers.get(drv)
        if v <= 0 or buf is None or not buf.n:
            return
        now = hub.latest_t

        def s_at_now(b) -> float:
            return (float(b.col("dist_lap")[-1])
                    + (now - float(b.col("t")[-1])) * v)

        s_own = s_at_now(buf)
        lap_own = buf.current_lap()
        for r, rbuf in hub.buffers.items():
            if r == drv or r in seen or not rbuf.n:
                continue
            if now - float(rbuf.col("t")[-1]) > 20.0:
                continue    # retirado o sin datos frescos
            visit = hub.last_pit_visit(r)
            if visit is not None and hub.pit_visit_open(visit):
                continue    # está en boxes: no es tráfico de pista
            ds = (s_own - s_at_now(rbuf)) % L
            x = ds / v          # s que tarda en llegar a mi posición
            delta = window_now - x   # + = queda delante tras mi parada
            if 0.0 <= delta <= TRAFFIC_CLOSE:
                ahead_close.append((r, delta))
                if rbuf.current_lap() < lap_own:
                    lapped.add(r)
            elif -TRAFFIC_SPAN <= delta < 0.0:
                zone.append((r, -delta))
                if rbuf.current_lap() < lap_own:
                    lapped.add(r)

    def _leader_lap_s(self, leader: str) -> float:
        buf = self.hub.buffers.get(leader)
        if buf is None or not buf.n:
            return 90.0
        cur = buf.current_lap()
        vals = []
        for lap in range(max(1, cur - 3), cur):
            lt = self.analyzer.lap_time(leader, lap)
            if lt == lt:
                vals.append(float(lt))
        return float(np.median(vals)) if vals else 90.0

    def _lap_trends(self, gaps: dict, lap_s: float) -> dict[str, float]:
        """s/vuelta que cada auto pierde (+) o gana (−) contra el líder,
        medidos de los snapshots (~2 vueltas hacia atrás). Dos venenos a
        evitar: una PARADA en el medio hace saltar el gap ~20 s (el
        escáner proyectaría a ese auto 'perdiendo 10 s/vuelta'), y una
        NEUTRALIZACIÓN comprime los gaps (la tendencia sería la
        compactación, no el ritmo) — en ambos casos, plano."""
        if len(self._gap_snaps) < 2:
            return {}
        t_now = self._gap_snaps[-1][0]
        past = None
        for t, snap in self._gap_snaps:
            if t_now - t <= 2.5 * lap_s:
                past = (t, snap)
                break
        if past is None or t_now - past[0] < 0.4 * lap_s:
            return {}
        if neutral_between(self.hub, past[0], t_now) is not None:
            return {}
        dt_laps = (t_now - past[0]) / lap_s
        out: dict[str, float] = {}
        for d, g in gaps.items():
            g0 = past[1].get(d)
            if g is None or g0 is None:
                continue
            if self._pitted_between(d, past[0], t_now):
                continue    # su gap saltó por la parada, no por ritmo
            out[d] = (g - g0) / dt_laps
        return out

    def _pitted_between(self, drv: str, t0: float, t1: float) -> bool:
        for visit in self.hub.pit_lane.get(drv, []):
            t_in = float(visit[1])
            t_out = visit[2]
            if t_in <= t1 and (t_out is None or float(t_out) >= t0):
                return True
        return False

    def _recent_lap_time(self, drv: str) -> float:
        buf = self.hub.buffers.get(drv)
        if buf is None or not buf.n:
            return float("nan")
        return float(self.analyzer.lap_time(drv, buf.current_lap() - 1))

    def _drift(self, rival: str, own_lt: float) -> float:
        """s/vuelta que un rival más lento recula en pista respecto a
        nosotros (piso 0.3 para no congelar la proyección)."""
        r_lt = self._recent_lap_time(rival)
        if r_lt == r_lt and own_lt == own_lt:
            return max(0.3, r_lt - own_lt)
        return 1.5

    def _scan_pit_laps(self, gaps: dict, trends: dict, drv: str,
                       window_now: float, traffic_now: dict | None,
                       window_green: float | None = None,
                       horizon: int = 5) -> dict | None:
        """Escáner de vuelta de parada: proyecta los gaps k vueltas con
        la tendencia medida y califica el rejoin de cada candidata —
        green (aire), yellow (zona poblada atrás), red (atrapado). k=0
        usa el tráfico ya calculado (incluye la pasada espacial); los
        atrapados espaciales sin gap (doblados) se ARRASTRAN a futuro
        con su deriva de ritmo — más lentos, reculan k×Δ por vuelta.
        k≥1 proyecta con la ventana VERDE (window_green): si hoy hay
        SC/VSC, la ventana barata no va a seguir ahí en 2-3 vueltas.
        best es None si ninguna candidata está limpia (no se miente
        'now' cuando now es rojo)."""
        own = gaps.get(drv)
        if own is None:
            return None
        w_future = window_green if window_green is not None else window_now
        carry = []
        if traffic_now is not None:
            own_lt = self._recent_lap_time(drv)
            for d, delta in traffic_now["ahead_close"]:
                if gaps.get(d) is None:
                    carry.append((d, delta, self._drift(d, own_lt)))
            for d, mdelta in traffic_now["zone"]:
                if gaps.get(d) is None:
                    carry.append((d, -mdelta, self._drift(d, own_lt)))
        ratings = []
        for k in range(horizon + 1):
            if k == 0 and traffic_now is not None:
                traffic = traffic_now
            else:
                pg = {d: g + trends.get(d, 0.0) * k
                      for d, g in gaps.items() if g is not None}
                traffic = self._traffic_at(pg, drv, pg[drv] + w_future)
                for d, delta0, drift in carry:
                    dk = delta0 - k * drift
                    if 0.0 <= dk <= TRAFFIC_CLOSE:
                        traffic["ahead_close"].append((d, dk))
                    elif -TRAFFIC_SPAN <= dk < 0.0:
                        traffic["zone"].append((d, -dk))
                traffic["ahead_close"].sort(key=lambda e: e[1])
                traffic["zone"].sort(key=lambda e: e[1])
                traffic["clear"] = not traffic["ahead_close"]
            rating = ("red" if not traffic["clear"] else
                      "yellow" if traffic["zone"] else "green")
            who = (traffic["ahead_close"][0][0] if traffic["ahead_close"]
                   else traffic["zone"][0][0] if traffic["zone"] else None)
            ratings.append({"k": k, "rating": rating, "who": who})
        best = next((e["k"] for e in ratings if e["rating"] == "green"),
                    next((e["k"] for e in ratings
                          if e["rating"] == "yellow"), None))
        return {"ratings": ratings, "best": best,
                "trend_based": bool(trends)}

    def _update_stuck(self, ordered: list, gaps: dict) -> None:
        """Cuenta vueltas seguidas 'pegado' (< STUCK_GAP del de adelante):
        pasar es difícil — estar atrapado pide buscar aire por estrategia."""
        for i, drv in enumerate(ordered[1:], start=1):
            g = gaps.get(drv)
            g_prev = gaps.get(ordered[i - 1])
            buf = self.hub.buffers.get(drv)
            lap = buf.current_lap() if buf is not None and buf.n else 0
            seen = self._stuck_lap_seen.get(drv)
            if seen == lap:
                continue  # una cuenta por vuelta
            if seen is not None and lap < seen:
                self._stuck_count[drv] = 0  # seek hacia atrás: de cero
            self._stuck_lap_seen[drv] = lap
            if g is not None and g_prev is not None \
                    and (g - g_prev) < STUCK_GAP:
                self._stuck_count[drv] = self._stuck_count.get(drv, 0) + 1
            else:
                self._stuck_count[drv] = 0

    def _recent_pits(self) -> list:
        """(rival, t_in, gap_pre) de cada entrada a boxes de los últimos
        COVER_WINDOW_S — precalculado una vez por evaluación para no
        rebarrer visitas y snapshots por cada auto."""
        now = self.hub.latest_t
        out = []
        for rival, visits in self.hub.pit_lane.items():
            for visit in visits:
                t_in = float(visit[1])
                if 0.0 <= now - t_in <= COVER_WINDOW_S:
                    g_pre = self._gap_before(t_in, rival)
                    if g_pre is not None:
                        out.append((rival, t_in, g_pre))
        return out

    def _recent_rival_pit(self, drv: str, gaps: dict, u_range: float,
                          recent_pits: list) -> tuple | None:
        """Rival que ENTRÓ a boxes hace poco estando en rango de undercut
        (u_range: la ganancia de goma fresca VIGENTE, medida si la hay).
        Usa el gap PRE-parada (snapshot anterior a su entrada: el gap
        actual de un auto en boxes ya está inflado por la parada en
        curso). Devuelve (rival, hace_s, gap_pre, was_behind, t_in).
        Con varias paradas simultáneas (doble stack) manda el rival más
        CERCANO pre-parada, priorizando al que venía detrás — solo ése
        te undercutea; el de adelante abre overcut, no cover."""
        if gaps.get(drv) is None:
            return None
        now = self.hub.latest_t
        best = None
        for rival, t_in, g_pre in recent_pits:
            if rival == drv:
                continue
            g_own_pre = self._gap_before(t_in, drv)
            if g_own_pre is None:
                continue
            delta_pre = g_pre - g_own_pre  # + = el rival venía detrás
            if abs(delta_pre) <= u_range + 2.0:
                key = (0 if delta_pre > 0 else 1, abs(delta_pre))
                if best is None or key < best[0]:
                    best = (key, (rival, now - t_in, abs(delta_pre),
                                  delta_pre > 0, t_in))
        return best[1] if best is not None else None

    # ------------------------------------------------------------ veredicto

    def evaluate(self) -> dict[str, Advice]:
        from .ui.pit_strategy import current_gaps, project_rejoin

        hub = self.hub
        # solo carreras/sprints: en quali o práctica nada de esto aplica
        name = str(hub.session_meta.get("name", "")).strip().lower()
        typ = str(hub.session_meta.get("type", "")).strip().lower()
        if not (typ == "race" or name in ("race", "sprint")):
            self.advices = {}
            return {}
        ordered, gaps = current_gaps(hub, self.analyzer)
        if not ordered:
            return {}
        # snapshot para leer gaps pre-parada en el futuro
        if not self._gap_snaps or hub.latest_t > self._gap_snaps[-1][0]:
            self._gap_snaps.append((hub.latest_t, dict(gaps)))
        neutral = neutralization(hub)
        self.measures.update()
        meas = self.measures
        if meas.window is not None and meas.window[1] >= 2:
            window = float(meas.window[0])
            w_src = f"measured from {meas.window[1]} stops"
        else:
            window = float(self.pit_window)
            w_src = "configured"
        if neutral == "SC":
            cheap, f_src = ((float(meas.sc[0]), f"measured ({meas.sc[1]})")
                            if meas.sc else (SC_FACTOR, "phase-1 estimate"))
        elif neutral == "VSC":
            cheap, f_src = ((float(meas.vsc[0]),
                             f"measured ({meas.vsc[1]})")
                            if meas.vsc else (VSC_FACTOR,
                                              "phase-1 estimate"))
        else:
            cheap, f_src = 1.0, ""
        window_now = window * cheap
        if meas.gain is not None and meas.gain[1] >= 2:
            u_range = float(meas.gain[0])
            u_src = f"measured from {meas.gain[1]} stops"
        else:
            u_range, u_src = UNDERCUT_RANGE, "phase-1 estimate"
        self._lap_s = self._leader_lap_s(ordered[0])
        trends = self._lap_trends(gaps, self._lap_s)
        self._update_stuck(ordered, gaps)
        stints = {d: self._stint(d) for d in ordered}
        recent_pits = self._recent_pits()
        leader_buf = hub.buffers.get(ordered[0])
        lap_now = (leader_buf.current_lap()
                   if leader_buf is not None and leader_buf.n else 0)
        total_laps = int(hub.lap_count[1]) if hub.lap_count[1] else 0
        laps_left = (total_laps - lap_now) if total_laps else None

        advices: dict[str, Advice] = {}
        for i, drv in enumerate(ordered):
            trace: list[str] = []
            factors: dict = {"pos": i + 1, "gap": gaps.get(drv),
                             "window": window, "window_src": w_src,
                             "neutral": neutral, "cheap_factor": cheap,
                             "measures": {"window": meas.window,
                                          "sc": meas.sc, "vsc": meas.vsc,
                                          "gain": meas.gain}}
            stint = stints[drv]
            factors["stint"] = dict(stint)
            trace.append(
                f"tyre {stint['compound'] or '?'} age {stint['age']} · "
                f"deg {stint['slope']:+.3f} s/lap"
                if stint["slope"] == stint["slope"] else
                f"tyre {stint['compound'] or '?'} age {stint['age']} · "
                "deg: not enough clean laps yet")
            if stint["cliff"]:
                trace.append(
                    f"tyre CLIFF onset: +{stint['marginal']:.2f}s/lap "
                    f"now and accelerating ({stint['curv']:+.3f}s/lap²)")
            trace.append(f"session measures: {meas.summary()} · "
                         f"window in use {window:.1f}s ({w_src})")

            # proyección de rejoin con la ventana VIGENTE (barata si SC/VSC)
            proj = project_rejoin(gaps, drv, window_now)
            proj_norm = project_rejoin(gaps, drv, window)
            rejoin_txt = "—"
            traffic = None
            scan = None
            scan_txt = ""
            if proj is not None and gaps.get(drv) is not None:
                new_pos = proj[0]
                traffic = self._traffic_at(gaps, drv,
                                           gaps[drv] + window_now,
                                           window_now=window_now,
                                           spatial=True)
                factors["rejoin"] = {
                    "pos_now": i + 1, "pos_cheap": new_pos,
                    "pos_normal": proj_norm[0] if proj_norm else None,
                    "traffic_ahead": traffic["ahead_close"],
                    "traffic_zone": [d for d, _g in traffic["zone"]],
                    "traffic_lapped": sorted(traffic["lapped"]),
                }
                if traffic["clear"]:
                    air = "clear air"
                else:
                    t_drv, t_gap = traffic["ahead_close"][0]
                    t_tag = (" (lapped)" if t_drv in traffic["lapped"]
                             else "")
                    air = f"stuck behind {t_drv}{t_tag} (+{t_gap:.1f}s)"
                rejoin_txt = f"→ P{new_pos} · {air}"
                trace.append(
                    f"box now → P{new_pos} (normal window → "
                    f"P{proj_norm[0] if proj_norm else '?'}); {air}")
                scan = self._scan_pit_laps(gaps, trends, drv, window_now,
                                           traffic, window_green=window)
                if scan is not None:
                    factors["pit_lap_scan"] = scan
                    dots = " ".join(e["rating"][0].upper()
                                    for e in scan["ratings"])
                    best_txt = ("none in +5" if scan["best"] is None
                                else "now" if scan["best"] == 0
                                else f"+{scan['best']} laps")
                    scan_txt = (" (scan: no clean pit lap in the next 5)"
                                if scan["best"] is None else
                                f" (scan: cleanest pit lap {best_txt})")
                    trace.append(
                        "pit-lap scan now..+5 (gap trends "
                        + ("measured" if scan["trend_based"] else "flat")
                        + f"): [{dots}] → cleanest {best_txt}")
            # tag corto para la celda Why de los veredictos de espera
            hold_tag = (f" · aim +{scan['best']}"
                        if scan is not None and scan["best"] else "")

            # proyección a bandera: quedarse corre cada vuelta restante
            # con una goma `age` vueltas más vieja que la del plan de
            # parar — la diferencia por vuelta es deg × edad actual
            flag_delta = None
            if laps_left is not None and laps_left > 0 \
                    and stint["slope"] == stint["slope"] \
                    and stint["slope"] > 0.02 and stint["age"] > 0:
                lost = stint["slope"] * laps_left * stint["age"]
                flag_delta = lost - window_now
                factors["flag_proj"] = {
                    "laps_left": laps_left,
                    "slope": round(stint["slope"], 3),
                    "age": stint["age"],
                    "stay_loss": round(lost, 1),
                    "stop_cost": round(window_now, 1),
                    "net": round(flag_delta, 1)}
                trace.append(
                    f"flag projection: staying {laps_left} laps on "
                    f"tyres {stint['age']} laps old at "
                    f"{stint['slope']:+.3f}s/lap deg ≈ {lost:.1f}s lost "
                    f"vs stop cost {window_now:.1f}s → net "
                    f"{flag_delta:+.1f}s for pitting now")

            threats: list[str] = []
            # el rival posicional es el próximo EN VUELTA: un doblado en
            # el medio no amenaza la posición (lo cubre la pasada espacial)
            nxt = next((d for d in ordered[i + 1:]
                        if gaps.get(d) is not None), None)
            gap_behind = None
            if nxt is not None and gaps.get(nxt) is not None \
                    and gaps.get(drv) is not None:
                gap_behind = gaps[nxt] - gaps[drv]
                factors["gap_behind"] = gap_behind
                # la ventana NO entra en el rango de amenaza: ambos autos
                # la pagan; el undercut salta solo si el gap es menor a la
                # ganancia acumulada de goma fresca
                if gap_behind < u_range:
                    threats.append(
                        f"{nxt} undercut range ({gap_behind:.1f}s < "
                        f"{u_range:.1f}s)")
                    trace.append(
                        f"threat: {nxt} at {gap_behind:.1f}s can undercut "
                        f"(fresh-tyre gain ~{u_range:.1f}s over 2-3 laps "
                        f"[{u_src}]; the window cancels out — both cars "
                        "pay it)")
            if i > 0 and gaps.get(drv) is not None:
                prev = next((d for d in reversed(ordered[:i])
                             if gaps.get(d) is not None), None)
                p_st = stints.get(prev, {}) if prev is not None else {}
                if p_st.get("cliff") \
                        and (gaps[drv] - gaps[prev]) <= 5.0:
                    threats.append(
                        f"{prev} ahead on the tyre cliff "
                        f"(+{p_st['marginal']:.1f}s/lap) — attack window")
                    trace.append(
                        f"opportunity: {prev} directly ahead is on the "
                        f"cliff (+{p_st['marginal']:.2f}s/lap, "
                        "worsening): stay close — the position may come "
                        "free without pitting")

            # ---- decisión por prioridad (cada rama descarta las demás) ----
            action, reason, urgency = "STAY", "no pressure", 0
            visit = hub.last_pit_visit(drv)
            in_pit = visit is not None and hub.pit_visit_open(visit)
            cover = self._recent_rival_pit(drv, gaps, u_range,
                                           recent_pits)
            stuck = self._stuck_count.get(drv, 0)
            endgame = laps_left is not None and laps_left <= ENDGAME_LAPS

            if in_pit:
                action, reason = "IN PIT", "stop in progress"
                trace.append("decision: IN PIT (visit open)")
            elif neutral is not None and proj is not None:
                eff_pos = proj[0]
                if neutral == "SC":
                    # la fila se compacta: los gaps muestreados son
                    # optimistas — lo que importa es quién NO va a parar.
                    # Solo autos EN VUELTA: un doblado no te quita posición
                    stay_out = [r for r in ordered[i + 1:]
                                if gaps.get(r) is not None
                                and self._likely_stays_out(r, stints)]
                    pack_lost = min(len(stay_out),
                                    int(window_now / PACK_SPACING))
                    eff_pos = i + 1 + pack_lost
                    factors["sc_pack"] = {"stay_out_behind": stay_out,
                                          "pack_pos": eff_pos,
                                          "spacing_s": PACK_SPACING}
                    if stay_out:
                        trace.append(
                            "SC pack projection: the queue bunches to "
                            f"~{PACK_SPACING}s/car; rivals behind likely "
                            "to STAY OUT (" + ", ".join(stay_out)
                            + ") close up and jump you while you pit → "
                            f"rejoin ~P{eff_pos} (sampled gaps said "
                            f"P{proj[0]} — optimistic while the field "
                            "packs)")
                    else:
                        trace.append(
                            "SC pack projection: every rival behind is "
                            "likely to pit too (worn tyres, no recent "
                            "stop) — packing costs nothing, relative "
                            f"order holds (~P{eff_pos})")
                saved = (proj_norm[0] - eff_pos) if proj_norm else 0
                factors["positions_saved_by_neutral"] = saved
                if stint["age"] < FRESH_AGE_MIN:
                    action, reason = "STAY", (
                        f"{neutral}: tyres only {stint['age']} laps old")
                    trace.append(
                        f"decision: STAY under {neutral} — tyres are "
                        f"fresh ({stint['age']} < {FRESH_AGE_MIN} laps): "
                        "a stop gains nothing, cheap or not")
                elif laps_left is not None and laps_left <= ENDGAME_LAPS:
                    action, reason = "STAY", (
                        f"{neutral}: only {laps_left} laps left")
                    trace.append(
                        f"decision: STAY under {neutral} — {laps_left} "
                        f"laps to the flag (≤{ENDGAME_LAPS}): track "
                        "position is worth more than fresh tyres now")
                elif traffic is not None and not traffic["clear"] \
                        and saved <= 0:
                    action, reason, urgency = (
                        "STAY", f"{neutral}: rejoin into traffic", 1)
                    trace.append(
                        f"decision: STAY under {neutral} — cheap stop "
                        "saves no position AND rejoins stuck (passing is "
                        "hard: clear air outweighs the cheap window)")
                else:
                    action = "BOX NOW"
                    reason = (f"{neutral}: cheap stop "
                              f"(window ×{cheap:.2f})")
                    urgency = 2
                    trace.append(
                        f"decision: BOX NOW — {neutral} pays the window "
                        f"at ×{cheap:.2f} [{f_src} factor]: "
                        f"P{i + 1} → P{eff_pos} vs P"
                        f"{proj_norm[0] if proj_norm else '?'} at green; "
                        f"saves {saved} position(s)"
                        + (" (pack-projected)" if neutral == "SC"
                           else "") + scan_txt)
            elif cover is not None:
                rival, ago, pre, was_behind, r_t_in = cover
                r_info = self.hub.drivers.get(rival)
                r_code = r_info.code if r_info else rival
                factors["cover"] = {"rival": rival, "ago_s": ago,
                                    "gap_pre": pre,
                                    "was_behind": was_behind}
                r_neutral = neutral_between(hub, r_t_in, r_t_in + 30.0)
                if r_neutral is not None and neutral is None:
                    trace.append(
                        f"asymmetry: {r_code}'s stop was cheap (under "
                        f"{r_neutral}) but responding now pays the FULL "
                        "window — covering only stops further bleeding")
                if endgame:
                    action, reason = "STAY", (
                        f"{r_code}'s stop can't pay back — "
                        f"{laps_left} laps left")
                    trace.append(
                        f"decision: STAY — {r_code} pitted but only "
                        f"{laps_left} laps remain: their window "
                        f"(~{window:.0f}s) cannot be repaid at "
                        f"~{u_range / 2.5:.1f}s/lap — hold track "
                        "position, the undercut dies at the flag")
                elif not was_behind:
                    # el de ADELANTE paró: eso no se cubre — abre overcut
                    threats.append(f"{r_code} (ahead) pitted — overcut "
                                   "window open")
                    action, reason = "WATCH", (
                        f"{r_code} ahead pitted — extend or pit to cover")
                    urgency = 1
                    trace.append(
                        f"decision: WATCH — {r_code} (was {pre:.1f}s "
                        f"AHEAD) pitted {ago:.0f}s ago: no cover needed; "
                        "staying out builds an overcut, pitting soon "
                        "covers the position" + scan_txt)
                elif stint["age"] <= FRESH_ABSORB:
                    action, reason = "STAY", (
                        f"absorbing {r_code}'s undercut (fresh tyres)")
                    trace.append(
                        f"decision: STAY — {r_code} pitted from "
                        f"{pre:.1f}s behind, but our tyres are "
                        f"{stint['age']} laps old (≤{FRESH_ABSORB}): "
                        "their fresh-tyre gain can't overcome ours — "
                        "the undercut is absorbed, no response needed")
                elif traffic is not None and not traffic["clear"] \
                        and traffic["ahead_close"][0][0] != rival:
                    trap, tgap = traffic["ahead_close"][0]
                    t_tag = (" (lapped)" if trap in traffic["lapped"]
                             else "")
                    action, reason = "WATCH", (
                        f"cover would trap you behind {trap}{hold_tag}")
                    urgency = 1
                    trace.append(
                        f"decision: WATCH — covering {r_code} rejoins "
                        f"+{tgap:.1f}s behind {trap}{t_tag}: the response "
                        "would trade the undercut loss for a trap "
                        "(passing is hard); pit when the rejoin clears"
                        + scan_txt)
                else:
                    action = f"COVER {r_code}"
                    reason = (f"rival pitted {ago:.0f}s ago — respond "
                              "this lap")
                    urgency = 2
                    trace.append(
                        f"decision: COVER — {r_code} entered pit "
                        f"{ago:.0f}s ago from {pre:.1f}s behind "
                        f"(pre-stop gap via snapshot): fresh tyres gain "
                        f"~{u_range:.1f}s over 2-3 laps [{u_src}]; "
                        "respond before their out-lap completes or "
                        "concede the position")
                    if traffic is not None and not traffic["clear"]:
                        trace.append(
                            f"note: rejoin lands right behind {r_code} — "
                            "their undercut may already be done; "
                            "covering limits the damage")
            elif gap_behind is not None \
                    and gap_behind > window_now + 1.0 \
                    and stint["age"] >= 8:
                if endgame:
                    action, reason = "STAY", (
                        f"free window, but only {laps_left} laps left")
                    trace.append(
                        f"decision: STAY — the {gap_behind:.1f}s gap "
                        "makes a stop free on paper, but with "
                        f"{laps_left} laps to the flag fresh tyres buy "
                        "nothing — hold to the end")
                elif traffic is not None and not traffic["clear"]:
                    who, wgap = traffic["ahead_close"][0]
                    w_tag = (" (lapped)" if who in traffic["lapped"]
                             else "")
                    action, reason = "WATCH", (
                        f"free gap behind, but rejoin stuck behind "
                        f"{who}{hold_tag}")
                    urgency = 1
                    trace.append(
                        f"decision: WATCH — {gap_behind:.1f}s behind "
                        "exceeds the window (stop would be free on "
                        "paper) BUT the rejoin lands "
                        f"+{wgap:.1f}s behind {who}{w_tag}: passing is "
                        "hard — wait for a cleaner window instead of a "
                        "free stop into traffic" + scan_txt)
                else:
                    action, reason = "FREE STOP", (
                        f"gap behind {gap_behind:.1f}s > window — no loss")
                    urgency = 1
                    trace.append(
                        f"decision: FREE STOP — {gap_behind:.1f}s to the "
                        f"next car exceeds window {window_now:.1f}+1.0s "
                        f"margin, tyres have {stint['age']} laps and the "
                        "rejoin is CLEAR (track-position check incl. "
                        "lapped cars): pitting is free")
            elif stuck >= STUCK_LAPS and traffic is not None \
                    and traffic["clear"] and proj is not None \
                    and (proj[0] - (i + 1)) <= AIR_MAX_LOSS \
                    and stint["age"] >= 6 and not endgame:
                action = "BOX FOR AIR"
                reason = (f"stuck {stuck} laps · rejoin in clear air")
                urgency = 1
                factors["stuck_laps"] = stuck
                trace.append(
                    f"decision: BOX FOR AIR — {stuck} laps trapped under "
                    f"{STUCK_GAP}s (passing is hard); boxing rejoins "
                    f"P{proj[0]} (max loss {AIR_MAX_LOSS}) in CLEAR AIR: "
                    "free pace beats the nominal position")
            elif not endgame and (
                    stint["cliff"]
                    or (stint["life"] is not None and stint["life"] <= 2
                        and (laps_left is None
                             or laps_left > stint["life"]))):
                if stuck >= 2:
                    # vueltas lentas por ir en un tren ≠ goma muerta:
                    # verificar en aire antes de quemar la parada
                    action, reason = "WATCH", (
                        f"deg signal masked by traffic — verify "
                        f"pace{hold_tag}")
                    urgency = 1
                    trace.append(
                        "decision: WATCH — lap times are collapsing BUT "
                        f"the car has been stuck {stuck} laps in "
                        "traffic: the rise may be the train, not the "
                        "tyre; verify in clear air before burning the "
                        "stop" + scan_txt)
                elif stint["cliff"]:
                    action, reason = "BOX SOON", (
                        f"tyre cliff: +{stint['marginal']:.1f}s/lap and "
                        "worsening")
                    urgency = 2
                    trace.append(
                        "decision: BOX SOON — cliff detected: lap times "
                        f"accelerating ({stint['curv']:+.3f}s/lap², now "
                        f"+{stint['marginal']:.2f}s/lap): the stint is "
                        "over whatever the plan was" + scan_txt)
                else:
                    action, reason = "BOX SOON", (
                        f"stint life ~{stint['life']} laps")
                    urgency = 1
                    trace.append(
                        f"decision: BOX SOON — measured deg "
                        f"{stint['slope']:+.3f} s/lap projects "
                        f"{stint['life']} laps before losing "
                        f"{STINT_LIFE_DROP}s/lap")
            elif flag_delta is not None and flag_delta > FLAG_NET_MIN \
                    and laps_left is not None \
                    and ENDGAME_LAPS < laps_left <= LAST_CALL_LAPS \
                    and traffic is not None and traffic["clear"]:
                action, reason = "BOX SOON", (
                    f"last call — a stop still nets ~{flag_delta:.0f}s "
                    "by the flag")
                urgency = 1
                trace.append(
                    "decision: BOX SOON — flag projection favours "
                    f"pitting (+{flag_delta:.1f}s net) and only "
                    f"{laps_left} laps remain: every lap waited erodes "
                    "the net; rejoin is clear" + scan_txt)
            elif threats:
                action, reason = "WATCH", threats[0]
                urgency = 1
                trace.append(
                    "decision: WATCH — undercut threat active; no better "
                    "move yet" + scan_txt)
            else:
                trace.append(
                    "decision: STAY — no neutralization, no rival stop to "
                    "cover, gap behind "
                    + (f"{gap_behind:.1f}s" if gap_behind is not None
                       else "n/a")
                    + " inside window, tyres alive, not trapped")

            advices[drv] = Advice(
                drv=drv, action=action, reason=reason, urgency=urgency,
                rejoin_txt=rejoin_txt, threats=threats, trace=trace,
                factors=factors)
            self._log_change(lap_now, advices[drv])
        self.advices = advices
        return advices

    # ------------------------------------------------------------- registro

    def _log_change(self, lap: int, adv: Advice) -> None:
        if self._last_action.get(adv.drv) == adv.action:
            return
        self._last_action[adv.drv] = adv.action
        info = self.hub.drivers.get(adv.drv)
        code = info.code if info else adv.drv
        self.log.appendleft((self.hub.latest_t, lap, code, adv.action,
                             adv.reason))
        try:
            if self._log_path is None:
                self._log_path = config.data_dir() / "strategy-log.jsonl"
                self._log_path.parent.mkdir(parents=True, exist_ok=True)
            if self._log_path.exists() \
                    and self._log_path.stat().st_size > LOG_MAX_BYTES:
                self._log_path.write_text("", "utf-8")
            with self._log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({
                    "wall": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "t": round(self.hub.latest_t, 1), "lap": lap,
                    "car": code, "action": adv.action,
                    "reason": adv.reason, "trace": adv.trace,
                    "factors": adv.factors,
                }, default=str) + "\n")
        except OSError:
            pass
