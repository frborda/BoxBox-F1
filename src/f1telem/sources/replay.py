"""Fuente replay: reproduce una sesión histórica descargada con FastF1
como si estuviera ocurriendo en vivo (con multiplicador de velocidad).
"""
from __future__ import annotations

import time

import numpy as np
from PySide6.QtCore import Signal

from .. import config
from ..models import DriverInfo, Sample
from .base import BaseSource

_FALLBACK_COLORS = [
    "#3671C6", "#FF8000", "#E80020", "#27F4D2", "#229971",
    "#64C4FF", "#B6BABD", "#52E252", "#6692FF", "#C92D4B",
]


class ReplaySource(BaseSource):
    # línea de tiempo: aviso de limpieza antes de re-emitir la historia,
    # progreso (t_inicio, t_actual, t_fin) para el deslizador y marcas de
    # vuelta [(vuelta, t_inicio_de_vuelta)] para la regla
    seekReset = Signal()
    progress = Signal(float, float, float)
    lapMarks = Signal(object)

    def __init__(self, year: int, gp: str, session: str, speed: float = 1.0, parent=None):
        super().__init__(parent)
        self.year = int(year)
        self.gp = gp
        self.session_name = session
        self.speed = max(0.1, float(speed))
        self._seek_t: float | None = None
        self._paused = False

    def request_seek(self, t: float) -> None:
        """Salta a un tiempo de sesión (avanzar/retroceder en la tanda)."""
        self._seek_t = float(t)

    def set_speed(self, speed: float) -> None:
        self.speed = max(0.1, float(speed))  # el reloj acumulativo lo toma al vuelo

    def set_paused(self, paused: bool) -> None:
        self._paused = bool(paused)

    def run(self) -> None:
        try:
            stream = self._load()
        except Exception as exc:  # red, sesión inexistente, datos incompletos...
            self.failed.emit(f"Could not load the session: {exc}")
            return
        if stream is None or not len(stream["t"]):
            self.failed.emit("The session has no telemetry available.")
            return
        self._play(stream)

    # ------------------------------------------------------------------ carga

    def _load(self) -> dict | None:
        import fastf1

        cache = config.cache_dir()
        cache.mkdir(parents=True, exist_ok=True)
        fastf1.Cache.enable_cache(str(cache))

        self.statusChanged.emit(
            f"Loading {self.year} {self.gp} {self.session_name} "
            "(first run downloads data and may take several minutes)..."
        )
        session = fastf1.get_session(self.year, self.gp, self.session_name)
        session.load(laps=True, telemetry=True, weather=True, messages=True)
        if not self._running:
            return None

        infos: dict[str, DriverInfo] = {}
        for i, num in enumerate(session.drivers):
            code, name, team, color = str(num), "", "", _FALLBACK_COLORS[i % len(_FALLBACK_COLORS)]
            try:
                row = session.get_driver(num)
                if isinstance(row.get("Abbreviation"), str):
                    code = row["Abbreviation"]
                if isinstance(row.get("FullName"), str):
                    name = row["FullName"]
                if isinstance(row.get("TeamName"), str):
                    team = row["TeamName"]
                tc = row.get("TeamColor")
                if isinstance(tc, str) and len(tc) in (6, 7):
                    color = tc if tc.startswith("#") else f"#{tc}"
            except Exception:
                pass
            infos[str(num)] = DriverInfo(str(num), code, name, team, color)
        self.driversDiscovered.emit(infos)

        parts = []
        lap_lengths: list[float] = []
        first_lap_starts: list[float] = []
        for num in session.drivers:
            part = self._prepare_driver(session, str(num), lap_lengths, first_lap_starts)
            if part is not None:
                parts.append(part)
        if not parts:
            return None

        if lap_lengths:
            self.trackLength.emit(float(np.median(lap_lengths)))
        self._emit_outline(session)
        self._emit_lap_marks(session)
        self._emit_corners(session)
        self._emit_tyres(session)
        self._emit_pits(session)
        self._emit_track_status(session)
        self._emit_weather(session)
        self._emit_sector_yellows(session)
        self._emit_sector_times(session)
        self._emit_race_control(session)
        self._emit_session_meta(session)
        pos_stream = self._prepare_positions(session)

        stream = {
            key: np.concatenate([p[key] for p in parts])
            for key in parts[0]
        }
        order = np.argsort(stream["t"], kind="stable")
        stream = {key: val[order] for key, val in stream.items()}
        # arrancar poco antes de la primera vuelta (evita la hora de garage)
        stream["t_start"] = (min(first_lap_starts) - 5.0) if first_lap_starts else float(stream["t"][0])
        stream["drivers"] = [infos[n].number for n in sorted(infos)]
        stream["pos"] = pos_stream
        return stream

    def _prepare_positions(self, session) -> dict | None:
        """Stream global de posiciones (x, y) ordenado por tiempo de sesión."""
        parts = []
        for num in session.drivers:
            try:
                pd_ = session.pos_data[str(num)]
            except (KeyError, AttributeError):
                continue
            if pd_ is None or pd_.empty:
                continue
            t = pd_["SessionTime"].dt.total_seconds().to_numpy(dtype=np.float64)
            x = pd_["X"].to_numpy(dtype=np.float64)
            y = pd_["Y"].to_numpy(dtype=np.float64)
            ok = np.isfinite(t) & np.isfinite(x) & np.isfinite(y)
            if not ok.any():
                continue
            parts.append({
                "t": t[ok], "x": x[ok], "y": y[ok],
                "driver": np.full(int(ok.sum()), str(num), dtype=object),
            })
        if not parts:
            return None
        stream = {key: np.concatenate([p[key] for p in parts]) for key in parts[0]}
        order = np.argsort(stream["t"], kind="stable")
        return {key: val[order] for key, val in stream.items()}

    def _emit_lap_marks(self, session) -> None:
        """Inicio de cada vuelta (del primer auto que la arranca) para la
        regla de la línea de tiempo."""
        try:
            starts = (
                session.laps.dropna(subset=["LapStartTime"])
                .groupby("LapNumber")["LapStartTime"].min()
            )
            marks = sorted(
                (int(lap), float(ts.total_seconds())) for lap, ts in starts.items()
            )
            if marks:
                self.lapMarks.emit(marks)
        except Exception:
            pass  # sin marcas la línea de tiempo sigue funcionando

    def _emit_corners(self, session) -> None:
        """Curvas reales del circuito (número, distancia en la vuelta, x, y)."""
        try:
            info = session.get_circuit_info()
            rows = []
            for _, row in info.corners.iterrows():
                letter = row.get("Letter") or ""
                rows.append((
                    f"T{int(row['Number'])}{letter}",
                    float(row["Distance"]),
                    float(row["X"]), float(row["Y"]),
                ))
            if rows:
                self.corners.emit(rows)
        except Exception:
            pass  # sin curvas, la pestaña y el mapa simplemente no las muestran

    def _emit_tyres(self, session) -> None:
        """Compuesto y edad de neumático por piloto y vuelta."""
        try:
            tyres: dict[str, dict[int, tuple[str, int]]] = {}
            for _, row in session.laps.iterrows():
                try:
                    drv = str(row["DriverNumber"])
                    lap = int(row["LapNumber"])
                except (ValueError, TypeError):
                    continue
                compound = row.get("Compound")
                compound = compound if isinstance(compound, str) else ""
                life = row.get("TyreLife")
                life = int(life) if life is not None and life == life else 0
                tyres.setdefault(drv, {})[lap] = (compound, life)
            if tyres:
                self.tyres.emit(tyres)
        except Exception:
            pass

    def _emit_pits(self, session) -> None:
        """Paradas en boxes: vuelta de entrada y tiempo de sesión."""
        try:
            pits: dict[str, list] = {}
            for _, row in session.laps.iterrows():
                t_in = row.get("PitInTime")
                if t_in is None or t_in != t_in:  # NaT
                    continue
                try:
                    drv = str(row["DriverNumber"])
                    lap = int(row["LapNumber"])
                except (ValueError, TypeError):
                    continue
                pits.setdefault(drv, []).append((lap, float(t_in.total_seconds())))
            for stops in pits.values():
                stops.sort()
            if pits:
                self.pits.emit(pits)
        except Exception:
            pass
        self._emit_pit_lane(session)

    def _emit_pit_lane(self, session) -> None:
        """Visitas a la calle de boxes: entrada (PitInTime, fin de la vuelta
        de entrada) apareada con la salida siguiente (PitOutTime)."""
        try:
            visits: dict[str, list] = {}
            for num in session.drivers:
                laps = session.laps.pick_drivers(str(num))
                ins: list[tuple[float, int]] = []
                outs: list[float] = []
                for _, row in laps.iterrows():
                    t_in = row.get("PitInTime")
                    if t_in is not None and t_in == t_in:
                        try:
                            ins.append((float(t_in.total_seconds()),
                                        int(row["LapNumber"])))
                        except (ValueError, TypeError):
                            continue
                    t_out = row.get("PitOutTime")
                    if t_out is not None and t_out == t_out:
                        try:
                            outs.append(float(t_out.total_seconds()))
                        except (ValueError, TypeError):
                            continue
                outs.sort()
                for t_in, lap in sorted(ins):
                    t_out = next((t for t in outs if t > t_in), None)
                    visits.setdefault(str(num), []).append([lap, t_in, t_out])
            if visits:
                self.pitLane.emit(visits)
        except Exception:
            pass

    def _emit_track_status(self, session) -> None:
        """Períodos de bandera amarilla/roja/SC/VSC como (t0, t1, código)."""
        try:
            rows = [
                (float(row["Time"].total_seconds()), str(row["Status"]))
                for _, row in session.track_status.iterrows()
            ]
            periods = []
            for i, (t0, code) in enumerate(rows):
                if code == "1":  # pista libre
                    continue
                t1 = rows[i + 1][0] if i + 1 < len(rows) else float("inf")
                periods.append((t0, t1, code))
            if periods:
                self.trackStatus.emit(periods)
        except Exception:
            pass

    def _emit_weather(self, session) -> None:
        """Clima muestreado (~1/min): aire, pista, viento y lluvia, más
        humedad, presión y dirección del viento si la sesión los trae."""

        def _opt(row, key: str) -> float:
            try:
                return float(row[key])
            except (KeyError, ValueError, TypeError):
                return float("nan")

        try:
            rows = []
            for _, row in session.weather_data.iterrows():
                try:
                    rows.append((
                        float(row["Time"].total_seconds()),
                        float(row["AirTemp"]),
                        float(row["TrackTemp"]),
                        float(row["WindSpeed"]),
                        bool(row["Rainfall"]),
                        _opt(row, "Humidity"),
                        _opt(row, "Pressure"),
                        _opt(row, "WindDirection"),
                    ))
                except (ValueError, TypeError):
                    continue
            if rows:
                self.weather.emit(rows)
        except Exception:
            pass

    def _emit_sector_yellows(self, session) -> None:
        """Amarillas por sector de comisarios: (t0, t1, d0, d1) en metros de
        vuelta, cruzando los mensajes de dirección de carrera con las
        posiciones de los sectores de comisarios del circuito."""
        try:
            marshals = session.get_circuit_info().marshal_sectors
            dists = {int(n): float(d) for n, d in zip(marshals["Number"], marshals["Distance"])}
            if not dists:
                return
            numbers = sorted(dists)

            def sector_range(sec: int) -> tuple[float, float] | None:
                # el puesto de comisarios n marca el INICIO de su sector:
                # cubre desde su posición hasta el puesto siguiente
                if sec not in dists:
                    return None
                idx = numbers.index(sec)
                d1 = dists[numbers[(idx + 1) % len(numbers)]]
                return dists[sec], d1

            open_flags: dict[int, float] = {}
            periods = []
            for _, row in session.race_control_messages.iterrows():
                if str(row.get("Scope")) != "Sector":
                    continue
                sector = row.get("Sector")
                if sector is None or sector != sector:
                    continue
                sec = int(sector)
                t = float((row["Time"] - session.t0_date).total_seconds())
                flag = str(row.get("Flag"))
                if flag in ("YELLOW", "DOUBLE YELLOW"):
                    open_flags.setdefault(sec, t)
                elif sec in open_flags:  # CLEAR / GREEN
                    periods.append((open_flags.pop(sec), t, sec))
            for sec, t0 in open_flags.items():
                periods.append((t0, float("inf"), sec))
            result = []
            for t0, t1, sec in periods:
                rng = sector_range(sec)
                if rng is not None:
                    result.append((t0, t1, rng[0], rng[1]))
            if result:
                self.sectorYellows.emit(result)
        except Exception:
            pass

    def _emit_sector_times(self, session) -> None:
        """Tiempos oficiales por vuelta (S1-S3 y vuelta): el hub ubica con
        S1/S2 los límites reales de sector y las tablas muestran estos
        valores exactos en lugar de los interpolados."""
        try:
            reports = []
            columns = (("Sector1Time", 0, 10.0, 180.0),
                       ("Sector2Time", 1, 10.0, 180.0),
                       ("Sector3Time", 2, 10.0, 180.0),
                       ("LapTime", 3, 30.0, 600.0))
            for _, row in session.laps.iterrows():
                try:
                    drv = str(row["DriverNumber"])
                    lap = int(row["LapNumber"])
                except (ValueError, TypeError):
                    continue
                for col, idx, lo, hi in columns:
                    val = row.get(col)
                    if val is None or val != val:  # NaT
                        continue
                    secs = float(val.total_seconds())
                    if lo < secs < hi:
                        reports.append((drv, lap, idx, secs))
            if reports:
                self.sectorTimes.emit(reports)
        except Exception:
            pass  # sin tiempos de sector se mantienen los tercios de vuelta

    def _emit_race_control(self, session) -> None:
        """Mensajes de dirección de carrera con el mismo contrato que la
        fuente en vivo (t relativo al inicio de la sesión)."""

        def clean(value) -> str:
            return "" if value is None or value != value else str(value)

        try:
            rows = []
            for _, row in session.race_control_messages.iterrows():
                try:
                    t = float((row["Time"] - session.t0_date).total_seconds())
                except (KeyError, TypeError):
                    continue
                lap = row.get("Lap")
                sector = row.get("Sector")
                rows.append({
                    "t": t,
                    "lap": int(lap) if lap is not None and lap == lap else None,
                    "category": clean(row.get("Category")),
                    "flag": clean(row.get("Flag")),
                    "scope": clean(row.get("Scope")),
                    "sector": int(sector) if sector is not None and sector == sector else None,
                    "mode": "",
                    "driver": clean(row.get("RacingNumber")),
                    "message": clean(row.get("Message")),
                })
            if rows:
                self.raceControl.emit(rows)
        except Exception:
            pass  # sin mensajes el panel queda vacío

    def _emit_session_meta(self, session) -> None:
        try:
            info = getattr(session, "session_info", None) or {}
            self.sessionMeta.emit({
                "type": str(info.get("Type") or session.name or ""),
                "meeting": str((info.get("Meeting") or {}).get("Name") or ""),
                "name": str(info.get("Name") or session.name or ""),
                "year": self.year,  # clave circuito+año de los microsectores
            })
        except Exception:
            pass
        try:
            total = getattr(session, "total_laps", None)
            if total and total == total:
                self.lapCount.emit((0, int(total)))
        except Exception:
            pass

    def _emit_outline(self, session) -> None:
        """Trazado del circuito: posiciones de la vuelta más rápida."""
        try:
            lap = session.laps.pick_fastest()
            num = str(lap["DriverNumber"])
            t0 = lap["LapStartTime"].total_seconds()
            t1 = lap["Time"].total_seconds()
            pd_ = session.pos_data[num]
            t = pd_["SessionTime"].dt.total_seconds().to_numpy(dtype=np.float64)
            mask = (t >= t0) & (t <= t1)
            xs = pd_["X"].to_numpy(dtype=np.float64)[mask]
            ys = pd_["Y"].to_numpy(dtype=np.float64)[mask]
            if len(xs) > 50:
                self.trackOutline.emit((xs, ys))
        except Exception:
            pass  # sin trazado el mapa se arma solo con las estelas

    def _prepare_driver(self, session, num: str, lap_lengths: list, first_lap_starts: list) -> dict | None:
        try:
            cd = session.car_data[num]
        except (KeyError, AttributeError):
            return None
        if cd is None or cd.empty:
            return None

        st = cd["SessionTime"].dt.total_seconds().to_numpy(dtype=np.float64)
        speed = np.nan_to_num(cd["Speed"].to_numpy(dtype=np.float64))
        dt = np.diff(st, prepend=st[0])
        dt = np.clip(dt, 0.0, 5.0)  # huecos largos no suman distancia irreal
        dist_total = np.cumsum(speed / 3.6 * dt)

        n = len(st)
        lap_no = np.zeros(n, dtype=np.int32)
        base = np.zeros(n, dtype=np.float64)

        laps = session.laps.pick_drivers(num)
        if len(laps):
            starts = laps["LapStartTime"].dt.total_seconds().to_numpy(dtype=np.float64)
            numbers = laps["LapNumber"].to_numpy(dtype=np.int64)
            # vueltas con parada: el pit lane distorsiona la distancia
            # integrada, no aportan al largo de vuelta
            has_pit = np.zeros(len(laps), dtype=bool)
            for col in ("PitInTime", "PitOutTime"):
                if col in laps:
                    has_pit |= laps[col].notna().to_numpy()
            valid = ~np.isnan(starts)
            starts, numbers, has_pit = starts[valid], numbers[valid], has_pit[valid]
            if len(starts):
                first_lap_starts.append(float(starts[0]))
                bounds = np.searchsorted(st, starts)
                for k in range(len(bounds)):
                    i0 = int(bounds[k])
                    i1 = int(bounds[k + 1]) if k + 1 < len(bounds) else n
                    if i0 >= i1:
                        continue
                    # base = distancia integrada EN el cruce oficial de meta
                    # (interpolada); usar la muestra previa corría la línea
                    # hasta ~20 m antes y sesgaba vuelta y S3 en ~0,25 s
                    d0 = float(np.interp(starts[k], st, dist_total))
                    lap_no[i0:i1] = numbers[k]
                    base[i0:i1] = d0
                    if k + 1 < len(bounds) and not has_pit[k]:
                        length = float(np.interp(starts[k + 1], st, dist_total) - d0)
                        if 1000.0 < length < 30000.0:
                            lap_lengths.append(length)

        gear = cd["nGear"].fillna(0).to_numpy(dtype=np.int16) if "nGear" in cd else np.zeros(n, np.int16)
        brake_raw = cd["Brake"].to_numpy() if "Brake" in cd else np.zeros(n)
        brake = brake_raw.astype(np.float64) * (100.0 if brake_raw.dtype == bool else 1.0)
        return {
            "t": st,
            "lap": lap_no,
            "dist_lap": dist_total - base,
            "dist_total": dist_total,
            "speed": speed,
            "throttle": np.nan_to_num(cd["Throttle"].to_numpy(dtype=np.float64)) if "Throttle" in cd else np.zeros(n),
            "brake": np.nan_to_num(brake.astype(np.float64)),
            "rpm": np.nan_to_num(cd["RPM"].to_numpy(dtype=np.float64)) if "RPM" in cd else np.zeros(n),
            "gear": gear,
            "drs": cd["DRS"].fillna(0).to_numpy(dtype=np.int16) if "DRS" in cd else np.zeros(n, np.int16),
            "driver": np.full(n, num, dtype=object),
        }

    # ------------------------------------------------------------ reproducción

    def _play(self, stream: dict) -> None:
        t_arr = stream["t"]
        n = len(t_arr)
        t_start = float(stream["t_start"])
        t_end = float(t_arr[-1])
        base = int(np.searchsorted(t_arr, t_start))
        cursor = base
        pos = stream.get("pos")
        pos_cursor = int(np.searchsorted(pos["t"], t_start)) if pos else 0

        def sample_at(k: int) -> Sample:
            return Sample(
                driver=str(stream["driver"][k]),
                t=float(t_arr[k]),
                lap=int(stream["lap"][k]),
                dist_lap=float(stream["dist_lap"][k]),
                dist_total=float(stream["dist_total"][k]),
                speed=float(stream["speed"][k]),
                throttle=float(stream["throttle"][k]),
                brake=float(stream["brake"][k]),
                rpm=float(stream["rpm"][k]),
                gear=int(stream["gear"][k]),
                drs=int(stream["drs"][k]),
            )

        self.statusChanged.emit(
            f"Replaying {self.year} {self.gp} {self.session_name} at x{self.speed:g}"
        )
        # reloj acumulativo: t_pos avanza speed·dt por tick, así la pausa y el
        # cambio de velocidad en caliente no requieren recalibrar nada
        t_pos = t_start
        wall_prev = time.monotonic()
        finished_msg = False
        tick = 0
        self.progress.emit(t_start, t_start, t_end)
        while self._running:
            time.sleep(0.1)
            wall_now = time.monotonic()
            if not self._paused:
                t_pos += (wall_now - wall_prev) * self.speed
            wall_prev = wall_now
            seek = self._seek_t
            if seek is not None:
                self._seek_t = None
                target = min(max(float(seek), t_start), t_end)
                # el aviso viaja en cola ANTES que la historia re-emitida,
                # así la GUI limpia y luego recibe los datos en orden
                self.seekReset.emit()
                j = int(np.searchsorted(t_arr, target))
                k0 = base
                while k0 < j and self._running and self._seek_t is None:
                    k1 = min(k0 + 25000, j)
                    batch = [sample_at(k) for k in range(k0, k1)
                             if stream["lap"][k] > 0]
                    if batch:
                        self.batch.emit(batch)
                    k0 = k1
                if pos is not None:
                    pj = int(np.searchsorted(pos["t"], target))
                    p0 = int(np.searchsorted(pos["t"], target - 60.0))
                    if pj > p0:  # solo la cola reciente (estelas del mapa)
                        self.positions.emit([
                            (str(pos["driver"][k]), float(pos["t"][k]),
                             float(pos["x"][k]), float(pos["y"][k]))
                            for k in range(p0, pj)
                        ])
                    pos_cursor = pj
                cursor = j
                t_pos = target
                finished_msg = False
                self.progress.emit(t_start, target, t_end)
                continue
            if cursor >= n:
                if not finished_msg:
                    self.statusChanged.emit(
                        "Replay finished — use the timeline to seek back."
                    )
                    self.progress.emit(t_start, t_end, t_end)
                    finished_msg = True
                continue
            now = min(t_pos, t_end)
            if pos is not None:
                pj = int(np.searchsorted(pos["t"], now))
                if pj > pos_cursor:
                    self.positions.emit([
                        (str(pos["driver"][k]), float(pos["t"][k]),
                         float(pos["x"][k]), float(pos["y"][k]))
                        for k in range(pos_cursor, pj)
                    ])
                    pos_cursor = pj
            tick += 1
            if tick % 5 == 0:
                self.progress.emit(t_start, min(now, t_end), t_end)
            j = int(np.searchsorted(t_arr, now))
            if j <= cursor:
                continue
            batch = [
                sample_at(k)
                for k in range(cursor, j)
                # lap 0 = garage/vuelta previa: sin posición de pista válida
                if stream["lap"][k] > 0
            ]
            cursor = j
            self.batch.emit(batch)
