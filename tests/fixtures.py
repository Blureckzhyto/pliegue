"""Documentos de prueba generados (nunca archivos personales).

`portada(path)` reproduce el caso real que motivó v1.3.4–1.3.5: una portada con
Montserrat-Bold 14,14 pt incrustada como SUBCONJUNTO (solo las letras usadas, todas
mayúsculas) y con el texto dentro de un Form XObject. Necesita la biblioteca de fuentes
(python tools/fetch_assets.py) para tomar Montserrat.

Si se define la variable de entorno PLIEGUE_PORTADA con la ruta de un PDF real, las
pruebas usan una copia de ese archivo en lugar del generado.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

LINES = [
    (60, 560, "ALUMNOS:"),
    (80, 580, "RAMIREZ TORRES ANA"),
    (60, 600, "GRUPO: 3B1"),
    (60, 620, "CARRERA: INGENIERÍA EN SISTEMAS COMPUTACIONALES"),
    (60, 640, "DOCENTE: CASTRO VEGA LUIS"),
    (60, 660, "MATERIA: PROGRAMACIÓN WEB"),
]


def montserrat_bold() -> bytes:
    from core import font_finder

    choice = font_finder.find_same(font_finder.describe("Montserrat-Bold"))
    if choice is None:
        raise RuntimeError("Falta Montserrat en assets/fonts/library: ejecuta python tools/fetch_assets.py")
    return font_finder.static_buffer(choice)


def portada(path: Path) -> Path:
    path = Path(path)
    real = os.environ.get("PLIEGUE_PORTADA")
    if real and Path(real).exists():
        shutil.copy(real, path)
        return path
    import fitz

    inner = fitz.open()
    page = inner.new_page(width=595, height=842)
    page.insert_font(fontname="MontB", fontbuffer=montserrat_bold())
    for x, y, text in LINES:
        page.insert_text((x, y), text, fontname="MontB", fontsize=14.14, color=(0.07, 0.07, 0.07))
    inner.subset_fonts()                    # subconjunto: solo las letras usadas
    outer = fitz.open()
    target = outer.new_page(width=595, height=842)
    target.draw_rect(fitz.Rect(0, 0, 595, 120), color=None, fill=(0.0, 0.3, 0.6))
    target.show_pdf_page(target.rect, inner, 0)   # el texto queda dentro de un Form XObject
    outer.save(path, garbage=3, deflate=True)
    return path


# --------------------------------------------------------------------------- texto que no cabe

LOREM = ("Este informe describe los resultados del trimestre con el detalle de cada región y explica "
         "las diferencias frente al periodo anterior para que el equipo pueda tomar decisiones. ")


def _styles():
    from reportlab.lib.enums import TA_JUSTIFY
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet

    sheet = getSampleStyleSheet()
    body = ParagraphStyle("Cuerpo", parent=sheet["BodyText"], fontName="Helvetica", fontSize=11,
                          leading=14, textColor="#223344", spaceAfter=8)
    return {"title": sheet["Title"], "h2": sheet["Heading2"], "body": body,
            "just": ParagraphStyle("Just", parent=body, alignment=TA_JUSTIFY)}


def _picture(path: Path) -> Path:
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (240, 120), "#2F7FE0")
    draw = ImageDraw.Draw(image)
    draw.ellipse((20, 20, 100, 100), fill="#FFE600")
    draw.rectangle((130, 30, 220, 90), fill="#E5484D")
    image.save(path)
    return path


def _table():
    from reportlab.lib import colors
    from reportlab.platypus import Table, TableStyle

    rows = [["Región", "Enero", "Febrero"], ["Norte", "1,200", "1,310"], ["Centro", "1,337", "1,400"],
            ["Sur", "1,455", "1,490"]]
    table = Table(rows, colWidths=[140, 90, 90])
    table.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.6, colors.HexColor("#556677")),
                               ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EEF1F5")),
                               ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold")]))
    return table


def reflow_page(path: Path) -> Path:
    """Una columna: título, párrafos y, debajo, otro título, una imagen y una tabla."""
    from reportlab.lib.pagesizes import A4
    from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer

    s = _styles()
    picture = _picture(Path(path).with_suffix(".png"))
    SimpleDocTemplate(str(path), pagesize=A4, leftMargin=72, rightMargin=72, topMargin=72,
                      bottomMargin=72).build([
        Paragraph("Informe trimestral", s["title"]), Paragraph(LOREM * 3, s["body"]),
        Paragraph("Primer parrafo de cierre. " + LOREM, s["body"]),
        Paragraph("Resultados por region", s["h2"]), Image(str(picture), width=180, height=90),
        Spacer(1, 10), _table(), Spacer(1, 10), Paragraph("Texto final debajo de la tabla.", s["body"])])
    return path


def reflow_columns(path: Path) -> Path:
    """Dos columnas con párrafos: solo debe moverse la columna editada."""
    from reportlab.lib.pagesizes import A4
    from reportlab.platypus import BaseDocTemplate, Frame, FrameBreak, PageTemplate, Paragraph

    s = _styles()
    doc = BaseDocTemplate(str(path), pagesize=A4)
    frames = [Frame(60, 60, 225, 720, id="a", leftPadding=0, rightPadding=0),
              Frame(310, 60, 225, 720, id="b", leftPadding=0, rightPadding=0)]
    doc.addPageTemplates([PageTemplate(id="dos", frames=frames)])
    left = [Paragraph(f"Columna izquierda {n}. " + LOREM, s["body"]) for n in range(1, 5)]
    right = [Paragraph(f"Columna derecha {n}. " + LOREM, s["body"]) for n in range(1, 5)]
    doc.build(left + [FrameBreak()] + right)
    return path


def reflow_full(path: Path) -> Path:
    """Una página casi llena: empujar el contenido pasa del margen inferior."""
    from reportlab.lib.pagesizes import A4
    from reportlab.platypus import Paragraph, SimpleDocTemplate

    s = _styles()
    SimpleDocTemplate(str(path), pagesize=A4, leftMargin=72, rightMargin=72, topMargin=72, bottomMargin=72).build(
        [Paragraph(f"Parrafo {n:02d}. " + LOREM, s["body"]) for n in range(1, 15)])
    return path


def reflow_justified(path: Path) -> Path:
    from reportlab.lib.pagesizes import A4
    from reportlab.platypus import Paragraph, SimpleDocTemplate

    s = _styles()
    SimpleDocTemplate(str(path), pagesize=A4, leftMargin=72, rightMargin=72, topMargin=72, bottomMargin=72).build(
        [Paragraph(LOREM * 4, s["just"]), Paragraph("Segundo parrafo. " + LOREM * 2, s["just"])])
    return path


def reflow_annots(path: Path) -> Path:
    """La página de reflow_page con un enlace arriba, un enlace y una nota abajo y un resaltado."""
    import fitz

    plain = Path(path).with_name(Path(path).stem + "_base.pdf")
    reflow_page(plain)
    doc = fitz.open(plain)
    page = doc[0]
    top = page.search_for("Informe trimestral")[0]
    below = page.search_for("Texto final")[0]
    heading = page.search_for("Resultados por region")[0]
    page.insert_link({"kind": fitz.LINK_URI, "from": top, "uri": "https://example.com/arriba"})
    page.insert_link({"kind": fitz.LINK_URI, "from": below, "uri": "https://example.com/abajo"})
    page.add_text_annot(fitz.Point(heading.x1 + 20, heading.y0), "Nota de prueba")
    page.add_highlight_annot(heading)
    doc.save(path)
    doc.close()
    return path


def reflow_beside(path: Path) -> Path:
    """Un párrafo angosto con una imagen a la derecha que empieza más arriba y termina más abajo:
    cruza la línea de corte."""
    from reportlab.pdfgen import canvas

    picture = _picture(Path(path).with_suffix(".png"))
    c = canvas.Canvas(str(path), pagesize=(595, 842))
    c.setFont("Helvetica", 11)
    y = 720
    for n in range(6):
        c.drawString(72, y, f"Linea {n + 1} del parrafo junto a la imagen, con su texto.")
        y -= 14
    y -= 14
    c.drawString(72, y, "Linea debajo del parrafo, todavia al lado de la imagen.")
    c.drawImage(str(picture), 380, 560, width=150, height=180)
    c.drawString(72, 500, "Texto final de la pagina.")
    c.save()
    return path
