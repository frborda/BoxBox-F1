"""Pruebas de los sectores oficiales y microsectores del feed, sin red:
decodificación de TimingData (tiempos de sector y Segments), derivación de
los límites reales de S1/S2 cruzando tiempos con telemetría sintética, y el
analizador con las marcas ancladas a esos límites.

Uso:  python tests/sector_bounds_check.py
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.stdout.reconfigure(errors="replace")

import numpy as np

from f1telem.hub import DataHub
from f1telem.models import Sample
from f1telem.sources.capture import CaptureSource
from f1telem.timing import N_MICRO, SECTOR_STEP, TimingAnalyzer

FAILURES: list[str] = []


def check(cond: bool, msg: str) -> None:
    tag = "OK " if cond else "FAIL"
    print(f"[{tag}] {msg}", flush=True)
    if not cond:
        FAILURES.append(msg)


# ------------------------------------------------- decodificador TimingData

src = CaptureSource("nonexistent.jsonl")
sector_reports: list = []
seg_updates: list = []
src.sectorTimes.connect(lambda batch: sector_reports.extend(batch))
src.segmentStatus.connect(lambda batch: seg_updates.extend(batch))

src._on_timing({"Lines": {"44": {"NumberOfLaps": 3, "Sectors": {
    "0": {"Value": "26.123", "Segments": {"0": {"Status": 2049}, "1": {"Status": 0}}},
}}}})
check(sector_reports == [("44", 4, 0, 26.123)],
      f"decoder: tiempo de S1 atribuido a la vuelta en curso ({sector_reports})")
check(("44", 0, 0, 2049) in seg_updates and ("44", 0, 1, 0) in seg_updates,
      f"decoder: estados de segmentos emitidos ({seg_updates})")

n_before = len(sector_reports), len(seg_updates)
src._on_timing({"Lines": {"44": {"Sectors": {
    "0": {"Value": "26.123", "Segments": {"0": {"Status": 2049}}},
}}}})
check((len(sector_reports), len(seg_updates)) == n_before,
      "decoder: valores repetidos no se re-emiten")

src._on_timing({"Lines": {"44": {"Sectors": {
    "0": {"Segments": {"0": {"Status": 2051}}},
    "1": {"Value": "31.5"},
}}}})
check(seg_updates[-1] == ("44", 0, 0, 2051), "decoder: cambio de estado sí se re-emite")
check(sector_reports[-1] == ("44", 4, 1, 31.5), "decoder: tiempo de S2 emitido")

# S3 y tiempo de vuelta cierran la vuelta que se acaba de completar (la 4:
# NumberOfLaps pasa a 4 en el mismo mensaje)
src._on_timing({"Lines": {"44": {"NumberOfLaps": 4,
                                 "LastLapTime": {"Value": "1:44.361"},
                                 "Sectors": {"2": {"Value": "28.1"}}}}})
check(("44", 4, 2, 28.1) in sector_reports, "decoder: S3 atribuido a la vuelta cerrada")
check(any(r[:3] == ("44", 4, 3) and abs(r[3] - 104.361) < 1e-6
          for r in sector_reports),
      "decoder: tiempo de vuelta oficial parseado de m:ss.mmm")

# el snapshot inicial trae valores de la vuelta anterior: solo Segments
n_before = len(sector_reports)
src._handle({"R": {"TimingData": {"Lines": {"16": {"NumberOfLaps": 7,
    "LastLapTime": {"Value": "1:50.000"},
    "Sectors": [
        {"Value": "30.0", "Segments": [{"Status": 2048}, {"Status": 2064}]},
        {"Value": "bad"},
    ]}}}}})
check(("16", 0, 1, 2064) in seg_updates, "decoder: snapshot en formato lista")
check(len(sector_reports) == n_before,
      "decoder: el snapshot no emite tiempos (atribución dudosa)")

# Value vacío o no parseable nunca emite
src._on_timing({"Lines": {"16": {"Sectors": [{"Value": ""}, {"Value": "bad"}]}}})
check(not any(r[0] == "16" for r in sector_reports),
      "decoder: Value vacío o inválido no emite tiempo")

# retiro oficial: Retired del feed (Stopped es transitorio y no cuenta)
ret_events: list = []
src.retirements.connect(lambda r: ret_events.append(list(r)))
src._on_timing({"Lines": {"44": {"Retired": True, "Stopped": True}}})
check(ret_events and ret_events[-1] == ["44"], "decoder: Retired emitido")
src._on_timing({"Lines": {"44": {"Retired": True}}})
check(len(ret_events) == 1, "decoder: Retired repetido no re-emite")
src._on_timing({"Lines": {"16": {"Stopped": True}}})
check(len(ret_events) == 1, "decoder: Stopped solo no retira")

# ---------------------------------------- trazado del circuito (MultiViewer)

from f1telem.sources.live import _circuit_rows  # noqa: E402

outline, corners = _circuit_rows({
    "x": list(range(20)), "y": list(range(20)),
    "corners": [
        {"number": 1, "length": 3818.58, "trackPosition": {"x": -2268.2, "y": 4480.0}},
        {"number": 2, "trackPosition": {"x": 0, "y": 0}},  # sin length: fuera
    ],
})
check(outline is not None and len(outline[0]) == 20, "circuito: trazado parseado")
check(corners == [("T1", 381.858, -2268.2, 4480.0)],
      f"circuito: curva con distancia en metros ({corners})")
check(_circuit_rows({"x": [1], "y": [1]})[0] is None,
      "circuito: trazado corto descartado")

fetches: list = []
src._fetch_circuit = lambda key, year: fetches.append((key, year))
session_info = {"Meeting": {"Name": "Belgian GP",
                            "Circuit": {"Key": 7, "ShortName": "Spa"}},
                "Name": "Race", "StartDate": "2026-07-19T15:00:00"}
src._on_session_info(session_info)
src._on_session_info(session_info)  # segundo aviso no re-pide
import time as _time  # noqa: E402
_time.sleep(0.3)  # el fetch corre en un hilo propio
check(fetches == [(7, "2026")],
      f"circuito: SessionInfo dispara UN fetch con clave y año ({fetches})")

# ------------------------------------------------- derivación de límites

L = 6000.0
V = 50.0  # m/s constantes -> vuelta de 120 s
B1, B2 = 1800.0, 4200.0  # límites oficiales "reales" del circuito sintético
S1, S2 = B1 / V, (B2 - B1) / V  # 36 s y 48 s

hub = DataHub()
hub.on_track_length(L)

samples = []
for drv, phase in (("44", 0.0), ("16", 0.11)):
    t = phase
    while t < 8 * 120.0:
        total = t * V
        samples.append(Sample(
            driver=drv, t=t, lap=int(total // L) + 1, dist_lap=total % L,
            dist_total=total, speed=V * 3.6, throttle=80.0, brake=0.0,
            rpm=11000.0, gear=6, drs=0,
        ))
        t += 0.25  # 4 Hz
hub.on_batch(samples)

reports = []
for drv in ("44", "16"):
    for lap in range(2, 7):  # 5 vueltas x 2 pilotos = 10 obs por límite
        reports.append((drv, lap, 0, S1))
        reports.append((drv, lap, 1, S2))
hub.on_sector_times(reports)
hub.maybe_derive_sector_bounds()
check(hub.sector_bounds is not None, "bounds: derivados con suficientes observaciones")
if hub.sector_bounds:
    b1, b2 = hub.sector_bounds
    check(abs(b1 - B1) < 15.0, f"bounds: fin de S1 en {b1:.0f} m (real {B1:.0f})")
    check(abs(b2 - B2) < 15.0, f"bounds: fin de S2 en {b2:.0f} m (real {B2:.0f})")

# robustez: reportes basura (p. ej. atribución errónea del snapshot inicial)
hub.on_sector_times([("44", 7, 0, 50.0), ("16", 7, 0, 21.0)])  # 2500 m y 1050 m
hub._bounds_next_try = 0.0
before = hub.sector_bounds
hub.maybe_derive_sector_bounds()
check(hub.sector_bounds is not None and abs(hub.sector_bounds[0] - before[0]) < 5.0,
      f"bounds: la mediana absorbe reportes basura ({hub.sector_bounds[0]:.0f} m)")

# vuelta 1 nunca aporta observaciones (largada desde la grilla)
n_reports = len(hub.official_times)
hub.on_sector_times([("44", 1, 0, 10.0)])
check(len(hub.official_times) == n_reports + 1, "bounds: reporte de vuelta 1 se guarda")

# gana el primer valor: una atribución tardía no pisa el dato correcto
hub.on_sector_times([("16", 3, 3, 100.0), ("16", 3, 3, 999.0)])
check(hub.official_times[("16", 3)][3] == 100.0,
      "hub: first-write-wins en tiempos oficiales")

# ------------------------------------------------- analizador anclado

an = TimingAnalyzer(hub)
dists = an._mark_dists()
check(len(dists) == N_MICRO + 1, "marks: siguen siendo 25 marcas")
check(abs(dists[SECTOR_STEP] - hub.sector_bounds[0]) < 0.01
      and abs(dists[2 * SECTOR_STEP] - hub.sector_bounds[1]) < 0.01,
      "marks: las marcas 8 y 16 caen en los límites oficiales")

sectors = an.sector_times("44", 3)
check(all(math.isfinite(s) for s in sectors),
      f"analyzer: sectores finitos ({[f'{s:.2f}' for s in sectors]})")
check(abs(sectors[0] - S1) < 0.2 and abs(sectors[1] - S2) < 0.2,
      f"analyzer: S1/S2 coinciden con los oficiales ({sectors[0]:.2f}, {sectors[1]:.2f})")
lap_time = an.lap_time("44", 3)
check(abs(sum(sectors) - lap_time) < 0.05, "analyzer: S1+S2+S3 = vuelta")
micro = an.micro_times("44", 3)
check(micro is not None and abs(float(np.nansum(micro)) - lap_time) < 0.05,
      "analyzer: los 24 µsectores suman la vuelta")

# invariante: los 8 µ de cada sector suman EXACTO el sector mostrado (el
# oficial si ya llegó — re-escalado proporcional — o el interpolado si no)
hub.on_sector_times([("44", 5, 2, 36.4),   # S3 oficial distinto del interp
                     ("44", 7, 0, 36.9)])  # S1 oficial solo (caso mixto)
for lap in (3, 5, 7):
    micro_l = an.micro_times("44", lap)
    secs_l = an.sector_times("44", lap)
    for k in range(3):
        seg_sum = float(micro_l[k * SECTOR_STEP:(k + 1) * SECTOR_STEP].sum())
        check(abs(seg_sum - secs_l[k]) < 1e-6,
              f"invariante: µ de S{k + 1} suman el sector (vuelta {lap}: "
              f"{seg_sum:.3f} vs {secs_l[k]:.3f})")

# lo mismo para los valores rodantes (µ y sector de la vuelta en curso)
hub.on_sector_times([("44", 8, 0, 36.6)])
lm, _lm_laps = an.latest_micro_times("44")
ls, _ls_laps = an.latest_sector_times("44")
check(abs(float(lm[:SECTOR_STEP].sum()) - float(ls[0])) < 1e-6
      and abs(float(ls[0]) - 36.6) < 1e-9,
      f"invariante rodante: µ1-8 suman el S1 oficial "
      f"({float(lm[:SECTOR_STEP].sum()):.3f} vs {float(ls[0]):.3f})")

# al llegar los tiempos oficiales, las tablas los muestran tal cual
hub.on_sector_times([("44", 4, 3, 119.512), ("44", 4, 2, 35.5)])
check(an.lap_time("44", 4) == 119.512,
      "analyzer: tiempo de vuelta oficial reemplaza al interpolado")
check(an.sector_times("44", 4)[2] == 35.5,
      "analyzer: S3 oficial reemplaza al interpolado")
check(an.best_lap("44")[1] == 119.512,
      "analyzer: best usa el tiempo oficial")

# sin límites derivados se vuelve a tercios iguales
hub2 = DataHub()
hub2.on_track_length(L)
an2 = TimingAnalyzer(hub2)
d2 = an2._mark_dists()
check(abs(d2[SECTOR_STEP] - L / 3) < 0.01, "marks: sin bounds, tercios de vuelta")

# el cambio de geometría invalida los caches del analizador
an2.lap_marks("44", 2)  # puebla _geo_used
hub2.sector_bounds = (B1, B2)
an2._check_track_len()
check(abs(an2._mark_dists()[SECTOR_STEP] - B1) < 0.01,
      "analyzer: al aparecer bounds cambian las marcas")

# ------------------------------------------------- cortes fuera de curvas

hub5 = DataHub()
hub5.on_track_length(L)  # tercios: µ de 250 m, marcas cada 250 m
hub5.corners = [("T1", 950.0, 0.0, 0.0), ("T5", 2550.0, 0.0, 0.0)]
an5 = TimingAnalyzer(hub5)
d5 = an5._mark_dists()
check(len(d5) == N_MICRO + 1 and float(d5[0]) == 0.0
      and abs(float(d5[-1]) - L) < 1e-9,
      "snap: 25 marcas con extremos intactos")
check(bool((np.diff(d5) > 0.3 * L / N_MICRO).all()),
      "snap: orden y ancho mínimo garantizados")
zones = [(850.0, 1010.0), (2450.0, 2610.0)]  # vértice −100 m / +60 m
inner = [float(d5[i]) for i in range(1, N_MICRO) if i % SECTOR_STEP]
check(not any(z0 < m < z1 for m in inner for z0, z1 in zones),
      "snap: ningún corte interno dentro de una zona de curva")
check(abs(float(d5[4]) - 1010.0) < 1e-6 and abs(float(d5[10]) - 2450.0) < 1e-6,
      f"snap: cortes movidos al borde de la zona ({float(d5[4]):.0f}, "
      f"{float(d5[10]):.0f})")
check(abs(float(d5[SECTOR_STEP]) - L / 3) < 1e-9,
      "snap: el límite de sector nunca se mueve")
# al conocerse las curvas más tarde, el cache de marcas se invalida
hub5b = DataHub()
hub5b.on_track_length(L)
an5b = TimingAnalyzer(hub5b)
check(abs(float(an5b._mark_dists()[4]) - 1000.0) < 1e-9,
      "snap: sin curvas, marcas equiespaciadas")
hub5b.corners = [("T1", 950.0, 0.0, 0.0)]
check(abs(float(an5b._mark_dists()[4]) - 1010.0) < 1e-6,
      "snap: al llegar las curvas las marcas se recolocan")

# curva y contracurva: el hueco entre ambas zonas es demasiado chico para
# un corte (cambio de sentido) — las zonas se fusionan
hub5c = DataHub()
hub5c.on_track_length(L)
# zonas [850, 1010] y [1050, 1210]: hueco de 40 m; antes el corte de 1000 m
# era atraído a 1010 (plena transición de la chicana)
hub5c.corners = [("T1", 950.0, 0.0, 0.0), ("T2", 1150.0, 0.0, 0.0)]
an5c = TimingAnalyzer(hub5c)
d5c = an5c._mark_dists()
check(abs(float(d5c[4]) - 1000.0) < 1e-9,
      f"snap: chicana fusionada — el corte no se atrae a la transición "
      f"({float(d5c[4]):.0f} m)")
# corrida la chicana: los cortes vecinos salen ANTES del primer frenaje y
# DESPUÉS de la salida de la segunda curva, nunca al medio
hub5c.corners = [("T1", 1050.0, 0.0, 0.0), ("T2", 1250.0, 0.0, 0.0)]
d5c = an5c._mark_dists()
check(abs(float(d5c[4]) - 950.0) < 1e-6 and abs(float(d5c[5]) - 1310.0) < 1e-6,
      f"snap: cortes al frenaje de la 1ª y salida de la 2ª "
      f"({float(d5c[4]):.0f}, {float(d5c[5]):.0f})")
inner_c = [float(d5c[i]) for i in range(1, N_MICRO) if i % SECTOR_STEP]
check(not any(1110.0 <= m <= 1150.0 for m in inner_c),
      "snap: ningún corte en el cambio de sentido")

# ---------------------------------------- frenaje real medido por curva

# la zona de frenaje no es fija: depende de qué tan rápido se llega y
# cuánto hay que frenar — se mide del canal de freno (mediana)
hub7 = DataHub()
hub7.on_track_length(L)
hub7.corners = [("T1", 1500.0, 0.0, 0.0),   # frenada larga: 200 m
                ("T8", 3500.0, 0.0, 0.0)]   # viraje a fondo: sin freno
brk_samples = []
for drv in ("44", "16"):
    t = 0.0
    while t < 4 * 120.0:
        total = t * V
        d_lap = total % L
        braking = 100.0 if 1300.0 <= d_lap < 1500.0 else 0.0
        brk_samples.append(Sample(
            driver=drv, t=t, lap=int(total // L) + 1, dist_lap=d_lap,
            dist_total=total, speed=V * 3.6, throttle=80.0, brake=braking,
            rpm=11000.0, gear=6, drs=0,
        ))
        t += 0.25
hub7.on_batch(brk_samples)
hub7.maybe_derive_brake_zones()
check(hub7.brake_dists is not None, "frenaje: derivado con 2 autos y vueltas")
bd = hub7.brake_dists or {}
check(abs(bd.get(1500.0, 0.0) - 200.0) < 15.0,
      f"frenaje: T1 frena ~200 m antes ({bd.get(1500.0, 0.0):.0f} m)")
check(3500.0 not in bd, "frenaje: viraje a fondo sin zona de frenaje")
an7 = TimingAnalyzer(hub7)
d7 = an7._mark_dists()
# marca nominal de 1500 m: zona [1300, 1560] -> sale a la salida (1560)
check(abs(float(d7[6]) - 1560.0) < 1e-6,
      f"frenaje: corte fuera de la frenada medida ({float(d7[6]):.0f} m)")
# marca nominal de 3500 m: sin frenaje solo protege el vértice (±30/60)
check(abs(float(d7[14]) - 3470.0) < 1e-6,
      f"frenaje: viraje a fondo solo guarda el vértice ({float(d7[14]):.0f} m)")

# ------------------------------------------- marcas personalizadas (panel)

hub8 = DataHub()
hub8.on_track_length(L)
hub8.sector_bounds = (B1, B2)
hub8.custom_micro = [600.0, 1200.0, 3000.0, 4800.0, 5400.0]  # 3+2+3 µ
an8 = TimingAnalyzer(hub8)
m8 = an8._mark_dists()
check(len(m8) == 9 and an8.n_micro() == 8,
      f"custom: 5 cortes + límites = 8 µ ({an8.n_micro()})")
check(an8.sector_slices() == ((0, 3), (3, 5), (5, 8)),
      f"custom: cantidad libre por sector ({an8.sector_slices()})")
check(abs(float(m8[3]) - B1) < 1e-9 and abs(float(m8[5]) - B2) < 1e-9,
      "custom: los límites oficiales siempre son marcas")
hub8.on_batch([s for s in samples if s.driver == "44"])
sec8 = an8.sector_times("44", 3)
check(abs(sec8[0] - S1) < 0.2 and abs(sec8[1] - S2) < 0.2,
      f"custom: S1/S2 correctos con µ variables ({sec8[0]:.2f}, {sec8[1]:.2f})")
micro8 = an8.micro_times("44", 3)
for k, (a8, b8) in enumerate(an8.sector_slices()):
    check(abs(float(micro8[a8:b8].sum()) - sec8[k]) < 1e-6,
          f"custom: µ de S{k + 1} suman el sector ({b8 - a8} µ)")
# los rodantes respetan la config: un valor por µ configurado
roll8 = an8.latest_micro_times("44")
check(roll8 is not None and len(roll8[0]) == 8,
      "custom: µ rodantes con la cantidad configurada")

# ------------------------------------------------- autos fuera de carrera

hub9 = DataHub()
hub9.on_track_length(L)
act_samples = []
for drv, t_end in (("44", 300.0), ("16", 200.0)):  # 16 se clava en t=200
    t = 0.0
    while t < t_end:
        total = t * V
        act_samples.append(Sample(
            driver=drv, t=t, lap=int(total // L) + 1, dist_lap=total % L,
            dist_total=total, speed=V * 3.6, throttle=0.0, brake=0.0,
            rpm=0.0, gear=0, drs=0))
        t += 0.5
act_samples.sort(key=lambda s: s.t)  # cronológico, como una fuente real
hub9.on_batch(act_samples)
check(hub9.is_active("44"), "activo: auto en movimiento")
check(not hub9.is_active("16"),
      "activo: clavado / sin datos frescos queda fuera de carrera")
hub9.on_batch([Sample("5", 299.0, 1, 10.0, 10.0, 0.0,
                      0.0, 0.0, 0.0, 0, 0)])
check(hub9.is_active("5"), "activo: en vuelta 1 la grilla parada no caduca")
hub9.on_retirements(["44"])
check(not hub9.is_active("44"), "activo: Retired oficial lo saca")
hub9.on_retirements([])
check(hub9.is_active("44"), "activo: des-retirado vuelve")

# ------------------------------------------- re-anclaje en vivo (S1 oficial)

# fuente en vivo simulada: el cruce de meta se "descubre" con latencia
# variable (como TimingData); sin corrección el tiempo de vuelta hereda esa
# varianza, y el re-anclaje con el S1 oficial la reduce
hub4 = DataHub()
hub4.on_track_length(L)
FAST, SLOW = 80.0, 40.0                 # m/s: 0-2400 m rápidos, resto lento
S1_T = 1800.0 / FAST                    # 22.5 s hasta b1
S2_T = 600.0 / FAST + 1800.0 / SLOW    # 52.5 s de b1 a b2
LAP_T = 2400.0 / FAST + 3600.0 / SLOW  # 120 s por vuelta


def true_dist(t: float) -> float:
    n, tau = divmod(t, LAP_T)
    d = FAST * tau if tau <= 30.0 else 2400.0 + SLOW * (tau - 30.0)
    return n * L + d


LAT = [0.4, 1.8, 0.6, 1.6, 0.4, 1.8, 0.6, 1.6]  # latencia de cada cruce
live_samples = []
lap_now, base_now, next_cross = 1, 0.0, 1
t = 0.0
while t < 9 * LAP_T:
    total = true_dist(t)
    if (next_cross <= len(LAT)
            and t >= next_cross * LAP_T + LAT[next_cross - 1]):
        lap_now = next_cross + 1
        base_now = total  # como el decodificador: base en la detección
        next_cross += 1
    speed = FAST if (t % LAP_T) <= 30.0 else SLOW
    live_samples.append(Sample(
        driver="7", t=t, lap=lap_now, dist_lap=total - base_now,
        dist_total=total, speed=speed * 3.6, throttle=0.0, brake=0.0,
        rpm=0.0, gear=0, drs=0,
    ))
    t += 0.25
hub4.on_batch(live_samples)
hub4.on_sector_times([("7", lap, 0, S1_T) for lap in range(2, 9)]
                     + [("7", lap, 1, S2_T) for lap in range(2, 9)])
hub4.maybe_derive_sector_bounds()
check(hub4.sector_bounds is not None, "vivo: límites derivados del marco con latencia")

an4 = TimingAnalyzer(hub4)
hub4.live_frames = False
uncorr = np.array([an4.lap_time("7", lap) for lap in range(3, 8)])
hub4.live_frames = True
corr = np.array([an4.lap_time("7", lap) for lap in range(3, 8)])
err_u = float(np.abs(uncorr - LAP_T).mean())
err_c = float(np.abs(corr - LAP_T).mean())
check(err_u > 0.5, f"vivo: sin corrección la latencia mete error ({err_u:.2f} s)")
check(err_c < 0.6 * err_u,
      f"vivo: re-anclaje con S1 oficial reduce el error ({err_u:.2f} -> {err_c:.2f} s)")

# ---------------------------------------------- escalado por vuelta real

# una vuelta que integró 2% de más (deriva de integración): el tiempo de
# vuelta debe ser el real entre cruces, no el tiempo hasta "L integrado"
hub3 = DataHub()
hub3.on_track_length(L)
LEN_INT = L * 1.02          # 6120 m integrados por vuelta
DUR = LEN_INT / V           # 122.4 s reales por vuelta
drift = []
t = 0.0
while t < 4 * DUR:
    total = t * V
    drift.append(Sample(
        driver="99", t=t, lap=int(total // LEN_INT) + 1, dist_lap=total % LEN_INT,
        dist_total=total, speed=V * 3.6, throttle=0.0, brake=0.0,
        rpm=0.0, gear=0, drs=0,
    ))
    t += 0.25
hub3.on_batch(drift)
an3 = TimingAnalyzer(hub3)
lt = an3.lap_time("99", 2)
check(math.isfinite(lt) and abs(lt - DUR) < 0.05,
      f"scale: vuelta con deriva +2% da el tiempo real ({lt:.2f} vs {DUR:.2f} s)")
m = an3.lap_marks("99", 2)
check(m is not None and bool(np.isfinite(m).all()),
      "scale: todas las marcas finitas con deriva de integración")
sec3 = an3.sector_times("99", 2)
check(abs(sum(sec3) - lt) < 0.05, "scale: S1+S2+S3 = vuelta con deriva")

# ---------------------------------------- conexión a mitad de carrera

# al conectar con la carrera iniciada, la distancia integrada de cada auto
# arranca de 0 donde esté: sin proyección, la torre ordenaba mal hasta que
# todos cruzaban la meta
import math as _math  # noqa: E402

hub6 = DataHub()
hub6.live_frames = True
hub6.on_track_length(L)
_N = 720
hub6.on_outline(([_math.cos(2 * _math.pi * k / _N) * 1000 for k in range(_N)],
                 [_math.sin(2 * _math.pi * k / _N) * 1000 for k in range(_N)]))

join_samples, join_pos = [], []
for drv, phys0 in (("A", 4500.0), ("B", 1500.0)):  # A va físicamente adelante
    t = 0.0
    while t <= 20.0:  # 1000 m integrados desde la conexión
        d = t * V
        frac = ((phys0 + d) % L) / L
        join_samples.append(Sample(
            driver=drv, t=t, lap=44, dist_lap=d, dist_total=d, speed=V * 3.6,
            throttle=0.0, brake=0.0, rpm=0.0, gear=0, drs=0,
        ))
        join_pos.append((drv, t, _math.cos(2 * _math.pi * frac) * 1000,
                         _math.sin(2 * _math.pi * frac) * 1000))
        t += 0.25
hub6.on_batch(join_samples)
hub6.on_positions(join_pos)
an6 = TimingAnalyzer(hub6)

off_a = hub6.provisional_start_offset("A")
check(off_a == 0.0, f"mid-join: marco reescrito, offset residual 0 ({off_a})")
d_last = float(hub6.buffers["A"].col("dist_lap")[-1])
check(abs(d_last - 5500.0) < 60.0,
      f"mid-join: dist_lap de A medida desde la meta real ({d_last:.0f} m)")
check(an6.real_positions_ready("A") and an6.real_positions_ready("B"),
      "mid-join: posiciones confiables con proyección, sin esperar el cruce")
pos_a = float(an6.position_time("A")[0][-1])
pos_b = float(an6.position_time("B")[0][-1])
check(pos_a > pos_b, "mid-join: A ordena delante de B (posición física real)")
check(abs(pos_a - (43 * L + 5500.0)) < 80.0,
      f"mid-join: posición absoluta de A correcta ({pos_a:.0f})")
lt_join = an6.lap_time("A", 44)
check(lt_join != lt_join, "mid-join: la vuelta parcial de entrada no se cronometra")

# muestras posteriores en el marco viejo se corrigen; al cruzar la meta la
# corrección se retira (el decodificador ya ancla en la meta real)
hub6.on_batch([Sample("A", 21.0, 44, 21.0 * V, 21.0 * V, V * 3.6,
                      0.0, 0.0, 0.0, 0, 0)])
d_new = float(hub6.buffers["A"].col("dist_lap")[-1])
check(abs(d_new - (4500.0 + 21.0 * V)) < 60.0,
      f"mid-join: muestra nueva corregida al marco real ({d_new:.0f} m)")
hub6.on_batch([Sample("A", 31.0, 45, 50.0, 31.0 * V, V * 3.6,
                      0.0, 0.0, 0.0, 0, 0)])
check("A" not in hub6._dist_fix, "mid-join: la corrección se retira al cruzar")
check(float(hub6.buffers["A"].col("dist_lap")[-1]) == 50.0,
      "mid-join: tras el cruce las muestras quedan tal cual (ancla real)")

# sin proyección todavía (sin posiciones), un tercer auto NO está listo
hub6.on_batch([Sample("C", 0.0, 44, 400.0, 400.0, V * 3.6,
                      0.0, 0.0, 0.0, 0, 0)])
check(not an6.real_positions_ready("C"), "mid-join: sin proyección no está listo")

# ------------------------------------------------- segmentos en el hub

hub.on_segments([("44", 0, 0, 2049), ("44", 0, 5, 2051), ("44", 2, 3, 2064)])
check(hub.segment_counts == {0: 6, 2: 4},
      f"hub: cantidad de segmentos aprendida del feed ({hub.segment_counts})")
check(hub.segments["44"][(0, 5)] == 2051, "hub: estado por (sector, µ)")
hub.clear_samples()
check(not hub.segments and hub.sector_bounds is not None,
      "hub: seek limpia segmentos pero conserva los límites derivados")
hub.reset()
check(hub.sector_bounds is None and not hub.official_times,
      "hub: reset limpia todo")

print()
if FAILURES:
    print(f"{len(FAILURES)} FALLA(S)")
    raise SystemExit(1)
print("Todo OK")
