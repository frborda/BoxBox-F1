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
    # las tablas se refrescan a 2 Hz: esperar a que el resumen se pueble
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

    # ↺: los subpaneles internos ocultados con ✕ vuelven de fábrica
    tg_win = win._panels["times_gap"]._win
    tables = win._panels["times_tables"]
    check(tg_win.reset_btn.isVisible(), "ventana con subpaneles muestra ↺")
    tables._hide_docked()
    check(not tables.is_panel_visible(), "subpanel oculto con ✕")
    tg_win.reset_btn.click()
    check(tables.is_panel_visible() and not tables.floating,
          "↺ restaura el subpanel oculto")
    tables.detach()
    pump(app, 0.2)
    tg_win.reset_btn.click()
    check(tables.is_panel_visible() and not tables.floating,
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

    # subpanel interno: las tablas de Times/Gap flotadas aparte siguen
    # refrescando aunque su ventana madre esté cerrada
    tp = win.chart_timing.tables_panel
    tp.detach()
    pump(app, 0.3)
    check(tp.floating and win.chart_timing.tabs.isVisible(),
          "tablas de Times/Gap flotantes")
    win._catalog_checks["times_gap"].setChecked(False)
    pump(app, 1.2)
    check(win.chart_timing.summary_table.rowCount() > 0,
          "tablas flotantes se refrescan con su ventana madre cerrada")
    tp.attach()
    win._catalog_checks["times_gap"].setChecked(True)
    pump(app, 0.3)
    check(not tp.floating, "tablas reacopladas dentro de su ventana")

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
    check(len(win.pitlane_view.rows) == 0,
          "pit lane: vacío con todas las visitas cerradas")
    win.hub.pit_lane.setdefault(first, []).append(
        [9, win.hub.latest_t - 15.0, None])
    win.pitlane_view.refresh()
    win.tower.refresh()
    check(len(win.pitlane_view.rows) == 1, "pit lane: piloto adentro listado")
    _code, _color, compound, lane_s, _stop_s, _out = win.pitlane_view.rows[0]
    check(compound == "MEDIUM" and lane_s >= 14.5,
          f"pit lane: compuesto de entrada y reloj corriendo ({compound}, {lane_s:.1f}s)")
    # filtro 👥 propio: ocultar al piloto lo saca del panel
    item_pl = next(win.pitlane_view.filter_btn.list.item(i)
                   for i in range(win.pitlane_view.filter_btn.list.count())
                   if win.pitlane_view.filter_btn.list.item(i)
                   .data(Qt.UserRole) == first)
    item_pl.setCheckState(Qt.Unchecked)
    check(len(win.pitlane_view.rows) == 0, "pit lane: filtro 👥 oculta al piloto")
    item_pl.setCheckState(Qt.Checked)
    check(len(win.pitlane_view.rows) == 1, "pit lane: filtro 👥 restaurado")
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
    check(len(win.pitlane_view.rows) == 0,
          "pit lane: visita futura (replay) no lista al auto")
    row_f = next(r for r in win.tower.rows if r.drv == first)
    check(not row_f.pit_open and row_f.pit_lap == 4,
          "torre: visita futura no marca 'en calle ahora'")
    win.hub.pit_lane[first].pop()
    win.pitlane_view.refresh()

    # retención del que sale del pit: atenuado hasta fin de S2 o 2 minutos
    from f1telem.hub import DataHub as _DH
    from f1telem.models import Sample as _S
    from f1telem.ui.pitlane import PitlaneView as _PLV
    hub_pl = _DH()
    hub_pl.on_track_length(3000.0)  # sin bounds: fin de S2 = 2000 m
    plv = _PLV(hub_pl)
    hub_pl.on_batch([_S("9", k * 0.5, 1, 50.0 * (k * 0.5), 50.0 * (k * 0.5),
                        180.0, 0.0, 0.0, 0.0, 0, 0) for k in range(61)])
    hub_pl.pit_lane["9"] = [[1, 2.0, 6.0]]  # salió en t=6 (pos 300 m)
    plv.refresh()
    check(len(plv.rows) == 1 and plv.rows[0][5] is True
          and abs(plv.rows[0][3] - 4.0) < 1e-6,
          "pit lane: el que salió queda atenuado con relojes congelados")
    hub_pl.on_batch([_S("9", t, 1, 50.0 * t, 50.0 * t, 180.0,
                        0.0, 0.0, 0.0, 0, 0)
                     for t in [30.5 + k * 0.5 for k in range(24)]])  # 2100 m
    plv.refresh()
    check(not plv.rows, "pit lane: al cruzar el fin del S2 desaparece")
    hub_pl.on_batch([_S("10", k * 2.0, 1, 400.0 + 0.2 * (k * 2.0),
                        400.0 + 0.2 * (k * 2.0), 20.0, 0.0, 0.0, 0.0, 0, 0)
                     for k in range(51)])  # nunca llega al S2
    hub_pl.pit_lane["10"] = [[1, 2.0, 6.0]]
    plv.refresh()
    check(len(plv.rows) == 1 and plv.rows[0][5] is True,
          "pit lane: sin cruzar el S2 sigue retenido (< 2 min)")
    hub_pl.on_batch([_S("10", t, 1, 400.0 + 0.2 * t, 400.0 + 0.2 * t, 20.0,
                        0.0, 0.0, 0.0, 0, 0)
                     for t in [102.0 + k * 2.0 for k in range(15)]])
    plv.refresh()
    check(not plv.rows, "pit lane: a los 2 minutos expira la retención")

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
    L_w = win.hub.track_length
    worst_deg = 0.0
    for drv, (angle, _code, _color) in lw._dots.items():
        real = float(win.hub.buffers[drv].col("dist_lap")[-1]) % L_w / L_w * 360.0
        diff = abs((angle - real + 180.0) % 360.0 - 180.0)
        worst_deg = max(worst_deg, diff)
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
