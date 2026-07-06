"""Verifica que la app real abre y cierra limpiamente (ruta normal de main)."""
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from f1telem.ui.main_window import MainWindow
from f1telem.ui.theme import apply_theme

app = QApplication(sys.argv)
apply_theme(app)
window = MainWindow()
window.show()
QTimer.singleShot(1500, window.close)
QTimer.singleShot(2000, app.quit)
code = app.exec()
print(f"exec() devolvio {code}")
raise SystemExit(code)
