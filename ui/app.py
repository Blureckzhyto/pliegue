"""Ventana principal: barra lateral, enrutado de vistas, tema claro/oscuro y animaciones."""
from __future__ import annotations

import tkinter as tk

import customtkinter as ctk

from core import errors
from core.settings import Settings
from ui import anim, startup, theme
from ui.assets import asset_path, icon, logo
from ui.components.scrolling import ScrollFrame
from ui.components.toast import ToastManager
from ui.views.home import HomeView

try:
    from tkinterdnd2 import TkinterDnD

    class _Root(ctk.CTk, TkinterDnD.DnDWrapper):
        """Raíz CTk con tkdnd cargado, para arrastrar y soltar en widgets hijos."""

        def __init__(self):
            super().__init__()
            try:
                self.TkdndVersion = TkinterDnD._require(self)
                self.dnd_available = True
            except Exception:
                self.dnd_available = False
except ImportError:
    class _Root(ctk.CTk):  # type: ignore[no-redef]
        dnd_available = False

VERSION = "1.4.1"
VIEW_SLIDE_PX = 12
THEMES = {"Claro": "claro", "Oscuro": "oscuro", "Sistema": "sistema"}
THEME_ICONS = {"claro": "ui_sun", "oscuro": "ui_moon", "sistema": "ui_monitor"}
SYSTEM_POLL_MS = 1500
CROSSFADE_MS = 220

# (categoría, color, [(clave, símbolo, nombre, "módulo:Vista")]); la vista se importa recién
# al abrir la herramienta (arranque rápido: nada de PyMuPDF, pandas, etc. al inicio). El icono de cada
# herramienta está en assets/icons/tool_<clave>_*.png
CATEGORIES = [
    ("ORDENAR PDF", theme.CAT_ORDENAR, [
        ("merge", "⊕", "Unir PDF", "merge:MergeView"),
        ("split", "÷", "Dividir PDF", "split:SplitView"),
        ("extract", "⇱", "Extraer páginas", "pages:ExtractPagesView"),
        ("delete", "⊖", "Eliminar páginas", "pages:DeletePagesView"),
        ("organize", "⇅", "Ordenar páginas", "organize:OrganizeView"),
    ]),
    ("OPTIMIZAR PDF", theme.CAT_OPTIMIZAR, [
        ("compress", "⇊", "Comprimir PDF", "compress:CompressView"),
        ("repair", "↺", "Reparar PDF", "optimize:RepairView"),
        ("ocr", "OCR", "OCR PDF", "optimize:OcrView"),
    ]),
    ("CONVERTIR A PDF", theme.CAT_CONVERTIR_A, [
        ("jpg2pdf", "JPG", "JPG a PDF", "jpg_to_pdf:JpgToPdfView"),
        ("word2pdf", "DOC", "Word a PDF", "word_to_pdf:WordToPdfView"),
        ("pptx2pdf", "PPT", "PowerPoint a PDF", "office:PptxToPdfView"),
        ("excel2pdf", "XLS", "Excel a PDF", "excel_to_pdf:ExcelToPdfView"),
        ("html2pdf", "</>", "HTML a PDF", "office:HtmlToPdfView"),
    ]),
    ("CONVERTIR DESDE PDF", theme.CAT_CONVERTIR_DESDE, [
        ("pdf2jpg", "JPG", "PDF a JPG", "pdf_to_jpg:PdfToJpgView"),
        ("pdf2word", "DOC", "PDF a Word", "pdf_to_word:PdfToWordView"),
        ("pdf2pptx", "PPT", "PDF a PowerPoint", "office:PdfToPptxView"),
        ("pdf2excel", "XLS", "PDF a Excel", "pdf_to_excel:PdfToExcelView"),
        ("pdfa", "A", "PDF a PDF/A", "office:PdfToPdfaView"),
    ]),
    ("EDITAR PDF", theme.CAT_EDITAR, [
        ("rotate", "↻", "Rotar PDF", "rotate:RotateView"),
        ("numbers", "#", "Números de página", "page_numbers:PageNumbersView"),
        ("watermark", "≋", "Marca de agua", "watermark:WatermarkView"),
        ("crop", "⌗", "Recortar PDF", "crop:CropView"),
        ("edit", "T", "Editar PDF", "editor:PdfEditorView"),
        ("forms", "≡", "Formularios PDF", "forms:FormView"),
    ]),
    ("EDITAR & FIRMAR", theme.CAT_SEGURIDAD, [
        ("sign", "∫", "Firmar PDF", "sign:SignView"),
        ("protect", "⊠", "Proteger PDF", "protect:ProtectPdfView"),
        ("unprotect", "⊡", "Desproteger PDF", "protect:UnprotectPdfView"),
        ("redact", "▬", "Censurar PDF", "edit:CensorView"),
        ("compare", "⇆", "Comparar PDF", "compare:CompareView"),
    ]),
    ("IMÁGENES", theme.CAT_IMAGENES, [
        ("imgconvert", "⇄", "Convertir imágenes", "image_converter:ImageConverterView"),
        ("imgcompress", "%", "Comprimir imágenes", "image_compressor:ImageCompressorView"),
    ]),
]


def _warm_up_in_background():
    """Después de abrir: el índice de fuentes del editor (lee su caché o lo arma) en otro hilo,
    así el primer doble clic en un texto no espera a revisar 450 fuentes."""
    import threading

    def work():
        try:
            from core import font_finder

            font_finder.font_index()
        except Exception:
            errors.record("No se pudo preparar el índice de fuentes al iniciar")

    threading.Thread(target=work, name="precarga", daemon=True).start()


def _view_class(path: str):
    """"merge:MergeView" -> la clase (importa ui.views.merge solo la primera vez)."""
    import importlib

    module, name = path.split(":")
    return getattr(importlib.import_module(f"ui.views.{module}"), name)


def category_title(name: str) -> str:
    """'CONVERTIR DESDE PDF' -> 'Convertir desde PDF'."""
    return name.capitalize().replace("pdf", "PDF").replace(" & ", " & ")


class App(_Root):
    # la barra de título oscura la pone _apply_titlebar sin el ocultar/mostrar de customtkinter
    _deactivate_windows_window_header_manipulation = True

    def __init__(self):
        startup.prepare()
        super().__init__()
        from ui import fonts
        theme.configure_fonts(*fonts.choose(self))
        self.settings = Settings()
        self.theme_pref = self.settings.get("theme", "sistema")
        if self.theme_pref not in THEMES.values():
            self.theme_pref = "sistema"
        self._mode = self._wanted_mode()
        theme.apply(self._mode)
        self.bind("<Map>", lambda e: e.widget is self and self._apply_titlebar(), add="+")
        self.bind("<Map>", self._first_map, add="+")
        anim.set_enabled(not self.settings.get("reduce_motion", False))
        self.categories = CATEGORIES
        self.building_meta = None
        self.toasts = ToastManager(self)
        self._flush_tk_cleanup()

        self.title("Pliegue")
        self._set_window_icon()
        self.geometry("1320x840")
        self.minsize(1100, 700)
        self.configure(fg_color=theme.BG_PRIMARY)
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(1, weight=1)

        self._views: dict[str, ctk.CTkFrame] = {}
        self._meta: dict[str, tuple] = {}
        self._nav_items: dict[str, ctk.CTkButton] = {}
        self._active = None
        for _category, color, tools in CATEGORIES:
            for key, symbol, name, cls in tools:
                self._meta[key] = (cls, name, color, symbol)

        self._build_sidebar()
        self.content = ctk.CTkFrame(self, fg_color=theme.BG_PRIMARY, corner_radius=0)
        self.content.grid(row=0, column=1, sticky="nsew")
        self.show_view("home")
        self.after(SYSTEM_POLL_MS, self._poll_system_theme)
        if self.winfo_ismapped():   # Tk ya la mostró mientras se armaba
            self._first_map(type("E", (), {"widget": self})())

    def _first_map(self, event):
        """La ventana ya se ve: se cierra la pantalla de carga y, cuando Tk termina de dibujar,
        se anota el arranque y empieza (en segundo plano) lo que no hacía falta para abrir."""
        if event.widget is not self or getattr(self, "_shown", False):
            return
        self._shown = True
        startup.mark("ventana")
        startup.close_splash()

        def ready():
            startup.mark("usable")
            startup.write_timing(VERSION)
            self.after(1500, _warm_up_in_background)

        self.after_idle(lambda: self.after(10, ready))

    def _flush_tk_cleanup(self):
        startup.flush_tk_cleanup()
        self.after(1000, self._flush_tk_cleanup)

    def report_callback_exception(self, kind, value, tb):
        """Error en un callback de Tk: se registra completo y se avisa (nunca en silencio)."""
        errors.record("Error en la interfaz", value)
        try:
            self.toasts.show("error", f"Ocurrió un error: {errors.reason(value)} (detalles en errores.log)")
        except Exception:
            errors.record("No se pudo mostrar el aviso de error")

    def _set_window_icon(self):
        ico = str(asset_path("icon.ico"))
        try:
            self.iconbitmap(ico)
            self.iconbitmap(default=ico)  # diálogos y ventanas secundarias
        except Exception:
            pass
        # Tk entrega a Windows un ícono de 32 px y la barra de tareas lo reduce a 24 px
        # (se ve borroso); se reemplaza por el cuadro exacto del .ico ya con la ventana creada.
        self.after(300, lambda: startup.set_native_icon(int(self.wm_frame(), 16), ico))

    # ------------------------------------------------------------------ barra lateral

    def _build_sidebar(self):
        sidebar = ctk.CTkFrame(self, width=248, corner_radius=0, fg_color=theme.SIDEBAR_BG)
        sidebar.grid(row=0, column=0, sticky="nsw")
        sidebar.grid_propagate(False)
        sidebar.grid_rowconfigure(1, weight=1)
        sidebar.grid_columnconfigure(0, weight=1)

        self._wordmark = logo("wordmark_sidebar", 34)
        brand = ctk.CTkLabel(sidebar, text="" if self._wordmark else "Pliegue", image=self._wordmark,
                             font=theme.FONT_TITLE, text_color=theme.TEXT_PRIMARY, cursor="hand2")
        brand.grid(row=0, column=0, sticky="w", padx=22, pady=(24, 14))
        brand.bind("<Button-1>", lambda _e: self.show_view("home"))

        nav = ScrollFrame(sidebar, fg_color=theme.SIDEBAR_BG, corner_radius=0,
                          scrollbar_button_color=theme.SIDEBAR_BG,
                          scrollbar_button_hover_color=theme.BORDER_STRONG)
        nav.grid(row=1, column=0, sticky="nsew")
        self._nav = nav
        self._add_nav_item(nav, "home", "Inicio")
        for category, _color, tools in CATEGORIES:
            ctk.CTkLabel(nav, text=theme.spaced(category), font=(theme.FONT_SEMIBOLD, 11),
                         text_color=theme.SIDEBAR_TEXT).pack(anchor="w", padx=20, pady=(18, 4))
            for key, _symbol, name, _cls in tools:
                self._add_nav_item(nav, key, name)
        anim.enhance(nav)
        self._indicator = ctk.CTkFrame(nav, width=4, height=18, corner_radius=2,
                                       fg_color=theme.ACCENT)

        footer = ctk.CTkFrame(sidebar, fg_color="transparent")
        footer.grid(row=2, column=0, sticky="ew", padx=20, pady=(8, 14))
        theme_row = ctk.CTkFrame(footer, fg_color="transparent")
        theme_row.pack(fill="x", pady=(0, 10))
        self._theme_icon = ctk.CTkLabel(theme_row, text="", width=22,
                                        image=icon(THEME_ICONS[self.theme_pref], 18))
        self._theme_icon.pack(side="left", padx=(0, 6))
        self._theme_selector = ctk.CTkSegmentedButton(
            theme_row, values=list(THEMES), height=28, command=self._theme_chosen,
            **{**theme.SEGMENTED, "font": (theme.FONT, 12)})
        self._theme_selector.set(next(k for k, v in THEMES.items() if v == self.theme_pref))
        self._theme_selector.pack(side="left", fill="x", expand=True)
        self._reduce = ctk.CTkSwitch(footer, text="Reducir animaciones", font=theme.FONT_SMALL,
                                     text_color=theme.TEXT_SECONDARY, progress_color=theme.ACCENT,
                                     button_color=theme.SWITCH_KNOB,
                                     button_hover_color=theme.SWITCH_KNOB,
                                     fg_color=theme.BORDER_STRONG, command=self._toggle_motion)
        if self.settings.get("reduce_motion", False):
            self._reduce.select()
        self._reduce.pack(anchor="w")
        version_row = ctk.CTkFrame(footer, fg_color="transparent")
        version_row.pack(anchor="w", pady=(6, 0))
        ctk.CTkLabel(version_row, text=f"Versión {VERSION}  ·", font=(theme.FONT, 11),
                     text_color=theme.TEXT_MUTED).pack(side="left")
        self._about_button = ctk.CTkButton(
            version_row, text="Acerca de", width=10, height=20, corner_radius=6, font=(theme.FONT, 11),
            fg_color="transparent", hover_color=theme.SIDEBAR_HOVER, text_color=theme.TEXT_SECONDARY,
            command=self.show_about)
        self._about_button.pack(side="left", padx=(2, 0))

    def show_about(self):
        """Versión, licencia AGPL-3.0, código fuente y licencias de terceros."""
        from ui.about import show_about

        return show_about(self, VERSION)

    def _add_nav_item(self, nav, key, name):
        button = ctk.CTkButton(nav, text="  " + name, anchor="w", height=36, corner_radius=10,
                               fg_color=theme.SIDEBAR_BG, hover_color=theme.SIDEBAR_HOVER,
                               text_color=theme.SIDEBAR_ITEM, font=(theme.FONT, 14),
                               command=lambda k=key: self.show_view(k))
        button.pack(fill="x", padx=12, pady=1)
        self._nav_items[key] = button

    def _toggle_motion(self):
        reduce = bool(self._reduce.get())
        self.settings.set("reduce_motion", reduce)
        anim.set_enabled(not reduce)

    # ------------------------------------------------------------------ tema

    def _wanted_mode(self) -> str:
        if self.theme_pref == "sistema":
            return "dark" if theme.system_prefers_dark() else "light"
        return "dark" if self.theme_pref == "oscuro" else "light"

    def _theme_chosen(self, label):
        self.set_theme(THEMES[label])

    def set_theme(self, pref: str, animate: bool = True) -> None:
        """'claro', 'oscuro' o 'sistema': se guarda y se aplica al instante, sin reiniciar."""
        self.theme_pref = pref
        self.settings.set("theme", pref)
        self._theme_selector.set(next(k for k, v in THEMES.items() if v == pref))
        self._theme_icon.configure(image=icon(THEME_ICONS[pref], 18))
        self._apply_mode(self._wanted_mode(), animate)

    def _apply_mode(self, mode: str, animate: bool = True) -> None:
        if mode == self._mode:
            return

        def switch():
            self._mode = mode
            theme.set_mode(mode)
            self._apply_titlebar()

        if animate and anim.ENABLED and self._is_foreground():
            self._crossfade(switch)
        else:
            switch()

    def _hwnd(self) -> int:
        return int(self.wm_frame(), 16)

    def _apply_titlebar(self) -> None:
        try:
            startup.set_dark_titlebar(self._hwnd(), self._mode == "dark")
        except (tk.TclError, ValueError):
            pass

    def _is_foreground(self) -> bool:
        try:
            import ctypes

            return ctypes.windll.user32.GetForegroundWindow() == self._hwnd() and \
                self.state() != "iconic"
        except Exception:
            return False

    def _crossfade(self, apply) -> None:
        """Fundido entre el tema anterior y el nuevo: una captura de la ventana tapa el
        cambio y se desvanece encima de la interfaz ya recoloreada."""
        try:
            from PIL import ImageGrab, ImageTk

            self.update_idletasks()
            x, y = self.winfo_rootx(), self.winfo_rooty()
            w, h = self.winfo_width(), self.winfo_height()
            shot = ImageTk.PhotoImage(ImageGrab.grab(bbox=(x, y, x + w, y + h), all_screens=True))
            cover = tk.Toplevel(self)
            cover.overrideredirect(True)
            cover.geometry(f"{w}x{h}+{x}+{y}")
            tk.Label(cover, image=shot, bd=0, highlightthickness=0).pack()
            cover._shot = shot
            cover.update()
        except Exception:
            apply()
            return
        apply()
        self.update_idletasks()

        def step(t):
            cover.attributes("-alpha", max(0.0, 1 - t))

        anim.animate(cover, "crossfade", CROSSFADE_MS, step, done=cover.destroy)

    def _poll_system_theme(self) -> None:
        """Con "Sistema", sigue en vivo el modo claro/oscuro de Windows."""
        try:
            if self.theme_pref == "sistema":
                self._apply_mode(self._wanted_mode())
            self.after(SYSTEM_POLL_MS, self._poll_system_theme)
        except tk.TclError:
            pass

    # ------------------------------------------------------------------ navegación

    def _activate_nav(self, key):
        previous, self._active = self._active, key
        for name, button in self._nav_items.items():
            if name == key:
                anim.set_button_colors(button, theme.SIDEBAR_ACTIVE_BG, theme.SIDEBAR_ACTIVE_HOVER)
                anim.fade(button, "text_color", theme.SIDEBAR_ITEM, theme.SIDEBAR_ACTIVE, 180, "text")
            elif name == previous:
                anim.set_button_colors(button, theme.SIDEBAR_BG, theme.SIDEBAR_HOVER)
                anim.fade(button, "text_color", theme.SIDEBAR_ACTIVE, theme.SIDEBAR_ITEM, 180, "text")
            else:
                button.configure(text_color=theme.SIDEBAR_ITEM)
        target = self._nav_items[key]
        self.update_idletasks()
        y_to = target.winfo_y() + (target.winfo_height() - 18) / 2
        start = getattr(self, "_indicator_y", None)
        indicator = self._indicator
        if start is None:
            indicator.place(x=14, y=y_to)
            self._indicator_y = y_to
            return

        def step(t):
            self._indicator_y = start + (y_to - start) * t
            indicator.place_configure(y=self._indicator_y)

        anim.animate(indicator, "slide", 180, step)

    # ------------------------------------------------------------------ enrutado

    def show_view(self, key: str):
        if key not in self._views:
            if key == "home":
                self._views[key] = HomeView(self.content, self)
            else:
                path, name, color, symbol = self._meta[key]
                cls = _view_class(path)
                self.building_meta = (name, color, symbol, key)
                try:
                    self._views[key] = cls(self.content, self)
                finally:
                    self.building_meta = None
            anim.enhance(self._views[key])
        view = self._views[key]
        for name, other in self._views.items():
            if name != key:
                other.place_forget()
        self._activate_nav(key)
        view.place(x=0, y=VIEW_SLIDE_PX, relwidth=1, relheight=1)
        view.lift()

        def step(t):
            view.place_configure(y=round(VIEW_SLIDE_PX * (1 - t)))

        anim.animate(view, "enter", 200, step)
        fade = getattr(view, "fade_in_labels", None)
        if fade:
            for label, color in fade:
                anim.fade(label, "text_color", theme.BG_PRIMARY, color, 200, key="enter_text")
