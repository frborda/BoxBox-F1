"""Configuración persistente y rutas de datos de la aplicación."""
from __future__ import annotations

import json
import os
from pathlib import Path

APP_NAME = "f1telem"


def _base(env: str) -> Path:
    root = os.environ.get(env) or str(Path.home())
    return Path(root) / APP_NAME


def config_dir() -> Path:
    return _base("APPDATA")


def data_dir() -> Path:
    return _base("LOCALAPPDATA")


def cache_dir() -> Path:
    return data_dir() / "cache"


def recordings_dir() -> Path:
    return data_dir() / "recordings"


DEFAULTS = {
    "replay": {"year": 2025, "gp": "Bahrain", "session": "R", "speed": 5.0},
    "ui": {
        "gap_window_laps": 0.0,      # ventana X del gráfico de gap en vueltas (0 = todo)
        "carrera_window_laps": 1.0,  # ventana X del modo Carrera en vueltas (0 = todo)
        "show_map": True,            # panel del mapa del circuito visible
        "show_trails": True,         # estelas de los autos en el mapa
        "show_tower": True,          # torre de tiempos visible
        "show_peaks": False,         # valores en texto sobre picos máx/mín
    },
    "updates": {
        "check_on_startup": True,    # buscar nuevas versiones al abrir
        "skip_version": "",          # versión que el usuario eligió omitir
    },
}


def load_config() -> dict:
    path = config_dir() / "config.json"
    cfg = json.loads(json.dumps(DEFAULTS))  # copia profunda
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
        for key, val in saved.items():
            if isinstance(val, dict) and isinstance(cfg.get(key), dict):
                cfg[key].update(val)
            else:
                cfg[key] = val
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg: dict) -> None:
    path = config_dir() / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
