"""Ventana «Acerca de» (AGPL-3.0): versión, aviso de licencia, enlace al código fuente y
licencias de terceros. Dentro del exe (run_all.py --exe) comprueba además la carpeta
«Licencias» junto a Pliegue.exe."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import project  # noqa: E402
from ui import about  # noqa: E402
from ui.app import VERSION, App  # noqa: E402

SETTINGS = __import__("core.paths").paths.settings_file()
backup = SETTINGS.read_bytes() if SETTINGS.exists() else None
results, log = [], []


def check(name, cond, detail=""):
    results.append(bool(cond))
    log.append(("  PASS  " if cond else "  FAIL  ") + name + (f"  [{detail}]" if detail else ""))


folder = about.licenses_dir()
check("la licencia AGPL-3.0 está junto a la app", any((folder / n).exists() for n in ("LICENSE", "LICENSE.txt"))
      and "GNU AFFERO GENERAL PUBLIC LICENSE" in next(
          (folder / n).read_text(encoding="utf-8") for n in ("LICENSE.txt", "LICENSE") if (folder / n).exists()),
      str(folder))
third = folder / "THIRD_PARTY_LICENSES.md"
check("THIRD_PARTY_LICENSES.md presente y con PyMuPDF/Poppler", third.exists()
      and all(k in third.read_text(encoding="utf-8") for k in ("pymupdf", "Poppler", "Tesseract", "WeasyPrint")))
texts = folder / ("terceros" if getattr(sys, "frozen", False) else "licenses")
check("textos de licencia de terceros presentes", texts.is_dir() and len(list(texts.rglob("*"))) > 50, str(texts))
check("el proyecto tiene usuario de GitHub y URL del código", bool(project.GITHUB_USER)
      and project.source_url().startswith("https://github.com/"), project.source_url())

app = App()
app.geometry("1100x760+40+40")


def run():
    app.update()
    check("el enlace «Acerca de» está en la barra lateral", app._about_button.winfo_ismapped())
    dialog = app.show_about()
    end = time.time() + 3
    while time.time() < end:
        app.update()
    import customtkinter as ctk

    labels = [w.cget("text") for w in dialog.winfo_children()[0].winfo_children() if isinstance(w, ctk.CTkLabel)]
    joined = " ".join(str(t) for t in labels)
    check("muestra el nombre y la versión", f"Pliegue · versión {VERSION}" in joined and "PDFKit" not in joined,
          joined[:120])
    check("el título de la ventana es «Acerca de Pliegue»", dialog.title() == "Acerca de Pliegue", dialog.title())
    check("menciona la GNU Affero General Public License", "GNU Affero General Public License" in joined)
    check("muestra la URL del código fuente", project.source_url() in joined)
    check("botones: código fuente, licencia y terceros",
          set(dialog.ed_buttons) == {"source", "license", "third"})
    dialog.destroy()
    app.destroy()


app.after(600, run)
try:
    app.mainloop()
finally:
    if backup is not None:
        SETTINGS.write_bytes(backup)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
print("\n".join(log))
print(f"\n{sum(results)}/{len(results)} OK")
sys.exit(0 if results and all(results) else 1)
