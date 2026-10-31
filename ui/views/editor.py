"""Editar PDF: editor real.

Detecta el texto y las imágenes de cada página (o los reconoce con OCR en
páginas escaneadas), permite editar el texto en el lugar con su mismo estilo,
mover, redimensionar, reordenar capas y borrar cualquier elemento, y agregar
texto, resaltados, dibujos, imágenes y redacciones.

La página que se ve es la que produce core.pdf_editor con las ediciones ya
aplicadas (mismo camino que el guardado), renderizada en segundo plano.
"""
from __future__ import annotations

import copy
import math
import tkinter as tk
import tkinter.font as tkfont
from tkinter import colorchooser, filedialog

import customtkinter as ctk
from PIL import Image, ImageTk

from core import errors, pdf_editor
from ui import anim, theme
from ui.assets import asset_path, icon
from ui.components import FilePicker, ScrollBar, ScrollFrame, Scroller, run_task

from .base import ToolView

TOOLS = [("select", "Seleccionar y editar", "select"), ("text", "Añadir texto", "text"),
         ("highlight", "Resaltar", "highlight"), ("draw", "Dibujar", "draw"),
         ("image", "Insertar imagen", "image"), ("redact", "Redactar (borra el contenido)", "redact-tool")]
TOOL_HINTS = {
    "select": "Doble clic en un texto para editarlo · arrastra para mover · tiradores: ancho del cuadro · "
              "Supr borra · flechas mueven 1 pt (Mayús: 10 pt).",
    "text": "Clic donde quieras escribir: toma el estilo del texto más cercano.",
    "highlight": "Arrastra sobre el texto para resaltarlo.",
    "draw": "Dibuja a mano alzada.",
    "image": "Clic donde quieras insertar una imagen PNG o JPG.",
    "redact": "Arrastra para tapar: al guardar, todo lo que quede debajo se borra de verdad.",
}
ZOOMS = ["Ajustar ancho", "50 %", "75 %", "100 %", "125 %", "150 %", "200 %", "300 %"]
PX_PER_PT = 96 / 72
MARGIN = 24
HANDLE = 5
WIDTH_HANDLE = 4          # tiradores de ancho de un cuadro de texto
WIDTH_PREVIEW_MS = 50     # pausa entre vistas previas al arrastrar uno
THUMB_ZOOM = 0.18
THUMB_W = 116
DRAG_PX = 4               # menos movimiento que esto con el botón presionado es un clic
HIT_PX = 3                # tolerancia alrededor de cada caja al hacer clic
PROPS_H = 64              # alto fijo de la barra de propiedades (dos filas)
THUMB_BATCH = 6           # miniaturas por tarea en segundo plano
DRAW_COLORS = ["#E5484D", "#1F2133", "#2F7FE0", "#30A46C"]
HIGHLIGHT_COLORS = ["#FFE600", "#9CF2B6", "#FFB3D9", "#9ED8FF"]
IMAGE_TYPES = [("Imágenes", "*.png *.jpg *.jpeg"), ("Todos los archivos", "*.*")]
UNDO_LIMIT = 100


def _icon_button(parent, name, command, tooltip_view=None, hint=None, width=36):
    button = ctk.CTkButton(parent, text="", image=icon(f"ui_{name}", 18), width=width, height=34,
                           command=command, **{**theme.BTN_SECONDARY, "border_width": 0,
                                               "fg_color": theme.SURFACE})
    if tooltip_view and hint:
        button.bind("<Enter>", lambda _e: tooltip_view.show_info(hint), add="+")
    return button


class PdfEditorView(ToolView):
    title = "Editar PDF"
    description = ("Doble clic en un texto para editarlo con su misma fuente, tamaño y color. "
                   "Mueve, redimensiona u ordena cualquier elemento.")
    run_label = "Guardar PDF"

    # ------------------------------------------------------------------ construcción

    def build_body(self, body):
        self.ed_session = None
        self.ed_src = None
        self.ed_models: dict[int, dict] = {}
        self.ed_states: dict[int, dict] = {}
        self.ed_order: list[int] = []
        self.ed_rotations: dict[int, int] = {}
        self.ed_current = None
        self.ed_tool = "select"
        self.ed_level = "line"
        self.ed_zoom = "Ajustar ancho"
        self.ed_scale = 1.0
        self.ed_image = None            # imagen PIL de la página (con ediciones)
        self.ed_photo = None
        self.ed_layout: dict[str, list] = {}
        self.ed_selected = None
        self.ed_hover = None
        self.ed_drag = None
        self.ed_editor = None           # (widget, element_id) mientras se edita en el lugar
        self.ed_undo: list = []
        self.ed_redo: list = []
        self.ed_next_id = 1
        self.ed_token = 0
        self.ed_render_job = None
        self.ed_verify = {}          # (página, id) -> cómo estaba antes de la edición sin comprobar
        self.ed_confirm_dialog = None
        self.ed_overflow_dialog = None
        self.ed_push = True          # el texto que crece desplaza el contenido de abajo
        self.ed_adjust = False       # el texto que se acorta sube el contenido de abajo
        self.ed_reflowing = False    # hay una edición integrándose al documento (en segundo plano)
        self.ed_width_job = None     # vista previa pendiente al arrastrar un tirador de ancho
        self.ed_width_busy = False
        self.ed_width_again = False
        self.ed_pending_double = None  # doble clic llegado antes de que la página esté lista
        self.ed_thumbs: dict[int, ImageTk.PhotoImage] = {}
        self.ed_thumbs_loading: set[int] = set()
        self.ed_thumb_job = None
        self.ed_thumb_drag = None
        self.ed_message_text = ""
        self.ed_font_note = ""
        self.ed_draw_color = DRAW_COLORS[0]
        self.ed_highlight_color = HIGHLIGHT_COLORS[0]
        self.ed_stroke = 2.0

        self.picker = FilePicker(body, exts=[".pdf"], multiple=False, compact=True,
                                 label="Arrastra el PDF a editar", on_change=self._file_changed)
        self.picker.pack(fill="x")

        bar = ctk.CTkFrame(body, **theme.CARD)
        bar.pack(fill="x", pady=(12, 8))
        row = ctk.CTkFrame(bar, fg_color="transparent")
        row.pack(fill="x", padx=10, pady=(8, 4))
        self.ed_tool_buttons = {}
        for key, hint, icon_name in TOOLS:
            button = _icon_button(row, icon_name, lambda k=key: self._set_tool(k), self, hint)
            button.pack(side="left", padx=2)
            self.ed_tool_buttons[key] = button
        ctk.CTkFrame(row, width=1, height=24, fg_color=theme.BORDER).pack(side="left", padx=8)
        _icon_button(row, "undo", self._undo, self, "Deshacer (Ctrl+Z)").pack(side="left", padx=2)
        _icon_button(row, "redo", self._redo, self, "Rehacer (Ctrl+Y)").pack(side="left", padx=2)
        ctk.CTkFrame(row, width=1, height=24, fg_color=theme.BORDER).pack(side="left", padx=8)
        _icon_button(row, "rotate-left", lambda: self._rotate(-90), self, "Rotar página a la izquierda"
                     ).pack(side="left", padx=2)
        _icon_button(row, "rotate-right", lambda: self._rotate(90), self, "Rotar página a la derecha"
                     ).pack(side="left", padx=2)
        _icon_button(row, "trash", self._delete_page, self, "Eliminar esta página").pack(side="left", padx=2)
        right = ctk.CTkFrame(row, fg_color="transparent")
        right.pack(side="right")
        _icon_button(right, "zoom-out", lambda: self._zoom_step(-1), self, "Alejar (Ctrl+rueda)").pack(side="left")
        self.ed_zoom_menu = ctk.CTkOptionMenu(right, values=ZOOMS, width=130, height=32,
                                              command=self._set_zoom, **theme.OPTIONMENU)
        self.ed_zoom_menu.set(self.ed_zoom)
        self.ed_zoom_menu.pack(side="left", padx=4)
        _icon_button(right, "zoom-in", lambda: self._zoom_step(1), self, "Acercar (Ctrl+rueda)").pack(side="left")
        self.ed_layers_button = ctk.CTkButton(right, text="Capas", image=icon("ui_layers", 18),
                                              width=96, height=34, command=self._toggle_layers,
                                              **{**theme.BTN_SECONDARY, "border_width": 0,
                                                 "fg_color": theme.SURFACE})
        self.ed_layers_button.pack(side="left", padx=(8, 0))

        # alto fijo: si cambiara al seleccionar, el lienzo (y la página) se moverían bajo el mouse
        self.ed_props = ctk.CTkFrame(bar, fg_color="transparent", height=PROPS_H)
        self.ed_props.pack(fill="x", padx=12, pady=(0, 8))
        self.ed_props.pack_propagate(False)
        self.ed_props_for = None
        self.ed_space = False

        main = ctk.CTkFrame(body, fg_color="transparent")
        main.pack(fill="both", expand=True)
        main.grid_columnconfigure(1, weight=1)
        main.grid_rowconfigure(0, weight=1)

        thumbs = ctk.CTkFrame(main, width=THUMB_W + 22, **theme.CARD)
        thumbs.grid(row=0, column=0, sticky="ns", padx=(0, 12))
        thumbs.grid_propagate(False)
        self.ed_thumb_canvas = tk.Canvas(thumbs, bg=theme.resolve(theme.SURFACE),
                                         highlightthickness=0, width=THUMB_W + 8)
        self.ed_thumb_canvas.pack(fill="both", expand=True, padx=4, pady=6)
        self.ed_thumb_canvas.bind("<Button-1>", self._thumb_press)
        self.ed_thumb_canvas.bind("<B1-Motion>", self._thumb_motion)
        self.ed_thumb_canvas.bind("<ButtonRelease-1>", self._thumb_release)
        self.ed_thumb_scroller = Scroller(self.ed_thumb_canvas, on_scroll=self._load_visible_thumbs)

        page_frame = ctk.CTkFrame(main, **theme.CARD)
        page_frame.grid(row=0, column=1, sticky="nsew")
        self.ed_canvas = tk.Canvas(page_frame, bg=theme.resolve(theme.CANVAS_BG),
                                   highlightthickness=0)
        vbar = ScrollBar(page_frame)
        hbar = ScrollBar(page_frame, orientation="horizontal")
        self.ed_scroller = Scroller(self.ed_canvas, vbar=vbar, hbar=hbar)
        theme.on_mode_change(page_frame, self._mode_changed)
        vbar.pack(side="right", fill="y", padx=(0, 2), pady=4)
        hbar.pack(side="bottom", fill="x", padx=4, pady=(0, 2))
        self.ed_canvas.pack(side="left", fill="both", expand=True, padx=(4, 0), pady=4)
        self.ed_badge = ctk.CTkLabel(page_frame, text="  Texto detectado por OCR  ", corner_radius=8,
                                     fg_color=theme.INFO_BG, text_color=theme.INFO, font=theme.FONT_SMALL)
        c = self.ed_canvas
        c.bind("<Configure>", lambda _e: self.ed_zoom == "Ajustar ancho" and self._schedule_render(150))
        c.bind("<Motion>", self._on_motion)
        c.bind("<Button-1>", self._on_press)
        c.bind("<B1-Motion>", self._on_drag)
        c.bind("<ButtonRelease-1>", self._on_release)
        c.bind("<Double-Button-1>", self._on_double)
        c.bind("<MouseWheel>", self._on_wheel)
        # mover la vista: barras, rueda, botón central o Espacio + arrastre (nunca el clic izquierdo solo)
        c.bind("<ButtonPress-2>", lambda e: c.scan_mark(e.x, e.y))
        c.bind("<B2-Motion>", lambda e: c.scan_dragto(e.x, e.y, gain=1))
        c.bind("<KeyPress-space>", self._space_down)
        c.bind("<KeyRelease-space>", self._space_up)
        c.bind("<Delete>", lambda _e: self._delete_selected("tecla Supr"))
        c.bind("<BackSpace>", self._on_canvas_backspace)
        c.bind("<Key>", self._on_canvas_key)
        c.bind("<Escape>", lambda _e: self._select(None))
        for key, (dx, dy) in {"Left": (-1, 0), "Right": (1, 0), "Up": (0, -1), "Down": (0, 1)}.items():
            c.bind(f"<{key}>", lambda _e, d=(dx, dy): self._nudge(*d, 1))
            c.bind(f"<Shift-{key}>", lambda _e, d=(dx, dy): self._nudge(*d, 10))
            if key in ("Left", "Right"):       # Alt + flechas: el ancho del cuadro de texto
                c.bind(f"<Alt-{key}>", lambda _e, s=dx: self._nudge_width(s, 1))
                c.bind(f"<Alt-Shift-{key}>", lambda _e, s=dx: self._nudge_width(s, 10))
        for seq in ("<Control-z>", "<Control-Z>"):
            c.bind(seq, lambda _e: self._undo())
        for seq in ("<Control-y>", "<Control-Y>", "<Control-Shift-Z>"):
            c.bind(seq, lambda _e: self._redo())

        self.ed_layers = ScrollFrame(main, width=210, **theme.SCROLLFRAME)
        self.ed_layers_visible = False
        self._set_tool("select", announce=False)
        self._message("Carga un PDF para empezar a editar.")

    # ------------------------------------------------------------------ utilidades de estado

    def _st(self, index=None) -> dict:
        index = self.ed_current if index is None else index
        return self.ed_states.setdefault(index, {"overrides": {}, "added": [], "z": None})

    def _snapshot(self):
        # los bytes del documento van tal cual (no se copian): una edición que movió contenido
        # cambió el documento base, y deshacerla es volver a estos bytes
        data = self.ed_session.data if self.ed_session else None
        return copy.deepcopy((self.ed_states, self.ed_order, self.ed_rotations, self.ed_next_id)) + (data,)

    def _push_undo(self):
        self.ed_undo.append(self._snapshot())
        del self.ed_undo[:-UNDO_LIMIT]
        self.ed_redo.clear()

    def _restore(self, snapshot):
        states, order, rotations, next_id, data = snapshot
        self.ed_states, self.ed_order, self.ed_rotations, self.ed_next_id = copy.deepcopy(
            (states, order, rotations, next_id))
        if self.ed_session is not None and data is not None and data is not self.ed_session.data:
            self.ed_session.adopt(data)
            self.ed_models.clear()
            self.ed_thumbs.clear()
            self.ed_thumbs_loading.clear()
            self.ed_layout = {}
        self.ed_verify.clear()   # el estado restaurado ya se mostró bien antes
        if self.ed_current not in self.ed_order:
            self.ed_current = self.ed_order[0] if self.ed_order else None
        self._select(None, render=False)
        self._draw_thumbs()
        self._show_page()

    def _undo(self):
        self._commit_edit()
        if self.ed_reflowing:
            return
        if not self.ed_undo:
            self.show_info("No hay nada para deshacer.")
            return
        self.ed_redo.append(self._snapshot())
        self._restore(self.ed_undo.pop())
        self.show_info("Se deshizo el último cambio.")

    def _redo(self):
        if self.ed_reflowing:
            return
        if not self.ed_redo:
            self.show_info("No hay nada para rehacer.")
            return
        self.ed_undo.append(self._snapshot())
        self._restore(self.ed_redo.pop())
        self.show_info("Se rehízo el cambio.")

    # ------------------------------------------------------------------ documento

    def _message(self, text):
        self.ed_message_text = text
        self.ed_canvas.delete("all")
        self.ed_canvas.create_text(28, 28, anchor="nw", text=text, font=theme.FONT_BODY,
                                   fill=theme.resolve(theme.TEXT_MUTED))

    def _mode_changed(self):
        self.ed_canvas.configure(bg=theme.resolve(theme.CANVAS_BG))
        self.ed_thumb_canvas.configure(bg=theme.resolve(theme.SURFACE))
        if self.ed_image is not None:
            self._draw_page()
        else:
            self._message(self.ed_message_text)
        self._draw_thumbs()

    def _file_changed(self, files):
        if not files:
            return
        src = files[0]
        self._commit_edit(cancel=True)
        if self.ed_session:
            self.ed_session.close()
        self.ed_session, self.ed_src = None, None
        self.ed_image, self.ed_layout, self.ed_hover = None, {}, None
        self._select(None, render=False)
        self.ed_models, self.ed_states, self.ed_rotations = {}, {}, {}
        self.ed_undo, self.ed_redo = [], []
        self.ed_verify.clear()
        self.ed_thumbs.clear()
        self.ed_thumbs_loading.clear()
        self.ed_token += 1
        token = self.ed_token
        self._message("Abriendo el PDF…")
        self.show_info("Abriendo el PDF…")

        def opened(session):
            if token != self.ed_token:
                session.close()
                return
            self.ed_session, self.ed_src = session, src
            self.ed_order = list(range(session.page_count))
            self.ed_current = 0
            self.ed_thumb_canvas.yview_moveto(0)
            self._show_page()
            self.show_info(f"{getattr(src, 'name', src)} abierto · doble clic en un texto para editarlo.")

        run_task(self, lambda _p: pdf_editor.EditorSession(src), opened,
                 lambda exc: (self._message(""), self.show_error(str(exc))))

    def _show_page(self):
        if self.ed_session is None or self.ed_current is None:
            return
        index = self.ed_current
        self._highlight_thumb()
        if index not in self.ed_models:
            self._message("Analizando la página…")
            token = self.ed_token

            def loaded(model):
                self.ed_models[index] = model
                if token == self.ed_token and index == self.ed_current:
                    self._show_page()

            def failed(exc):
                self.show_error(str(exc))

            run_task(self, lambda _p: self.ed_session.analyze(index), loaded, failed)
            return
        model = self.ed_models[index]
        if model["ocr"]:
            self.ed_badge.place(x=16, y=12)
            self.ed_badge.lift()
        else:
            self.ed_badge.place_forget()
        self._schedule_render(0)
        self._build_layers()

    # ------------------------------------------------------------------ render

    def _page_scale(self):
        if self.ed_session is None:
            return 1.0
        width, _height = self.ed_session.page_size(self.ed_current)
        if self.ed_rotations.get(self.ed_current, 0) % 180:
            width = _height
        if self.ed_zoom == "Ajustar ancho":
            return max(0.2, (self.ed_canvas.winfo_width() - 2 * MARGIN) / width)
        return int(self.ed_zoom.split()[0]) / 100 * PX_PER_PT

    def _schedule_render(self, delay=60):
        if self.ed_render_job:
            self.after_cancel(self.ed_render_job)
        self.ed_render_job = self.after(delay, self._render)

    def _render(self):
        self.ed_render_job = None
        if self.ed_session is None or self.ed_current not in self.ed_models:
            return
        if self.ed_canvas.winfo_width() < 50:  # aún sin tamaño real: "ajustar ancho" daría una página diminuta
            self._schedule_render(80)
            return
        index, scale = self.ed_current, self._page_scale()
        state = copy.deepcopy(self.ed_states.get(index))
        session = self.ed_session
        self.ed_token += 1
        token = self.ed_token
        # ediciones recién confirmadas: el render comprueba que el texto nuevo quedó y se ve
        verify = [uid for (page, uid) in self.ed_verify if page == index]
        kept = []   # falta aceptar la fuente: se comprueban cuando de verdad se dibujen

        def done(result):
            if token != self.ed_token or index != self.ed_current:
                return
            for uid in verify:
                if uid not in kept:
                    self.ed_verify.pop((index, uid), None)
            image, layout, notes = result
            self.ed_scale, self.ed_image, self.ed_layout = scale, image, layout
            self._update_thumb(index, image)
            self._draw_page()

        def failed(exc):
            if token != self.ed_token or index != self.ed_current:
                return
            if verify:   # la edición no quedó: vuelve el texto original (nunca un hueco)
                self._rollback(index, verify, exc)
            else:
                self.show_error(f"No se pudo mostrar la página: {errors.reason(exc)}")

        run_task(self, lambda _p: session.render_page(index, state, scale, verify=verify,
                                                      kept_out=kept), done, failed)

    def _rollback(self, index, uids, exc):
        """Deshace las ediciones `uids` de la página `index` que no se pudieron aplicar
        (o solo las que fallaron, si se sabe cuáles) y avisa con el motivo."""
        failures = getattr(exc, "failures", None) or {uid: errors.reason(exc) for uid in uids}
        state = self.ed_states.get(index, {})
        for uid in uids:
            previous = self.ed_verify.pop((index, uid), None)
            if uid not in failures:
                continue
            if uid.startswith("N"):
                element = next((a for a in state.get("added", []) if a["id"] == uid), None)
                if element is not None and previous is not None:
                    element["text"] = previous
            elif previous is None:
                state.get("overrides", {}).pop(uid, None)
            else:
                state.setdefault("overrides", {})[uid] = previous
            self.ed_layout.pop(uid, None)
        reason = "; ".join(dict.fromkeys(failures.values()))
        errors.log.error("Edición deshecha en la página %d (%s): %s", index + 1,
                         ", ".join(failures), reason)
        self._build_layers()
        self._build_props()
        self._schedule_render(0)
        self.show_error(f"No se pudo aplicar la edición: {reason}")

    def _draw_page(self):
        c = self.ed_canvas
        c.delete("all")
        image = self.ed_image
        rotation = self.ed_rotations.get(self.ed_current, 0) % 360
        if rotation:
            image = image.rotate(-rotation, expand=True)
        self.ed_photo = ImageTk.PhotoImage(image)
        c.create_rectangle(MARGIN + 2, MARGIN + 3, MARGIN + image.width + 2, MARGIN + image.height + 3,
                           fill=theme.resolve(theme.PAGE_SHADOW), outline="")
        c.create_image(MARGIN, MARGIN, anchor="nw", image=self.ed_photo, tags="page")
        c.configure(scrollregion=(0, 0, image.width + 2 * MARGIN, image.height + 2 * MARGIN))
        self._draw_overlay()

    def _editable(self):
        if self.ed_session is None or self.ed_image is None:
            return False
        if self.ed_rotations.get(self.ed_current, 0) % 360:
            self.show_info("La página está rotada: vuelve a 0° para editar sus elementos "
                           "(la rotación se aplica al guardar).")
            return False
        return True

    # ------------------------------------------------------------------ elementos

    def _model(self):
        return self.ed_models.get(self.ed_current)

    def _originals(self):
        model = self._model()
        return {u["id"]: u for u in model["lines"] + model["blocks"] + model["images"]} if model else {}

    def _element(self, uid):
        """Elemento efectivo (original con cambios o agregado) o None."""
        state = self._st()
        for added in state["added"]:
            if added["id"] == uid:
                return added
        originals = self._originals()
        if uid in originals:
            return pdf_editor._effective(originals[uid], state["overrides"].get(uid, {}))
        return None

    def _box(self, uid):
        if uid in self.ed_layout:
            return self.ed_layout[uid]
        element = self._element(uid)
        return element.get("bbox") if element else None

    def _block_mode(self, block_id):
        return block_id in self._st()["overrides"]

    def _selectable(self):
        """Ids seleccionables de arriba hacia abajo."""
        model = self._model()
        if not model:
            return []
        state = self._st()
        order = state["z"] or self.ed_session.default_order(model, state)
        ids = []
        for uid in reversed(order):
            if uid.startswith("N") or uid.startswith("I"):
                ids.append(uid)
            elif uid.startswith("B"):
                ids.append(uid)
            elif uid.startswith("L"):
                line = next((l for l in model["lines"] if l["id"] == uid), None)
                if line and self.ed_level == "block" and not self._block_mode(line["block_id"]):
                    if line["block_id"] not in ids:
                        ids.append(line["block_id"])
                else:
                    ids.append(uid)
        return [uid for uid in ids if not state["overrides"].get(uid, {}).get("deleted")]

    def _to_screen(self, box):
        s = self.ed_scale
        return (MARGIN + box[0] * s, MARGIN + box[1] * s, MARGIN + box[2] * s, MARGIN + box[3] * s)

    def _to_pt(self, x, y):
        return ((self.ed_canvas.canvasx(x) - MARGIN) / self.ed_scale,
                (self.ed_canvas.canvasy(y) - MARGIN) / self.ed_scale)

    def _hit(self, x_pt, y_pt):
        """Elemento bajo el punto (el de más arriba gana), con 3 px de tolerancia en pantalla."""
        tol = HIT_PX / max(self.ed_scale, 1e-6)
        for uid in self._selectable():
            box = self._box(uid)
            if box and box[0] - tol <= x_pt <= box[2] + tol and box[1] - tol <= y_pt <= box[3] + tol:
                return uid
        return None

    def _kind(self, uid):
        element = self._element(uid)
        return element["kind"] if element else None

    def _resizable(self, uid):
        element = self._element(uid)
        return bool(element) and element["kind"] in ("image", "highlight", "redact") and (
            element.get("movable", True))

    def _movable(self, uid):
        element = self._element(uid)
        return bool(element) and element.get("movable", True)

    # ------------------------------------------------------------------ dibujo de selección

    def _draw_overlay(self):
        c = self.ed_canvas
        c.delete("overlay")
        geom = getattr(self, "ed_editor_geom", None)
        drag = self.ed_drag
        if drag and drag.get("mode") == "width":
            self._draw_width_drag(drag)
            return
        if self.ed_editor and geom:
            x0, y0, x1, y1 = geom
            c.create_rectangle(x0 - 3, y0 - 3, x1 + 3, y1 + 3, outline=theme.resolve(theme.ACCENT),
                               width=2, tags="overlay")
            self._draw_width_handles(x0, y0, x1, y1)
            return
        if self.ed_hover and self.ed_hover != self.ed_selected:
            box = self._box(self.ed_hover)
            if box:
                x0, y0, x1, y1 = self._to_screen(box)
                c.create_rectangle(x0 - 3, y0 - 3, x1 + 3, y1 + 3,
                                   outline=theme.resolve(theme.FOCUS_RING), width=2, tags="overlay")
        if self.ed_selected:
            box = self._text_frame(self.ed_selected) or self._box(self.ed_selected)
            if box:
                x0, y0, x1, y1 = self._to_screen(box)
                accent = theme.resolve(theme.ACCENT)
                c.create_rectangle(x0 - 3, y0 - 3, x1 + 3, y1 + 3, outline=accent, width=2,
                                   tags="overlay")
                if self._resizable(self.ed_selected):  # tiradores blancos: van sobre la página
                    for hx, hy in ((x0, y0), (x1, y0), (x0, y1), (x1, y1)):
                        c.create_rectangle(hx - HANDLE, hy - HANDLE, hx + HANDLE, hy + HANDLE,
                                           fill="#FFFFFF", outline=accent, width=2, tags="overlay")
                if self._text_frame(self.ed_selected) and self.ed_tool == "select":
                    self._draw_width_handles(x0, y0, x1, y1)

    # ------------------------------------------------------------------ ancho del cuadro de texto

    def _text_frame(self, uid):
        """Cuadro de un texto en puntos: su ancho elegido o, si no, lo que ocupa."""
        element = self._element(uid)
        if not element or element["kind"] != "text":
            return None
        box = self._box(uid)
        if not box:
            return None
        fixed = element.get("box")
        return [fixed[0], box[1], fixed[1], box[3]] if fixed else list(box)

    @staticmethod
    def _handle_points(x0, y0, x1, y1):
        """Tiradores en pantalla: los lados y las esquinas (todos cambian solo el ancho)."""
        middle = (y0 + y1) / 2
        return [("w", x0 - 3, middle), ("e", x1 + 3, middle), ("w", x0 - 3, y0 - 3), ("e", x1 + 3, y0 - 3),
                ("w", x0 - 3, y1 + 3), ("e", x1 + 3, y1 + 3)]

    def _draw_width_handles(self, x0, y0, x1, y1):
        accent = theme.resolve(theme.ACCENT)
        for _side, hx, hy in self._handle_points(x0, y0, x1, y1):
            self.ed_canvas.create_rectangle(hx - WIDTH_HANDLE, hy - WIDTH_HANDLE, hx + WIDTH_HANDLE,
                                            hy + WIDTH_HANDLE, fill="#FFFFFF", outline=accent, width=2,
                                            tags="overlay")

    def _handle_frame(self):
        """(id, cuadro en pantalla) del texto con tiradores visibles."""
        if self.ed_tool != "select":
            return None, None
        if self.ed_editor:
            return self.ed_editor[1], getattr(self, "ed_editor_geom", None)
        uid = self.ed_selected
        frame = self._text_frame(uid) if uid else None
        return uid, self._to_screen(frame) if frame else None

    def _handle_at(self, event):
        """"w", "e" o None según el tirador que esté bajo el mouse."""
        _uid, frame = self._handle_frame()
        if not frame or not self._editable_quiet():
            return None
        cx, cy = self.ed_canvas.canvasx(event.x), self.ed_canvas.canvasy(event.y)
        for side, hx, hy in self._handle_points(*frame):
            if abs(cx - hx) <= WIDTH_HANDLE + 3 and abs(cy - hy) <= WIDTH_HANDLE + 3:
                return side
        return None

    def _width_limits(self, uid):
        element = self._element(uid)
        ov = self._st()["overrides"].get(uid, {})
        limits = self.ed_session.width_limits(self.ed_current, uid, ov, element)
        dx = ov.get("dx", 0)
        return {**limits, "left": limits["left"] + dx, "right": limits["right"] + dx}

    @staticmethod
    def _width_bounds(frame, side, x, symmetric, limits):
        """Bordes nuevos del cuadro con el tirador en `x`: nunca menos que la palabra más larga
        ni más allá de la columna. Con Mayús, simétrico desde el centro."""
        x0, x1, least = frame[0], frame[2], limits["min_width"]
        if symmetric:
            center = (x0 + x1) / 2
            half = max(least / 2, abs(x - center))
            half = min(half, center - limits["left"], limits["right"] - center)
            return center - half, center + half
        if side == "e":
            return x0, max(x0 + least, min(x, limits["right"]))
        return min(x1 - least, max(x, limits["left"])), x1

    def _start_width_drag(self, uid, side, event):
        snapshot = self._snapshot()      # un solo paso de deshacer, también si entra a modo párrafo
        self._prepare(uid, push=False)
        frame = self._text_frame(uid)
        if frame is None:
            return
        self._select(uid)
        self.ed_drag = {"mode": "width", "uid": uid, "side": side, "frame": frame, "x0": frame[0],
                        "x1": frame[2], "moved": False, "limits": self._width_limits(uid),
                        "snapshot": snapshot, "start": self._to_pt(event.x, event.y)[0],
                        "origin_x": frame[2] if side == "e" else frame[0]}
        self.show_info("Arrastra para cambiar el ancho · Mayús: desde el centro.")

    def _width_moved(self, drag, x, symmetric):
        target = drag["origin_x"] + x - drag["start"]
        x0, x1 = self._width_bounds(drag["frame"], drag["side"], target, symmetric, drag["limits"])
        if abs(x0 - drag["x0"]) < 0.01 and abs(x1 - drag["x1"]) < 0.01:
            return
        drag.update(x0=x0, x1=x1)
        drag["moved"] = abs(x0 - drag["frame"][0]) > 0.25 or abs(x1 - drag["frame"][2]) > 0.25
        self._draw_overlay()
        if self.ed_width_job is None:
            self.ed_width_job = self.after(WIDTH_PREVIEW_MS, self._width_preview)

    def _draw_width_drag(self, drag):
        c = self.ed_canvas
        accent = theme.resolve(theme.ACCENT)
        frame = drag["frame"]
        x0, y0, x1, y1 = self._to_screen([drag["x0"], frame[1], drag["x1"], frame[3]])
        c.create_rectangle(x0 - 3, y0 - 3, x1 + 3, y1 + 3, outline=accent, dash=(5, 3), width=2, tags="overlay")
        self._draw_width_handles(x0, y0, x1, y1)
        limits = drag["limits"]
        height = self.ed_image.height + MARGIN if self.ed_image is not None else y1
        touching = []
        if drag["x1"] >= limits["right"] - 0.05:
            touching.append(x1)
        if drag["x0"] <= limits["left"] + 0.05:
            touching.append(x0)
        for x in touching:      # guía: llegó al margen o al borde de la columna
            c.create_line(x, MARGIN, x, height, fill=accent, dash=(2, 4), width=1, tags="overlay")
        drag["guide"] = bool(touching)

    def _width_override(self, uid, x0, x1, state):
        """Pone el ancho en `state` (el estado de la página o una copia para la vista previa)."""
        if uid.startswith("N"):
            element = next(a for a in state["added"] if a["id"] == uid)
            element["box"] = [x0, x1]
            element["origin"] = [x0, element["origin"][1]]
            element["bbox"] = [x0, element["bbox"][1], x1, element["bbox"][3]]
            return None
        ov = state["overrides"].setdefault(uid, {})
        dx = ov.get("dx", 0)
        ov["box"] = [x0 - dx, x1 - dx]
        if "text" not in ov:     # un párrafo se reacomoda corrido, sin los saltos de renglón del PDF
            element = self._element(uid)
            flowed = self.ed_session.flow_text(self.ed_current, uid) if uid.startswith("B") else None
            ov["text"] = flowed or element["text"]
        return ov

    def _width_preview(self):
        """El texto se reacomoda mientras se arrastra (con la misma lógica que al soltar)."""
        self.ed_width_job = None
        drag = self.ed_drag
        if not drag or drag.get("mode") != "width" or self.ed_session is None:
            return
        if self.ed_width_busy:
            self.ed_width_again = True
            return
        state = copy.deepcopy(self._st())
        self._width_override(drag["uid"], drag["x0"], drag["x1"], state)
        index, scale, session, uid = self.ed_current, self.ed_scale, self.ed_session, drag["uid"]
        push, adjust = self.ed_push, self.ed_adjust
        self.ed_width_busy = True

        def finished():
            self.ed_width_busy = False
            if self.ed_width_again and self.ed_drag is drag:
                self.ed_width_again = False
                self.ed_width_job = self.after(WIDTH_PREVIEW_MS, self._width_preview)

        def done(image):
            if self.ed_drag is drag:
                self.ed_image = image
                self._draw_page()
            finished()

        run_task(self, lambda _p: session.preview_width(index, state, uid, scale, push, adjust), done,
                 lambda _exc: finished())

    def _apply_width(self, uid, x0, x1, snapshot=None):
        """Aplica el ancho: las mismas reglas que al escribir (desplazar, diálogo, deshacer)."""
        if snapshot is None:
            self._push_undo()
        else:
            self.ed_undo.append(snapshot)
            del self.ed_undo[:-UNDO_LIMIT]
            self.ed_redo.clear()
        state = self._st()
        previous = copy.deepcopy(state["overrides"].get(uid))
        override = self._width_override(uid, x0, x1, state)
        self.ed_layout.pop(uid, None)
        if override is not None:
            self.ed_verify.setdefault((self.ed_current, uid), previous)
            if self._needs_reflow(uid, override):
                self.ed_verify.pop((self.ed_current, uid), None)
                self._start_reflow(uid)
                return
        self._after_change()
        self.show_info("Ancho del cuadro actualizado (Ctrl+Z para deshacer).")

    def _nudge_width(self, sign, step):
        """Alt + flechas: el borde derecho, de a 1 pt (con Mayús, 10 pt)."""
        uid = self.ed_selected
        if not uid or self._kind(uid) != "text" or not self._editable() or self.ed_reflowing:
            return "break"
        snapshot = self._snapshot()
        self._prepare(uid, push=False)
        frame = self._text_frame(uid)
        if frame is None:
            return "break"
        x0, x1 = self._width_bounds(frame, "e", frame[2] + sign * step, False, self._width_limits(uid))
        if abs(x1 - frame[2]) > 0.01:
            self._apply_width(uid, x0, x1, snapshot)
        return "break"

    def _select(self, uid, render=True):
        # volver a elegir lo mismo no reconstruye la barra (sin parpadeos ni saltos)
        same = uid == self.ed_selected and self.ed_props_for == (uid, self.ed_tool)
        self.ed_selected = uid
        if not same:
            self.ed_font_note = ""
            self._build_props()
            self._build_layers()
        if render:
            self._draw_overlay()
        if uid and self._kind(uid) == "text" and not same:
            self._refresh_font_note(uid)

    # ------------------------------------------------------------------ herramientas

    def _set_tool(self, key, announce=True):
        self._commit_edit()
        self.ed_tool = key
        for name, button in self.ed_tool_buttons.items():
            anim.set_button_colors(button, theme.SELECTED_TINT if name == key else theme.SURFACE,
                                   theme.SELECTED_TINT_HOVER if name == key else theme.BG_HOVER)
        cursors = {"select": "arrow", "text": "xterm", "highlight": "crosshair", "draw": "pencil",
                   "image": "plus", "redact": "crosshair"}
        self.ed_canvas.configure(cursor=cursors[key])
        self._build_props()
        if announce:
            self.show_info(TOOL_HINTS[key])

    # ------------------------------------------------------------------ mouse

    def _on_motion(self, event):
        if self.ed_drag or not self._editable_quiet():
            return
        if self._handle_at(event):
            self.ed_canvas.configure(cursor="sb_h_double_arrow")
            return
        x, y = self._to_pt(event.x, event.y)
        hover = self._hit(x, y) if self.ed_tool == "select" else None
        if hover != self.ed_hover:
            self.ed_hover = hover
            self._draw_overlay()
        corner = self._corner_at(event) if self.ed_selected and self._resizable(self.ed_selected) else None
        if self.ed_tool == "select":
            self.ed_canvas.configure(cursor="sizing" if corner else ("fleur" if hover else "arrow"))

    def _editable_quiet(self):
        return (self.ed_session is not None and self.ed_image is not None
                and not self.ed_rotations.get(self.ed_current, 0) % 360)

    def _corner_at(self, event):
        box = self._box(self.ed_selected)
        if not box:
            return None
        x0, y0, x1, y1 = self._to_screen(box)
        cx, cy = self.ed_canvas.canvasx(event.x), self.ed_canvas.canvasy(event.y)
        for name, (hx, hy) in {"nw": (x0, y0), "ne": (x1, y0), "sw": (x0, y1), "se": (x1, y1)}.items():
            if abs(cx - hx) <= HANDLE + 3 and abs(cy - hy) <= HANDLE + 3:
                return name
        return None

    def _space_down(self, _event=None):
        if not self.ed_space:
            self.ed_space = True
            self.ed_canvas.configure(cursor="fleur")

    def _space_up(self, _event=None):
        self.ed_space = False
        self.ed_canvas.configure(cursor="arrow" if self.ed_tool == "select" else "crosshair")

    def _on_press(self, event):
        if self.focus_get() is not self.ed_canvas:
            self.ed_canvas.focus_force()   # también activa la ventana: el clic sigue procesándose
        handle = self._handle_at(event)
        if self.ed_editor:
            uid = self.ed_editor[1]
            self._commit_edit()
            if handle and not self.ed_reflowing and self.ed_overflow_dialog is None:
                self._start_width_drag(uid, handle, event)
            return
        if self.ed_reflowing:
            return
        if self.ed_space:   # Espacio + arrastre: mover la vista
            self.ed_canvas.scan_mark(event.x, event.y)
            self.ed_drag = {"mode": "pan"}
            return
        if not self._editable():
            return
        x, y = self._to_pt(event.x, event.y)
        tool = self.ed_tool
        if tool == "select":
            if handle:
                self._start_width_drag(self.ed_selected, handle, event)
                return
            corner = self._corner_at(event) if self.ed_selected and self._resizable(self.ed_selected) else None
            if corner:
                self.ed_drag = {"mode": "resize", "corner": corner, "uid": self.ed_selected,
                                "start": (x, y), "box": list(self._box(self.ed_selected)),
                                "shift": bool(event.state & 0x1)}
                return
            previous = self.ed_selected
            uid = self._hit(x, y)
            self._select(uid)
            screen = (event.x, event.y)
            if uid and self._movable(uid):
                self.ed_drag = {"mode": "move", "uid": uid, "start": (x, y), "screen": screen,
                                "moved": False, "box": list(self._box(uid)),
                                "reselect": uid == previous}
            elif uid and self._kind(uid) == "text":
                self.ed_drag = {"mode": "click", "uid": uid, "screen": screen,
                                "reselect": uid == previous}
            elif uid:
                self.show_info("Esta imagen está girada o deformada: no se puede mover.")
        elif tool == "text":
            self._add_text(x, y)
        elif tool == "image":
            self._add_image(x, y)
        elif tool in ("highlight", "redact"):
            self.ed_drag = {"mode": "rect", "start": (x, y), "box": [x, y, x, y]}
        elif tool == "draw":
            self.ed_drag = {"mode": "draw", "points": [(x, y)]}

    def _on_drag(self, event):
        drag = self.ed_drag
        if not drag:
            return
        if drag["mode"] == "pan":
            self.ed_canvas.scan_dragto(event.x, event.y, gain=1)
            return
        x, y = self._to_pt(event.x, event.y)
        c = self.ed_canvas
        if drag["mode"] == "width":
            self._width_moved(drag, x, bool(event.state & 0x1))
        elif drag["mode"] == "move":
            dx, dy = x - drag["start"][0], y - drag["start"][1]
            # hasta 4 px de temblor sigue siendo un clic (no mueve nada)
            if not drag["moved"] and math.hypot(event.x - drag["screen"][0],
                                                event.y - drag["screen"][1]) <= DRAG_PX:
                return
            if not drag["moved"]:
                drag["moved"] = True
                self._start_ghost(drag)
            drag["delta"] = (dx, dy)
            c.coords("ghost", MARGIN + (drag["box"][0] + dx) * self.ed_scale,
                     MARGIN + (drag["box"][1] + dy) * self.ed_scale)
            c.delete("overlay")
        elif drag["mode"] == "resize":
            drag["new"] = self._resized_box(drag, x, y, bool(event.state & 0x1))
            c.delete("overlay")
            c.delete("preview")
            c.create_rectangle(*self._to_screen(drag["new"]), outline=theme.resolve(theme.ACCENT),
                               dash=(5, 3), width=2, tags="preview")
        elif drag["mode"] == "rect":
            x0, y0 = drag["start"]
            drag["box"] = [min(x0, x), min(y0, y), max(x0, x), max(y0, y)]
            c.delete("preview")
            color = self.ed_highlight_color if self.ed_tool == "highlight" else "#000000"
            c.create_rectangle(*self._to_screen(drag["box"]), fill=color,
                               stipple="gray50" if self.ed_tool == "highlight" else "",
                               outline="", tags="preview")
        elif drag["mode"] == "draw":
            drag["points"].append((x, y))
            c.delete("preview")
            pts = [v for p in drag["points"] for v in (MARGIN + p[0] * self.ed_scale,
                                                         MARGIN + p[1] * self.ed_scale)]
            if len(pts) >= 4:
                c.create_line(*pts, fill=self.ed_draw_color, width=max(1, self.ed_stroke * self.ed_scale),
                              capstyle="round", joinstyle="round", smooth=True, tags="preview")

    def _start_ghost(self, drag):
        """Vista rápida del arrastre: se tapa el original y se mueve un recorte."""
        x0, y0, x1, y1 = (int(v) for v in self._to_screen(drag["box"]))
        crop = self.ed_image.crop((max(0, x0 - MARGIN), max(0, y0 - MARGIN),
                                   max(1, x1 - MARGIN), max(1, y1 - MARGIN)))
        self.ed_ghost_photo = ImageTk.PhotoImage(crop)
        bg = self._sample_bg(drag["box"])
        self.ed_canvas.create_rectangle(x0, y0, x1, y1, fill=bg, outline="", tags="ghost_cover")
        self.ed_canvas.create_image(x0, y0, anchor="nw", image=self.ed_ghost_photo, tags="ghost")

    def _resized_box(self, drag, x, y, keep_ratio):
        x0, y0, x1, y1 = drag["box"]
        corner = drag["corner"]
        nx0, ny0, nx1, ny1 = x0, y0, x1, y1
        if "w" in corner:
            nx0 = min(x, x1 - 4)
        else:
            nx1 = max(x, x0 + 4)
        if "n" in corner:
            ny0 = min(y, y1 - 4)
        else:
            ny1 = max(y, y0 + 4)
        if keep_ratio:
            ratio = (y1 - y0) / max(0.1, x1 - x0)
            height = (nx1 - nx0) * ratio
            if "n" in corner:
                ny0 = ny1 - height
            else:
                ny1 = ny0 + height
        return [nx0, ny0, nx1, ny1]

    def _on_release(self, event):
        drag, self.ed_drag = self.ed_drag, None
        c = self.ed_canvas
        c.delete("preview")
        c.delete("ghost")
        c.delete("ghost_cover")
        if not drag or drag["mode"] == "pan":
            return
        if drag["mode"] == "width":
            if self.ed_width_job is not None:
                self.after_cancel(self.ed_width_job)
                self.ed_width_job = None
            if drag["moved"]:
                self._apply_width(drag["uid"], drag["x0"], drag["x1"], drag["snapshot"])
            else:
                self._draw_overlay()
        elif drag["mode"] == "move" and drag.get("moved"):
            self._move(drag["uid"], *drag["delta"])
        elif drag["mode"] in ("move", "click") and drag.get("reselect") \
                and self._kind(drag["uid"]) == "text":
            # clic sobre un texto que ya estaba elegido: editarlo, con el cursor donde se hizo clic
            self._begin_edit(drag["uid"], click=(event.x, event.y))
        elif drag["mode"] == "resize" and drag.get("new"):
            self._push_undo()
            self._set_box(drag["uid"], drag["new"])
            self._after_change()
        elif drag["mode"] == "rect":
            box = drag["box"]
            if box[2] - box[0] > 2 and box[3] - box[1] > 2:
                if self.ed_tool == "highlight":
                    self._add({"kind": "highlight", "bbox": box, "color": self.ed_highlight_color})
                else:
                    self._add({"kind": "redact", "bbox": box})
        elif drag["mode"] == "draw" and len(drag["points"]) > 1:
            xs = [p[0] for p in drag["points"]]
            ys = [p[1] for p in drag["points"]]
            pad = self.ed_stroke
            self._add({"kind": "draw", "points": [list(p) for p in drag["points"]],
                       "color": self.ed_draw_color, "width": self.ed_stroke,
                       "bbox": [min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad]})
        else:
            self._draw_overlay()

    def _on_double(self, event):
        if self.ed_tool != "select":
            return
        if self.ed_session is not None and (self.ed_image is None or self._model() is None):
            self._defer_double(event.x, event.y)   # la página todavía se está preparando
            return
        if not self._editable():
            return
        if self.ed_editor:   # el primer clic del doble clic cerró otro cuadro de edición
            self._commit_edit()
        x, y = self._to_pt(event.x, event.y)
        uid = self._hit(x, y)
        if uid and self._kind(uid) == "text":
            self.ed_drag = None
            self._select(uid)
            self._begin_edit(uid, click=(event.x, event.y))

    def _defer_double(self, x, y, tries=0):
        """Abre el editor del doble clic que llegó mientras la página cargaba."""
        self.ed_pending_double = (x, y)
        if self.ed_image is not None and self._model() is not None:
            self.ed_pending_double = None

            class _Click:
                pass

            click = _Click()
            click.x, click.y = x, y
            self._on_double(click)
        elif tries < 100 and self.ed_session is not None:
            self.after(100, lambda: self.ed_pending_double == (x, y) and self._defer_double(x, y, tries + 1))

    def _on_canvas_key(self, event):
        """Escribir sobre un texto elegido (sin estar editándolo) abre el editor y agrega la
        letra al final: lo que se teclea nunca se pierde ni va al lienzo."""
        ctrl, alt = bool(event.state & 0x4), bool(event.state & 0x20000)
        if ctrl != alt or not event.char or not event.char.isprintable():
            return None   # atajos con Ctrl o Alt, flechas, teclas sin carácter (AltGr = Ctrl+Alt sí escribe)
        if self.ed_editor:   # el foco quedó en el lienzo: la letra va al cuadro abierto
            widget = self.ed_editor[0]
            widget.focus_force()
            widget.insert("insert", event.char)
            widget.event_generate("<KeyRelease>")
            return "break"
        uid = self.ed_selected
        if not uid or self._kind(uid) != "text" or not self._editable():
            return None
        self._begin_edit(uid, caret="end")
        if self.ed_editor:
            widget = self.ed_editor[0]
            widget.insert("end-1c", event.char)
            widget.mark_set("insert", "end-1c")
            widget.event_generate("<KeyRelease>")
        return "break"

    def _on_canvas_backspace(self, _event):
        """Retroceso sobre un texto elegido: abre el editor y borra solo la última letra
        (nunca el elemento entero)."""
        if self.ed_editor:
            widget = self.ed_editor[0]
            widget.focus_force()
            widget.delete("insert-1c")
            widget.event_generate("<KeyRelease>")
            return "break"
        uid = self.ed_selected
        if not uid or self._kind(uid) != "text" or not self._editable():
            return "break"
        self._begin_edit(uid, caret="end")
        if self.ed_editor:
            widget = self.ed_editor[0]
            widget.delete("end-2c")
            widget.mark_set("insert", "end-1c")
            widget.event_generate("<KeyRelease>")
        return "break"

    def _on_wheel(self, event):
        if event.state & 0x4:  # Ctrl + rueda: zoom (el desplazamiento lo hace el motor compartido)
            self._zoom_step(1 if event.delta > 0 else -1)
            return "break"
        return None

    # ------------------------------------------------------------------ cambios del modelo

    def _after_change(self):
        self._build_layers()
        self._build_props()
        self._schedule_render(0)

    def _prepare(self, uid, push=True):
        """Un bloque se edita como párrafo: antes del primer cambio se activa ese modo
        (combinando lo que ya se haya editado en sus líneas)."""
        if uid and uid.startswith("B") and uid not in self._st()["overrides"]:
            self._enter_block_mode(uid, push)

    def _move(self, uid, dx, dy):
        self._prepare(uid)
        self._push_undo()
        element = self._element(uid)
        if uid.startswith("N"):
            self._shift_added(element, dx, dy)
        elif element["kind"] == "image":
            box = element["bbox"]
            self._st()["overrides"].setdefault(uid, {})["bbox"] = [box[0] + dx, box[1] + dy,
                                                                    box[2] + dx, box[3] + dy]
        else:
            ov = self._st()["overrides"].setdefault(uid, {})
            ov["dx"] = ov.get("dx", 0) + dx
            ov["dy"] = ov.get("dy", 0) + dy
        if uid in self.ed_layout:
            b = self.ed_layout[uid]
            self.ed_layout[uid] = [b[0] + dx, b[1] + dy, b[2] + dx, b[3] + dy]
        self._after_change()

    @staticmethod
    def _shift_added(element, dx, dy):
        b = element["bbox"]
        element["bbox"] = [b[0] + dx, b[1] + dy, b[2] + dx, b[3] + dy]
        if "origin" in element:
            element["origin"] = [element["origin"][0] + dx, element["origin"][1] + dy]
        if "box" in element:
            element["box"] = [element["box"][0] + dx, element["box"][1] + dx]
        if "points" in element:
            element["points"] = [[p[0] + dx, p[1] + dy] for p in element["points"]]

    def _set_box(self, uid, box):
        if uid.startswith("N"):
            element = self._element(uid)
            element["bbox"] = list(box)
        else:
            self._st()["overrides"].setdefault(uid, {})["bbox"] = list(box)
        self.ed_layout[uid] = list(box)

    def _nudge(self, dx, dy, step):
        if self.ed_selected and self._movable(self.ed_selected) and self._editable():
            self._move(self.ed_selected, dx * step, dy * step)
        return "break"

    def _delete_selected(self, trigger="?"):
        uid = self.ed_selected
        if not uid or not self._editable():
            return
        element = self._element(uid) or {}
        kind = element.get("kind", "?")
        self._prepare(uid)
        self._push_undo()
        snapshot = self.ed_undo[-1]
        state = self._st()
        if uid.startswith("N"):
            state["added"] = [a for a in state["added"] if a["id"] != uid]
            if state["z"]:
                state["z"] = [u for u in state["z"] if u != uid]
        else:
            state["overrides"].setdefault(uid, {})["deleted"] = True
        self.ed_layout.pop(uid, None)
        self._select(None, render=False)
        self._after_change()
        names = {"text": "Texto", "image": "Imagen", "draw": "Trazo", "highlight": "Resaltado",
                 "redact": "Censura", "signature": "Firma"}
        what = names.get(kind, "Elemento")
        errors.actions.info("Eliminado con %s: página %d, %s %s%s", trigger, self.ed_current + 1, uid,
                            kind, f" «{element.get('text', '')[:60]}»" if kind == "text" else "")
        self.show_info(f"{what} eliminado (Ctrl+Z para deshacer).")

        def undo():
            if self.ed_undo and self.ed_undo[-1] is snapshot:
                self._undo()
            else:   # después hubo otros cambios: no se pisan
                self.show_info("Hubo otros cambios después: usa Ctrl+Z para deshacer paso a paso.")

        toasts = getattr(self.app, "toasts", None)
        if toasts is not None:
            self.ed_delete_toast = toasts.show("info", f"{what} eliminado", action=("Deshacer", undo),
                                               duration=5000)

    def _add(self, element, select=True):
        self._push_undo()
        element["id"] = f"N{self.ed_next_id}"
        self.ed_next_id += 1
        state = self._st()
        state["added"].append(element)
        if state["z"] is not None:
            state["z"].append(element["id"])
        if select:
            self._select(element["id"], render=False)
        self._after_change()
        return element

    def _add_text(self, x, y):
        style = self.ed_session.nearby_style(self.ed_current, x, y) or {
            "font": "Arial", "font_xref": 0, "size": 12.0, "color": "#1F2133", "bold": False,
            "italic": False, "orig_bold": False, "orig_italic": False, "serif": False,
            "mono": False, "family": "Arial", "requested_family": "Original", "dir": [1, 0],
            "align": "left", "line_height": 14.4}
        style["dir"] = [1, 0]
        size = float(style["size"])
        element = {"kind": "text", "level": "line", "text": "", "style": style,
                   "origin": [x, y + size * 0.8], "bbox": [x, y, x + size * 4, y + size * 1.2]}
        element = self._add(element)
        self._begin_edit(element["id"])

    def _add_image(self, x, y):
        path = filedialog.askopenfilename(filetypes=IMAGE_TYPES)
        if not path:
            return
        try:
            with Image.open(path) as im:
                w, h = im.size
        except Exception as exc:
            self.show_error(f"No se pudo abrir la imagen: {exc}")
            return
        width = min(220.0, w * 0.75)
        height = width * h / w
        self._add({"kind": "image", "path": path, "bbox": [x, y, x + width, y + height]})

    # ------------------------------------------------------------------ edición en el lugar

    def _tk_family(self, style):
        family = style.get("requested_family", "Original")
        if family == "Original":
            family = style.get("family", "Arial")
        return family if family in tkfont.families(self) else "Arial"

    def _sample_bg(self, box):
        if self.ed_image is None:
            return "#FFFFFF"
        s = self.ed_scale
        x = int(max(0, box[0] * s - 3))
        y = int(max(0, box[1] * s - 3))
        x = min(x, self.ed_image.width - 1)
        y = min(y, self.ed_image.height - 1)
        r, g, b = self.ed_image.getpixel((x, y))[:3]
        return f"#{r:02X}{g:02X}{b:02X}"

    def _display_family(self, style):
        """(familia, negrita, cursiva) de la fuente con que se va a guardar el texto (la
        familia completa instalada o de la biblioteca, o la incrustada), registrada solo
        para esta app; None si no se puede."""
        if self.ed_session is None or style.get("requested_family", "Original") != "Original":
            return None
        found = self.ed_session.display_font(style)
        if not found:
            return None
        path, family, bold, italic = found
        from ui.fonts import register_font_file

        if register_font_file(path) and family in tkfont.families(self):
            return family, bold, italic
        return None

    def _tk_font(self, style, size_px, bold=None, italic=None):
        shown = self._display_family(style)
        if shown:
            family, bold, italic = shown
        else:
            family = self._tk_family(style)
            bold = style.get("bold") if bold is None else bold
            italic = style.get("italic") if italic is None else italic
        return tkfont.Font(family=family, size=-size_px, weight="bold" if bold else "normal",
                           slant="italic" if italic else "roman")

    def _format_spans(self, widget, uid, element, scale):
        """Cada fragmento con su fuente, peso, inclinación y color (como queda al guardar)."""
        lines = [element] if element.get("spans") else [
            line for line in (self._model() or {}).get("lines", [])
            if line["id"] in element.get("line_ids", [])]
        if not lines or any(not line.get("spans") for line in lines):
            return
        shown = widget.get("1.0", "end-1c")
        offsets, position = [], 0
        for line in lines:       # cada línea original dentro del texto que se muestra
            position = shown.find(line["text"], position)
            if position < 0:
                return   # ya se editó: el reparto entre fragmentos cambió
            offsets.append(position)
            position += len(line["text"])
        for row, line in enumerate(lines):
            col = 0
            for n, span in enumerate(line["spans"]):
                length = min(len(span["text"]), len(line["text"]) - col)
                if length <= 0:
                    break
                flags = span.get("flags", 0)
                bold = bool(flags & 16) or "bold" in span["font"].lower()
                italic = bool(flags & 2) or any(k in span["font"].lower() for k in ("italic", "oblique"))
                style = {"font_objnum": span.get("font_objnum"), "requested_family": "Original",
                         "family": pdf_editor.windows_family_for(span["font"], flags)}
                font = self._tk_font(style, max(6, round(float(span["size"]) * scale)), bold, italic)
                tag = f"span_{row}_{n}"
                widget.tag_configure(tag, font=font, foreground=span["color"])
                widget.tag_add(tag, f"1.0+{offsets[row] + col}c", f"1.0+{offsets[row] + col + length}c")
                widget._fonts = getattr(widget, "_fonts", []) + [font]
                col += length

    def _edit_room(self, uid, element, origin):
        """Hasta dónde puede crecer el texto: {right, rest_x, align} en puntos."""
        room = None
        if not uid.startswith("N"):
            ov = self._st()["overrides"].get(uid, {})
            try:
                room = self.ed_session.edit_geometry(self.ed_current, uid, ov)
            except Exception:
                errors.record("No se pudo calcular el ancho del párrafo")
            if room is not None:
                room["right"] += ov.get("dx", 0)
                room["rest_x"] += ov.get("dx", 0)
        if room is None:
            width = self._model()["width"]
            box = element.get("box")
            room = {"right": box[1] if box else width - max(36.0, min(72.0, origin[0])), "rest_x": origin[0],
                    "align": element["style"].get("align", "left")}
        return room

    def _begin_edit(self, uid, click=None, caret=None):
        if self.ed_editor or self.ed_reflowing:
            return
        self._prepare(uid)
        element = self._element(uid)
        if element is None or element["kind"] != "text":
            return
        if uid.startswith("L") and self.ed_level == "block":
            return
        style = element["style"]
        s = self.ed_scale
        size_px = max(6, round(float(style["size"]) * s))
        font = self._tk_font(style, size_px)
        box = self._box(uid) or element["bbox"]
        is_block = element.get("level") == "block"
        origin = element["lines"][0]["origin"] if element.get("lines") else element["origin"]
        # sobre el texto: mismo x de inicio (el origen del primer glifo) y misma línea base
        left = MARGIN + origin[0] * s
        top = MARGIN + origin[1] * s - font.metrics("ascent")
        line_px = float(style.get("line_height") or style["size"] * 1.2) * s
        shown = element["text"]
        if is_block and "text" not in self._st()["overrides"].get(uid, {}):
            # un párrafo se edita corrido: los saltos de renglón del PDF no son parte del texto
            shown = self.ed_session.flow_text(self.ed_current, uid) or shown
        room = self._edit_room(uid, element, origin)
        base_x = min(origin[0], room["rest_x"])
        left = MARGIN + base_x * s
        # Tk solo usa tamaños enteros y mide un poco más ancho que el PDF: sin esta corrección, una
        # línea llena se vería partida en dos con solo abrirla
        widths = [font.measure(l["text"]) / max(1.0, (l["bbox"][2] - l["bbox"][0]) * s)
                  for l in element.get("lines", []) if l.get("text")]
        ratio = min(1.08, max([1.0] + widths))
        block_width = max(20, (room["right"] - base_x) * s * ratio) + 4   # + el cursor al final
        widget = tk.Text(self.ed_canvas, wrap="word", font=font,
                         fg=style["color"], bg=self._sample_bg(box), insertbackground=style["color"],
                         relief="flat", bd=0, highlightthickness=0, padx=0, pady=0, undo=True,
                         spacing1=0, spacing3=0,
                         spacing2=max(0, int(line_px - font.metrics("linespace"))))

        def fit(_event=None, settled=True):
            """El cuadro crece con el texto: a lo ancho hasta el borde del párrafo y, de ahí en
            adelante, hacia abajo, como quedará al confirmar. Al abrirlo (`settled` falso) no se
            fuerza a dibujarse: el foco se le da al mostrarse, después del aviso de activación de
            la ventana; dibujado antes, ese aviso le devolvería el foco al lienzo."""
            if not widget.winfo_exists():
                return
            rows = widget.get("1.0", "end-1c").split("\n")
            indent = (origin[0] - base_x) * s
            width = block_width if is_block else min(block_width, max(font.measure(r) for r in rows) + indent + 4)
            self.ed_canvas.itemconfigure("editor", width=max(8, width))
            if settled:
                widget.update_idletasks()
            counted = widget.count("1.0", "end", "displaylines")
            count = max(len(rows), counted[0] if counted else 1)
            height = font.metrics("linespace") + (count - 1) * line_px + 2
            self.ed_canvas.itemconfigure("editor", height=height)
            self.ed_editor_geom = [left, top, left + max(8, width), top + height]
            self._draw_overlay()
        widget.insert("1.0", shown)
        self._format_spans(widget, uid, element, s)
        widget.tag_configure("all", justify={"center": "center", "right": "right"}.get(room["align"], "left"),
                             lmargin1=(origin[0] - base_x) * s, lmargin2=(room["rest_x"] - base_x) * s)
        widget.tag_add("all", "1.0", "end")
        self.ed_canvas.create_window(left, top, anchor="nw", window=widget, tags="editor")
        self.ed_editor = (widget, uid, font)
        self.ed_editor_shown = shown     # el texto con que se abrió (para saber si cambió)
        fit(settled=False)
        widget.after_idle(fit)
        widget.bind("<KeyRelease>", fit, add="+")
        widget.bind("<<Paste>>", lambda _e: widget.after_idle(fit), add="+")
        widget.focus_force()
        widget.mark_set("insert", "end-1c")
        if click is not None and caret is None:   # cursor donde se hizo clic (cuando el texto ya está dibujado)
            cx = self.ed_canvas.canvasx(click[0]) - left
            cy = self.ed_canvas.canvasy(click[1]) - top

            def place_cursor():
                if widget.winfo_exists():
                    widget.update_idletasks()   # que el cuadro ya tenga su tamaño y su texto acomodado
                    widget.mark_set("insert", widget.index(f"@{int(cx)},{int(cy)}"))

            widget.after(30, place_cursor)
        if not is_block:
            widget.bind("<Return>", lambda _e: (self._commit_edit(), "break")[1])
        widget.bind("<Control-Return>", lambda _e: (self._commit_edit(), "break")[1])
        for sequence, sign, step in (("<Alt-Left>", -1, 1), ("<Alt-Right>", 1, 1),
                                     ("<Alt-Shift-Left>", -1, 10), ("<Alt-Shift-Right>", 1, 10)):
            widget.bind(sequence, lambda _e, a=sign, b=step: (self._commit_edit(), self._nudge_width(a, b))[1])
        widget.bind("<Escape>", lambda _e: (self._commit_edit(cancel=True), "break")[1])
        self.show_info("Escribe el texto nuevo · Enter (o Ctrl+Enter en párrafos) confirma · Esc cancela.")

    def _commit_edit(self, cancel=False):
        if not self.ed_editor:
            return
        widget, uid, _font = self.ed_editor
        shown = self.ed_editor_shown
        self.ed_editor = None
        self.ed_editor_geom = None
        text = widget.get("1.0", "end-1c")
        self.ed_canvas.delete("editor")
        widget.destroy()
        element = self._element(uid)
        if cancel or element is None:
            if element is not None and uid.startswith("N") and not element["text"]:
                self._st()["added"] = [a for a in self._st()["added"] if a["id"] != uid]
                self._select(None)
            self._draw_overlay()
            return
        if text == shown:
            if uid.startswith("N") and not text:
                self._st()["added"] = [a for a in self._st()["added"] if a["id"] != uid]
                self._select(None)
            self._draw_overlay()
            return
        if not text.strip() and element["text"].strip():   # Enter con el texto vacío
            self._draw_overlay()
            self._confirm("Eliminar texto", "¿Eliminar este texto?", "Eliminar", "Cancelar",
                          lambda: self._delete_text(uid),
                          lambda: self.show_info("No se cambió el texto."))
            return
        self._push_undo()
        if uid.startswith("N"):
            if not text.strip():
                self._st()["added"] = [a for a in self._st()["added"] if a["id"] != uid]
                self._select(None, render=False)
            else:
                self.ed_verify.setdefault((self.ed_current, uid), element["text"])
                element["text"] = text
        else:
            overrides = self._st()["overrides"]
            self.ed_verify.setdefault((self.ed_current, uid), copy.deepcopy(overrides.get(uid)))
            override = overrides.setdefault(uid, {})
            override["text"] = text
            override.pop("font_ok", None)    # texto nuevo: hay que volver a confirmar
            if self._needs_reflow(uid, override):
                self.ed_verify.pop((self.ed_current, uid), None)  # se comprueba al integrarla
                self._start_reflow(uid)
                return
        self._finish_commit(uid)

    def _finish_commit(self, uid):
        self.ed_layout.pop(uid, None)
        self._after_change()
        self.show_info("Cambio aplicado (Ctrl+Z para deshacer).")
        if self.ed_selected:
            self._refresh_font_note(self.ed_selected)
        if not uid.startswith("N"):
            self._check_substitution(uid)

    # ------------------------------------------------------------------ el texto ya no cabe

    def _needs_reflow(self, uid, override):
        try:
            return self.ed_session.needs_reflow(self.ed_current, uid, override, self.ed_push, self.ed_adjust)
        except Exception:
            errors.record("No se pudo calcular cómo acomodar el texto")
            return False

    def _start_reflow(self, uid):
        """Integra la edición al documento en segundo plano: el texto ocupa otra cantidad de
        renglones y hay que mover contenido. Deshacer la revierte entera, de un solo paso."""
        index, session = self.ed_current, self.ed_session
        state = copy.deepcopy(self._st())
        push, adjust = self.ed_push, self.ed_adjust
        self.ed_reflowing = True
        self.show_info("Acomodando el texto…")

        def done(result):
            self.ed_reflowing = False
            if session is not self.ed_session:
                return
            if result.status == "done":
                self._adopt_reflow(index, result)
            elif result.status == "overflow":
                self._ask_overflow(index, result)
            elif result.status == "pending":
                self._ask_substitution(result.pending, when="reflow")
            else:
                self._finish_commit(uid)

        def failed(exc):
            self.ed_reflowing = False
            if session is self.ed_session:
                self._reflow_failed(index, uid, exc)

        run_task(self, lambda _p: session.reflow(index, state, uid, push, adjust), done, failed)

    def _reflow_failed(self, index, uid, exc):
        """No quedó bien: vuelve todo como estaba y se dice por qué (nunca en silencio)."""
        reason = errors.reason(exc)
        errors.log.error("Edición deshecha en la página %d (%s): %s", index + 1, uid, reason)
        if self.ed_undo:
            self._restore(self.ed_undo.pop())
        self.show_error(f"No se pudo aplicar la edición: {reason}")

    def _adopt_reflow(self, index, result):
        """El documento base pasa a ser el resultado; las páginas nuevas van después de `index`."""
        added = result.added_pages

        def moved(i):
            return i + added if i > index else i

        self.ed_session.adopt(result.data)
        self.ed_states = {moved(i): state for i, state in self.ed_states.items() if i != index}
        self.ed_rotations = {moved(i): r for i, r in self.ed_rotations.items()}
        position = self.ed_order.index(index) if index in self.ed_order else len(self.ed_order)
        order = [moved(i) for i in self.ed_order]
        order[position + 1:position + 1] = list(range(index + 1, index + 1 + added))
        self.ed_order = order
        self.ed_models.clear()
        self.ed_thumbs.clear()
        self.ed_thumbs_loading.clear()
        self.ed_layout = {}
        self.ed_verify.clear()
        self._select(None, render=False)
        self._draw_thumbs()
        self._show_page()
        self.show_info(f"{result.message} (Ctrl+Z para deshacer)")

    def _ask_overflow(self, index, result):
        """El texto ya no cabe en la página: se elige qué hacer viendo cómo quedaría cada opción."""
        if self.ed_overflow_dialog is not None:
            return
        root = self.winfo_toplevel()
        dialog = ctk.CTkToplevel(root, fg_color=theme.BG_PRIMARY)
        dialog.title("El texto no cabe en la página")
        dialog.resizable(False, False)
        dialog.transient(root)
        self.ed_overflow_dialog = dialog
        lines = result.extra_lines
        word = "línea más" if lines == 1 else "líneas más"
        ctk.CTkLabel(dialog, text=f"El texto ocupa {lines} {word} y lo de abajo ya no cabe en esta página.",
                     font=theme.FONT_BODY, text_color=theme.TEXT_PRIMARY, wraplength=640,
                     justify="left").pack(padx=24, pady=(20, 10), anchor="w")
        body = ctk.CTkFrame(dialog, fg_color="transparent")
        body.pack(fill="x", padx=24)
        options = ctk.CTkFrame(body, fg_color="transparent")
        options.pack(side="left", anchor="n", padx=(0, 16))
        preview = ctk.CTkFrame(body, **theme.CARD)
        preview.pack(side="left", anchor="n")
        choice = tk.StringVar(value="newpage")
        note = ctk.CTkLabel(options, text="", font=theme.FONT_SMALL, text_color=theme.TEXT_SECONDARY,
                            wraplength=300, justify="left")
        dialog.ed_photos = {}
        dialog.ed_preview_labels = []

        def show(*_args):
            for label in dialog.ed_preview_labels:
                label.destroy()
            dialog.ed_preview_labels = []
            candidate = result.candidates[choice.get()]
            note.configure(text=candidate.message)
            for image in candidate.previews[:2]:
                photo = ImageTk.PhotoImage(image)
                dialog.ed_photos.setdefault(choice.get(), []).append(photo)
                label = tk.Label(preview, image=photo, bd=1, relief="solid")
                label.pack(side="left", padx=6, pady=6)
                dialog.ed_preview_labels.append(label)

        def finish(apply):
            if self.ed_overflow_dialog is None:
                return
            self.ed_overflow_dialog = None
            selected = choice.get()
            dialog.grab_release()
            dialog.destroy()
            if apply:
                self._adopt_reflow(index, result.candidates[selected])
            else:
                if self.ed_undo:
                    self._restore(self.ed_undo.pop())
                self.show_info("No se cambió nada: el texto queda como estaba.")

        dialog.ed_radios = {}
        for key, label in (("newpage", "Pasar el sobrante a una página nueva"),
                           ("shrink", "Reducir un poco el tamaño de letra del párrafo"),
                           ("tighten", "Reducir el interlineado del párrafo")):
            available = key in result.candidates
            radio = ctk.CTkRadioButton(options, text=label if available else f"{label} (no alcanza)",
                                       variable=choice, value=key, command=show,
                                       state="normal" if available else "disabled",
                                       font=theme.FONT_BODY, text_color=theme.TEXT_PRIMARY,
                                       fg_color=theme.ACCENT, hover_color=theme.ACCENT)
            radio.pack(anchor="w", pady=4)
            dialog.ed_radios[key] = radio
        note.pack(anchor="w", pady=(10, 0))
        row = ctk.CTkFrame(dialog, fg_color="transparent")
        row.pack(fill="x", padx=24, pady=(14, 20))
        dialog.ed_choice = choice
        dialog.ed_buttons = {
            "apply": ctk.CTkButton(row, text="Aplicar", command=lambda: finish(True), **theme.BTN_PRIMARY),
            "cancel": ctk.CTkButton(row, text="Cancelar", command=lambda: finish(False), **theme.BTN_SECONDARY),
        }
        dialog.ed_buttons["apply"].pack(side="left", padx=(0, 8))
        dialog.ed_buttons["cancel"].pack(side="left")
        dialog.bind("<Escape>", lambda _e: finish(False))
        dialog.protocol("WM_DELETE_WINDOW", lambda: finish(False))
        show()
        dialog.update_idletasks()
        x = root.winfo_rootx() + (root.winfo_width() - dialog.winfo_reqwidth()) // 2
        y = root.winfo_rooty() + (root.winfo_height() - dialog.winfo_reqheight()) // 3
        dialog.geometry(f"+{max(0, x)}+{max(0, y)}")
        dialog.after(260, lambda: dialog.winfo_exists() and dialog.iconbitmap(str(asset_path("icon.ico"))))
        dialog.after(10, lambda: dialog.winfo_exists() and dialog.grab_set())

    def _delete_text(self, uid):
        """Quita un texto (confirmado por el usuario al dejarlo vacío)."""
        if self._element(uid) is None:
            return
        self._select(uid, render=False)
        self._delete_selected("Enter con el texto vacío")

    def _confirm(self, title, message, yes, no, on_yes, on_no=None):
        """Pregunta modal con dos botones (el de `yes` es el principal)."""
        if self.ed_confirm_dialog is not None:
            return
        root = self.winfo_toplevel()
        dialog = ctk.CTkToplevel(root, fg_color=theme.BG_PRIMARY)
        dialog.title(title)
        dialog.resizable(False, False)
        dialog.transient(root)
        self.ed_confirm_dialog = dialog
        dialog.ed_message = message
        ctk.CTkLabel(dialog, text=message, font=theme.FONT_BODY, text_color=theme.TEXT_PRIMARY,
                     wraplength=420, justify="left").pack(padx=24, pady=(22, 16), anchor="w")
        row = ctk.CTkFrame(dialog, fg_color="transparent")
        row.pack(fill="x", padx=24, pady=(0, 20))

        def choose(accepted):
            if self.ed_confirm_dialog is None:
                return
            self.ed_confirm_dialog = None
            dialog.grab_release()
            dialog.destroy()
            action = on_yes if accepted else on_no
            if action:
                action()

        dialog.ed_buttons = {
            "yes": ctk.CTkButton(row, text=yes, command=lambda: choose(True), **theme.BTN_PRIMARY),
            "no": ctk.CTkButton(row, text=no, command=lambda: choose(False), **theme.BTN_SECONDARY),
        }
        dialog.ed_buttons["yes"].pack(side="left", padx=(0, 8))
        dialog.ed_buttons["no"].pack(side="left")
        dialog.bind("<Return>", lambda _e: choose(True))
        dialog.bind("<Escape>", lambda _e: choose(False))
        dialog.protocol("WM_DELETE_WINDOW", lambda: choose(False))
        dialog.update_idletasks()
        x = root.winfo_rootx() + (root.winfo_width() - dialog.winfo_reqwidth()) // 2
        y = root.winfo_rooty() + (root.winfo_height() - dialog.winfo_reqheight()) // 3
        dialog.geometry(f"+{max(0, x)}+{max(0, y)}")
        dialog.after(260, lambda: dialog.winfo_exists() and dialog.iconbitmap(
            str(asset_path("icon.ico"))))
        dialog.after(10, lambda: dialog.winfo_exists() and (dialog.grab_set(),
                                                              dialog.ed_buttons["yes"].focus_set()))

    def _check_substitution(self, uid):
        index, session = self.ed_current, self.ed_session
        state = copy.deepcopy(self._st())

        def done(items):
            mine = [item for item in items if item["uid"] == uid]
            if mine and self.ed_session is session and self.ed_current == index:
                self._ask_substitution(mine, when="edit")

        run_task(self, lambda _p: session.pending_substitutions(index, state), done, lambda _e: None)

    # ------------------------------------------------------------------ nivel línea / párrafo

    def _set_level(self, level):
        self._commit_edit()
        if level == self.ed_level:
            return
        self.ed_level = level
        uid = self.ed_selected
        if level == "block" and uid and uid.startswith("L"):
            line = next(l for l in self._model()["lines"] if l["id"] == uid)
            self._enter_block_mode(line["block_id"])
            self._select(line["block_id"])
        elif level == "line" and uid and uid.startswith("B") and not self._block_mode(uid):
            self._select(None)
        self._draw_overlay()
        self.show_info("Editando párrafos completos: el texto se acomoda al ancho original."
                       if level == "block" else "Editando línea por línea.")

    def _enter_block_mode(self, block_id, push=True):
        """Pasa un bloque a edición por párrafo, conservando lo ya editado en sus líneas."""
        state = self._st()
        if block_id in state["overrides"]:
            return
        model = self._model()
        block = next(b for b in model["blocks"] if b["id"] == block_id)
        texts, changed = [], False
        for line_id in block["line_ids"]:
            ov = state["overrides"].get(line_id, {})
            if ov:
                changed = True
            if not ov.get("deleted"):
                line = next(l for l in model["lines"] if l["id"] == line_id)
                texts.append(ov.get("text", line["text"]))
        if push:
            self._push_undo()
        for line_id in block["line_ids"]:
            state["overrides"].pop(line_id, None)
        state["overrides"][block_id] = {"text": "\n".join(texts)} if changed else {}
        if state["z"]:
            positions = [state["z"].index(l) for l in block["line_ids"] if l in state["z"]]
            state["z"] = [u for u in state["z"] if u not in block["line_ids"]]
            state["z"].insert(min(positions) if positions else len(state["z"]), block_id)
        self._after_change()

    # ------------------------------------------------------------------ estilo

    def _style_of(self, uid):
        element = self._element(uid)
        return element["style"] if element and element["kind"] == "text" else None

    def _set_style(self, key, value):
        uid = self.ed_selected
        if not uid or self._style_of(uid) is None:
            return
        self._commit_edit()
        if uid.startswith("L") and self.ed_level == "block":
            return
        self._prepare(uid)
        self._push_undo()
        if uid.startswith("N"):
            self._element(uid)["style"][key] = value
        else:
            ov = self._st()["overrides"].setdefault(uid, {})
            ov.setdefault("style", {})[key] = value
        self.ed_layout.pop(uid, None)
        self._after_change()
        self._refresh_font_note(uid)

    def _refresh_font_note(self, uid):
        element = self._element(uid)
        if not element or element["kind"] != "text" or self.ed_session is None:
            return
        style, text = copy.deepcopy(element["style"]), element["text"] or "A"
        modified = uid.startswith("N") or bool(self._st()["overrides"].get(uid, {}).get("text")
                                               or self._st()["overrides"].get(uid, {}).get("style"))

        index, session = self.ed_current, self.ed_session
        original = not uid.startswith("N")

        def note_for(_p):
            if original:   # fuente y tamaño reales del PDF (y qué pasa si faltan caracteres)
                return session.font_note(index, uid, element["text"])
            return session.font_report(style, text) or ("Fuente: " + style.get("font", "?")
                                                        if not modified else "")

        def done(note):
            if self.ed_selected == uid:
                self.ed_font_note = note or ""
                warn = "sustituida" in self.ed_font_note or "no incluye" in self.ed_font_note
                if hasattr(self, "ed_note_label") and self.ed_note_label.winfo_exists():
                    self.ed_note_label.configure(
                        text=self.ed_font_note,
                        text_color=theme.WARNING if warn else theme.TEXT_SECONDARY)

        run_task(self, note_for, done, lambda _e: None)

    # ------------------------------------------------------------------ barra de propiedades

    def _build_props(self):
        self.ed_props_for = (self.ed_selected, self.ed_tool)
        for child in self.ed_props.winfo_children():
            child.destroy()
        p = ctk.CTkFrame(self.ed_props, fg_color="transparent")
        p.pack(fill="x")
        self.ed_props_row = p
        uid = self.ed_selected
        if self.ed_tool == "highlight":
            self._prop_label("Color")
            self._swatches(HIGHLIGHT_COLORS, self.ed_highlight_color,
                           lambda c: setattr(self, "ed_highlight_color", c))
            return
        if self.ed_tool == "draw":
            self._prop_label("Color")
            self._swatches(DRAW_COLORS, self.ed_draw_color, lambda c: setattr(self, "ed_draw_color", c))
            self._prop_label("Grosor")
            menu = ctk.CTkOptionMenu(p, values=["1", "2", "3", "5", "8"], width=64, height=30,
                                     command=lambda v: setattr(self, "ed_stroke", float(v)),
                                     **theme.OPTIONMENU)
            menu.set(str(int(self.ed_stroke)))
            menu.pack(side="left", padx=(0, 12))
            return
        level = ctk.CTkSegmentedButton(p, values=["Línea", "Párrafo"], height=30,
                                       command=lambda v: self._set_level("line" if v == "Línea" else "block"),
                                       **theme.SEGMENTED)
        level.set("Línea" if self.ed_level == "line" else "Párrafo")
        level.pack(side="left", padx=(0, 12))
        if not uid:
            ctk.CTkLabel(p, text=TOOL_HINTS.get(self.ed_tool, ""), font=theme.FONT_SMALL,
                         text_color=theme.TEXT_SECONDARY).pack(side="left")
            return
        style = self._style_of(uid)
        if style is not None:
            fam = ctk.CTkOptionMenu(p, values=pdf_editor.FAMILY_CHOICES, width=150, height=30,
                                    command=lambda v: self._set_style("requested_family", v),
                                    **theme.OPTIONMENU)
            fam.set(style.get("requested_family", "Original"))
            fam.pack(side="left", padx=(0, 6))
            size = ctk.CTkEntry(p, width=52, height=30, **theme.ENTRY)
            size.insert(0, f"{float(style['size']):.2f}".rstrip("0").rstrip("."))
            size.pack(side="left", padx=(0, 6))
            theme.attach_focus_ring(size)

            def apply_size(_e=None, entry=size):
                try:
                    value = float(entry.get().replace(",", "."))
                except ValueError:
                    return
                if 2 <= value <= 400 and abs(value - float(style["size"])) > 0.01:
                    self._set_style("size", value)

            size.bind("<Return>", apply_size)
            size.bind("<FocusOut>", apply_size, add="+")
            color = ctk.CTkButton(p, text="", width=30, height=30, corner_radius=8,
                                  fg_color=style["color"], hover_color=style["color"],
                                  border_width=1, border_color=theme.BORDER_STRONG,
                                  command=lambda: self._pick_color(style["color"]))
            color.pack(side="left", padx=(0, 6))
            for key, name in (("bold", "bold"), ("italic", "italic")):
                active = bool(style.get(key))
                b = ctk.CTkButton(p, text="", image=icon(f"ui_{name}", 18), width=32, height=30,
                                  corner_radius=8, border_width=0,
                                  fg_color=theme.SELECTED_TINT if active else theme.BG_PRIMARY,
                                  hover_color=theme.BG_HOVER,
                                  command=lambda k=key, a=active: self._set_style(k, not a))
                b.pack(side="left", padx=1)
            ctk.CTkFrame(p, width=1, height=22, fg_color=theme.BORDER).pack(side="left", padx=6)
            for align in ("left", "center", "right", "justify"):
                active = style.get("align", "left") == align
                b = ctk.CTkButton(p, text="", image=icon(f"ui_align-{align}", 18), width=32, height=30,
                                  corner_radius=8, border_width=0,
                                  fg_color=theme.SELECTED_TINT if active else theme.BG_PRIMARY,
                                  hover_color=theme.BG_HOVER,
                                  command=lambda a=align: self._set_style("align", a))
                b.pack(side="left", padx=1)
        right = ctk.CTkFrame(p, fg_color="transparent")
        right.pack(side="right")
        for name, hint, command in (("front", "Traer al frente", lambda: self._layer("front")),
                                    ("up", "Subir", lambda: self._layer("up")),
                                    ("down", "Bajar", lambda: self._layer("down")),
                                    ("back", "Enviar atrás", lambda: self._layer("back")),
                                    ("trash", "Eliminar (Supr)",
                                     lambda: self._delete_selected("botón Eliminar de la barra"))):
            _icon_button(right, name, command, self, hint, width=32).pack(side="left", padx=1)
        if style is not None:
            # segunda línea: así la nota de fuente nunca queda recortada
            second = ctk.CTkFrame(self.ed_props, fg_color="transparent")
            second.pack(fill="x", pady=(4, 0))
            self.ed_flow_switches = {}
            for key, label, value in (("adjust", "Ajustar contenido de abajo", self.ed_adjust),
                                      ("push", "Desplazar contenido de abajo", self.ed_push)):
                switch = ctk.CTkCheckBox(second, text=label, width=20, height=18, checkbox_width=16,
                                         checkbox_height=16, command=lambda k=key: self._toggle_flow(k),
                                         **{**theme.CHECKBOX, "font": theme.FONT_SMALL})
                if value:
                    switch.select()
                switch.pack(side="right", padx=(10, 0))
                self.ed_flow_switches[key] = switch
            self.ed_note_label = ctk.CTkLabel(second, text=self.ed_font_note, anchor="w",
                                              font=theme.FONT_SMALL, text_color=theme.TEXT_SECONDARY)
            self.ed_note_label.pack(side="left", fill="x", expand=True)
        anim.enhance(self.ed_props)

    def _toggle_flow(self, key):
        value = bool(self.ed_flow_switches[key].get())
        if key == "push":
            self.ed_push = value
            self.show_info("El texto que crece desplaza el contenido de abajo." if value
                           else "El texto que crece ya no mueve el contenido de abajo.")
        else:
            self.ed_adjust = value
            self.show_info("Si el texto se acorta, el contenido de abajo sube." if value
                           else "Si el texto se acorta, el contenido de abajo se queda en su lugar.")

    def _prop_label(self, text):
        ctk.CTkLabel(self.ed_props_row, text=text, font=theme.FONT_SECTION,
                     text_color=theme.TEXT_SECONDARY).pack(side="left", padx=(0, 6))

    def _swatches(self, colors, current, on_pick):
        for color in colors:
            b = ctk.CTkButton(self.ed_props_row, text="", width=26, height=26, corner_radius=8,
                              fg_color=color, hover_color=color,
                              border_width=3 if color == current else 1,
                              border_color=theme.TEXT_PRIMARY if color == current else theme.BORDER_STRONG,
                              command=lambda c=color: (on_pick(c), self._build_props()))
            b.pack(side="left", padx=2)
        ctk.CTkFrame(self.ed_props_row, width=12, height=1, fg_color="transparent").pack(side="left")

    def _pick_color(self, current):
        chosen = colorchooser.askcolor(color=current, title="Color del texto")
        if chosen and chosen[1]:
            self._set_style("color", chosen[1].upper())

    # ------------------------------------------------------------------ capas

    def _ensure_z(self):
        state = self._st()
        default = self.ed_session.default_order(self._model(), state)
        if state["z"] is None:
            state["z"] = default
        else:
            state["z"] = [u for u in state["z"] if u in default] + [u for u in default if u not in state["z"]]
        return state["z"]

    def _layer(self, action):
        uid = self.ed_selected
        if not uid or not self._editable():
            return
        if uid.startswith("L") and self.ed_level == "block":
            return
        self._prepare(uid)
        self._push_undo()
        z = self._ensure_z()
        if uid not in z:  # línea dentro de un bloque en modo párrafo
            return
        i = z.index(uid)
        z.pop(i)
        if action == "front":
            z.append(uid)
        elif action == "back":
            z.insert(0, uid)
        elif action == "up":
            z.insert(min(len(z), i + 1), uid)
        else:
            z.insert(max(0, i - 1), uid)
        self._after_change()
        self.show_info({"front": "Traído al frente.", "back": "Enviado atrás.", "up": "Subido una capa.",
                        "down": "Bajado una capa."}[action])

    def _toggle_layers(self):
        self.ed_layers_visible = not self.ed_layers_visible
        if self.ed_layers_visible:
            self.ed_layers.grid(row=0, column=2, sticky="ns", padx=(12, 0))
            self._build_layers()
        else:
            self.ed_layers.grid_forget()

    def _layer_label(self, uid):
        element = self._element(uid)
        if element is None:
            return uid
        kind = element["kind"]
        if kind == "text":
            text = element["text"].replace("\n", " ").strip() or "(vacío)"
            return "T  " + (text[:22] + "…" if len(text) > 23 else text)
        return {"image": "Imagen", "highlight": "Resaltado", "draw": "Dibujo",
                "redact": "Redacción"}.get(kind, kind)

    def _build_layers(self):
        if not self.ed_layers_visible or self._model() is None:
            return
        for child in self.ed_layers.winfo_children():
            child.destroy()
        ctk.CTkLabel(self.ed_layers, text="Capas (arriba primero)", font=theme.FONT_SECTION,
                     text_color=theme.TEXT_SECONDARY).pack(anchor="w", padx=6, pady=(4, 6))
        state = self._st()
        order = state["z"] or self.ed_session.default_order(self._model(), state)
        self.ed_layer_rows = []
        for uid in reversed(order):
            if state["overrides"].get(uid, {}).get("deleted"):
                continue
            selected = uid == self.ed_selected
            row = ctk.CTkButton(self.ed_layers, text=self._layer_label(uid), anchor="w", height=30,
                                corner_radius=8, font=theme.FONT_SMALL,
                                fg_color=theme.SELECTED_TINT if selected else theme.BG_PRIMARY,
                                hover_color=theme.BG_HOVER, text_color=theme.TEXT_PRIMARY,
                                command=lambda u=uid: self._select(u))
            row.pack(fill="x", padx=4, pady=1)
            row.bind("<ButtonPress-1>", lambda e, u=uid: setattr(self, "ed_layer_drag", (u, e.y_root)), add="+")
            row.bind("<ButtonRelease-1>", lambda e, u=uid: self._layer_drop(u, e.y_root), add="+")
            self.ed_layer_rows.append((uid, row))

    def _layer_drop(self, uid, y_root):
        start = getattr(self, "ed_layer_drag", None)
        self.ed_layer_drag = None
        if not start or abs(y_root - start[1]) < 8:
            return
        target = None
        for other, row in self.ed_layer_rows:
            if row.winfo_rooty() <= y_root <= row.winfo_rooty() + row.winfo_height():
                target = other
        if not target or target == uid:
            return
        self._push_undo()
        z = self._ensure_z()
        if uid not in z or target not in z:
            return
        z.remove(uid)
        # la lista se muestra invertida: soltar "sobre" una fila = quedar justo encima
        z.insert(z.index(target) + 1 if y_root < start[1] else z.index(target), uid)
        self._after_change()

    # ------------------------------------------------------------------ páginas

    def _rotate(self, delta):
        if self.ed_session is None:
            return
        self._commit_edit()
        self._push_undo()
        self.ed_rotations[self.ed_current] = (self.ed_rotations.get(self.ed_current, 0) + delta) % 360
        self._select(None, render=False)
        self._draw_page() if self.ed_image is not None else None
        self._update_thumb(self.ed_current, self.ed_image)
        if self.ed_rotations[self.ed_current]:
            self.show_info("Página rotada. La rotación se aplica al guardar; vuelve a 0° para editar.")

    def _delete_page(self):
        if self.ed_session is None:
            return
        if len(self.ed_order) <= 1:
            self.show_error("No puedes eliminar la única página que queda.")
            return
        self._commit_edit()
        self._push_undo()
        position = self.ed_order.index(self.ed_current)
        self.ed_order.remove(self.ed_current)
        self.ed_current = self.ed_order[min(position, len(self.ed_order) - 1)]
        self._select(None, render=False)
        self._draw_thumbs()
        self._show_page()
        self.show_info("Página eliminada (Ctrl+Z para deshacer).")

    def _set_zoom(self, value):
        self.ed_zoom = value
        self._schedule_render(0)

    def _zoom_step(self, direction):
        steps = ZOOMS[1:]
        if self.ed_zoom == "Ajustar ancho":
            current = self._page_scale() / PX_PER_PT * 100
            index = min(range(len(steps)), key=lambda i: abs(int(steps[i].split()[0]) - current))
        else:
            index = steps.index(self.ed_zoom)
        index = max(0, min(len(steps) - 1, index + direction))
        self.ed_zoom = steps[index]
        self.ed_zoom_menu.set(self.ed_zoom)
        self._schedule_render(0)

    # ------------------------------------------------------------------ miniaturas

    def _load_visible_thumbs(self):
        """Genera en segundo plano las miniaturas de las páginas visibles (y una pantalla
        alrededor); el resto se genera al desplazar."""
        session = self.ed_session
        if session is None or self.ed_thumbs_loading:
            return
        c = self.ed_thumb_canvas
        height = max(1, c.winfo_height())
        top, bottom = c.canvasy(0) - height, c.canvasy(0) + 2 * height
        slots, _total = self._thumb_slots()
        missing = [index for index, y, h in slots
                   if y + h >= top and y <= bottom and index not in self.ed_thumbs]
        if not missing:
            return
        batch = missing[:THUMB_BATCH]
        self.ed_thumbs_loading.update(batch)

        def work(_progress):
            thumbs = {}
            for index in batch:
                image, _layout, _notes = session.render_page(index, None, THUMB_ZOOM)
                image.thumbnail((THUMB_W, THUMB_W * 2))
                thumbs[index] = image
            return thumbs

        def done(images):
            if session is not self.ed_session:
                return
            self.ed_thumbs_loading.difference_update(batch)
            for index, image in images.items():
                if index not in self.ed_thumbs:
                    self.ed_thumbs[index] = ImageTk.PhotoImage(image)
            self._draw_thumbs()

        def failed(_exc):
            self.ed_thumbs_loading.difference_update(batch)

        run_task(self, work, done, failed)

    def _schedule_thumb_load(self):
        if self.ed_thumb_job is None:
            self.ed_thumb_job = self.after_idle(self._run_thumb_load)

    def _run_thumb_load(self):
        self.ed_thumb_job = None
        self._load_visible_thumbs()

    def _update_thumb(self, index, image):
        if image is None:
            return
        thumb = image.copy()
        rotation = self.ed_rotations.get(index, 0) % 360
        if rotation:
            thumb = thumb.rotate(-rotation, expand=True)
        thumb.thumbnail((THUMB_W, THUMB_W * 2))
        self.ed_thumbs[index] = ImageTk.PhotoImage(thumb)
        self._draw_thumbs()

    def _thumb_slots(self):
        slots, y = [], 6
        for index in self.ed_order:
            photo = self.ed_thumbs.get(index)
            height = photo.height() if photo else self._thumb_height(index)
            slots.append((index, y, height))
            y += height + 30
        return slots, y

    def _thumb_height(self, index):
        """Alto que tendrá la miniatura (antes de generarla), según el tamaño de la página."""
        try:
            width, height = self.ed_session.page_size(index)
        except Exception:
            return int(THUMB_W * 1.41)
        width, height = width * THUMB_ZOOM, height * THUMB_ZOOM
        factor = min(1.0, THUMB_W / max(1.0, width), THUMB_W * 2 / max(1.0, height))
        return max(20, int(height * factor))

    def _draw_thumbs(self, marker=None):
        c = self.ed_thumb_canvas
        c.delete("all")
        r = theme.resolve
        accent, border = r(theme.ACCENT), r(theme.BORDER)
        slots, total = self._thumb_slots()
        for position, (index, y, height) in enumerate(slots):
            selected = index == self.ed_current
            x = 6
            c.create_rectangle(x - 3, y - 3, x + THUMB_W + 3, y + height + 3,
                               outline=accent if selected else border,
                               width=2 if selected else 1, fill=r(theme.SURFACE))
            photo = self.ed_thumbs.get(index)
            if photo:
                c.create_image(x + THUMB_W / 2, y, anchor="n", image=photo)
            c.create_text(x + THUMB_W / 2, y + height + 13, text=str(position + 1),
                          fill=accent if selected else r(theme.TEXT_SECONDARY), font=theme.FONT_SMALL)
        if marker is not None:
            y = slots[marker][1] - 12 if marker < len(slots) else total - 18
            c.create_line(4, y, THUMB_W + 10, y, fill=accent, width=3)
        c.configure(scrollregion=(0, 0, THUMB_W + 12, total))
        if self.ed_session is not None:
            self._schedule_thumb_load()

    def _highlight_thumb(self):
        self._draw_thumbs()

    def _thumb_position(self, event, insertion=False):
        y = self.ed_thumb_canvas.canvasy(event.y)
        slots, _total = self._thumb_slots()
        for position, (_index, top, height) in enumerate(slots):
            if y < top + height + 24:
                if insertion and y > top + height / 2:
                    return position + 1
                return position
        return len(slots) if insertion else None

    def _thumb_press(self, event):
        position = self._thumb_position(event)
        if position is None or self.ed_session is None:
            return
        self.ed_thumb_drag = {"from": position, "y": event.y, "active": False}

    def _thumb_motion(self, event):
        drag = self.ed_thumb_drag
        if not drag:
            return
        if abs(event.y - drag["y"]) > 8:
            drag["active"] = True
        if drag["active"]:
            self._draw_thumbs(marker=self._thumb_position(event, insertion=True))

    def _thumb_release(self, event):
        drag, self.ed_thumb_drag = self.ed_thumb_drag, None
        if not drag:
            return
        if not drag["active"]:
            self._commit_edit()
            self.ed_current = self.ed_order[drag["from"]]
            self._select(None, render=False)
            self.ed_image = None
            self._show_page()
            return
        target = self._thumb_position(event, insertion=True)
        source = drag["from"]
        if target is None or target in (source, source + 1):
            self._draw_thumbs()
            return
        self._push_undo()
        page = self.ed_order.pop(source)
        self.ed_order.insert(target - 1 if target > source else target, page)
        self._draw_thumbs()
        self.show_info("Orden de páginas actualizado.")

    # ------------------------------------------------------------------ guardar

    def run(self, allow_substitution=False):
        self._commit_edit()
        if self.ed_reflowing or self.ed_overflow_dialog is not None:   # primero termina esa edición
            if self.ed_reflowing:
                self.after(150, lambda: self.run(allow_substitution))
            return
        if self.ed_session is None:
            self.show_error("Abre un PDF primero.")
            return
        out_dir = self.get_output_dir()
        if not out_dir:
            return
        output = self.output_file(out_dir, f"{self.ed_src.stem}_editado.pdf")
        states = copy.deepcopy({k: v for k, v in self.ed_states.items() if k in self.ed_order})
        order, rotations, session = list(self.ed_order), dict(self.ed_rotations), self.ed_session

        def work(progress):
            try:
                return session.save(output, states, order, rotations, progress,
                                    allow_substitution=allow_substitution)
            except (pdf_editor.SubstitutionNeeded, pdf_editor.EditFailed) as exc:
                return exc

        def done(result):
            if isinstance(result, pdf_editor.EditFailed):   # no se guarda nada a medias
                errors.record("No se guardó: una edición no quedó aplicada", result)
                self._task_failed(RuntimeError(f"No se pudo aplicar la edición: {result}. "
                                               "No se guardó el archivo."))
                return
            if isinstance(result, pdf_editor.SubstitutionNeeded):
                self._running = False
                self.run_button.configure(state="normal", text=self.run_label)
                self.set_progress(0)
                self.show_info("Falta confirmar qué fuente usar.")
                self._ask_substitution(result.items)
                return
            path, notes = result
            self._task_done(f"Se guardó {path.name} en {out_dir}", toast=not notes)
            if notes:
                extra = f" (y {len(notes) - 2} más)" if len(notes) > 2 else ""
                self.show_warning(f"Se guardó {path.name}. " + " · ".join(notes[:2]) + extra)

        self.start_task(work, on_success=done)

    # ------------------------------------------------------------------ sustitución de fuentes

    def _ask_substitution(self, items, when="save"):
        """La fuente original no tiene algún carácter y su familia no está instalada ni en la
        biblioteca: usar una parecida (con su nombre y una muestra), volver a editar o cancelar.
        `when`: "edit" (al confirmar una edición) o "save" (al guardar)."""
        if getattr(self, "ed_subst_dialog", None) is not None:
            return
        first = items[0]
        missing = " ".join(first["missing"]) or "?"
        message = (f"La fuente original ({first['font']}) no incluye estos caracteres: {missing}. "
                   "¿Qué quieres hacer?")
        if len(items) > 1:
            message += f"\n(Hay {len(items) - 1} texto(s) más con el mismo problema.)"
        root = self.winfo_toplevel()
        dialog = ctk.CTkToplevel(root, fg_color=theme.BG_PRIMARY)
        dialog.title("Fuente no disponible")
        dialog.resizable(False, False)
        dialog.transient(root)
        self.ed_subst_dialog = dialog
        ctk.CTkLabel(dialog, text=message, font=theme.FONT_BODY, text_color=theme.TEXT_PRIMARY,
                     wraplength=520, justify="left").pack(padx=24, pady=(22, 10), anchor="w")
        sample = (first.get("text") or "").split("\n")[0][:60] or "Texto de ejemplo"
        previews = ctk.CTkFrame(dialog, **theme.CARD)
        previews.pack(fill="x", padx=24, pady=(0, 16))
        dialog._fonts = []
        for label, display in ((f"Original ({first['font']})", first.get("original_display")),
                               (f"Parecida ({first['substitute']})", first.get("substitute_display"))):
            ctk.CTkLabel(previews, text=label, font=theme.FONT_SECTION, text_color=theme.TEXT_MUTED,
                         anchor="w").pack(fill="x", padx=14, pady=(10, 0))
            font = self._preview_font(display)
            dialog._fonts.append(font)
            tk.Label(previews, text=sample, font=font, anchor="w", bg="#FFFFFF", fg="#111111",
                     padx=8, pady=4).pack(fill="x", padx=14, pady=(2, 4))
        ctk.CTkFrame(previews, height=6, fg_color="transparent").pack()
        row = ctk.CTkFrame(dialog, fg_color="transparent")
        row.pack(fill="x", padx=24, pady=(0, 20))

        def choose(action):
            dialog.grab_release()
            dialog.destroy()
            self.ed_subst_dialog = None
            if action == "substitute":
                for item in items:   # se acepta esa sustituta para esos textos
                    state = self.ed_states.get(item["page"], {})
                    state.setdefault("overrides", {}).setdefault(item["uid"], {})["font_ok"] = \
                        item["substitute"]
                if when == "save":
                    self.run()
                elif when == "reflow":
                    self._start_reflow(first["uid"])
                else:
                    self._after_change()
                    self.show_info(f"Se usará {first['substitute']} para las letras que faltan.")
            elif action == "edit":
                self._edit_problem(first)
            elif when in ("edit", "reflow"):   # se descarta la edición: el texto vuelve a ser el original
                if self.ed_undo:
                    self._restore(self.ed_undo.pop())
                self.show_info("Se descartó el cambio: la fuente original no tiene esas letras.")
            else:
                self.show_info("No se guardó. La fuente original no tiene todos los caracteres.")

        dialog.ed_buttons = {
            "substitute": ctk.CTkButton(row, text=f"Usar fuente similar ({first['substitute']})",
                                        command=lambda: choose("substitute"), **theme.BTN_PRIMARY),
            "edit": ctk.CTkButton(row, text="Editar el texto", command=lambda: choose("edit"),
                                  **theme.BTN_SECONDARY),
            "cancel": ctk.CTkButton(row, text="Cancelar", command=lambda: choose("cancel"),
                                    **theme.BTN_SECONDARY),
        }
        for key in ("substitute", "edit", "cancel"):
            dialog.ed_buttons[key].pack(side="left", padx=(0, 8))
        dialog.protocol("WM_DELETE_WINDOW", lambda: choose("cancel"))
        dialog.update_idletasks()
        x = root.winfo_rootx() + (root.winfo_width() - dialog.winfo_reqwidth()) // 2
        y = root.winfo_rooty() + (root.winfo_height() - dialog.winfo_reqheight()) // 3
        dialog.geometry(f"+{max(0, x)}+{max(0, y)}")
        dialog.after(260, lambda: dialog.winfo_exists() and dialog.iconbitmap(
            str(asset_path("icon.ico"))))
        dialog.after(10, lambda: dialog.winfo_exists() and dialog.grab_set())

    def _preview_font(self, display, size=20):
        """Fuente para la muestra del diálogo (registrada solo para esta app)."""
        if display:
            path, family, bold, italic = display
            from ui.fonts import register_font_file

            if register_font_file(path) and family in tkfont.families(self):
                return tkfont.Font(family=family, size=-size, weight="bold" if bold else "normal",
                                   slant="italic" if italic else "roman")
        return tkfont.Font(family="Arial", size=-size)

    def _edit_problem(self, item):
        """Lleva al texto que no entra en la fuente original y lo abre para editar."""
        page, uid = item["page"], item["uid"]

        def open_editor(tries=0):
            if self.ed_current == page and self.ed_image is not None and self._element(uid):
                self._select(uid)
                self._begin_edit(uid)
            elif tries < 50:
                self.after(100, lambda: open_editor(tries + 1))

        if self.ed_current != page and page in self.ed_order:
            self._commit_edit()
            self.ed_current = page
            self._select(None, render=False)
            self.ed_image = None
            self._show_page()
        open_editor()
