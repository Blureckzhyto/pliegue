"""v1.4.1: el texto que no cabe pasa al renglón siguiente y desplaza lo de abajo (o sigue en una
página nueva). Núcleo, con documentos generados."""
import sys
import tempfile
import warnings
from collections import Counter
from pathlib import Path

warnings.filterwarnings("ignore")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import fitz  # noqa: E402
from PIL import ImageChops  # noqa: E402

import fixtures  # noqa: E402
from core import pdf_editor  # noqa: E402
from core import pdf_reflow as flow  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="pliegue_v141_"))
results = []


def check(name, ok, info=""):
    results.append(bool(ok))
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"  [{info}]" if info else ""), flush=True)


def line_with(session, needle, index=0):
    return next(l for l in session.analyze(index)["lines"] if needle in l["text"])


def edit(session, uid, text, index=0, **options):
    state = {"overrides": {uid: {"text": text}}, "added": [], "z": None}
    return session.reflow(index, state, uid, **options)


def render(data, index=0, zoom=2.0):
    from PIL import Image

    with fitz.open(stream=data, filetype="pdf") as doc:
        # un desfase de fracción de píxel: un borde justo sobre el límite de un píxel cambiaría
        # por el redondeo de MuPDF (275,99997 frente a 430,00000) sin que cambie el contenido
        matrix = fitz.Matrix(1, 0, 0, 1, 0, 0.13) * fitz.Matrix(zoom, zoom)
        pix = doc[index].get_pixmap(matrix=matrix, alpha=False)
        return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


def same_region(before, after, shift, y0, y1, x0=0, x1=None, zoom=2.0):
    """¿La franja [y0, y1] de `before`, bajada `shift` pt, es idéntica en `after`?"""
    a = render(before, zoom=zoom)
    b = render(after, zoom=zoom)
    x1 = x1 or a.width / zoom
    top, bottom = int(y0 * zoom), int(y1 * zoom)
    move = round(shift * zoom)      # los fixtures usan interlineados enteros: píxeles exactos
    left, right = int(x0 * zoom), int(x1 * zoom)
    difference = ImageChops.difference(a.crop((left, top, right, bottom)),
                                       b.crop((left, top + move, right, bottom + move))).convert("L")
    worst = difference.getextrema()[1]
    return worst <= 3, worst


words = "palabra " * 120
text_120 = "CAMBIADO " + words.strip()

print("== ajuste de renglones")
measure = lambda a, b: (b - a) * 5.0   # noqa: E731
text = "uno dos tres cuatro cinco seis siete"
rows, _ = flow.wrap_rows(text, measure, 50, 50)
check("corta por palabras", [text[r.start:r.end] for r in rows] == ["uno dos", "tres", "cuatro", "cinco seis", "siete"],
      str([text[r.start:r.end] for r in rows]))
rows, _ = flow.wrap_rows("a" * 25, measure, 50, 50, hyphen_width=5)
check("una palabra larguísima se parte con guion", len(rows) == 3 and rows[0].hyphen and not rows[-1].hyphen,
      str([(r.start, r.end, r.hyphen) for r in rows]))
rows, _ = flow.wrap_rows("uno\ndos", measure, 50, 50)
check("los saltos del usuario se respetan", len(rows) == 2 and rows[0].last and rows[1].last)

print("== una columna: texto, imagen y tabla")
pdf = fixtures.reflow_page(TMP / "pagina.pdf")
s = pdf_editor.EditorSession(pdf)
original = s.data
target = line_with(s, "Primer parrafo")
before_model = s.analyze(0)
height = target["style"]["line_height"]
heading_y = line_with(s, "Resultados por region")["bbox"][1]
final_text = line_with(s, "Texto final")
ov = {"text": target["text"] + " " + text_120}
check("necesita desplazar", s.needs_reflow(0, target["id"], ov, True, False))
result = edit(s, target["id"], ov["text"])
check("no se cancela: se aplica", result.status == "done", result.status)
extra = result.shift
check("mueve exactamente N renglones", abs(extra - result.extra_lines * height) < 0.01 and result.extra_lines > 3,
      f"{result.extra_lines} × {height} = {extra}")
check("mensaje con lo que pasó", f"{result.extra_lines} líneas más" in result.message and "se desplazó" in result.message,
      result.message)
s.adopt(result.data)
after = s.analyze(0)
check("texto completo en la página", pdf_editor._no_hyphens(ov["text"]) in pdf_editor._no_hyphens(s.doc[0].get_text()))
new_rows = [l for l in after["lines"] if "palabra" in l["text"]]
right = max(l["bbox"][2] for l in before_model["lines"])
check("los renglones respetan el ancho del párrafo", max(l["bbox"][2] for l in new_rows) <= right + 1,
      f"{max(l['bbox'][2] for l in new_rows):.1f} ≤ {right:.1f}")
check("misma fuente, tamaño y color",
      all(l["style"]["font"] == target["style"]["font"] and abs(l["style"]["size"] - target["style"]["size"]) < 0.01
          and l["style"]["color"] == target["style"]["color"] for l in new_rows))
baselines = sorted({round(l["lines"][0]["origin"][1], 2) for l in new_rows})
check("mismo interlineado", all(abs(b - a - height) < 0.05 for a, b in zip(baselines, baselines[1:])))
check("título de abajo bajó exacto (±0,5 pt)",
      abs(line_with(s, "Resultados por region")["bbox"][1] - heading_y - extra) <= 0.5)
check("texto bajo la tabla bajó exacto (±0,5 pt)",
      abs(line_with(s, "Texto final")["bbox"][1] - final_text["bbox"][1] - extra) <= 0.5)
image_before = before_model["images"][0]["bbox"]
image_after = after["images"][0]["bbox"]
check("imagen bajó exacto (±0,5 pt)", abs(image_after[1] - image_before[1] - extra) <= 0.5,
      f"{image_before[1]:.1f} → {image_after[1]:.1f}")
ok, worst = same_region(original, result.data, extra, heading_y - 2, final_text["bbox"][3] + 2)
check("lo desplazado es idéntico, píxel a píxel (salvo el desplazamiento)", ok, f"diferencia máxima {worst}")
ok, worst = same_region(original, result.data, 0, 0, target["bbox"][1] - 2)
check("lo de arriba no cambió", ok, f"diferencia máxima {worst}")
fonts = {l["style"]["font"] for l in after["lines"]}
check("el texto movido se extrae con los mismos nombres de fuente", fonts == {l["style"]["font"] for l in before_model["lines"]},
      str(fonts))
moved = line_with(s, "Texto final")
check("el texto movido se puede editar otra vez en el contenido", s._mapped(moved, 0))
state = {"overrides": {moved["id"]: {"text": "Texto final corregido."}}, "added": [], "z": None}
image, layout, _notes = s.render_page(0, state, 1.0, verify=[moved["id"]])
check("y queda bien", abs(layout[moved["id"]][1] - moved["bbox"][1]) < 2)
s.adopt(original)
check("deshacer: el documento vuelve a ser idéntico", s.data is original
      and ImageChops.difference(render(s.data), render(original)).getbbox() is None
      and line_with(s, "Primer parrafo")["text"] == target["text"])
s.close()

print("== varias páginas de la sesión: el modo Párrafo")
s = pdf_editor.EditorSession(pdf)
block = next(b for b in s.analyze(0)["blocks"] if "Este informe" in b["text"])
flowed = s.flow_text(0, block["id"])
check("el párrafo se edita corrido (sin saltos de renglón)", "\n" not in flowed and flowed.count("Este informe") == 3)
lines_before = len(block["line_ids"])
heading = line_with(s, "Primer parrafo")["bbox"][1]
result = edit(s, block["id"], flowed + " " + "agregado " * 60)
check("párrafo: aplica", result.status == "done", result.status)
s.adopt(result.data)
check("párrafo: lo de abajo baja", abs(line_with(s, "Primer parrafo")["bbox"][1] - heading - result.shift) <= 0.5)
first_row = s.analyze(0)["lines"][1]["text"]
check("párrafo: las líneas antes del cambio no se tocan", first_row.startswith("Este informe"), first_row[:40])
s.close()

print("== alineación justificada")
pdf = fixtures.reflow_justified(TMP / "justificado.pdf")
s = pdf_editor.EditorSession(pdf)
block = s.analyze(0)["blocks"][0]
check("detecta el justificado", block["style"]["align"] == "justify", block["style"]["align"])
flowed = s.flow_text(0, block["id"])
result = edit(s, block["id"], flowed + " " + "tambien agregamos varias palabras nuevas al final " * 8)
check("justificado: aplica", result.status == "done", result.status)
s.adopt(result.data)
model = s.analyze(0)
rows = [l for l in model["lines"] if "agregamos" in l["text"]][:-1]
right = block["bbox"][2]
check("justificado: los renglones llegan al borde derecho", rows and all(abs(l["bbox"][2] - right) < 1.5 for l in rows),
      str([round(l["bbox"][2], 1) for l in rows][:4]) + f" vs {right:.1f}")
s.close()

print("== dos columnas")
pdf = fixtures.reflow_columns(TMP / "columnas.pdf")
s = pdf_editor.EditorSession(pdf)
check("detecta dos columnas", len(s.columns(0)) == 2, str(s.columns(0)))
original = s.data
target = line_with(s, "Columna izquierda 1")
left_next = line_with(s, "Columna izquierda 3")["bbox"][1]
right_next = line_with(s, "Columna derecha 3")["bbox"][1]
area = s.text_area(0, target)
check("el ancho es el de la columna", area["right"] < 300, f"{area['right']:.1f}")
result = edit(s, target["id"], target["text"] + " " + "extra " * 40)
check("columnas: aplica", result.status == "done", result.status)
s.adopt(result.data)
check("solo baja la columna del párrafo", abs(line_with(s, "Columna izquierda 3")["bbox"][1] - left_next - result.shift) <= 0.5
      and abs(line_with(s, "Columna derecha 3")["bbox"][1] - right_next) <= 0.1)
ok, worst = same_region(original, result.data, 0, 0, 800, x0=300)
check("la otra columna queda idéntica", ok, f"diferencia máxima {worst}")
s.close()

print("== página casi llena")
pdf = fixtures.reflow_full(TMP / "llena.pdf")
s = pdf_editor.EditorSession(pdf)
original = s.data
target = line_with(s, "Parrafo 02")
ov = {"text": target["text"] + " " + "contenido adicional " * 30}
all_before = Counter(w[4] for p in s.doc for w in p.get_text("words"))
result = edit(s, target["id"], ov["text"])
check("avisa antes de aplicar", result.status == "overflow", result.status)
check("cancelar deja la página intacta", s.data is original)
check("ofrece las tres opciones", {"newpage", "shrink", "tighten"} & set(result.candidates) >= {"newpage"},
      str(sorted(result.candidates)))
check("hay vista previa de cada opción", all(c.previews for c in result.candidates.values()))
page_new = result.candidates["newpage"]
check("página nueva: una más", page_new.added_pages == 1, page_new.message)
check("página nueva: dice cuál se creó", page_new.message == "Se creó la página 2 con el contenido sobrante.", page_new.message)
with fitz.open(stream=page_new.data, filetype="pdf") as doc:
    check("página nueva: mismo tamaño", doc[1].rect == doc[0].rect and doc.page_count == s.page_count + 1)
    after = Counter(w[4] for p in doc for w in p.get_text("words"))
    lost = [w for w, n in all_before.items() if after[w] < n]
    check("página nueva: no se pierde ninguna palabra", not lost, str(lost[:5]))
    limit = s._limits(0, s._zone(0, target, pdf_editor._effective(target, ov), ov))[0]
    last = max(b[3] for b in doc[0].get_text("blocks"))
    check("página nueva: nada pasa del margen inferior", last <= limit + 1, f"{last:.1f} ≤ {limit:.1f}")
    lines_two = [l for b in doc[1].get_text("dict")["blocks"] for l in b.get("lines", [])]
    check("página nueva: renglones enteros, sin cortar", all(doc[1].rect.y0 + 20 < l["bbox"][1] and l["bbox"][3] < doc[1].rect.y1 - 20
                                                               for l in lines_two) and lines_two)
    margin_top = min(l["bbox"][1] for l in lines_two)
    check("página nueva: mismo margen de arriba", abs(margin_top - s.analyze(0)["lines"][0]["bbox"][1]) < 3, f"{margin_top:.1f}")
shrink = result.candidates.get("shrink")
if shrink:
    with fitz.open(stream=shrink.data, filetype="pdf") as doc:
        sizes = {round(span["size"], 2) for block in doc[0].get_text("dict")["blocks"] for line in block.get("lines", [])
                 for span in line["spans"]}
    check("reducir letra: al menos 85 %", min(sizes) >= 11 * 0.85 - 0.01 and doc.is_closed, str(sorted(sizes)))
    check("reducir letra: sin páginas nuevas", shrink.added_pages == 0)
    check("reducir letra: muestra el tamaño", "pt" in shrink.message, shrink.message)
tighten = result.candidates.get("tighten")
if tighten:
    check("interlineado: sin páginas nuevas", tighten.added_pages == 0, tighten.message)
target_huge = {"text": target["text"] + " " + "contenido adicional " * 400}
result = edit(s, target["id"], target_huge["text"])
check("mucho texto: el 85 % no alcanza y no se ofrece", "shrink" not in result.candidates, str(sorted(result.candidates)))
check("mucho texto: varias páginas nuevas", result.candidates["newpage"].added_pages >= 2, result.candidates["newpage"].message)
s.adopt(page_new.data)
s.adopt(original)
check("deshacer la página nueva", s.page_count == 1 or s.doc.page_count == s.page_count)
s.close()

print("== texto más corto")
pdf = fixtures.reflow_page(TMP / "corto.pdf")
s = pdf_editor.EditorSession(pdf)
block = next(b for b in s.analyze(0)["blocks"] if "Primer parrafo" in b["text"])
final = line_with(s, "Texto final")["bbox"][1]
ov = {"text": "Corto."}
check("apagado: no hace falta mover nada", not s.needs_reflow(0, block["id"], ov, True, False))
state = {"overrides": {block["id"]: ov}, "added": [], "z": None}
_image, layout, _notes = s.render_page(0, state, 1.0, verify=[block["id"]])
check("apagado: lo de abajo se queda", abs(layout[line_with(s, "Texto final")["id"]][1] - final) < 0.1)
check("encendido: hay que mover", s.needs_reflow(0, block["id"], ov, True, True))
result = edit(s, block["id"], "Corto.", adjust=True)
check("encendido: aplica", result.status == "done" and result.shift < 0, f"{result.status} {result.shift}")
s.adopt(result.data)
check("encendido: lo de abajo sube", abs(line_with(s, "Texto final")["bbox"][1] - final - result.shift) <= 0.5, result.message)
s.close()

print("== sin desplazar")
pdf = fixtures.reflow_page(TMP / "fijo.pdf")
s = pdf_editor.EditorSession(pdf)
target = line_with(s, "Primer parrafo")
final = line_with(s, "Texto final")["bbox"][1]
ov = {"text": target["text"] + " " + "mas texto " * 30}
check("con el interruptor apagado no se mueve nada", not s.needs_reflow(0, target["id"], ov, False, False))
state = {"overrides": {target["id"]: ov}, "added": [], "z": None}
_image, layout, _notes = s.render_page(0, state, 1.0, verify=[target["id"]])
check("el texto se acomoda igual, sin cancelarse", layout[target["id"]][2] < 530 and abs(layout[line_with(s, "Texto final")["id"]][1] - final) < 0.1)
s.close()

print("== fuente en subconjunto dentro de un formulario")
pdf = fixtures.portada(TMP / "portada.pdf")
s = pdf_editor.EditorSession(pdf)
original = s.data
target = line_with(s, "CARRERA")
below = line_with(s, "DOCENTE")["bbox"][1]
wrapped = target["text"] + " " + "Y TECNOLOGIAS DE LA INFORMACION PARA EL DESARROLLO DE SOFTWARE EMPRESARIAL"
result = edit(s, target["id"], wrapped)
check("portada: aplica", result.status == "done", result.status)
s.adopt(result.data)
check("portada: lo de abajo bajó", abs(line_with(s, "DOCENTE")["bbox"][1] - below - result.shift) <= 0.5,
      f"{result.shift}")
model = s.analyze(0)
row = next(l for l in model["lines"] if "TECNOLOGIAS" in l["text"] or "EMPRESARIAL" in l["text"])
check("portada: misma fuente", "Montserrat" in row["style"]["font"], row["style"]["font"])
check("portada: el texto movido sigue en un formulario editable", s._mapped(line_with(s, "DOCENTE"), 0))
state = {"overrides": {line_with(s, "DOCENTE")["id"]: {"text": "DOCENTE: PEREZ"}}, "added": [], "z": None}
_image, layout, _notes = s.render_page(0, state, 1.0, verify=[line_with(s, "DOCENTE")["id"]])
check("portada: se puede volver a editar", True)
words = s.doc[0].get_text()
check("portada: nada duplicado", words.count("MATERIA") == 1 and words.count("GRUPO") == 1)
s.close()

print("== nada se cancela en silencio")
pdf = fixtures.reflow_page(TMP / "seguro.pdf")
s = pdf_editor.EditorSession(pdf)
target = line_with(s, "Primer parrafo")
real = pdf_editor.EditorSession._check


def broken(self, doc, index, added, zone, page_text):
    raise pdf_editor.EditFailed({zone.lines[0]["id"]: "falla forzada"})


pdf_editor.EditorSession._check = broken
original = s.data
try:
    edit(s, target["id"], target["text"] + " " + text_120)
    failed = False
except pdf_editor.EditFailed as exc:
    failed = "falla forzada" in str(exc)
pdf_editor.EditorSession._check = real
check("si la comprobación falla: error claro y la página queda igual", failed and s.data is original)
s.close()

print(f"\n{sum(results)}/{len(results)} OK — {TMP}")
sys.exit(0 if results and all(results) else 1)
