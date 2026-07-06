"""Verificación del pipeline en vivo contra el servidor real de F1.

Fuera de sesión no llegan datos de autos, pero el negotiate + websocket +
Subscribe deben funcionar y puede llegar el snapshot inicial (R). Uso:
    python tests/live_check.py
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.stdout.reconfigure(errors="replace")

from PySide6.QtCore import QCoreApplication, Qt

from f1telem.sources.live import LiveSource

app = QCoreApplication(sys.argv)

statuses: list[str] = []
n_samples = [0]
src = LiveSource()
src.statusChanged.connect(lambda s: (statuses.append(s), print(f"  [status] {s}")),
                          Qt.DirectConnection)
src.failed.connect(lambda s: (statuses.append("FAILED: " + s), print(f"  [failed] {s}")),
                   Qt.DirectConnection)
src.batch.connect(lambda b: n_samples.__setitem__(0, n_samples[0] + len(b)),
                  Qt.DirectConnection)
src.start()

deadline = time.monotonic() + 20
while time.monotonic() < deadline:
    app.processEvents()
    time.sleep(0.05)
    if any("Connected" in s for s in statuses) and time.monotonic() > deadline - 12:
        break

src.stop()
src.wait(10000)

connected = any("Connected" in s or "Live:" in s for s in statuses)
print(f"conectado={connected}  muestras={n_samples[0]}  estados={len(statuses)}")
sys.stdout.flush()
os._exit(0 if connected else 1)
