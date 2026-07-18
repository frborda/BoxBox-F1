"""Pruebas del actualizador, sin red y sin tocar la instalación real:
comparación de versiones, parseo del release, extracción del zip (con rutas
al estilo Compress-Archive) y el script apply_update.ps1 aplicando un
intercambio completo sobre una instalación de mentira.

Uso:  python tests/updater_check.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.stdout.reconfigure(errors="replace")

# aislar %LOCALAPPDATA% antes de importar f1telem.config
_TMP = tempfile.mkdtemp(prefix="f1telem-updater-test-")
os.environ["LOCALAPPDATA"] = _TMP
os.environ["APPDATA"] = _TMP

from f1telem import updater  # noqa: E402

FAILURES: list[str] = []


def check(cond: bool, msg: str) -> None:
    tag = "OK " if cond else "FAIL"
    print(f"[{tag}] {msg}", flush=True)
    if not cond:
        FAILURES.append(msg)


# ------------------------------------------------------- versiones

check(updater.parse_version("v1.2.3") == (1, 2, 3), "parse_version v1.2.3")
check(updater.parse_version("1.10") == (1, 10), "parse_version 1.10")
check(updater.parse_version("v2.0.0-beta1") == (2, 0, 0), "parse_version prerelease")
check(updater.parse_version("garbage") == (0,), "parse_version sin números")
check(updater.is_newer("1.2.0", "1.1.9"), "is_newer mayor")
check(not updater.is_newer("1.1.0", "1.1.0"), "is_newer igual")
check(not updater.is_newer("1.0.9", "1.1.0"), "is_newer menor")
check(updater.is_newer("1.10.0", "1.9.0"), "is_newer no alfabético")

# ------------------------------------------------------- parseo del release

release = {
    "tag_name": "v9.9.9",
    "html_url": "https://github.com/frborda/F1-Live-Telemetry/releases/tag/v9.9.9",
    "body": "notas",
    "assets": [
        {"name": "otro.txt", "browser_download_url": "x", "size": 1},
        {
            "name": updater.ASSET_NAME,
            "browser_download_url": "https://example.com/a.zip",
            "size": 123,
            "digest": "sha256:" + "ab" * 32,
        },
    ],
}
info = updater._info_from_release(release)
check(info.version == "9.9.9", "release: versión sin la v")
check(info.url == "https://example.com/a.zip", "release: elige el asset por nombre")
check(info.sha256 == "ab" * 32, "release: digest sha256")

try:
    updater._info_from_release({"tag_name": "v1.0.0", "assets": []})
    check(False, "release sin zip debe fallar")
except RuntimeError:
    check(True, "release sin zip debe fallar")

# ------------------------------------------------------- extracción del zip

staging = updater.updates_dir()
staging.mkdir(parents=True, exist_ok=True)
zip_path = staging / updater.ASSET_NAME
with zipfile.ZipFile(zip_path, "w") as zf:
    # Compress-Archive usa "\" como separador: reproducirlo tal cual
    zf.writestr(r"F1LiveTelemetry\F1LiveTelemetry.exe", "new-exe")
    zf.writestr(r"F1LiveTelemetry\capture.ps1", "new-launcher")
    zf.writestr(r"F1LiveTelemetry\_internal\lib.dll", "new-lib")
payload = updater.extract(zip_path)
check((payload / "F1LiveTelemetry.exe").read_text() == "new-exe",
      "extract: encuentra la carpeta del exe")
check((payload / "_internal" / "lib.dll").exists(), "extract: rutas con backslash")
check(not zip_path.exists(), "extract: borra el zip")

# ------------------------------------------------------- apply_update.ps1

target = Path(_TMP) / "install"
(target / "_internal").mkdir(parents=True)
(target / "F1LiveTelemetry.exe").write_text("old-exe")
(target / "_internal" / "lib.dll").write_text("old-lib")
(target / "_internal" / "stale.dll").write_text("only-in-old")
(target / "user-notes.txt").write_text("keep-me")

script = staging / "apply_update.ps1"
script.write_text(updater._HELPER, encoding="utf-8-sig")
log = staging / "update.log"
# PID ya terminado: Wait-Process no espera y no hay instancias corriendo
proc = subprocess.Popen(["cmd", "/c", "exit"])
proc.wait()
result = subprocess.run(
    [updater._powershell(), "-NoProfile", "-ExecutionPolicy", "Bypass",
     "-File", str(script),
     "-ProcId", str(proc.pid),
     "-Source", str(payload),
     "-Target", str(target),
     "-LogPath", str(log)],
    capture_output=True, text=True, timeout=180,
)
log_text = log.read_text() if log.exists() else "(sin log)"
check(result.returncode == 0, f"helper: exit 0 (log: {log_text.strip()[-400:]})")
check((target / "F1LiveTelemetry.exe").read_text() == "new-exe",
      "helper: exe reemplazado")
check((target / "_internal" / "lib.dll").read_text() == "new-lib",
      "helper: _internal reemplazado")
check(not (target / "_internal" / "stale.dll").exists(),
      "helper: _internal viejo no deja archivos huérfanos")
check((target / "user-notes.txt").read_text() == "keep-me",
      "helper: conserva archivos ajenos en la carpeta")
check(not (target / "F1LiveTelemetry.exe.old").exists(), "helper: limpia el backup")
check(not (target / "_internal.old").exists(), "helper: limpia _internal.old")
check(not payload.exists(), "helper: limpia el payload extraído")
check("Update applied" in log_text, "helper: log de éxito")

# ------------------------------------------------------- limpieza

updater.cleanup()
check(not updater.updates_dir().exists(), "cleanup: borra la carpeta de staging")

print()
if FAILURES:
    print(f"{len(FAILURES)} FALLA(S)")
    raise SystemExit(1)
print("Todo OK")
