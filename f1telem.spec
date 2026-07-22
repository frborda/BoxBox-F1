# Spec de PyInstaller para F1 Telem (build onedir, sin consola).
# Dos ejecutables — visualizador y capturador — compartiendo el mismo
# _internal (COLLECT deduplica): el capturador es un exe aparte para poder
# actualizar la app principal sin frenar una captura en vivo.
from PyInstaller.utils.hooks import collect_all

datas, binaries, hiddenimports = [], [], []
for pkg in ("fastf1", "pyqtgraph"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis(
    ["src\\launch.py"],
    pathex=["src"],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "IPython", "jedi"],
    noarchive=False,
)
pyz = PYZ(a.pure)

cap = Analysis(
    ["src\\launch_capture.py"],
    pathex=["src"],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "IPython", "jedi"],
    noarchive=False,
)
pyz_cap = PYZ(cap.pure)

exe = EXE(
    pyz,
    a.scripts,
    exclude_binaries=True,
    name="BoxBox-F1",
    debug=False,
    console=False,
)
exe_cap = EXE(
    pyz_cap,
    cap.scripts,
    exclude_binaries=True,
    name="BoxBox-F1-Capture",
    debug=False,
    console=False,
)
# Carpetas _internal independientes: CPython mapea sus DLLs sin compartir
# borrado, así que la carpeta del capturador no puede tocarse mientras corre.
# Separarlas permite actualizar la app principal sin frenar la captura.
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    name="BoxBox-F1",
)
# COLLECT usa solo el basename como carpeta bajo dist: queda como hermano
# 'capture' y build.ps1 lo mueve dentro de BoxBox-F1\capture.
coll_cap = COLLECT(
    exe_cap,
    cap.binaries,
    cap.datas,
    name="capture",
)
