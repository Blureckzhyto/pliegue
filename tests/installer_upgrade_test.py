"""Instalador de la versión actual sobre una instalación de PDFKit 1.3.7 (el nombre anterior).

NO toca la instalación ni los datos del usuario: compila variantes de los instaladores con
OTRO AppId y otros nombres ("PDFKit Prueba" -> "Pliegue Prueba"), instala por usuario en una
carpeta temporal, verifica y desinstala esa copia de prueba con su propio desinstalador. La
app de prueba corre con datos en una carpeta temporal (PLIEGUE_DATA_DIR) y una carpeta
"PDFKit" falsa para comprobar la migración (PLIEGUE_LEGACY_DIRS).

    python tests\\installer_upgrade_test.py [carpeta dist de PDFKit 1.3.7]

Comprueba: carpeta vieja borrada y la nueva completa (Pliegue.exe + _internal + Licencias),
una sola entrada en "Agregar o quitar programas" llamada como la app nueva y con la versión
nueva, accesos directos viejos borrados y los nuevos apuntando a Pliegue.exe con su icono,
que la app abre desde el Escritorio y migra la configuración, el tiempo de arranque, y que
la desinstalación no deja nada.
"""
import ctypes
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import winreg
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import measure_startup  # noqa: E402

ISCC = Path(os.environ["LOCALAPPDATA"]) / "Programs" / "Inno Setup 6" / "ISCC.exe"
TEST_GUID = "3B1F6C2A-9D4E-4F7A-8C21-5E0D7A9B4C66"
VERSION = re.search(r'VERSION = "([^"]+)"', (ROOT / "ui" / "app.py").read_text(encoding="utf-8")).group(1)
OLD_NAME, NEW_NAME = "PDFKit Prueba", "Pliegue Prueba"
OLD_DIST = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "tests" / "_capturas" / "PDFKit_1.3.7_dist"
WORK = Path(tempfile.mkdtemp(prefix="pliegue_instalador_", dir=ROOT / "tests" / "_capturas"))
OLD_DIR, NEW_DIR = WORK / OLD_NAME, WORK / NEW_NAME
DATA = WORK / "datos"
results, log = [], []


def check(name, cond, detail=""):
    results.append(bool(cond))
    log.append(("  PASS  " if cond else "  FAIL  ") + name + (f"  [{detail}]" if detail else ""))


def replace_all(text: str, pairs) -> str:
    for a, b in pairs:
        assert a in text, f"no está en el .iss: {a!r}"
        text = text.replace(a, b)
    return text


def compile_iss(name: str, text: str) -> Path:
    iss = WORK / f"{name}.iss"
    iss.write_text(text, encoding="utf-8")
    proc = subprocess.run([str(ISCC), "/Q", str(iss)], capture_output=True, text=True, cwd=ROOT)
    if proc.returncode:
        raise SystemExit(f"ISCC falló con {name}:\n{proc.stdout[-800:]}{proc.stderr[-800:]}")
    return WORK / f"{name}.exe"


def old_installer() -> Path:
    """El instalador publicado de PDFKit 1.3.7 (etiqueta v1.3.7), con AppId y nombre de prueba."""
    text = subprocess.run(["git", "show", "v1.3.7:installer.iss"], cwd=ROOT, capture_output=True,
                          text=True, encoding="utf-8", check=True).stdout
    return compile_iss("pdfkit_137", replace_all(text, [
        ("AppId={{7E64A9D1-52F3-4C08-B1A4-9D0E6C2F5B83}", f"AppId={{{{{TEST_GUID}}}"),
        ('#define MyAppName "PDFKit"', f'#define MyAppName "{OLD_NAME}"'),
        ("OutputDir=installer", f"OutputDir={WORK}"),
        ("OutputBaseFilename=PDFKit_Setup", "OutputBaseFilename=pdfkit_137"),
        ("SetupIconFile=assets\\icon.ico", f"SetupIconFile={ROOT / 'assets' / 'icon.ico'}"),
        ('Source: "README.md"', f'Source: "{ROOT / "README.md"}"'),
        ('Source: "dist\\PDFKit\\', f'Source: "{OLD_DIST}\\'),
    ]))


def new_installer() -> Path:
    text = (ROOT / "installer.iss").read_text(encoding="utf-8")
    return compile_iss("pliegue_140", replace_all(text, [
        ('#define MyAppIdGuid "7E64A9D1-52F3-4C08-B1A4-9D0E6C2F5B83"', f'#define MyAppIdGuid "{TEST_GUID}"'),
        ('#define MyAppName "Pliegue"', f'#define MyAppName "{NEW_NAME}"'),
        ('#define FormerName "PDFKit"', f'#define FormerName "{OLD_NAME}"'),
        ("OutputDir=installer", f"OutputDir={WORK}"),
        ("OutputBaseFilename=Pliegue_Setup", "OutputBaseFilename=pliegue_140"),
        ("SetupIconFile=assets\\icon.ico", f"SetupIconFile={ROOT / 'assets' / 'icon.ico'}"),
        ('Source: "README.md"', f'Source: "{ROOT / "README.md"}"'),
        ('Source: "dist\\Pliegue\\', f'Source: "{ROOT / "dist" / "Pliegue"}\\'),
    ]))


def install(setup: Path, folder: Path) -> int:
    return subprocess.run([str(setup), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CURRENTUSER",
                           f"/DIR={folder}", f"/LOG={WORK / (setup.stem + '.log')}"], timeout=900).returncode


def uninstall_entries() -> list[dict]:
    found = []
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
                        values = {n: winreg.QueryValueEx(entry, n)[0] for n in
                                  ("DisplayName", "DisplayVersion", "InstallLocation", "Publisher")}
                    found.append({"key": sub, **values})
    unique = {e["key"]: e for e in found}   # la misma clave puede verse por las dos vistas
    return list(unique.values())


def shortcut(path: Path) -> list[str]:
    out = subprocess.run(["powershell", "-NoProfile", "-Command",
                          f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{path}');"
                          "$s.TargetPath; $s.IconLocation"], capture_output=True, text=True).stdout.split("\n")
    return [line.strip() for line in out if line.strip()]


def desktop() -> Path:
    out = subprocess.run(["powershell", "-NoProfile", "-Command", "[Environment]::GetFolderPath('Desktop')"],
                         capture_output=True, text=True).stdout.strip()
    return Path(out)


def window(title: str):
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd, _):
        text = ctypes.create_unicode_buffer(64)
        ctypes.windll.user32.GetWindowTextW(hwnd, text, 64)
        if text.value == title and ctypes.windll.user32.IsWindowVisible(hwnd):
            found.append(hwnd)
        return True

    ctypes.windll.user32.EnumWindows(callback, 0)
    return found[0] if found else None


sys.stdout.reconfigure(encoding="utf-8", errors="replace")
start_menu = Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
desk = desktop()
try:
    check("existe el build de PDFKit 1.3.7 para la prueba", (OLD_DIST / "PDFKit.exe").exists(), str(OLD_DIST))
    old_setup, new_setup = old_installer(), new_installer()
    check("compilan los dos instaladores de prueba", old_setup.exists() and new_setup.exists())

    # --- PDFKit 1.3.7 instalado
    check("instala PDFKit 1.3.7", install(old_setup, OLD_DIR) == 0)
    check("1.3.7: PDFKit.exe + _internal", (OLD_DIR / "PDFKit.exe").exists() and (OLD_DIR / "_internal").is_dir())
    old_links = [desk / f"{OLD_NAME}.lnk", start_menu / OLD_NAME / f"{OLD_NAME}.lnk"]
    check("1.3.7: accesos directos de PDFKit creados", all(p.exists() for p in old_links), str(old_links))
    before = uninstall_entries()
    check("1.3.7: una entrada en Agregar o quitar programas", len(before) == 1, str(before))

    # --- Pliegue encima
    check(f"instala Pliegue {VERSION} encima", install(new_setup, NEW_DIR) == 0)
    exe = NEW_DIR / "Pliegue.exe"
    check("la carpeta nueva está completa (Pliegue.exe, _internal, Licencias)",
          exe.exists() and len(list((NEW_DIR / "_internal").rglob("*"))) > 1000
          and (NEW_DIR / "Licencias" / "LICENSE.txt").exists())
    check("la carpeta vieja de PDFKit se borró", not OLD_DIR.exists(), str(list(OLD_DIR.glob("*")))[:200]
          if OLD_DIR.exists() else "")
    after = uninstall_entries()
    entry = after[0] if after else {}
    check("una sola entrada en Agregar o quitar programas, con el nombre nuevo",
          len(after) == 1 and entry.get("DisplayName") == NEW_NAME, str(after))
    check("con la versión y la carpeta nuevas y el publicador",
          entry.get("DisplayVersion") == VERSION and Path(entry.get("InstallLocation", ".")).resolve() == NEW_DIR.resolve()
          and entry.get("Publisher") == "Blureckzhyto", str(entry))
    check("los accesos directos viejos se borraron", not any(p.exists() for p in old_links)
          and not (start_menu / OLD_NAME).exists())
    new_links = {"Escritorio": desk / f"{NEW_NAME}.lnk", "menú Inicio": start_menu / NEW_NAME / f"{NEW_NAME}.lnk"}
    for where, link in new_links.items():
        target = shortcut(link) if link.exists() else []
        check(f"acceso directo nuevo en {where} -> Pliegue.exe, con su icono",
              len(target) == 2 and Path(target[0]) == exe and str(exe).lower() in target[1].lower(), str(target))

    # --- abrir desde el Escritorio, con datos de PDFKit falsos que se tienen que migrar
    legacy_local, legacy_home = DATA / "Local" / "PDFKit", DATA / "home" / ".pdfkit"
    (legacy_local / "logs").mkdir(parents=True)
    (legacy_local / "logs" / "errores.log").write_text("de PDFKit\n", encoding="utf-8")
    legacy_home.mkdir(parents=True)
    (legacy_home / "settings.json").write_text(json.dumps({"theme": "oscuro", "reduce_motion": True}),
                                               encoding="utf-8")
    new_data = DATA / "Local" / "Pliegue"
    os.environ["PLIEGUE_DATA_DIR"] = str(new_data)
    os.environ["PLIEGUE_LEGACY_DIRS"] = f"{legacy_local};{legacy_home}"
    os.startfile(str(new_links["Escritorio"]))
    hwnd = None
    end = time.time() + 40
    while time.time() < end and not hwnd:
        hwnd = window("Pliegue")
        time.sleep(0.2)
    check("abre desde el acceso directo del Escritorio (ventana «Pliegue»)", hwnd is not None)
    time.sleep(2)
    settings = json.loads((new_data / "settings.json").read_text(encoding="utf-8")) if (new_data / "settings.json").exists() else {}
    check("migró la configuración y los registros de PDFKit", settings.get("theme") == "oscuro"
          and (new_data / "logs" / "errores.log").exists(), str(settings))
    check("y borró las carpetas viejas", not legacy_local.exists() and not legacy_home.exists())
    if hwnd:
        ctypes.windll.user32.PostMessageW(hwnd, 0x0010, 0, 0)
        time.sleep(3)

    # --- arranque de la copia instalada (datos temporales: lo hace measure_startup)
    os.environ["PLIEGUE_LEGACY_DIRS"] = ""
    times = [measure_startup.launch(exe) for _ in range(4)]
    for n, (first, visible, usable) in enumerate(times):
        log.append(f"  INFO  arranque {'frío' if n == 0 else 'tibio'} {n + 1}: aviso {first:.2f} s · "
                   f"ventana {visible:.2f} s · usable {usable:.2f} s")
    check("ventana en menos de 2 s y usable en menos de 4 s (tibio)",
          all(t[1] and t[1] < 2 and t[2] and t[2] < 4 for t in times[1:]), str(times[1:]))

    # --- desinstalar la copia de prueba (solo la de prueba: otro AppId)
    subprocess.run([str(NEW_DIR / "unins000.exe"), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"], timeout=600)
    end = time.time() + 60
    while time.time() < end and (exe.exists() or uninstall_entries()):
        time.sleep(1)
    check("la desinstalación no deja la app, ni entradas, ni accesos directos",
          not exe.exists() and not (NEW_DIR / "_internal").exists() and not uninstall_entries()
          and not any(p.exists() for p in new_links.values()) and not (start_menu / NEW_NAME).exists())
finally:
    for var in ("PLIEGUE_DATA_DIR", "PLIEGUE_LEGACY_DIRS"):
        os.environ.pop(var, None)
    shutil.rmtree(WORK, ignore_errors=True)

print("\n".join(log))
print(f"\n{sum(results)}/{len(results)} OK")
sys.exit(0 if results and all(results) else 1)
