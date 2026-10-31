"""Instalador de la versión actual sobre Pliegue 1.4.0 (actualización normal, con el mismo nombre).

NO toca la instalación ni los datos del usuario: compila los dos instaladores con OTRO AppId y
otro nombre ("Pliegue Prueba"), instala por usuario en una carpeta temporal, verifica y
desinstala esa copia con su propio desinstalador. La app corre con datos temporales.

    python tests\\installer_141_test.py [carpeta dist de Pliegue 1.4.0]

La carpeta con el build de 1.4.0 (de la etiqueta v1.4.0) se guarda en
tests\\_capturas\\Pliegue_1.4.0_dist.

Comprueba: se actualiza en la misma carpeta y queda el exe nuevo (con su versión), una sola
entrada en "Agregar o quitar programas" con la versión nueva, los accesos directos siguen, no
queda ningún archivo viejo, la configuración del usuario se conserva, la app abre, el arranque
y que la desinstalación no deja nada.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import winreg
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import measure_startup  # noqa: E402

ISCC = Path(os.environ["LOCALAPPDATA"]) / "Programs" / "Inno Setup 6" / "ISCC.exe"
TEST_GUID = "6D2A9E14-71C3-4B0F-A5E8-3C91F0B7D244"
NAME = "Pliegue Prueba"
OLD_VERSION = "1.4.0"
NEW_VERSION = re.search(r'VERSION = "([^"]+)"', (ROOT / "ui" / "app.py").read_text(encoding="utf-8")).group(1)
OLD_DIST = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "tests" / "_capturas" / "Pliegue_1.4.0_dist"
WORK = Path(tempfile.mkdtemp(prefix="pliegue_instalador141_", dir=ROOT / "tests" / "_capturas"))
FOLDER = WORK / NAME
DATA = WORK / "datos"
results, log = [], []


def check(name, cond, detail=""):
    results.append(bool(cond))
    log.append(("  PASS  " if cond else "  FAIL  ") + name + (f"  [{detail}]" if detail else ""))


def build(text: str, name: str, dist: Path) -> Path:
    for a, b in (('#define MyAppIdGuid "7E64A9D1-52F3-4C08-B1A4-9D0E6C2F5B83"', f'#define MyAppIdGuid "{TEST_GUID}"'),
                 ('#define MyAppName "Pliegue"', f'#define MyAppName "{NAME}"'),
                 ('#define FormerName "PDFKit"', '#define FormerName "PDFKit Prueba Vieja"'),
                 ("OutputDir=installer", f"OutputDir={WORK}"),
                 ("OutputBaseFilename=Pliegue_Setup", f"OutputBaseFilename={name}"),
                 ("SetupIconFile=assets\\icon.ico", f"SetupIconFile={ROOT / 'assets' / 'icon.ico'}"),
                 ('Source: "README.md"', f'Source: "{ROOT / "README.md"}"'),
                 ('Source: "dist\\Pliegue\\', f'Source: "{dist}\\')):
        assert a in text, f"no está en el .iss: {a!r}"
        text = text.replace(a, b)
    iss = WORK / f"{name}.iss"
    iss.write_text(text, encoding="utf-8")
    proc = subprocess.run([str(ISCC), "/Q", str(iss)], capture_output=True, text=True, cwd=ROOT)
    if proc.returncode:
        raise SystemExit(f"ISCC falló con {name}:\n{proc.stdout[-800:]}{proc.stderr[-800:]}")
    return WORK / f"{name}.exe"


def install(setup: Path) -> int:
    return subprocess.run([str(setup), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CURRENTUSER",
                           f"/DIR={FOLDER}", f"/LOG={WORK / (setup.stem + '.log')}"], timeout=900).returncode


def entries() -> list[dict]:
    found = {}
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                key = winreg.OpenKey(hive, r"Software\Microsoft\Windows\CurrentVersion\Uninstall", 0,
                                     winreg.KEY_READ | view)
            except OSError:
                continue
            for i in range(winreg.QueryInfoKey(key)[0]):
                sub = winreg.EnumKey(key, i)
                if sub.startswith("{" + TEST_GUID):
                    with winreg.OpenKey(key, sub) as entry:
                        found[sub] = {n: winreg.QueryValueEx(entry, n)[0] for n in
                                      ("DisplayName", "DisplayVersion", "InstallLocation")}
    return list(found.values())


def file_version(path: Path) -> str:
    return subprocess.run(["powershell", "-NoProfile", "-Command", f"(Get-Item '{path}').VersionInfo.ProductVersion"],
                          capture_output=True, text=True).stdout.strip()


def desktop() -> Path:
    return Path(subprocess.run(["powershell", "-NoProfile", "-Command", "[Environment]::GetFolderPath('Desktop')"],
                               capture_output=True, text=True).stdout.strip())


sys.stdout.reconfigure(encoding="utf-8", errors="replace")
start_menu = Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
links = [desktop() / f"{NAME}.lnk", start_menu / NAME / f"{NAME}.lnk"]
exe = FOLDER / "Pliegue.exe"
try:
    check("existe el build de Pliegue 1.4.0", (OLD_DIST / "Pliegue.exe").exists(), str(OLD_DIST))
    check("ese build es de verdad la 1.4.0", file_version(OLD_DIST / "Pliegue.exe") == OLD_VERSION)
    old_text = subprocess.run(["git", "show", "v1.4.0:installer.iss"], cwd=ROOT, capture_output=True,
                              text=True, encoding="utf-8", check=True).stdout
    old_setup = build(old_text, "pliegue_140", OLD_DIST)
    new_setup = build((ROOT / "installer.iss").read_text(encoding="utf-8"), "pliegue_nueva", ROOT / "dist" / "Pliegue")
    check("compilan los dos instaladores de prueba", old_setup.exists() and new_setup.exists())

    check("instala 1.4.0", install(old_setup) == 0)
    check("1.4.0 instalada", file_version(exe) == OLD_VERSION and all(p.exists() for p in links))
    before = entries()
    check("1.4.0: una entrada en Agregar o quitar programas", len(before) == 1 and before[0]["DisplayVersion"] == OLD_VERSION, str(before))
    settings_dir = DATA / "Local" / "Pliegue"
    settings_dir.mkdir(parents=True)
    (settings_dir / "settings.json").write_text(json.dumps({"theme": "oscuro", "reduce_motion": True}), encoding="utf-8")
    old_files = {str(p.relative_to(FOLDER)).lower() for p in FOLDER.rglob("*") if p.is_file()}

    check(f"instala {NEW_VERSION} encima", install(new_setup) == 0)
    check("el exe es el nuevo", file_version(exe) == NEW_VERSION, file_version(exe))
    after = entries()
    check("una sola entrada, con la versión nueva y la misma carpeta",
          len(after) == 1 and after[0]["DisplayVersion"] == NEW_VERSION and after[0]["DisplayName"] == NAME
          and Path(after[0]["InstallLocation"]).resolve() == FOLDER.resolve(), str(after))
    check("los accesos directos siguen", all(p.exists() for p in links))
    expected = {str(p.relative_to(ROOT / "dist" / "Pliegue")).lower() for p in (ROOT / "dist" / "Pliegue").rglob("*") if p.is_file()}
    now = {str(p.relative_to(FOLDER)).lower() for p in FOLDER.rglob("*") if p.is_file()}
    missing = sorted(expected - now)
    check("no falta ningún archivo de la versión nueva", not missing, str(missing[:4]))
    stale = sorted(f for f in now - expected if not f.startswith("unins") and f != "readme.md")
    log.append(f"  INFO  archivos que quedan de la 1.4.0: {len(stale)} {stale[:4]}")

    times = [measure_startup.launch(exe) for _ in range(3)]
    for n, (first, visible, usable) in enumerate(times):
        log.append(f"  INFO  arranque {n + 1}: aviso {first:.2f} s · ventana {visible:.2f} s · usable {usable:.2f} s")
    check("la app abre (ventana en menos de 2 s, usable en menos de 4 s, tibio)",
          all(t[1] and t[1] < 2 and t[2] and t[2] < 4 for t in times[1:]), str(times[1:]))
    # la app instalada lee la configuración que dejó la versión anterior
    script, answer = WORK / "leer.py", WORK / "leido.txt"
    script.write_text("from core.settings import Settings\nprint(Settings().get('theme'))\n", encoding="utf-8")
    env = dict(os.environ, PLIEGUE_DATA_DIR=str(settings_dir), PLIEGUE_LEGACY_DIRS="")
    subprocess.run([str(exe), "--run-script", str(script), "--salida", str(answer)], env=env, timeout=120)
    read = answer.read_text(encoding="utf-8").strip() if answer.exists() else ""
    check("la configuración se conserva después de actualizar", read == "oscuro", read)

    subprocess.run([str(FOLDER / "unins000.exe"), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"], timeout=600)
    end = time.time() + 60
    while time.time() < end and (exe.exists() or entries()):
        time.sleep(1)
    check("la desinstalación no deja la app, ni entradas, ni accesos directos",
          not exe.exists() and not (FOLDER / "_internal").exists() and not entries()
          and not any(p.exists() for p in links))
finally:
    shutil.rmtree(WORK, ignore_errors=True)

print("\n".join(log))
print(f"\n{sum(results)}/{len(results)} OK")
sys.exit(0 if results and all(results) else 1)
