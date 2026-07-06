"""Tema oscuro de la aplicación y estilo base de los gráficos."""
from __future__ import annotations

import pyqtgraph as pg
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

# Superficies y tintas (grilla recesiva, texto en tokens de texto — la
# identidad de cada serie la lleva solo el color del trazo)
SURFACE = "#111318"
SURFACE_ALT = "#191c23"
BORDER = "#2a2e37"
TEXT = "#e8eaed"
TEXT_MUTED = "#9aa0a6"
ACCENT = "#4c8dff"

GRID_ALPHA = 0.12
LINE_WIDTH = 2.0

# estado de pista: código del feed -> (texto, color)
TRACK_STATUS = {
    "2": ("YELLOW FLAG", "#ffd12e"),
    "4": ("SAFETY CAR", "#ff9f1a"),
    "5": ("RED FLAG", "#ff4d4d"),
    "6": ("VIRTUAL SC", "#ffd12e"),
    "7": ("VSC ENDING", "#ffd12e"),
}


def apply_theme(app: QApplication) -> None:
    app.setStyle("Fusion")
    pal = QPalette()
    pal.setColor(QPalette.Window, QColor(SURFACE_ALT))
    pal.setColor(QPalette.WindowText, QColor(TEXT))
    pal.setColor(QPalette.Base, QColor(SURFACE))
    pal.setColor(QPalette.AlternateBase, QColor(SURFACE_ALT))
    pal.setColor(QPalette.Text, QColor(TEXT))
    pal.setColor(QPalette.Button, QColor(SURFACE_ALT))
    pal.setColor(QPalette.ButtonText, QColor(TEXT))
    pal.setColor(QPalette.ToolTipBase, QColor(SURFACE_ALT))
    pal.setColor(QPalette.ToolTipText, QColor(TEXT))
    pal.setColor(QPalette.Highlight, QColor(ACCENT))
    pal.setColor(QPalette.HighlightedText, QColor("#ffffff"))
    pal.setColor(QPalette.PlaceholderText, QColor(TEXT_MUTED))
    pal.setColor(QPalette.Disabled, QPalette.Text, QColor(TEXT_MUTED))
    pal.setColor(QPalette.Disabled, QPalette.ButtonText, QColor(TEXT_MUTED))
    app.setPalette(pal)
    app.setStyleSheet(
        f"""
        QGroupBox {{
            border: 1px solid {BORDER}; border-radius: 6px;
            margin-top: 12px; padding-top: 6px; font-weight: bold;
        }}
        QGroupBox::title {{ subcontrol-origin: margin; left: 8px; padding: 0 4px; }}
        QStatusBar {{ color: {TEXT_MUTED}; }}
        QListWidget {{ border: 1px solid {BORDER}; border-radius: 4px; }}
        """
    )

    pg.setConfigOptions(antialias=True, background=SURFACE, foreground=TEXT_MUTED)
