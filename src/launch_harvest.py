"""Lanzador para PyInstaller del cosechador de estrategia (consola).

Uso: BoxBox-F1-Harvest.exe 2024:Bahrain:R "2025:Monza" [--list f.txt]
Comparte el _internal y el caché de Fast-F1 con la app principal.
"""
import sys

# consola redirigida a archivo/pipe: cp1252 no sabe de ✔/✗ — degradar
# en lugar de morir con UnicodeEncodeError
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass

from f1telem.harvest import main

raise SystemExit(main())
