"""Fuente "Capture": sigue un archivo de captura grabado por el capturador.

Modo LIVE (por defecto): sigue la cola del archivo con el menor delay posible
— cada línea nueva se decodifica apenas se escribe (poll de 50 ms). Además
permite retroceder en el tiempo como el replay (re-parseando desde el inicio
con reloj propio y multiplicador de velocidad) y volver al vivo con go_live().
El formato del archivo es una línea JSON por mensaje SignalR (el mismo sobre
clásico que produce LiveSource al grabar).
"""
from __future__ import annotations

import json
import os
import time

from PySide6.QtCore import Signal

from .base import BaseSource
from .live import LiveDecoderMixin


class CaptureSource(LiveDecoderMixin, BaseSource):
    seekReset = Signal()
    progress = Signal(float, float, float)
    lapMarks = Signal(object)
    liveChanged = Signal(bool)

    def __init__(self, path, speed: float = 1.0, parent=None):
        super().__init__(parent)
        self._init_decoder()
        self.path = str(path)
        self.speed = max(0.1, float(speed))
        self.live_mode = True
        self._paused = False
        self._seek_t: float | None = None
        self._want_live = False
        self._t_end = 0.0
        self._marks: dict[int, float] = {}
        self._marks_dirty = False
        self._max_lap_seen = 0

    @property
    def session_key(self) -> str:
        return f"capture-{os.path.basename(self.path)}"

    def request_seek(self, t: float) -> None:
        self._seek_t = float(t)

    def go_live(self) -> None:
        self._want_live = True

    def set_speed(self, speed: float) -> None:
        self.speed = max(0.1, float(speed))

    def set_paused(self, paused: bool) -> None:
        self._paused = bool(paused)

    # ------------------------------------------------------------------

    def run(self) -> None:
        try:
            self._loop()
        except Exception as exc:
            if self._running:
                self.failed.emit(f"Capture playback failed: {exc}")

    def _read_line(self, f):
        """Próxima línea completa, o None (sin retroceder a medias líneas)."""
        pos = f.tell()
        line = f.readline()
        if not line or not line.endswith(b"\n"):
            f.seek(pos)
            return None
        return line

    def _loop(self) -> None:
        f = open(self.path, "rb")
        self.statusChanged.emit(
            f"Capture: following {os.path.basename(self.path)} (LIVE)"
        )
        self.liveChanged.emit(True)
        t_pos = 0.0
        wall_prev = time.monotonic()
        last_beat = 0.0
        try:
            while self._running:
                seek = self._seek_t
                if seek is not None:
                    self._seek_t = None
                    target = max(0.0, min(float(seek), self._t_end))
                    self.seekReset.emit()  # la GUI limpia; luego llega la historia
                    self._init_decoder()
                    f.close()
                    f = open(self.path, "rb")
                    self._set_live(False)
                    while self._running and self._seek_t is None:
                        line = self._read_line(f)
                        if line is None:
                            break
                        self._feed_line(line)
                        if self._last_rel_t >= target:
                            break
                    t_pos = self._last_rel_t
                    wall_prev = time.monotonic()
                    self._beat(t_pos, force=True)
                    continue
                if self._want_live:
                    self._want_live = False
                    self._paused = False
                    self._set_live(True)
                if self.live_mode:
                    if self._paused:  # pausar implica salir del vivo
                        self._set_live(False)
                        t_pos = self._last_rel_t
                        wall_prev = time.monotonic()
                        continue
                    line = self._read_line(f)
                    if line is not None:
                        self._feed_line(line)
                    else:
                        time.sleep(0.05)  # esperar datos nuevos: delay mínimo
                    t_pos = self._last_rel_t
                else:
                    time.sleep(0.05)
                    now_wall = time.monotonic()
                    if not self._paused:
                        t_pos += (now_wall - wall_prev) * self.speed
                    wall_prev = now_wall
                    while self._running and self._last_rel_t <= t_pos:
                        line = self._read_line(f)
                        if line is None:
                            break
                        self._feed_line(line)
                self._t_end = max(self._t_end, self._last_rel_t)
                now_wall = time.monotonic()
                if now_wall - last_beat > 0.4:
                    last_beat = now_wall
                    self._beat(self._last_rel_t if self.live_mode
                               else min(t_pos, self._t_end))
        finally:
            f.close()

    def _beat(self, t_now: float, force: bool = False) -> None:
        self.progress.emit(0.0, max(0.0, t_now), max(self._t_end, 1e-6))
        if self._marks_dirty or force:
            self._marks_dirty = False
            if self._marks:
                self.lapMarks.emit(sorted(self._marks.items()))

    def _feed_line(self, raw: bytes) -> None:
        raw = raw.strip()
        if not raw:
            return
        try:
            msg = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            return
        try:
            self._handle(msg)
        except Exception:
            pass  # una línea corrupta no corta la reproducción
        # marcas de vuelta: primera vez que algún auto arranca la vuelta n
        if self._laps_done:
            top = max(self._laps_done.values()) + 1
            if top > self._max_lap_seen and top > 1:
                self._max_lap_seen = top
                self._marks[top] = self._last_rel_t
                self._marks_dirty = True

    def _set_live(self, on: bool) -> None:
        if self.live_mode == on:
            return
        self.live_mode = on
        self.liveChanged.emit(on)
        self.statusChanged.emit(
            "Capture: LIVE — following the latest data" if on
            else f"Capture: replay at x{self.speed:g}"
        )
