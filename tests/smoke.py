"""Test de humo sin pantalla: levanta la app real con la fuente demo,
recorre los 3 modos y canales, y verifica el decodificador del feed en vivo.

Uso:  python tests/smoke.py   (requiere QT_QPA_PLATFORM=offscreen)
"""
from __future__ import annotations

import base64
import json
import math
import os
import sys
import time
import zlib
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("F1TELEM_NO_UPDATE_CHECK", "1")  # sin red hacia GitHub
os.environ.setdefault("F1TELEM_DEV_SOURCES", "1")      # fuente demo en el combo
os.environ.setdefault("F1TELEM_NO_SCHEDULE", "1")      # sin calendario (red lenta)
os.environ.setdefault("F1TELEM_NO_OPENF1", "1")        # sin red hacia OpenF1
# sandbox: el smoke NUNCA debe leer ni escribir la config/los datos reales
import tempfile  # noqa: E402

os.environ["APPDATA"] = tempfile.mkdtemp(prefix="f1smoke_app_")
os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="f1smoke_local_")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
# la consola Windows (cp1252) no soporta Δ, →, −: degradar en vez de crashear
sys.stdout.reconfigure(errors="replace")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from f1telem.sources.live import LiveSource, decompress_feed
from f1telem.ui.main_window import LapRuler, MainWindow
from f1telem.ui.theme import apply_theme

FAILURES: list[str] = []


def check(cond: bool, msg: str) -> None:
    tag = "OK " if cond else "FAIL"
    print(f"[{tag}] {msg}", flush=True)
    if not cond:
        FAILURES.append(msg)


def pump(app: QApplication, seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)


def test_live_decoder() -> None:
    src = LiveSource()
    got: list = []
    src.batch.connect(lambda batch: got.extend(batch), Qt.DirectConnection)
    statuses: list[str] = []
    src.statusChanged.connect(statuses.append, Qt.DirectConnection)

    def zpack(obj) -> str:
        raw = json.dumps(obj).encode()
        comp = zlib.compress(raw)[2:-4]  # deflate crudo sin cabecera zlib
        return base64.b64encode(comp).decode()

    car_entry = lambda utc, speed: {
        "Utc": utc,
        "Cars": {"1": {"Channels": {"0": 11200, "2": speed, "3": 7, "4": 99, "5": 0, "45": 12}}},
    }
    # snapshot inicial (respuesta R) + feeds incrementales
    src._handle({"R": {
        "DriverList": {"1": {"RacingNumber": "1", "Tla": "VER", "FullName": "Max Verstappen",
                              "TeamName": "Red Bull", "TeamColour": "3671C6"}},
        "TimingData": {"Lines": {"1": {"NumberOfLaps": 3}}},
    }})
    src._handle({"M": [{"H": "Streaming", "M": "feed",
                        "A": ["CarData.z", zpack({"Entries": [
                            car_entry("2026-07-05T14:00:00.1234567Z", 250),
                            car_entry("2026-07-05T14:00:01.1234567Z", 260),
                        ]}), "ts"]}]})
    check(len(got) == 2, f"decoder en vivo produce muestras ({len(got)})")
    check(got[-1].lap == 4, f"vuelta = NumberOfLaps+1 ({got[-1].lap})")
    check(abs(got[-1].dist_total - (250 + 260) / 2 / 3.6) < 0.1,
          f"distancia integrada trapezoidal ({got[-1].dist_total:.2f} m)")
    check(got[-1].speed == 260 and got[-1].gear == 7, "canales speed/gear correctos")

    # cruce de meta: TimingData incrementa vueltas -> dist_lap se reinicia
    src._handle({"M": [{"H": "Streaming", "M": "feed",
                        "A": ["TimingData", {"Lines": {"1": {"NumberOfLaps": 4}}}, "ts"]}]})
    src._handle({"M": [{"H": "Streaming", "M": "feed",
                        "A": ["CarData.z", zpack({"Entries": [
                            car_entry("2026-07-05T14:00:02.0000000Z", 270),
                        ]}), "ts"]}]})
    check(got[-1].lap == 5 and got[-1].dist_lap == 0.0,
          f"reinicio de dist_lap al cambiar de vuelta (lap={got[-1].lap}, d={got[-1].dist_lap})")
    rt = decompress_feed(zpack({"a": 1}))
    check(rt == {"a": 1}, "decompress_feed ida y vuelta")


def test_gap_grid_offset() -> None:
    """Semántica replay: la vuelta 1 no arranca en la línea (offset de
    grilla). Dos autos idénticos separados 200 m deben dar gap constante
    de 4 s a 50 m/s — también durante la vuelta 1 — y gap 0 solo si están
    igualados en pista."""
    import numpy as np
    from f1telem.hub import DataHub
    from f1telem.models import Sample
    from f1telem.timing import TimingAnalyzer

    hub = DataHub()
    L = 5000.0
    hub.on_track_length(L)
    v = 50.0  # m/s constantes

    def mk(drv: str, t: float, grid_offset: float) -> Sample:
        driven = v * t
        phys = driven - grid_offset  # posición física respecto de la línea
        if phys < L:
            lap, d = 1, driven  # la vuelta 1 incluye el offset de grilla
        else:
            lap = int(phys // L) + 1
            d = phys - (lap - 1) * L
        return Sample(drv, t, lap, d, driven, v * 3.6, 100.0, 0.0, 10000.0, 7, 0)

    batch = []
    for k in range(320):  # ~3 vueltas
        batch.append(mk("A", float(k), 0.0))
        batch.append(mk("B", float(k), 200.0))
    hub.on_batch(batch)
    an = TimingAnalyzer(hub)
    # vuelta 1 en curso SIN trazado/posiciones: no se puede estimar la grilla
    hub_early = DataHub()
    hub_early.on_track_length(L)
    hub_early.on_batch([mk("A", float(k), 0.0) for k in range(80)]
                       + [mk("B", float(k), 200.0) for k in range(80)])
    an_early = TimingAnalyzer(hub_early)
    check(an_early.gap_series("B", "A") is None,
          "sin trazado no hay gap en la vuelta 1 en curso")

    # vuelta 1 en curso CON trazado y posiciones: el offset de grilla se
    # estima por proyección y el gap real aparece desde el fin del S1
    from f1telem.sources.demo import TRACK_LEN as DL, _TRACK_X, _TRACK_Y, track_pos as _tp
    hub_l1 = DataHub()
    hub_l1.on_track_length(DL)
    hub_l1.on_outline((_TRACK_X, _TRACK_Y))
    batch, posb = [], []
    for k in range(80):
        t = float(k)
        for drv, grid in (("A", 0.0), ("B", 200.0)):
            driven = 50.0 * t
            batch.append(Sample(drv, t, 1, driven, driven, 180.0, 100.0, 0.0, 10000.0, 7, 0))
            posb.append((drv, t, *_tp(driven - grid)))
    hub_l1.on_batch(batch)
    hub_l1.on_positions(posb)
    an_l1 = TimingAnalyzer(hub_l1)
    check(abs((hub_l1.provisional_start_offset("B") or -1) - 200.0) < 25.0,
          f"offset de grilla estimado por proyección ({hub_l1.provisional_start_offset('B')})")
    g_l1 = an_l1.gap_series("B", "A")
    check(g_l1 is not None, "gap disponible DURANTE la vuelta 1 (tras el S1)")
    check(float(g_l1[0][0]) >= DL / 3.0 - 30.0,
          f"gap de vuelta 1 arranca en el S1 ({float(g_l1[0][0]):.0f} m)")
    check(abs(float(g_l1[1][-1]) - 4.0) < 0.4,
          f"gap real durante la vuelta 1 ({float(g_l1[1][-1]):+.2f} s)")

    x, y = an.gap_series("B", "A")
    check(float(x[0]) >= L / 3.0 - 1.0,
          f"gap arranca en el fin del S1 de la vuelta 1 ({float(x[0]):.0f} m)")
    check(abs(float(y[0]) - 4.0) < 0.3,
          f"gap inicial = diferencia real en el S1 ({float(y[0]):+.2f} s)")
    early = y[(x > 500) & (x < 4000)]   # dentro de la vuelta 1
    late = y[x > L * 1.2]               # después de la vuelta 1
    check(len(early) > 0 and abs(float(np.median(early)) - 4.0) < 0.3,
          f"gap correcto con desfase de grilla en vuelta 1 ({float(np.median(early)):+.2f} s)")
    check(len(late) > 0 and abs(float(np.median(late)) - 4.0) < 0.3,
          f"gap constante tras la vuelta 1 ({float(np.median(late)):+.2f} s)")
    # igualados en pista => gap 0: C parte del mismo lugar físico que A
    batch = [mk("C", float(k), 0.0) for k in range(320)]
    hub.on_batch(batch)
    xc, yc = an.gap_series("C", "A")
    check(float(np.nanmax(np.abs(yc))) < 0.2,
          f"gap 0 con autos igualados en pista (max {float(np.nanmax(np.abs(yc))):.3f} s)")


def _zpack(obj) -> str:
    raw = json.dumps(obj).encode()
    comp = zlib.compress(raw)[2:-4]  # deflate crudo sin cabecera zlib
    return base64.b64encode(comp).decode()


def test_capture_source() -> None:
    """Capturador + fuente Capture: archivo sintético seguido en vivo (cola
    con delay mínimo), salto hacia atrás y vuelta al LIVE."""
    import tempfile
    from f1telem.hub import DataHub
    from f1telem.sources.capture import CaptureSource

    def frame(k: int) -> str:
        utc = f"2026-07-06T14:00:{0:02d}.0000000Z".replace(":00.", f":00.")
        # Utc creciente: base + k segundos
        mm, ss = divmod(k, 60)
        utc = f"2026-07-06T14:{mm:02d}:{ss:02d}.0000000Z"
        data = _zpack({"Entries": [{
            "Utc": utc,
            "Cars": {"1": {"Channels": {"0": 11000, "2": 250 + (k % 7), "3": 7,
                                        "4": 99, "5": 0, "45": 12}}},
        }]})
        return json.dumps({"M": [{"H": "Streaming", "M": "feed",
                                  "A": ["CarData.z", data, ""]}]})

    path = Path(tempfile.mkdtemp()) / "capture_test.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps({"R": {
            "DriverList": {"1": {"RacingNumber": "1", "Tla": "VER",
                                 "FullName": "Max Verstappen",
                                 "TeamName": "Red Bull", "TeamColour": "3671C6"}},
            "TimingData": {"Lines": {"1": {"NumberOfLaps": 0}}},
        }}) + "\n")
        for k in range(60):
            if k and k % 20 == 0:  # cruce de meta cada 20 s
                f.write(json.dumps({"M": [{"H": "Streaming", "M": "feed",
                                           "A": ["TimingData",
                                                 {"Lines": {"1": {"NumberOfLaps": k // 20}}},
                                                 ""]}]}) + "\n")
            f.write(frame(k) + "\n")

    hub = DataHub()
    resets = [0]
    src = CaptureSource(path, speed=25.0)
    src.batch.connect(hub.on_batch, Qt.DirectConnection)
    src.driversDiscovered.connect(hub.on_drivers, Qt.DirectConnection)
    src.seekReset.connect(lambda: (resets.__setitem__(0, resets[0] + 1),
                                   hub.clear_samples()), Qt.DirectConnection)
    src.start()

    def wait_for(cond, timeout: float) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if cond():
                return True
            time.sleep(0.02)
        return False

    check(wait_for(lambda: hub.total_samples >= 60, 10.0),
          f"capture: archivo completo leído ({hub.total_samples} muestras)")
    check(src.live_mode, "capture: arranca en modo LIVE")
    check("VER" in [d.code for d in hub.drivers.values()], "capture: snapshot decodificado")

    # cola en vivo: agregar frames y medir el delay de llegada
    t_write = time.monotonic()
    with open(path, "a", encoding="utf-8") as f:
        for k in range(60, 65):
            f.write(frame(k) + "\n")
    arrived = wait_for(lambda: hub.total_samples >= 65, 3.0)
    delay = time.monotonic() - t_write
    check(arrived and delay < 1.0, f"capture: cola en vivo con delay mínimo ({delay * 1000:.0f} ms)")

    # torre vs saltos de tiempo: los datos jamás deben desaparecer
    from f1telem.ui.tower import TimingTower
    tower_c = TimingTower(hub)
    tower_c.refresh()
    check(len(tower_c.rows) == 1, "torre+seek: fila presente en vivo")

    # salto hacia atrás
    src.request_seek(20.0)
    check(wait_for(lambda: resets[0] == 1 and not src.live_mode
                   and 15.0 <= hub.latest_t <= 26.0, 5.0),
          f"capture: seek atrás reconstruye hasta el punto (t={hub.latest_t:.0f})")
    tower_c.clear_data()  # como hace la app en cada seekReset
    tower_c.refresh()
    check(len(tower_c.rows) == 1,
          f"torre+seek: fila reaparece tras el salto atrás ({len(tower_c.rows)})")

    # volver al vivo
    src.go_live()
    check(wait_for(lambda: src.live_mode and hub.latest_t >= 64.0, 5.0),
          f"capture: LIVE vuelve al último dato (t={hub.latest_t:.0f})")
    tower_c.clear_data()
    tower_c.refresh()
    check(len(tower_c.rows) == 1,
          f"torre+seek: fila reaparece tras volver al vivo ({len(tower_c.rows)})")

    src.stop()
    src.wait(5000)


def test_catch_projection() -> None:
    """Proyección de alcance con datos exactos: B (50,5 m/s) persigue a A
    (50 m/s) desde 300 m => rate ~0,99 s/vuelta, alcance en ~2 vueltas."""
    from f1telem.hub import DataHub
    from f1telem.models import Sample
    from f1telem.ui.tower import TimingTower

    hub = DataHub()
    L = 5000.0
    hub.on_track_length(L)

    def mk(drv: str, t: float, v: float, grid: float) -> Sample:
        driven = v * t
        phys = driven - grid
        if phys < L:
            lap, d = 1, driven
        else:
            lap = int(phys // L) + 1
            d = phys - (lap - 1) * L
        return Sample(drv, t, lap, d, driven, v * 3.6, 100.0, 0.0, 10000.0, 7, 0)

    batch = []
    for k in range(401):
        batch.append(mk("A", float(k), 50.0, 0.0))
        batch.append(mk("B", float(k), 50.5, 300.0))
    hub.on_batch(batch)
    tower = TimingTower(hub)
    pts = {d: tower.analyzer.position_time(d) for d in ("A", "B")}
    laps = tower._catch_laps("B", "A", pts, L)
    check(laps is not None and 1.6 < laps < 2.5,
          f"proyección de alcance ~2 vueltas ({None if laps is None else round(laps, 2)})")


def test_delta_wave() -> None:
    """Delta gráfico con la lógica JRT: delta = REL − REL_al_inicio_de_la_
    vuelta del cursor (que avanza con el auto que va DETRÁS de la pareja),
    reset por vuelta con la anterior atenuada; rojo/arriba = la referencia
    pierde contra ese rival esta vuelta, verde/abajo = le gana."""
    import numpy as np

    from f1telem.hub import DataHub
    from f1telem.models import Sample
    from f1telem.ui.tower import TimingTower

    hub = DataHub()
    L = 5000.0
    V = 50.0  # m/s: vuelta de 100 s
    hub.on_track_length(L)

    def gap_b(t: float) -> float:
        # constante 10 s hasta t=300; después crece 2 s por vuelta
        return 10.0 if t <= 300.0 else 10.0 + 2.0 * (t - 300.0) / 100.0

    def mk(drv: str, t: float, gap: float) -> Sample | None:
        pos = V * (t - gap)
        if pos < 0:
            return None
        lap = int(pos // L) + 1
        return Sample(drv, t, lap, pos % L, pos, V * 3.6,
                      100.0, 0.0, 10000.0, 7, 0)

    tower = TimingTower(hub)

    def feed(t0: float, t1: float) -> None:
        batch = []
        t = t0
        while t <= t1 + 1e-9:
            for drv, gap in (("A", 0.0), ("B", gap_b(t)), ("C", 10.0)):
                s = mk(drv, t, gap)
                if s is not None:
                    batch.append(s)
            t += 0.5
        hub.on_batch(batch)
        tower.refresh()

    feed(0.0, 300.0)
    for t_end in range(310, 401, 10):  # refrescos periódicos, como en vivo
        feed(t_end - 9.5, float(t_end))
    rows = {r.drv: r for r in tower.rows}
    check(rows["A"].wave is None and rows["B"].wave is not None,
          "delta: rivales con gráfico, la referencia (líder) sin")
    y_c = rows["C"].wave[1]
    check(float(np.nanmax(np.abs(y_c))) < 0.2,
          f"delta: REL clavado queda neutro "
          f"(max {float(np.nanmax(np.abs(y_c))):.2f})")
    y_b, prev_b = rows["B"].wave[1], rows["B"].wave[2]
    n = len(y_b)
    bi = int(((V * (400.0 - gap_b(400.0))) % L) / L * n)
    y_line = float(y_b[bi % n])
    check(-2.1 < y_line < -1.3,
          f"delta: B alejándose pinta verde creciente ({y_line:+.2f})")
    check(abs(float(y_b[2])) < 0.4,
          f"delta: cada vuelta arranca en ~0 ({float(y_b[2]):+.2f})")
    check(prev_b is not None,
          "delta: la vuelta anterior queda guardada para atenuar")
    check(float(np.nanmax(y_b)) < 0.15,
          "delta: la referencia ganando jamás pinta rojo")

    # referencia B (mitad del pelotón): el rival de ADELANTE escapándose
    # pinta rojo, y el cursor avanza con la referencia (la rezagada)
    tower.set_reference("B")
    for t_end in range(410, 471, 10):
        feed(t_end - 9.5, float(t_end))
    rows_b = {r.drv: r for r in tower.rows}
    check(rows_b["A"].wave is not None,
          "delta: el rival de adelante también se grafica")
    y_a = rows_b["A"].wave[1]
    bi_a = int(((V * (470.0 - gap_b(470.0))) % L) / L * len(y_a))
    y_probe = float(y_a[bi_a % len(y_a)])
    check(0.4 < y_probe < 1.5,
          f"delta: adelante escapándose pinta rojo con el cursor de la "
          f"referencia ({y_probe:+.2f})")

    # pintado incremental: 2 s más solo repintan los bins del cursor
    snap = tower._wave_store["A"].copy()
    feed(470.5, 472.0)
    y2 = tower._wave_store["A"]
    same = (y2 == snap) | (np.isnan(y2) & np.isnan(snap))
    check(0 < int((~same).sum()) <= 8,
          f"delta: pintado incremental ({int((~same).sum())} bins)")

    # escala de color: gris→rojo/verde hasta 1 s, magenta/cian en ±2 s
    check(tower._wave_color(2.0).name() == "#c852ff"
          and tower._wave_color(-2.0).name() == "#35d0c8"
          and tower._wave_color(0.5).red() > tower._wave_color(0.0).red(),
          "delta: gradiente y saturación de color JRT")


def test_openf1() -> None:
    """Cliente OpenF1: partes puras (sin red) — matching de sesión,
    parsers y limitador de requests."""
    from f1telem import openf1

    # limitador: nunca más de 30 requests por minuto, con reloj simulado
    clock = [0.0]
    sleeps: list[float] = []

    def _sleep(s: float) -> None:
        sleeps.append(s)
        clock[0] += s

    rl = openf1.RateLimiter(now=lambda: clock[0], sleep=_sleep)
    stamps = []
    for _ in range(60):  # cliente ansioso: pide sin pausa propia
        rl.wait()
        stamps.append(clock[0])
        clock[0] += 0.05
    worst = max(sum(1 for t in stamps if t0 <= t < t0 + 60.0) for t0 in stamps)
    check(worst <= 30, f"openf1: máximo {worst} requests en 60 s (≤ 30)")
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    check(min(gaps) >= openf1.MIN_INTERVAL_S - 1e-9,
          f"openf1: separación mínima entre requests ({min(gaps):.2f}s)")

    meetings = [
        {"meeting_key": 1, "meeting_name": "Bahrain Grand Prix",
         "meeting_official_name": "Formula 1 Gulf Air Bahrain Grand Prix",
         "circuit_short_name": "Sakhir", "location": "Sakhir",
         "country_name": "Bahrain"},
        {"meeting_key": 2, "meeting_name": "Miami Grand Prix",
         "meeting_official_name": "Formula 1 Crypto.com Miami Grand Prix",
         "circuit_short_name": "Miami", "location": "Miami",
         "country_name": "United States"},
    ]
    hit = openf1.match_meeting(meetings, "Miami Grand Prix")
    check(hit is not None and hit["meeting_key"] == 2,
          "openf1: meeting por nombre exacto")
    hit = openf1.match_meeting(meetings, "GP de Miami")
    check(hit is not None and hit["meeting_key"] == 2,
          "openf1: meeting por coincidencia de palabras")
    check(openf1.match_meeting(meetings, "Demo Grand Prix") is None,
          "openf1: sin coincidencia real no elige nada")

    sessions = [
        {"session_key": 10, "session_name": "Practice 1",
         "session_type": "Practice"},
        {"session_key": 11, "session_name": "Qualifying",
         "session_type": "Qualifying"},
        {"session_key": 12, "session_name": "Race", "session_type": "Race"},
    ]
    hit = openf1.pick_session(sessions, "Race", "Race")
    check(hit is not None and hit["session_key"] == 12,
          "openf1: tanda por nombre")
    hit = openf1.pick_session(sessions, "", "Qualifying")
    check(hit is not None and hit["session_key"] == 11,
          "openf1: tanda por tipo si el nombre no aparece")
    check(openf1.pick_session(sessions, "Sprint", "") is None,
          "openf1: tanda inexistente devuelve None")

    grid = openf1.parse_grid([
        {"position": 1, "driver_number": 1},
        {"position": 2, "driver_number": 44},
        {"position": None, "driver_number": 16},  # sin dato: fuera
    ])
    check(grid == {"1": 1, "44": 2}, f"openf1: parse_grid ({grid})")

    # finde con sprint: el endpoint trae DOS grillas por meeting, cada una
    # con la key de la clasificación que la definió
    sprint_sessions = [
        {"session_key": 20, "session_name": "Sprint Qualifying"},
        {"session_key": 21, "session_name": "Sprint"},
        {"session_key": 22, "session_name": "Qualifying"},
        {"session_key": 23, "session_name": "Race"},
    ]
    grid_rows = [
        {"session_key": 20, "position": 1, "driver_number": 44},
        {"session_key": 22, "position": 1, "driver_number": 1},
    ]
    race_rows = openf1.pick_grid_rows(grid_rows, sprint_sessions, "Race")
    check(openf1.parse_grid(race_rows) == {"1": 1},
          "openf1: grilla de carrera desde la Qualifying")
    spr_rows = openf1.pick_grid_rows(grid_rows, sprint_sessions, "Sprint")
    check(openf1.parse_grid(spr_rows) == {"44": 1},
          "openf1: grilla de sprint desde la Sprint Qualifying")
    solo = openf1.pick_grid_rows(
        [{"session_key": 9, "position": 1, "driver_number": 4}], [], "Race")
    check(openf1.parse_grid(solo) == {"4": 1},
          "openf1: con un único grupo de grilla se usa ese")

    pits = openf1.parse_pits([
        {"driver_number": 1, "lap_number": 14, "lane_duration": 21.7,
         "stop_duration": 2.3},
        {"driver_number": 1, "lap_number": 33, "lane_duration": 22.0,
         "stop_duration": None},          # pre-2024: sin dato oficial
        {"driver_number": 44, "lap_number": 15, "pit_duration": 23.1},
        {"driver_number": 16},            # fila rota: fuera
    ])
    check(pits["1"][14] == (21.7, 2.3), "openf1: parse_pits completo")
    check(pits["1"][33][1] != pits["1"][33][1],
          "openf1: stop_duration ausente queda NaN")
    check(pits["44"][15][0] == 23.1,
          "openf1: lane cae al pit_duration deprecado")
    check("16" not in pits, "openf1: fila sin vuelta descartada")

    shots = openf1.parse_headshots([
        {"driver_number": 1, "headshot_url": "https://x/ver.png"},
        {"driver_number": 44, "headshot_url": None},
    ])
    check(shots == {"1": "https://x/ver.png"},
          f"openf1: parse_headshots ({shots})")

    # normalización de clima en el hub: filas viejas de 5 campos -> 8
    from f1telem.hub import DataHub as _DH
    hub = _DH()
    hub.on_weather([(0.0, 25.0, 40.0, 3.0, False)])
    row = hub.weather[0]
    check(len(row) == 8 and all(v != v for v in row[5:]),
          "hub: clima de 5 campos rellenado con NaN")


def test_analysis_engine() -> None:
    """Motor de análisis con una pista 'estadio' sintética: 2 rectas de
    800 m y 2 curvas semicirculares de radio 100 m, con física analítica
    (G lateral = v²/R, clipping y lift & coast fabricados a propósito)."""
    import numpy as np

    from f1telem.analysis import AnalysisEngine
    from f1telem.hub import DataHub as _DH
    from f1telem.models import Sample as _S

    R, S = 100.0, 800.0
    arc = math.pi * R
    L = 2 * S + 2 * arc  # ≈ 2228.32 m
    # trazado: recta A (y=0) -> curva 1 -> recta B (y=200, invertida) -> curva 2
    d_pts = np.arange(0.0, L, 5.0)
    xs, ys = [], []
    for d in d_pts:
        if d < S:
            xs.append(d); ys.append(0.0)
        elif d < S + arc:
            a = (d - S) / R
            xs.append(S + R * math.sin(a)); ys.append(R - R * math.cos(a))
        elif d < 2 * S + arc:
            xs.append(S - (d - S - arc)); ys.append(2 * R)
        else:
            a = (d - 2 * S - arc) / R
            xs.append(-R * math.sin(a)); ys.append(R + R * math.cos(a))
    hub_an = _DH()
    hub_an.on_track_length(L)
    hub_an.on_outline((np.array(xs), np.array(ys)))
    hub_an.on_corners([("T1", S + arc / 2, 0.0, 0.0),
                       ("T2", 2 * S + 1.5 * arc, 0.0, 0.0)])

    c1, c2 = S, 2 * S + arc  # inicio de cada curva
    zones_d = [(0.0, S), (c1, c1 + arc), (c1 + arc, c2), (c2, L)]

    def gen_lap(lap_no, t, v, cv, clip=False, lift=False):
        out = []
        pos = (lap_no - 1) * L
        end = lap_no * L
        while pos < end:
            d = pos - (lap_no - 1) * L
            in_c1 = zones_d[1][0] <= d < zones_d[1][1]
            in_c2 = d >= zones_d[3][0]
            throttle, brake, a = 100.0, 0.0, 6.0
            if in_c1 or in_c2:
                v = cv
                throttle, a = 30.0, 0.0
            else:
                nxt = c1 if d < c1 else c2
                gap = nxt - d
                brake_d = max(0.0, (v * v - cv * cv) / (2.0 * 25.0))
                if gap <= brake_d + 5.0:
                    throttle, brake, a = 0.0, 100.0, -25.0
                elif lift and d > zones_d[2][0] and gap <= brake_d + 155.0:
                    throttle, a = 0.0, -1.5  # lift & coast
                elif clip and d < S and v >= 70.0:
                    a = 0.0  # derate: a fondo sin acelerar
            out.append(_S("1", t, lap_no, d, pos, v * 3.6, throttle,
                          brake, 10500.0, 5, 0))
            v = max(cv, v + a * 0.1)
            pos += v * 0.1
            t += 0.1
        return out, t, v

    t, v = 0.0, 43.0
    for lap_no, cv, clip, lift in ((1, 43.0, False, False),
                                   (2, 41.5, True, True),
                                   (3, 40.0, False, False),
                                   (4, 40.0, False, False)):
        batch, t, v = gen_lap(lap_no, t, v, cv, clip, lift)
        hub_an.on_batch(batch)

    eng = AnalysisEngine(hub_an)
    zs = eng.zones()
    n_corner = sum(1 for z in zs if z.kind == "corner")
    n_str = sum(1 for z in zs if z.kind == "straight")
    check(n_corner == 2 and n_str == 2,
          f"analysis: 2 curvas y 2 rectas detectadas ({n_corner}/{n_str})")
    check({z.label for z in zs if z.kind == "corner"} == {"T1", "T2"},
          "analysis: curvas etiquetadas con los vértices oficiales")
    check(any("main" in z.label for z in zs if z.kind == "straight"),
          "analysis: la recta principal marcada")

    chan = eng.channels("1")
    check(chan is not None and float(chan["a_lon"].min()) < -1.8,
          f"analysis: G de frenada ({chan['a_lon'].min():.2f})")
    check(0.4 < float(chan["a_lon"].max()) < 0.9,
          f"analysis: G de tracción ({chan['a_lon'].max():.2f})")

    m2 = eng.lap_metrics("1", 2)   # con clipping y lift
    m3 = eng.lap_metrics("1", 3)   # limpia
    # precisión: el plateau fabricado va de ~275 m (llega a 70 m/s) hasta
    # la frenada (~730 m); con guardas de transición, esperar 350-520 m
    check(m2 is not None and 350.0 <= m2.derate_total <= 520.0,
          f"analysis: clipping preciso ({m2.derate_total:.0f} m, real ~450)")
    check(m3.derate_total < 40.0,
          f"analysis: vuelta limpia sin derate ({m3.derate_total:.0f} m)")
    # precisión: el lift fabricado es de 150 m antes del punto de frenada
    check(120.0 <= m2.coast_total <= 200.0,
          f"analysis: lift & coast preciso ({m2.coast_total:.0f} m, real ~150)")
    check(m3.coast_total < 30.0,
          f"analysis: sin lift no hay coast ({m3.coast_total:.0f} m)")
    check(abs(m2.deploy_m + m2.derate_total - m2.wot_straight_m) < 60.0,
          "analysis: deploy + derate ≈ metros a fondo en recta")
    zi_da = next(iter(m2.derate_m))
    span_d = m2.derate_end[zi_da] - m2.derate_start[zi_da]
    check(abs(span_d - m2.derate_m[zi_da]) < 40.0,
          f"analysis: extensión medida del derate en el mapa "
          f"({span_d:.0f} vs {m2.derate_m[zi_da]:.0f} m)")
    corner_zi = [i for i, z in enumerate(zs) if z.kind == "corner"]
    vmin, alat = m3.corners[corner_zi[0]]
    expect = 40.0 * 40.0 / R / 9.81  # 1.63 g
    check(abs(vmin - 40.0) < 2.5,
          f"analysis: V min de curva ({vmin:.1f} m/s)")
    check(abs(alat - expect) < 0.4,
          f"analysis: G lateral = v²/R ({alat:.2f} vs {expect:.2f})")
    # degradación: la velocidad de curva cae 43 -> 40 entre vueltas
    m1 = eng.lap_metrics("1", 1)
    check(m1.corners[corner_zi[0]][1] > m3.corners[corner_zi[0]][1],
          "analysis: la G lateral cae con la degradación fabricada")
    # el coast quedó atribuido a la curva 2 (venía por la recta B)
    check(corner_zi[1] in m2.coast_m and m2.coast_m[corner_zi[1]] > 80.0,
          "analysis: coast atribuido a la curva siguiente")
    check(m2.coast_n.get(corner_zi[1], 0) == 1,
          "analysis: un solo evento de coast en esa curva")

    # envolvente convexa del círculo de fricción
    from f1telem.analysis import convex_hull
    hx, hy = convex_hull(np.array([0.0, 2.0, 2.0, 0.0, 1.0]),
                         np.array([0.0, 0.0, 2.0, 2.0, 1.0]))
    check(len(hx) == 5 and (hx[0], hy[0]) == (hx[-1], hy[-1]),
          f"analysis: envolvente = cuadrado cerrado ({len(hx)} vértices)")
    check(1.0 not in hx[:-1] or (1.0, 1.0) not in list(zip(hx, hy)),
          "analysis: el punto interior queda fuera de la envolvente")

    # máscara multi-zona: solo las dos curvas
    mask = eng.zone_mask(np.array([100.0, S + 50.0, S + arc + 100.0,
                                   2 * S + arc + 50.0]),
                         ("multi", frozenset(corner_zi)))
    check(list(mask) == [False, True, False, True],
          "analysis: máscara multi-zona (solo curvas elegidas)")

    # unidades crudas del feed: Position.z viene en décimas de metro. El
    # mismo circuito en dm debe dar las mismas zonas y la misma G lateral
    # (bug real: solo aparecían las horquillas y la G quedaba /10). Además
    # una curva oficial suave en plena recta debe ganar su propia zona.
    hub_dm = _DH()
    hub_dm.on_track_length(L)
    hub_dm.on_outline((np.array(xs) * 10.0, np.array(ys) * 10.0))
    hub_dm.on_corners([("T1", S + arc / 2, 0.0, 0.0),
                       ("T2", 2 * S + 1.5 * arc, 0.0, 0.0),
                       ("T9", 400.0, 0.0, 0.0)])  # kink suave en la recta A
    t2, v2 = 0.0, 43.0
    for lap_no, cv, clip, lift in ((1, 43.0, False, False),
                                   (2, 41.5, True, True),
                                   (3, 40.0, False, False),
                                   (4, 40.0, False, False)):
        batch, t2, v2 = gen_lap(lap_no, t2, v2, cv, clip, lift)
        hub_dm.on_batch(batch)
    eng_dm = AnalysisEngine(hub_dm)
    zs_dm = eng_dm.zones()
    n_c = sum(1 for z in zs_dm if z.kind == "corner")
    n_s = sum(1 for z in zs_dm if z.kind == "straight")
    check(n_c == 3 and n_s == 3,
          f"analysis: trazado en dm -> mismas zonas + kink oficial "
          f"({n_c}/{n_s})")
    check({z.label for z in zs_dm if z.kind == "corner"}
          == {"T1", "T2", "T9"},
          "analysis: zona carvada para la curva oficial suave")
    m3dm = eng_dm.lap_metrics("1", 3)
    zi_t1 = next(i for i, z in enumerate(zs_dm) if z.label == "T1")
    check(zi_t1 in m3dm.corners
          and abs(m3dm.corners[zi_t1][1] - expect) < 0.4,
          f"analysis: G lateral correcta con trazado en dm "
          f"({m3dm.corners[zi_t1][1]:.2f} vs {expect:.2f})")

    # líneas de tendencia (panel Acceleration)
    from f1telem.analysis import fit_trend
    xs_f = np.linspace(50.0, 300.0, 60)
    fit = fit_trend(xs_f, 2.0 * xs_f + 1.0, "linear")
    check(fit is not None and abs(fit[1][0] - (2.0 * fit[0][0] + 1.0)) < 1e-6
          and abs(fit[1][-1] - (2.0 * fit[0][-1] + 1.0)) < 1e-6,
          "analysis: tendencia lineal exacta")
    fit = fit_trend(xs_f, -3.0 * np.exp(-0.01 * xs_f), "exponential")
    check(fit is not None and fit[1][0] < 0
          and abs(fit[1][-1] - (-3.0 * math.exp(-0.01 * fit[0][-1]))) < 0.05,
          "analysis: tendencia exponencial con signo negativo")
    check(fit_trend(xs_f[:4], xs_f[:4], "linear") is None,
          "analysis: tendencia rechaza pocos puntos")


def test_preprocessing() -> None:
    """Preproceso de datos: despike, cruces por Hermite (exactos bajo
    aceleración constante) y perfiles de curva entrenados por ensamble."""
    import numpy as np

    from f1telem.analysis import AnalysisEngine
    from f1telem.hub import DataHub as _DH
    from f1telem.models import Sample as _S
    from f1telem.timing import TimingAnalyzer as _TA

    # despike: un pico imposible se reemplaza por la mediana vecina
    out = _TA._despike(np.array([200.0, 201.0, 400.0, 202.0, 203.0]))
    check(out[2] == 202.0 and out[1] == 201.0,
          f"preproc: pico imposible corregido ({out[2]:.0f})")

    # Hermite: frenada constante muestreada CADA 3 s; el cruce del fin de
    # S1 (d=1000) tiene solución analítica t=12.98s — lineal erraría ~35ms
    hub_h = _DH()
    hub_h.on_track_length(3000.0)
    hub_h.sector_bounds = (1000.0, 2000.0)
    hub_h._bounds_done = True
    v0 = 90.0              # d(t) = 90t − t², v(t) = 90 − 2t hasta t=20
    samples = []
    for k in range(0, 8):  # t = 0..21, muestras cada 3 s
        t = k * 3.0
        if t <= 20.0:
            d = v0 * t - t * t
            v = v0 - 2.0 * t
        else:
            d = 1400.0 + 50.0 * (t - 20.0)
            v = 50.0
        samples.append(_S("1", t, 1, d, d, v * 3.6, 100.0, 0.0, 0.0, 7, 0))
    for k in range(3):  # vuelta siguiente: ancla el cierre
        t = 53.0 + k
        samples.append(_S("1", t, 2, 50.0 * k, 3000.0 + 50.0 * k,
                          180.0, 100.0, 0.0, 0.0, 7, 0))
    # completar la vuelta 1 hasta la meta (v constante 50)
    extra = []
    for k in range(8, 18):
        t = k * 3.0
        d = 1400.0 + 50.0 * (t - 20.0)
        if d < 2999.0:
            extra.append(_S("1", t, 1, d, d, 180.0, 100.0, 0.0, 0.0, 7, 0))
    samples = [s for s in samples if s.lap == 1] + extra \
        + [s for s in samples if s.lap == 2]
    samples.sort(key=lambda s: s.t)
    hub_h.on_batch(samples)
    an_h = _TA(hub_h)
    marks = an_h.lap_marks("1", 1)
    i1 = an_h._sector_idx[0]
    t_analytic = (90.0 - math.sqrt(90.0 ** 2 - 4.0 * 1000.0)) / 2.0
    err = abs(float(marks[i1]) - t_analytic)
    check(err < 0.005,
          f"preproc: cruce Hermite exacto en frenada ({err * 1000:.1f} ms)")

    # perfiles de curva: 25 vueltas con fases de muestreo distintas; la
    # Vmin real (40 m/s) se reconstruye aunque ningún tick pise el apex
    R, S = 100.0, 800.0
    arc = math.pi * R
    L = 2 * S + 2 * arc
    d_pts = np.arange(0.0, L, 5.0)
    xs_p, ys_p = [], []
    for d in d_pts:
        if d < S:
            xs_p.append(d); ys_p.append(0.0)
        elif d < S + arc:
            a = (d - S) / R
            xs_p.append(S + R * math.sin(a)); ys_p.append(R - R * math.cos(a))
        elif d < 2 * S + arc:
            xs_p.append(S - (d - S - arc)); ys_p.append(2 * R)
        else:
            a = (d - 2 * S - arc) / R
            xs_p.append(-R * math.sin(a)); ys_p.append(R + R * math.cos(a))
    hub_p = _DH()
    hub_p.on_track_length(L)
    hub_p.on_outline((np.array(xs_p), np.array(ys_p)))
    hub_p.on_corners([("T1", S + arc / 2, 0.0, 0.0),
                      ("T2", 2 * S + 1.5 * arc, 0.0, 0.0)])
    hub_p.on_session_meta({"meeting": "Profile GP", "year": 2098,
                           "type": "Race", "name": "Race"})
    apex1 = S + arc / 2.0

    def v_true(d):
        # valle triangular: 40 m/s en el apex, +0.5 m/s por metro
        return min(65.0, 40.0 + 0.5 * abs(d - apex1)) \
            if S <= d <= S + arc else 65.0

    t_cur = 0.0
    batches = []
    for lap in range(1, 27):
        d = (lap * 7.3) % 17.0   # fase de muestreo distinta por vuelta
        while d < L:
            v = v_true(d)
            batches.append(_S("1", t_cur, lap, d, (lap - 1) * L + d,
                              v * 3.6, 80.0, 0.0, 0.0, 6, 0))
            t_cur += 17.0 / v
            d += 17.0
    hub_p.on_batch(batches)
    eng_p = AnalysisEngine(hub_p)
    eng_p.profiles.min_passes = 20
    zs_p = eng_p.zones()
    zi1 = next(i for i, z in enumerate(zs_p) if z.label == "T1")
    laps_done = eng_p.completed_laps("1")
    raw_vmin = {}
    for lap in laps_done:
        m = eng_p.lap_metrics("1", lap)   # entrena y guarda el crudo
        if m is not None and zi1 in m.corners:
            raw_vmin[lap] = m.corners[zi1][0]
    check(eng_p.profiles.ready(zi1),
          f"preproc: modelo entrenado ({eng_p.profiles.zones[zi1]['passes']}"
          " pasadas)")
    lap_bad, worst = max(raw_vmin.items(), key=lambda kv: kv[1])
    check(worst > 41.5,
          f"preproc: el submuestreo sobreestima la Vmin ({worst:.1f})")
    eng_p.set_refine(True)
    eng_p.zones()  # aplica la versión del modelo
    m_ref = eng_p.lap_metrics("1", lap_bad)
    v_ref = m_ref.corners[zi1][0]
    check(v_ref < worst - 0.8 and 39.0 <= v_ref <= 41.5,
          f"preproc: Vmin reconstruida ({worst:.1f} → {v_ref:.1f})")
    # pasada atípica (distorsión asimétrica): el modelo NO la corrige
    prof = eng_p.profiles.profile(zi1)
    centers, mean = prof
    weird = mean * np.where(centers < apex1, 1.3, 0.7)
    check(eng_p.profiles.refine_vmin(zi1, centers, weird) is None,
          "preproc: pasada atípica conserva el dato crudo")
    # persistencia: guardar y adoptar en un motor nuevo
    eng_p.save_profiles()
    eng_p2 = AnalysisEngine(hub_p)
    eng_p2.profiles.min_passes = 20
    eng_p2.zones()
    check(eng_p2.profiles.ready(zi1),
          "preproc: entrenamiento persistido y adoptado")


def test_quali_tower() -> None:
    """Torre en clasificación: tandas Q1-Q3 detectadas por banderas, best
    por tanda (con reset), bloques de eliminados inviolables, drop zone y
    línea de corte. 8 autos, corta 2 por tanda."""
    from f1telem.hub import DataHub as _DH
    from f1telem.models import Sample as _S
    from f1telem.ui.tower import TimingTower as _TT, quali_drops

    # formato por tamaño de grilla: Q3 SIEMPRE con 10 autos
    check(quali_drops(22) == (6, 6), "quali: formato 2026 (22 → 6+6)")
    check(quali_drops(20) == (5, 5), "quali: formato clásico (20 → 5+5)")
    check(quali_drops(21) == (5, 6), "quali: grilla impar reparte (5+6)")
    check(quali_drops(8) == (2, 2), "quali: grilla chica proporcional")

    hub_q = _DH()
    hub_q.on_track_length(1000.0)
    hub_q.on_session_meta({"type": "Qualifying", "name": "Qualifying",
                           "meeting": "Testland GP", "year": 2099})
    # historia completa por adelantado (replay): el hub filtra por timeline
    hub_q.on_race_control([
        {"t": 5.0, "message": "GREEN LIGHT - PIT EXIT OPEN"},
        {"t": 290.0, "message": "CHEQUERED FLAG"},
        {"t": 360.0, "message": "GREEN LIGHT - PIT EXIT OPEN"},
        {"t": 690.0, "message": "CHEQUERED FLAG"},
        {"t": 760.0, "message": "GREEN LIGHT - PIT EXIT OPEN"},
    ])
    state: dict = {}

    def run_laps(drv, start_t, lap_times):
        st = state.setdefault(drv, {"t": start_t, "lap": 1})
        st["t"] = max(st["t"], start_t)
        samples = []
        for T in lap_times:
            for k in range(20):
                frac = k / 20.0
                samples.append(_S(drv, st["t"] + T * frac, st["lap"],
                                  1000.0 * frac,
                                  (st["lap"] - 1) * 1000.0 + 1000.0 * frac,
                                  1000.0 / T * 3.6, 50.0, 0.0, 0.0, 5, 0))
            st["t"] += T
            st["lap"] += 1
        # vuelta lanzada y nunca cerrada: las previas cuentan como cerradas
        for k in range(3):
            samples.append(_S(drv, st["t"] + k * 0.7, st["lap"],
                              4.0 * k, (st["lap"] - 1) * 1000.0 + 4.0 * k,
                              20.0, 10.0, 0.0, 0.0, 2, 0))
        st["t"] += 2.5
        st["lap"] += 1
        hub_q.on_batch(samples)

    # Q1: bests 101..106 para "1".."6"; "7"=118 y "8"=119 quedan afuera
    for i in range(1, 7):
        run_laps(str(i), 10.0 + i, [110.0, 100.0 + i])
    run_laps("7", 16.0, [120.0, 118.0])
    run_laps("8", 17.0, [121.0, 119.0])
    tw = _TT(hub_q)
    tw.refresh()
    order1 = [r.drv for r in tw.rows]
    check(order1 == ["1", "2", "3", "4", "5", "6", "7", "8"],
          f"quali: Q1 ordena por vuelta rápida de la tanda ({order1})")
    check(tw.quali_cut_row == 6 and tw.rows[6].drop and tw.rows[7].drop,
          "quali: drop zone con los 2 que hoy quedan afuera")
    check(tw.rows[6].cut_txt.startswith("CUT +"),
          f"quali: cuánto necesita para salvarse ({tw.rows[6].cut_txt})")
    check("Q1" in tw.lap_label.text() and "top 6" in tw.lap_label.text(),
          f"quali: header de tanda ({tw.lap_label.text()})")
    check(abs(tw.rows[0].best - 101.0) < 0.5,
          f"quali: best de Q1 ({tw.rows[0].best:.1f})")

    # Q2: corren "1".."6"; "6" clava 130s — igual queda ARRIBA de los
    # eliminados de Q1 aunque ellos tengan tiempos viejos más rápidos
    for i in range(1, 6):
        run_laps(str(i), 365.0 + i, [92.0 + i, 95.0 + i])
    run_laps("6", 372.0, [130.0, 131.0])
    tw.refresh()
    order2 = [r.drv for r in tw.rows]
    check(order2[:6] == ["1", "2", "3", "4", "5", "6"]
          and order2[6:] == ["7", "8"],
          f"quali: eliminado nunca supera a uno vivo ({order2})")
    check(tw.rows[6].out_tag == "OUT Q1" and tw.rows[7].out_tag == "OUT Q1",
          "quali: tags OUT Q1 en el bloque eliminado")
    check(tw.quali_seps == [(6, "ELIMINATED Q1")],
          f"quali: separador del bloque ({tw.quali_seps})")
    check(abs(tw.rows[0].best - 93.0) < 0.5,
          f"quali: el best se resetea en Q2 ({tw.rows[0].best:.1f})")
    check(tw.quali_cut_row == 4 and tw.rows[4].drop and tw.rows[5].drop,
          "quali: drop zone de Q2 (P5 y P6)")

    # Q3: corren "1".."4"; clasificación final por bloques
    for i in range(1, 5):
        run_laps(str(i), 765.0 + i, [80.0 + i, 82.0 + i])
    tw.refresh()
    order3 = [r.drv for r in tw.rows]
    check(order3 == ["1", "2", "3", "4", "5", "6", "7", "8"],
          f"quali: clasificación final por bloques ({order3})")
    check(tw.rows[4].out_tag == "OUT Q2" and tw.rows[6].out_tag == "OUT Q1",
          "quali: tags OUT Q2 y OUT Q1")
    check([s for s in tw.quali_seps]
          == [(4, "ELIMINATED Q2"), (6, "ELIMINATED Q1")],
          f"quali: dos separadores rotulados ({tw.quali_seps})")
    check(tw.quali_cut_row is None, "quali: sin drop zone en Q3")
    check(abs(tw.rows[0].best - 81.0) < 0.5
          and abs(tw.rows[4].best - 97.0) < 0.5,
          f"quali: bests por tanda (Q3 {tw.rows[0].best:.1f}, "
          f"Q2 congelado {tw.rows[4].best:.1f})")
    check(tw.rows[1].gap_txt.startswith("+1.0")
          and tw.rows[4].gap_txt == "—",
          f"quali: gaps por delta de tiempos ({tw.rows[1].gap_txt})")
    check(tw.rows[0].pos == 1 and tw.rows[7].pos == 8,
          "quali: numeración de clasificación")
    # métrica elegida: ordena DENTRO de cada bloque (bloques inviolables)
    tw.sort_combo.setCurrentIndex(tw.sort_combo.findData("s1"))
    check(set(r.drv for r in tw.rows[:4]) == {"1", "2", "3", "4"}
          and tw.rows[6].out_tag == "OUT Q1",
          "quali: métrica respeta los bloques")
    tw.sort_combo.setCurrentIndex(tw.sort_combo.findData("position"))
    # seek atrás: el timeline al final de Q1 re-arma la tanda 1 (a t=260
    # todas las vueltas de Q1 ya cerraron y la cuadros aún no cayó)
    hub_q.latest_t = 260.0
    tw.refresh()
    check("Q1" in tw.lap_label.text()
          and abs(tw.rows[0].best - 101.0) < 0.5,
          f"quali: seek atrás vuelve a Q1 ({tw.lap_label.text()})")
    # el pintado con separadores/drop no debe crashear
    tw.canvas.resize(600, 400)
    tw.canvas.grab()


def test_app_demo(app: QApplication) -> None:
    win = MainWindow()
    # sin popups durante el smoke: el log del gestor alcanza para verificar
    win.cfg.setdefault("notifications", {})["popups"] = False
    win.show()
    pump(app, 0.3)

    # modelo todo-ventanas: arranque limpio (solo el hub, ninguna ventana)
    open_now = sorted(pid for pid, p in win._panels.items()
                      if getattr(p, "window_only", False) and p.is_panel_visible())
    check(open_now == [], f"arranque limpio: sin ventanas abiertas ({open_now})")
    check(len(win._catalog_checks) == len(win.PANEL_CATALOG)
          and all(win._catalog_checks[p].isChecked() == win._panels[p].is_panel_visible()
                  for p in win._catalog_checks),
          "catálogo del hub refleja el estado de cada ventana")
    # abrir el set de trabajo desde el catálogo (botones conmutables)
    for pid in ("race_chart", "tower", "map", "session"):
        win._catalog_checks[pid].setChecked(True)
    pump(app, 0.3)
    check(all(win._panels[p].is_panel_visible()
              for p in ("race_chart", "tower", "map", "session")),
          "catálogo abre las ventanas elegidas")

    # conectar fuente demo a x25
    win.source_combo.setCurrentIndex(win.source_combo.findData("demo"))
    win.speed_combo.setCurrentIndex(4)  # x25
    win.connect_btn.click()
    pump(app, 3.0)

    check(len(win.hub.drivers) == 6, f"demo publica 6 pilotos ({len(win.hub.drivers)})")
    check(win.driver_list.count() == 6, "lista de pilotos poblada")

    # seleccionar 4 pilotos
    for i in range(4):
        win.driver_list.item(i).setCheckState(Qt.Checked)
    pump(app, 6.0)
    check(win.hub.total_samples > 500, f"llegan muestras ({win.hub.total_samples})")

    # ventana Race chart (abierta por defecto)
    pump(app, 1.0)
    curve = next(iter(win.chart_rolling.curves.values()))
    x, y = curve.getData()
    check(x is not None and len(x) > 50, f"Carrera: curva con datos ({0 if x is None else len(x)})")
    vb_range = win.chart_rolling.getViewBox().viewRange()[0]
    width = vb_range[1] - vb_range[0]
    expected = win.hub.track_length * (1 + win.chart_rolling.RIGHT_MARGIN_FRAC)
    check(abs(width - expected) < win.hub.track_length * 0.02,
          f"Carrera: ventana X = 1 vuelta + margen derecho ({width:.0f} m)")
    last_xs = [c.getData()[0][-1] for c in win.chart_rolling.curves.values()
               if c.getData()[0] is not None and len(c.getData()[0])]
    gap_right = vb_range[1] - max(last_xs)
    check(gap_right > win.hub.track_length * 0.05,
          f"Carrera: espacio a la derecha para etiquetas ({gap_right:.0f} m)")

    # alineación en X: el perfil velocidad-vs-posición de dos autos debe estar
    # correlacionado (misma curva del circuito en la misma vertical)
    import numpy as np
    sel = win._selected_drivers()
    xa, ya = win.chart_rolling.curves[sel[0]].getData()
    xb, yb = win.chart_rolling.curves[sel[1]].getData()
    grid = np.linspace(max(xa[0], xb[0]), min(xa[-1], xb[-1]), 500)
    r = float(np.corrcoef(np.interp(grid, xa, ya), np.interp(grid, xb, yb))[0, 1])
    check(r > 0.9, f"Carrera: perfiles alineados en X entre autos (r={r:.3f})")

    # selector local 👥 de cada gráfico: nace del panel Drivers, se retoca
    # por ventana sin afectar al resto, y Drivers lo pisa al cambiar
    btn_rc = win._chart_sel_btns[0]
    check(btn_rc.selection() == sel,
          "gráficos: selector local arranca igual al panel Drivers")
    extra = next(win.driver_list.item(i).data(Qt.UserRole)
                 for i in range(win.driver_list.count())
                 if win.driver_list.item(i).checkState() != Qt.Checked)
    item_x = next(btn_rc.list.item(i) for i in range(btn_rc.list.count())
                  if btn_rc.list.item(i).data(Qt.UserRole) == extra)
    item_x.setCheckState(Qt.Checked)
    check(set(win.chart_rolling.curves) == set(sel) | {extra},
          f"gráficos: retoque local suma el auto en esa ventana "
          f"({len(win.chart_rolling.curves)})")
    check(win._chart_sel_btns[1].selection() == sel,
          "gráficos: el retoque no toca a los otros gráficos")
    check(win._selected_drivers() == sel,
          "gráficos: el retoque no toca al panel Drivers")
    idx_extra = next(i for i in range(win.driver_list.count())
                     if win.driver_list.item(i).data(Qt.UserRole) == extra)
    win.driver_list.item(idx_extra).setCheckState(Qt.Checked)
    sel5 = win._selected_drivers()
    check(btn_rc.selection() == sel5
          and set(win.chart_rolling.curves) == set(sel5),
          "gráficos: el panel Drivers pisa la selección local")
    win.driver_list.item(idx_extra).setCheckState(Qt.Unchecked)
    check(btn_rc.selection() == sel and win._selected_drivers() == sel,
          "gráficos: estado restaurado tras el cambio en Drivers")

    # ventana Race 2 (wrap): se abre desde el catálogo del hub
    win._catalog_checks["race2_chart"].setChecked(True)
    pump(app, 4.0)
    check(win._panels["race2_chart"].is_panel_visible(),
          "catálogo abre la ventana Race 2")
    curve = next(iter(win.chart_wrap.curves.values()))
    x, y = curve.getData()
    filled = 0 if y is None else int(np.isfinite(y).sum())
    check(filled > 100, f"Carrera 2: bins rellenados ({filled})")
    has_gap = y is not None and np.isnan(y).any()
    check(has_gap, "Carrera 2: hueco delante del cabezal (efecto 'comer')")

    # esperar a que haya vueltas cerradas para la referencia de qualy
    deadline = time.monotonic() + 30
    first = win._selected_drivers()[0]
    while time.monotonic() < deadline:
        pump(app, 0.5)
        if win.hub.buffers.get(first) and win.hub.buffers[first].completed_laps():
            break
    laps = win.hub.buffers[first].completed_laps()
    check(bool(laps), f"hay vueltas cerradas para referencia ({laps})")

    # ventana Qualy con referencia (el selector vive dentro de la vista)
    win._catalog_checks["quali_view"].setChecked(True)
    pump(app, 0.5)
    qv = win.chart_qualy
    qv.ref_driver_combo.setCurrentIndex(qv.ref_driver_combo.findData(first))
    qv._refresh_ref_laps()
    check(qv.ref_lap_combo.count() > 0, "combo de vueltas de referencia poblado")
    ref_text = qv.ref_lap_combo.itemText(0)
    check(ref_text.startswith("Lap") and ":" in ref_text,
          f"referencia muestra tiempo de vuelta ({ref_text!r})")
    # cambiar el Target debe reescribir los tiempos del combo de vueltas
    # aunque los números de vuelta coincidan
    def _other_with_laps():
        for i in range(qv.ref_driver_combo.count()):
            d = qv.ref_driver_combo.itemData(i)
            if (d != first and win.hub.buffers.get(d)
                    and win.hub.buffers[d].completed_laps()):
                return i
        return None
    deadline = time.monotonic() + 20
    other_idx = _other_with_laps()
    while time.monotonic() < deadline and other_idx is None:
        pump(app, 0.3)
        other_idx = _other_with_laps()
    ref_text = qv.ref_lap_combo.itemText(0)  # re-leer tras la espera
    qv.ref_driver_combo.setCurrentIndex(other_idx)
    other_text = qv.ref_lap_combo.itemText(0)
    check(qv.ref_lap_combo.count() > 0 and other_text != ref_text,
          f"cambiar Target actualiza los tiempos ({ref_text!r} -> {other_text!r})")
    qv.ref_driver_combo.setCurrentIndex(qv.ref_driver_combo.findData(first))
    check(qv.ref_lap_combo.itemText(0) == ref_text,
          "volver al Target original restaura sus tiempos")
    qv.ref_set_btn.click()
    pump(app, 1.0)
    rx, ry = qv.chart._ref_curve.getData()
    check(rx is not None and len(rx) > 50, f"Qualy: target dibujada ({0 if rx is None else len(rx)})")
    # recién cruzada la meta la vuelta en curso puede estar vacía unos ms
    # (cursor de reproducción): esperar a que aparezca
    deadline = time.monotonic() + 8
    lx, ly = qv.chart.curves[first].getData()
    while time.monotonic() < deadline and (lx is None or len(lx) == 0):
        pump(app, 0.3)
        lx, ly = qv.chart.curves[first].getData()
    check(lx is not None and len(lx) > 0, "Qualy: vuelta actual en vivo dibujada")
    check(qv.caption.text().startswith("Target:") and ":" in qv.caption.text(),
          f"Qualy: caption con la target ({qv.caption.text()[:40]!r})")
    # esperar a mitad de vuelta para que haya marcas cruzadas y delta con datos
    buf_q = win.hub.buffers[first]
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        pump(app, 0.3)
        dlq = float(buf_q.col("dist_lap")[-1])
        if win.hub.track_length * 0.35 < dlq < win.hub.track_length * 0.85:
            break
    dx, dy = qv.delta_curves[first].getData()
    check(dx is not None and len(dx) > 10 and bool(np.isfinite(dy).all()),
          f"Qualy: traza de delta vs target ({0 if dx is None else len(dx)} pts)")
    check(abs(float(dy[-1])) < 30, f"Qualy: delta acumulado acotado ({float(dy[-1]):+.2f} s)")
    qv._update_cards()
    check(len(qv.cards) == min(4, len(win._selected_drivers())),
          f"Qualy: tarjetas por piloto, máx 4 ({len(qv.cards)})")
    check(not qv.more_note.isVisible(), "Qualy: sin aviso con 4 pilotos")
    card = qv.cards[first]
    # alineación: cada fila = chip del sector + sus 8 microsectores
    r_s2 = card.grid.getItemPosition(card.grid.indexOf(card.sectors[1]))[:2]
    r_m9 = card.grid.getItemPosition(card.grid.indexOf(card.micros[8]))[:2]
    r_m16 = card.grid.getItemPosition(card.grid.indexOf(card.micros[15]))[:2]
    check(r_s2 == (1, 0) and r_m9 == (1, 1) and r_m16 == (1, 8),
          f"Qualy: µ9-µ16 alineados con S2 ({r_s2}, {r_m9}, {r_m16})")
    check(math.isfinite(card.last_delta),
          f"Qualy: delta total de vuelta en tarjeta ({card.last_delta:+.2f} s)")
    check(math.isfinite(card.sector_deltas[0]),
          f"Qualy: delta de S1 en tarjeta ({card.sector_deltas[0]:+.2f} s)")
    check(card.micro_filled >= 5,
          f"Qualy: microsectores poblados sin scroll ({card.micro_filled})")
    check("S:" in qv.caption.text(), "Qualy: caption con sectores de la target")

    # ventana Tiempos / Gap
    win._catalog_checks["times_gap"].setChecked(True)
    pump(app, 1.5)
    tv = win.chart_timing
    ref = tv.ref_combo.currentData()
    other = next(d for d in sel if d != ref)
    gx, gy = tv.curves[other].getData()
    check(gx is not None and len(gx) > 50 and bool(np.isfinite(gy).all()),
          f"Gap: serie con datos finitos ({0 if gx is None else len(gx)})")
    check(abs(float(gy[-1])) < 120, f"Gap: magnitud razonable ({float(gy[-1]):+.2f} s)")
    # las tablas se refrescan a 2 Hz sobre la pestaña ACTIVA (viven en el
    # panel Data tables): activar Summary y esperar a que se pueble
    win.data_table_view.tabs.setCurrentWidget(tv.summary_table)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if (tv.summary_table.rowCount() == len(sel)
                and tv.summary_table.item(0, 2) is not None):
            break
        pump(app, 0.3)
    cell = tv.summary_table.item(0, 2)
    check(tv.summary_table.rowCount() == len(sel)
          and cell is not None and ":" in cell.text(),
          f"Resumen: tiempos de vuelta poblados ({'—' if cell is None else cell.text()})")
    tvref = tv.ref_combo.currentData() or sel[0]
    tv._update_laps_table()  # las pestañas no visibles se actualizan al verlas
    tv._update_micro(tvref)
    check(tv.laps_table.rowCount() >= 1 and tv.laps_table.columnCount() == len(sel),
          f"Por vuelta: {tv.laps_table.rowCount()} vueltas × {tv.laps_table.columnCount()} pilotos")
    check(tv.micro_table.rowCount() == len(sel) and tv.micro_table.columnCount() == 24,
          "Microsectores: tabla poblada")
    an = tv.analyzer
    ref_lap = an.last_completed_lap(ref)
    lt = an.lap_time(ref, ref_lap)
    st = sum(an.sector_times(ref, ref_lap))
    check(math.isfinite(lt) and 60 < lt < 200, f"tiempo de vuelta plausible ({lt:.2f} s)")
    check(abs(lt - st) < 0.05, f"S1+S2+S3 = vuelta ({st:.2f} vs {lt:.2f})")
    mt = an.micro_times(ref, ref_lap)
    check(mt is not None and bool(np.isfinite(mt).all()) and abs(float(mt.sum()) - lt) < 0.05,
          "µsectores suman la vuelta")

    # orden por posición en pista en las 3 tablas + marcador de último µsector
    tv.refresh()  # actualiza curvas/leyenda/líneas; sin pump el estado no cambia
    ref0 = tv.ref_combo.currentData() or sel[0]
    tv._update_summary(ref0)
    tv._update_laps_table()
    tv._update_micro(ref0)
    ordered = tv._by_track_position()
    codes = [tv._code_of(d) for d in ordered]
    got_sum = [tv.summary_table.item(r, 0).text() for r in range(tv.summary_table.rowCount())]
    check(got_sum == codes, f"Resumen ordenado por posición en pista ({got_sum})")
    got_laps = [tv.laps_table.horizontalHeaderItem(c).text()
                for c in range(tv.laps_table.columnCount())]
    check(got_laps == codes, f"Por vuelta ordenado por posición ({got_laps})")
    got_micro = [tv.micro_table.verticalHeaderItem(r).text().split()[0]
                 for r in range(tv.micro_table.rowCount())]
    check(got_micro == codes, f"µsectores ordenado por posición ({got_micro})")
    drv0 = ordered[0]
    lm0 = tv.analyzer.latest_micro_times(drv0)
    cur0 = win.hub.buffers[drv0].current_lap()
    from_cur = np.nonzero(lm0[1] == cur0)[0]
    exp_idx = int(from_cur.max()) if len(from_cur) else 23
    marked = tv.micro_table.item(0, exp_idx)
    check(marked is not None and marked.font().underline(),
          f"último µsector marcado (µ{exp_idx + 1} de {got_micro[0]})")
    check(len(tv._lap_lines) >= 1, f"líneas de corte de vuelta ({len(tv._lap_lines)})")
    legend_texts = [lbl.text for _s, lbl in tv.legend.items]
    check(any(t.endswith("(ref)") for t in legend_texts),
          f"leyenda marca la referencia ({legend_texts})")

    # ventana X configurable (en vueltas) del gap — combo propio de la vista
    L = win.hub.track_length
    win.gap_window_combo.setCurrentIndex(win.gap_window_combo.findData(1.0))
    pump(app, 0.5)
    xr = tv.plot.getViewBox().viewRange()[0]
    width = xr[1] - xr[0]
    check(abs(width - L) < L * 0.02, f"Gap: ventana X de 1 vuelta aplicada ({width:.0f} m)")
    gx2, _ = tv.curves[other].getData()
    # el borde suavizado extrapola entre lotes (mucho a x25, y con todas las
    # ventanas abiertas el recálculo escalonado agrega hasta ~1 s de datos)
    check(abs(xr[1] - float(gx2[-1])) < 1800.0,
          f"Gap: la ventana termina en la posición actual "
          f"(borde a {abs(xr[1] - float(gx2[-1])):.0f} m)")
    win.gap_window_combo.setCurrentIndex(win.gap_window_combo.findData(0.0))
    pump(app, 0.5)
    xr = tv.plot.getViewBox().viewRange()[0]
    check(xr[1] - xr[0] > L * 1.5, f"Gap: 'Todo' vuelve al rango completo ({xr[1] - xr[0]:.0f} m)")
    # ticks del eje X con formato "V<vuelta> +<metros>"
    axis = tv.plot.getAxis("bottom")
    ticks = axis.tickStrings([L * 2.5], 1.0, 1000.0)
    check("L3" in ticks[0] and "m" in ticks[0], f"Gap: eje muestra vuelta+metros ({ticks[0]!r})")

    # sectores/µsectores rodantes: esperar a que el auto esté a mitad de vuelta
    buf = win.hub.buffers[ref]
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        pump(app, 0.3)
        dl = float(buf.col("dist_lap")[-1])
        if win.hub.track_length * 0.3 < dl < win.hub.track_length * 0.85:
            break
    lm = an.latest_micro_times(ref)
    cur = buf.current_lap()
    check(lm is not None and bool(np.isfinite(lm[0]).all()),
          "µsectores rodantes: 24 valores completos en tiempo real")
    laps_used = {int(v) for v in lm[1]}
    check(laps_used == {cur, cur - 1},
          f"µsectores rodantes: mezcla vuelta en curso y -1 vuelta ({laps_used}, V{cur})")
    ls = an.latest_sector_times(ref)
    check(ls is not None and bool(np.isfinite(ls[0]).all()), "sectores rodantes completos")
    pump(app, 1.5)
    tv._update_micro(tv.ref_combo.currentData() or sel[0])
    check(tv.micro_table.item(0, 0) is not None and tv.micro_table.item(0, 0).text() != "—",
          "tabla de µsectores poblada en vivo")

    # pestaña Curvas: velocidad mínima por curva real
    check(len(win.hub.corners) == 8, f"curvas del circuito publicadas ({len(win.hub.corners)})")
    tv._update_corners(ref0)
    check(tv.corners_table.columnCount() == 8, "tabla de curvas: una columna por curva")
    corner_vals = [tv.corners_table.item(0, c).text() for c in range(8)]
    finite_vals = [v for v in corner_vals if v != "—"]
    check(len(finite_vals) >= 6, f"velocidades mínimas por curva ({finite_vals[:4]})")
    check(all(40 <= float(v) <= 340 for v in finite_vals), "mínimas por curva plausibles")

    # torre de tiempos (estilo broadcast: filas pintadas, modelo en .rows)
    check(win.tower.isVisible(), "torre de tiempos visible")
    win.tower.refresh()
    rows = win.tower.rows
    check(len(rows) == 6, f"torre: todos los autos ({len(rows)})")
    check(rows[0].gap_txt == "leader", "torre: fila 1 = líder")
    g2, i2 = rows[1].gap_txt, rows[1].int_txt
    check(g2.startswith("+") and i2.startswith("+"), f"torre: gap e intervalo ({g2}, {i2})")
    check(math.isfinite(rows[0].last) and math.isfinite(rows[0].best)
          and rows[0].last_kind > 0,
          f"torre: última y mejor vuelta ({rows[0].last:.3f}, {rows[0].best:.3f})")
    check(rows[0].pits == 1, f"torre: contador de paradas ({rows[0].pits})")
    a5 = win.tower._avg_lap(rows[0].drv, 5)
    a10 = win.tower._avg_lap(rows[0].drv, 10)
    check(math.isfinite(a5) and math.isfinite(a10),
          f"torre: AVG5/AVG10 ({a5:.3f}, {a10:.3f})")
    check(all(len(r.segs) == 3 and len(r.sectors) == 3 for r in rows),
          "torre: rayitas y sectores por fila")
    check(rows[0].speed > 0 and not hasattr(rows[0], "gear"),
          f"torre: velocidad sin gear/rpm/drs ({rows[0].speed:.0f} km/h)")
    check(any(k > 0 for r in rows for g in r.segs for k in g),
          "torre: microsectores calculados con estado")
    check(win.tower.lap_label.text().startswith("LAP"),
          f"torre: encabezado con vuelta ({win.tower.lap_label.text()!r})")

    # pits, banderas y degradación
    check(len(win.hub.pits) == 6, f"pits publicados ({len(win.hub.pits)})")
    check(len(win.hub.track_status) == 2, f"períodos de bandera ({len(win.hub.track_status)})")
    check(len(win.lap_ruler._pits) == 6 and len(win.lap_ruler._status) == 2,
          "línea de tiempo con rombos de pits y bandas de bandera")
    tv._update_laps_table()
    row4 = [tv.laps_table.item(3, c) for c in range(tv.laps_table.columnCount())]
    check(any(it is not None and it.text().endswith(" P") for it in row4),
          "tabla Por vuelta: vuelta 4 marcada con P (parada)")
    comp_cells = sum(
        1 for r in range(tv.laps_table.rowCount()) for c in range(tv.laps_table.columnCount())
        if tv.laps_table.item(r, c) is not None and tv.laps_table.item(r, c).background().color().alpha() > 0
    )
    check(comp_cells >= 8, f"tabla Por vuelta: celdas teñidas por compuesto ({comp_cells})")
    tv._update_status_regions(ref0)
    check(len(tv._status_items) >= 1, f"bandas de bandera en el gap ({len(tv._status_items)})")

    # clima: barra de estado + lluvia en la línea de tiempo
    check(len(win.hub.weather) == 3, f"clima publicado ({len(win.hub.weather)})")
    check("Track" in win.meta_label.text(), f"clima en barra de estado ({win.meta_label.text()[:40]})")
    check(len(win.lap_ruler._rain) == 1, "franja de lluvia en la línea de tiempo")

    # amarilla por sector pintada en el mapa
    tmap = win.track_map
    win.hub.on_sector_yellows([(0.0, float("inf"), 2600.0, 3200.0)])
    tmap._yellow_sig = None
    tmap.refresh()
    yx, yy = tmap.yellow_curve.getData()
    finite = np.isfinite(yy) if yy is not None else np.array([])
    check(yx is not None and 10 < int(finite.sum()) < len(yx),
          f"mapa: tramo amarillo pintado ({0 if yx is None else int(finite.sum())} pts)")
    mapping = tmap._ensure_dist_map()
    dist_painted = mapping[0][finite]
    check(float(dist_painted.min()) >= 2550 and float(dist_painted.max()) <= 3250,
          f"mapa: amarillo en el sector correcto ({dist_painted.min():.0f}-{dist_painted.max():.0f} m)")
    win.hub.on_sector_yellows([])
    tmap._yellow_sig = None
    tmap.refresh()

    # selector de sesión navegable
    win.gp_combo.setEditText("Bahrain")
    check(win._selected_gp() == "Bahrain", "GP tipeado a mano se respeta")
    win._on_schedule(win.year_spin.value(),
                     [(1, "Bahrain Grand Prix"), (2, "Saudi Arabian Grand Prix")])
    check(win.gp_combo.count() == 2, f"calendario poblado ({win.gp_combo.count()})")
    win.gp_combo.setCurrentIndex(1)
    check(win._selected_gp() == "Saudi Arabian Grand Prix",
          f"evento elegido del calendario ({win._selected_gp()})")
    win.gp_combo.setEditText("Bahrain")
    check(win._panels["tower"].window_only
          and win._panels["tower"].content is win.tower
          and win._panels["map"].content is win.track_map,
          "torre y mapa en ventanas propias (todo-ventanas)")

    # tooltip crosshair: valores de todas las series en el punto del mouse
    from PySide6.QtCore import QPointF
    pump(app, 0.5)
    vb = win.chart_rolling.getViewBox()
    (x0, x1), (y0, y1) = vb.viewRange()
    probe = win.chart_rolling._probe
    probe._on_move(vb.mapViewToScene(QPointF((x0 + x1) / 2, (y0 + y1) / 2)))
    check(probe.label.isVisible() and len(probe.rows) >= 2,
          f"tooltip Carrera: {len(probe.rows)} series en el punto ({probe.rows[:2]})")
    codes = {r[0] for r in probe.rows}
    check("VER" in codes or "NOR" in codes, f"tooltip identifica pilotos ({codes})")
    probe._on_move(vb.mapViewToScene(QPointF(x1 + (x1 - x0), y1)))  # fuera de rango de datos
    pump(app, 0.5)
    vbg = tv.plot.getViewBox()
    (gx0, gx1), (gy0, gy1) = vbg.viewRange()
    tv._probe._on_move(vbg.mapViewToScene(QPointF((gx0 + gx1) / 2, (gy0 + gy1) / 2)))
    check(tv._probe.label.isVisible() and len(tv._probe.rows) >= 2,
          f"tooltip Gap: {len(tv._probe.rows)} series en el punto")

    # doble click sobre una línea la oculta; en zona vacía las restaura
    pump(app, 0.5)
    # sin pump entre medio: la vista deslizante queda congelada para el test
    target = sel[1]
    hider = win.chart_rolling.hider
    xd, yd = win.chart_rolling.curves[target].getData()
    (x0, x1), (y0, y1) = vb.viewRange()
    j = min(max(int(np.searchsorted(xd, (x0 + x1) / 2)), 0), len(xd) - 1)
    on_curve = vb.mapViewToScene(QPointF(float(xd[j]), float(yd[j])))
    key = hider._nearest(on_curve)
    check(key in sel, f"doble click detecta la serie más cercana ({key})")
    hider.handle_double_click(on_curve)
    hidden_visible = key is not None and win.chart_rolling.curves[key].isVisible()
    check(key is not None and not hidden_visible, "doble click oculta la serie")
    probe._on_move(on_curve)
    codes_now = {r[0] for r in probe.rows}
    check(key is None or win.chart_rolling._code_of(key) not in codes_now,
          f"tooltip omite la serie oculta ({codes_now})")
    restored = False
    for frac in (0.98, 0.02, 0.6, 0.35):
        cand = vb.mapViewToScene(QPointF(float(xd[j]), y0 + (y1 - y0) * frac))
        if hider._nearest(cand) is None:
            hider.handle_double_click(cand)
            restored = True
            break
    check(restored and key is not None and win.chart_rolling.curves[key].isVisible(),
          "doble click en zona vacía restaura las series ocultas")

    # lista de pilotos en orden alfabético
    texts = [win.driver_list.item(i).text() for i in range(win.driver_list.count())]
    check(texts == sorted(texts, key=str.upper), f"lista alfabética ({[t[:3] for t in texts]})")

    # mapa del circuito
    mp = win.track_map
    check(mp.isVisible(), "mapa visible por defecto")
    check(mp.width() >= 280, f"mapa con ancho útil ({mp.width()} px)")
    check(len(mp._corner_items) == 8, f"mapa: curvas etiquetadas ({len(mp._corner_items)})")

    # estelas: 5 s, con degradado y toggle
    trail0 = mp.trails[win._selected_drivers()[0]]
    tx0, _ty0 = trail0.getData()
    check(tx0 is not None and 2 <= len(tx0) <= 45,
          f"estela corta de ~5 s ({0 if tx0 is None else len(tx0)} pts)")
    pen0 = trail0.opts.get("pen")
    check(pen0 is not None and pen0.brush().gradient() is not None,
          "estela con degradado hacia la cola")
    win.trails_check.setChecked(False)
    pump(app, 0.3)
    tx_off, _ = trail0.getData()
    check(tx_off is None or len(tx_off) == 0, "checkbox apaga las estelas")
    win.trails_check.setChecked(True)
    pump(app, 0.3)
    tx_on, _ = trail0.getData()
    check(tx_on is not None and len(tx_on) >= 2, "checkbox reactiva las estelas")
    ox, oy = mp.outline_curve.getData()
    check(ox is not None and len(ox) > 300, f"mapa: trazado del circuito ({0 if ox is None else len(ox)} pts)")
    pts = mp.dots.points()
    check(len(pts) == win.driver_list.count(),
          f"mapa: todos los autos visibles por defecto ({len(pts)})")
    # filtro 👥 propio de la ventana: ocultar un auto saca su punto, sin
    # tocar la selección de comparación del panel Drivers
    item0 = mp.filter_btn.list.item(0)
    hidden_drv = item0.data(Qt.UserRole)
    item0.setCheckState(Qt.Unchecked)
    pump(app, 0.3)
    check(len(mp.dots.points()) == win.driver_list.count() - 1
          and hidden_drv not in mp.selected,
          f"mapa: filtro 👥 oculta el auto ({len(mp.dots.points())})")
    check(len(win._selected_drivers()) == len(sel),
          "filtro 👥 no toca la selección del panel Drivers")
    check(win.cfg["ui"].get("map_hidden_cars") == [hidden_drv],
          "filtro 👥 persistido por ventana")
    item0.setCheckState(Qt.Checked)
    pump(app, 0.3)
    check(len(mp.dots.points()) == win.driver_list.count(),
          "mapa: filtro 👥 restaurado muestra todo")

    # torre: mismo filtro por ventana; solo saca filas de la vista (las
    # demás conservan su posición y gap reales)
    win.tower.refresh()
    n_rows0 = len(win.tower.rows)
    t_item = win.tower.filter_btn.list.item(0)
    t_drv = t_item.data(Qt.UserRole)
    t_item.setCheckState(Qt.Unchecked)
    check(len(win.tower.rows) == n_rows0 - 1
          and all(r.drv != t_drv for r in win.tower.rows),
          "torre: filtro 👥 saca la fila del auto oculto")
    t_item.setCheckState(Qt.Checked)
    check(len(win.tower.rows) == n_rows0, "torre: filtro 👥 restaurado")

    # ordenamiento: selector de métrica + default según tipo de sesión
    idx_best_t = win.tower.sort_combo.findData("best")
    win.tower.sort_combo.setCurrentIndex(idx_best_t)
    finite_b = [r.best for r in win.tower.rows
                if r.best == r.best and math.isfinite(r.best)]
    check(len(finite_b) >= 3 and finite_b == sorted(finite_b),
          "torre: orden por Best lap ascendente")
    idx_s1_t = win.tower.sort_combo.findData("s1")
    win.tower.sort_combo.setCurrentIndex(idx_s1_t)
    s1_vals = [win.tower._sort_value(r.drv, "s1") for r in win.tower.rows]
    check(s1_vals == sorted(s1_vals), "torre: orden por mejor S1")
    win.tower.sort_combo.setCurrentIndex(
        win.tower.sort_combo.findData("position"))
    check(win.tower.rows[0].gap_txt == "leader",
          "torre: Position en carrera vuelve al orden de pista")
    # en práctica/quali, Position = vuelta rápida por defecto
    win.hub.session_meta["type"] = "Practice"
    win.hub.session_meta["name"] = "Practice 1"
    win.tower.refresh()
    finite_p = [r.best for r in win.tower.rows
                if r.best == r.best and math.isfinite(r.best)]
    check(finite_p == sorted(finite_p),
          "torre: práctica ordena por vuelta rápida por defecto")
    win.hub.session_meta["type"] = "Race"
    win.hub.session_meta["name"] = "Race"
    win.tower.refresh()

    # ↺: los subpaneles internos ocultados con ✕ vuelven de fábrica
    # (las tarjetas de Lap Compare - Live; las tablas ya son panel propio)
    win._panels["quali_view"].set_panel_visible(True)
    pump(app, 0.2)
    qv_win = win._panels["quali_view"]._win
    cards = win._panels["quali_cards"]
    check(qv_win.reset_btn.isVisible(), "ventana con subpaneles muestra ↺")
    cards._hide_docked()
    check(not cards.is_panel_visible(), "subpanel oculto con ✕")
    qv_win.reset_btn.click()
    check(cards.is_panel_visible() and not cards.floating,
          "↺ restaura el subpanel oculto")
    cards.detach()
    pump(app, 0.2)
    qv_win.reset_btn.click()
    check(cards.is_panel_visible() and not cards.floating,
          "↺ re-acopla el subpanel flotado")
    # cada auto debe estar sobre la pista (cerca del trazado) y coherente con
    # su dist_lap (el mapa demo se genera desde la misma distancia de arco)
    from f1telem.sources.demo import track_pos
    worst = 0.0
    for drv in sel:
        pb = win.hub.positions[drv]
        px, py = pb.x[-1], pb.y[-1]
        d_near = min(np.hypot(ox - px, oy - py))
        worst = max(worst, float(d_near))
    check(worst < 30, f"mapa: autos sobre el trazado (peor distancia {worst:.1f} m)")
    drv0 = sel[0]
    exp_x, exp_y = track_pos(float(win.hub.buffers[drv0].col("dist_total")[-1]))
    got_x, got_y = win.hub.positions[drv0].x[-1], win.hub.positions[drv0].y[-1]
    dpos = float(np.hypot(exp_x - got_x, exp_y - got_y))
    check(dpos < 60, f"mapa: posición coherente con la distancia graficada ({dpos:.1f} m)")
    map_panel = win._panels["map"]
    map_panel.set_panel_visible(False)
    check(not mp.isVisible(), "mapa se oculta desde el catálogo")
    check(not win._catalog_checks["map"].isChecked(),
          "checkbox del catálogo refleja el cierre")
    map_panel.set_panel_visible(True)
    check(mp.isVisible(), "mapa se vuelve a mostrar")
    check(win.cfg["panels"]["visible"].get("map") is True,
          "visibilidad de ventanas persistida")

    # cerrar la ventana con la X solo la oculta (geometría conservada)
    g_before = map_panel._win.geometry()
    map_panel._win.close()
    pump(app, 0.2)
    check(not map_panel.is_panel_visible() and map_panel._win is not None,
          "cerrar la ventana la oculta sin destruirla")
    win._catalog_checks["map"].setChecked(True)
    pump(app, 0.2)
    check(mp.isVisible() and map_panel._win.geometry() == g_before,
          "reabrir desde el catálogo conserva la geometría")

    # fijar (pin) una ventana y persistir su estado
    tower_panel = win._panels["tower"]
    tower_panel._win.pin_btn.setChecked(True)
    check(tower_panel.pinned, "ventana fijada (sin marco, encima)")
    st = tower_panel.save_state()
    check(st["floating"] and st["pinned"] and len(st["geom"]) == 4,
          f"estado de ventana persistible ({st})")
    tower_panel._win.pin_btn.setChecked(False)

    # 📷: captura del panel al portapapeles (sin diálogo en el test)
    pix = win._panels["tower"].capture_panel(ask_save=False)
    check(not pix.isNull() and pix.width() > 10,
          f"captura: imagen del panel ({pix.width()}x{pix.height()})")
    clip = QApplication.clipboard().pixmap()
    check(clip is not None and not clip.isNull(),
          "captura: copiada al portapapeles")
    check(win._panels["tower"]._win.shot_btn.isVisible()
          or win._panels["tower"]._win.shot_btn is not None,
          "captura: botón 📷 en la barra de la ventana")

    # imán de ventanas: acercar un borde a otro lo pega sin hueco
    from f1telem.ui import docks as _docks
    mw_w = win._panels["map"]._win
    tw_w = win._panels["tower"]._win
    _docks.set_snap_enabled(True)
    # zona despejada (lejos de la cascada de ventanas del test); el borde
    # se mide DESPUÉS de posicionar mw (su propio imán pudo ajustarla)
    mw_w.move(100, 900)
    mw_w.resize(300, 300)
    tw_w.resize(200, 260)
    mg = mw_w.frameGeometry()
    edge = mg.x() + mg.width()
    tw_w.move(edge + 7, mg.y() + 20)   # a 7 px del borde: debe pegarse
    check(tw_w.pos().x() == edge,
          f"imán: borde pegado sin hueco (x={tw_w.pos().x()} vs {edge})")
    _docks.set_snap_enabled(False)
    tw_w.move(edge + 7, mg.y() + 20)
    check(tw_w.pos().x() == edge + 7,
          f"imán: apagado respeta la posición exacta (x={tw_w.pos().x()})")
    _docks.set_snap_enabled(True)
    # ajuste fino por teclado: flecha = 1 px, Ctrl+flecha = estirar 1 px
    from PySide6.QtCore import QEvent as _QEvent
    from PySide6.QtGui import QKeyEvent as _QKeyEvent
    x0 = tw_w.pos().x()
    tw_w.keyPressEvent(_QKeyEvent(_QEvent.KeyPress, Qt.Key_Right,
                                  Qt.KeyboardModifier.NoModifier))
    check(tw_w.pos().x() == x0 + 1, "teclado: flecha mueve 1 px")
    w0 = tw_w.width()
    tw_w.keyPressEvent(_QKeyEvent(_QEvent.KeyPress, Qt.Key_Right,
                                  Qt.KeyboardModifier.ControlModifier))
    check(tw_w.width() == w0 + 1, "teclado: Ctrl+flecha estira 1 px")
    check(win.snap_check.isChecked(), "imán: toggle en Settings activo")

    # timeline: botones de salto relativo −15m…−5s / +5s…+15m, agrupados
    # ARRIBA de la barra (la barra ocupa todo el ancho)
    check(len(win.seek_back_btns) == 6 and len(win.seek_fwd_btns) == 6,
          "timeline: 6 saltos hacia atrás y 6 hacia adelante")
    from PySide6.QtWidgets import QVBoxLayout as _QVBL
    check(isinstance(win.seek_row.layout(), _QVBL)
          and win.seek_row.layout().count() == 2,
          "timeline: controles arriba, barra a todo el ancho abajo")
    seeks: list[float] = []
    orig_seek = win._seek_to_time
    win._seek_to_time = seeks.append
    win._progress = (0.0, 100.0, 1000.0)
    win._seek_relative(30)
    win._seek_relative(-900)   # clamp al inicio
    win._seek_relative(900)
    win._progress = None
    win._seek_relative(5)      # sin progreso: no hace nada
    win._seek_to_time = orig_seek
    check(seeks == [130.0, 0.0, 1000.0],
          f"timeline: saltos relativos con clamp ({seeks})")

    # Data tables: las tablas de timing viven en su propio panel, con las
    # pestañas clásicas adoptadas + "All data" (piloto × vuelta)
    win._panels["data_table"].set_panel_visible(True)
    pump(app, 0.3)
    dt = win.data_table_view
    check(dt.tabs.count() == 7 and dt.tabs.tabText(0) == "All data",
          f"data tables: pestañas adoptadas + All data ({dt.tabs.count()})")
    dt.tabs.setCurrentWidget(win.chart_timing.summary_table)
    win._catalog_checks["times_gap"].setChecked(False)
    pump(app, 1.2)
    check(win.chart_timing.summary_table.rowCount() > 0,
          "data tables: Summary se refresca con Times/Gap cerrado")
    win._catalog_checks["times_gap"].setChecked(True)
    dt.tabs.setCurrentIndex(0)
    dt.refresh()
    total_rows = sum(len(dt._laps_for(d)) for d in dt.drivers())
    check(dt.table.rowCount() == total_rows and total_rows > 0
          and dt.table.columnCount() == 11,
          f"data tables: fila por piloto y vuelta ({dt.table.rowCount()})")
    # coherencia de una fila: S1+S2+S3 = tiempo; AVG5 poblado
    d0 = dt.drivers()[0]
    l0 = dt._laps_for(d0)[-1]
    secs = win.chart_timing.analyzer.sector_times(d0, l0)
    lt0 = win.chart_timing.analyzer.lap_time(d0, l0)
    check(abs(sum(secs) - lt0) < 0.05,
          f"data tables: S1+S2+S3 = vuelta ({sum(secs):.3f} vs {lt0:.3f})")
    check(dt.table.item(0, 8).text() != "—",
          "data tables: AVG5 poblado en la primera fila")
    # filtro de pilotos: destildar uno saca sus filas
    d_hide = dt.drivers()[0]
    n_hide = len(dt._laps_for(d_hide))
    item_dt = next(dt.sel_btn.list.item(i)
                   for i in range(dt.sel_btn.list.count())
                   if dt.sel_btn.list.item(i).data(Qt.UserRole) == d_hide)
    item_dt.setCheckState(Qt.Unchecked)
    check(dt.table.rowCount() == total_rows - n_hide,
          f"data tables: filtro de pilotos ({dt.table.rowCount()})")
    item_dt.setCheckState(Qt.Checked)
    # rango de vueltas: [3, 4] son 2 filas por piloto en All data y By lap
    dt.from_spin.setValue(3)
    dt.to_spin.setValue(4)
    check(dt.table.rowCount() == 2 * len(dt.drivers()),
          f"data tables: rango de vueltas ({dt.table.rowCount()})")
    dt.tabs.setCurrentWidget(win.chart_timing.laps_table)
    dt.refresh()
    check(win.chart_timing.laps_table.rowCount() == 2,
          "data tables: By lap respeta el rango")
    # Summary también: el Best debe salir de las vueltas 3-4
    dt.tabs.setCurrentWidget(win.chart_timing.summary_table)
    dt.refresh()
    best_txt = win.chart_timing.summary_table.item(0, 3).text()
    check("(L3)" in best_txt or "(L4)" in best_txt,
          f"data tables: Summary respeta el rango ({best_txt})")
    check("laps 3–4" in win.chart_timing._note.text(),
          "data tables: la nota indica el rango activo")
    # Degradation: stints recortados al rango elegido
    dt.from_spin.setValue(6)
    dt.to_spin.setValue(9)
    dt.tabs.setCurrentWidget(win.chart_timing._deg_container)
    dt.refresh()
    st_deg = win.chart_timing.stint_table
    check(st_deg.rowCount() >= 1 and all(
        st_deg.item(r, 3).text().startswith("L6-")
        and int(st_deg.item(r, 3).text().split("-L")[1]) <= 9
        for r in range(st_deg.rowCount())),
        "data tables: Degradation respeta el rango "
        f"({st_deg.item(0, 3).text() if st_deg.rowCount() else '—'})")
    dt.from_spin.setValue(1)
    dt.to_spin.setValue(0)
    dt.refresh()
    best_all = win.chart_timing.summary_table.item(0, 3).text()
    check("(L" in best_all, "data tables: sin rango vuelve el Best global")
    dt.tabs.setCurrentIndex(0)
    pump(app, 0.3)

    # el hub concentra fuente, catálogo y perfiles; Drivers y Timeline son
    # ventanas destacadas (borde de acento), cerradas por defecto
    check(win.source_combo.isVisible()
          and len(win._catalog_checks) == len(win.PANEL_CATALOG)
          and "border" in win._catalog_checks["drivers"].styleSheet()
          and "border" in win._catalog_checks["timeline"].styleSheet(),
          "hub: fuente + catálogo + perfiles; Drivers/Timeline destacados")
    win._catalog_checks["drivers"].setChecked(True)
    pump(app, 0.2)
    check(win._panels["drivers"].is_panel_visible()
          and win.driver_list.isVisible(),
          "pilotos: ventana propia desde el catálogo")
    win._catalog_checks["drivers"].setChecked(False)
    pump(app, 0.2)

    # torre: tamaño de fuente A+/A−
    s0 = win.tower.scale
    win.tower._change_scale(+0.2)
    check(abs(win.tower.scale - (s0 + 0.2)) < 1e-9
          and win.tower.row_h == int(38 * win.tower.scale),
          f"torre: A+ escala fuente y filas (x{win.tower.scale:.1f})")
    check(abs(win.cfg["ui"].get("tower_scale", 0) - win.tower.scale) < 1e-9,
          "torre: escala persistida en config")
    win.tower._change_scale(-0.2)

    # canal POR VISTA: cambiar el del Race no toca el de Race 2
    win.race_channel_combo.setCurrentIndex(
        win.race_channel_combo.findData("throttle"))
    pump(app, 1.5)
    x, y = win.chart_rolling.curves[first].getData()
    check(y is not None and float(max(y)) <= 105.0, "cambio de canal a acelerador aplicado")
    check(win.chart_wrap.channel == "speed",
          f"canal independiente por ventana (Race 2 sigue en {win.chart_wrap.channel})")

    # valores en picos (máx de rectas / mín de curvas)
    win.race_channel_combo.setCurrentIndex(
        win.race_channel_combo.findData("speed"))
    win.peaks_check.setChecked(True)
    pump(app, 0.5)
    visible_peaks = [p for p in win.chart_rolling._peak_pool if p.isVisible()]
    check(len(visible_peaks) >= 6,
          f"picos marcados en el gráfico de velocidad ({len(visible_peaks)})")
    peak_ys = [float(p.pos().y()) for p in visible_peaks]
    check(all(40 <= y <= 360 for y in peak_ys),
          f"valores de picos plausibles ({min(peak_ys):.0f}..{max(peak_ys):.0f} km/h)")
    ymins = [y for y in peak_ys if y < 150]
    ymaxs = [y for y in peak_ys if y > 250]
    check(len(ymins) >= 2 and len(ymaxs) >= 2,
          f"hay mínimos de curva y máximos de recta ({len(ymins)} mín, {len(ymaxs)} máx)")
    win.peaks_check.setChecked(False)
    pump(app, 0.3)
    check(not any(p.isVisible() for p in win.chart_rolling._peak_pool),
          "toggle apaga los valores en picos")

    # ventana X configurable de la vista Race (por defecto 1 vuelta)
    pump(app, 0.3)
    win.race_window_combo.setCurrentIndex(win.race_window_combo.findData(2.0))
    pump(app, 0.5)
    xr = win.chart_rolling.getViewBox().viewRange()[0]
    expected = win.hub.track_length * 2.0 * (1 + win.chart_rolling.RIGHT_MARGIN_FRAC)
    check(abs((xr[1] - xr[0]) - expected) < win.hub.track_length * 0.05,
          f"Carrera: ventana de 2 vueltas aplicada ({xr[1] - xr[0]:.0f} m)")
    xy2 = win.chart_rolling._xy.get(first)
    check(xy2 is not None and float(xy2[0][-1] - xy2[0][0]) >= win.hub.track_length * 2.0,
          f"Carrera: datos cubren la ventana ampliada "
          f"({0 if xy2 is None else float(xy2[0][-1] - xy2[0][0]):.0f} m)")
    cx2, _cy2 = win.chart_rolling.curves[first].getData()
    # el cursor de reproducción va como mucho ~1 horizonte detrás del último
    # dato (a x25 el horizonte en metros escala con la velocidad)
    sm2 = win.chart_rolling._tip_sm.get(first)
    horizon = (sm2._rate * max(2.0 * sm2._gap, 0.2)
               if sm2 is not None and sm2._rate > 0 else 500.0)
    allowed = max(500.0, horizon * 1.3)
    check(float(cx2[-1] - cx2[0]) >= win.hub.track_length * 2.0 - allowed,
          f"Carrera: dibujado cubre la ventana menos el retardo de reproducción "
          f"({float(cx2[-1] - cx2[0]):.0f} m, tolerancia {allowed:.0f})")
    win.race_window_combo.setCurrentIndex(win.race_window_combo.findData(0.0))
    pump(app, 1.0)
    xr = win.chart_rolling.getViewBox().viewRange()[0]
    check(xr[0] <= 1.0 and xr[1] - xr[0] > win.hub.track_length * 3,
          f"Carrera: 'Todo' muestra desde el inicio ({xr[0]:.0f}..{xr[1]:.0f} m)")
    win.race_window_combo.setCurrentIndex(win.race_window_combo.findData(1.0))
    pump(app, 0.3)

    # seleccionar todos
    win.all_check.setChecked(True)
    pump(app, 0.3)
    check(len(win._selected_drivers()) == win.driver_list.count(),
          f"'Seleccionar todos' marca todos ({len(win._selected_drivers())})")
    win.all_check.setChecked(False)
    pump(app, 0.3)
    check(len(win._selected_drivers()) == 0, "'Seleccionar todos' desmarca todos")
    for i in range(4):
        win.driver_list.item(i).setCheckState(Qt.Checked)
    pump(app, 0.3)
    check(not win.all_check.isChecked(), "checkbox refleja selección parcial")

    # correlación mouse gráfico <-> mapa
    pump(app, 0.5)
    vb0 = win.chart_rolling.getViewBox()
    (cx0, cx1), (cy0, cy1) = vb0.viewRange()
    x_mid = (cx0 + cx1) / 2
    win.chart_rolling._probe._on_move(vb0.mapViewToScene(QPointF(x_mid, (cy0 + cy1) / 2)))
    check(mp.probe_marker.isVisible(), "hover en gráfico marca el mapa")
    exp_x, exp_y = track_pos(win.chart_rolling.dist_at(x_mid))
    got = mp.probe_marker.getData()
    dpos = float(np.hypot(exp_x - got[0][0], exp_y - got[1][0]))
    check(dpos < 40, f"marca del mapa en el punto de pista correcto ({dpos:.0f} m)")
    # inverso: hover sobre el trazado del mapa -> línea en el gráfico activo
    mapping = mp._ensure_dist_map()
    i_pt = 150
    sp = mp.getPlotItem().vb.mapViewToScene(QPointF(float(mapping[1][i_pt]), float(mapping[2][i_pt])))
    mp._on_scene_move(sp)
    check(win.chart_rolling._track_marker.isVisible(), "hover en mapa marca el gráfico")
    d_marker = win.chart_rolling.dist_at(float(win.chart_rolling._track_marker.value()))
    d_exp = float(mapping[0][i_pt])
    check(abs(d_marker - d_exp) < 20, f"referencia del gráfico en el metro correcto ({d_marker:.0f} vs {d_exp:.0f})")
    win.chart_rolling._probe._hide()
    check(not mp.probe_marker.isVisible(), "al salir del gráfico se apaga la marca del mapa")

    # degradación: esperar suficientes vueltas del segundo stint
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        pump(app, 0.5)
        if len(win.hub.buffers[first].completed_laps()) >= 8:
            break
    tv._update_degradation()
    check(len(tv._deg_curves) >= 4,
          f"degradación: series por stint/compuesto ({len(tv._deg_curves)})")
    deg_x, deg_y = tv._deg_curves[0].getData()
    check(len(deg_x) >= 2 and all(60 < y < 200 for y in deg_y),
          f"degradación: edad vs tiempo de vuelta plausible ({len(deg_x)} pts)")
    check(tv.stint_table.rowCount() >= 4,
          f"resumen de stints poblado ({tv.stint_table.rowCount()} filas)")
    prom_txt = tv.stint_table.item(0, 4).text()
    deg_txt = tv.stint_table.item(0, 5).text()
    check(":" in prom_txt, f"stints: ritmo promedio ({prom_txt})")
    check(deg_txt == "—" or deg_txt[0] in "+-",
          f"stints: pendiente de degradación ({deg_txt})")

    # paneles de contexto: tira de sesión, race control, estrategia y clima
    win.session_strip.refresh()
    check(win.session_strip.session_label.text() == "Demo Grand Prix — Race",
          f"tira de sesión: nombre ({win.session_strip.session_label.text()})")
    lap_txt = win.session_strip.lap_label.text()
    check(lap_txt.startswith("LAP ") and lap_txt.endswith("/20"),
          f"tira de sesión: vuelta actual/total ({lap_txt})")
    check(win.session_strip.clock_label.text().startswith("⏱"),
          f"tira de sesión: reloj ({win.session_strip.clock_label.text()})")
    check(win.session_strip.flag_label.isVisible(),
          "tira de sesión: badge de bandera visible")
    check("SAFETY CAR" in win.session_strip.rcm_label.text(),
          f"tira de sesión: último mensaje RCM ({win.session_strip.rcm_label.text()})")

    for pid in ("strategy", "race_control", "weather", "weather_chart"):
        win._panels[pid].set_panel_visible(True)
    pump(app, 0.2)
    win.race_control_view.refresh()
    check(win.race_control_view.list.count() == 5,
          f"race control: mensajes listados ({win.race_control_view.list.count()})")

    win.strategy_view.refresh()
    check(len(win.strategy_view.rows) == 6,
          f"estrategia: una fila por auto ({len(win.strategy_view.rows)})")
    stints0 = win.strategy_view.rows[0][3]
    check(stints0[0] == ("SOFT", 1, 4) and stints0[1][0] == "MEDIUM",
          f"estrategia: stints SOFT 1-4 y MEDIUM ({stints0[:2]})")

    win.tyre_stints_view.refresh()
    check(len(win.tyre_stints_view.rows) == 6,
          f"tyre stints: una fila por auto ({len(win.tyre_stints_view.rows)})")
    chips0 = win.tyre_stints_view.rows[0][3]
    check(chips0[0] == ("SOFT", 1, 4, True)
          and chips0[1][0] == "MEDIUM" and chips0[1][3],
          f"tyre stints: compuesto, vueltas y N de juego nuevo ({chips0[:2]})")
    # juego usado (edad inicial > 1): el chip sale sin la "N" — inyectado
    # en vueltas PASADAS (las futuras quedan clipeadas por el timeline)
    drv_ts = win.tyre_stints_view.rows[0][0]
    saved_tyres = dict(win.hub.tyres[drv_ts])
    lap_ts = win.hub.buffers[drv_ts].current_lap()
    win.hub.tyres[drv_ts][lap_ts - 3] = ("HARD", 6)
    win.hub.tyres[drv_ts][lap_ts - 2] = ("HARD", 7)
    win.tyre_stints_view.refresh()
    chips_ts = next(r[3] for r in win.tyre_stints_view.rows if r[0] == drv_ts)
    check(("HARD", lap_ts - 3, lap_ts - 2, False) in chips_ts,
          f"tyre stints: juego usado sin N ({chips_ts[1:3]})")
    win.hub.tyres[drv_ts].clear()
    win.hub.tyres[drv_ts].update(saved_tyres)
    win.tyre_stints_view.refresh()

    # anti-spoiler: los stints nunca pasan la vuelta actual del timeline
    win.strategy_view.refresh()
    lead_now = max(b.current_lap() for b in win.hub.buffers.values() if b.n)
    check(all(s[2] <= lead_now for _d, _c, _col, st in win.strategy_view.rows
              for s in st),
          f"estrategia: stints cortados en la vuelta actual (lider {lead_now})")
    check(all(c[2] <= lead_now for r in win.tyre_stints_view.rows
              for c in r[3]),
          "tyre stints: sin vueltas futuras")

    # panel Microsectors: cortes en tabla y mapa, persistentes por circuito
    win._catalog_checks["micro_config"].setChecked(True)
    pump(app, 0.4)
    mcv = win.micro_config_view
    mcv.refresh()
    check(mcv.table.rowCount() == 21 and len(mcv.cut_xy) == 21,
          f"µconfig: 21 cortes automáticos en tabla y mapa "
          f"({mcv.table.rowCount()}/{len(mcv.cut_xy)})")
    mkey = win.hub.circuit_key()
    check(mkey == "demo-grand-prix", f"µconfig: clave de circuito ({mkey!r})")
    mcv._add_cut()
    pump(app, 0.6)
    check(win.hub.custom_micro is not None and mcv.table.rowCount() == 22,
          "µconfig: agregar corte pasa a modo custom")
    check(win.cfg.get("microsectors", {}).get(mkey) == win.hub.custom_micro,
          "µconfig: config guardada por circuito+año")
    win.chart_timing._update_micro(win._selected_drivers()[0])
    check(win.tower.analyzer.n_micro() == 25
          and win.chart_timing.micro_table.columnCount() == 25,
          f"µconfig: torre y tabla siguen la cantidad nueva "
          f"({win.tower.analyzer.n_micro()} µ)")
    # tarjetas de Qualy: target vieja + config nueva no puede romper (las
    # marcas de la target se releen del analyzer) y la grilla se rearma
    win.chart_qualy._update_cards()
    any_card = next(iter(win.chart_qualy.cards.values()))
    check(len(any_card.micros) == 25,
          f"µconfig: tarjetas de Qualy siguen la cantidad nueva "
          f"({len(any_card.micros)})")
    # regresión: seek (clear_data) con config custom activa — la torre debe
    # re-dimensionar sus acumuladores a la config, no al default de 24
    win.tower.clear_data()
    win.tower.refresh()
    check(len(win.tower._sess_micro) == 25,
          f"µconfig: la torre re-dimensiona tras un seek "
          f"({len(win.tower._sess_micro)} µ)")
    mcv.select_cut(0)
    d_new = float(mcv.cuts()[0]) + 40.0
    idx_new = mcv.move_cut(0, d_new)
    check(abs(mcv.cuts()[idx_new] - round(d_new, 1)) < 0.2,
          f"µconfig: mover corte ({mcv.cuts()[idx_new]:.1f} m)")
    mcv.select_cut(0)
    mcv._remove_cut()
    check(mcv.table.rowCount() == 21, "µconfig: quitar corte")
    # persistente por fin de semana: recargar la trae tal cual
    saved_cuts = list(win.hub.custom_micro)
    win.hub.custom_micro = None
    win._micro_cfg_key = None
    win._load_micro_cfg()
    check(win.hub.custom_micro == saved_cuts,
          "µconfig: la config del circuito se recarga sola")
    mcv._reset_auto()
    pump(app, 0.6)
    check(win.hub.custom_micro is None
          and mkey not in win.cfg.get("microsectors", {})
          and win.tower.analyzer.n_micro() == 24,
          "µconfig: reset vuelve a los cortes automáticos")

    # candado: congela los cortes tal como están; los ajustes automáticos
    # que llegan con los autos girando ya no los mueven
    mcv.lock_btn.click()
    check(win.hub.custom_micro is not None
          and len(win.hub.custom_micro) == 21
          and win.cfg.get("microsectors", {}).get(mkey) == win.hub.custom_micro,
          "µconfig: candado congela y guarda los cortes actuales")
    cuts_locked = list(mcv.cuts())
    bd_prev = win.hub.brake_dists
    win.hub.brake_dists = {float(win.hub.corners[0][1]): 250.0}  # "llega" dato
    check(mcv.cuts() == cuts_locked,
          "µconfig: con candado, una derivación nueva no mueve los cortes")
    win.hub.brake_dists = bd_prev
    mcv.lock_btn.click()
    pump(app, 0.4)
    check(win.hub.custom_micro is None,
          "µconfig: soltar el candado vuelve al automático")

    # abandonos: fuera del mapa, al fondo de la torre con RET y sin gaps
    # (por la vía oficial: la heurística de movimiento se prueba en la
    # rueda, donde el refresh es síncrono y el demo no repone los datos)
    win.hub.on_retirements([first])
    pump(app, 0.5)
    check(len(win.track_map.dots.points()) == win.driver_list.count() - 1,
          "map: el auto fuera de carrera desaparece del mapa")
    win.tower.refresh()
    row_ret = win.tower.rows[-1]
    check(row_ret.drv == first and row_ret.retired and row_ret.gap_txt == "—",
          "torre: fuera de carrera al fondo con RET y sin gaps")
    win.hub.on_retirements([])
    pump(app, 0.3)
    check(len(win.track_map.dots.points()) == win.driver_list.count(),
          "map: vuelve al des-retirarse")

    win.weather_now.refresh()
    check(win.weather_now._values["air"].text().endswith("°"),
          f"clima: temperatura de aire ({win.weather_now._values['air'].text()})")
    win.weather_chart.refresh()
    wx = win.weather_chart.c_air.getData()[0]
    lead_lap = max(b.current_lap() for b in win.hub.buffers.values() if b.n)
    check(wx is not None and len(wx) == 3 and wx[-1] <= lead_lap + 1
          and all(np.diff(wx) >= 0),
          f"clima: eje X en vueltas del líder ({None if wx is None else [round(float(v), 2) for v in wx]})")
    check(len(win.weather_chart._rain_items) == 1,
          f"clima: banda de lluvia ({len(win.weather_chart._rain_items)})")

    # anti-spoiler: nada posterior al timeline en race control/strip/clima
    n_rc = win.race_control_view.list.count()
    win.hub.race_control.append({
        "t": win.hub.latest_t + 500.0, "lap": 99, "category": "Other",
        "flag": "RED", "scope": "Track", "sector": None, "mode": "",
        "message": "FUTURE SPOILER"})
    win.race_control_view.refresh()
    check(win.race_control_view.list.count() == n_rc,
          "race control: mensaje futuro oculto")
    win.session_strip.refresh()
    check("FUTURE" not in win.session_strip.rcm_label.text(),
          "strip: el último mensaje respeta el timeline")
    win.hub.race_control.pop()
    n_wx = len(win.weather_chart.c_air.getData()[0])
    win.hub.weather.append((win.hub.latest_t + 999.0, 30.0, 50.0, 9.9, True))
    win.weather_chart._sig = None
    win.weather_chart.refresh()
    check(len(win.weather_chart.c_air.getData()[0]) == n_wx,
          "clima: lectura futura fuera del gráfico")
    win.hub.weather.pop()
    win.weather_chart._sig = None

    # clima extendido (humedad, presión, dirección del viento) + brújula
    check(all(len(r) == 8 for r in win.hub.weather),
          "clima: filas normalizadas a 8 campos")
    win.weather_now.refresh()
    check(win.weather_now._values["hum"].text() == "60%"
          and win.weather_now._values["press"].text() == "1011 mb",
          f"clima: humedad y presión ({win.weather_now._values['hum'].text()}, "
          f"{win.weather_now._values['press'].text()})")
    check("205°" in win.weather_now._values["wind"].text(),
          f"clima: dirección en la celda de viento "
          f"({win.weather_now._values['wind'].text()})")
    win.track_map.refresh()
    badge = win.track_map.wind_badge
    check(badge._dir == 205.0 and abs(badge._speed - 3.1) < 1e-6
          and not badge.isHidden(),
          f"map: brújula de viento con la última lectura ({badge._dir}°)")
    check("205" in badge.toolTip(), "map: tooltip de la brújula")

    # grilla oficial (OpenF1): el Δ posición se ancla en ella, no en el
    # primer orden observado
    win.tower.refresh()
    order_now = [r.drv for r in win.tower.rows]
    grid_official = {drv: len(order_now) - i
                     for i, drv in enumerate(order_now)}  # grilla invertida
    win.hub.on_grid(grid_official)
    win.tower.refresh()
    ok_delta = all(
        r.delta == grid_official[r.drv] - r.pos and r.grid == grid_official[r.drv]
        for r in win.tower.rows if r.ready)
    check(ok_delta, "torre: Δ posición contra la grilla oficial")
    check(f"grid P{win.tower.rows[0].grid}"
          in win.tower._row_tooltip(win.tower.rows[0]),
          "torre: grilla oficial en el tooltip")
    win.hub.grid = {}

    # paradas oficiales (OpenF1): contraste contra el tiempo medido
    win.hub.on_official_pits({first: {4: (21.3, 2.4)}})
    win.tower.refresh()
    row_f = next(r for r in win.tower.rows if r.drv == first)
    check(abs(row_f.pit_stop_off - 2.4) < 1e-9,
          f"torre: parada oficial de la vuelta 4 ({row_f.pit_stop_off})")
    check("official 2.4s" in win.tower._row_tooltip(row_f),
          "torre: parada oficial en el tooltip")
    check(win.hub.official_stop(first, 5) == (21.3, 2.4)
          and win.hub.official_stop(first, 6) is None,
          "hub: parada oficial con tolerancia de ±1 vuelta")
    win.hub.official_pits = {}

    # fotos de pilotos (OpenF1/CDN): tooltips con la imagen local
    photo = Path(os.environ["LOCALAPPDATA"]) / "fake-headshot.png"
    photo.write_bytes(b"\x89PNG\r\n\x1a\n")
    win.hub.on_headshots({first: str(photo)})
    pump(app, 0.3)  # driversChanged rearma la lista de pilotos
    check("<img" in win.tower._row_tooltip(
        next(r for r in win.tower.rows if r.drv == first)),
        "torre: tooltip con foto del piloto")
    item_first = next(
        win.driver_list.item(i) for i in range(win.driver_list.count())
        if win.driver_list.item(i).data(Qt.UserRole) == first)
    check("<img" in item_first.toolTip(),
          "drivers: tooltip de la lista con foto")
    other = next(d for d in order_now if d != first)
    item_other = next(
        win.driver_list.item(i) for i in range(win.driver_list.count())
        if win.driver_list.item(i).data(Qt.UserRole) == other)
    check("<img" not in item_other.toolTip() and item_other.toolTip(),
          "drivers: sin foto el tooltip queda en texto")
    win.hub.headshots.clear()

    win.tower.refresh()
    tyres_shown = {r.tyre for r in win.tower.rows}
    check(tyres_shown == {"MEDIUM"} and all(r.tyre_age > 0 for r in win.tower.rows),
          f"torre: compuesto y edad actuales ({tyres_shown})")

    # violeta único: un solo mejor absoluto por vuelta, por sector y por µ
    check(sum(1 for r in win.tower.rows if r.best_kind == 3) == 1,
          "torre: un único violeta de mejor vuelta en la tanda")
    for k in range(3):
        n_p = sum(1 for r in win.tower.rows if r.sectors[k][1] == 3)
        check(n_p <= 1, f"torre: a lo sumo un violeta en S{k + 1} ({n_p})")
    mu_dup = {
        (k, i)
        for k in range(3)
        for i in range(len(win.tower.rows[0].segs[k]))
        if sum(1 for r in win.tower.rows if r.segs[k][i] == 3) > 1
    }
    check(not mu_dup, f"torre: violetas de µ únicos entre pilotos ({mu_dup})")

    # referencia por click: los deltas se calculan contra el auto elegido
    ref_pick = win.tower.rows[2].drv
    win.tower.set_reference(ref_pick)
    check(win.tower.ref_drv == ref_pick, "torre: referencia elegida")
    r_ref = next(r for r in win.tower.rows if r.drv == ref_pick)
    check(r_ref.ref_gap_txt == "—", "torre: la fila de referencia sin gap propio")
    ahead_ref = [r for r in win.tower.rows if r.pos < r_ref.pos]
    behind_ref = [r for r in win.tower.rows
                  if r.pos > r_ref.pos and not r.retired]
    check(ahead_ref and all(r.ref_gap_txt.startswith("+") for r in ahead_ref),
          f"torre: adelante de la ref en POSITIVO (la ref pierde) "
          f"({[r.ref_gap_txt for r in ahead_ref]})")
    check(behind_ref and all(r.ref_gap_txt.startswith("-") for r in behind_ref),
          f"torre: detrás de la ref en NEGATIVO (favorable) "
          f"({[r.ref_gap_txt for r in behind_ref]})")
    win.tower.set_reference(ref_pick)  # mismo click: la saca
    check(win.tower.ref_drv is None and win.tower.rows[0].ref_gap_txt == "",
          "torre: click de nuevo saca la referencia")
    # convención de signo Y color desde la referencia: POSITIVO/rojo = la
    # ref pierde contra esa fila, NEGATIVO/verde = le gana
    check(win.tower._ref_color(1.0).name() == "#ff6b5e"
          and win.tower._ref_color(-1.0).name() == "#2fbf71",
          "torre: signo y color desde el punto de vista de la referencia")

    # onda delta: ventana rodante de exactamente 1 vuelta contra el líder
    win.tower.refresh()
    waves = [r for r in win.tower.rows if r.wave is not None]
    check(len(waves) >= 4, f"torre: ondas delta calculadas ({len(waves)})")
    check(win.tower.rows[0].wave is None,
          "torre: el líder (blanco de la onda) no se grafica a sí mismo")
    fr_w = waves[0].wave[0]
    check(len(fr_w) == 240 and bool((fr_w >= 0.0).all())
          and bool((fr_w <= 1.0).all()),
          "torre: onda en bins de fracción de vuelta")
    check(all(0.0 <= r.wave_now <= 1.0 for r in waves),
          "torre: línea blanca de posición actual en rango")

    # columnas mostrar/ocultar (▦), persistidas en config
    win.tower.set_column("pills", False)
    win.tower.set_column("wave", False)
    check(win.cfg["ui"]["tower_cols"] == {"pills": False, "wave": False},
          "torre: columnas ocultas persistidas")
    check(not win.tower._col_checks["pills"].isChecked(),
          "torre: popup ▦ refleja el estado")
    pump(app, 0.4)  # repinta sin esas columnas, sin romper
    win.tower.set_column("pills", True)
    win.tower.set_column("wave", True)
    check(win.cfg["ui"]["tower_cols"] == {},
          "torre: restaurar columnas limpia la config")

    # chips de comisarios: ⚠ investigación → +5s sanción → SERVED limpia
    code_f = win.hub.drivers[first].code
    base_rc = len(win.hub.race_control)
    win.hub.race_control.append({
        "t": win.hub.latest_t - 3.0, "lap": 5, "category": "Other",
        "flag": "", "scope": "Driver", "sector": None, "mode": "",
        "message": f"TURN 4 INCIDENT INVOLVING CAR {first} ({code_f}) "
                   f"UNDER INVESTIGATION"})
    check(win.hub.stewards_flags().get(first) == "⚠",
          "comisarios: investigación abre el chip ⚠")
    win.hub.race_control.append({
        "t": win.hub.latest_t - 2.0, "lap": 5, "category": "Other",
        "flag": "", "scope": "Driver", "sector": None, "mode": "",
        "message": f"FIA STEWARDS: 5 SECOND TIME PENALTY FOR CAR {first} "
                   f"({code_f}) - TRACK LIMITS"})
    check(win.hub.stewards_flags().get(first) == "+5s",
          "comisarios: sanción pendiente +5s")
    win.tower.refresh()
    row_f = next(r for r in win.tower.rows if r.drv == first)
    check(row_f.stew == "+5s", "torre: chip de sanción en la fila")
    win.hub.race_control.append({
        "t": win.hub.latest_t - 1.0, "lap": 6, "category": "Other",
        "flag": "", "scope": "Driver", "sector": None, "mode": "",
        "message": f"CAR {first} ({code_f}) PENALTY SERVED"})
    check(win.hub.stewards_flags().get(first) is None,
          "comisarios: SERVED limpia el chip")
    while len(win.hub.race_control) > base_rc:
        win.hub.race_control.pop()

    # posición NETA del ciclo de paradas (Pit strategy): con una parada
    # extra pagada y ventana grande, el auto queda neto P1
    psv = win.pit_strategy_view
    prev_window = psv.window_spin.value()
    win.hub.pits.setdefault(first, []).append(
        (win.hub.buffers[first].current_lap(), win.hub.latest_t - 5.0))
    psv.window_spin.setValue(60.0)
    psv._last_table = 0.0
    psv.refresh()
    row_i = next(i for i in range(psv.table.rowCount())
                 if psv.table.item(i, 1).text() == code_f)
    net_txt = psv.table.item(row_i, 3).text()
    cur_p = row_i + 1
    stops_t = sorted((len(win.hub.pit_stops_done(d))
                      for d in win.hub.buffers), reverse=True)
    check(net_txt.startswith("P")
          and (int(net_txt[1:]) < cur_p or cur_p == 1),
          f"pit strategy: la parada extra pagada mejora el neto "
          f"(P{cur_p} -> {net_txt}; stops={stops_t})")
    win.hub.pits[first].pop()
    psv.window_spin.setValue(prev_window)

    # momentos en la timeline: hover y click sobre un sobrepaso
    t_m = win.hub.latest_t - 60.0
    win.notifier.moments.append(
        {"t": t_m, "lap": 3, "text": "L3: TST overtakes ZZZ for P4"})
    win._moments_n = -1  # forzar el volcado en el próximo tick del notifier
    deadline = time.monotonic() + 8.0  # bajo carga el tick pierde frecuencia
    while time.monotonic() < deadline and win._moments_n == -1:
        pump(app, 0.3)
    check(len(win.lap_ruler._moments) == len(win.notifier.moments),
          f"timeline: momentos volcados a la regla (regla="
          f"{len(win.lap_ruler._moments)} notif={len(win.notifier.moments)} "
          f"n={win._moments_n} tick={win._tick_n})")
    check("overtakes" in win.lap_ruler.hint_at(t_m),
          "timeline: hover muestra el momento")
    target_m = win.lap_ruler._target_for_click(t_m)
    check(target_m is not None and abs(target_m - (t_m - 5.0)) < 1e-6,
          "timeline: click salta justo antes del momento")
    # capas de la timeline: ocultar sobrepasos apaga dibujo, hover y click
    win._timeline_layer_toggled("overtakes", False)
    check(win.cfg["ui"]["timeline_layers"] == {"overtakes": False},
          "timeline: capa oculta persistida")
    check(win.lap_ruler._moment_near(t_m) is None,
          "timeline: capa oculta apaga hover y click del momento")
    win._timeline_layer_toggled("overtakes", True)
    check(win.cfg["ui"].get("timeline_layers", {}) == {}
          and win.lap_ruler._moment_near(t_m) is not None,
          "timeline: capa restaurada limpia la persistencia")

    # pit lane: última pasada en la torre y panel con relojes corriendo
    row0 = win.tower.rows[0]
    check(row0.pit_lap == 4 and abs(row0.pit_lane_s - 21.0) < 0.6
          and row0.pit_stop_s == row0.pit_stop_s and row0.pit_stop_s < 0.5
          and not row0.pit_open and not row0.pit_out,
          f"torre: última pasada por boxes (L{row0.pit_lap}, "
          f"{row0.pit_lane_s:.1f}s en calle, {row0.pit_stop_s:.1f}s detenido)")
    win._panels["pitlane"].set_panel_visible(True)
    pump(app, 0.2)
    win.pitlane_view.refresh()
    pl_rows = win.pitlane_view.rows
    n_pit = sum(1 for d in win.hub.pit_lane
                if win.hub.last_pit_visit(d) is not None)
    check(n_pit >= 1 and len(pl_rows) == n_pit
          and all(not r.inside for r in pl_rows)
          and win.pitlane_view.sep_index == 0,
          f"pit lane: los que ya salieron siguen listados ({len(pl_rows)})")
    check(all(r.tyre_in == "SOFT" and r.tyre_out == "MEDIUM" for r in pl_rows),
          "pit lane: compuestos de entrada y salida (SOFT → MEDIUM)")
    check(all(r.laps_ago is not None and r.laps_ago >= 1 for r in pl_rows),
          "pit lane: indicador de vueltas desde la salida")
    win.hub.pit_lane.setdefault(first, []).append(
        [9, win.hub.latest_t - 15.0, None])
    win.pitlane_view.refresh()
    win.tower.refresh()
    pl_rows = win.pitlane_view.rows
    check(len(pl_rows) == n_pit and pl_rows[0].drv == first
          and pl_rows[0].inside and win.pitlane_view.sep_index == 1,
          "pit lane: el reingreso renueva la fila y la sube a opacidad plena")
    check(pl_rows[0].tyre_in == "MEDIUM" and pl_rows[0].lane_s >= 14.5,
          f"pit lane: compuesto de entrada y reloj corriendo "
          f"({pl_rows[0].tyre_in}, {pl_rows[0].lane_s:.1f}s)")
    # filtro 👥 propio: ocultar al piloto lo saca del panel
    item_pl = next(win.pitlane_view.filter_btn.list.item(i)
                   for i in range(win.pitlane_view.filter_btn.list.count())
                   if win.pitlane_view.filter_btn.list.item(i)
                   .data(Qt.UserRole) == first)
    item_pl.setCheckState(Qt.Unchecked)
    check(len(win.pitlane_view.rows) == n_pit - 1,
          "pit lane: filtro 👥 oculta al piloto")
    item_pl.setCheckState(Qt.Checked)
    check(len(win.pitlane_view.rows) == n_pit, "pit lane: filtro 👥 restaurado")
    row_f = next(r for r in win.tower.rows if r.drv == first)
    check(row_f.pit_open and row_f.pit_lap == 9,
          "torre: pasada abierta marcada (en calle ahora)")
    win.hub.pit_lane[first].pop()
    win.pitlane_view.refresh()

    # tag OUT: la última visita cerró hace menos de una vuelta
    cur_out = win.hub.buffers[first].current_lap()
    win.hub.pit_lane[first].append(
        [cur_out, win.hub.latest_t - 30.0, win.hub.latest_t - 10.0])
    win.tower.refresh()
    row_f = next(r for r in win.tower.rows if r.drv == first)
    check(row_f.pit_out and not row_f.pit_open,
          "torre: tag OUT en la vuelta de salida")
    win.hub.pit_lane[first].pop()
    win.tower.refresh()

    # replay: la historia completa llega por adelantado; una visita futura
    # sin salida no debe listar al auto como "en boxes ahora"
    win.hub.pit_lane.setdefault(first, []).append(
        [12, win.hub.latest_t + 500.0, None])
    win.pitlane_view.refresh()
    win.tower.refresh()
    row_pl = next(r for r in win.pitlane_view.rows if r.drv == first)
    check(not row_pl.inside,
          "pit lane: visita futura (replay) no lo marca en boxes")
    row_f = next(r for r in win.tower.rows if r.drv == first)
    check(not row_f.pit_open and row_f.pit_lap == 4,
          "torre: visita futura no marca 'en calle ahora'")
    win.hub.pit_lane[first].pop()
    win.pitlane_view.refresh()

    # pit lane sintético: persistencia, vueltas desde la salida, dato
    # oficial, compuesto con retraso, reingreso y clavados
    from f1telem.hub import DataHub as _DH
    from f1telem.models import Sample as _S
    from f1telem.ui.pitlane import PitlaneView as _PLV
    hub_pl = _DH()
    hub_pl.on_track_length(3000.0)
    plv = _PLV(hub_pl)
    hub_pl.on_batch([_S("9", k * 0.5, 1, 50.0 * (k * 0.5), 50.0 * (k * 0.5),
                        180.0, 0.0, 0.0, 0.0, 0, 0) for k in range(61)])
    hub_pl.pit_lane["9"] = [[1, 2.0, 6.0]]  # salió en t=6
    plv.refresh()
    check(len(plv.rows) == 1 and not plv.rows[0].inside
          and abs(plv.rows[0].lane_s - 4.0) < 1e-6,
          "pit lane: el que salió queda listado con relojes congelados")
    check(plv.rows[0].laps_ago == 0 and plv.sep_index == 0,
          "pit lane: salida en la misma vuelta = 0L")
    # el dato oficial de OpenF1 corrige la detención medida
    hub_pl.on_official_pits({"9": {1: (4.1, 2.3)}})
    plv.refresh()
    check(abs(plv.rows[0].stop_s - 2.3) < 1e-9 and plv.rows[0].stop_official,
          "pit lane: detención corregida con el dato oficial (STOP✓)")
    # mucho después (otras vueltas, >2 min) la fila NO desaparece
    hub_pl.on_batch([_S("9", 130.0 + k, 3, 500.0 + 10.0 * k, 6500.0 + 10.0 * k,
                        180.0, 0.0, 0.0, 0.0, 0, 0) for k in range(11)])
    plv.refresh()
    check(len(plv.rows) == 1 and not plv.rows[0].inside,
          "pit lane: la fila persiste (sin expirar por tiempo ni S2)")
    check(plv.rows[0].laps_ago == 2,
          f"pit lane: salió hace 2 vueltas ({plv.rows[0].laps_ago})")
    # compuesto de salida con retraso de origen: se completa al llegar
    hub_pl.on_tyres({"9": {1: ("SOFT", 5)}})
    plv.refresh()
    check(plv.rows[0].tyre_in == "SOFT" and plv.rows[0].tyre_out == "",
          "pit lane: compuesto de salida aún sin dato (aro '?')")
    hub_pl.on_tyres({"9": {1: ("SOFT", 5), 2: ("HARD", 1)}})
    plv.refresh()
    check(plv.rows[0].tyre_out == "HARD",
          "pit lane: el compuesto de salida se completa al llegar el dato")
    # reingreso: la visita nueva renueva la fila (opacidad plena, sin ✓)
    hub_pl.pit_lane["9"].append([3, hub_pl.latest_t - 5.0, None])
    plv.refresh()
    check(len(plv.rows) == 1 and plv.rows[0].inside
          and not plv.rows[0].stop_official and plv.sep_index == 1,
          "pit lane: reingreso renueva la fila a opacidad plena")
    # clavado a velocidad 0 más de 30 s: resaltado (¿abandono/reparación?)
    hub_pl.on_batch([_S("10", 100.0 + k, 2, 800.0, 3800.0, 0.0,
                        0.0, 0.0, 0.0, 0, 0) for k in range(41)])
    hub_pl.pit_lane["10"] = [[2, 102.0, None]]
    plv.refresh()
    row10 = next(r for r in plv.rows if r.drv == "10")
    row9 = next(r for r in plv.rows if r.drv == "9")
    check(row10.inside and row10.stalled and not row9.stalled,
          "pit lane: clavado ≥30 s a velocidad 0 resaltado")
    check([r.drv for r in plv.rows] == ["9", "10"] and plv.sep_index == 2,
          "pit lane: adentro arriba, ingreso más reciente primero")
    # línea separadora entre los que están adentro y los que salieron
    hub_pl.on_batch([_S("11", k * 1.0, 1, 30.0 * k, 30.0 * k, 150.0,
                        0.0, 0.0, 0.0, 0, 0) for k in range(51)])
    hub_pl.pit_lane["11"] = [[1, 10.0, 20.0]]
    plv.refresh()
    check(len(plv.rows) == 3 and plv.sep_index == 2
          and [r.inside for r in plv.rows] == [True, True, False],
          "pit lane: línea separadora entre adentro y afuera")

    # control hub: destacados con ícono/color y catálogo completo en grupos
    check(win._catalog_checks["drivers"].text().startswith("👥")
          and win._catalog_checks["timeline"].text().startswith("⏱"),
          "hub: Drivers y Timeline destacados con ícono")
    check(set(win._catalog_checks)
          == {pid for pid, _t, _d in win.PANEL_CATALOG},
          "hub: todos los paneles presentes tras el agrupado")
    grouped_ids = set(win._FEATURED) | {
        p for _n, pids in win.PANEL_GROUPS for p in pids}
    check({pid for pid, _t, _d in win.PANEL_CATALOG} <= grouped_ids,
          "hub: ningún panel quedó fuera de las secciones")
    # columnas responsivas del catálogo según el ancho del hub (ancho
    # explícito: el mínimo alcanzable depende de las fuentes del sistema)
    grid0, btns0 = win._catalog_sections[0]
    win._relayout_catalog(250)
    check(win._catalog_cols == 1
          and grid0.getItemPosition(grid0.indexOf(btns0[1]))[:2] == (1, 0),
          f"hub: 1 columna con ancho angosto ({win._catalog_cols})")
    win._relayout_catalog(1000)
    check(win._catalog_cols == 4
          and grid0.getItemPosition(grid0.indexOf(btns0[1]))[:2] == (0, 1),
          f"hub: 4 columnas con ancho amplio ({win._catalog_cols})")
    win._relayout_catalog(500)
    check(win._catalog_cols == 3, "hub: 3 columnas en ancho intermedio")
    win._relayout_catalog()  # vuelve al ancho real de la ventana
    # el hub escrolea: puede achicarse verticalmente sin límite de contenido
    from PySide6.QtWidgets import QScrollArea as _QSA
    check(isinstance(win.centralWidget(), _QSA),
          "hub: contenido dentro de un scroll")
    g_hub2 = win.geometry()
    win.resize(g_hub2.width(), 380)
    pump(app, 0.2)
    check(win.height() <= 400,
          f"hub: se reduce verticalmente ({win.height()})")
    win.resize(g_hub2.width(), g_hub2.height())
    pump(app, 0.2)
    # sesión status encogible + rename del editor de microsectores
    check(win.session_strip.minimumSizeHint().width() <= 200
          or win.session_strip.minimumWidth() == 90,
          "session strip: puede encogerse horizontalmente")
    check(win._panels["micro_config"].title == "Microsectors Editor",
          "hub: botón renombrado a Microsectors Editor")

    # sección Analysis: hub lanzador + paneles con datos del demo
    btn_an = win._catalog_checks["analysis"]
    btn_an.setChecked(True)
    pump(app, 0.2)
    check(win._panels["analysis"].is_panel_visible(),
          "analysis: ventana del hub abierta desde el catálogo")
    win.analysis_launcher.buttons["an_gg"].setChecked(True)
    win.analysis_launcher.buttons["an_deploy"].setChecked(True)
    pump(app, 0.3)
    check(win._panels["an_gg"].is_panel_visible()
          and win._panels["an_deploy"].is_panel_visible(),
          "analysis: paneles abiertos desde el lanzador")
    gg = win.analysis_views["an_gg"]
    gg.refresh()
    check(len(gg._items) >= 1, "analysis: g-g con contenido del demo")
    check(gg.table.rowCount() >= 1, "analysis: tabla del g-g con filas")
    gg.points_check.setChecked(False)
    check(len(gg._items) >= 1 and all(
        type(i).__name__ != "ScatterPlotItem" for i in gg._items),
        "analysis: solo la envolvente al ocultar los puntos")
    gg.points_check.setChecked(True)
    dp = win.analysis_views["an_deploy"]
    dp.refresh()
    check(dp.controls.zone_btn.list.count() >= 2,
          "analysis: selector multi-zona poblado "
          f"({dp.controls.zone_btn.list.count()})")
    check(len(dp.controls.drivers()) == win.driver_list.count(),
          "analysis: todos los autos por defecto en la botonera")
    check(dp.table.rowCount() >= 1, "analysis: tabla por vuelta con filas")
    # modo Total: barras por piloto y tabla agregada
    dp.controls.mode_combo.setCurrentIndex(1)
    dp.refresh()
    check(dp.controls.mode() == "total" and dp.table.rowCount() >= 1
          and dp.table.columnCount() == 6,
          "analysis: modo Total con tabla agregada")
    dp.controls.mode_combo.setCurrentIndex(0)
    # multi-zona: solo curvas via atajo
    dp.controls.zone_btn._quick("corner")
    dp.refresh()
    check(dp.controls.selector()[0] == "multi",
          "analysis: atajo Corners deja selección múltiple")
    dp.controls.zone_btn._quick(None)
    # tildes del mapa: mostrar/ocultar derate y lift & coast por separado
    dp.refresh()
    n_lines_on = sum(1 for i in dp._overlay
                     if type(i).__name__ == "PlotDataItem")
    check(n_lines_on >= 1,
          f"deploy map: tramos pintados sobre el trazado ({n_lines_on})")
    dp.derate_check.setChecked(False)
    dp.coast_check.setChecked(False)
    n_lines_off = sum(1 for i in dp._overlay
                      if type(i).__name__ == "PlotDataItem")
    check(n_lines_off == 0,
          "deploy map: ambos tildes destildados limpian el trazado")
    dp.derate_check.setChecked(True)
    dp.coast_check.setChecked(True)
    bal = win.analysis_views["an_battery"]
    bal.refresh()
    check(bal.table.rowCount() >= 1, "analysis: tabla de batería con filas")
    n_b0 = len(bal.p_charge.listDataItems())
    bal.trend_check.setChecked(True)
    bal.refresh()
    check(len(bal.p_charge.listDataItems()) > n_b0,
          "battery: tendencias agregadas")
    bal.trend_check.setChecked(False)
    bal.refresh()
    grip = win.analysis_views["an_grip"]
    grip.refresh()
    # tendencias en Grip degradation (y Deploy & Coast): tilde + tipo
    # (el panel está cerrado: _dirty difiere el redraw al refresh visible)
    n_g0 = len(grip.p_g.listDataItems())
    grip.trend_check.setChecked(True)
    grip.refresh()
    check(len(grip.p_g.listDataItems()) > n_g0,
          f"grip: tendencias agregadas ({len(grip.p_g.listDataItems())})")
    grip.trend_combo.setCurrentIndex(2)  # exponencial: no debe crashear
    grip.refresh()
    grip.trend_check.setChecked(False)
    grip.refresh()
    check(len(grip.p_g.listDataItems()) == n_g0,
          "grip: tendencias ocultadas")
    dp.trend_check.setChecked(True)
    dp.refresh()
    dp.trend_check.setChecked(False)
    dp.refresh()
    gf = win.analysis_views["an_gforce"]
    win.analysis_launcher.buttons["an_gforce"].setChecked(True)
    win._panels["map"].set_panel_visible(True)
    pump(app, 0.2)
    gf.refresh()
    # sincronización de hover: un punto de pista se refleja en todos
    win._on_analysis_hover(1000.0)
    check(gf._hover_lines[0].isVisible() and gf.map_probe.isVisible()
          and dp.map_probe.isVisible(),
          "analysis: hover sincronizado entre gráficos y mapas")
    check(win.track_map.probe_marker.isVisible(),
          "analysis: hover reflejado en el track map principal")
    win._on_analysis_hover(None)
    check(not gf.map_probe.isVisible() and not dp.map_probe.isVisible(),
          "analysis: hover None limpia los marcadores")
    win.analysis_launcher.buttons["an_gg"].setChecked(False)
    pump(app, 0.2)
    check(not win._panels["an_gg"].is_panel_visible(),
          "analysis: cierre desde el lanzador sincroniza")

    # panel Acceleration: puntos v vs G y tendencias opcionales
    win.analysis_launcher.buttons["an_accel"].setChecked(True)
    pump(app, 0.2)
    ac = win.analysis_views["an_accel"]
    ac.refresh()
    check(len(ac._items) >= 1 and ac.table.rowCount() >= 1,
          "accel: nube de puntos y tabla")
    # solo aceleración: sin puntos de frenada (G negativa)
    sc0 = next(i for i in ac._items
               if type(i).__name__ == "ScatterPlotItem")
    _sx, sy = sc0.getData()
    check(float(min(sy)) >= 0.0,
          f"accel: sin frenada en la nube (min {float(min(sy)):.2f}G)")
    n_before = len(ac._items)
    ac.trend_check.setChecked(True)
    ac.refresh()
    check(len(ac._items) > n_before and any(
        type(i).__name__ == "PlotDataItem" for i in ac._items),
        "accel: líneas de tendencia agregadas")
    ac.trend_combo.setCurrentIndex(2)  # exponencial: no debe crashear
    ac.refresh()
    check(any(type(i).__name__ == "PlotDataItem" for i in ac._items),
          "accel: tendencia exponencial dibujada")
    ac.trend_check.setChecked(False)
    ac.refresh()

    # Lap Compare histórico: sets piloto→vuelta con target para el delta
    check(win._panels["quali_view"].title == "Lap Compare - Live",
          "lap compare: panel en vivo renombrado")
    win._panels["lap_compare"].set_panel_visible(True)
    pump(app, 0.2)
    lc = win.lap_compare_view
    lc.refresh()
    check(lc.driver_combo.count() == win.driver_list.count()
          and lc.lap_combo.count() >= 3,
          f"lap compare: combos poblados ({lc.driver_combo.count()} "
          f"pilotos, {lc.lap_combo.count()} vueltas)")
    lc._add_clicked()
    lc.lap_combo.setCurrentIndex(0)
    lc._add_clicked()
    lc.driver_combo.setCurrentIndex(1)
    lc._add_clicked()
    check(len(lc.entries) == 3 and lc.target == 0,
          f"lap compare: 3 sets agregados ({lc.entries})")
    check(len(lc.p_chan.listDataItems()) == 3
          and len(lc.p_delta.listDataItems()) == 2,
          "lap compare: 3 trazas y 2 deltas contra el target")
    lc.sets_list.setCurrentRow(2)
    lc._target_clicked()
    check(lc.target == 2 and "🎯" in lc.sets_list.item(2).text(),
          "lap compare: target cambiado al tercer set")
    lc.sets_list.setCurrentRow(0)
    lc._remove_clicked()
    check(len(lc.entries) == 2 and lc.target == 1,
          "lap compare: quitar un set reubica el target")
    idx_g = lc.channel_combo.findData("gear")
    lc.channel_combo.setCurrentIndex(idx_g)
    check(len(lc.p_chan.listDataItems()) == 2,
          "lap compare: cambio de canal redibuja")
    # correlación gráfico <-> pista en ambos sentidos
    win._on_analysis_hover(800.0)
    check(lc._hover_lines[0].isVisible() and lc._hover_lines[1].isVisible(),
          "lap compare: hover ajeno marca la línea en sus gráficos")
    lc.hover_dist_cb(600.0)
    check(win.track_map.probe_marker.isVisible(),
          "lap compare: hover propio marca el punto en el track map")
    lc.hover_dist_cb(None)
    check(not lc._hover_lines[0].isVisible(),
          "lap compare: hover None limpia la línea")

    # Pit lane map: recorrido por los dos carriles con un auto sintético
    from f1telem.ui.pitlane_map import PitlaneMapView as _PLM
    hub_pm = _DH()
    hub_pm.on_track_length(3000.0)
    hub_pm.on_tyres({"9": {1: ("SOFT", 3), 2: ("HARD", 1)}})
    plm = _PLM(hub_pm)
    plm.smooth = False  # los saltos sintéticos de datos van sin animación

    def _seg(t0, t1, d0, v_ms, spd):
        out, t = [], t0
        while t < t1 - 1e-9:
            d = d0 + v_ms * (t - t0)
            out.append(_S("9", t, 1, d, d, spd, 0.0, 0.0, 0.0, 0, 0))
            t += 0.5
        return out

    # llega a 180 km/h, entra a la calle en t=10 (dist 500) a 60 km/h
    hub_pm.on_batch(_seg(0.0, 10.0, 0.0, 50.0, 180.0)
                    + _seg(10.0, 12.01, 500.0, 16.67, 60.0))
    hub_pm.pit_lane["9"] = [[1, 10.0, None]]
    plm.refresh()
    check(len(plm.cars) == 1 and not plm.cars[0].stopped
          and 0.15 <= plm.cars[0].frac <= 0.35
          and 1.5 <= plm.cars[0].lane_s <= 2.5
          and plm.cars[0].tyre == "SOFT",
          f"pit map: circulando con ruedas de entrada "
          f"(frac {plm.cars[0].frac:.2f}, {plm.cars[0].lane_s:.1f}s)")
    # se clava a 0 km/h: carril de detención, reloj de parada corriendo
    hub_pm.on_batch(_seg(12.0, 14.0, 533.3, 16.67, 60.0)
                    + _seg(14.0, 16.01, 566.7, 0.0, 0.0))
    plm.refresh()
    check(plm.cars[0].stopped and 0.35 <= plm.cars[0].frac <= 0.65
          and 1.4 <= plm.cars[0].stop_s <= 2.6,
          f"pit map: detenido en el carril de boxes "
          f"({plm.cars[0].stop_s:.1f}s)")
    check(plm.cars[0].tyre == "SOFT",
          "pit map: durante la detención siguen las gomas de entrada")
    # reanuda: vuelve al carril de circulación con las gomas nuevas y el
    # tiempo de detención congelado
    hub_pm.on_batch(_seg(16.0, 17.0, 566.7, 0.0, 0.0)
                    + _seg(17.0, 19.51, 566.7, 16.67, 60.0))
    plm.refresh()
    check(not plm.cars[0].stopped and plm.cars[0].frac > 0.65
          and 2.5 <= plm.cars[0].stop_s <= 3.5
          and plm.cars[0].tyre == "HARD",
          f"pit map: reanudó con gomas de salida y parada congelada "
          f"({plm.cars[0].stop_s:.1f}s, {plm.cars[0].tyre})")
    plm.canvas.grab()  # el pintado no debe crashear
    # animación: con reloj de pared simulado, lotes de 0.5 s de datos a
    # cadencia real — la posición reproduce con retraso, avanza monótona
    # y nunca alcanza de golpe al último dato
    fake_wall = [1000.0]
    plm._now = lambda: fake_wall[0]
    plm.smooth = True
    plm._tsm.clear()
    plm.refresh()  # inicializa el reloj de reproducción en el estado actual
    f0 = plm.cars[0].frac
    fracs = []
    for k in range(4):
        t0 = 19.5 + 0.5 * k
        hub_pm.on_batch(_seg(t0, t0 + 0.51,
                             608.4 + 16.67 * (t0 - 19.5), 16.67, 60.0))
        fake_wall[0] += 0.5
        plm.refresh()
        fracs.append(plm.cars[0].frac)
    plm.smooth = False
    plm._now = time.monotonic
    plm.refresh()
    f_raw = plm.cars[0].frac
    steps = [b - a for a, b in zip([f0] + fracs, fracs)]
    check(all(s >= -1e-9 for s in steps) and fracs[-1] > f0 + 0.03
          and fracs[-1] <= f_raw + 1e-9 and fracs[0] < f_raw - 0.02,
          f"pit map: animación retrasada y progresiva "
          f"({f0:.2f} → {[round(f, 2) for f in fracs]} → {f_raw:.2f})")
    # dos autos detenidos juntos: las etiquetas no deben superponerse
    # (de-conflicto por fila) — se ejercita el pintado con ambos parados
    hub_pm.on_tyres({"9": {1: ("SOFT", 3), 2: ("HARD", 1)},
                     "10": {1: ("MEDIUM", 5)}})
    hub_pm.on_batch([_S("10", t, 1, 560.0, 560.0, 0.0, 0.0, 0.0, 0.0, 0, 0)
                     for t in [12.0 + k * 0.5 for k in range(20)]])
    hub_pm.pit_lane["10"] = [[1, 12.0, None]]
    plm.refresh()
    plm.canvas.grab()
    check(len(plm.cars) == 2, "pit map: dos autos en la calle a la vez")
    # filtro 👥 propio (persistible): ocultar un auto lo saca del mapa
    item_pm = next(plm.filter_btn.list.item(i)
                   for i in range(plm.filter_btn.list.count())
                   if plm.filter_btn.list.item(i).data(Qt.UserRole) == "10")
    item_pm.setCheckState(Qt.Unchecked)
    check(len(plm.cars) == 1 and all(c.drv != "10" for c in plm.cars),
          "pit map: filtro 👥 oculta al piloto")
    item_pm.setCheckState(Qt.Checked)
    check(len(plm.cars) == 2, "pit map: filtro 👥 restaurado")
    plm.canvas.grab()  # pintado completo con garajes/entrada/salida
    # visita cerrada: fuera del mapa
    hub_pm.pit_lane["9"][0][2] = 20.0
    plm.refresh()
    check(all(c.drv != "9" for c in plm.cars),
          "pit map: al salir de la calle desaparece")
    win._panels["pitlane_map"].set_panel_visible(True)
    pump(app, 0.2)
    win.pitlane_map_view.refresh()
    win.pitlane_map_view.canvas.grab()
    check(win.pitlane_map_view.cars == [],
          "pit map: en el demo sin visitas abiertas queda vacío")

    # gestor de notificaciones: los eventos del demo quedaron en el log
    kinds_logged = {k for _s, k, _c, _t in win.notifier.log}
    check({"pit_in", "pit_out", "yellow", "sc"} <= kinds_logged,
          f"notificaciones: eventos del demo registrados ({sorted(kinds_logged)})")
    win._panels["notifications"].set_panel_visible(True)
    pump(app, 0.2)
    win.notifications_view.refresh()
    check(win.notifications_view.list.count() == len(win.notifier.log),
          f"notificaciones: panel refleja el log ({win.notifications_view.list.count()})")

    # pit strategy: Ventana de Box editable con traba + proyección de rejoin
    from f1telem.ui.pit_strategy import project_rejoin
    win._panels["pit_strategy"].set_panel_visible(True)
    pump(app, 0.2)
    ps = win.pit_strategy_view
    ps.lock_check.setChecked(True)
    ps.window_spin.setValue(20.0)
    ps.apply_auto(5.0, 2, 9)
    check(abs(ps.window_spin.value() - 20.0) < 1e-9,
          "ventana de box: la traba impide que el cálculo automático la pise")
    ps.lock_check.setChecked(False)
    ps.apply_auto(6.5, 2, 9)
    check(abs(ps.window_spin.value() - 6.5) < 1e-9
          and "6.5" in ps.auto_label.text(),
          f"ventana de box: sin traba el automático aplica ({ps.auto_label.text()})")
    ps.window_spin.setValue(20.0)
    ps._last_table = 0.0
    ps.refresh()
    check(ps.table.rowCount() == 6,
          f"undercut: una fila por auto ({ps.table.rowCount()})")
    check(ps.table.item(0, 3) is not None
          and ps.table.item(0, 3).text().startswith("P"),
          f"undercut: posición proyectada ({ps.table.item(0, 3).text()!r})")
    ordered_ps, gaps_ps = ps.current_gaps()
    proj_big = project_rejoin(gaps_ps, ordered_ps[0], 999.0)
    n_classif = sum(1 for g in gaps_ps.values() if g is not None)
    check(proj_big is not None and proj_big[0] == n_classif,
          f"undercut: ventana enorme manda al líder al fondo (P{proj_big[0]})")
    # gráfico de reinserción: chips [adelante] ─s─ [propio] ─s─ [atrás]
    from f1telem.ui.pit_strategy import _RejoinGraphic
    check(ps.table.columnCount() == 6, "rejoin: columnas Behind/Margin "
          "reemplazadas por el gráfico")
    g0 = ps.table.cellWidget(0, 5)
    check(isinstance(g0, _RejoinGraphic) and g0._data is not None
          and g0._data[0] is not None,
          "rejoin: gráfico con datos en la fila del líder")
    with_sides = [ps.table.cellWidget(i, 5) for i in range(ps.table.rowCount())]
    check(any(w._data and (w._data[1] or w._data[2]) for w in with_sides),
          "rejoin: al menos una fila con vecinos y márgenes")
    tt = next((w.toolTip() for w in with_sides
               if w._data and (w._data[1] or w._data[2])), "")
    check("pits now" in tt and ("behind" in tt or "ahead of" in tt),
          f"rejoin: tooltip explica la reinserción ({tt!r})")

    # parada "gratis" (gap con el de atrás > Ventana de Box + 1 s): con
    # una ventana enorme solo el último la tiene, en torre y en strategy
    ps.window_spin.setValue(120.0)
    win.tower.refresh()
    frees = [r.free_stop for r in win.tower.rows]
    check(frees[-1] and sum(frees) == 1,
          f"free stop: solo el último con ventana enorme ({frees})")
    check("FREE stop" in win.tower._row_tooltip(win.tower.rows[-1]),
          "free stop: tooltip de la torre lo explica")
    ps._last_table = 0.0
    ps.refresh()
    marks = [ps.table.item(i, 4).text().endswith("✓")
             for i in range(ps.table.rowCount())]
    check(marks[-1] and sum(marks) == 1,
          f"free stop: tag ✓ en pit strategy solo en el último ({marks})")
    check("FREE" in ps.table.item(ps.table.rowCount() - 1, 4).toolTip(),
          "free stop: tooltip del ✓ en pit strategy")
    ps.window_spin.setValue(20.0)
    win.tower.refresh()
    check(win.tower.rows[-1].free_stop,
          "free stop: el último siempre puede parar gratis")

    # estado del capturador en el hub (sandbox: no hay capturador)
    win._update_capturer_status()
    check(win.capturer_status.text() == "Capturer: not running",
          f"hub: estado del capturador ({win.capturer_status.text()})")

    # opacidad de overlays fijados (persistida por ventana)
    tower_win = win._panels["tower"]._win
    tower_win.pin_btn.setChecked(True)
    tower_win.opacity_slider.setValue(70)
    check(abs(tower_win.windowOpacity() - 0.7) < 0.05,
          "overlay: opacidad aplicada al fijar")
    st_op = win._panels["tower"].save_state()
    check(st_op.get("opacity") == 70, "overlay: opacidad persistida")
    tower_win.pin_btn.setChecked(False)
    check(abs(tower_win.windowOpacity() - 1.0) < 0.05,
          "overlay: al des-fijar vuelve opaco")
    tower_win.opacity_slider.setValue(100)

    # regla de la línea de tiempo: preview y salto a incidentes
    ruler = LapRuler(lambda t: None)
    ruler.resize(400, 18)
    ruler.set_range(0.0, 100.0)
    ruler.set_marks([(1, 0.0), (2, 50.0)])
    ruler.set_status([(60.0, 70.0, "4")])
    hint = ruler.hint_at(65.0)
    check("L2" in hint and "SAFETY" in hint,
          f"regla: preview con vuelta y estado ({hint!r})")
    check(ruler._target_for_click(65.0) == 60.0,
          "regla: click en banda salta al inicio del incidente")
    check(ruler._target_for_click(30.0) == 50.0,
          "regla: click normal va a la vuelta más cercana")

    # rueda de vuelta: ángulo por auto (norte = meta), sectores y fantasma
    win._catalog_checks["lap_wheel"].setChecked(True)
    pump(app, 0.6)
    lw = win.lap_wheel
    check(len(lw._dots) == 6, f"rueda: un punto por auto ({len(lw._dots)})")
    lw_item = lw.filter_btn.list.item(0)
    lw_item.setCheckState(Qt.Unchecked)
    check(len(lw._dots) == 5, f"rueda: filtro 👥 oculta el auto ({len(lw._dots)})")
    lw_item.setCheckState(Qt.Checked)
    check(len(lw._dots) == 6, "rueda: filtro 👥 restaurado")
    # fuera de carrera: sin punto y sin intervalos contra ese auto
    win.hub.last_move_t[first] = -1e9
    lw.refresh()
    check(first not in lw._dots and len(lw._dots) == 5,
          "rueda: fuera de carrera sin punto en la rueda")
    check(all(first not in (iv[0], iv[1]) for iv in lw._intervals),
          "rueda: sin intervalos contra un auto fuera de carrera")
    win.hub.last_move_t[first] = win.hub.latest_t
    lw.refresh()
    check(len(lw._dots) == 6, "rueda: restaurado tras reaparecer datos")
    # modo elástico (⏱ Gap): líder en el norte, ángulos crecen con el orden
    win.tower.refresh()
    lw.elastic_btn.click()
    check(lw.elastic() and bool(win.cfg["ui"].get("wheel_elastic")),
          "rueda: modo gap activado y persistido")
    lead_drv = win.tower.rows[0].drv
    check(lead_drv in lw._dots and lw._dots[lead_drv][0] < 1.0,
          f"rueda: líder en el norte en modo gap "
          f"({lw._dots.get(lead_drv, (99,))[0]:.1f}°)")
    angs = [lw._dots[r.drv][0] for r in win.tower.rows if r.drv in lw._dots]
    check(all(b >= a for a, b in zip(angs, angs[1:])),
          f"rueda: ángulos por gap siguen el orden de carrera "
          f"({[f'{a:.0f}' for a in angs]})")
    lw.elastic_btn.click()
    check(not lw.elastic(), "rueda: vuelta al modo físico")

    # Track dominance: cada µ pintado con el color del más rápido
    win._catalog_checks["dominance"].setChecked(True)
    pump(app, 0.4)
    dv = win.dominance_view
    dv._invalidate()
    n_mu_dom = win.tower.analyzer.n_micro()
    check(len(dv._seg_items) == n_mu_dom == sum(dv.counts.values()),
          f"dominance: todos los µ pintados "
          f"({len(dv._seg_items)}/{n_mu_dom})")
    check(len(dv.counts) >= 1 and all(v > 0 for v in dv.counts.values()),
          f"dominance: leyenda con dominadores ({dv.counts})")
    check(len(dv._label_items) >= 1,
          f"dominance: iniciales sobre las zonas ({len(dv._label_items)})")
    top_dom = max(dv.counts, key=dv.counts.get)
    item_dom = next(dv.filter_btn.list.item(i)
                    for i in range(dv.filter_btn.list.count())
                    if dv.filter_btn.list.item(i).data(Qt.UserRole) == top_dom)
    item_dom.setCheckState(Qt.Unchecked)
    check(top_dom not in dv.counts
          and sum(dv.counts.values()) == n_mu_dom,
          "dominance: sin el dominador, sus µ pasan a los demás")
    item_dom.setCheckState(Qt.Checked)
    dv.from_spin.setValue(900)  # rango imposible: mapa sin pintar
    check(not dv._seg_items and not dv._label_items
          and "no timed laps" in dv.legend.text(),
          "dominance: rango sin vueltas queda vacío")
    dv.from_spin.setValue(0)
    check(sum(dv.counts.values()) == n_mu_dom,
          "dominance: rango restaurado repinta todo")
    # pausar el demo: los relojes suavizados convergen al último dato y la
    # comparación deja de depender de la carga de la máquina
    win.source.set_paused(True)
    pump(app, 1.3)
    lw.refresh()
    L_w = win.hub.track_length
    worst_deg = 0.0
    for drv, (angle, _code, _color) in lw._dots.items():
        real = float(win.hub.buffers[drv].col("dist_lap")[-1]) % L_w / L_w * 360.0
        diff = abs((angle - real + 180.0) % 360.0 - 180.0)
        worst_deg = max(worst_deg, diff)
    win.source.set_paused(False)
    check(worst_deg < 30.0,
          f"rueda: ángulos coherentes con la posición real (peor {worst_deg:.1f}°)")
    b1_deg, b2_deg = lw._sector_bounds_deg()
    check(0.0 < b1_deg < b2_deg < 360.0,
          f"rueda: límites de sector ({b1_deg:.0f}° / {b2_deg:.0f}°)")
    check(len(win.hub.corners) == 8, "rueda: curvas disponibles para pintar")
    lw.sim_combo.setCurrentIndex(lw.sim_combo.findData(first))
    win.cfg.setdefault("strategy", {})["pit_window"] = 20.0
    lw.refresh()
    check(lw._ghost is not None and lw._ghost[0] == first,
          "rueda: fantasma de parada para el piloto elegido")
    car_angle = lw._dots[first][0]
    ghost_angle = lw._ghost[1]
    behind_deg = (car_angle - ghost_angle) % 360.0
    check(10.0 < behind_deg < 200.0,
          f"rueda: el fantasma cae detrás del auto ({behind_deg:.0f}°)")
    check(lw.result_label.text().startswith(win.hub.drivers[first].code)
          and "→ P" in lw.result_label.text(),
          f"rueda: proyección de posición ({lw.result_label.text()!r})")
    check(lw._ghost_sm is not None, "rueda: fantasma con motor de reproducción")
    # anillo interno: un intervalo entre cada par de autos consecutivos
    check(len(lw._intervals) == 5,
          f"rueda: intervalos entre autos consecutivos ({len(lw._intervals)})")
    check(all(secs > 0 for _b, _a, secs, _ba, _sp in lw._intervals),
          "rueda: intervalos positivos (de atrás hacia adelante)")
    _ordered_w, gaps_w = ps.current_gaps()
    last_gap = gaps_w.get(_ordered_w[-1])
    if last_gap is not None:
        total_int = sum(secs for _b, _a, secs, _ba, _sp in lw._intervals)
        check(abs(total_int - last_gap) < 0.6,
              f"rueda: la suma de intervalos cierra con el gap total "
              f"({total_int:.2f} vs {last_gap:.2f})")
    for _b, _a, _secs, b_ang, span in lw._intervals:
        check(0.0 <= b_ang < 360.0 and 0.0 <= span <= 360.0,
              "rueda: geometría de arco válida")
        break
    check(lw._pit_arc is not None and 0.0 <= lw._pit_arc[0] < 360.0
          and lw._pit_arc[1] > 0.0,
          f"rueda: tramo de boxes dibujado ({lw._pit_arc})")
    # amarillas por sector pintadas como arcos (igual que el mapa)
    win.hub.on_sector_yellows([(0.0, float("inf"), 2600.0, 3200.0)])
    arcs = lw._active_yellows_deg()
    exp0 = 2600.0 / L_w * 360.0
    exp1 = 3200.0 / L_w * 360.0
    check(len(arcs) == 1 and abs(arcs[0][0] - exp0) < 1.0
          and abs(arcs[0][1] - exp1) < 1.0,
          f"rueda: arco amarillo del sector con bandera ({arcs})")
    win.hub.on_sector_yellows([])
    lw.sim_combo.setCurrentIndex(0)
    win._catalog_checks["lap_wheel"].setChecked(False)
    pump(app, 0.2)

    # race trace: gap por microsector contra referencia elegible
    win._catalog_checks["race_trace"].setChecked(True)
    pump(app, 0.3)
    tc = win.chart_trace
    tc._dirty = True
    tc.refresh()
    sel = win._selected_drivers()
    visible = [d for d, c in tc._curves.items() if c.isVisible()]
    check(sorted(visible) == sorted(sel),
          f"race trace: una curva por piloto seleccionado ({len(visible)})")
    tx0, ty0 = tc._curves[sel[0]].getData()
    step = tc._checkpoint_step()
    check(tx0 is not None and len(tx0) > 50
          and abs((tx0[1] - tx0[0]) * win.hub.track_length - step) < 1.0,
          f"race trace: un punto por microsector ({0 if tx0 is None else len(tx0)} pts)")
    check(len(tc._status_items) >= 1, "race trace: bandas de SC/bandera")
    # tooltip de cursor: gap por piloto en el X del mouse (1 decimal)
    tvb = tc.plot.getViewBox()
    (tx0, tx1), (ty0, ty1) = tvb.viewRange()
    x_mid = tx0 + (tx1 - tx0) * 0.6
    tc._probe._on_move(tvb.mapViewToScene(QPointF(x_mid, (ty0 + ty1) / 2)))
    check(len(tc._probe.rows) == len(sel),
          f"race trace: tooltip con el gap de cada piloto ({len(tc._probe.rows)})")
    gaps = [y for _lbl, y in tc._probe.rows]
    check(gaps == sorted(gaps), "race trace: tooltip en orden de carrera")
    check("+0.0 s" in tc._probe.label.toHtml() or "-0.0 s" in tc._probe.label.toHtml()
          or f"{gaps[0]:+.1f} s" in tc._probe.label.toHtml(),
          "race trace: gaps con 1 decimal en segundos")
    tc._probe._hide()
    idx_ref = tc.ref_combo.findData(sel[0])
    tc.ref_combo.setCurrentIndex(idx_ref)
    pump(app, 0.2)
    _rx, ry = tc._curves[sel[0]].getData()
    check(ry is not None and len(ry) and float(np.max(np.abs(ry))) < 1e-6,
          "race trace: la referencia es su propia línea de cero")
    tc.x_spin.setValue(2)
    tc.y_spin.setValue(10.0)
    pump(app, 0.2)
    (xr0, xr1), (yr0, yr1) = tc.plot.getViewBox().viewRange()
    check(abs((xr1 - xr0) - 2.0) < 0.25, f"race trace: rango X en vueltas ({xr1 - xr0:.2f})")
    check(abs(yr0 + 10.0) < 0.5 and abs(yr1 - 10.0) < 0.5,
          f"race trace: rango Y ±s ({yr0:.1f}..{yr1:.1f})")
    tc.x_spin.setValue(0)
    tc.y_spin.setValue(0.0)
    tc.ref_combo.setCurrentIndex(0)
    win._catalog_checks["race_trace"].setChecked(False)
    pump(app, 0.2)

    # perfiles de ventanas: aplicar restaura el set completo con geometría
    win._panels["map"].set_panel_visible(False)
    win.cfg.setdefault("layouts", {})["smoke"] = {
        "visible": {"race_chart": True, "tower": True, "map": True,
                    "session": True, "race2_chart": True,
                    "quali_view": True, "times_gap": True},
        "float": {"tower": {"floating": True, "visible": True,
                            "geom": [60, 60, 420, 520], "pinned": True}},
        "win_max": False,
    }
    win._reload_profiles()
    win._apply_layout_profile("smoke")
    pump(app, 0.3)
    check(win._panels["map"].is_panel_visible(), "perfil: ventana reabierta")
    check(win._panels["tower"].pinned
          and win._panels["tower"]._win.geometry().height() == 520,
          "perfil: geometría y fijado restaurados")
    check(not win._panels["race_trace"].is_panel_visible(),
          "perfil: ventana fuera del perfil queda cerrada")
    win._panels["tower"]._win.pin_btn.setChecked(False)
    win._delete_layout_profile("smoke")
    check("smoke" not in win.cfg.get("layouts", {}), "perfil: borrado")
    pump(app, 0.2)

    # pausa y velocidad en caliente
    win.speed_combo.setCurrentIndex(win.speed_combo.findData(10.0))
    pump(app, 0.2)
    check(abs(win.source.speed - 10.0) < 1e-9, f"velocidad en caliente ({win.source.speed:g})")
    win.source.set_paused(True)
    pump(app, 0.4)
    n0 = win.hub.total_samples
    pump(app, 0.6)
    check(win.hub.total_samples == n0, "pausa congela la reproducción")
    win.source.set_paused(False)
    pump(app, 0.6)
    check(win.hub.total_samples > n0, "reanudar continúa la reproducción")
    win.speed_combo.setCurrentIndex(win.speed_combo.findData(25.0))
    pump(app, 0.2)

    # desconexión limpia
    win.connect_btn.click()
    pump(app, 0.5)
    check(win.source is None, "desconexión limpia")

    # cursor de reproducción: sin muestras nuevas sigue barriendo lo recibido
    # hasta consumirlo (la punta existe, o la curva ya llegó al último dato)
    pump(app, 0.4)
    tip_curve = win.chart_rolling._tips.get(first)
    tx, ty = tip_curve.getData() if tip_curve is not None else (None, None)
    cx, _cy = win.chart_rolling.curves[first].getData()
    xy_first = win.chart_rolling._xy.get(first)
    consumed = (xy_first is not None and cx is not None
                and len(cx) == len(xy_first[0]))
    sweeping = tx is not None and len(tx) == 2 and float(tx[1]) > float(tx[0])
    check(sweeping or consumed,
          f"punta de reproducción barre o consumió todo ({'-' if tx is None or len(tx) < 2 else f'{float(tx[1] - tx[0]):.0f} m'})")

    # fuente Capture: el visualizador gestiona el capturador — acá se simula
    # uno ya corriendo (heartbeat fresco, sin spawn) y se verifica que la
    # conexión ocurre sola recién cuando empiezan a fluir datos
    import tempfile
    from f1telem import config as f1cfg
    from f1telem.sources.capture import CaptureSource as _CapSrc
    os.environ["F1TELEM_NO_CAPTURE_SPAWN"] = "1"
    # sandbox: el recordings real puede tener una captura activa ahora mismo
    _old_lad = os.environ.get("LOCALAPPDATA")
    os.environ["LOCALAPPDATA"] = tempfile.mkdtemp()
    lock = f1cfg.capture_lock_path()
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("1")
    win.source_combo.setCurrentIndex(win.source_combo.findData("capture"))
    pump(app, 0.1)
    check(win.connect_btn.text() == "Open capturer",
          f"captura: el botón dice qué hace ({win.connect_btn.text()!r})")
    win.connect_btn.click()
    pump(app, 0.4)
    check(win.source is None and win._cap_waiting
          and win.connect_btn.text() == "Cancel",
          "captura: sin datos queda esperando al capturador")
    check("aiting" in win.status_label.text(),
          f"captura: estado de espera ({win.status_label.text()[:60]})")
    check(f1cfg.capture_show_path().exists(),
          "captura: pedido de mostrar al capturador en bandeja")
    f1cfg.capture_show_path().unlink(missing_ok=True)
    win.connect_btn.click()  # cancelar la espera
    pump(app, 0.2)
    check(not win._cap_waiting and win.connect_btn.text() == "Open capturer"
          and win.source is None, "captura: espera cancelable")
    win.connect_btn.click()  # esperar de nuevo
    pump(app, 0.3)
    cap_path = f1cfg.recordings_dir() / "capture_wait_test.jsonl"
    with open(cap_path, "w", encoding="utf-8") as f:
        f.write(json.dumps({"R": {"DriverList": {"1": {
            "RacingNumber": "1", "Tla": "VER", "TeamColour": "3671C6"}}}}) + "\n")
        data = _zpack({"Entries": [{
            "Utc": "2026-07-06T14:00:00.0000000Z",
            "Cars": {"1": {"Channels": {"0": 11000, "2": 250, "3": 7,
                                        "4": 99, "5": 0, "45": 12}}},
        }]})
        f.write(json.dumps({"M": [{"H": "Streaming", "M": "feed",
                                   "A": ["CarData.z", data, ""]}]}) + "\n")
    deadline = time.monotonic() + 6.0
    while time.monotonic() < deadline and win.source is None:
        pump(app, 0.2)
    check(isinstance(win.source, _CapSrc),
          "captura: conexión automática al empezar a fluir datos")
    check(not win._cap_waiting and win.connect_btn.text() == "Disconnect",
          "captura: la espera termina al conectar")
    check("timeline" in win._catalog_checks
          and not win._panels["timeline"].is_panel_visible()
          and win.seek_row.isEnabled(),
          "timeline: panel del catálogo (cerrado por defecto), habilitado con captura")

    # watchdog: si el capturador arranca OTRO archivo (import) y el actual
    # queda quieto, el visualizador lo sigue solo (crecimiento sostenido)
    old_t = time.time() - 30
    os.utime(cap_path, (old_t, old_t))
    cap2 = f1cfg.recordings_dir() / "capture_wait_test2.jsonl"
    cap2.write_text(json.dumps({"R": {"DriverList": {"1": {
        "RacingNumber": "1", "Tla": "VER", "TeamColour": "3671C6"}}}}) + "\n",
        encoding="utf-8")
    for _k in range(3):
        with open(cap2, "a", encoding="utf-8") as f:
            f.write(json.dumps({"M": [{"H": "Streaming", "M": "feed",
                                       "A": ["Heartbeat", {}, ""]}]}) + "\n")
        win._poll_capture_follow()
        pump(app, 0.15)
    check(isinstance(win.source, _CapSrc) and win.source.path == str(cap2),
          f"captura: watchdog sigue al archivo nuevo ({Path(win.source.path).name})")
    win.connect_btn.click()  # desconectar
    pump(app, 0.4)
    lock.unlink()
    if _old_lad is not None:
        os.environ["LOCALAPPDATA"] = _old_lad

    win.close()
    pump(app, 0.3)


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    apply_theme(app)
    test_live_decoder()
    test_capture_source()
    test_gap_grid_offset()
    test_catch_projection()
    test_delta_wave()
    test_openf1()
    test_analysis_engine()
    test_preprocessing()
    test_quali_tower()
    test_app_demo(app)
    print()
    if FAILURES:
        print(f"{len(FAILURES)} fallas")
        return 1
    print("Todos los chequeos pasaron.")
    return 0


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    # sin exec(): el teardown por GC de Qt puede abortar el proceso, así que
    # salimos explícitamente una vez reportado el resultado
    os._exit(code)
