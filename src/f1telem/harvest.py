"""Cosechador headless de estrategia: recorre carreras históricas de
Fast-F1 a máxima velocidad (sin UI, sin timers) alimentando el MISMO
DataHub + StrategyEngine del vivo, y deja por carrera el insumo de
calibración de fases futuras:

- <clave>.jsonl         un registro por CAMBIO de veredicto, con los
                        factores y la traza completos del motor MÁS su
                        DESENLACE medido: posición y gap 3 vueltas
                        después, qué veredicto regía entonces, si paró
                        en ese lapso y la posición final de carrera.
                        Sin desenlace no hay backtesting posible.
- <clave>.summary.json  lo que la sesión midió (pérdida real de box,
                        factores SC/VSC, ganancia de goma fresca), las
                        neutralizaciones y metadatos — los priors por
                        circuito de una futura auto-calibración.

Uso (con el venv del repo o el de WSL):

    python -m f1telem.harvest 2024:Bahrain:R "2024:Monza:R"
    python -m f1telem.harvest --list carreras.txt --out D:/estrategia

`carreras.txt`: una sesión por línea (year:gp[:session], # comenta).
La sesión por defecto es R; el motor solo opina en carreras/sprints —
otras sesiones producen 0 decisiones (se avisa). La primera vez que se
pide una sesión, Fast-F1 la descarga (minutos); después usa su caché.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import time
from pathlib import Path

import numpy as np

from . import config
from .hub import DataHub
from .models import Sample
from .strategy_engine import StrategyEngine
from .timing import TimingAnalyzer

OUTCOME_LAPS = 3     # vueltas para medir el desenlace de un veredicto
EVAL_STEP_S = 5.0    # segundos de sesión entre evaluaciones del motor


class DecisionRecorder:
    """Registra cada cambio de veredicto del motor y le resuelve el
    desenlace cuando ese auto completa OUTCOME_LAPS vueltas más."""

    def __init__(self, hub: DataHub, engine: StrategyEngine):
        self.hub = hub
        self.engine = engine
        self.records: list[dict] = []
        self._last: dict[str, str] = {}
        self._pending: list[dict] = []

    def evaluate(self) -> dict:
        advices = self.engine.evaluate()
        for drv, adv in advices.items():
            if adv.action == self._last.get(drv):
                continue
            self._last[drv] = adv.action
            if adv.action == "IN PIT":
                continue    # la visita ya está registrada en pit_lane
            buf = self.hub.buffers.get(drv)
            lap = buf.current_lap() if buf is not None and buf.n else 0
            info = self.hub.drivers.get(drv)
            rec = {
                "t": round(self.hub.latest_t, 1), "lap": lap,
                "drv": drv, "car": info.code if info else drv,
                "action": adv.action, "reason": adv.reason,
                "urgency": adv.urgency, "threats": list(adv.threats),
                "factors": adv.factors, "trace": list(adv.trace),
                "outcome": {"resolved_lap": None, "pos_then": None,
                            "gap_behind_then": None, "action_then": None,
                            "pitted_within": None, "pos_final": None},
            }
            self.records.append(rec)
            self._pending.append(rec)
        self._resolve(advices)
        return advices

    def _resolve(self, advices: dict) -> None:
        still = []
        for rec in self._pending:
            drv = rec["drv"]
            buf = self.hub.buffers.get(drv)
            lap = buf.current_lap() if buf is not None and buf.n else 0
            adv = advices.get(drv)
            if adv is None or lap < rec["lap"] + OUTCOME_LAPS:
                still.append(rec)
                continue
            out = rec["outcome"]
            out["resolved_lap"] = lap
            out["pos_then"] = adv.factors.get("pos")
            out["gap_behind_then"] = adv.factors.get("gap_behind")
            out["action_then"] = adv.action
            out["pitted_within"] = self._pitted(drv, rec["t"],
                                                self.hub.latest_t)
        self._pending = still

    def _pitted(self, drv: str, t0: float, t1: float) -> bool:
        return any(t0 < float(v[1]) <= t1
                   for v in self.hub.pit_lane.get(drv, []))

    def finalize(self) -> None:
        """Cierra lo pendiente al terminar la carrera: posición final
        para todos; los no resueltos (la bandera llegó antes de las
        3 vueltas) quedan con resolved_lap None pero con su parada."""
        advices = self.engine.advices
        for rec in self.records:
            adv = advices.get(rec["drv"])
            if adv is not None:
                rec["outcome"]["pos_final"] = adv.factors.get("pos")
            if rec["outcome"]["pitted_within"] is None:
                rec["outcome"]["pitted_within"] = self._pitted(
                    rec["drv"], rec["t"], self.hub.latest_t)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")


def harvest_session(year: int, gp: str, ses: str, out_dir: Path,
                    eval_step: float = EVAL_STEP_S) -> dict:
    """Corre una sesión completa por el motor y escribe sus archivos.
    Devuelve el resumen (también persistido como .summary.json)."""
    from .sources.replay import ReplaySource

    wall0 = time.monotonic()
    hub = DataHub()
    src = ReplaySource(year, gp, ses)
    src._running = True     # _load() aborta sin esta bandera de QThread
    for sig, slot in ((src.driversDiscovered, hub.on_drivers),
                      (src.trackLength, hub.on_track_length),
                      (src.trackOutline, hub.on_outline),
                      (src.corners, hub.on_corners),
                      (src.tyres, hub.on_tyres),
                      (src.pits, hub.on_pits),
                      (src.pitLane, hub.on_pit_lane),
                      (src.trackStatus, hub.on_track_status),
                      (src.weather, hub.on_weather),
                      (src.sectorYellows, hub.on_sector_yellows),
                      (src.sectorTimes, hub.on_sector_times),
                      (src.raceControl, hub.on_race_control),
                      (src.lapCount, hub.on_lap_count),
                      (src.sessionMeta, hub.on_session_meta),
                      (src.qualiParts, hub.on_quali_parts)):
        sig.connect(slot)
    src.statusChanged.connect(lambda m: print(f"  {m}", flush=True))
    stream = src._load()
    if stream is None or not len(stream["t"]):
        raise RuntimeError("la sesión no tiene telemetría disponible")

    analyzer = TimingAnalyzer(hub)
    engine = StrategyEngine(hub, analyzer)
    engine._log_path = Path(os.devnull)   # el recorder es el dueño del log
    recorder = DecisionRecorder(hub, engine)

    t_arr = stream["t"]
    lap_arr = stream["lap"]
    n = len(t_arr)
    cursor = int(np.searchsorted(t_arr, float(stream["t_start"])))
    t_next = float(stream["t_start"])
    total = int(hub.lap_count[1]) if hub.lap_count[1] else 0
    evals = 0
    while cursor < n:
        t_next += eval_step
        j = int(np.searchsorted(t_arr, t_next))
        if j > cursor:
            batch = [Sample(str(stream["driver"][k]), float(t_arr[k]),
                            int(lap_arr[k]), float(stream["dist_lap"][k]),
                            float(stream["dist_total"][k]),
                            float(stream["speed"][k]),
                            float(stream["throttle"][k]),
                            float(stream["brake"][k]),
                            float(stream["rpm"][k]),
                            int(stream["gear"][k]), int(stream["drs"][k]))
                     for k in range(cursor, j) if lap_arr[k] > 0]
            cursor = j
            if batch:
                hub.on_batch(batch)
        recorder.evaluate()
        evals += 1
        if evals % 120 == 0:
            lead = max((b.current_lap() for b in hub.buffers.values()
                        if b.n), default=0)
            print(f"  lap {lead}/{total or '?'} · "
                  f"{len(recorder.records)} decisiones", flush=True)
    recorder.finalize()

    key = f"{year}-{_slug(gp)}-{_slug(ses)}"
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / f"{key}.jsonl").open("w", encoding="utf-8") as fh:
        for rec in recorder.records:
            fh.write(json.dumps(rec, default=str) + "\n")
    meas = engine.measures
    summary = {
        "year": year, "gp": gp, "session": ses,
        "meeting": hub.session_meta.get("meeting", ""),
        "session_name": hub.session_meta.get("name", ""),
        "total_laps": total,
        "track_length": hub.track_length,
        "drivers": len(hub.buffers),
        "decisions": len(recorder.records),
        "eval_step_s": eval_step,
        "measures": {"pit_loss": meas.window, "sc_factor": meas.sc,
                     "vsc_factor": meas.vsc, "fresh_gain": meas.gain},
        "neutralizations": [
            (round(a, 1), None if b == float("inf") else round(b, 1),
             str(c))
            for a, b, c in hub.track_status if str(c) in ("4", "6", "7")],
        "wall_seconds": round(time.monotonic() - wall0, 1),
    }
    (out_dir / f"{key}.summary.json").write_text(
        json.dumps(summary, indent=2, default=str), "utf-8")
    return summary


def _parse_spec(spec: str) -> tuple[int, str, str]:
    parts = [p.strip() for p in spec.split(":")]
    if len(parts) == 2:
        parts.append("R")
    if len(parts) != 3 or not parts[0].isdigit() or not parts[1]:
        raise ValueError("formato esperado year:gp[:session]")
    return int(parts[0]), parts[1], parts[2] or "R"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m f1telem.harvest",
        description="Cosecha logs de estrategia (con desenlaces) de "
                    "sesiones históricas Fast-F1, a máxima velocidad.")
    ap.add_argument("specs", nargs="*",
                    help='sesiones "year:gp[:session]" '
                         '(ej. 2024:Bahrain:R "2024:Monza")')
    ap.add_argument("--list", dest="list_file",
                    help="archivo con una sesión por línea (# comenta)")
    ap.add_argument("--out", default=None,
                    help="carpeta de salida (default: "
                         "<datos>/strategy-races)")
    ap.add_argument("--eval-step", type=float, default=EVAL_STEP_S,
                    help="segundos de sesión entre evaluaciones "
                         f"(default {EVAL_STEP_S:g})")
    args = ap.parse_args(argv)

    specs = list(args.specs)
    if args.list_file:
        for line in Path(args.list_file).read_text("utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                specs.append(line)
    if not specs:
        ap.error("indicá al menos una sesión (o --list archivo)")

    from PySide6.QtCore import QCoreApplication
    if QCoreApplication.instance() is None:
        QCoreApplication([])
    out_dir = (Path(args.out) if args.out
               else config.data_dir() / "strategy-races")
    ok = 0
    for spec in specs:
        try:
            year, gp, ses = _parse_spec(spec)
        except ValueError as exc:
            print(f"✗ {spec!r}: {exc}", flush=True)
            continue
        print(f"▶ {year} {gp} {ses}", flush=True)
        try:
            s = harvest_session(year, gp, ses, out_dir,
                                float(args.eval_step))
        except Exception as exc:
            print(f"✗ {year} {gp} {ses}: {exc}", flush=True)
            continue
        ok += 1
        note = ("" if s["decisions"] else
                " (0 decisiones: el motor solo opina en carreras)")
        print(f"✔ {year} {gp} {ses}: {s['decisions']} decisiones en "
              f"{s['wall_seconds']:.0f}s{note}", flush=True)
    print(f"{ok}/{len(specs)} sesiones cosechadas → {out_dir}",
          flush=True)
    return 0 if ok == len(specs) and ok > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
