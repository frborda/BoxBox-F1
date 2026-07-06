"""Genera capturas PNG de los 3 modos con la fuente demo (offscreen)."""
from __future__ import annotations

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

out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")
out_dir.mkdir(parents=True, exist_ok=True)

app = QApplication(sys.argv)
apply_theme(app)
win = MainWindow()
win.show()


def pump(seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)


win.speed_combo.setCurrentIndex(4)  # x25
win.connect_btn.click()
pump(2.0)
for i in range(4):
    win.driver_list.item(i).setCheckState(Qt.Checked)
pump(10.0)

for idx, name in [(0, "modo_carrera"), (1, "modo_carrera2"), (2, "modo_qualy"), (3, "modo_tiempos")]:
    win.mode_combo.setCurrentIndex(idx)
    if idx == 2:
        win._refresh_ref_laps()
        if win.ref_lap_combo.count():
            win.ref_set_btn.click()
    pump(3.0)
    win.grab().save(str(out_dir / f"{name}.png"))
    print(f"guardado {out_dir / (name + '.png')}")

win.connect_btn.click()
pump(0.5)
sys.stdout.flush()
os._exit(0)
