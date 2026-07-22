"""Punto de entrada de la aplicación."""
from __future__ import annotations

import sys


def _selftest() -> int:
    """Verifica que todas las dependencias diferidas estén disponibles
    (útil para validar el build congelado): BoxBox-F1.exe --selftest"""
    try:
        import fastf1  # noqa: F401
        import pyqtgraph  # noqa: F401
        import requests  # noqa: F401
        import signalrcore  # noqa: F401

        from .capture_app import CaptureWindow  # noqa: F401
        from .sources import (  # noqa: F401
            CaptureSource, DemoSource, LiveSource, ReplaySource,
        )
        return 0
    except Exception:
        import traceback

        traceback.print_exc()
        return 1


def _handle_protocol(url: str) -> int:
    """El navegador abrió f1telemetry://auth?token=... (extensión): guardar
    el token F1TV y avisar. El capturador abierto lo detecta solo."""
    from PySide6.QtWidgets import QApplication, QMessageBox

    from .capture_app import save_token, token_from_url
    from .ui.theme import apply_theme

    app = QApplication(sys.argv)
    apply_theme(app)
    token = token_from_url(url)
    if token:
        save_token(token)
        QMessageBox.information(
            None, "BoxBox-F1",
            "F1TV token saved — the capturer will pick it up automatically.",
        )
        return 0
    QMessageBox.warning(
        None, "BoxBox-F1", "No F1TV token found in the link."
    )
    return 1


def main() -> int:
    if "--selftest" in sys.argv:
        return _selftest()
    for arg in sys.argv[1:]:
        if arg.startswith("f1telemetry://"):
            return _handle_protocol(arg)
    if "--capture" in sys.argv:
        from .capture_app import main as capture_main

        return capture_main()

    from PySide6.QtWidgets import QApplication

    from .ui.main_window import MainWindow
    from .ui.theme import apply_theme

    app = QApplication(sys.argv)
    app.setApplicationName("BoxBox-F1")
    app.setOrganizationName("f1telem")
    apply_theme(app)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
