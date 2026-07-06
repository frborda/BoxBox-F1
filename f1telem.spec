# Spec de PyInstaller para F1 Telem (build onedir, sin consola).
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

exe = EXE(
    pyz,
    a.scripts,
    exclude_binaries=True,
    name="F1LiveTelemetry",
    debug=False,
    console=False,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    name="F1LiveTelemetry",
)
