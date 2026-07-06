"""Capturador: aplicación mínima que graba el stream de F1 Live Timing a un
archivo de captura, para que el visualizador lo siga en vivo (o lo rebobine).

Se abre con `F1LiveTelemetry.exe --capture` (o `python -m f1telem --capture`).
Incluye el login F1TV (mismo token que FastF1): el streaming en vivo requiere
una suscripción F1TV activa; sin token se intenta sin autenticación.
"""
from __future__ import annotations

import threading
import time
import webbrowser

from PySide6.QtCore import QTimer, Signal, QObject
from PySide6.QtWidgets import (
    QGridLayout, QGroupBox, QHBoxLayout, QLabel, QPushButton, QVBoxLayout,
    QWidget,
)

from . import config
from .sources.live import LiveSource
from .ui import theme


class _AuthWorker(QObject):
    """Login F1TV por navegador (flujo de FastF1) sin bloquear la GUI."""

    done = Signal(str)
    progress = Signal(str)

    TIMEOUT_S = 900  # the F1 login (password/2FA) can take a while

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True, name="f1tv-auth").start()

    def _run(self) -> None:
        try:
            from http.server import HTTPServer

            from fastf1.internals import f1auth

            f1auth._auth_finished.clear()
            httpd = HTTPServer(("127.0.0.1", 0), f1auth.AuthHandler)
            port = httpd.server_port
            server = threading.Thread(target=httpd.serve_forever, daemon=True)
            server.start()
            url = f"https://f1login.fastf1.dev?port={port}"
            self.progress.emit(
                f"Waiting for browser sign-in (up to {self.TIMEOUT_S // 60}"
                f" min) — keep this window open.\nIf the browser did not"
                f" open, go to: {url}"
            )
            webbrowser.open(url)
            ok = f1auth._auth_finished.wait(timeout=self.TIMEOUT_S)
            httpd.shutdown()
            token = f1auth._subscription_token if ok else None
            if token:
                f1auth.AUTH_DATA_FILE.write_text(token)
                self.done.emit("Signed in — F1TV token saved.")
            else:
                self.done.emit("Sign-in timed out or was cancelled.")
        except Exception as exc:
            self.done.emit(f"Sign-in failed: {exc}")


class CaptureWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("F1 Live Telemetry — Capture")
        self.setMinimumWidth(460)
        self.source: LiveSource | None = None
        self.path = ""
        self._samples = 0
        self._positions = 0
        self._drivers = 0

        lay = QVBoxLayout(self)
        box = QGroupBox("Live capture")
        grid = QGridLayout(box)
        self.path_label = QLabel("—")
        self.status_label = QLabel("Idle")
        self.status_label.setWordWrap(True)
        self.counter_label = QLabel("samples: 0 · positions: 0 · drivers: 0")
        self.token_label = QLabel("")
        grid.addWidget(QLabel("Output:"), 0, 0)
        grid.addWidget(self.path_label, 0, 1)
        grid.addWidget(QLabel("Status:"), 1, 0)
        grid.addWidget(self.status_label, 1, 1)
        grid.addWidget(QLabel("Data:"), 2, 0)
        grid.addWidget(self.counter_label, 2, 1)
        grid.addWidget(QLabel("F1TV:"), 3, 0)
        grid.addWidget(self.token_label, 3, 1)
        lay.addWidget(box)

        buttons = QHBoxLayout()
        self.toggle_btn = QPushButton("Stop capture")
        self.toggle_btn.clicked.connect(self._toggle)
        self.auth_btn = QPushButton("Sign in with F1TV…")
        self.auth_btn.clicked.connect(self._sign_in)
        buttons.addWidget(self.toggle_btn)
        buttons.addWidget(self.auth_btn)
        buttons.addStretch(1)
        lay.addLayout(buttons)
        hint = QLabel(
            "Open the main app and pick the \"Capture (recorded live)\" source\n"
            "to follow this file live or seek back in time."
        )
        hint.setStyleSheet(f"color: {theme.TEXT_MUTED};")
        lay.addWidget(hint)

        self._auth = _AuthWorker()
        self._auth.done.connect(self._on_auth_done)
        self._auth.progress.connect(self.status_label.setText)
        self._update_token_label()

        self._timer = QTimer(self)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self._refresh)
        self._timer.start()
        self._start()

    # ------------------------------------------------------------------

    def _update_token_label(self) -> None:
        if LiveSource.stored_token():
            self.token_label.setText("token found (authenticated)")
        else:
            self.token_label.setText("no token — sign in for full live data")

    def _start(self) -> None:
        rec_dir = config.recordings_dir()
        rec_dir.mkdir(parents=True, exist_ok=True)
        self.path = str(rec_dir / f"capture_{time.strftime('%Y%m%d_%H%M%S')}.jsonl")
        self.path_label.setText(self.path)
        self._samples = self._positions = self._drivers = 0
        source = LiveSource(record_path=self.path)
        source.statusChanged.connect(self.status_label.setText)
        source.failed.connect(self.status_label.setText)
        source.batch.connect(self._on_batch)
        source.positions.connect(self._on_positions)
        source.driversDiscovered.connect(self._on_drivers)
        self.source = source
        source.start()
        self.toggle_btn.setText("Stop capture")

    def _stop(self) -> None:
        if self.source is not None:
            source, self.source = self.source, None
            source.stop()
            source.wait(8000)
            source.deleteLater()
        self.status_label.setText("Stopped.")
        self.toggle_btn.setText("Start capture")

    def _toggle(self) -> None:
        if self.source is not None:
            self._stop()
        else:
            self._start()

    def _sign_in(self) -> None:
        self.auth_btn.setEnabled(False)
        self.token_label.setText("waiting for browser sign-in…")
        self._auth.start()

    def _on_auth_done(self, message: str) -> None:
        self.auth_btn.setEnabled(True)
        self.status_label.setText(message)
        self._update_token_label()

    def _on_batch(self, samples: list) -> None:
        self._samples += len(samples)

    def _on_positions(self, batch: list) -> None:
        self._positions += len(batch)

    def _on_drivers(self, infos: dict) -> None:
        self._drivers = len(infos)

    def _refresh(self) -> None:
        self.counter_label.setText(
            f"samples: {self._samples:,} · positions: {self._positions:,}"
            f" · drivers: {self._drivers}"
        )

    def closeEvent(self, event) -> None:
        self._stop()
        super().closeEvent(event)


def main() -> int:
    import sys

    from PySide6.QtWidgets import QApplication

    from .ui.theme import apply_theme

    app = QApplication(sys.argv)
    app.setApplicationName("F1 Live Telemetry Capture")
    apply_theme(app)
    window = CaptureWindow()
    window.show()
    return app.exec()
