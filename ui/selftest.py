"""Autoprueba del editor dentro del programa instalado (también funciona desde el código).

    Pliegue.exe --autotest <reporte.json> <pdf>[::texto de la línea] [<pdf> ...]

Parte de la lista de verificación de cada versión: se corre contra el exe compilado.
Para cada PDF (sobre una copia; el original no se toca):
  * núcleo: cambia la última palabra de la línea, guarda y vuelve a extraer el texto;
  * interfaz: abre la línea en el editor real, escribe y confirma con Enter, comprueba
    que la vista previa cambió y tiene texto, guarda y vuelve a extraer texto y fuente;
  * falla forzada al reinsertar el texto: el original queda intacto y se avisa;
  * reinserción que no dibuja nada: la verificación lo detecta y deja el original;
  * Enter con el texto vacío: pregunta «¿Eliminar este texto?»;
  * con clics y teclas reales del sistema: doble clic recién abierto el PDF y con la ventana
    sin foco (abre el editor al primer intento), escribir y Retroceso sobre un texto elegido
    (abren el editor; nunca se borra el elemento), Supr con aviso «Deshacer» que lo restaura
    igual, y Supr sobre un elemento agregado.
Todo lo que falle (incluidas las excepciones de hilos y de Tk) queda en el reporte.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

NEW_WORD = "Prueba"
_PROGRESS: list = []        # archivo de avance (<reporte>.log), por si algo se cuelga


def _say(message: str) -> None:
    if _PROGRESS:
        with open(_PROGRESS[0], "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%H:%M:%S')} {message}\n")


FORCED = "falla forzada de la autoprueba"


def _family(name: str) -> str:
    from core import font_finder

    return font_finder.family_key(font_finder.parse_name(name)[0])


def _line_fonts(path: Path, y: float) -> tuple[str, set]:
    import fitz

    text, fonts = "", set()
    with fitz.open(path) as doc:
        for block in doc[0].get_text("rawdict")["blocks"]:
            for line in block.get("lines", []):
                for span in line["spans"]:
                    for ch in span["chars"]:
                        if abs(ch["origin"][1] - y) < 1.0:
                            text += ch["c"]
                            if ch["c"].strip():
                                fonts.add(span["font"])
    return text, fonts


def _pick_line(model, needle="CARRERA"):
    lines = [l for l in model["lines"] if len(l["text"].split()) >= 2]
    wanted = [l for l in lines if needle and needle in l["text"]]
    return (wanted or sorted(lines, key=lambda l: -len(l["text"])))[0]


def _new_text(line) -> str:
    words = line["text"].split(" ")
    words[-1] = NEW_WORD
    return " ".join(words)


def _dark(image) -> int:
    return sum(1 for p in image.convert("L").tobytes() if p < 128)


def _core_check(pdf: Path, out_dir: Path, needle: str) -> dict:
    from core import pdf_editor

    result = {"pdf": pdf.name, "modo": "núcleo"}
    session = pdf_editor.EditorSession(pdf)
    try:
        line = _pick_line(session.analyze(0), needle)
        span = line["spans"][0]
        new = _new_text(line)
        state = {"overrides": {line["id"]: {"text": new}}, "added": [], "z": None}
        result["pendientes"] = session.pending_substitutions(0, state)
        out = out_dir / f"nucleo_{pdf.stem}.pdf"
        session.save(out, {0: state}, list(range(session.page_count)), {}, allow_substitution=True)
        text, fonts = _line_fonts(out, span["origin"][1])
        result.update(texto=text, fuentes=sorted(fonts), fuente_original=span["font"],
                      ok=NEW_WORD in text and bool(fonts)
                      and all(_family(f) == _family(span["font"]) for f in fonts))
        result["registro"] = session.font_log[-6:]
    finally:
        session.close()
    return result


class _Driver:
    """Maneja la ventana real: abrir, editar con el teclado y esperar el render."""

    def __init__(self, app):
        self.app = app
        app.show_view("edit")
        self.v = app._views["edit"]

    def settle(self, ms):
        end = time.perf_counter() + ms / 1000
        while time.perf_counter() < end:
            self.app.update()

    def wait(self, cond, timeout=60):
        end = time.time() + timeout
        while not cond() and time.time() < end:
            self.app.update()
        return cond()

    def open(self, pdf: Path, out_dir: Path, name: str, settle_ms: int = 800) -> bool:
        v = self.v
        _say(f"abrir {pdf.name} ({name})")
        self.opened = getattr(self, "opened", 0) + 1   # una copia por caso: siempre se vuelve a abrir
        copy = out_dir / "entradas" / f"{self.opened}_{pdf.name}"
        copy.parent.mkdir(exist_ok=True)
        shutil.copy(pdf, copy)
        pdf = copy
        v.output_selector.entry.delete(0, "end")
        v.output_selector.entry.insert(0, str(out_dir))
        v.output_selector.name_entry.delete(0, "end")
        v.output_selector.name_entry.insert(0, name)
        v.ed_image = None
        v.picker.add_files([str(pdf)])
        ok = self.wait(lambda: v.ed_session is not None and v.ed_image is not None
                       and v._model() is not None and Path(v.ed_src) == pdf)
        self.settle(settle_ms)
        return ok

    def rendered(self):
        _say("esperar render")
        self.settle(300)
        self.wait(lambda: self.v.ed_render_job is None and (
            not self.v.ed_verify or getattr(self.v, "ed_subst_dialog", None) is not None), 30)
        self.settle(1500)

    def crop(self, line):
        box = [int(c * self.v.ed_scale) for c in line["bbox"]]
        return self.v.ed_image.crop(box)

    def edit(self, line, new, result) -> bool:
        """Abre la línea, escribe `new` y confirma con Enter (como el usuario)."""
        _say(f"editar {line['id']} -> {new!r}")
        v, c = self.v, self.v.ed_canvas
        _x0, y0, _x1, _y1 = v._to_screen(line["bbox"])          # la línea a la vista
        region = float(c.cget("scrollregion").split()[3])
        c.yview_moveto(max(0.0, (y0 - 150) / region))
        self.settle(200)
        v._select(line["id"])
        v._begin_edit(line["id"])
        if not v.ed_editor:
            result["error"] = "no se abrió el cuadro de edición"
            return False
        widget = v.ed_editor[0]
        self.settle(300)                                  # el editor coloca el cursor con after()
        for _ in range(30):                               # Tk entrega las teclas al widget con foco
            widget.focus_force()
            self.settle(100)
            if widget.focus_get() is widget:
                break
        widget.delete("1.0", "end")
        widget.insert("1.0", new)
        if widget.focus_get() is widget:
            widget.event_generate("<KeyPress-Return>")
            result["enter"] = "tecla"
        else:   # sin foco del sistema (otra ventana al frente): la misma acción que su Enter
            v._commit_edit()
            result["enter"] = "directo (sin foco)"
        self.settle(300)
        result["editor_cerrado"] = v.ed_editor is None
        return True


def _gui_check(app, pdf: Path, out_dir: Path, needle: str) -> dict:
    result = {"pdf": pdf.name, "modo": "interfaz"}
    d = _Driver(app)
    v = d.v
    if not d.open(pdf, out_dir, f"interfaz_{pdf.stem}"):
        result.update(ok=False, error="no se abrió el PDF", estado=v.status.cget("text"))
        return result
    line = _pick_line(v._model(), needle)
    span = line["spans"][0]
    before_img = d.crop(line)
    if not d.edit(line, _new_text(line), result):
        result["ok"] = False
        return result
    d.rendered()
    if getattr(v, "ed_subst_dialog", None) is not None:
        result["dialogo"] = v.ed_subst_dialog.ed_buttons["substitute"].cget("text")
        v.ed_subst_dialog.ed_buttons["substitute"].invoke()
        d.rendered()
    shown = d.crop(line)
    changed = shown.tobytes() != before_img.tobytes()
    dark, before_dark = _dark(shown), _dark(before_img)
    result["cambio_estado"] = {k: o for k, o in v._st()["overrides"].items() if o}
    result["vista_previa"] = {"pixeles_texto": dark, "pixeles_antes": before_dark,
                              "cambio": changed, "estado": v.status.cget("text")}
    _say("guardar")
    v.run()
    d.wait(lambda: not v._running and list(out_dir.glob(f"interfaz_{pdf.stem}*.pdf")), 90)
    d.settle(500)
    saved = sorted(out_dir.glob(f"interfaz_{pdf.stem}*.pdf"))
    if not saved:
        result.update(ok=False, error="no se guardó", estado=v.status.cget("text"))
        return result
    text, fonts = _line_fonts(saved[-1], span["origin"][1])
    result.update(texto=text, fuentes=sorted(fonts), fuente_original=span["font"],
                  estado=v.status.cget("text"),
                  ok=(changed and dark > 0.4 * before_dark and NEW_WORD in text and bool(fonts)
                      and all(_family(f) == _family(span["font"]) for f in fonts)))
    return result


def _failure_checks(app, pdf: Path, out_dir: Path, needle: str) -> list[dict]:
    """La edición es atómica: si reinsertar el texto falla (o no dibuja nada), queda el
    original, se avisa y el error queda en errores.log. Y Enter vacío pregunta."""
    from core import errors, pdf_editor
    from core import pdf_textops as tops

    d = _Driver(app)
    v = d.v
    results = []
    log_file = errors.logs_dir() / "errores.log"

    def raise_forced(*_a, **_k):
        raise RuntimeError(FORCED)

    def draw_nothing(self, page, unit, overlay):
        return list(unit.get("bbox", [0, 0, 0, 0])), None

    cases = [
        ("falla al reinsertar", {(pdf_editor.EditorSession, "_register_font"): raise_forced,
                                 (tops.Rewriter, "emit_after"): raise_forced,
                                 (pdf_editor.EditorSession, "_draw_text"): raise_forced}, FORCED),
        ("reinserción sin dibujo", {(tops.Rewriter, "emit_after"): lambda *_a, **_k: None,
                                    (pdf_editor.EditorSession, "_draw_text"): draw_nothing},
         "no quedó en la página"),
    ]
    for name, patches, expected in cases:
        result = {"pdf": pdf.name, "modo": f"interfaz · {name}"}
        results.append(result)
        if not d.open(pdf, out_dir, f"falla_{pdf.stem}"):
            result.update(ok=False, error="no se abrió el PDF")
            continue
        line = _pick_line(v._model(), needle)
        before = d.crop(line).tobytes()
        log_size = log_file.stat().st_size if log_file.exists() else 0
        saved = {key: getattr(*key) for key in patches}
        for (owner, attr), func in patches.items():
            setattr(owner, attr, func)
        try:
            v.ed_session._font_cache.clear()
            if not d.edit(line, _new_text(line), result):
                result["ok"] = False
                continue
            d.rendered()
        finally:
            for (owner, attr), func in saved.items():
                setattr(owner, attr, func)
        status = v.status.cget("text")
        logged = log_file.exists() and expected.split()[0] in log_file.read_bytes()[log_size:].decode(
            "utf-8", "replace")
        override = v._st()["overrides"].get(line["id"], {})
        intact = d.crop(line).tobytes() == before
        result.update(estado=status, original_intacto=intact, sin_cambio=not override.get("text"),
                      en_errores_log=logged,
                      ok=(intact and not override.get("text") and logged
                          and "No se pudo aplicar la edición" in status and expected in status))

    # Enter con el texto vacío: pregunta y, si se cancela, el texto sigue igual
    result = {"pdf": pdf.name, "modo": "interfaz · Enter vacío"}
    results.append(result)
    if d.open(pdf, out_dir, f"vacio_{pdf.stem}"):
        line = _pick_line(v._model(), needle)
        before = d.crop(line).tobytes()
        d.edit(line, "", result)
        dialog = v.ed_confirm_dialog
        result["pregunta"] = dialog is not None
        if dialog is not None:
            result["texto_pregunta"] = dialog.ed_message
            dialog.ed_buttons["no"].invoke()
        d.rendered()
        result.update(original_intacto=d.crop(line).tobytes() == before,
                      ok=bool(dialog is not None and result.get("texto_pregunta") == "¿Eliminar este texto?"
                              and d.crop(line).tobytes() == before
                              and not v._st()["overrides"].get(line["id"])))
    else:
        result.update(ok=False, error="no se abrió el PDF")
    return results


# --------------------------------------------------------------- clics y teclas reales

VK = {"x": 0x58, "space": 0x20, "BackSpace": 0x08, "Delete": 0x2E, "Return": 0x0D, "Escape": 0x1B}


class OtherAppInTheWay(RuntimeError):
    """Un clic o una tecla real iría a otra app: la prueba no lo manda."""


def _owned_by_us(hwnd) -> bool:
    import ctypes
    import os
    from ctypes import wintypes

    if not hwnd:
        return False
    pid = wintypes.DWORD()
    ctypes.windll.user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
    return pid.value == os.getpid()


def _wait_clear(clear, problem: str, seconds: float = 6.0) -> None:
    """Antes de un clic o una tecla real: si otra app está en el camino (una notificación, otra
    ventana), espera unos segundos y reintenta; si sigue ahí, no manda nada."""
    import time
    import tkinter

    end = time.time() + seconds
    while not clear():
        if time.time() > end:
            raise OtherAppInTheWay(problem)
        if tkinter._default_root is not None:
            tkinter._default_root.update()
        time.sleep(0.25)


def _mouse(x, y, clicks=1):
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    user32.WindowFromPoint.argtypes = [wintypes.POINT]
    user32.WindowFromPoint.restype = wintypes.HWND
    point = wintypes.POINT(int(x), int(y))
    _wait_clear(lambda: _owned_by_us(user32.WindowFromPoint(point)),
                f"otra ventana tapa el punto ({int(x)}, {int(y)}): no se hizo clic")
    user32.SetCursorPos(int(x), int(y))
    for _ in range(clicks):
        user32.mouse_event(0x0002, 0, 0, 0, 0)
        user32.mouse_event(0x0004, 0, 0, 0, 0)


def _drag(start, end, settle, steps=10, before_release=None):
    """Arrastre real del mouse. `before_release` corre con el botón todavía presionado."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    user32.WindowFromPoint.argtypes = [wintypes.POINT]
    user32.WindowFromPoint.restype = wintypes.HWND
    point = wintypes.POINT(int(start[0]), int(start[1]))
    _wait_clear(lambda: _owned_by_us(user32.WindowFromPoint(point)),
                f"otra ventana tapa el punto ({point.x}, {point.y}): no se arrastró")
    user32.SetCursorPos(point.x, point.y)
    settle(60)
    user32.mouse_event(0x0002, 0, 0, 0, 0)
    settle(60)
    for n in range(1, steps + 1):
        user32.SetCursorPos(int(start[0] + (end[0] - start[0]) * n / steps),
                            int(start[1] + (end[1] - start[1]) * n / steps))
        settle(40)
    if before_release is not None:
        before_release()
    user32.mouse_event(0x0004, 0, 0, 0, 0)
    settle(60)


def _handle_point(v, side):
    """Tirador de ancho (lado izquierdo o derecho, a media altura) en coordenadas de pantalla."""
    _uid, frame = v._handle_frame()
    _name, x, y = next(p for p in v._handle_points(*frame) if p[0] == side)
    c = v.ed_canvas
    return c.winfo_rootx() + x - c.canvasx(0), c.winfo_rooty() + y - c.canvasy(0)


def _key(name):
    import ctypes

    user32 = ctypes.windll.user32
    user32.GetForegroundWindow.restype = ctypes.c_void_p
    _wait_clear(lambda: _owned_by_us(user32.GetForegroundWindow()),
                f"la ventana activa es de otra app: no se mandó la tecla {name}")
    user32.keybd_event(VK[name], 0, 0, 0)
    user32.keybd_event(VK[name], 0, 0x0002, 0)


def _screen_point(v, line):
    """Centro de la línea en la pantalla (la lleva a la vista si hace falta)."""
    c = v.ed_canvas
    x0, y0, x1, y1 = v._to_screen(line["bbox"])
    region = float(c.cget("scrollregion").split()[3])
    if not (c.canvasy(0) + 40 < y0 < c.canvasy(0) + c.winfo_height() - 40):
        c.yview_moveto(max(0.0, (y0 - 150) / region))
        c.update()
    return (c.winfo_rootx() + (x0 + x1) / 2 - c.canvasx(0),
            c.winfo_rooty() + (y0 + y1) / 2 - c.canvasy(0))


def _input_checks(app, pdf: Path, out_dir: Path, needle: str) -> list[dict]:
    """Foco y borrado (v1.3.7) con el mouse y el teclado de verdad. Si otra app se pone en el
    camino, esa prueba queda «omitida» (ok None, no cuenta como falla) y no se sigue."""
    results: list[dict] = []
    try:
        _input_cases(app, pdf, out_dir, needle, results)
    except OtherAppInTheWay as exc:
        if results:
            results[-1].update(ok=None, omitida=str(exc))
    return results


def _input_cases(app, pdf: Path, out_dir: Path, needle: str, results: list[dict]) -> list[dict]:
    import tkinter as tk

    from core import errors

    d = _Driver(app)
    v = d.v
    actions_log = errors.logs_dir() / "acciones.log"

    def new_result(name):
        result = {"pdf": pdf.name, "modo": f"teclado/mouse · {name}"}
        results.append(result)
        return result

    def select(line):
        """Elegir la línea con un clic real (sin estar elegida antes: un clic sobre un texto ya
        elegido abre el editor donde se hizo clic)."""
        v._select(None)
        x, y = _screen_point(v, line)
        _mouse(x, y)
        d.settle(400)

    def other_window():
        """Otra ventana al frente: la principal queda sin foco (como al volver de otra app)."""
        other = tk.Toplevel(app)
        other.title("otra ventana")
        other.geometry(f"240x90+{app.winfo_rootx() + app.winfo_width() - 260}+{app.winfo_rooty() + 8}")
        other.attributes("-topmost", True)
        other.focus_force()
        d.settle(400)
        return other

    # 1) doble clic apenas aparece la página, con la ventana sin foco
    result = new_result("doble clic recién abierto, ventana sin foco")
    other = other_window()
    if not d.open(pdf, out_dir, f"foco_{pdf.stem}", settle_ms=0):
        result.update(ok=False, error="no se abrió el PDF")
        return results
    line = _pick_line(v._model(), needle)
    x, y = _screen_point(v, line)
    other.focus_force()
    d.settle(150)
    result["ventana_activa_antes"] = app.focus_get() is not None and app.focus_get().winfo_toplevel() is app
    _mouse(x, y, clicks=2)
    d.wait(lambda: v.ed_editor is not None, 3)
    d.settle(300)
    editor = v.ed_editor
    result.update(editor_abierto=editor is not None and editor[1] == line["id"],
                  foco_en_editor=editor is not None and app.focus_get() is editor[0])
    result["ok"] = result["editor_abierto"] and result["foco_en_editor"] and not result["ventana_activa_antes"]
    other.destroy()
    if v.ed_editor:
        _key("Escape")
        d.settle(300)

    # 2) escribir sobre una línea elegida: abre el editor y agrega la letra al final
    result = new_result("escribir «x» sobre la línea elegida")
    original = v._element(line["id"])["text"]
    select(line)
    result["elegida"] = v.ed_selected == line["id"] and v.ed_editor is None
    _key("x")
    d.wait(lambda: v.ed_editor is not None, 3)
    d.settle(200)
    typed = v.ed_editor[0].get("1.0", "end-1c") if v.ed_editor else None
    _key("Return")
    d.rendered()
    saved = v._st()["overrides"].get(line["id"], {}).get("text")
    result.update(en_editor=typed, guardado=saved, estado=v.status.cget("text"),
                  ok=typed == original + "x" and saved == original + "x"
                  and "No se pudo" not in v.status.cget("text"))

    # 3) Retroceso sobre la línea elegida: solo la última letra
    result = new_result("Retroceso sobre la línea elegida")
    current = v._element(line["id"])["text"]
    select(line)
    _key("BackSpace")
    d.wait(lambda: v.ed_editor is not None, 3)
    d.settle(200)
    shown = v.ed_editor[0].get("1.0", "end-1c") if v.ed_editor else None
    _key("Return")
    d.rendered()
    saved = v._st()["overrides"].get(line["id"], {}).get("text")
    result.update(en_editor=shown, guardado=saved, sigue_la_linea=not v._st()["overrides"].get(
        line["id"], {}).get("deleted"), ok=shown == current[:-1] and saved == current[:-1])

    # 4) Supr sobre una línea original: se borra, aviso «Deshacer», y vuelve idéntica
    result = new_result("Supr sobre una línea original + Deshacer")
    select(line)
    before = d.crop(line).tobytes()
    log_size = actions_log.stat().st_size if actions_log.exists() else 0
    _key("Delete")
    d.rendered()
    deleted = bool(v._st()["overrides"].get(line["id"], {}).get("deleted"))
    toast = getattr(v, "ed_delete_toast", None)
    toast_ok = bool(toast is not None and toast.winfo_exists() and hasattr(toast, "action_button")
                    and toast.action_button.cget("text") == "Deshacer")
    logged = actions_log.exists() and "tecla Supr" in actions_log.read_bytes()[log_size:].decode(
        "utf-8", "replace")
    if toast_ok:
        toast.action_button.invoke()
    d.rendered()
    restored = d.crop(line).tobytes() == before and not v._st()["overrides"].get(line["id"], {}).get("deleted")
    result.update(borrada=deleted, aviso_deshacer=toast_ok, en_acciones_log=logged, restaurada_igual=restored,
                  ok=deleted and toast_ok and logged and restored)

    # 5) Supr sobre un elemento agregado: se borra directo, también con «Deshacer»
    result = new_result("Supr sobre un elemento agregado")
    box = [line["bbox"][0], line["bbox"][3] + 30, line["bbox"][0] + 120, line["bbox"][3] + 60]
    v._add({"kind": "highlight", "bbox": box, "color": "#FFEB3B"})
    d.rendered()
    uid = v.ed_selected
    v._select(None)
    x, y = _screen_point(v, {"bbox": box})     # elegirlo con un clic real (el lienzo toma el foco)
    _mouse(x, y)
    d.settle(400)
    result["elegido"] = v.ed_selected == uid
    _key("Delete")
    d.rendered()
    gone = all(a["id"] != uid for a in v._st()["added"])
    toast = getattr(v, "ed_delete_toast", None)
    result["aviso"] = bool(toast is not None and toast.winfo_exists())
    result["deshacer_disponible"] = bool(v.ed_undo)
    if toast is not None and toast.winfo_exists():
        toast.action_button.invoke()
    d.rendered()
    back = any(a["id"] == uid for a in v._st()["added"])
    result.update(borrado=gone, deshacer=back, ok=bool(uid) and gone and back)

    # 6) escribir más de lo que cabe: el texto pasa al renglón siguiente, nada se cancela
    result = new_result("texto que no cabe en la línea")
    original = v._element(line["id"])["text"]
    select(line)
    d.settle(200)
    _key("x")
    d.wait(lambda: v.ed_editor is not None, 3)
    d.settle(200)
    widget = v.ed_editor[0]
    start_height = v.ed_editor_geom[3] - v.ed_editor_geom[1]
    widget.insert("end-1c", " palabra" * 40)
    widget.event_generate("<KeyRelease>")
    d.settle(200)
    result["crecio"] = v.ed_editor_geom[3] - v.ed_editor_geom[1] > start_height * 1.5
    _key("Return")
    d.wait(lambda: not v.ed_reflowing, 20)
    d.rendered()
    if v._model() is None:
        d.wait(lambda: v._model() is not None, 10)
    text = v.ed_session.doc[v.ed_current].get_text()
    result["estado"] = v.status.cget("text")
    result.update(completo="palabra palabra" in text and "No se pudo" not in v.status.cget("text"))
    v._undo()
    d.wait(lambda: v._model() is not None and v.ed_image is not None, 10)
    d.rendered()
    back_text = " ".join(v.ed_session.doc[v.ed_current].get_text().split())
    result["deshecho"] = "palabra" not in back_text and " ".join(original.split()) in back_text
    result["ok"] = result["crecio"] and result["completo"] and result["deshecho"]

    # 7) angostar el cuadro con el tirador: el texto se reacomoda, Deshacer vuelve exacto
    result = new_result("cambiar el ancho del cuadro con el mouse")
    line = _pick_line(v._model(), needle)
    data = v.ed_session.data
    select(line)
    d.settle(200)
    frame = v._text_frame(line["id"])
    start = _handle_point(v, "e")
    width = frame[2] - frame[0]
    _drag(start, (start[0] - width * 0.55 * v.ed_scale, start[1]), d.settle)
    d.wait(lambda: not v.ed_reflowing, 20)
    d.wait(lambda: v._model() is not None and v.ed_image is not None, 10)
    d.rendered()
    rows = [l for l in v._model()["lines"] if l["bbox"][0] < frame[2] and l["bbox"][1] >= frame[1] - 2]
    words = " ".join(v.ed_session.doc[v.ed_current].get_text().split())
    result.update(estado=v.status.cget("text"), renglones=len(rows),
                  completo=all(w in words for w in original.split()))
    v._undo()
    d.wait(lambda: v._model() is not None and v.ed_image is not None, 10)
    d.rendered()
    result["deshecho"] = v.ed_session.data is data
    result["ok"] = len(rows) >= 2 and result["completo"] and result["deshecho"] \
        and "No se pudo" not in result["estado"]
    return results


def run(argv: list[str]) -> int:
    i = argv.index("--autotest")
    report_path = Path(argv[i + 1])
    _PROGRESS[:] = [report_path.with_suffix(".log")]
    _PROGRESS[0].write_text("", encoding="utf-8")
    import faulthandler    # si algo se cuelga: pilas de todos los hilos cada 90 s

    stacks = open(report_path.with_suffix(".pilas.txt"), "w", encoding="utf-8")
    faulthandler.dump_traceback_later(90, repeat=True, file=stacks)
    # cada PDF puede llevar la línea a editar: "archivo.pdf::texto de la línea"
    pdfs = [(Path(p.split("::")[0]), p.split("::")[1] if "::" in p else "CARRERA") for p in argv[i + 2:]]
    work = Path(tempfile.mkdtemp(prefix="pliegue_autotest_"))
    report = {"version": None, "congelado": bool(getattr(sys, "frozen", False)),
              "carpeta": str(work), "resultados": [], "errores_hilos": [], "errores": []}
    thread_errors = report["errores_hilos"]
    previous_hook = threading.excepthook

    def thread_hook(args):
        thread_errors.append("".join(traceback.format_exception(args.exc_type, args.exc_value,
                                                                args.exc_traceback)))
        previous_hook(args)

    threading.excepthook = thread_hook
    copies = []
    for pdf, needle in pdfs:
        copy = work / pdf.name
        shutil.copy(pdf, copy)          # nunca se toca el original
        copies.append((copy, needle))
    for pdf, needle in copies:
        try:
            report["resultados"].append(_core_check(pdf, work, needle))
        except Exception:
            report["errores"].append(traceback.format_exc())
    app = None
    try:
        from ui.app import VERSION, App

        def tk_error(self, exc, val, tb):
            report["errores"].append("".join(traceback.format_exception(exc, val, tb)))

        App.report_callback_exception = tk_error
        report["version"] = VERSION
        app = App()
        app.geometry("1320x840+40+40")
        app.attributes("-topmost", True)

        def driver():
            # Dentro de mainloop: si no, Tk rechaza los after() de los hilos de trabajo.
            for pdf, needle in copies:
                for check in (_gui_check, _failure_checks, _input_checks):
                    try:
                        _say(f"== {check.__name__} {pdf.name}")
                        found = check(app, pdf, work, needle)
                        report["resultados"] += found if isinstance(found, list) else [found]
                    except Exception:
                        report["errores"].append(traceback.format_exc())
            app.quit()

        app.after(800, driver)
        app.mainloop()
    except Exception:
        report["errores"].append(traceback.format_exc())
    finally:
        if app is not None:
            try:
                app.destroy()
            except Exception:
                pass
    # las omitidas (otra app en el camino: no se mandó el clic o la tecla) no cuentan como falla
    report["omitidas"] = [r["modo"] for r in report["resultados"] if r.get("omitida")]
    report["ok"] = (bool(report["resultados"])
                    and all(r.get("ok") for r in report["resultados"] if not r.get("omitida"))
                    and not report["errores"] and not report["errores_hilos"])
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    faulthandler.cancel_dump_traceback_later()
    stacks.close()
    return 0 if report["ok"] else 1


def run_script(argv: list[str]) -> int:
    """Pliegue.exe --run-script <prueba.py> [--salida <archivo>] [argumentos]

    Corre una suite de tests/ DENTRO del exe: con el Python, las bibliotecas y el código
    (core/, ui/) empaquetados — así se detecta lo que falte en el paquete (imports diferidos,
    módulos excluidos). La salida va a --salida (el exe no tiene consola)."""
    import runpy

    i = argv.index("--run-script")
    rest = argv[i + 1:]
    script = Path(rest.pop(0))
    out_path = None
    if "--salida" in rest:
        j = rest.index("--salida")
        out_path = Path(rest[j + 1])
        del rest[j:j + 2]
    stream = open(out_path, "w", encoding="utf-8") if out_path else None
    if stream is not None:
        sys.stdout = sys.stderr = stream
    import core  # noqa: F401  (los del exe; el sys.path de la suite ya no los reemplaza)
    import ui  # noqa: F401

    sys.argv = [str(script)] + rest
    sys.path.insert(0, str(script.resolve().parent))   # como `python suite.py`: su carpeta en sys.path
    code = 0
    try:
        runpy.run_path(str(script), run_name="__main__")
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    except BaseException:
        traceback.print_exc()
        code = 1
    finally:
        if stream is not None:
            stream.flush()
            stream.close()
    return code
