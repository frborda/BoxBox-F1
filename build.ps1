# Builds the Windows executable with PyInstaller.
# Output: dist\F1LiveTelemetry\F1LiveTelemetry.exe
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $root ".venv\Scripts\python.exe"

if (-not (Test-Path $python)) {
    Write-Host "Creating virtual environment and installing dependencies..."
    python -m venv (Join-Path $root ".venv")
    & $python -m pip install --disable-pip-version-check -q -r (Join-Path $root "requirements.txt") pyinstaller
}

& $python -m PyInstaller --noconfirm --clean (Join-Path $root "f1telem.spec")
if ($LASTEXITCODE -eq 0) {
    Copy-Item (Join-Path $root "capture.ps1") (Join-Path $root "dist\F1LiveTelemetry\") -Force
    # zip listo para subir al release de GitHub (el actualizador lo busca por
    # este nombre y espera la carpeta F1LiveTelemetry\ en la raíz del zip)
    $zip = Join-Path $root "dist\F1LiveTelemetry-win64.zip"
    Write-Host "Zipping release asset..."
    if (Test-Path $zip) { Remove-Item $zip -Force }
    Compress-Archive -Path (Join-Path $root "dist\F1LiveTelemetry") -DestinationPath $zip
    Write-Host ""
    Write-Host "Done: $(Join-Path $root 'dist\F1LiveTelemetry\F1LiveTelemetry.exe')"
    Write-Host "      $zip"
}
