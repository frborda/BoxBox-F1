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

Valores estimados de fase 1 (marcados en cada traza, a medir en fase 2):
factor de abaratamiento VSC/SC y ganancia de goma fresca del undercut.
"""
from __future__ import annotations

import json
import time
from collections import deque
from dataclasses import dataclass, field

import numpy as np

from . import config

# --- constantes de fase 1 (cada uso queda trazado) ---
VSC_FACTOR = 0.55        # la ventana de box se paga a este factor bajo VSC
SC_FACTOR = 0.45         # ídem bajo safety car
UNDERCUT_GAIN = 1.5      # s que gana el que boxea primero (1ª vuelta)
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
        self._log_path = None

    def reset(self) -> None:
        self.advices.clear()
        self.log.clear()
        self._last_action.clear()
        self._stuck_count.clear()
        self._stuck_lap_seen.clear()
        self._gap_snaps.clear()

    def _gap_before(self, t_ref: float, drv: str) -> float | None:
        """Gap al líder del auto en el último snapshot ANTERIOR a t_ref."""
        for t, snap in reversed(self._gap_snaps):
            if t < t_ref:
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
               "life": None, "laps_used": 0}
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
        return out

    def _traffic_at(self, gaps: dict, drv: str, gap_after: float) -> dict:
        """Densidad de tráfico alrededor del punto de reinserción: cuántos
        autos quedan justo delante (atrapan) y en la zona de atrás."""
        ahead_close = []
        zone = []
        for d, g in gaps.items():
            if d == drv or g is None:
                continue
            delta = gap_after - g   # + = ese auto queda delante
            if 0.0 <= delta <= TRAFFIC_CLOSE:
                ahead_close.append((d, delta))
            elif -TRAFFIC_SPAN <= delta < 0.0:
                zone.append((d, -delta))
        return {"ahead_close": ahead_close, "zone": zone,
                "clear": not ahead_close}

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

    def _recent_rival_pit(self, drv: str, gaps: dict) -> tuple | None:
        """Rival que ENTRÓ a boxes hace menos de COVER_WINDOW_S estando en
        rango de undercut. Usa el gap PRE-parada (snapshot anterior a su
        entrada: el gap actual de un auto en boxes ya está inflado por la
        parada en curso). Devuelve (rival, hace_s, gap_pre, was_behind):
        solo el rival que venía DETRÁS te undercutea; el de adelante que
        para te abre la ventana de overcut, no un cover."""
        g_own = gaps.get(drv)
        if g_own is None:
            return None
        now = self.hub.latest_t
        for rival, visits in self.hub.pit_lane.items():
            if rival == drv:
                continue
            for visit in visits:
                t_in = float(visit[1])
                if not (0.0 <= now - t_in <= COVER_WINDOW_S):
                    continue
                g_pre = self._gap_before(t_in, rival)
                g_own_pre = self._gap_before(t_in, drv)
                if g_pre is None or g_own_pre is None:
                    continue
                delta_pre = g_pre - g_own_pre  # + = el rival venía detrás
                if abs(delta_pre) <= UNDERCUT_RANGE + 2.0:
                    return rival, now - t_in, abs(delta_pre), delta_pre > 0
        return None

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
        window = float(self.pit_window)
        cheap = (SC_FACTOR if neutral == "SC"
                 else VSC_FACTOR if neutral == "VSC" else 1.0)
        window_now = window * cheap
        self._update_stuck(ordered, gaps)
        leader_buf = hub.buffers.get(ordered[0])
        lap_now = (leader_buf.current_lap()
                   if leader_buf is not None and leader_buf.n else 0)
        total_laps = int(hub.lap_count[1]) if hub.lap_count[1] else 0
        laps_left = (total_laps - lap_now) if total_laps else None

        advices: dict[str, Advice] = {}
        for i, drv in enumerate(ordered):
            trace: list[str] = []
            factors: dict = {"pos": i + 1, "gap": gaps.get(drv),
                             "window": window, "neutral": neutral,
                             "cheap_factor": cheap}
            stint = self._stint(drv)
            factors["stint"] = dict(stint)
            trace.append(
                f"tyre {stint['compound'] or '?'} age {stint['age']} · "
                f"deg {stint['slope']:+.3f} s/lap"
                if stint["slope"] == stint["slope"] else
                f"tyre {stint['compound'] or '?'} age {stint['age']} · "
                "deg: not enough clean laps yet")

            # proyección de rejoin con la ventana VIGENTE (barata si SC/VSC)
            proj = project_rejoin(gaps, drv, window_now)
            proj_norm = project_rejoin(gaps, drv, window)
            rejoin_txt = "—"
            traffic = None
            if proj is not None and gaps.get(drv) is not None:
                new_pos = proj[0]
                traffic = self._traffic_at(gaps, drv,
                                           gaps[drv] + window_now)
                factors["rejoin"] = {
                    "pos_now": i + 1, "pos_cheap": new_pos,
                    "pos_normal": proj_norm[0] if proj_norm else None,
                    "traffic_ahead": traffic["ahead_close"],
                    "traffic_zone": [d for d, _g in traffic["zone"]],
                }
                air = ("clear air" if traffic["clear"] else
                       f"stuck behind {traffic['ahead_close'][0][0]}"
                       f" (+{traffic['ahead_close'][0][1]:.1f}s)")
                rejoin_txt = f"→ P{new_pos} · {air}"
                trace.append(
                    f"box now → P{new_pos} (normal window → "
                    f"P{proj_norm[0] if proj_norm else '?'}); {air}")

            threats: list[str] = []
            nxt = ordered[i + 1] if i + 1 < len(ordered) else None
            gap_behind = None
            if nxt is not None and gaps.get(nxt) is not None \
                    and gaps.get(drv) is not None:
                gap_behind = gaps[nxt] - gaps[drv]
                factors["gap_behind"] = gap_behind
                # la ventana NO entra en el rango de amenaza: ambos autos
                # la pagan; el undercut salta solo si el gap es menor a la
                # ganancia acumulada de goma fresca
                if gap_behind < UNDERCUT_RANGE:
                    threats.append(
                        f"{nxt} undercut range ({gap_behind:.1f}s < "
                        f"{UNDERCUT_RANGE:.1f}s)")
                    trace.append(
                        f"threat: {nxt} at {gap_behind:.1f}s can undercut "
                        f"(fresh-tyre gain ~{UNDERCUT_RANGE}s over 2-3 "
                        "laps [phase-1 estimate]; the window cancels out "
                        "— both cars pay it)")

            # ---- decisión por prioridad (cada rama descarta las demás) ----
            action, reason, urgency = "STAY", "no pressure", 0
            visit = hub.last_pit_visit(drv)
            in_pit = visit is not None and hub.pit_visit_open(visit)
            cover = self._recent_rival_pit(drv, gaps)
            stuck = self._stuck_count.get(drv, 0)

            if in_pit:
                action, reason = "IN PIT", "stop in progress"
                trace.append("decision: IN PIT (visit open)")
            elif neutral is not None and proj is not None:
                saved = (proj_norm[0] - proj[0]) if proj_norm else 0
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
                        f"at ×{cheap:.2f} [phase-1 factor]: "
                        f"P{i + 1} → P{proj[0]} vs P"
                        f"{proj_norm[0] if proj_norm else '?'} at green; "
                        f"saves {saved} position(s). Gaps are the ones "
                        "sampled now — SC bunching keeps shrinking them "
                        "[phase-2: re-project as the field packs]")
            elif cover is not None:
                rival, ago, pre, was_behind = cover
                r_info = self.hub.drivers.get(rival)
                r_code = r_info.code if r_info else rival
                factors["cover"] = {"rival": rival, "ago_s": ago,
                                    "gap_pre": pre,
                                    "was_behind": was_behind}
                if not was_behind:
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
                        "covers the position [phase-2 picks the lap]")
                elif stint["age"] <= FRESH_ABSORB:
                    action, reason = "STAY", (
                        f"absorbing {r_code}'s undercut (fresh tyres)")
                    trace.append(
                        f"decision: STAY — {r_code} pitted from "
                        f"{pre:.1f}s behind, but our tyres are "
                        f"{stint['age']} laps old (≤{FRESH_ABSORB}): "
                        "their fresh-tyre gain can't overcome ours — "
                        "the undercut is absorbed, no response needed")
                else:
                    action = f"COVER {r_code}"
                    reason = (f"rival pitted {ago:.0f}s ago — respond "
                              "this lap")
                    urgency = 2
                    trace.append(
                        f"decision: COVER — {r_code} entered pit "
                        f"{ago:.0f}s ago from {pre:.1f}s behind "
                        f"(pre-stop gap via snapshot): fresh tyres gain "
                        f"~{UNDERCUT_RANGE}s over 2-3 laps [phase-1 "
                        "estimate]; respond before their out-lap "
                        "completes or concede the position")
                    if traffic is not None and not traffic["clear"]:
                        trace.append(
                            "note: own rejoin is NOT clear — covering "
                            "may trap you; phase 2 will weigh both")
            elif gap_behind is not None \
                    and gap_behind > window_now + 1.0 \
                    and stint["age"] >= 8:
                if traffic is not None and not traffic["clear"]:
                    who = traffic["ahead_close"][0][0]
                    action, reason = "WATCH", (
                        f"free gap behind, but rejoin stuck behind {who}")
                    urgency = 1
                    trace.append(
                        f"decision: WATCH — {gap_behind:.1f}s behind "
                        "exceeds the window (stop would be free on "
                        "paper) BUT the rejoin lands "
                        f"+{traffic['ahead_close'][0][1]:.1f}s behind "
                        f"{who}: passing is hard — wait for a cleaner "
                        "window instead of a free stop into traffic")
                else:
                    action, reason = "FREE STOP", (
                        f"gap behind {gap_behind:.1f}s > window — no loss")
                    urgency = 1
                    trace.append(
                        f"decision: FREE STOP — {gap_behind:.1f}s to the "
                        f"next car exceeds window {window_now:.1f}+1.0s "
                        f"margin, tyres have {stint['age']} laps and the "
                        "rejoin is CLEAR (lapped cars not assessed yet "
                        "[phase-2]): pitting is free")
            elif stuck >= STUCK_LAPS and traffic is not None \
                    and traffic["clear"] and proj is not None \
                    and (proj[0] - (i + 1)) <= AIR_MAX_LOSS \
                    and stint["age"] >= 6:
                action = "BOX FOR AIR"
                reason = (f"stuck {stuck} laps · rejoin in clear air")
                urgency = 1
                factors["stuck_laps"] = stuck
                trace.append(
                    f"decision: BOX FOR AIR — {stuck} laps trapped under "
                    f"{STUCK_GAP}s (passing is hard); boxing rejoins "
                    f"P{proj[0]} (max loss {AIR_MAX_LOSS}) in CLEAR AIR: "
                    "free pace beats the nominal position")
            elif stint["life"] is not None and stint["life"] <= 2:
                action, reason = "BOX SOON", (
                    f"stint life ~{stint['life']} laps")
                urgency = 1
                trace.append(
                    f"decision: BOX SOON — measured deg "
                    f"{stint['slope']:+.3f} s/lap projects "
                    f"{stint['life']} laps before losing "
                    f"{STINT_LIFE_DROP}s/lap")
            elif threats:
                action, reason = "WATCH", threats[0]
                urgency = 1
                trace.append(
                    "decision: WATCH — undercut threat active; no better "
                    "move yet (phase 2 adds the pre-emptive pit lap)")
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
