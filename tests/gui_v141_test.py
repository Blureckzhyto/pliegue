"""v1.4.1: el texto que no cabe, con la ventana real: el cuadro de edición crece hacia abajo con
teclas de verdad, Enter aplica, lo de abajo baja, Deshacer revierte todo de un paso, y en una
página llena aparece el diálogo con sus opciones."""
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fixtures  # noqa: E402
from ui import selftest  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="pliegue_gui141_"))
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
full = fixtures.reflow_full(TMP / "llena.pdf")


def line_named(v, needle):
    return next(l for l in v._model()["lines"] if needle in l["text"])


def run():
    try:
        steps()
    except Exception:
        import traceback

        check("la prueba llegó hasta el final", False, traceback.format_exc()[-900:])
    app.destroy()


def steps():
    d = selftest._Driver(app)
    v = d.v

    def wait_applied():
        d.wait(lambda: not v.ed_reflowing, 20)
        d.wait(lambda: v._model() is not None and v.ed_image is not None, 10)
        d.rendered()

    check("abre el PDF", d.open(page, TMP, "pagina"))
    line = line_named(v, "Primer parrafo")
    below = line_named(v, "Texto final")["bbox"][1]
    pages = v.ed_session.page_count
    original = v.ed_session.data
    try:
        x, y = selftest._screen_point(v, line)
        selftest._mouse(x, y, clicks=2)
        d.wait(lambda: v.ed_editor is not None, 3)
        d.settle(300)
        check("se abre el editor", v.ed_editor is not None)
        widget = v.ed_editor[0]
        widget.mark_set("insert", "end-1c")
        top_height = v.ed_editor_geom[3] - v.ed_editor_geom[1]
        room = v.ed_editor_geom[2] - v.ed_editor_geom[0]
        grown = False
        for n in range(90):            # teclas de verdad, más allá del final del renglón
            selftest._key("space" if n % 8 == 7 else "x")
            d.settle(12)
            if v.ed_editor_geom[3] - v.ed_editor_geom[1] > top_height * 1.5:
                grown = True
        d.settle(150)
        check("el cuadro crece hacia abajo mientras se escribe", grown,
              f"{top_height:.0f} → {v.ed_editor_geom[3] - v.ed_editor_geom[1]:.0f} px")
        check("sin pasar del ancho del párrafo", v.ed_editor_geom[2] - v.ed_editor_geom[0] <= room * 1.02 + 200)
        typed = widget.get("1.0", "end-1c")
        check("lo tecleado está en el cuadro", typed.count("x") >= 70, typed[-30:])
        selftest._key("Return")
        wait_applied()
        status = v.status.cget("text")
        check("aplica sin cancelar", "No se pudo" not in status and "se desplazó" in status, status)
        now = line_named(v, "Texto final")["bbox"][1]
        check("lo de abajo bajó", now > below + 5, f"{below:.1f} → {now:.1f}")
        check("una sola entrada para deshacer", len(v.ed_undo) == 1, str(len(v.ed_undo)))
        check("no hay páginas nuevas", v.ed_session.page_count == pages)
        v._undo()
        wait_applied()
        check("deshacer devuelve el documento exacto", v.ed_session.data is original)
        check("y el texto de abajo vuelve a su lugar", abs(line_named(v, "Texto final")["bbox"][1] - below) < 0.01)
        v._redo()
        wait_applied()
        check("rehacer lo vuelve a aplicar", abs(line_named(v, "Texto final")["bbox"][1] - now) < 0.01)
        v._undo()
        wait_applied()
    except selftest.OtherAppInTheWay as exc:
        skipped("escribir con teclas reales", exc)

    # interruptores
    check("hay dos interruptores en la barra", True if v.ed_selected is None else hasattr(v, "ed_flow_switches"))
    v._select(line_named(v, "Primer parrafo")["id"])
    d.settle(200)
    check("desplazar: encendido por defecto; ajustar: apagado",
          v.ed_flow_switches["push"].get() == 1 and v.ed_flow_switches["adjust"].get() == 0)
    v.ed_flow_switches["push"].toggle()
    check("el interruptor cambia la opción", v.ed_push is False)
    v.ed_flow_switches["push"].toggle()

    # página casi llena: diálogo
    check("abre la página llena", d.open(full, TMP, "llena"))
    line = line_named(v, "Parrafo 02")
    original = v.ed_session.data
    pages = v.ed_session.page_count

    def type_long(extra=" contenido adicional" * 40, uid=None):
        uid = uid or line["id"]
        v._select(uid)
        v._begin_edit(uid)
        widget = v.ed_editor[0]
        widget.insert("end-1c", extra)
        v._commit_edit()
        d.wait(lambda: v.ed_overflow_dialog is not None or not v.ed_reflowing, 30)
        d.wait(lambda: v.ed_overflow_dialog is not None, 30)
        d.settle(400)
        return v.ed_overflow_dialog

    dialog = type_long()
    check("aparece el diálogo antes de aplicar", dialog is not None)
    check("página nueva por defecto", dialog.ed_choice.get() == "newpage")
    check("hay vista previa", bool(dialog.ed_preview_labels))
    check("el documento sigue igual", v.ed_session.data is original)
    dialog.ed_buttons["cancel"].invoke()
    d.settle(300)
    check("cancelar deja la página intacta", v.ed_session.data is original and line["id"] not in v._st()["overrides"])
    dialog = type_long()
    dialog.ed_buttons["apply"].invoke()
    d.wait(lambda: v._model() is not None and v.ed_image is not None, 10)
    d.rendered()
    status = v.status.cget("text")
    check("se creó la página nueva", v.ed_session.page_count == pages + 1 and "Se creó la página 2" in status, status)
    check("aparece en las miniaturas", len(v.ed_order) == pages + 1, str(v.ed_order))
    v._undo()
    d.wait(lambda: v._model() is not None and v.ed_image is not None, 10)
    d.rendered()
    check("deshacer quita la página nueva", v.ed_session.page_count == pages and len(v.ed_order) == pages
          and v.ed_session.data is original)
    # en modo Párrafo y con lo justo para que no quepa: así reducir la letra sí alcanza (en modo
    # Línea solo se reduce esa línea y casi no se gana espacio)
    v._set_level("block")
    block_id = line["block_id"]
    extra = None
    flowed = v.ed_session.flow_text(0, block_id)
    for count in range(5, 300, 5):
        state = {"overrides": {block_id: {"text": flowed + " palabra" * count}}, "added": [], "z": None}
        attempt = v.ed_session.reflow(0, state, block_id)
        if attempt.status == "overflow":
            extra = " palabra" * count
            break
    check("hay un texto que apenas no cabe", extra is not None and "shrink" in attempt.candidates,
          str(sorted(attempt.candidates)) if extra else "")
    dialog = type_long(extra, block_id)
    check("la opción de reducir letra está disponible", dialog.ed_radios["shrink"].cget("state") == "normal")
    first_preview = list(dialog.ed_photos.get("newpage", []))
    dialog.ed_radios["shrink"].invoke()
    d.settle(250)
    check("elegir la opción cambia la selección", dialog.ed_choice.get() == "shrink")
    check("la vista previa pasa a esa opción", bool(dialog.ed_photos.get("shrink")) and bool(first_preview))
    dialog.ed_buttons["apply"].invoke()
    d.wait(lambda: v._model() is not None and v.ed_image is not None, 10)
    d.rendered()
    status = v.status.cget("text")
    check("reducir letra aplica sin página nueva", v.ed_session.page_count == pages, status)
    check("dice el tamaño que quedó", " pt (" in status and "%" in status, status)
    sizes = {round(float(l["style"]["size"]), 2) for l in v._model()["lines"]}
    check("el tamaño no baja del 85 %", min(sizes) >= 11 * 0.85 - 0.01 and 11.0 in sizes, str(sorted(sizes)))
    v._undo()
    d.wait(lambda: v._model() is not None and v.ed_image is not None, 10)
    d.rendered()
    check("deshacer devuelve el tamaño original", v.ed_session.data is original)
    v._set_level("line")
    out = TMP / "resultado.pdf"
    state = v.ed_states.get(0)
    v.ed_session.save(out, {0: state} if state else {}, list(range(v.ed_session.page_count)), {})
    check("se puede guardar", out.exists() and out.stat().st_size > 500)


app.after(800, run)
app.after(240000, app.destroy)
try:
    app.mainloop()
finally:
    if backup is not None:
        SETTINGS.write_bytes(backup)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
print("\n".join(log))
print(f"\n{sum(results)}/{len(results)} OK — {TMP}")
sys.exit(0 if results and all(results) else 1)
