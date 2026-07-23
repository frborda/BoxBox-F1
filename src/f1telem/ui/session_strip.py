"""Tira de estado de sesión: nombre de la tanda, bandera vigente, vuelta
actual/total (carrera) o tiempo restante, y el último mensaje de dirección
de carrera. Pensada como banner sobre la zona central (desacoplable)."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QSizePolicy, QWidget

from ..hub import DataHub
from . import theme

_CLEAR = ("TRACK CLEAR", "#21a05a")


def fmt_clock(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


class SessionStrip(QWidget):
    def __init__(self, hub: DataHub, parent=None):
        super().__init__(parent)
        self.hub = hub
        lay = QHBoxLayout(self)
        lay.setContentsMargins(6, 2, 6, 2)
        lay.setSpacing(12)

        self.session_label = QLabel("")
        self.session_label.setStyleSheet(
            f"color: {theme.TEXT}; font-weight: bold;")
        lay.addWidget(self.session_label)
        self.flag_label = QLabel("")
        self.flag_label.setVisible(False)
        lay.addWidget(self.flag_label)
        self.lap_label = QLabel("")
        self.lap_label.setStyleSheet(f"color: {theme.TEXT}; font-weight: bold;")
        lay.addWidget(self.lap_label)
        self.clock_label = QLabel("")
        self.clock_label.setStyleSheet(f"color: {theme.TEXT_MUTED};")
        lay.addWidget(self.clock_label)
        self.rcm_label = QLabel("")
        self.rcm_label.setStyleSheet(f"color: {theme.TEXT_MUTED};")
        self.rcm_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self.rcm_label, stretch=1)
        # los textos largos (sesión y race control) no deben imponer un
        # ancho mínimo: con la ventana angosta se recortan y listo
        for label in (self.session_label, self.rcm_label):
            label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setMinimumWidth(90)

    def clear_data(self) -> None:
        for label in (self.session_label, self.lap_label,
                      self.clock_label, self.rcm_label):
            label.setText("")
        self.flag_label.setVisible(False)

    def refresh(self) -> None:
        hub = self.hub
        meta = hub.session_meta
        parts = [p for p in (meta.get("meeting"), meta.get("name")) if p]
        self.session_label.setText(" — ".join(parts))

        # bandera vigente al instante más reciente de datos
        text, color = _CLEAR
        found = bool(hub.total_samples)
        for t0, t1, code in hub.track_status:
            if t0 <= hub.latest_t <= t1 and code in theme.TRACK_STATUS:
                text, color = theme.TRACK_STATUS[code]
                break
        if found:
            self.flag_label.setText(text)
            self.flag_label.setStyleSheet(
                f"background: {color}; color: #111318; font-weight: bold;"
                "padding: 1px 8px; border-radius: 3px;"
            )
        self.flag_label.setVisible(found)

        cur, total = hub.lap_count
        if total:
            if cur <= 0:  # el replay no trae la vuelta en curso: usar el líder
                cur = max((b.current_lap() for b in hub.buffers.values()
                           if b.n), default=0)
            self.lap_label.setText(f"LAP {cur}/{total}" if cur else "")
        else:
            self.lap_label.setText("")

        remaining = hub.clock_remaining()
        self.clock_label.setText(
            f"⏱ {fmt_clock(remaining)}" if remaining is not None else "")

        # último mensaje HASTA el instante del timeline (sin spoilers)
        msg = next((m for m in reversed(hub.race_control)
                    if float(m.get("t", 0.0)) <= hub.latest_t), None)
        if msg is not None:
            lap = msg.get("lap")
            prefix = f"L{lap} · " if lap else ""
            color = theme.FLAG_COLORS.get(
                str(msg.get("flag", "")).upper(),
                "#ff9f1a" if msg.get("mode") else theme.TEXT_MUTED)
            self.rcm_label.setStyleSheet(f"color: {color};")
            self.rcm_label.setText(prefix + str(msg.get("message", "")))
        else:
            self.rcm_label.setText("")
