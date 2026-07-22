"""Diálogo de actualización: aviso de nueva versión, descarga e instalación."""
from __future__ import annotations

import sys
import threading
import webbrowser

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QHBoxLayout, QLabel, QMessageBox,
    QProgressBar, QPushButton, QTextBrowser, QVBoxLayout,
)

from .. import __version__, config, updater

_TITLE = "BoxBox-F1"


class UpdateChecker(QObject):
    """Consulta el último release de GitHub sin bloquear la GUI."""

    finished = Signal(object)  # UpdateInfo si hay versión más nueva, o None
    failed = Signal(str)

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True, name="update-check").start()

    def _run(self) -> None:
        try:
            updater.cleanup()
            info = updater.check_latest()
            self.finished.emit(info if updater.is_newer(info.version) else None)
        except Exception as exc:
            self.failed.emit(str(exc))


class _InstallWorker(QObject):
    """Descarga, extrae y lanza el instalador sin bloquear la GUI."""

    progress = Signal(int, str)  # porcentaje, texto de estado
    done = Signal()
    failed = Signal(str)         # "" cuando el usuario canceló

    def __init__(self, info: updater.UpdateInfo, do_main: bool = True,
                 do_capture: bool = True):
        super().__init__()
        self.info = info
        self.do_main = do_main
        self.do_capture = do_capture
        self.cancel = threading.Event()

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True, name="update-install").start()

    def _run(self) -> None:
        try:
            def on_progress(done: int, total: int) -> None:
                pct = int(done * 100 / total) if total else 0
                self.progress.emit(
                    pct, f"Downloading… {done / 1e6:.0f} / {total / 1e6:.0f} MB"
                )

            zip_path = updater.download(self.info, on_progress, self.cancel)
            self.progress.emit(100, "Extracting…")
            payload = updater.extract(zip_path)
            self.progress.emit(100, "Starting installer…")
            updater.launch_installer(payload, sys.argv[1:],
                                     do_main=self.do_main,
                                     do_capture=self.do_capture)
            self.done.emit()
        except updater.InstallCancelled:
            self.failed.emit("")
        except Exception as exc:
            self.failed.emit(str(exc))


class UpdateDialog(QDialog):
    """Muestra la versión disponible con sus notas y ofrece instalarla."""

    def __init__(self, info: updater.UpdateInfo, cfg: dict, parent=None,
                 allow_install: bool = True):
        super().__init__(parent)
        self.info = info
        self.cfg = cfg
        # el capturador no se auto-instala: su carpeta está mapeada mientras
        # graba; la actualización la aplica la app principal
        self._allow_install = allow_install and updater.can_autoupdate()
        self._worker: _InstallWorker | None = None
        self.setWindowTitle(f"{_TITLE} — Update")
        self.setMinimumWidth(540)

        lay = QVBoxLayout(self)
        head = QLabel(
            f"<b>Version {info.version} is available</b>"
            f" — you have {__version__}."
        )
        lay.addWidget(head)
        notes = QTextBrowser()
        notes.setOpenExternalLinks(True)
        notes.setMarkdown(info.notes.strip() or "*(no release notes)*")
        notes.setMaximumHeight(240)
        lay.addWidget(notes)
        # qué instalar: la app y el capturador se actualizan por separado
        self.main_check = QCheckBox("Update the main app (it will restart)")
        self.main_check.setChecked(True)
        self.cap_check = QCheckBox(
            "Update the capturer (replaced once it is closed — a running"
            " capture keeps recording)"
        )
        self.cap_check.setChecked(True)
        if self._allow_install:
            lay.addWidget(self.main_check)
            lay.addWidget(self.cap_check)
        else:
            self.main_check.setVisible(False)
            self.cap_check.setVisible(False)
        self.startup_check = QCheckBox("Check for updates at startup")
        self.startup_check.setChecked(
            bool(self.cfg.setdefault("updates", {}).get("check_on_startup", True))
        )
        self.startup_check.toggled.connect(self._startup_toggled)
        lay.addWidget(self.startup_check)
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setVisible(False)
        lay.addWidget(self.bar)
        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        lay.addWidget(self.status_label)

        btns = QHBoxLayout()
        self.install_btn = QPushButton("Download and install")
        if not self._allow_install:
            # desde el código, sin permisos, o en el capturador: solo la web
            self.install_btn.setText("Open download page")
            self.install_btn.setToolTip(
                "Auto-install runs from the main app's packaged build. In the"
                " capturer, update from the main app so the capture keeps"
                " running."
            )
        self.web_btn = QPushButton("View on GitHub")
        self.skip_btn = QPushButton("Skip this version")
        self.later_btn = QPushButton("Later")
        for btn in (self.install_btn, self.web_btn, self.skip_btn, self.later_btn):
            btns.addWidget(btn)
        btns.addStretch(1)
        lay.addLayout(btns)

        self.install_btn.clicked.connect(self._install)
        self.web_btn.clicked.connect(lambda: webbrowser.open(self.info.html_url))
        self.skip_btn.clicked.connect(self._skip)
        self.later_btn.clicked.connect(self.reject)

    # ------------------------------------------------------------------

    def _startup_toggled(self, on: bool) -> None:
        self.cfg.setdefault("updates", {})["check_on_startup"] = on
        config.save_config(self.cfg)

    def _skip(self) -> None:
        self.cfg.setdefault("updates", {})["skip_version"] = self.info.version
        config.save_config(self.cfg)
        self.reject()

    def _install(self) -> None:
        if not self._allow_install:
            webbrowser.open(self.info.html_url)
            self.accept()
            return
        if self._worker is not None:  # segundo clic = cancelar la descarga
            self._worker.cancel.set()
            return
        do_main = self.main_check.isChecked()
        do_capture = self.cap_check.isChecked()
        if not (do_main or do_capture):
            QMessageBox.information(
                self, _TITLE, "Pick at least one component to update."
            )
            return
        self._did_main = do_main
        self.skip_btn.setEnabled(False)
        self.later_btn.setEnabled(False)
        self.main_check.setEnabled(False)
        self.cap_check.setEnabled(False)
        self.install_btn.setText("Cancel")
        self.bar.setVisible(True)
        self.status_label.setText("Starting download…")
        self._worker = _InstallWorker(self.info, do_main, do_capture)
        self._worker.progress.connect(self._on_progress)
        self._worker.done.connect(self._on_done)
        self._worker.failed.connect(self._on_failed)
        self._worker.start()

    def _on_progress(self, pct: int, text: str) -> None:
        self.bar.setValue(pct)
        self.status_label.setText(text)

    def _on_done(self) -> None:
        self._worker = None
        if getattr(self, "_did_main", True):
            self.status_label.setText("Update ready — restarting…")
            # el instalador espera a que la app termine; cerrar todo la termina
            self.accept()
            QApplication.closeAllWindows()
            return
        # solo el capturador: la app sigue abierta; el instalador queda en
        # segundo plano y reemplaza el capturador apenas se cierre
        self.bar.setVisible(False)
        self.install_btn.setEnabled(False)
        self.install_btn.setText("Installer running")
        self.later_btn.setEnabled(True)
        self.later_btn.setText("Close")
        self.status_label.setText(
            "Installer running in the background: the capturer will be"
            " replaced as soon as it is closed (waits up to 60 min)."
        )

    def _on_failed(self, message: str) -> None:
        self._worker = None
        self.bar.setVisible(False)
        self.bar.setValue(0)
        self.status_label.setText("")
        self.skip_btn.setEnabled(True)
        self.later_btn.setEnabled(True)
        self.install_btn.setText("Download and install")
        if message:
            QMessageBox.warning(self, _TITLE, f"Update failed:\n{message}")

    def closeEvent(self, event) -> None:
        if self._worker is not None:
            self._worker.cancel.set()
        super().closeEvent(event)


def run_check(parent, cfg: dict, silent: bool, allow_install: bool = True) -> None:
    """Busca actualizaciones y muestra el resultado.

    silent=True (arranque): solo aparece el diálogo si hay una versión nueva
    no omitida; los errores de red se ignoran. silent=False (a demanda):
    también avisa "estás al día" o el error. allow_install=False (capturador):
    el diálogo no ofrece auto-instalar, solo abrir la descarga.
    """
    if getattr(parent, "_update_checker", None) is not None:
        return  # ya hay una comprobación en marcha
    checker = UpdateChecker()
    parent._update_checker = checker  # evitar que lo recoja el GC

    def on_finished(info) -> None:
        parent._update_checker = None
        if info is None:
            if not silent:
                QMessageBox.information(
                    parent, _TITLE, f"You're up to date (v{__version__})."
                )
            return
        skip = str(cfg.get("updates", {}).get("skip_version", ""))
        if silent and info.version == skip:
            return
        dialog = UpdateDialog(info, cfg, parent, allow_install=allow_install)
        dialog.setAttribute(Qt.WA_DeleteOnClose)
        dialog.show()

    def on_failed(message: str) -> None:
        parent._update_checker = None
        if not silent:
            QMessageBox.warning(
                parent, _TITLE, f"Could not check for updates:\n{message}"
            )

    checker.finished.connect(on_finished)
    checker.failed.connect(on_failed)
    checker.start()
