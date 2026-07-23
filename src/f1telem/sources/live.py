"""Fuente en vivo: cliente SignalR Core de F1 Live Timing.

F1 migró el streaming a SignalR Core (`wss://livetiming.formula1.com/signalrcore`)
con token de suscripción F1TV — el mismo que usa FastF1 (se lee de su archivo
`f1auth.json`; el capturador ofrece el login por navegador). Sin token se
intenta sin autenticación, que puede entregar datos parciales según la sesión.

Se decodifica CarData.z / Position.z (zlib+base64), la distancia se integra
de la velocidad y la vuelta sale de TimingData. Cada frame recibido se graba
en un archivo de captura (una línea JSON por mensaje, en el mismo formato de
sobre que entiende `CaptureSource` para re-reproducirlo).
"""
from __future__ import annotations

import base64
import datetime as dt
import json
import logging
import os
import re
import threading
import time
import zlib

from .. import config
from ..models import DriverInfo, Sample
from .base import BaseSource

NEGOTIATE_URL = "https://livetiming.formula1.com/signalrcore/negotiate"
WSS_URL = "wss://livetiming.formula1.com/signalrcore"
FEEDS = [
    "Heartbeat",
    "CarData.z",
    "Position.z",
    "TimingData",
    "TimingAppData",
    "DriverList",
    "SessionInfo",
    "TrackStatus",
    "WeatherData",
    "LapCount",
    "RaceControlMessages",
    "ExtrapolatedClock",
]
_UTC_RE = re.compile(r"(\.\d{1,6})\d*")


def _parse_utc(text: str) -> float:
    """'2026-07-05T14:02:03.1234567Z' -> epoch en segundos. Los stamps sin
    zona (RaceControlMessages) también son UTC."""
    text = _UTC_RE.sub(r"\1", text.replace("Z", "+00:00"))
    parsed = dt.datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.timestamp()


def _parse_clock(text) -> float | None:
    """'1:23:45' / '23:45' -> segundos (None si no parsea)."""
    try:
        secs = 0.0
        for part in str(text).split(":"):
            secs = secs * 60.0 + float(part)
        return secs
    except (ValueError, TypeError):
        return None


def decompress_feed(data: str) -> dict:
    """Decodifica un payload .z (base64 + deflate crudo)."""
    raw = zlib.decompress(base64.b64decode(data), -zlib.MAX_WBITS)
    return json.loads(raw)


def _index_items(obj):
    """Itera pares (índice, valor): el feed manda dicts {"0": ...} en los
    diffs y listas en los snapshots iniciales."""
    if isinstance(obj, dict):
        for key, val in obj.items():
            try:
                yield int(key), val
            except (ValueError, TypeError):
                continue
    elif isinstance(obj, list):
        yield from enumerate(obj)


def _parse_time_value(text) -> float:
    """'26.123' o '1:44.361' -> segundos (0.0 si no parsea)."""
    try:
        if ":" in text:
            m, s = text.split(":", 1)
            return float(m) * 60.0 + float(s)
        return float(text)
    except (ValueError, AttributeError, TypeError):
        return 0.0


def _circuit_rows(info: dict):
    """Trazado y curvas del JSON de la API de circuitos de MultiViewer
    (mismo sistema de coordenadas que Position.z; length en decímetros)."""
    xs = info.get("x") or []
    ys = info.get("y") or []
    outline = (xs, ys) if len(xs) > 10 and len(xs) == len(ys) else None
    rows = []
    for c in info.get("corners") or []:
        num = c.get("number")
        pos = c.get("trackPosition") or {}
        dist = c.get("length")
        if num is None or dist is None:
            continue
        rows.append((
            f"T{int(num)}{c.get('letter') or ''}",
            float(dist) / 10.0,
            float(pos.get("x", 0.0)),
            float(pos.get("y", 0.0)),
        ))
    return outline, rows


class _CarState:
    __slots__ = ("last_t", "last_speed", "dist_total", "lap", "lap_start_dist")

    def __init__(self):
        self.last_t: float | None = None
        self.last_speed = 0.0
        self.dist_total = 0.0
        self.lap = 1
        self.lap_start_dist = 0.0


class LiveDecoderMixin:
    """Decodificación del stream de live timing a señales de BaseSource.

    La usan LiveSource (red) y CaptureSource (archivo grabado). Las subclases
    deben heredar también de BaseSource: el mixin emite sus señales.
    """

    def _init_decoder(self) -> None:
        self._states: dict[str, _CarState] = {}
        self._laps_done: dict[str, int] = {}
        self._t0: float | None = None
        self._last_rel_t = 0.0
        self._drivers: dict[str, DriverInfo] = {}
        self._status_closed: list[tuple[float, float, str]] = []
        self._status_open: tuple[float, str] | None = None
        self._weather_log: list[tuple] = []
        self._sector_sent: set[tuple[str, int, int]] = set()
        self._segment_state: dict[tuple[str, int, int], int] = {}
        # dirección de carrera, stints, reloj y paradas
        self._rcm: dict[int, dict] = {}
        self._stints: dict[str, dict[int, dict]] = {}
        self._tyres: dict[str, dict[int, tuple[str, int]]] = {}
        self._clock: list | None = None
        self._lap_count = [0, 0]
        self._pit_counts: dict[str, int] = {}
        self._pit_log: dict[str, list] = {}
        self._retired: set[str] = set()
        # visitas a la calle de boxes: transiciones de InPit
        self._inpit: dict[str, bool] = {}
        self._pit_visits: dict[str, list] = {}  # {num: [[vuelta, t_in, t_out|None]]}
        # el snapshot inicial trae valores de la vuelta ANTERIOR: no sirven
        # para atribuir tiempos a la vuelta en curso (los Segments sí)
        self._in_snapshot = False
        # persiste entre seeks de la captura: el trazado ya emitido se
        # conserva en el hub, no hace falta re-pedirlo a la API
        self._circuit_fetched = getattr(self, "_circuit_fetched", False)

    # ------------------------------------------------------------- protocolo

    def _handle(self, msg: dict) -> None:
        # respuesta al Subscribe: snapshot inicial de todos los feeds
        snapshot = msg.get("R")
        if isinstance(snapshot, dict):
            self._in_snapshot = True
            try:
                for feed, data in snapshot.items():
                    self._feed(feed, data)
            finally:
                self._in_snapshot = False
        for item in msg.get("M", []) or []:
            if item.get("M") == "feed":
                args = item.get("A") or []
                if len(args) >= 2:
                    self._feed(args[0], args[1])

    def _feed(self, name: str, data) -> None:
        if name.endswith(".z"):
            try:
                data = decompress_feed(data)
            except Exception:
                return
            name = name[:-2]
        if name == "CarData":
            self._on_car_data(data)
        elif name == "Position":
            self._on_position(data)
        elif name == "TimingData":
            self._on_timing(data)
        elif name == "DriverList":
            self._on_driver_list(data)
        elif name == "SessionInfo":
            self._on_session_info(data)
        elif name == "TrackStatus":
            self._on_track_status(data)
        elif name == "WeatherData":
            self._on_weather(data)
        elif name == "RaceControlMessages":
            self._on_race_control(data)
        elif name == "TimingAppData":
            self._on_timing_app(data)
        elif name == "ExtrapolatedClock":
            self._on_clock(data)
        elif name == "LapCount":
            self._on_lap_count(data)

    def _on_track_status(self, data) -> None:
        """Banderas/SC: cierra el período abierto y abre uno nuevo si aplica."""
        if not isinstance(data, dict):
            return
        code = str(data.get("Status", "") or "")
        t = self._last_rel_t
        if self._status_open is not None:
            t0, prev = self._status_open
            self._status_closed.append((t0, t, prev))
            self._status_open = None
        if code and code != "1":
            self._status_open = (t, code)
        periods = list(self._status_closed)
        if self._status_open is not None:
            periods.append((self._status_open[0], float("inf"), self._status_open[1]))
        self.trackStatus.emit(periods)

    def _on_weather(self, data) -> None:
        if not isinstance(data, dict):
            return

        def _opt(key: str) -> float:
            try:
                return float(data.get(key))
            except (ValueError, TypeError):
                return float("nan")

        try:
            entry = (
                self._last_rel_t,
                float(data.get("AirTemp", 0) or 0),
                float(data.get("TrackTemp", 0) or 0),
                float(data.get("WindSpeed", 0) or 0),
                str(data.get("Rainfall", "0")) == "1",
                _opt("Humidity"),
                _opt("Pressure"),
                _opt("WindDirection"),
            )
        except (ValueError, TypeError):
            return
        self._weather_log.append(entry)
        self.weather.emit(list(self._weather_log))

    def _on_race_control(self, data) -> None:
        """Mensajes de dirección de carrera: banderas, SC/VSC, sanciones.
        El snapshot trae la lista completa; los diffs llegan como
        {"Messages": {"41": {...}}} — el índice deduplica."""
        if not isinstance(data, dict):
            return
        changed = False
        for idx, msg in _index_items(data.get("Messages")):
            if not isinstance(msg, dict) or not msg.get("Message"):
                continue
            utc = None
            try:
                utc = _parse_utc(str(msg["Utc"]))
            except (KeyError, ValueError):
                pass
            lap = msg.get("Lap")
            self._rcm[idx] = {
                "utc": utc,
                "t": self._last_rel_t,
                "lap": int(lap) if isinstance(lap, int) else None,
                "category": str(msg.get("Category") or ""),
                "flag": str(msg.get("Flag") or ""),
                "scope": str(msg.get("Scope") or ""),
                "sector": msg.get("Sector"),
                "mode": str(msg.get("Mode") or ""),
                "driver": str(msg.get("RacingNumber") or ""),
                "message": str(msg.get("Message") or ""),
            }
            changed = True
        if changed:
            self._emit_race_control()

    def _emit_race_control(self) -> None:
        """El t relativo sale del stamp UTC del mensaje cuando el ancla de
        tiempo (_t0) ya existe; los emitidos antes se recalculan acá."""
        rows = []
        for key in sorted(self._rcm):
            row = dict(self._rcm[key])
            utc = row.pop("utc", None)
            if utc is not None and self._t0 is not None:
                row["t"] = utc - self._t0
            rows.append(row)
        self.raceControl.emit(rows)

    def _on_timing_app(self, data) -> None:
        """Stints de neumáticos: se acumulan los diffs por índice y se
        reconstruye el mapa por vuelta {vuelta: (compuesto, edad)} con el
        mismo contrato que emite el replay."""
        if not isinstance(data, dict):
            return
        changed = set()
        for num, line in (data.get("Lines") or {}).items():
            if not isinstance(line, dict):
                continue
            for idx, st in _index_items(line.get("Stints")):
                if isinstance(st, dict):
                    self._stints.setdefault(num, {}).setdefault(idx, {}).update(st)
                    changed.add(num)
        if not changed:
            return
        for num in changed:
            self._rebuild_tyres(num)
        if self._tyres:
            self.tyres.emit({d: dict(m) for d, m in self._tyres.items()})

    def _rebuild_tyres(self, num: str) -> None:
        laps: dict[int, tuple[str, int]] = {}
        lap0 = 1
        for idx in sorted(self._stints.get(num, {})):
            st = self._stints[num][idx]
            compound = str(st.get("Compound") or "")
            try:
                total = int(st.get("TotalLaps") or 0)
                start = int(st.get("StartLaps") or 0)
            except (TypeError, ValueError):
                total = start = 0
            run = max(total - start, 1)  # vueltas corridas en este stint
            for j in range(run):
                laps[lap0 + j] = (compound, start + j + 1)
            lap0 += run
        if laps:
            self._tyres[num] = laps

    def _on_clock(self, data) -> None:
        if not isinstance(data, dict):
            return
        remaining = _parse_clock(data.get("Remaining"))
        if remaining is not None:
            extrap = bool(data.get(
                "Extrapolating", self._clock[2] if self._clock else False))
            self._clock = [self._last_rel_t, remaining, extrap]
        elif self._clock is not None and "Extrapolating" in data:
            self._clock[2] = bool(data.get("Extrapolating"))
        if self._clock is not None:
            self.sessionClock.emit(tuple(self._clock))

    def _on_lap_count(self, data) -> None:
        if not isinstance(data, dict):
            return
        if isinstance(data.get("CurrentLap"), int):
            self._lap_count[0] = data["CurrentLap"]
        if isinstance(data.get("TotalLaps"), int):
            self._lap_count[1] = data["TotalLaps"]
        self.lapCount.emit(tuple(self._lap_count))

    def _on_session_info(self, data) -> None:
        if not isinstance(data, dict):
            return
        meeting = (data.get("Meeting") or {}).get("Name", "")
        name = data.get("Name", "")
        if meeting or name:
            self.statusChanged.emit(f"Live: {meeting} — {name}")
        stype = str(data.get("Type") or "")
        meta_year = str(data.get("StartDate", ""))[:4]
        if meeting or name or stype:
            self.sessionMeta.emit({
                "type": stype, "meeting": meeting, "name": name,
                "year": meta_year if meta_year.isdigit() else "",
            })
        # trazado completo inmediato: la API de circuitos de MultiViewer usa
        # las mismas coordenadas que Position.z. Sin esto, el mapa se
        # autoconstruía recién cuando un auto cerraba ~2 vueltas (minutos), y
        # quedaba incompleto o deformado si ese auto pasaba por boxes.
        circuit_key = ((data.get("Meeting") or {}).get("Circuit") or {}).get("Key")
        year = str(data.get("StartDate", ""))[:4]
        if circuit_key and year.isdigit() and not self._circuit_fetched:
            self._circuit_fetched = True
            threading.Thread(
                target=self._fetch_circuit, args=(circuit_key, year),
                daemon=True, name="circuit-fetch",
            ).start()

    def _fetch_circuit(self, key, year) -> None:
        try:
            import requests

            resp = requests.get(
                f"https://api.multiviewer.app/api/v1/circuits/{key}/{year}",
                timeout=20, headers={"User-Agent": "BoxBox-F1"},
            )
            resp.raise_for_status()
            outline, corners = _circuit_rows(resp.json())
            if outline is not None:
                self.trackOutline.emit(outline)
            if corners:
                self.corners.emit(corners)
        except Exception:
            pass  # sin red, el mapa se autoconstruye siguiendo a un auto

    def _on_driver_list(self, data) -> None:
        if not isinstance(data, dict):
            return
        changed = False
        for num, entry in data.items():
            if not isinstance(entry, dict) or not num.isdigit():
                continue
            old = self._drivers.get(num)
            color = entry.get("TeamColour")
            info = DriverInfo(
                number=num,
                code=entry.get("Tla") or (old.code if old else num),
                name=entry.get("FullName") or entry.get("BroadcastName") or (old.name if old else ""),
                team=entry.get("TeamName") or (old.team if old else ""),
                color=(f"#{color}" if isinstance(color, str) and len(color) == 6 else (old.color if old else "#9aa0a6")),
            )
            if old is None or (info.code, info.name, info.color) != (old.code, old.name, old.color):
                self._drivers[num] = info
                changed = True
        if changed:
            self.driversDiscovered.emit(dict(self._drivers))

    def _report_time(self, out: list, num: str, lap: int, idx: int,
                     value, lo: float, hi: float) -> None:
        """Valida y deduplica un tiempo oficial (sector 0-2 o vuelta 3)."""
        if lap < 1 or not isinstance(value, str) or not value:
            return
        key = (num, lap, idx)
        if key in self._sector_sent:
            return
        secs = _parse_time_value(value)
        if lo < secs < hi:
            self._sector_sent.add(key)
            out.append((num, lap, idx, secs))

    def _on_timing(self, data) -> None:
        if not isinstance(data, dict):
            return
        sector_reports: list[tuple] = []
        seg_updates: list[tuple] = []
        pits_changed = False
        lane_changed = False
        retired_changed = False
        for num, line in (data.get("Lines") or {}).items():
            if not isinstance(line, dict):
                continue
            # abandono oficial (Stopped es transitorio — un trompo — y lo
            # cubre la heurística de movimiento del hub; acá solo Retired)
            ret = line.get("Retired")
            if ret is True and num not in self._retired:
                self._retired.add(num)
                retired_changed = True
            elif ret is False and num in self._retired:
                self._retired.discard(num)
                retired_changed = True
            if isinstance(line.get("NumberOfLaps"), int):
                self._laps_done[num] = line["NumberOfLaps"]
            done = self._laps_done.get(num, 0)
            # paradas: los incrementos del contador oficial marcan la parada
            # (el snapshot solo fija la línea base, sin instante conocido)
            n_stops = line.get("NumberOfPitStops")
            if isinstance(n_stops, int):
                prev = self._pit_counts.get(num)
                self._pit_counts[num] = n_stops
                if prev is not None and n_stops > prev and not self._in_snapshot:
                    self._pit_log.setdefault(num, []).append(
                        (done + 1, self._last_rel_t))
                    pits_changed = True
            # visitas a la calle de boxes: entrada/salida por InPit (en el
            # snapshot, un auto ya adentro abre su visita en ese instante)
            inpit = line.get("InPit")
            if isinstance(inpit, bool):
                was = self._inpit.get(num)
                self._inpit[num] = inpit
                if inpit and was is not True:
                    self._pit_visits.setdefault(num, []).append(
                        [done + 1, self._last_rel_t, None])
                    lane_changed = True
                elif not inpit and was is True:
                    visits = self._pit_visits.get(num)
                    if visits and visits[-1][2] is None:
                        visits[-1][2] = self._last_rel_t
                        lane_changed = True
            # tiempo oficial de la vuelta recién cerrada (formato "1:44.361")
            if not self._in_snapshot:
                last = line.get("LastLapTime")
                if isinstance(last, dict):
                    self._report_time(sector_reports, num, done, 3,
                                      last.get("Value"), 30.0, 600.0)
            for s_idx, sector in _index_items(line.get("Sectors")):
                if not isinstance(sector, dict):
                    continue
                # tiempos oficiales de sector: S1/S2 llegan a mitad de vuelta
                # (vuelta en curso); S3 cierra la vuelta recién completada
                if s_idx in (0, 1, 2) and not self._in_snapshot:
                    lap = done if s_idx == 2 else done + 1
                    self._report_time(sector_reports, num, lap, s_idx,
                                      sector.get("Value"), 10.0, 180.0)
                # estado de los microsectores oficiales (rayitas de colores)
                for g_idx, seg in _index_items(sector.get("Segments")):
                    if not isinstance(seg, dict):
                        continue
                    status = seg.get("Status")
                    if not isinstance(status, int):
                        continue
                    skey = (num, s_idx, g_idx)
                    if self._segment_state.get(skey) != status:
                        self._segment_state[skey] = status
                        seg_updates.append((num, s_idx, g_idx, status))
        if sector_reports:
            self.sectorTimes.emit(sector_reports)
        if seg_updates:
            self.segmentStatus.emit(seg_updates)
        if pits_changed:
            self.pits.emit({k: list(v) for k, v in self._pit_log.items()})
        if lane_changed:
            self.pitLane.emit({k: [list(v) for v in vs]
                               for k, vs in self._pit_visits.items()})
        if retired_changed:
            self.retirements.emit(sorted(self._retired))

    def _on_position(self, data) -> None:
        if not isinstance(data, dict):
            return
        batch: list[tuple] = []
        for entry in data.get("Position", []) or []:
            try:
                t_utc = _parse_utc(entry["Timestamp"])
            except (KeyError, ValueError):
                continue
            if self._t0 is None:
                self._t0 = t_utc
            for num, p in (entry.get("Entries") or {}).items():
                x, y = p.get("X"), p.get("Y")
                if x is None or y is None:
                    continue
                batch.append((num, t_utc - self._t0, float(x), float(y)))
        if batch:
            self.positions.emit(batch)

    def _on_car_data(self, data) -> None:
        if not isinstance(data, dict):
            return
        batch: list[Sample] = []
        for entry in data.get("Entries", []) or []:
            try:
                t_utc = _parse_utc(entry["Utc"])
            except (KeyError, ValueError):
                continue
            if self._t0 is None:
                self._t0 = t_utc
            self._last_rel_t = t_utc - self._t0
            for num, car in (entry.get("Cars") or {}).items():
                ch = car.get("Channels") or {}
                speed = float(ch.get("2", 0) or 0)
                state = self._states.setdefault(num, _CarState())
                if state.last_t is not None:
                    step = min(max(t_utc - state.last_t, 0.0), 5.0)
                    state.dist_total += (state.last_speed + speed) / 2.0 / 3.6 * step
                state.last_t = t_utc
                state.last_speed = speed

                lap = self._laps_done.get(num, 0) + 1
                if lap != state.lap:
                    state.lap = lap
                    state.lap_start_dist = state.dist_total

                batch.append(
                    Sample(
                        driver=num,
                        t=t_utc - self._t0,
                        lap=state.lap,
                        dist_lap=state.dist_total - state.lap_start_dist,
                        dist_total=state.dist_total,
                        speed=speed,
                        throttle=min(100.0, float(ch.get("4", 0) or 0)),
                        brake=min(100.0, float(ch.get("5", 0) or 0)),
                        rpm=float(ch.get("0", 0) or 0),
                        gear=int(ch.get("3", 0) or 0),
                        drs=int(ch.get("45", 0) or 0),
                    )
                )
        if batch:
            self.batch.emit(batch)


class LiveSource(LiveDecoderMixin, BaseSource):
    def __init__(self, record_path: str | None = None, parent=None):
        super().__init__(parent)
        self._init_decoder()
        self.record_path = record_path
        self._recorder = None
        self._rec_lock = threading.Lock()
        self._connected = False
        self._conn = None

    @staticmethod
    def stored_token() -> str | None:
        """Token de suscripción F1TV guardado por FastF1 (si existe)."""
        try:
            from fastf1.internals.f1auth import AUTH_DATA_FILE

            token = AUTH_DATA_FILE.read_text().strip()
            return token or None
        except Exception:
            return None

    def run(self) -> None:
        try:
            rec_dir = config.recordings_dir()
            rec_dir.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d_%H%M%S")
            path = self.record_path or str(rec_dir / f"live_{stamp}.jsonl")
            # line-buffered: el visualizador puede seguir el archivo en vivo
            self._recorder = open(path, "a", buffering=1, encoding="utf-8")
            self.statusChanged.emit(f"Capturing to {os.path.basename(path)}")
        except OSError:
            self._recorder = None
        try:
            self._run_client()
        except Exception as exc:
            if self._running:
                self.failed.emit(f"Could not connect to F1 Live Timing: {exc}")
        finally:
            if self._conn is not None:
                try:
                    self._conn.stop()
                except Exception:
                    pass
            if self._recorder is not None:
                self._recorder.close()

    # ------------------------------------------------------- SignalR Core

    def _run_client(self) -> None:
        import requests
        from signalrcore.hub_connection_builder import HubConnectionBuilder

        token = self.stored_token()
        if token is None:
            self.statusChanged.emit(
                "No F1TV token found — connecting unauthenticated (data may be "
                "partial); sign in from the Capture window."
            )
        headers: dict[str, str] = {}
        self.statusChanged.emit("Negotiating connection with F1 Live Timing...")
        resp = requests.options(NEGOTIATE_URL, headers=headers, timeout=20)
        cookie = resp.cookies.get("AWSALBCORS")
        if cookie:
            headers["Cookie"] = f"AWSALBCORS={cookie}"
        options = {"verify_ssl": True, "headers": headers}
        if token:
            options["access_token_factory"] = lambda: token
        conn = (
            HubConnectionBuilder()
            .with_url(WSS_URL, options=options)
            .configure_logging(logging.WARNING)
            .with_automatic_reconnect({
                "type": "raw",
                "keep_alive_interval": 10,
                "reconnect_interval": 5,
                "max_attempts": 100000,
            })
            .build()
        )
        conn.on_open(self._on_ws_open)
        conn.on_close(lambda: (
            self.statusChanged.emit("Connection closed; reconnecting...")
            if self._running else None
        ))
        conn.on("feed", self._on_ws_feed)
        self._conn = conn
        conn.start()
        started = time.monotonic()
        while self._running and not self._connected:
            time.sleep(0.1)
            if time.monotonic() - started > 30:
                raise ConnectionError("timed out opening the websocket")
        while self._running:
            time.sleep(0.2)

    def _on_ws_open(self) -> None:
        self._connected = True
        try:
            self._conn.send("Subscribe", [FEEDS], on_invocation=self._on_ws_snapshot)
        except Exception:
            return
        self.statusChanged.emit(
            "Connected to F1 Live Timing. Waiting for data "
            "(it only flows during an official session)..."
        )

    def _on_ws_snapshot(self, msg) -> None:
        result = getattr(msg, "result", None)
        if not isinstance(result, dict):
            return
        self._record_line({"R": result})
        self._in_snapshot = True
        try:
            for topic, data in result.items():
                try:
                    self._feed(topic, data)
                except Exception:
                    pass
        finally:
            self._in_snapshot = False

    def _on_ws_feed(self, msg) -> None:
        if not isinstance(msg, list) or len(msg) < 2:
            return
        topic, data = msg[0], msg[1]
        stamp = msg[2] if len(msg) > 2 else ""
        self._record_line({"M": [{"H": "Streaming", "M": "feed", "A": [topic, data, stamp]}]})
        try:
            self._feed(topic, data)
        except Exception:
            pass  # un mensaje malformado no debe tirar la conexión

    def _record_line(self, obj) -> None:
        if self._recorder is None:
            return
        try:
            with self._rec_lock:
                self._recorder.write(json.dumps(obj, separators=(",", ":")) + "\n")
        except OSError:
            self._recorder = None
