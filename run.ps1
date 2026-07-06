# Ejecuta la app en modo desarrollo (usa el venv del proyecto).
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$env:PYTHONPATH = Join-Path $root "src"
& (Join-Path $root ".venv\Scripts\python.exe") -m f1telem
