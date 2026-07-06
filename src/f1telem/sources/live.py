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
    "DriverList",
    "SessionInfo",
    "TrackStatus",
    "WeatherData",
    "LapCount",
]
_UTC_RE = re.compile(r"(\.\d{1,6})\d*")


def _parse_utc(text: str) -> float:
    """'2026-07-05T14:02:03.1234567Z' -> epoch en segundos."""
    text = _UTC_RE.sub(r"\1", text.replace("Z", "+00:00"))
    return dt.datetime.fromisoformat(text).timestamp()


def decompress_feed(data: str) -> dict:
    """Decodifica un payload .z (base64 + deflate crudo)."""
    raw = zlib.decompress(base64.b64decode(data), -zlib.MAX_WBITS)
    return json.loads(raw)


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

    # ------------------------------------------------------------- protocolo

    def _handle(self, msg: dict) -> None:
        # respuesta al Subscribe: snapshot inicial de todos los feeds
        snapshot = msg.get("R")
        if isinstance(snapshot, dict):
            for feed, data in snapshot.items():
                self._feed(feed, data)
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
        try:
            entry = (
                self._last_rel_t,
                float(data.get("AirTemp", 0) or 0),
                float(data.get("TrackTemp", 0) or 0),
                float(data.get("WindSpeed", 0) or 0),
                str(data.get("Rainfall", "0")) == "1",
            )
        except (ValueError, TypeError):
            return
        self._weather_log.append(entry)
        self.weather.emit(list(self._weather_log))

    def _on_session_info(self, data) -> None:
        if not isinstance(data, dict):
            return
        meeting = (data.get("Meeting") or {}).get("Name", "")
        name = data.get("Name", "")
        if meeting or name:
            self.statusChanged.emit(f"Live: {meeting} — {name}")

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

    def _on_timing(self, data) -> None:
        if not isinstance(data, dict):
            return
        for num, line in (data.get("Lines") or {}).items():
            if isinstance(line, dict) and isinstance(line.get("NumberOfLaps"), int):
                self._laps_done[num] = line["NumberOfLaps"]

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

    @property
    def session_key(self) -> str:
        return f"live-{dt.date.today().isoformat()}"

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
        for topic, data in result.items():
            try:
                self._feed(topic, data)
            except Exception:
                pass

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
