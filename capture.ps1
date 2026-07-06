# Launches the live capturer (F1LiveTelemetry.exe --capture).
# Works both next to the exe and from the repo root (dist\F1LiveTelemetry\).
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$candidates = @(
    (Join-Path $root "F1LiveTelemetry.exe"),
    (Join-Path $root "dist\F1LiveTelemetry\F1LiveTelemetry.exe")
)
$exe = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $exe) {
    Write-Host "F1LiveTelemetry.exe not found next to this script (run build.ps1 first)."
    exit 1
}
Start-Process -FilePath $exe -ArgumentList "--capture"
