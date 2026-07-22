"""Punto de entrada del capturador (exe propio: BoxBox-F1-Capture).

Separado del visualizador para que una actualización de la app principal
no obligue a frenar una captura en vivo en curso.
"""
import sys

from f1telem.main import _handle_protocol, _selftest

if "--selftest" in sys.argv:
    raise SystemExit(_selftest())

for arg in sys.argv[1:]:
    if arg.startswith("f1telemetry://"):
        raise SystemExit(_handle_protocol(arg))

from f1telem.capture_app import main

raise SystemExit(main())
