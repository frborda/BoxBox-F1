# Launches the live capturer (BoxBox-F1-Capture.exe, its own executable so the
# main app can be updated without stopping a live capture).
# Works both next to the exe and from the repo root (dist\BoxBox-F1\).
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$candidates = @(
    (Join-Path $root "BoxBox-F1-Capture.exe"),
    (Join-Path $root "dist\BoxBox-F1\BoxBox-F1-Capture.exe")
)
$exe = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if ($exe) {
    Start-Process -FilePath $exe
    exit 0
}
# compatibilidad con builds viejos (un solo exe)
$legacy = @(
    (Join-Path $root "BoxBox-F1.exe"),
    (Join-Path $root "dist\BoxBox-F1\BoxBox-F1.exe")
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $legacy) {
    Write-Host "BoxBox-F1-Capture.exe not found next to this script (run build.ps1 first)."
    exit 1
}
Start-Process -FilePath $legacy -ArgumentList "--capture"
