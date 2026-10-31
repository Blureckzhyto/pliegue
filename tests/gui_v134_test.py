"""Pruebas GUI del editor (v1.3.4) con el archivo real del usuario: alineación del cuadro de
edición, fuente con que se muestra, edición sin diálogo y, sin la familia, diálogo con la
vista previa intacta hasta responder."""
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from PIL import ImageChops, ImageGrab

TMP = Path(tempfile.mkdtemp(prefix="pliegue_gui134_"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import fixtures  # noqa: E402

WORK = fixtures.portada(TMP / "portada.pdf")
SHOTS = Path(__file__).parent / "_capturas" / "shots_v134"
SHOTS.mkdir(parents=True, exist_ok=True)
SHOTS.mkdir(exist_ok=True)
SETTINGS = __import__("core.paths").paths.settings_file()
backup = SETTINGS.read_bytes() if SETTINGS.exists() else None

import tkinter as tk

errors = []
tk.Tk.report_callback_exception = lambda self, exc, val, tb: errors.append(f"{exc.__name__}: {val}")

from core import font_finder
from ui.app import App
from ui.views.editor import MARGIN

app = App()
app.geometry("1320x840+40+40")
app.attributes("-topmost", True)
results, log = [], []
v = None


def check(name, cond, detail=""):
    results.append(bool(cond))
    log.append(("  PASS  " if cond else "  FAIL  ") + name + (f"  [{detail}]" if detail else ""))


def settle(ms=300):
    end = time.perf_counter() + ms / 1000
    while time.perf_counter() < end:
        app.update()


def wait(cond, timeout=60):
    end = time.time() + timeout
    while not cond() and time.time() < end:
        app.update()
    return cond()


def grab(name):
    app.update_idletasks()
    x, y = app.winfo_rootx(), app.winfo_rooty()
    ImageGrab.grab(bbox=(x, y, x + app.winfo_width(), y + app.winfo_height()), all_screens=True).save(
        SHOTS / f"{name}.png")


def target_line():
    return next(l for l in v._model()["lines"] if "CARRERA" in l["text"])


def bring_into_view(line):
    c = v.ed_canvas
    x0, y0, x1, y1 = v._to_screen(line["bbox"])
    region = float(c.cget("scrollregion").split()[3])
    c.yview_moveto(max(0.0, (y0 - 150) / region))
    c.xview_moveto(0)
    settle(150)
    return int((x0 + x1) / 2 - c.canvasx(0)), int((y0 + y1) / 2 - c.canvasy(0))


def set_zoom(zoom):
    v._commit_edit(cancel=True)
    v._select(None)
    v._set_zoom(zoom)
    want = int(zoom.split()[0]) / 100 * 96 / 72
    wait(lambda: abs(v.ed_scale - want) < 1e-6 and v.ed_image is not None)
    settle(400)


def double_click(x, y):
    c = v.ed_canvas
    for _ in range(2):
        c.event_generate("<Button-1>", x=x, y=y)
        c.event_generate("<ButtonRelease-1>", x=x, y=y)
    settle(300)


def open_file():
    global v
    app.show_view("edit")
    v = app._views["edit"]
    v.picker.add_files([str(WORK)])
    wait(lambda: v.ed_image is not None and v._model() is not None)
    settle(500)


def run():
    open_file()
    line = target_line()
    span = line["spans"][0]
    for zoom in ("50 %", "100 %", "200 %"):
        set_zoom(zoom)
        settle(700)
        x, y = bring_into_view(line)
        double_click(x, y)
        ok = v.ed_editor is not None
        check(f"{zoom}: doble clic abre el cuadro de edición", ok)
        if not ok:
            continue
        widget, uid, font = v.ed_editor
        s = v.ed_scale
        left, top = v.ed_canvas.coords("editor")
        want_left = MARGIN + span["origin"][0] * s
        want_base = MARGIN + span["origin"][1] * s
        base = top + font.metrics("ascent")
        check(f"{zoom}: el cuadro empieza en la x del texto (±1 px)", abs(left - want_left) <= 1,
              f"{left:.1f} vs {want_left:.1f}")
        check(f"{zoom}: misma línea base (±1 px)", abs(base - want_base) <= 1, f"{base:.1f} vs {want_base:.1f}")
        width = float(v.ed_canvas.itemcget("editor", "width"))
        line_w = (line["bbox"][2] - line["bbox"][0]) * s
        text_w = font.measure(widget.get("1.0", "end-1c"))
        # Tk solo admite tamaños enteros en píxeles: a 100 % (18,86 px → 19 px) el texto queda
        # hasta ~1 % más ancho; el cuadro mide exactamente su texto + 4 px para el cursor
        check(f"{zoom}: el cuadro mide su texto (sin ancho extra) y no más que la línea + 2 %",
              abs(width - (text_w + 4)) <= 1 and width <= line_w * 1.02 + 6,
              f"cuadro {width:.0f}, texto {text_w}, línea {line_w:.0f}")
        check(f"{zoom}: sin borde ni resaltado del cuadro", int(widget.cget("highlightthickness")) == 0
              and int(widget.cget("bd")) == 0 and int(widget.cget("padx")) == 0)
        # además del recuadro están los 6 tiradores de ancho (rellenos, los del recuadro no)
        drawn = v.ed_canvas.find_withtag("overlay")
        frames = [i for i in drawn if not v.ed_canvas.itemcget(i, "fill")]
        check(f"{zoom}: un solo recuadro de selección", len(frames) == 1 and len(drawn) == 7, str(len(drawn)))
        actual = font.actual()
        check(f"{zoom}: el cuadro muestra Montserrat negrita", actual["family"] == "Montserrat"
              and actual["weight"] == "bold", str(actual))
        if zoom == "200 %":
            grab("editando_200")
        v._commit_edit(cancel=True)
        settle(200)

    # editar con minúsculas: Montserrat (de la biblioteca), sin diálogo
    set_zoom("100 %")
    x, y = bring_into_view(line)
    double_click(x, y)
    widget = v.ed_editor[0]
    start = widget.search("SISTEMAS", "1.0")
    widget.delete(start, f"{start}+8c")
    widget.insert(start, "Sis")
    grab("editando_100_con_minusculas")
    v._commit_edit()
    wait(lambda: v.ed_render_job is None and v.ed_image is not None, 30)
    settle(2500)
    check("al confirmar no aparece ningún diálogo (Montserrat está en la biblioteca)",
          getattr(v, "ed_subst_dialog", None) is None)
    expected, _l, _n = v.ed_session.render_page(0, v._st(), v.ed_scale)
    box = [int(c * v.ed_scale) for c in line["bbox"]]
    shown = v.ed_image.crop(box)
    check("la vista previa muestra lo que se guardará (misma imagen)",
          ImageChops.difference(shown, expected.crop(box)).getbbox() is None)
    v._select(line["id"])
    wait(lambda: "Fuente original" in v.ed_font_note, 10)
    check("la barra dice que las letras nuevas usan Montserrat Bold (incluida)",
          "Montserrat Bold (incluida)" in v.ed_font_note, v.ed_font_note)
    grab("despues_vista_previa_100")

    # sin Montserrat (ni instalada ni en la biblioteca): diálogo y el original hasta responder
    real_index = font_finder.font_index()
    font_finder._INDEX = [f for f in real_index if font_finder.family_key(f.family) != "montserrat"]
    try:
        open_file()
        line = target_line()
        set_zoom("100 %")
        original = v.ed_image.copy()
        x, y = bring_into_view(line)
        double_click(x, y)
        widget = v.ed_editor[0]
        start = widget.search("SISTEMAS", "1.0")
        widget.delete(start, f"{start}+8c")
        widget.insert(start, "Sis")
        v._commit_edit()
        wait(lambda: getattr(v, "ed_subst_dialog", None) is not None, 30)
        settle(1500)
        dialog = getattr(v, "ed_subst_dialog", None)
        check("sin Montserrat: aparece el diálogo al confirmar la edición", dialog is not None)
        box = [int(c * v.ed_scale) for c in line["bbox"]]
        check("sin Montserrat: la vista previa sigue mostrando el original",
              ImageChops.difference(original.crop(box), v.ed_image.crop(box)).getbbox() is None)
        if dialog is not None:
            label = dialog.ed_buttons["substitute"].cget("text")
            check("el diálogo ofrece una sans negrita con su nombre (no una serif)",
                  "Poppins Bold" in label or "DM Sans Bold" in label, label)
            dialog.update_idletasks()
            x0, y0 = dialog.winfo_rootx(), dialog.winfo_rooty()
            ImageGrab.grab(bbox=(x0 - 8, y0 - 32, x0 + dialog.winfo_width() + 8,
                                 y0 + dialog.winfo_height() + 8), all_screens=True).save(
                SHOTS / "dialogo_sustitucion.png")
            dialog.ed_buttons["substitute"].invoke()
            wait(lambda: v.ed_render_job is None, 20)
            settle(2500)
            check("aceptada: la vista previa ya muestra la parecida",
                  ImageChops.difference(original.crop(box), v.ed_image.crop(box)).getbbox() is not None)
    finally:
        font_finder._INDEX = real_index
    check("sin excepciones en la interfaz", not errors, "; ".join(errors[:3]))
    app.destroy()


app.after(800, run)
app.after(600000, app.destroy)
try:
    app.mainloop()
finally:
    if backup is not None:
        SETTINGS.write_bytes(backup)
print("\n".join(log))
print(f"\n{sum(results)}/{len(results)} OK — {TMP}")
sys.exit(0 if results and all(results) else 1)
