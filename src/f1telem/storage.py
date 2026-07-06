"""Escritura opcional de telemetría en PostgreSQL local.

Corre en un hilo propio con una cola: la GUI encola lotes y el hilo los
inserta en tandas. Si psycopg no está instalado o la conexión falla, la
aplicación sigue funcionando y solo se avisa por la barra de estado.
"""
from __future__ import annotations

import queue
import threading

from PySide6.QtCore import QObject, Signal

from .models import Sample

_DDL = """
CREATE TABLE IF NOT EXISTS telemetry (
    id BIGSERIAL PRIMARY KEY,
    session_key TEXT NOT NULL,
    driver TEXT NOT NULL,
    lap INT NOT NULL,
    t DOUBLE PRECISION NOT NULL,
    dist_lap REAL NOT NULL,
    dist_total DOUBLE PRECISION NOT NULL,
    speed REAL,
    throttle REAL,
    brake REAL,
    rpm REAL,
    gear SMALLINT,
    drs SMALLINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS telemetry_session_driver_lap
    ON telemetry (session_key, driver, lap);
"""

_INSERT = """
INSERT INTO telemetry
    (session_key, driver, lap, t, dist_lap, dist_total,
     speed, throttle, brake, rpm, gear, drs)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""


class PgWriter(QObject):
    statusChanged = Signal(str)

    def __init__(self, pg_cfg: dict, parent=None):
        super().__init__(parent)
        self.cfg = pg_cfg
        self._queue: queue.Queue = queue.Queue(maxsize=500)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.session_key = ""
        self.rows_written = 0

    @property
    def active(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, session_key: str) -> None:
        self.stop()
        self.session_key = session_key
        self.rows_written = 0
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="pg-writer")
        self._thread.start()

    def stop(self) -> None:
        if self._thread is not None:
            self._stop.set()
            self._thread.join(timeout=5)
            self._thread = None

    def enqueue(self, samples: list[Sample]) -> None:
        if not self.active:
            return
        try:
            self._queue.put_nowait(samples)
        except queue.Full:
            pass  # ante una BD lenta se descartan lotes, nunca se frena la GUI

    # -------------------------------------------------------------- hilo BD

    def _run(self) -> None:
        try:
            import psycopg
        except ImportError:
            self.statusChanged.emit("PostgreSQL: psycopg is not installed; recording disabled.")
            return
        dsn = (
            f"host={self.cfg.get('host', 'localhost')} "
            f"port={self.cfg.get('port', 5432)} "
            f"dbname={self.cfg.get('dbname', 'f1telem')} "
            f"user={self.cfg.get('user', 'postgres')} "
            f"password={self.cfg.get('password', '')}"
        )
        try:
            with psycopg.connect(dsn, autocommit=True) as conn:
                conn.execute(_DDL)
                self.statusChanged.emit(f"PostgreSQL: recording to '{self.cfg.get('dbname')}'.")
                self._write_loop(conn)
        except Exception as exc:
            self.statusChanged.emit(f"PostgreSQL: error ({exc}); recording disabled.")

    def _write_loop(self, conn) -> None:
        pending: list[tuple] = []
        while not self._stop.is_set() or not self._queue.empty():
            try:
                samples = self._queue.get(timeout=0.5)
                pending.extend(
                    (
                        self.session_key, s.driver, s.lap, s.t, s.dist_lap, s.dist_total,
                        s.speed, s.throttle, s.brake, s.rpm, s.gear, s.drs,
                    )
                    for s in samples
                )
            except queue.Empty:
                pass
            if pending and (len(pending) >= 500 or self._queue.empty()):
                with conn.cursor() as cur:
                    cur.executemany(_INSERT, pending)
                self.rows_written += len(pending)
                pending.clear()
