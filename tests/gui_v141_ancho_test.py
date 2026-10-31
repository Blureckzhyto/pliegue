"""v1.4.1: cambiar el ancho del cuadro de texto, con arrastre real del mouse y teclas de verdad:
renglones, contenido de abajo, límites de columna y margen, el diálogo cuando no cabe, deshacer,
y que arrastrar desde el centro siga moviendo el cuadro."""
import ctypes
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fixtures  # noqa: E402
from ui import selftest  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="pliegue_gui_ancho_"))
SETTINGS = __import__("core.paths").paths.settings_file()
backup = SETTINGS.read_bytes() if SETTINGS.exists() else None
results, log = [], []


def check(name, cond, detail=""):
    results.append(bool(cond))
    log.append(("  PASS  " if cond else "  FAIL  ") + name + (f"  [{detail}]" if detail else ""))


def skipped(name, reason):
    log.append(f"  OMITIDA  {name}  [{reason}]")


from ui.app import App  # noqa: E402

app = App()
app.geometry("1320x840+40+40")
app.attributes("-topmost", True)
page = fixtures.reflow_page(TMP / "pagina.pdf")
columns = fixtures.reflow_columns(TMP / "columnas.pdf")
full = fixtures.reflow_full(TMP / "llena.pdf")
user32 = ctypes.windll.user32


def line_named(v, needle):
    return next(l for l in v._model()["lines"] if needle in l["text"])


def run():
    try:
        steps()
    except selftest.OtherAppInTheWay as exc:
        skipped("el resto de la prueba", exc)
    except Exception:
        import traceback

        check("la prueba llegó hasta el final", False, traceback.format_exc()[-900:])
    app.destroy()


def steps():
    d = selftest._Driver(app)
    v = d.v

    def ready():
        d.wait(lambda: not v.ed_reflowing, 20)
        d.wait(lambda: v._model() is not None and v.ed_image is not None, 10)
        d.rendered()

    def pick(uid):
        """Elige un texto con un clic real sobre él."""
        v._select(None)
        box = v._box(uid)
        x, y = selftest._screen_point(v, {"bbox": box})
        selftest._mouse(x, y)
        d.settle(350)
        check_uid = v.ed_selected
        return check_uid

    def drag_edge(side, delta_pt, before_release=None, shift=False):
        start = selftest._handle_point(v, side)
        if shift:
            user32.keybd_event(0x10, 0, 0, 0)
        try:
            selftest._drag(start, (start[0] + delta_pt * v.ed_scale, start[1]), d.settle,
                           before_release=before_release)
        finally:
            if shift:
                user32.keybd_event(0x10, 0, 0x0002, 0)

    # ---------------------------------------------------------------- modo Línea: angostar
    check("abre el PDF", d.open(page, TMP, "pagina"))
    original = v.ed_session.data
    line = line_named(v, "Primer parrafo")
    uid = line["id"]
    final = line_named(v, "Texto final")["bbox"][1]
    height = line["style"]["line_height"]
    pick(uid)
    check("se elige el texto", v.ed_selected == uid, str(v.ed_selected))
    _id, frame = v._handle_frame()
    kinds = {p[0] for p in v._handle_points(*frame)}
    check("hay tiradores a los dos lados y en las esquinas", kinds == {"w", "e"} and len(v._handle_points(*frame)) == 6)
    x, y = selftest._handle_point(v, "e")
    user32.SetCursorPos(int(x), int(y))
    d.settle(250)
    check("el cursor cambia a ↔ sobre el tirador", v.ed_canvas.cget("cursor") == "sb_h_double_arrow", v.ed_canvas.cget("cursor"))
    before_image = v.ed_image
    live = {}

    def sample():
        d.wait(lambda: not v.ed_width_busy, 5)
        d.settle(250)
        d.wait(lambda: not v.ed_width_busy, 5)
        live["image"] = v.ed_image
        live["drag"] = dict(v.ed_drag or {})

    width = line["bbox"][2] - line["bbox"][0]
    drag_edge("e", -width * 0.5, before_release=sample)
    check("mientras se arrastra, la página se actualiza", live.get("image") is not before_image)
    check("con el botón apretado aún no hay nada aplicado", v.ed_session.data is not None and live["drag"].get("moved"))
    ready()
    status = v.status.cget("text")
    rows = [l for l in v._model()["lines"] if l["bbox"][1] >= line["bbox"][1] - 2 and l["bbox"][1] < line["bbox"][1] + 40]
    check("modo Línea: se convierte en párrafo", len(rows) >= 2, str([r["text"][:24] for r in rows]))
    check("los renglones respetan el ancho nuevo", all(l["bbox"][2] <= line["bbox"][0] + width * 0.5 + 8 for l in rows),
          str([round(l["bbox"][2]) for l in rows]))
    now = line_named(v, "Texto final")["bbox"][1]
    extra = now - final
    check("lo de abajo baja lo justo", extra > 0 and abs(extra / height - round(extra / height)) < 0.03, f"{extra:.1f} pt")
    check("mensaje de lo que pasó", "se desplazó" in status, status)
    check("deshacer es un solo paso", len(v.ed_undo) == 1, str(len(v.ed_undo)))
    v._undo()
    ready()
    check("deshacer vuelve al documento exacto", v.ed_session.data is original)

    # ---------------------------------------------------------------- modo Párrafo: ensanchar
    v._set_level("block")
    block = line_named(v, "Este informe")["block_id"]
    pick(block)
    check("párrafo elegido", v.ed_selected == block, str(v.ed_selected))
    box = v._text_frame(block)
    drag_edge("e", -(box[2] - box[0]) * 0.45)
    ready()
    narrowed = v.ed_session.data
    final_narrow = line_named(v, "Texto final")["bbox"][1]
    block = line_named(v, "Este informe")["block_id"]
    count = len(next(b for b in v._model()["blocks"] if b["id"] == block)["line_ids"])
    check("angostar agrega renglones", count >= 7, str(count))
    pick(block)
    tall = v._box(block)
    tall = tall[3] - tall[1]
    old_right = v._text_frame(block)[2]
    drag_edge("e", 150)
    ready()
    now_box = v.ed_layout.get(block)
    check("ensanchar deja menos renglones", now_box and now_box[3] - now_box[1] < tall - 20,
          f"alto {tall:.0f} → {now_box[3] - now_box[1] if now_box else 0:.0f}")
    check("sobra espacio: lo de abajo se queda (Ajustar apagado)",
          abs(line_named(v, "Texto final")["bbox"][1] - final_narrow) < 0.01)
    check("los renglones llegan al ancho nuevo", now_box and old_right + 100 < now_box[2] <= old_right + 152,
          f"{old_right:.0f} → {now_box[2] if now_box else 0:.0f}")
    v._undo()
    ready()
    check("deshacer el ensanche", v.ed_session.data is narrowed and not v.ed_states.get(0, {}).get("overrides"))
    v.ed_adjust = True
    block = line_named(v, "Este informe")["block_id"]
    pick(block)
    drag_edge("e", 150)
    ready()
    moved_up = line_named(v, "Texto final")["bbox"][1]
    check("con Ajustar encendido, lo de abajo sube", moved_up < final_narrow - 1, f"{final_narrow:.1f} → {moved_up:.1f}")
    v.ed_adjust = False
    while v.ed_undo:
        v._undo()
        ready()
    check("todo deshecho", v.ed_session.data is original)

    # ---------------------------------------------------------------- límites de la columna
    v._set_level("line")
    line = line_named(v, "Texto final")
    pick(line["id"])
    limits = v._width_limits(line["id"])
    guide = {}
    drag_edge("e", 700, before_release=lambda: guide.update(v.ed_drag or {}))
    check("el margen derecho es el tope", abs(guide.get("x1", 0) - limits["right"]) < 0.01,
          f"{guide.get('x1')} vs {limits['right']}")
    check("muestra la guía al tocar el margen", guide.get("guide") is True)
    ready()
    v._undo()
    ready()
    pick(line_named(v, "Texto final")["id"])
    limits = v._width_limits(v.ed_selected)
    start_frame = v._text_frame(v.ed_selected)
    held = {}
    drag_edge("e", -600, before_release=lambda: held.update(v.ed_drag or {}))
    check("ancho mínimo = la palabra más larga", abs(held.get("x1", 0) - held.get("x0", 0) - limits["min_width"]) < 0.01,
          f"{held.get('x1', 0) - held.get('x0', 0):.1f} vs {limits['min_width']:.1f}")
    ready()
    v._undo()
    ready()

    # mayúscula: simétrico
    pick(line_named(v, "Texto final")["id"])
    frame = v._text_frame(v.ed_selected)
    center = (frame[0] + frame[2]) / 2
    held = {}
    drag_edge("e", -30, before_release=lambda: held.update(v.ed_drag or {}), shift=True)
    check("Mayús + arrastrar cambia el ancho desde el centro",
          held and abs((held["x0"] + held["x1"]) / 2 - center) < 0.05 and held["x1"] < frame[2] - 5
          and held["x0"] > frame[0] + 5, f"{held.get('x0')} {held.get('x1')}")
    ready()
    v._undo()
    ready()

    # Alt + flechas
    selected = line_named(v, "Texto final")["id"]
    pick(selected)
    frame = v._text_frame(selected)
    v.ed_canvas.focus_force()
    d.settle(150)

    def alt(key, shift=False):
        code = {"Left": 0x25, "Right": 0x27}[key]
        selftest._wait_clear(lambda: selftest._owned_by_us(user32.GetForegroundWindow()), "la ventana activa no es Pliegue")
        user32.keybd_event(0x12, 0, 0, 0)
        if shift:
            user32.keybd_event(0x10, 0, 0, 0)
        user32.keybd_event(code, 0, 0x0001, 0)
        user32.keybd_event(code, 0, 0x0003, 0)
        if shift:
            user32.keybd_event(0x10, 0, 0x0002, 0)
        user32.keybd_event(0x12, 0, 0x0002, 0)
        d.settle(350)

    alt("Right")
    d.settle(200)
    one = v._st()["overrides"].get(selected, {}).get("box")
    check("Alt+flecha: 1 pt", one and abs(one[1] - frame[2] - 1) < 0.01, str(one))
    alt("Right", shift=True)
    two = v._st()["overrides"].get(selected, {}).get("box")
    check("Alt+Mayús+flecha: 10 pt", two and abs(two[1] - frame[2] - 11) < 0.01, str(two))
    while v.ed_undo:
        v._undo()
        ready()

    # ---------------------------------------------------------------- mover sigue moviendo
    line = line_named(v, "Texto final")
    pick(line["id"])
    d.settle(800)       # más que el tiempo de doble clic de Windows: esto es un arrastre aparte
    x, y = selftest._screen_point(v, line)
    selftest._drag((x, y), (x + 60, y + 30), d.settle)
    d.settle(400)
    ov = v._st()["overrides"].get(line["id"], {})
    check("arrastrar desde el centro mueve el cuadro", abs(ov.get("dx", 0)) > 20 and abs(ov.get("dy", 0)) > 10
          and "box" not in ov, str(ov))
    v._undo()
    ready()

    # ---------------------------------------------------------------- texto agregado
    v.ed_canvas.yview_moveto(0)      # el texto agregado va arriba: que el tirador quede a la vista
    d.settle(200)
    v._add_text(100, 140)
    d.settle(200)
    widget = v.ed_editor[0]
    widget.insert("end-1c", "Este texto fue agregado con varias palabras para ver como se acomoda en el cuadro")
    v._commit_edit()
    ready()
    added = v.ed_selected
    check("texto agregado elegido", added and added.startswith("N"), str(added))
    frame = v._text_frame(added)
    drag_edge("e", -(frame[2] - frame[0]) * 0.5)
    ready()
    element = v._element(added)
    check("texto agregado: toma el ancho nuevo", element.get("box") and element["box"][1] < frame[2] - 20, str(element.get("box")))
    box = v._box(added)
    check("texto agregado: se reacomoda en más renglones", box and box[3] - box[1] > (frame[3] - frame[1]) * 1.5,
          f"{frame[3] - frame[1]:.0f} → {box[3] - box[1]:.0f}")
    v._undo()
    ready()
    check("texto agregado: deshacer vuelve al ancho anterior", not v._element(added).get("box"))

    # ---------------------------------------------------------------- dos columnas
    check("abre dos columnas", d.open(columns, TMP, "columnas"))
    line = line_named(v, "Columna izquierda 2")
    pick(line["id"])
    column = v.ed_session.columns(0)[0]
    held = {}
    drag_edge("e", 500, before_release=lambda: held.update(v.ed_drag or {}))
    check("no invade la otra columna", held.get("x1", 999) <= column[1] + 0.5, f"{held.get('x1')} ≤ {column[1]}")
    ready()

    # ---------------------------------------------------------------- no cabe en la página
    check("abre la página llena", d.open(full, TMP, "llena"))
    original = v.ed_session.data
    pages = v.ed_session.page_count
    line = line_named(v, "Parrafo 02")
    v._set_level("block")
    block = line["block_id"]

    def narrow_full():
        pick(block)
        box = v._text_frame(block)
        drag_edge("e", -(box[2] - box[0]) * 0.6)
        d.wait(lambda: v.ed_overflow_dialog is not None, 30)
        d.settle(400)
        return v.ed_overflow_dialog

    dialog = narrow_full()
    check("aparece el mismo diálogo", dialog is not None and dialog.ed_choice.get() == "newpage")
    check("con vista previa", bool(dialog.ed_preview_labels))
    dialog.ed_buttons["cancel"].invoke()
    d.settle(400)
    ready()
    check("cancelar deja la página intacta", v.ed_session.data is original and block not in
          {k for k, ov in v._st()["overrides"].items() if ov})
    dialog = narrow_full()
    dialog.ed_buttons["apply"].invoke()
    ready()
    status = v.status.cget("text")
    check("página nueva", v.ed_session.page_count == pages + 1 and "Se creó la página 2" in status, status)
    v._undo()
    ready()
    check("deshacer quita la página y el cambio de ancho", v.ed_session.data is original and v.ed_session.page_count == pages)
    v._set_level("line")


app.after(800, run)
app.after(280000, app.destroy)
try:
    app.mainloop()
finally:
    if backup is not None:
        SETTINGS.write_bytes(backup)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
print("\n".join(log))
print(f"\n{sum(results)}/{len(results)} OK — {TMP}")
sys.exit(0 if results and all(results) else 1)
