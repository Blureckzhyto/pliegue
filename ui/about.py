"""Ventana "Acerca de": versión, aviso de la licencia AGPL-3.0, enlace al código fuente y
licencias de terceros (lo exige la AGPL al distribuir la app compilada)."""
from __future__ import annotations

import os
import sys
import webbrowser
from pathlib import Path

import customtkinter as ctk

from core import project
from ui import theme
from ui.assets import asset_path, logo

NOTICE = ("Pliegue es software libre: puedes redistribuirlo y modificarlo según los términos de la "
          "GNU Affero General Public License versión 3, publicada por la Free Software Foundation. "
          "Se distribuye SIN NINGUNA GARANTÍA. El código fuente completo de esta versión está "
          "disponible en el enlace de abajo.")


def licenses_dir() -> Path:
    """Carpeta con LICENSE, THIRD_PARTY_LICENSES.md y los textos de terceros."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "Licencias"
    return Path(__file__).resolve().parent.parent


def _open(path: Path) -> None:
    try:
        os.startfile(str(path))  # type: ignore[attr-defined]
    except OSError:
        pass


def show_about(root, version: str) -> ctk.CTkToplevel:
    dialog = ctk.CTkToplevel(root, fg_color=theme.BG_PRIMARY)
    dialog.title("Acerca de Pliegue")
    dialog.resizable(False, False)
    dialog.transient(root)
    body = ctk.CTkFrame(dialog, fg_color="transparent")
    body.pack(padx=28, pady=24)
    image = logo("wordmark_sidebar", 40)
    if image is not None:
        ctk.CTkLabel(body, text="", image=image).pack(anchor="w")
    ctk.CTkLabel(body, text=f"Pliegue · versión {version}", font=theme.FONT_HEADING,
                 text_color=theme.TEXT_PRIMARY).pack(anchor="w", pady=(12, 0))
    ctk.CTkLabel(body, text=f"© {project.YEAR} {project.GITHUB_USER or 'Pliegue'} · Licencia AGPL-3.0", font=theme.FONT_SMALL,
                 text_color=theme.TEXT_SECONDARY).pack(anchor="w", pady=(2, 10))
    ctk.CTkLabel(body, text=NOTICE, font=theme.FONT_SMALL, text_color=theme.TEXT_SECONDARY,
                 wraplength=440, justify="left").pack(anchor="w")
    ctk.CTkLabel(body, text="Funciona sin internet: tus archivos nunca salen de tu computadora.",
                 font=theme.FONT_SMALL, text_color=theme.TEXT_PRIMARY, wraplength=440,
                 justify="left").pack(anchor="w", pady=(10, 14))
    folder = licenses_dir()
    license_file = next((p for p in (folder / "LICENSE.txt", folder / "LICENSE") if p.exists()), folder)
    buttons = ctk.CTkFrame(body, fg_color="transparent")
    buttons.pack(anchor="w")
    dialog.ed_buttons = {
        "source": ctk.CTkButton(buttons, text="Código fuente", width=130,
                                command=lambda: webbrowser.open(project.source_url()), **theme.BTN_PRIMARY),
        "license": ctk.CTkButton(buttons, text="Licencia AGPL-3.0", width=140,
                                 command=lambda: _open(license_file), **theme.BTN_SECONDARY),
        "third": ctk.CTkButton(buttons, text="Licencias de terceros", width=160,
                               command=lambda: _open(folder), **theme.BTN_SECONDARY),
    }
    for button in dialog.ed_buttons.values():
        button.pack(side="left", padx=(0, 8))
    ctk.CTkLabel(body, text=project.source_url(), font=theme.FONT_SMALL, text_color=theme.TEXT_MUTED).pack(
        anchor="w", pady=(12, 0))
    dialog.bind("<Escape>", lambda _e: dialog.destroy())
    dialog.update_idletasks()
    x = root.winfo_rootx() + (root.winfo_width() - dialog.winfo_reqwidth()) // 2
    y = root.winfo_rooty() + (root.winfo_height() - dialog.winfo_reqheight()) // 3
    dialog.geometry(f"+{max(0, x)}+{max(0, y)}")
    dialog.after(260, lambda: dialog.winfo_exists() and dialog.iconbitmap(str(asset_path("icon.ico"))))
    dialog.after(10, lambda: dialog.winfo_exists() and dialog.grab_set())
    return dialog
