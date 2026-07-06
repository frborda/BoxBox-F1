"""Captura del modo Carrera con valores en picos activados (offscreen)."""
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from f1telem.ui.main_window import MainWindow
from f1telem.ui.theme import apply_theme

out = Path(sys.argv[1])
out.parent.mkdir(parents=True, exist_ok=True)
app = QApplication(sys.argv)
apply_theme(app)
win = MainWindow()
win.show()


def pump(seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)


win.speed_combo.setCurrentIndex(4)
win.connect_btn.click()
pump(2.0)
for i in range(2):  # 2 pilotos para ver los picos con claridad
    win.driver_list.item(i).setCheckState(Qt.Checked)
win.peaks_check.setChecked(True)
pump(8.0)
win.grab().save(str(out))
print(f"guardado {out}")
win.connect_btn.click()
pump(0.3)
sys.stdout.flush()
os._exit(0)
