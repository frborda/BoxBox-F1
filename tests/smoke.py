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
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
# la consola Windows (cp1252) no soporta Δ, →, −: degradar en vez de crashear
sys.stdout.reconfigure(errors="replace")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from f1telem.sources.live import LiveSource, decompress_feed
from f1telem.ui.main_window import MainWindow
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
    check(abs((hub_l1.provisional_lap1_offset("B") or -1) - 200.0) < 25.0,
          f"offset de grilla estimado por proyección ({hub_l1.provisional_lap1_offset('B')})")
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


def test_app_demo(app: QApplication) -> None:
    win = MainWindow()
    win.show()

    # conectar fuente demo a x25
    win.source_combo.setCurrentIndex(0)
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

    # modo Carrera (rolling)
    win.mode_combo.setCurrentIndex(0)
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

    # modo Carrera 2 (wrap)
    win.mode_combo.setCurrentIndex(1)
    pump(app, 4.0)
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

    # modo Qualy con referencia
    win.mode_combo.setCurrentIndex(2)
    pump(app, 0.5)
    win.ref_driver_combo.setCurrentIndex(win.ref_driver_combo.findData(first))
    win._refresh_ref_laps()
    check(win.ref_lap_combo.count() > 0, "combo de vueltas de referencia poblado")
    ref_text = win.ref_lap_combo.itemText(0)
    check(ref_text.startswith("Lap") and ":" in ref_text,
          f"referencia muestra tiempo de vuelta ({ref_text!r})")
    win.ref_set_btn.click()
    pump(app, 1.0)
    qv = win.chart_qualy
    rx, ry = qv.chart._ref_curve.getData()
    check(rx is not None and len(rx) > 50, f"Qualy: target dibujada ({0 if rx is None else len(rx)})")
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

    # modo Tiempos / Gap
    win.mode_combo.setCurrentIndex(3)
    pump(app, 1.5)
    tv = win.chart_timing
    ref = tv.ref_combo.currentData()
    other = next(d for d in sel if d != ref)
    gx, gy = tv.curves[other].getData()
    check(gx is not None and len(gx) > 50 and bool(np.isfinite(gy).all()),
          f"Gap: serie con datos finitos ({0 if gx is None else len(gx)})")
    check(abs(float(gy[-1])) < 120, f"Gap: magnitud razonable ({float(gy[-1]):+.2f} s)")
    check(tv.summary_table.rowCount() == len(sel)
          and ":" in tv.summary_table.item(0, 2).text(),
          f"Resumen: tiempos de vuelta poblados ({tv.summary_table.item(0, 2).text()})")
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

    # ventana X configurable (en vueltas) del gráfico de gap — combo global
    L = win.hub.track_length
    check(win.window_combo.isEnabled(), "combo de ventana habilitado en Tiempos/Gap")
    win.window_combo.setCurrentIndex(win.window_combo.findData(1.0))
    pump(app, 0.5)
    xr = tv.plot.getViewBox().viewRange()[0]
    width = xr[1] - xr[0]
    check(abs(width - L) < L * 0.02, f"Gap: ventana X de 1 vuelta aplicada ({width:.0f} m)")
    gx2, _ = tv.curves[other].getData()
    # el borde suavizado extrapola un poco entre lotes (mucho a x25)
    check(abs(xr[1] - float(gx2[-1])) < 900.0, "Gap: la ventana termina en la posición actual")
    win.window_combo.setCurrentIndex(win.window_combo.findData(0.0))
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

    # torre de tiempos
    check(win.tower.isVisible(), "torre de tiempos visible")
    win.tower.refresh()
    check(win.tower.table.rowCount() == 6, f"torre: todos los autos ({win.tower.table.rowCount()})")
    check(win.tower.table.item(0, 2).text() == "leader", "torre: fila 1 = líder")
    g2 = win.tower.table.item(1, 2).text()
    i2 = win.tower.table.item(1, 3).text()
    check(g2.startswith("+") and i2.startswith("+"), f"torre: gap e intervalo ({g2}, {i2})")
    last_txt = win.tower.table.item(0, 4).text()
    best_txt = win.tower.table.item(0, 5).text()
    check(":" in last_txt and ":" in best_txt,
          f"torre: última y mejor vuelta ({last_txt}, {best_txt})")
    pit_txt = win.tower.table.item(0, 6).text()
    check(pit_txt == "1", f"torre: contador de paradas ({pit_txt})")
    avg5_txt = win.tower.table.item(0, 7).text()
    avg10_txt = win.tower.table.item(0, 8).text()
    check(":" in avg5_txt and ":" in avg10_txt,
          f"torre: AVG5/AVG10 ({avg5_txt}, {avg10_txt})")
    headers = [win.tower.table.horizontalHeaderItem(c).text() for c in range(9)]
    check(headers == ["P", "Driver", "Gap", "Int", "Last", "Best", "Pit", "AVG5", "AVG10"],
          f"torre: orden de columnas pedido ({headers})")

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
    check(win.right_split.orientation() == Qt.Vertical
          and win.right_split.widget(0) is win.tower
          and win.right_split.widget(1) is win.track_map,
          "mapa del circuito debajo de la torre")

    # tooltip crosshair: valores de todas las series en el punto del mouse
    from PySide6.QtCore import QPointF
    win.mode_combo.setCurrentIndex(0)
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
    win.mode_combo.setCurrentIndex(3)
    pump(app, 0.5)
    vbg = tv.plot.getViewBox()
    (gx0, gx1), (gy0, gy1) = vbg.viewRange()
    tv._probe._on_move(vbg.mapViewToScene(QPointF((gx0 + gx1) / 2, (gy0 + gy1) / 2)))
    check(tv._probe.label.isVisible() and len(tv._probe.rows) >= 2,
          f"tooltip Gap: {len(tv._probe.rows)} series en el punto")

    # doble click sobre una línea la oculta; en zona vacía las restaura
    win.mode_combo.setCurrentIndex(0)
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
    check(len(pts) == len(sel), f"mapa: un punto por piloto seleccionado ({len(pts)})")
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
    win.map_check.setChecked(False)
    check(not mp.isVisible(), "mapa se oculta con el checkbox")
    win.map_check.setChecked(True)
    check(mp.isVisible(), "mapa se vuelve a mostrar")

    # cambio de canal (volviendo al modo Carrera, que es el que se refresca)
    win.channel_combo.setCurrentIndex(1)  # acelerador
    win.mode_combo.setCurrentIndex(0)
    pump(app, 1.5)
    x, y = win.chart_rolling.curves[first].getData()
    check(y is not None and float(max(y)) <= 105.0, "cambio de canal a acelerador aplicado")

    # valores en picos (máx de rectas / mín de curvas)
    win.channel_combo.setCurrentIndex(0)  # velocidad
    win.mode_combo.setCurrentIndex(0)
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

    # ventana X configurable del modo Carrera (por defecto 1 vuelta)
    win.mode_combo.setCurrentIndex(0)
    pump(app, 0.3)
    check(win.window_combo.isEnabled(), "combo de ventana habilitado en Carrera")
    win.window_combo.setCurrentIndex(win.window_combo.findData(2.0))
    pump(app, 0.5)
    xr = win.chart_rolling.getViewBox().viewRange()[0]
    expected = win.hub.track_length * 2.0 * (1 + win.chart_rolling.RIGHT_MARGIN_FRAC)
    check(abs((xr[1] - xr[0]) - expected) < win.hub.track_length * 0.05,
          f"Carrera: ventana de 2 vueltas aplicada ({xr[1] - xr[0]:.0f} m)")
    cx2, _cy2 = win.chart_rolling.curves[first].getData()
    check(float(cx2[-1] - cx2[0]) >= win.hub.track_length * 2.0,
          f"Carrera: datos cubren la ventana ampliada ({float(cx2[-1] - cx2[0]):.0f} m)")
    win.window_combo.setCurrentIndex(win.window_combo.findData(0.0))
    pump(app, 1.0)
    xr = win.chart_rolling.getViewBox().viewRange()[0]
    check(xr[0] <= 1.0 and xr[1] - xr[0] > win.hub.track_length * 3,
          f"Carrera: 'Todo' muestra desde el inicio ({xr[0]:.0f}..{xr[1]:.0f} m)")
    win.window_combo.setCurrentIndex(win.window_combo.findData(1.0))
    pump(app, 0.3)
    win.mode_combo.setCurrentIndex(1)
    pump(app, 0.2)
    check(not win.window_combo.isEnabled(), "combo de ventana deshabilitado en Carrera 2")
    win.mode_combo.setCurrentIndex(0)
    pump(app, 0.2)

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
    win.mode_combo.setCurrentIndex(0)
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

    # punta suavizada: sin muestras nuevas, la línea sigue creciendo con la
    # velocidad aprendida (segmento de punta extrapolado)
    pump(app, 0.4)
    tip_curve = win.chart_rolling._tips.get(first)
    tx, ty = tip_curve.getData() if tip_curve is not None else (None, None)
    check(tx is not None and len(tx) == 2 and float(tx[1]) > float(tx[0]),
          f"punta de serie extrapolada entre lotes ({'-' if tx is None else f'{float(tx[1] - tx[0]):.0f} m'})")
    win.close()
    pump(app, 0.3)


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    apply_theme(app)
    test_live_decoder()
    test_gap_grid_offset()
    test_catch_projection()
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
