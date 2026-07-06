"""Punto de entrada de la aplicación."""
from __future__ import annotations

import sys


def _selftest() -> int:
    """Verifica que todas las dependencias diferidas estén disponibles
    (útil para validar el build congelado): F1Telem.exe --selftest"""
    try:
        import fastf1  # noqa: F401
        import psycopg  # noqa: F401
        import pyqtgraph  # noqa: F401
        import requests  # noqa: F401
        import signalrcore  # noqa: F401

        from .capture_app import CaptureWindow  # noqa: F401
        from .sources import (  # noqa: F401
            CaptureSource, DemoSource, LiveSource, ReplaySource,
        )
        from .storage import PgWriter  # noqa: F401
        return 0
    except Exception:
        import traceback

        traceback.print_exc()
        return 1


def main() -> int:
    if "--selftest" in sys.argv:
        return _selftest()
    if "--capture" in sys.argv:
        from .capture_app import main as capture_main

        return capture_main()

    from PySide6.QtWidgets import QApplication

    from .ui.main_window import MainWindow
    from .ui.theme import apply_theme

    app = QApplication(sys.argv)
    app.setApplicationName("F1 Live Telemetry")
    app.setOrganizationName("f1telem")
    apply_theme(app)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
