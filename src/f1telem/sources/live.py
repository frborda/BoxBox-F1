"""Fuente en vivo: cliente SignalR (clásico) de F1 Live Timing.

Se conecta a https://livetiming.formula1.com/signalr, se suscribe a los
feeds y decodifica CarData.z (zlib+base64). La distancia se obtiene
integrando la velocidad en el tiempo; el número de vuelta sale de
TimingData (NumberOfLaps por piloto).

Solo hay datos cuando una sesión oficial está en curso; fuera de sesión
la conexión queda a la espera. El stream crudo se graba en
%LOCALAPPDATA%/f1telem/recordings para análisis posterior.
"""
from __future__ import annotations

import asyncio
import base64
import datetime as dt
import json
import re
import time
import urllib.parse
import zlib

from .. import config
from ..models import DriverInfo, Sample
from .base import BaseSource

BASE_URL = "https://livetiming.formula1.com/signalr"
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
_CONNECTION_DATA = json.dumps([{"name": "Streaming"}])
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


class LiveSource(BaseSource):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._states: dict[str, _CarState] = {}
        self._laps_done: dict[str, int] = {}
        self._t0: float | None = None
        self._last_rel_t = 0.0
        self._status_closed: list[tuple[float, float, str]] = []
        self._status_open: tuple[float, str] | None = None
        self._weather_log: list[tuple] = []
        self._drivers: dict[str, DriverInfo] = {}
        self._recorder = None

    @property
    def session_key(self) -> str:
        return f"live-{dt.date.today().isoformat()}"

    def run(self) -> None:
        try:
            rec_dir = config.recordings_dir()
            rec_dir.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d_%H%M%S")
            self._recorder = open(rec_dir / f"live_{stamp}.jsonl", "a", encoding="utf-8")
        except OSError:
            self._recorder = None
        try:
            asyncio.run(self._main())
        finally:
            if self._recorder:
                self._recorder.close()

    async def _main(self) -> None:
        attempts = 0
        while self._running:
            try:
                await self._connect_once()
                attempts = 0
            except Exception as exc:
                attempts += 1
                if not self._running:
                    break
                if attempts >= 5:
                    self.failed.emit(f"Could not connect to F1 Live Timing: {exc}")
                    return
                self.statusChanged.emit(
                    f"Connection dropped ({exc}); retry {attempts}/5 in 5 s..."
                )
                await asyncio.sleep(5)

    async def _connect_once(self) -> None:
        import aiohttp

        self.statusChanged.emit("Negotiating connection with F1 Live Timing...")
        headers = {"User-Agent": "BestHTTP", "Accept-Encoding": "gzip,identity"}
        timeout = aiohttp.ClientTimeout(total=None, connect=20, sock_read=90)
        async with aiohttp.ClientSession(headers=headers, timeout=timeout) as http:
            params = {"connectionData": _CONNECTION_DATA, "clientProtocol": "1.5"}
            async with http.get(f"{BASE_URL}/negotiate", params=params) as resp:
                resp.raise_for_status()
                nego = await resp.json(content_type=None)
            token = nego["ConnectionToken"]

            ws_url = (
                BASE_URL.replace("https://", "wss://")
                + "/connect?transport=webSockets&clientProtocol=1.5"
                + f"&connectionToken={urllib.parse.quote(token)}"
                + f"&connectionData={urllib.parse.quote(_CONNECTION_DATA)}"
            )
            async with http.ws_connect(ws_url, heartbeat=30) as ws:
                await ws.send_json({"H": "Streaming", "M": "Subscribe", "A": [FEEDS], "I": 1})
                self.statusChanged.emit(
                    "Connected to F1 Live Timing. Waiting for data "
                    "(it only flows during an official session)..."
                )
                while self._running:
                    try:
                        msg = await ws.receive(timeout=1.0)
                    except asyncio.TimeoutError:
                        continue
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        self._record(msg.data)
                        try:
                            self._handle(json.loads(msg.data))
                        except Exception:
                            pass  # un mensaje malformado no debe tirar la conexión
                    elif msg.type in (
                        aiohttp.WSMsgType.CLOSED,
                        aiohttp.WSMsgType.CLOSE,
                        aiohttp.WSMsgType.ERROR,
                    ):
                        raise ConnectionError("websocket cerrado por el servidor")

    # ------------------------------------------------------------- protocolo

    def _record(self, raw: str) -> None:
        if self._recorder and raw and raw != "{}":
            try:
                self._recorder.write(raw + "\n")
            except OSError:
                self._recorder = None

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
