"""Integración con FastF1 real: carga una sesión y valida el stream de replay.

Descarga datos la primera vez (requiere red). Uso:
    python tests/replay_check.py [año] [gp] [sesión]
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import numpy as np
from PySide6.QtCore import QCoreApplication, Qt

from f1telem.sources.replay import ReplaySource

app = QCoreApplication(sys.argv)

year = int(sys.argv[1]) if len(sys.argv) > 1 else 2025
gp = sys.argv[2] if len(sys.argv) > 2 else "Bahrain"
ses = sys.argv[3] if len(sys.argv) > 3 else "FP1"

src = ReplaySource(year, gp, ses, speed=2000.0)
src._running = True

drivers: list[dict] = []
lengths: list[float] = []
statuses: list[str] = []
outlines: list = []
pos_count = [0]
lap_marks: list = []
src.lapMarks.connect(lap_marks.extend, Qt.DirectConnection)
corners_data: list = []
tyres_data: list = []
pits_data: list = []
status_data: list = []
src.corners.connect(corners_data.append, Qt.DirectConnection)
src.tyres.connect(tyres_data.append, Qt.DirectConnection)
weather_data: list = []
yellow_data: list = []
src.pits.connect(pits_data.append, Qt.DirectConnection)
src.trackStatus.connect(status_data.append, Qt.DirectConnection)
src.weather.connect(weather_data.append, Qt.DirectConnection)
src.sectorYellows.connect(yellow_data.append, Qt.DirectConnection)
src.driversDiscovered.connect(drivers.append, Qt.DirectConnection)
src.trackLength.connect(lengths.append, Qt.DirectConnection)
src.trackOutline.connect(outlines.append, Qt.DirectConnection)
src.positions.connect(lambda b: pos_count.__setitem__(0, pos_count[0] + len(b)), Qt.DirectConnection)
src.statusChanged.connect(lambda s: (statuses.append(s), print(f"  [estado] {s}")), Qt.DirectConnection)

stream = src._load()
assert stream is not None and len(stream["t"]) > 10000, "stream vacío"
n = len(stream["t"])
print(f"OK stream: {n:,} muestras de {len(set(stream['driver']))} autos")
assert drivers and len(drivers[0]) >= 15, f"pilotos: {len(drivers[0]) if drivers else 0}"
some = list(drivers[0].values())[:3]
print(f"OK pilotos: {[(d.code, d.team, d.color) for d in some]}")
assert lengths and 3000 < lengths[0] < 8000, f"largo de vuelta raro: {lengths}"
print(f"OK largo de vuelta: {lengths[0]:.0f} m")
assert int(stream["lap"].max()) >= 5, f"pocas vueltas: {stream['lap'].max()}"
in_lap = stream["dist_lap"][stream["lap"] > 0]
assert np.nanmax(in_lap) < lengths[0] * 2.5, "dist_lap fuera de rango"
print(f"OK vueltas hasta {int(stream['lap'].max())}, dist_lap max {np.nanmax(in_lap):.0f} m")
assert outlines and len(outlines[0][0]) > 100, f"trazado del circuito: {len(outlines[0][0]) if outlines else 0} pts"
print(f"OK trazado del circuito: {len(outlines[0][0])} puntos")
assert len(lap_marks) >= 10, f"pocas marcas de vuelta: {len(lap_marks)}"
times = [t for _lap, t in lap_marks]
assert times == sorted(times), "marcas de vuelta desordenadas"
print(f"OK marcas de vuelta para la línea de tiempo: {len(lap_marks)} (V1..V{lap_marks[-1][0]})")
assert corners_data and len(corners_data[0]) >= 8, f"pocas curvas: {len(corners_data[0]) if corners_data else 0}"
print(f"OK curvas del circuito: {len(corners_data[0])} ({corners_data[0][0][0]}..{corners_data[0][-1][0]})")
assert tyres_data and len(tyres_data[0]) >= 15, f"neumáticos de pocos pilotos: {len(tyres_data[0]) if tyres_data else 0}"
compounds = {c for laps_map in tyres_data[0].values() for c, _l in laps_map.values() if c}
assert compounds, "sin compuestos de neumáticos"
print(f"OK neumáticos: {len(tyres_data[0])} pilotos, compuestos {sorted(compounds)}")
n_stops = sum(len(v) for v in pits_data[0].values()) if pits_data else 0
print(f"OK pits: {len(pits_data[0]) if pits_data else 0} pilotos, {n_stops} paradas")
assert pits_data and n_stops >= 10, f"pocas paradas: {n_stops}"
print(f"OK estado de pista: {len(status_data[0]) if status_data else 0} períodos")
assert status_data and len(status_data[0]) >= 1, "sin períodos de bandera"
assert weather_data and len(weather_data[0]) >= 10, "sin datos de clima"
temps = [row[2] for row in weather_data[0]]
assert 5 < min(temps) and max(temps) < 70, f"temperaturas de pista raras: {min(temps)}-{max(temps)}"
print(f"OK clima: {len(weather_data[0])} lecturas, pista {min(temps):.0f}-{max(temps):.0f}°")
n_yellow = len(yellow_data[0]) if yellow_data else 0
print(f"OK amarillas por sector: {n_yellow}")

import fastf1
schedule = fastf1.get_event_schedule(2025, include_testing=False)
assert len(schedule) >= 20, f"calendario corto: {len(schedule)}"
print(f"OK calendario 2025: {len(schedule)} eventos")
assert stream.get("pos") is not None and len(stream["pos"]["t"]) > 10000, "sin stream de posiciones"
print(f"OK posiciones: {len(stream['pos']['t']):,} puntos de {len(set(stream['pos']['driver']))} autos")

# reproducción acelerada de una porción
count = [0]
src.batch.connect(lambda b: count.__setitem__(0, count[0] + len(b)), Qt.DirectConnection)
import threading, time
th = threading.Thread(target=src._play, args=(stream,), daemon=True)
th.start()
time.sleep(5)
src._running = False
th.join(timeout=5)
assert count[0] > 5000, f"la reproducción emitió pocas muestras: {count[0]}"
print(f"OK reproducción: {count[0]:,} muestras emitidas en 5 s a x2000")
assert pos_count[0] > 5000, f"la reproducción emitió pocas posiciones: {pos_count[0]}"
print(f"OK reproducción: {pos_count[0]:,} posiciones emitidas")

# integridad de tiempos de vuelta/sectores con datos reales: reproducir la
# sesión completa dentro de un hub y verificar que casi todas las vueltas
# cerradas tienen tiempo (las marcas robustas anclan los cruces de meta)
from f1telem.hub import DataHub
from f1telem.timing import TimingAnalyzer

hub = DataHub()
hub.on_track_length(lengths[0])
src.batch.connect(hub.on_batch, Qt.DirectConnection)
prog = [0.0, 0.0]
src.progress.connect(lambda a, b, c: (prog.__setitem__(0, b), prog.__setitem__(1, c)),
                     Qt.DirectConnection)
src.speed = 5000.0
src._running = True
th = threading.Thread(target=src._play, args=(stream,), daemon=True)
th.start()
deadline = time.monotonic() + 240
while time.monotonic() < deadline:  # el loop ya no termina solo: esperar el fin
    time.sleep(0.2)
    if prog[1] and prog[0] >= prog[1] - 1.0:
        break
src._running = False
th.join(timeout=30)
an = TimingAnalyzer(hub)
total = finite = 0
for drv, buf in hub.buffers.items():
    for lap in buf.completed_laps():
        total += 1
        if np.isfinite(an.lap_time(drv, lap)):
            finite += 1
ratio = finite / max(total, 1)
print(f"OK tiempos de vuelta completos: {finite}/{total} ({ratio:.1%})")
assert ratio > 0.9, f"tiempos de vuelta incompletos: {finite}/{total}"

# línea de tiempo: salto hacia atrás re-emite la historia hasta el punto
hub2 = DataHub()
hub2.on_track_length(lengths[0])
resets = [0]
src.seekReset.connect(lambda: (resets.__setitem__(0, resets[0] + 1), hub2.clear_samples()),
                      Qt.DirectConnection)
src.batch.connect(hub2.on_batch, Qt.DirectConnection)
src.speed = 500.0
src._running = True
th = threading.Thread(target=src._play, args=(stream,), daemon=True)
th.start()
time.sleep(1.5)
t_target = float(stream["t_start"]) + 600.0
src.request_seek(t_target)
time.sleep(3.0)
src._running = False
th.join(timeout=60)
assert resets[0] == 1, f"seekReset emitido {resets[0]} veces"
t_max = max(float(b.col("t")[-1]) for b in hub2.buffers.values())
assert t_target - 10 < t_max < t_target + 500 * 5, f"tras el salto t_max={t_max:.0f}"
print(f"OK línea de tiempo: salto a t+600 s, reconstruido hasta t_max={t_max - float(stream['t_start']):.0f} s")

print("REPLAY CHECK: PASS")
sys.stdout.flush()
os._exit(0)
