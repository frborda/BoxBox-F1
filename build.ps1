# Builds the Windows executable with PyInstaller.
# Output: dist\BoxBox-F1\BoxBox-F1.exe
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $root ".venv\Scripts\python.exe"

if (-not (Test-Path $python)) {
    Write-Host "Creating virtual environment and installing dependencies..."
    python -m venv (Join-Path $root ".venv")
    & $python -m pip install --disable-pip-version-check -q -r (Join-Path $root "requirements.txt") pyinstaller
}

# la app o el capturador corriendo DESDE dist bloquean sus DLLs y PyInstaller
# aborta en COLLECT al limpiar la carpeta: avisar y cortar antes
$app = Join-Path $root "dist\BoxBox-F1"
$locking = @(Get-Process -Name "BoxBox-F1", "BoxBox-F1-Capture" -ErrorAction SilentlyContinue |
             Where-Object { $_.Path -and $_.Path.StartsWith($app) })
if ($locking) {
    Write-Host "Build blocked - close these first (they run from dist\BoxBox-F1):"
    $locking | ForEach-Object { Write-Host "  $($_.Name)  (PID $($_.Id))" }
    exit 1
}
# pre-limpieza con reintentos: locks transitorios (antivirus, sync de la
# carpeta) se sueltan en segundos; mejor reintentar aca que morir en COLLECT
foreach ($dir in @($app, (Join-Path $root "dist\capture"))) {
    for ($i = 1; (Test-Path $dir); $i++) {
        try { Remove-Item $dir -Recurse -Force -ErrorAction Stop } catch {
            if ($i -ge 5) {
                Write-Host "Cannot clean $dir after $i tries: $_"
                exit 1
            }
            Write-Host "dist locked (try $i), retrying in 2 s..."
            Start-Sleep -Seconds 2
        }
    }
}

# rutas absolutas: PyInstaller resuelve dist/build contra el CWD si no
& $python -m PyInstaller --noconfirm --clean `
    --distpath (Join-Path $root "dist") `
    --workpath (Join-Path $root "build") `
    (Join-Path $root "f1telem.spec")
if ($LASTEXITCODE -eq 0) {
    # el capturador sale como carpeta hermana 'capture': moverla dentro de la
    # app (su propio _internal, para actualizar la app sin frenar la captura)
    $capSrc = Join-Path $root "dist\capture"
    $capDst = Join-Path $app "capture"
    if (Test-Path $capSrc) {
        if (Test-Path $capDst) { Remove-Item $capDst -Recurse -Force }
        Move-Item $capSrc $capDst -Force
    }
    Copy-Item (Join-Path $root "capture.ps1") $app -Force
    # extensión de Chrome para el login F1TV (cargar descomprimida)
    Copy-Item (Join-Path $root "extension") $app -Recurse -Force
    # zip listo para subir al release de GitHub (el actualizador lo busca por
    # este nombre y espera la carpeta BoxBox-F1\ en la raíz del zip)
    $zip = Join-Path $root "dist\BoxBox-F1-win64.zip"
    Write-Host "Zipping release asset..."
    if (Test-Path $zip) { Remove-Item $zip -Force }
    Compress-Archive -Path (Join-Path $root "dist\BoxBox-F1") -DestinationPath $zip
    # instalador (si Inno Setup 6 esta disponible): dist\BoxBox-F1-setup.exe
    $iscc = @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
              "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
              "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe") |
        Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $iscc) {
        $cmd = Get-Command iscc.exe -ErrorAction SilentlyContinue
        if ($cmd) { $iscc = $cmd.Source }
    }
    if ($iscc) {
        $initPy = Get-Content (Join-Path $root "src\f1telem\__init__.py") -Raw
        $version = if ($initPy -match '__version__\s*=\s*"([^"]+)"') { $Matches[1] } else { "0.0.0" }
        Write-Host "Building installer (Inno Setup, v$version)..."
        & $iscc /Q "/DAppVersion=$version" "/O$(Join-Path $root 'dist')" `
            (Join-Path $root "installer\BoxBox-F1.iss")
        if ($LASTEXITCODE -eq 0) {
            Write-Host "      $(Join-Path $root 'dist\BoxBox-F1-setup.exe')"
        }
    } else {
        Write-Host "Inno Setup not found - skipping installer (winget install JRSoftware.InnoSetup)"
    }
    Write-Host ""
    Write-Host "Done: $(Join-Path $root 'dist\BoxBox-F1\BoxBox-F1.exe')"
    Write-Host "      $zip"
} else {
    # sin esto el script "terminaba bien" (exit 0) con el build roto
    Write-Host "Build FAILED (PyInstaller exit $LASTEXITCODE)."
    exit 1
}
