"""Capturador: aplicación mínima que graba el stream de F1 Live Timing a un
archivo de captura, para que el visualizador lo siga en vivo (o lo rebobine).

Se abre con `F1LiveTelemetry.exe --capture` (o `python -m f1telem --capture`).
Incluye el login F1TV (mismo token que FastF1): el streaming en vivo requiere
una suscripción F1TV activa; sin token se intenta sin autenticación.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.parse
import webbrowser

from PySide6.QtCore import QEvent, Qt, QTimer, Signal, QObject
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QFileDialog, QGridLayout, QGroupBox, QHBoxLayout, QInputDialog, QLabel,
    QMenu, QMessageBox, QPushButton, QSystemTrayIcon, QVBoxLayout, QWidget,
)

from . import __version__, config
from .sources.importer import ImportPlayer, parse_hms
from .sources.live import LiveSource
from .ui import theme
from .ui.update_dialog import run_check

# archivo de trabajo ÚNICO de la reproducción importada: se sobrescribe en
# cada importación y se borra al salir — reproducir no acumula archivos
IMPORT_PLAYBACK_NAME = "import_live.jsonl"


def _tray_icon() -> QIcon:
    """Ícono de la bandeja: círculo rojo F1 con la C de capture."""
    pix = QPixmap(32, 32)
    pix.fill(Qt.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor("#e10600"))
    p.setPen(Qt.NoPen)
    p.drawEllipse(2, 2, 28, 28)
    p.setPen(QColor("#ffffff"))
    font = p.font()
    font.setBold(True)
    font.setPointSize(12)
    p.setFont(font)
    p.drawText(pix.rect(), Qt.AlignCenter, "C")
    p.end()
    return QIcon(pix)


def _pna_handler(base):
    """Handler del servidor de auth con el header de Private/Local Network
    Access: Chrome 130+ bloquea con "Failed to fetch" cualquier pedido de una
    página/extensión hacia localhost si el preflight no lo trae (por eso la
    extensión vieja de FastF1 fallaba con "Could not connect to the local
    FastF1 application")."""
    class _Handler(base):
        def _send_cors_headers(self):
            super()._send_cors_headers()
            self.send_header("Access-Control-Allow-Private-Network", "true")
    return _Handler


def extract_subscription_token(text: str) -> str | None:
    """Saca el subscriptionToken de lo que el usuario pegue: la cookie
    login-session (JSON, quizá URL-encodeada) o el JWT pelado."""
    text = (text or "").strip().strip('"')
    if not text:
        return None
    for candidate in (text, urllib.parse.unquote(text)):
        try:
            data = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(data, dict):
            tok = data.get("data", {}).get("subscriptionToken") or \
                data.get("subscriptionToken")
            if isinstance(tok, str) and tok:
                return tok
    # JWT crudo (header.payload.firma, base64url)
    if text.count(".") == 2 and text[:2] == "ey":
        return text
    return None


def save_token(token: str) -> None:
    from fastf1.internals import f1auth
    f1auth._subscription_token = token
    f1auth.AUTH_DATA_FILE.write_text(token)


def token_from_url(url: str) -> str | None:
    """Token del enlace f1telemetry://auth?token=... que abre la extensión
    (vía el diálogo nativo del navegador 'Abrir F1 Live Telemetry')."""
    parsed = urllib.parse.urlparse(url)
    raw = (urllib.parse.parse_qs(parsed.query).get("token") or [""])[0]
    if not raw:
        raw = urllib.parse.unquote(parsed.netloc + parsed.path)
    return extract_subscription_token(raw)


def register_protocol() -> None:
    """Asocia f1telemetry:// a este exe (HKCU, sin permisos de admin): la
    extensión puede entregar el token con el diálogo nativo del navegador,
    sin pasar por localhost. Solo aplica al build congelado."""
    if not getattr(sys, "frozen", False):
        return
    try:
        import winreg

        root = winreg.CreateKey(
            winreg.HKEY_CURRENT_USER, r"Software\Classes\f1telemetry"
        )
        winreg.SetValueEx(root, None, 0, winreg.REG_SZ, "URL:F1 Live Telemetry")
        winreg.SetValueEx(root, "URL Protocol", 0, winreg.REG_SZ, "")
        cmd = winreg.CreateKey(root, r"shell\open\command")
        winreg.SetValueEx(cmd, None, 0, winreg.REG_SZ,
                          f'"{sys.executable}" "%1"')
        winreg.CloseKey(cmd)
        winreg.CloseKey(root)
    except OSError:
        pass  # sin registro queda el flujo por localhost / pegar token


class _AuthWorker(QObject):
    """Login F1TV por navegador (flujo de FastF1) sin bloquear la GUI."""

    done = Signal(str)
    progress = Signal(str)

    TIMEOUT_S = 900  # the F1 login (password/2FA) can take a while

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True, name="f1tv-auth").start()

    def _run(self) -> None:
        try:
            import socket
            from http.server import HTTPServer

            from fastf1.internals import f1auth

            handler = _pna_handler(f1auth.AuthHandler)
            f1auth._auth_finished.clear()
            httpd = HTTPServer(("127.0.0.1", 0), handler)
            port = httpd.server_port
            server = threading.Thread(target=httpd.serve_forever, daemon=True)
            server.start()
            # The browser extension posts to "localhost", which resolves to
            # ::1 first on Windows — listen there too, on the same port.
            httpd6 = None
            try:
                class _V6Server(HTTPServer):
                    address_family = socket.AF_INET6

                httpd6 = _V6Server(("::1", port), handler)
                threading.Thread(
                    target=httpd6.serve_forever, daemon=True
                ).start()
            except OSError:
                pass
            url = f"https://f1login.fastf1.dev?port={port}"
            self.progress.emit(
                f"Waiting for browser sign-in (up to {self.TIMEOUT_S // 60}"
                f" min) — keep this window open.\nIf the browser did not"
                f" open, go to: {url}\nIf it fails, use \"Paste token…\"."
            )
            webbrowser.open(url)
            ok = f1auth._auth_finished.wait(timeout=self.TIMEOUT_S)
            httpd.shutdown()
            if httpd6 is not None:
                httpd6.shutdown()
            token = f1auth._subscription_token if ok else None
            if token:
                save_token(token)
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
        self.cfg = config.load_config()
        self.source: LiveSource | None = None
        self.path = ""
        self._samples = 0
        self._positions = 0
        self._drivers = 0
        self._import_mode = False

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
        self.toggle_btn = QPushButton("Start live capture")
        self.toggle_btn.clicked.connect(self._toggle)
        self.auth_btn = QPushButton("Sign in with F1TV…")
        self.auth_btn.clicked.connect(self._sign_in)
        self.paste_btn = QPushButton("Paste token…")
        self.paste_btn.setToolTip(
            "If the browser sign-in fails (e.g. the FastF1 extension can't\n"
            "reach the app), paste the F1TV login-session cookie or token here."
        )
        self.paste_btn.clicked.connect(self._paste_token)
        self.import_btn = QPushButton("Import capture…")
        self.import_btn.setToolTip(
            "Replay a recorded capture as if it were arriving live: the full\n"
            "history up to the chosen start minute is delivered at once (the\n"
            "main app always sees the whole race) and from there it plays in\n"
            "real time, just like a live session."
        )
        self.import_btn.clicked.connect(self._import)
        buttons.addWidget(self.toggle_btn)
        buttons.addWidget(self.auth_btn)
        buttons.addWidget(self.paste_btn)
        buttons.addWidget(self.import_btn)
        buttons.addStretch(1)
        self.version_btn = QPushButton(f"v{__version__}")
        self.version_btn.setFlat(True)
        self.version_btn.setToolTip("Check for updates")
        self.version_btn.clicked.connect(
            lambda: run_check(self, self.cfg, silent=False, allow_install=False)
        )
        buttons.addWidget(self.version_btn)
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
        register_protocol()  # habilita el enlace f1telemetry:// del navegador
        self._tok_present = LiveSource.stored_token() is not None
        self._refresh_n = 0
        self._write_heartbeat()  # el visualizador sabe que estamos abiertos
        self._update_token_label()

        self._timer = QTimer(self)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self._refresh)
        self._timer.start()
        # arranque INACTIVO a propósito: conectar al vivo recién cuando el
        # usuario lo pide evita crear un archivo de captura fantasma (solo
        # heartbeats) al que el visualizador se engancharía por error
        self.status_label.setText(
            "Idle — press Start live capture, or Import capture… to replay "
            "a recording as live.")

        # bandeja del sistema: minimizar o cerrar solo esconde la ventana
        # (la captura sigue); salir de verdad es clic derecho -> Exit
        self._really_quit = False
        self._tray: QSystemTrayIcon | None = None
        if QSystemTrayIcon.isSystemTrayAvailable():
            self._tray = QSystemTrayIcon(_tray_icon(), self)
            self._tray.setToolTip("F1 Telemetry Capture")
            tray_menu = QMenu()
            show_action = tray_menu.addAction("Show capturer")
            show_action.triggered.connect(self._show_from_tray)
            tray_menu.addSeparator()
            quit_action = tray_menu.addAction("Exit")
            quit_action.triggered.connect(self._quit)
            self._tray.setContextMenu(tray_menu)
            self._tray.activated.connect(self._on_tray_activated)
            self._tray.show()

        if (bool(self.cfg.get("updates", {}).get("check_on_startup", True))
                and not os.environ.get("F1TELEM_NO_UPDATE_CHECK")):
            QTimer.singleShot(
                3000, lambda: run_check(self, self.cfg, silent=True,
                                        allow_install=False)
            )

    # ------------------------------------------------------------------

    def _update_token_label(self) -> None:
        if LiveSource.stored_token():
            self.token_label.setText("token found (authenticated)")
        else:
            self.token_label.setText("no token — sign in for full live data")

    def _paste_token(self) -> None:
        text, ok = QInputDialog.getMultiLineText(
            self, "Paste F1TV token",
            "Sign in at f1tv.formula1.com, then in DevTools open\n"
            "Application → Cookies → the 'login-session' cookie and paste its\n"
            "value here (or paste the subscription token / JWT directly):",
        )
        if not ok:
            return
        token = extract_subscription_token(text)
        if token is None:
            QMessageBox.warning(
                self, "F1 Live Telemetry",
                "Could not find a token in what you pasted. Expected the\n"
                "'login-session' cookie value or a JWT (starts with 'ey').",
            )
            return
        save_token(token)
        self.status_label.setText("F1TV token saved from paste.")
        self._update_token_label()

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
        self.toggle_btn.setText("Stop live capture")

    def _stop(self) -> None:
        if self.source is not None:
            source, self.source = self.source, None
            source.stop()
            source.wait(8000)
            source.deleteLater()
        self.status_label.setText("Stopped.")
        self.toggle_btn.setText("Start live capture")

    def _toggle(self) -> None:
        if self._import_mode:
            # frenar la importación deja el capturador inactivo (el vivo se
            # arranca a mano, nunca solo)
            self._stop()
            self._import_mode = False
            self.import_btn.setEnabled(True)
            self._cleanup_playback_file()
            return
        if self.source is not None:
            self._stop()
        else:
            self._start()

    # ------------------------------------------------------------ importar

    @staticmethod
    def _fmt_mmss(seconds: float) -> str:
        seconds = max(0.0, seconds)
        return f"{int(seconds // 60)}:{int(seconds % 60):02d}"

    def _cleanup_playback_file(self) -> None:
        """Borra el archivo de trabajo de la reproducción (mejor esfuerzo: si
        el visualizador todavía lo tiene abierto, quedará para la próxima
        sobrescritura)."""
        try:
            (config.recordings_dir() / IMPORT_PLAYBACK_NAME).unlink()
        except OSError:
            pass

    def _import(self) -> None:
        rec_dir = config.recordings_dir()
        path, _ = QFileDialog.getOpenFileName(
            self, "Import capture", str(rec_dir), "Captures (*.jsonl)"
        )
        if not path:
            return
        out = str(rec_dir / IMPORT_PLAYBACK_NAME)
        if os.path.normcase(os.path.abspath(path)) == os.path.normcase(out):
            QMessageBox.warning(
                self, "F1 Live Telemetry",
                "That file is the playback working file itself — pick a"
                " recorded capture.",
            )
            return
        text, ok = QInputDialog.getText(
            self, "Import capture",
            "Start real-time playback at (hh:mm:ss). The full history up to\n"
            "that point is delivered instantly, so the main app always sees\n"
            "the whole race:",
            text="00:00:00",
        )
        if not ok:
            return
        start_at = parse_hms(text)
        if start_at is None:
            QMessageBox.warning(
                self, "F1 Live Telemetry",
                f"Could not parse '{text}'. Use hh:mm:ss, e.g. 00:01:30.",
            )
            return
        self._stop()
        rec_dir.mkdir(parents=True, exist_ok=True)
        self.path = out
        self.path_label.setText(out)
        player = ImportPlayer(path, out, start_at=start_at)
        player.progress.connect(self._on_import_progress)
        player.failed.connect(self.status_label.setText)
        player.statusChanged.connect(self.status_label.setText)
        player.finished.connect(self._on_import_finished)
        self.source = player
        self._import_mode = True
        self.import_btn.setEnabled(False)
        self.toggle_btn.setText("Stop import")
        player.start()

    def _on_import_progress(self, t0: float, t_now: float, t_end: float) -> None:
        self.counter_label.setText(
            f"import: {self._fmt_mmss(t_now)} / {self._fmt_mmss(t_end)}"
        )

    def _on_import_finished(self) -> None:
        """La reproducción terminó sola: liberar la UI (volver a inactivo)
        sin borrar el archivo de trabajo — el visualizador puede seguir
        navegando lo reproducido hasta que se importe otra cosa."""
        if not self._import_mode:
            return  # ya la frenó el usuario con Stop import
        if self.source is not None and not self.source.isFinished():
            return  # finished espurio (no debería pasar)
        if self.source is not None:
            self.source.deleteLater()
            self.source = None
        self._import_mode = False
        self.import_btn.setEnabled(True)
        self.toggle_btn.setText("Start live capture")
        self.status_label.setText(
            "Import finished — the recording stays available in the main "
            "app. Import another capture or start a live capture.")

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
        # el visualizador puede pedir que la ventana (escondida en la
        # bandeja) se muestre: aparece al frente y se consume el pedido
        show_req = config.capture_show_path()
        if show_req.exists():
            try:
                show_req.unlink()
            except OSError:
                pass
            self._show_from_tray()
        # otro proceso pudo guardar el token (enlace f1telemetry:// del
        # navegador): reflejarlo sin reiniciar
        self._refresh_n += 1
        if self._refresh_n % 2 == 0:  # latido cada ~1 s para el visualizador
            self._write_heartbeat()
        if self._refresh_n % 4 == 0:
            present = LiveSource.stored_token() is not None
            if present != self._tok_present:
                self._tok_present = present
                self._update_token_label()
                if present:
                    self.status_label.setText("F1TV token detected — saved.")

    def _write_heartbeat(self) -> None:
        try:
            lock = config.capture_lock_path()
            lock.parent.mkdir(parents=True, exist_ok=True)
            lock.write_text(str(os.getpid()), encoding="utf-8")
        except OSError:
            pass

    # -------------------------------------------------- bandeja del sistema

    def _on_tray_activated(self, reason) -> None:
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self._show_from_tray()

    def _show_from_tray(self) -> None:
        self.setWindowState(self.windowState() & ~Qt.WindowMinimized)
        self.show()
        self.raise_()
        self.activateWindow()

    def _quit(self) -> None:
        self._really_quit = True
        self.close()

    def changeEvent(self, event) -> None:
        # minimizar -> esconder a la bandeja (la captura sigue corriendo)
        if (event.type() == QEvent.WindowStateChange and self.isMinimized()
                and self._tray is not None):
            QTimer.singleShot(0, self.hide)
        super().changeEvent(event)

    def closeEvent(self, event) -> None:
        if self._tray is not None and not self._really_quit:
            # la X solo esconde a la bandeja; salir es clic derecho -> Exit
            event.ignore()
            self.hide()
            self._tray.showMessage(
                "F1 Telemetry Capture",
                "Still running in the tray. Right-click the icon and choose "
                "Exit to quit.")
            return
        self._stop()
        if self._import_mode:
            self._cleanup_playback_file()
        try:
            config.capture_lock_path().unlink(missing_ok=True)
        except OSError:
            pass
        if self._tray is not None:
            self._tray.hide()
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
