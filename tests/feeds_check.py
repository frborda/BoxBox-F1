"""Chequeos de los decodificadores de topics nuevos del live timing:
RaceControlMessages, TimingAppData (stints), ExtrapolatedClock, LapCount,
SessionInfo (meta) y paradas por NumberOfPitStops. Al final, si hay una
captura real grande en recordings, se decodifica entera y se validan los
totales contra lo esperado.

Uso:  python tests/feeds_check.py
"""
from __future__ import annotations

import glob
import json
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication([])

from f1telem.hub import DataHub  # noqa: E402
from f1telem.sources.live import LiveSource, _parse_clock, _parse_utc  # noqa: E402
from f1telem.ui.strategy import collect_stints  # noqa: E402

FAILS = []


def check(cond: bool, label: str) -> None:
    print(("[ OK ] " if cond else "[FAIL] ") + label)
    if not cond:
        FAILS.append(label)


def make_source() -> tuple[LiveSource, dict]:
    src = LiveSource()
    got: dict = {}
    src.raceControl.connect(lambda rows: got.__setitem__("rcm", rows))
    src.tyres.connect(lambda data: got.__setitem__("tyres", data))
    src.sessionClock.connect(lambda v: got.__setitem__("clock", v))
    src.lapCount.connect(lambda v: got.__setitem__("laps", v))
    src.sessionMeta.connect(lambda m: got.__setitem__("meta", m))
    src.pits.connect(lambda p: got.__setitem__("pits", p))
    src.pitLane.connect(lambda d: got.__setitem__("lane", d))
    src.weather.connect(lambda w: got.__setitem__("weather", w))
    src.trackStatus.connect(lambda s: got.__setitem__("status", s))
    src.batch.connect(lambda b: got.setdefault("samples", []).extend(b))
    return src, got


# ---------------------------------------------------------------- parsers

check(abs(_parse_utc("2026-07-19T13:00:00Z") - _parse_utc("2026-07-19T13:00:00")) < 1e-6,
      "parse_utc: sin zona horaria se asume UTC")
check(_parse_clock("1:23:45") == 5025.0, "parse_clock: h:mm:ss")
check(_parse_clock("23:45") == 1425.0, "parse_clock: mm:ss")
check(_parse_clock(None) is None, "parse_clock: None no parsea")

# ------------------------------------------------- decodificador sintético

src, got = make_source()
src._t0 = _parse_utc("2026-07-19T13:00:00Z")
src._last_rel_t = 100.0

src._feed("SessionInfo", {
    "Meeting": {"Name": "Belgian Grand Prix", "Circuit": {"Key": 0}},
    "Type": "Race", "Name": "Race", "StartDate": "2026-07-19T15:00:00",
})
check(got.get("meta", {}).get("type") == "Race", "SessionInfo: tipo de sesión")

src._feed("RaceControlMessages", {"Messages": [
    {"Utc": "2026-07-19T13:01:40", "Lap": 3, "Category": "Flag",
     "Flag": "YELLOW", "Scope": "Sector", "Sector": 7,
     "Message": "YELLOW IN TRACK SECTOR 7"},
]})
rcm = got.get("rcm", [])
check(len(rcm) == 1 and rcm[0]["flag"] == "YELLOW", "RCM: snapshot en lista")
check(abs(rcm[0]["t"] - 100.0) < 1e-6, "RCM: t relativo desde el stamp UTC")

src._feed("RaceControlMessages", {"Messages": {"1": {
    "Utc": "2026-07-19T13:05:00Z", "Lap": 5, "Category": "SafetyCar",
    "Mode": "SAFETY CAR", "Message": "SAFETY CAR DEPLOYED"}}})
rcm = got.get("rcm", [])
check(len(rcm) == 2 and rcm[1]["mode"] == "SAFETY CAR", "RCM: diff por índice")
src._feed("RaceControlMessages", {"Messages": {"1": {
    "Utc": "2026-07-19T13:05:00Z", "Lap": 5, "Category": "SafetyCar",
    "Mode": "SAFETY CAR", "Message": "SAFETY CAR DEPLOYED"}}})
check(len(got.get("rcm", [])) == 2, "RCM: reenvío del mismo índice no duplica")

src._feed("TimingAppData", {"Lines": {"16": {"Stints": [
    {"Compound": "SOFT", "New": "true", "TotalLaps": 3, "StartLaps": 0},
]}}})
tyres = got.get("tyres", {}).get("16", {})
check(tyres.get(1) == ("SOFT", 1) and tyres.get(3) == ("SOFT", 3),
      "TimingAppData: stint inicial -> mapa por vuelta")
src._feed("TimingAppData", {"Lines": {"16": {"Stints": {
    "0": {"TotalLaps": 4},
    "1": {"Compound": "MEDIUM", "New": "true", "TotalLaps": 0, "StartLaps": 0},
}}}})
tyres = got.get("tyres", {}).get("16", {})
check(tyres.get(4) == ("SOFT", 4), "TimingAppData: diff extiende el stint")
check(tyres.get(5) == ("MEDIUM", 1), "TimingAppData: stint nuevo tras la parada")
stints = collect_stints(tyres)
check(stints == [("SOFT", 1, 4), ("MEDIUM", 5, 5)],
      f"collect_stints agrupa por compuesto ({stints})")

src._feed("ExtrapolatedClock", {"Remaining": "1:23:45", "Extrapolating": True})
check(got.get("clock") == (100.0, 5025.0, True), "ExtrapolatedClock decodificado")

src._feed("LapCount", {"CurrentLap": 14, "TotalLaps": 44})
src._feed("LapCount", {"CurrentLap": 15})
check(got.get("laps") == (15, 44), "LapCount: diff conserva el total")

src._in_snapshot = True
src._feed("TimingData", {"Lines": {"16": {"NumberOfPitStops": 1,
                                          "NumberOfLaps": 11}}})
src._in_snapshot = False
check("pits" not in got, "Pits: el snapshot solo fija la línea base")
src._feed("TimingData", {"Lines": {"16": {"NumberOfPitStops": 2}}})
pits = got.get("pits", {}).get("16", [])
check(pits == [(12, 100.0)], f"Pits: incremento del contador -> parada ({pits})")

# visitas a la calle de boxes (InPit)
src._in_snapshot = True
src._feed("TimingData", {"Lines": {"44": {"NumberOfLaps": 20, "InPit": True}}})
src._in_snapshot = False
lane = got.get("lane", {}).get("44", [])
check(len(lane) == 1 and lane[0][0] == 21 and lane[0][2] is None,
      f"InPit en snapshot abre la visita ({lane})")
src._last_rel_t = 130.0
src._feed("TimingData", {"Lines": {"44": {"InPit": False}}})
lane = got["lane"]["44"]
check(lane[0][2] == 130.0, "salida de boxes cierra la visita")
src._feed("TimingData", {"Lines": {"44": {"InPit": True}}})
check(len(got["lane"]["44"]) == 2 and got["lane"]["44"][1][2] is None,
      "nueva entrada abre otra visita")

# ------------------------------------------------------------- hub

hub = DataHub()
hub.on_session_clock((100.0, 5025.0, True))
hub.latest_t = 160.0
check(hub.clock_remaining() == 5025.0 - 60.0, "hub: reloj extrapolado")
hub.on_session_clock((100.0, 600.0, False))
check(hub.clock_remaining() == 600.0, "hub: reloj congelado (banderas rojas)")
hub.on_lap_count((15, 44))
hub.on_session_meta({"type": "Race", "meeting": "X", "name": "Race"})
check(hub.lap_count == (15, 44) and hub.session_meta["type"] == "Race",
      "hub: lap_count y session_meta")
from f1telem.models import DriverInfo, Sample  # noqa: E402

hub2 = DataHub()
batch = []
for i in range(20):
    t = i * 0.5
    speed = 0.0 if 3.0 <= t <= 6.0 else 80.0
    batch.append(Sample(driver="1", t=t, lap=10, dist_lap=100.0 + i,
                        dist_total=100.0 + i, speed=speed, throttle=0.0,
                        brake=0.0, rpm=0.0, gear=1, drs=0))
hub2.on_batch(batch)
stopped = hub2.pit_stationary_time("1", 0.0, 9.5)
check(3.0 <= stopped <= 4.0,
      f"hub: tiempo detenido por velocidad 0 ({stopped:.1f} s)")
hub2.on_pit_lane({"1": [[4, 10.0, 32.0]]})
check(hub2.last_pit_visit("1") == [4, 10.0, 32.0], "hub: última visita a boxes")

hub.reset()
check(hub.lap_count == (0, 0) and not hub.session_meta
      and not hub.race_control and not hub.pit_lane,
      "hub: reset limpia el estado nuevo")

# --------------------------------------------- gestor de notificaciones

from f1telem.ui.notifications import NotificationCenter  # noqa: E402

ncfg = {"notifications": {"popups": False}}
hub3 = DataHub()
hub3.on_track_length(5000.0)
hub3.on_drivers({"1": DriverInfo("1", "VER")})


def lap_samples(lap: int, t0: float, dur: float) -> list:
    rows = []
    for i in range(50):
        f = i / 50.0
        rows.append(Sample(driver="1", t=t0 + f * dur, lap=lap,
                           dist_lap=5000.0 * f, dist_total=5000.0 * (lap - 1 + f),
                           speed=200.0, throttle=50.0, brake=0.0, rpm=9000.0,
                           gear=5, drs=0))
    return rows


hub3.on_batch(lap_samples(1, 0.0, 100.0) + lap_samples(2, 100.0, 80.0)
              + lap_samples(3, 180.0, 75.0)[:1])
center = NotificationCenter(hub3, ncfg)
center.check()  # línea base: la historia previa no notifica
check(center.log == [], "notif: la línea base no dispara nada")

hub3.on_batch(lap_samples(3, 180.0, 75.0)[1:] + lap_samples(4, 255.0, 90.0)[:2])
center.check()
check(any(k == "fast_lap" and "VER" in txt for _s, k, _c, txt in center.log),
      f"notif: vuelta rápida detectada ({center.log})")

hub3.on_pit_lane({"1": [[4, 260.0, None]]})
center.check()
check(any(k == "pit_in" for _s, k, _c, _t in center.log), "notif: entrada a boxes")
hub3.on_pit_lane({"1": [[4, 260.0, 282.5]]})
center.check()
out_msgs = [txt for _s, k, _c, txt in center.log if k == "pit_out"]
check(len(out_msgs) == 1 and "22.5s" in out_msgs[0],
      f"notif: salida de boxes con tiempo en calle ({out_msgs})")
center.check()
check(len([1 for _s, k, _c, _t in center.log if k == "pit_in"]) == 1,
      "notif: sin duplicados en pasadas ya anunciadas")

# auto detenido en pista (fuera de boxes, sostenido >= 3 s)
hub3.on_batch([Sample(driver="1", t=300.0, lap=4, dist_lap=2000.0,
                      dist_total=17000.0, speed=0.0, throttle=0.0, brake=0.0,
                      rpm=0.0, gear=0, drs=0)])
center.check()
hub3.on_batch([Sample(driver="1", t=304.0, lap=4, dist_lap=2000.0,
                      dist_total=17000.0, speed=0.0, throttle=0.0, brake=0.0,
                      rpm=0.0, gear=0, drs=0)])
center.check()
check(any(k == "stopped" for _s, k, _c, _t in center.log),
      "notif: auto detenido en pista")

hub3.on_track_status([(310.0, float("inf"), "4")])
hub3.on_batch([Sample(driver="1", t=311.0, lap=4, dist_lap=2100.0,
                      dist_total=17100.0, speed=180.0, throttle=80.0,
                      brake=0.0, rpm=9000.0, gear=5, drs=0)])
center.check()
check(any(k == "sc" for _s, k, _c, _t in center.log), "notif: safety car")

hub3.on_race_control([{"t": 312.0, "lap": 4, "category": "Other", "flag": "",
                       "scope": "Driver", "sector": None, "mode": "",
                       "driver": "1",
                       "message": "5 SECOND TIME PENALTY FOR CAR 1"}])
center.check()
check(any(k == "penalty" for _s, k, _c, _t in center.log), "notif: sanción")

ncfg["notifications"]["kinds"] = {"pit_in": False}
hub3.on_pit_lane({"1": [[4, 260.0, 282.5], [6, 400.0, None]]})
center.check()
check(len([1 for _s, k, _c, _t in center.log if k == "pit_in"]) == 1,
      "notif: categoría deshabilitada no notifica")

center2 = NotificationCenter(hub3, ncfg)
center2.check()
check(center2.log == [], "notif: centro nuevo sobre estado viejo no notifica")

# ------------------------------------- Ventana de Box (pit window) y rejoin

from f1telem.timing import TimingAnalyzer  # noqa: E402
from f1telem.ui.pit_strategy import (  # noqa: E402
    PitWindowEstimator, clean_at, project_rejoin,
)

hub4 = DataHub()
hub4.on_track_length(5000.0)
DT, V = 0.5, 50.0  # paso 0.5 s · 50 m/s -> vuelta de 100 s


def synth_car(num: str, lead: float, pit: bool):
    """4+ vueltas a ritmo constante; el que para se detiene 5 s en el metro
    4990 de la vuelta 3. Devuelve (samples, t_in, t_out) con la visita
    medida al pasar por 4950 (entrada) y 50 de la vuelta 4 (salida)."""
    rows, t, pos, stopped = [], 0.0, float(lead), 0.0
    t_in = t_out = None
    while t < 420.0:
        lap = int(pos // 5000) + 1
        dist = pos - (lap - 1) * 5000.0
        in_stop = pit and lap == 3 and dist >= 4990.0 and stopped < 5.0
        speed = 0.0 if in_stop else 180.0
        rows.append(Sample(driver=num, t=t, lap=lap, dist_lap=dist,
                           dist_total=pos, speed=speed, throttle=80.0,
                           brake=0.0, rpm=9000.0, gear=6, drs=0))
        if pit and t_in is None and lap == 3 and dist >= 4950.0:
            t_in = t
        if pit and t_out is None and lap == 4 and dist >= 50.0:
            t_out = t
        t += DT
        if in_stop:
            stopped += DT
        else:
            pos += V * DT
    return rows, t_in, t_out

batch4 = []
for i, num in enumerate(("1", "2", "3", "4", "5")):
    rows, _ti, _to = synth_car(num, 100.0 - 20.0 * i, pit=False)
    batch4.extend(rows)
rows9, t_in9, t_out9 = synth_car("9", -20.0, pit=True)
batch4.extend(rows9)
batch4.sort(key=lambda s: s.t)
hub4.on_batch(batch4)
hub4.on_pit_lane({"9": [[3, t_in9, t_out9]]})

est4 = PitWindowEstimator(hub4, TimingAnalyzer(hub4))
bounds = est4.window_bounds()
check(bounds is not None and abs(bounds[0] - 4375.0) < 1.0
      and abs(bounds[1] - 1250.0) < 1.0,
      f"ventana de box: 2 µsectores antes/después del pit ({bounds})")
ref4 = est4.reference(*bounds)
check(ref4 is not None and abs(ref4[0] - 25.0) < 0.3 and ref4[1] == 15,
      f"ventana de box: referencia de 3 vueltas limpias del top 5 ({ref4})")
est_val = est4.estimate()
check(est_val is not None and 2.4 <= est_val[0] <= 3.6 and est_val[1] == 1,
      f"ventana de box: pérdida con detención normalizada a 3 s ({est_val})")

hub4.on_track_status([(100.0, 150.0, "4")])
check(clean_at(hub4, 60.0, 99.0) and not clean_at(hub4, 140.0, 160.0),
      "ventana de box: detección de tramo limpio")

proj = project_rejoin({"1": 0.0, "2": 2.0, "3": 10.0}, "1", 5.0)
check(proj is not None and proj[0] == 2 and proj[1] == ("2", 3.0)
      and proj[2] == ("3", 5.0),
      f"rejoin: posición y márgenes proyectados ({proj})")
check(project_rejoin({"1": None, "2": 0.0}, "1", 5.0) is None,
      "rejoin: sin gap (doblado) no proyecta")

# --------------------------------------------- captura real (si existe)

rec_dir = os.path.join(os.environ.get("LOCALAPPDATA", ""), "f1telem", "recordings")
captures = [p for p in glob.glob(os.path.join(rec_dir, "capture_*.jsonl"))
            if os.path.getsize(p) > 1_000_000]
if captures:
    path = max(captures, key=os.path.getsize)
    src2, got2 = make_source()
    n_lines = 0
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            try:
                src2._handle(json.loads(line))
            except Exception:
                pass
            n_lines += 1
    print(f"-- captura real: {os.path.basename(path)} ({n_lines} líneas)")
    check(len(got2.get("samples", [])) > 10000, "captura: telemetría decodificada")
    check(got2.get("meta", {}).get("type") == "Race", "captura: SessionInfo Type")
    laps = got2.get("laps", (0, 0))
    check(laps[1] > 0 and laps[0] >= 14, f"captura: LapCount {laps}")
    check(len(got2.get("weather", [])) >= 10, "captura: clima acumulado")
    check(got2.get("clock") is None or got2["clock"][1] > 0,
          "captura: reloj (si el topic estaba suscripto)")
else:
    print("-- sin captura real >1MB: chequeos de captura omitidos")

print()
if FAILS:
    print(f"{len(FAILS)} chequeos fallaron")
    sys.exit(1)
print("Todo OK")
