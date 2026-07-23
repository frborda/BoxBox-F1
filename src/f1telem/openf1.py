"""Cliente de OpenF1 (https://openf1.org): enriquece la sesión con datos
que el feed no trae — grilla oficial de largada, duración oficial de las
paradas (auto detenido, publicada desde el GP de EE.UU. 2024) y fotos de
los pilotos. Usa solo el histórico gratuito (el tiempo real de OpenF1 es
pago, y para vivo ya está el feed): en una carrera en vivo la grilla igual
aparece porque sale de la clasificación, que ya es histórica; las paradas
se reintentan espaciadas por si se van publicando.

Presupuesto de red por sesión: resolver la sesión (≤2 requests), pilotos
(1), grilla (1) y paradas (1 + reintentos en vivo cada 3 min, acotados).
Un limitador global impone 2.2 s entre requests (< 28/min) para no rozar
el límite público de 30/min. Lo inmutable (calendario, grilla, pilotos) se
cachea en disco: repetir un replay no vuelve a pedir nada. Las fotos se
descargan del CDN de F1 (no cuentan para el límite de OpenF1) una única
vez y quedan en el caché para siempre.
"""
from __future__ import annotations

import json
import math
import os
import re
import threading
import time

from PySide6.QtCore import QObject, QTimer, Signal

from . import config

BASE = "https://api.openf1.org/v1"
MIN_INTERVAL_S = 2.2   # separación mínima entre requests a OpenF1
PIT_POLL_MS = 180_000  # reintento de paradas en vivo (3 min)
PIT_POLL_MAX = 20      # y acotado: como mucho 1 hora de reintentos
FIRST_YEAR = 2023      # OpenF1 no tiene datos anteriores

# palabras que no distinguen un GP de otro al comparar nombres
_STOP_WORDS = {"grand", "prix", "gp", "formula", "1", "f1", "the", "de",
               "del", "di", "grande", "premio"}


class RateLimiter:
    """Impone una separación mínima entre requests (compartida por todos
    los hilos del cliente). Inyectable para poder probarla sin dormir."""

    def __init__(self, min_interval: float = MIN_INTERVAL_S,
                 now=time.monotonic, sleep=time.sleep):
        self.min_interval = float(min_interval)
        self._now = now
        self._sleep = sleep
        self._lock = threading.Lock()
        self._last = -math.inf

    def wait(self) -> None:
        with self._lock:
            wait_s = self._last + self.min_interval - self._now()
            if wait_s > 0:
                self._sleep(wait_s)
            self._last = self._now()


def _tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", str(text or "").lower())
    return {w for w in words if w not in _STOP_WORDS}


def match_meeting(meetings: list[dict], meeting_name: str) -> dict | None:
    """Encuentra el meeting de OpenF1 que corresponde al nombre del feed
    ("British Grand Prix", "Gran Premio de España", ...): nombre exacto
    primero, si no el de mayor coincidencia de palabras significativas
    contra nombre/circuito/lugar/país."""
    want = str(meeting_name or "").strip().lower()
    if not want:
        return None
    for m in meetings:
        if str(m.get("meeting_name", "")).strip().lower() == want:
            return m
    want_toks = _tokens(meeting_name)
    if not want_toks:
        return None
    best, best_score = None, 0
    for m in meetings:
        cand = " ".join(str(m.get(k, "")) for k in (
            "meeting_name", "meeting_official_name", "circuit_short_name",
            "location", "country_name"))
        score = len(want_toks & _tokens(cand))
        if score > best_score:
            best, best_score = m, score
    return best


def pick_session(sessions: list[dict], name: str, stype: str) -> dict | None:
    """Elige la tanda dentro del meeting: por nombre exacto ("Race",
    "Sprint", "Qualifying", "Practice 1"...), si no por tipo (la última,
    que es la más reciente del fin de semana)."""
    want = str(name or "").strip().lower()
    if want:
        for s in sessions:
            if str(s.get("session_name", "")).strip().lower() == want:
                return s
    want_type = str(stype or "").strip().lower()
    if want_type:
        typed = [s for s in sessions
                 if str(s.get("session_type", "")).strip().lower() == want_type]
        if typed:
            return typed[-1]
    return None


def parse_grid(rows: list[dict]) -> dict[str, int]:
    """starting_grid -> {nº de auto: posición de grilla}."""
    out: dict[str, int] = {}
    for row in rows:
        try:
            pos = int(row["position"])
            drv = str(int(row["driver_number"]))
        except (KeyError, TypeError, ValueError):
            continue
        if pos > 0:
            out[drv] = pos
    return out


def pick_grid_rows(rows: list[dict], sessions: list[dict],
                   session_name: str) -> list[dict]:
    """starting_grid se consulta por meeting y sus filas llevan la key de
    la TANDA QUE DEFINIÓ la grilla (la clasificación), no la de la carrera:
    elegir el grupo correcto — Qualifying para la carrera, Sprint
    Qualifying/Shootout para la sprint. Con un único grupo se usa ese."""
    by_sess: dict = {}
    for row in rows:
        by_sess.setdefault(row.get("session_key"), []).append(row)
    if not by_sess:
        return []
    if len(by_sess) == 1:
        return next(iter(by_sess.values()))
    name_of = {
        s.get("session_key"): str(s.get("session_name", "")).strip().lower()
        for s in sessions}
    want = ({"sprint qualifying", "sprint shootout"}
            if str(session_name).strip().lower() == "sprint"
            else {"qualifying"})
    for skey, group in by_sess.items():
        if name_of.get(skey) in want:
            return group
    return []


def parse_pits(rows: list[dict]) -> dict[str, dict[int, tuple[float, float]]]:
    """pit -> {nº: {vuelta: (s en la calle, s detenido)}}. stop_duration
    existe recién desde el GP de EE.UU. 2024: antes queda NaN."""
    out: dict[str, dict[int, tuple[float, float]]] = {}

    def _f(value) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return float("nan")

    for row in rows:
        try:
            drv = str(int(row["driver_number"]))
            lap = int(row["lap_number"])
        except (KeyError, TypeError, ValueError):
            continue
        lane = _f(row.get("lane_duration", row.get("pit_duration")))
        stop = _f(row.get("stop_duration"))
        out.setdefault(drv, {})[lap] = (lane, stop)
    return out


def parse_headshots(rows: list[dict]) -> dict[str, str]:
    """drivers -> {nº de auto: URL de la foto} (solo los que tienen)."""
    out: dict[str, str] = {}
    for row in rows:
        url = str(row.get("headshot_url") or "").strip()
        try:
            drv = str(int(row["driver_number"]))
        except (KeyError, TypeError, ValueError):
            continue
        if url.startswith("http"):
            out[drv] = url
    return out


class OpenF1Client(QObject):
    """Resuelve la sesión y publica los datos por señales Qt (los fetch
    corren en hilos daemon; las señales llegan encoladas al hilo de la
    GUI). Ante cualquier error de red se rinde en silencio: son datos de
    cortesía, la app funciona igual sin ellos."""

    gridReady = Signal(object)          # {nº: posición de grilla}
    officialPitsReady = Signal(object)  # {nº: {vuelta: (lane_s, stop_s)}}
    headshotsReady = Signal(object)     # {nº: ruta local de la foto}
    _pollWanted = Signal()              # pedir el timer desde el hilo worker

    def __init__(self, parent=None):
        super().__init__(parent)
        self._gen = 0
        self._key: tuple | None = None
        self._session_key: int | None = None
        self._live = False
        self._limiter = RateLimiter()
        self._polls_left = 0
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(PIT_POLL_MS)
        self._poll_timer.timeout.connect(self._poll_pits)
        self._pollWanted.connect(self._start_polling)

    # ------------------------------------------------------------- ciclo

    def reset(self) -> None:
        """Nueva fuente: invalida los workers en vuelo y frena el sondeo."""
        self._gen += 1
        self._key = None
        self._session_key = None
        self._poll_timer.stop()
        self._polls_left = 0

    def set_live(self, live: bool) -> None:
        self._live = bool(live)

    def request(self, meta: dict) -> None:
        """Llamar con la meta de sesión ({year, meeting, name, type}):
        idempotente, un solo ciclo de carga por sesión."""
        if os.environ.get("F1TELEM_NO_OPENF1"):
            return
        year = str(meta.get("year") or "").strip()
        meeting = str(meta.get("meeting") or "").strip()
        name = str(meta.get("name") or "").strip()
        stype = str(meta.get("type") or "").strip()
        if not year.isdigit() or int(year) < FIRST_YEAR or not meeting:
            return
        key = (year, meeting.lower(), (name or stype).lower())
        if key == self._key:
            return
        self._key = key
        self._gen += 1
        threading.Thread(
            target=self._load, args=(self._gen, int(year), meeting, name, stype),
            daemon=True, name="openf1-load",
        ).start()

    # ------------------------------------------------------------- red

    def _get(self, path: str, params: dict) -> list:
        self._limiter.wait()
        import requests

        resp = requests.get(f"{BASE}/{path}", params=params, timeout=15,
                            headers={"User-Agent": "BoxBox-F1"})
        if resp.status_code == 429:  # nunca debería con el limitador, pero
            time.sleep(25.0)
            self._limiter.wait()
            resp = requests.get(f"{BASE}/{path}", params=params, timeout=15,
                                headers={"User-Agent": "BoxBox-F1"})
        if resp.status_code == 404:
            return []  # OpenF1 responde 404 (no lista vacía) sin datos
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else []

    @staticmethod
    def _cache_dir():
        path = config.cache_dir() / "openf1"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _cached_json(self, name: str) -> list | None:
        try:
            data = json.loads((self._cache_dir() / name).read_text("utf-8"))
            return data if isinstance(data, list) and data else None
        except (OSError, ValueError):
            return None

    def _write_cache(self, name: str, rows: list) -> None:
        if not rows:
            return  # una respuesta vacía puede ser "todavía no": no fijarla
        try:
            (self._cache_dir() / name).write_text(json.dumps(rows), "utf-8")
        except OSError:
            pass

    def _get_cached(self, cache_name: str, path: str, params: dict) -> list:
        """Caché primero (datos inmutables una vez publicados)."""
        rows = self._cached_json(cache_name)
        if rows is not None:
            return rows
        rows = self._get(path, params)
        self._write_cache(cache_name, rows)
        return rows

    # ------------------------------------------------------------- carga

    def _resolve_session(self, year: int, meeting: str, name: str,
                         stype: str) -> tuple[dict, list[dict]] | None:
        """(tanda elegida, todas las tandas del meeting) o None."""
        meetings = self._cached_json(f"meetings-{year}.json") or []
        found = match_meeting(meetings, meeting)
        if found is None:
            meetings = self._get("meetings", {"year": year})
            self._write_cache(f"meetings-{year}.json", meetings)
            found = match_meeting(meetings, meeting)
        if found is None:
            return None
        mkey = found.get("meeting_key")
        if mkey is None:
            return None
        sessions = self._get_cached(
            f"sessions-{mkey}.json", "sessions", {"meeting_key": mkey})
        session = pick_session(sessions, name, stype)
        return None if session is None else (session, sessions)

    def _load(self, gen: int, year: int, meeting: str, name: str,
              stype: str) -> None:
        try:
            resolved = self._resolve_session(year, meeting, name, stype)
            if resolved is None or gen != self._gen:
                return
            session, sessions = resolved
            skey = session.get("session_key")
            mkey = session.get("meeting_key")
            if skey is None:
                return
            self._session_key = int(skey)
            sname = str(session.get("session_name", "")).strip().lower()

            self._load_headshots(gen, skey)
            # grilla oficial: solo tandas que largan desde la grilla; el
            # endpoint se consulta por meeting (indexa por la clasificación
            # que definió la grilla, no por la carrera)
            if sname in ("race", "sprint") and mkey is not None:
                grid = parse_grid(pick_grid_rows(
                    self._get_cached(f"grid-m{mkey}.json", "starting_grid",
                                     {"meeting_key": mkey}),
                    sessions, sname))
                if grid and gen == self._gen:
                    self.gridReady.emit(grid)
            self._fetch_pits(gen, skey, use_cache=True)
            if self._live and gen == self._gen:
                self._pollWanted.emit()
        except Exception:
            pass  # sin red / sin datos: la app sigue sin el enriquecimiento

    def _load_headshots(self, gen: int, skey) -> None:
        urls = parse_headshots(self._get_cached(
            f"drivers-{skey}.json", "drivers", {"session_key": skey}))
        paths: dict[str, str] = {}
        for drv, url in urls.items():
            local = self._download_headshot(drv, url)
            if local:
                paths[drv] = local
            if gen != self._gen:
                return
        if paths and gen == self._gen:
            self.headshotsReady.emit(paths)

    @staticmethod
    def _download_headshot(drv: str, url: str) -> str | None:
        """Foto al caché permanente (CDN de F1, no cuenta para OpenF1)."""
        safe = re.sub(r"[^A-Za-z0-9._-]", "", url.rsplit("/", 1)[-1])[-60:]
        target = config.cache_dir() / "headshots" / f"{drv}-{safe}"
        if target.exists():
            return str(target)
        try:
            import requests

            resp = requests.get(url, timeout=15,
                                headers={"User-Agent": "BoxBox-F1"})
            resp.raise_for_status()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(resp.content)
            time.sleep(0.25)  # cortesía con el CDN
            return str(target)
        except Exception:
            return None

    def _fetch_pits(self, gen: int, skey, use_cache: bool = False) -> None:
        try:
            rows = self._cached_json(f"pit-{skey}.json") if use_cache else None
            if rows is None:
                rows = self._get("pit", {"session_key": skey})
                if not self._live:  # en vivo estaría incompleto: no fijarlo
                    self._write_cache(f"pit-{skey}.json", rows)
            pits = parse_pits(rows)
            if pits and gen == self._gen:
                self.officialPitsReady.emit(pits)
        except Exception:
            pass

    # --------------------------------------------- sondeo de paradas (vivo)

    def _start_polling(self) -> None:
        self._polls_left = PIT_POLL_MAX
        self._poll_timer.start()

    def _poll_pits(self) -> None:
        if self._polls_left <= 0 or self._session_key is None:
            self._poll_timer.stop()
            return
        self._polls_left -= 1
        threading.Thread(
            target=self._fetch_pits, args=(self._gen, self._session_key),
            daemon=True, name="openf1-pits",
        ).start()
