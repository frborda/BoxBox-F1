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


def capture_lock_path() -> Path:
    """Latido del capturador: el proceso lo re-escribe cada segundo mientras
    vive; el visualizador lo considera corriendo si el mtime es fresco."""
    return data_dir() / "capture.lock"


def capture_show_path() -> Path:
    """Pedido de "mostrate": el visualizador lo crea y el capturador (que
    puede estar escondido en la bandeja del sistema) se trae al frente."""
    return data_dir() / "capture.show"


def capture_running(max_age: float = 5.0) -> bool:
    import time

    lock = capture_lock_path()
    try:
        return time.time() - lock.stat().st_mtime < max_age
    except OSError:
        return False


DEFAULTS = {
    "replay": {"year": 2025, "gp": "Bahrain", "session": "R", "speed": 5.0},
    "ui": {
        "gap_window_laps": 0.0,      # ventana X del gráfico de gap en vueltas (0 = todo)
        "carrera_window_laps": 1.0,  # ventana X del modo Carrera en vueltas (0 = todo)
        "show_trails": True,         # estelas de los autos en el mapa
        "show_peaks": False,         # valores en texto sobre picos máx/mín
        "tower_scale": 1.0,          # escala de fuente de la torre (A+/A−)
    },
    "updates": {
        "check_on_startup": True,    # buscar nuevas versiones al abrir
        "skip_version": "",          # versión que el usuario eligió omitir
    },
    "strategy": {
        "pit_window": 20.0,          # Ventana de Box en segundos
        "pit_window_locked": False,  # True: el cálculo automático no la pisa
    },
    "panels": {
        "visible": {},  # global, independiente del modo: {panel: visible}
        "float": {},    # estado flotante: {panel: {geom, pinned, visible}}
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
