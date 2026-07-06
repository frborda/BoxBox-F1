"""Punto de entrada de la aplicación."""
from __future__ import annotations

import sys


def _selftest() -> int:
    """Verifica que todas las dependencias diferidas estén disponibles
    (útil para validar el build congelado): F1Telem.exe --selftest"""
    try:
        import aiohttp  # noqa: F401
        import fastf1  # noqa: F401
        import psycopg  # noqa: F401
        import pyqtgraph  # noqa: F401

        from .sources import DemoSource, LiveSource, ReplaySource  # noqa: F401
        from .storage import PgWriter  # noqa: F401
        return 0
    except Exception:
        import traceback

        traceback.print_exc()
        return 1


def main() -> int:
    if "--selftest" in sys.argv:
        return _selftest()

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
