r"""Actualizador: comprueba el último release de GitHub, descarga el zip y
lanza un script que reemplaza la instalación cuando la app se cierra.

El build es PyInstaller *onedir*: el exe y `_internal\` están bloqueados
mientras la app corre, así que la instalación la hace un PowerShell externo
(`apply_update.ps1`) que espera a que salgan todas las instancias, respalda
la versión actual, copia la nueva y relanza la app (con rollback si falla).
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import zipfile
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from . import __version__, config

REPO = "frborda/F1-Live-Telemetry"
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_URL = f"https://github.com/{REPO}/releases"
ASSET_NAME = "BoxBox-F1-win64.zip"
EXE_NAME = "BoxBox-F1.exe"
_HTTP_HEADERS = {
    "Accept": "application/vnd.github+json",
    "User-Agent": "BoxBox-F1",
}


class InstallCancelled(Exception):
    """El usuario canceló la descarga."""


@dataclass
class UpdateInfo:
    version: str    # "1.2.0" (tag sin la "v")
    tag: str        # "v1.2.0"
    url: str        # descarga directa del asset
    size: int       # bytes del asset
    sha256: str     # digest publicado por GitHub ("" si no hay)
    notes: str      # cuerpo del release (markdown)
    html_url: str   # página del release


def parse_version(text: str) -> tuple[int, ...]:
    """'v1.2.3' -> (1, 2, 3); tolera prefijos/sufijos no numéricos."""
    nums = re.findall(r"\d+", text.split("-")[0].split("+")[0])
    return tuple(int(n) for n in nums) if nums else (0,)


def is_newer(version: str, current: str = __version__) -> bool:
    return parse_version(version) > parse_version(current)


def _info_from_release(release: dict) -> UpdateInfo:
    assets = release.get("assets") or []
    asset = next((a for a in assets if a.get("name") == ASSET_NAME), None)
    if asset is None:  # tolerar un asset renombrado mientras siga siendo zip
        asset = next(
            (a for a in assets if str(a.get("name", "")).endswith(".zip")), None
        )
    if asset is None:
        raise RuntimeError("The latest release has no .zip asset to download.")
    tag = str(release.get("tag_name") or "")
    digest = str(asset.get("digest") or "")
    return UpdateInfo(
        version=tag.lstrip("vV") or "0",
        tag=tag,
        url=str(asset.get("browser_download_url") or ""),
        size=int(asset.get("size") or 0),
        sha256=digest[len("sha256:"):] if digest.startswith("sha256:") else "",
        notes=str(release.get("body") or ""),
        html_url=str(release.get("html_url") or RELEASES_URL),
    )


def check_latest(timeout: float = 15.0) -> UpdateInfo:
    """Consulta el último release publicado (excluye borradores y prereleases)."""
    import requests

    resp = requests.get(API_LATEST, timeout=timeout, headers=_HTTP_HEADERS)
    resp.raise_for_status()
    return _info_from_release(resp.json())


# ------------------------------------------------------------- instalación

def install_dir() -> Path | None:
    """Carpeta de la app congelada (onedir); None si corre desde el código."""
    if not getattr(sys, "frozen", False):
        return None
    return Path(sys.executable).resolve().parent


def can_autoupdate() -> bool:
    """Solo el build congelado con permiso de escritura puede autoinstalarse."""
    target = install_dir()
    if target is None:
        return False
    try:
        probe = target / ".update-probe"
        probe.touch()
        probe.unlink()
        return True
    except OSError:
        return False


def updates_dir() -> Path:
    return config.data_dir() / "updates"


def cleanup() -> None:
    """Borra restos de instalaciones anteriores (mejor esfuerzo)."""
    target = install_dir()
    if target is not None:
        leftovers = [
            target / (EXE_NAME + ".old"), target / "_internal.old",
            target / "capture" / "BoxBox-F1-Capture.exe.old",
            target / "capture" / "_internal.old",
        ]
        for leftover in leftovers:
            try:
                if leftover.is_dir():
                    shutil.rmtree(leftover)
                elif leftover.exists():
                    leftover.unlink()
            except OSError:
                pass
    try:
        if updates_dir().exists():
            shutil.rmtree(updates_dir())
    except OSError:
        pass


def download(info: UpdateInfo, progress=None, cancelled=None) -> Path:
    """Descarga el zip del release verificando el sha256 publicado.

    progress(bytes_descargados, bytes_totales); cancelled: threading.Event.
    """
    import requests

    dest = updates_dir()
    dest.mkdir(parents=True, exist_ok=True)
    zip_path = dest / ASSET_NAME
    hasher = sha256()
    done = 0
    with requests.get(
        info.url, stream=True, timeout=30, headers=_HTTP_HEADERS
    ) as resp:
        resp.raise_for_status()
        total = int(resp.headers.get("Content-Length") or info.size or 0)
        with open(zip_path, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=256 * 1024):
                if cancelled is not None and cancelled.is_set():
                    fh.close()
                    zip_path.unlink(missing_ok=True)
                    raise InstallCancelled()
                fh.write(chunk)
                hasher.update(chunk)
                done += len(chunk)
                if progress is not None:
                    progress(done, total)
    if info.sha256 and hasher.hexdigest().lower() != info.sha256.lower():
        zip_path.unlink(missing_ok=True)
        raise RuntimeError("Downloaded file failed the integrity check (sha256).")
    return zip_path


def extract(zip_path: Path) -> Path:
    """Extrae el zip y devuelve la carpeta que contiene el ejecutable."""
    dest = zip_path.parent / "payload"
    if dest.exists():
        shutil.rmtree(dest)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(dest)
    zip_path.unlink(missing_ok=True)
    for exe in dest.rglob(EXE_NAME):
        return exe.parent
    raise RuntimeError(f"{EXE_NAME} not found inside the downloaded zip.")


# Script que aplica la actualización de la app principal (BoxBox-F1).
# La carpeta del capturador (Target\capture) tiene su propio _internal y solo
# se actualiza si NO hay un capturador corriendo — así una captura en vivo no
# se frena. Respalda exe y _internal como *.old y hace rollback si la copia
# falla. Todo queda en update.log.
_HELPER = r'''param(
    [int]$ProcId,
    [string]$Source,
    [string]$Target,
    [string]$LogPath,
    [string]$RestartArgs = "",
    [string]$DoMain = "1",
    [string]$DoCapture = "1"
)
$ErrorActionPreference = "Stop"

function Log($msg) {
    $stamp = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
    try { Add-Content -Path $LogPath -Value "[$stamp] $msg" } catch { }
}

function Retry([scriptblock]$Action, [int]$Times = 10) {
    for ($i = 1; $i -le $Times; $i++) {
        try { & $Action; return } catch {
            if ($i -eq $Times) { throw }
            Start-Sleep -Seconds 1
        }
    }
}

$exe = Join-Path $Target "BoxBox-F1.exe"
try {
    if ($DoMain -eq "1") {
        Log "Waiting for process $ProcId to exit"
        try { Wait-Process -Id $ProcId -Timeout 120 -ErrorAction Stop } catch { }
        # esperar solo al visualizador; el capturador es un exe aparte con su
        # propio _internal y puede seguir grabando
        $deadline = (Get-Date).AddMinutes(10)
        $running = @()
        while ((Get-Date) -lt $deadline) {
            $running = @(Get-Process -Name "BoxBox-F1" -ErrorAction SilentlyContinue |
                         Where-Object { $_.Path -eq $exe })
            if ($running.Count -eq 0) { break }
            Start-Sleep -Seconds 2
        }
        if ($running.Count -gt 0) {
            Log "Aborted: the app is still running after 10 minutes"
            exit 1
        }

        Log "Backing up current version"
        $exeOld = "$exe.old"
        $internal = Join-Path $Target "_internal"
        $internalOld = "$internal.old"
        if (Test-Path $exeOld) { Remove-Item $exeOld -Force }
        if (Test-Path $internalOld) { Remove-Item $internalOld -Recurse -Force }
        Retry { Move-Item $exe $exeOld -Force }
        Retry { if (Test-Path $internal) { Move-Item $internal $internalOld -Force } }

        try {
            Log "Copying new main app from $Source"
            # todo menos la subcarpeta capture\ (fase propia mas abajo)
            Get-ChildItem -LiteralPath $Source -Force | Where-Object { $_.Name -ne "capture" } |
                ForEach-Object { Copy-Item $_.FullName $Target -Recurse -Force }
            Log "Main app updated"
        } catch {
            Log "Copy failed: $_ -- rolling back"
            if (Test-Path $exe) { Remove-Item $exe -Force }
            if (Test-Path $internal) { Remove-Item $internal -Recurse -Force }
            Move-Item $exeOld $exe -Force
            if (Test-Path $internalOld) { Move-Item $internalOld $internal -Force }
            throw
        }
        try { Remove-Item $exeOld -Force } catch { }
        try { Remove-Item $internalOld -Recurse -Force } catch { }

        Log "Restarting: $exe $RestartArgs"
        try {
            if ($RestartArgs) {
                Start-Process -FilePath $exe -ArgumentList $RestartArgs -WorkingDirectory $Target
            } else {
                Start-Process -FilePath $exe -WorkingDirectory $Target
            }
        } catch {
            Log "Restart failed: $_"
        }
    }

    if ($DoCapture -eq "1") {
        $capSrc = Join-Path $Source "capture"
        $capDst = Join-Path $Target "capture"
        if (Test-Path $capSrc) {
            $capExe = Join-Path $capDst "BoxBox-F1-Capture.exe"
            # una captura en curso nunca se interrumpe: esperar (hasta 60 min)
            # a que el capturador se cierre para reemplazarlo
            $deadline = (Get-Date).AddMinutes(60)
            $capRun = @()
            while ((Get-Date) -lt $deadline) {
                $capRun = @(Get-Process -Name "BoxBox-F1-Capture" -ErrorAction SilentlyContinue |
                            Where-Object { $_.Path -eq $capExe })
                if ($capRun.Count -eq 0) { break }
                Start-Sleep -Seconds 5
            }
            if ($capRun.Count -gt 0) {
                Log "Capturer still running after 60 min: skipped (next update will retry)"
            } else {
                try {
                    if (-not (Test-Path $capDst)) { New-Item -ItemType Directory -Path $capDst | Out-Null }
                    $capOld = Join-Path $capDst "_internal.old"
                    if (Test-Path $capOld) { Remove-Item $capOld -Recurse -Force }
                    $capInt = Join-Path $capDst "_internal"
                    if (Test-Path $capInt) { Retry { Move-Item $capInt $capOld -Force } }
                    if (Test-Path $capExe) {
                        Retry { Move-Item $capExe "$capExe.old" -Force }
                    }
                    Copy-Item (Join-Path $capSrc "*") $capDst -Recurse -Force
                    try { Remove-Item "$capExe.old" -Force } catch { }
                    try { Remove-Item $capOld -Recurse -Force } catch { }
                    Log "Capturer updated"
                } catch {
                    Log "Capturer update failed: $_"
                }
            }
        }
    }

    try { Remove-Item $Source -Recurse -Force } catch { }
} catch {
    Log "Update failed: $_"
    exit 1
}
'''


def _powershell() -> str:
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    exe = Path(system_root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    return str(exe) if exe.exists() else "powershell.exe"


def launch_installer(payload: Path, restart_args: list[str],
                     do_main: bool = True, do_capture: bool = True) -> None:
    """Lanza `apply_update.ps1` desatendido. Con do_main la app debe cerrarse
    después (el instalador la espera y la relanza); con solo do_capture la app
    sigue abierta y el capturador se reemplaza cuando se cierre."""
    target = install_dir()
    if target is None:
        raise RuntimeError("Auto-install only works from the packaged build.")
    staging = updates_dir()
    script = staging / "apply_update.ps1"
    script.write_text(_HELPER, encoding="utf-8-sig")
    cmd = [
        _powershell(), "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-WindowStyle", "Hidden", "-File", str(script),
        "-ProcId", str(os.getpid()),
        "-Source", str(payload),
        "-Target", str(target),
        "-LogPath", str(staging / "update.log"),
        "-DoMain", "1" if do_main else "0",
        "-DoCapture", "1" if do_capture else "0",
    ]
    if restart_args:
        cmd += ["-RestartArgs", " ".join(restart_args)]
    flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(cmd, creationflags=flags, close_fds=True, cwd=str(staging))
