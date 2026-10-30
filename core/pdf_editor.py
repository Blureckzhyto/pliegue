"""Editor de PDF real (núcleo, sin UI).

Detecta el texto y las imágenes originales de cada página, permite reemplazar,
mover, reordenar o borrar esos elementos y agregar elementos nuevos, y guarda el
resultado conservando el estilo original:

* El texto modificado se quita SOLO dentro de su caja con una redacción de
  texto (las imágenes y los gráficos de debajo no se tocan) y se vuelve a
  escribir en la misma línea base, con el mismo tamaño, color y rotación.
* Fuentes, en este orden: la fuente incrustada en el PDF si tiene todos los
  glifos del texto nuevo (suelen ser subconjuntos), la fuente de Windows más
  parecida, y por último una fuente incluida. Cada sustitución se informa.
* Las páginas escaneadas se analizan con el Tesseract incluido.

La vista previa del editor usa exactamente el mismo camino que el guardado
(`render_page`), así que lo que se ve es lo que se guarda.

Coordenadas: puntos PDF con origen arriba a la izquierda (convención de
PyMuPDF), sobre una copia normalizada del documento sin /Rotate.
"""
from __future__ import annotations

import copy
import os
import re
import threading
import unicodedata
from io import BytesIO
from pathlib import Path

from . import errors, font_finder
from . import pdf_textops as tops
from .edit_sign import ENCRYPTED_MSG
from .pdf_flow import FlowMixin

try:
    import pymupdf as fitz
except ImportError:  # PyMuPDF < 1.24
    import fitz


class SubstitutionNeeded(Exception):
    """Al guardar, algún texto editado necesita una fuente distinta de la original.
    `items`: [{"page", "uid", "font", "missing", "substitute"}]."""

    def __init__(self, items):
        super().__init__("Hace falta confirmar una sustitución de fuente.")
        self.items = items


class EditFailed(Exception):
    """Una edición no quedó bien aplicada (falló al reinsertar el texto, o el texto nuevo no
    aparece o no se ve en la página). `failures`: {id: motivo}. Quien la recibe deja el
    texto original: la edición es atómica."""

    def __init__(self, failures: dict):
        self.failures = failures
        super().__init__("; ".join(dict.fromkeys(failures.values())))


class _Unsupported(Exception):
    """El texto no se puede editar en el propio contenido: se usa el camino anterior."""


class _Pending(Exception):
    """Falta que el usuario acepte una fuente parecida: el texto queda como estaba."""

# flags de los spans de PyMuPDF
F_ITALIC, F_SERIF, F_MONO, F_BOLD = 2, 4, 8, 16
OCR_MIN_CHARS = 20          # menos texto que esto + imágenes => página escaneada
OCR_DPI = 300
LIFT_EPS = 0.01

# familias de Windows: (normal, negrita, cursiva, negrita cursiva)
WINDOWS_FONTS = {
    "Arial": ("arial.ttf", "arialbd.ttf", "ariali.ttf", "arialbi.ttf"),
    "Times New Roman": ("times.ttf", "timesbd.ttf", "timesi.ttf", "timesbi.ttf"),
    "Courier New": ("cour.ttf", "courbd.ttf", "couri.ttf", "courbi.ttf"),
    "Calibri": ("calibri.ttf", "calibrib.ttf", "calibrii.ttf", "calibriz.ttf"),
    "Cambria": ("cambria.ttc", "cambriab.ttf", "cambriai.ttf", "cambriaz.ttf"),
    "Segoe UI": ("segoeui.ttf", "segoeuib.ttf", "segoeuii.ttf", "segoeuiz.ttf"),
    "Verdana": ("verdana.ttf", "verdanab.ttf", "verdanai.ttf", "verdanaz.ttf"),
    "Georgia": ("georgia.ttf", "georgiab.ttf", "georgiai.ttf", "georgiaz.ttf"),
    "Tahoma": ("tahoma.ttf", "tahomabd.ttf", "tahoma.ttf", "tahomabd.ttf"),
    "Trebuchet MS": ("trebuc.ttf", "trebucbd.ttf", "trebucit.ttf", "trebucbi.ttf"),
}
BUNDLED_FAMILY = "Plus Jakarta Sans"
BUNDLED_FONTS = ("PlusJakartaSans-Regular.ttf", "PlusJakartaSans-Bold.ttf",
                 "PlusJakartaSans-Italic.ttf", "PlusJakartaSans-BoldItalic.ttf")
# palabra clave en el nombre de la fuente del PDF -> familia de Windows
FONT_KEYWORDS = [
    (("arial", "helvetica", "liberationsans", "nimbussans", "arimo"), "Arial"),
    (("timesnewroman", "times", "tinos", "liberationserif", "nimbusroman"), "Times New Roman"),
    (("couriernew", "courier", "cousine", "liberationmono", "nimbusmono"), "Courier New"),
    (("calibri", "carlito"), "Calibri"), (("cambria", "caladea"), "Cambria"),
    (("segoeui", "segoe"), "Segoe UI"), (("verdana",), "Verdana"), (("georgia",), "Georgia"),
    (("tahoma",), "Tahoma"), (("trebuchet",), "Trebuchet MS"),
]
FAMILY_CHOICES = ["Original"] + list(WINDOWS_FONTS) + [BUNDLED_FAMILY]


def _hex(color: int) -> str:
    return f"#{color & 0xFFFFFF:06X}"


def _rgb(hex_color: str) -> tuple[float, float, float]:
    h = hex_color.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


def _clean_font_name(name: str) -> str:
    return re.sub(r"^[A-Z]{6}\+", "", name or "")


def windows_family_for(font_name: str, flags: int = 0) -> str:
    """Familia de Windows más parecida a una fuente del PDF."""
    key = re.sub(r"[^a-z]", "", _clean_font_name(font_name).lower())
    for keywords, family in FONT_KEYWORDS:
        if any(k in key for k in keywords):
            return family
    if flags & F_MONO:
        return "Courier New"
    # el indicador "serif" de PyMuPDF no es fiable (marca así a fuentes sin serifa como
    # Montserrat): la clase sale del nombre, y ante la duda es una sans
    if font_finder.describe(font_name).kind == "serif":
        return "Times New Roman"
    return "Arial"


def _font_dir() -> Path:
    return Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"


def _variant(bold: bool, italic: bool) -> int:
    return (1 if bold else 0) + (2 if italic else 0)


# --------------------------------------------------------------------------- sesión

class EditorSession(FlowMixin):
    """Copia normalizada del PDF abierta con PyMuPDF + análisis por página.

    El documento de la sesión solo se usa bajo `self.lock` (PyMuPDF no es
    seguro entre hilos); las vistas previas y el guardado abren su propia copia.
    Una edición que mueve contenido (pdf_flow.py) se integra al documento base: `adopt`
    cambia los bytes de la sesión y `self.data` antes del cambio sirve para deshacerlo.
    """

    def __init__(self, src):
        from pypdf import PdfReader, PdfWriter

        self.src = Path(src)
        try:
            reader = PdfReader(str(src))
        except Exception as exc:
            raise RuntimeError(f"No se pudo leer el PDF: {exc}") from exc
        if reader.is_encrypted:
            raise RuntimeError(ENCRYPTED_MSG)
        if any(page.rotation for page in reader.pages):
            # se integra /Rotate al contenido: todas las coordenadas quedan "como se ven"
            writer = PdfWriter(clone_from=reader)
            for page in writer.pages:
                if page.rotation:
                    page.transfer_rotation_to_content()
            buf = BytesIO()
            writer.write(buf)
            self.data = buf.getvalue()
        else:
            self.data = self.src.read_bytes()
        self._setup()

    @classmethod
    def from_bytes(cls, src, data: bytes) -> "EditorSession":
        """Sesión sobre un contenido ya normalizado (el documento con cambios integrados)."""
        session = cls.__new__(cls)
        session.src = Path(src)
        session.data = data
        session._setup()
        return session

    def _setup(self):
        self.lock = threading.Lock()
        self.doc = fitz.open(stream=self.data, filetype="pdf")
        self.page_count = self.doc.page_count
        self._font_cache: dict[tuple, tuple] = {}
        self._ops_lock = threading.Lock()
        self.font_log: list[str] = []   # cada decisión de fuente (también en el registro)
        self._reset_analysis()

    def _reset_analysis(self):
        self._models: dict[int, dict] = {}
        self._pk = None                                  # el mismo PDF abierto con pikepdf
        self._fonts: dict[int, tops.FontInfo] = {}       # fuentes del PDF por número de objeto
        self._texts: dict[int, tops.PageText] = {}       # glifos ubicados por página
        self._form_uses: dict[int, int] | None = None
        self._full_fonts: dict = {}

    def adopt(self, data: bytes) -> None:
        """Cambia el documento base por `data` (una edición integrada, o deshacerla)."""
        with self.lock:
            if data is self.data:
                return
            self.doc.close()
            if self._pk:
                self._pk.close()
            self.data = data
            self.doc = fitz.open(stream=data, filetype="pdf")
            self.page_count = self.doc.page_count
            self._reset_analysis()

    def close(self):
        with self.lock:
            self.doc.close()
            if self._pk:
                self._pk.close()

    def page_size(self, index: int) -> tuple[float, float]:
        with self.lock:
            rect = self.doc[index].rect
        return rect.width, rect.height

    # ------------------------------------------------------------------ análisis

    def analyze(self, index: int, ocr_lang: str = "spa+eng") -> dict:
        """Modelo de la página: líneas, bloques e imágenes originales."""
        if index in self._models:
            return self._models[index]
        with self.lock:
            page = self.doc[index]
            text = page.get_text("rawdict", flags=fitz.TEXT_PRESERVE_WHITESPACE
                                 | fitz.TEXT_PRESERVE_IMAGES)
            image_info = page.get_image_info(xrefs=True)
            fonts = {}
            for xref, _ext, _type, basefont, *_rest in page.get_fonts(full=True):
                fonts.setdefault(_clean_font_name(basefont), xref)
            plain = page.get_text().strip()
            width, height = page.rect.width, page.rect.height
            page_text = self._interpret(index)
        # cada carácter de PyMuPDF con el glifo del contenido que lo dibuja
        chars = [(ch["origin"], ch["c"]) for block in text["blocks"] if block["type"] == 0
                 for line in block["lines"] for span in line["spans"] for ch in span["chars"]]
        gids = iter(tops.match_chars(page_text, chars) if page_text.ok else [None] * len(chars))
        for block in text["blocks"]:
            if block["type"] == 0:
                for line in block["lines"]:
                    for span in line["spans"]:
                        span["text"] = "".join(ch["c"] for ch in span["chars"])
                        span["gids"] = [next(gids) for _ in span["chars"]]
        model = {"index": index, "width": width, "height": height, "ocr": False,
                 "lines": [], "blocks": [], "images": []}
        z = 0
        for bi, block in enumerate(text["blocks"]):
            if block["type"] == 1:
                image = self._match_image(block["bbox"], image_info, bi, z)
                if image:
                    model["images"].append(image)
                z += 1
                continue
            unit = self._text_block(block, bi, fonts, z, page_text if page_text.ok else None)
            if unit:
                model["blocks"].append(unit["block"])
                model["lines"].extend(unit["lines"])
            z += 1
        if len(plain) < OCR_MIN_CHARS and model["images"]:
            self._ocr_page(index, model, ocr_lang)
        self._models[index] = model
        return model

    def _interpret(self, index: int) -> tops.PageText:
        """Glifos de la página ubicados en su flujo de contenido (con self.lock tomado)."""
        if index in self._texts:
            return self._texts[index]
        if self._pk is None:
            import pikepdf

            try:
                self._pk = pikepdf.open(BytesIO(self.data))
            except Exception:
                errors.record("pikepdf no pudo abrir el PDF (se edita con el camino anterior)")
                self._pk = False
        page_text = tops.PageText()
        if self._pk:
            try:
                page_text = tops.interpret(self.doc, self._pk, index, self._fonts)
                page_text.op_of = {g.gid: op for op in page_text.ops for g in op.glyphs}
            except Exception:
                errors.record(f"No se pudo interpretar el contenido de la página {index + 1}")
                page_text = tops.PageText()
        self._texts[index] = page_text
        return page_text

    @staticmethod
    def _span_font(page_text, span):
        if page_text is None or not page_text.ok:
            return None
        for gid in span.get("gids", []):
            if gid is not None and gid in page_text.op_of:
                return page_text.op_of[gid].state.font
        return None

    def _editable_stream(self, key) -> bool:
        """El texto de la página siempre; el de un formulario solo si se dibuja una sola vez
        en todo el documento (si no, cambiarlo afectaría otros lugares)."""
        if key == "page":
            return True
        if self._form_uses is None:
            uses: dict[int, int] = {}
            with self.lock:
                for i in range(self.doc.page_count):
                    for xref, *_rest in self.doc.get_page_xobjects(i):
                        uses[xref] = uses.get(xref, 0) + 1
            self._form_uses = uses
        return self._form_uses.get(key, 0) <= 1

    def _mapped(self, unit, index) -> bool:
        """¿Se puede editar este texto en el propio contenido (con su fuente original)?"""
        page_text = self._texts.get(index)
        if page_text is None or not page_text.ok:
            return False
        lines = [unit] if unit.get("spans") is not None else [
            line for line in self.analyze(index)["lines"] if line["id"] in unit.get("line_ids", [])]
        if not lines:
            return False
        for line in lines:
            for span in line.get("spans") or []:
                for char, gid in zip(span["text"], span["gids"]):
                    if gid is None:
                        if char.strip():
                            return False
                        continue
                    op = page_text.op_of[gid]
                    font = op.state.font
                    if font is None or not font.supported or not self._editable_stream(op.stream):
                        return False
        return True

    @staticmethod
    def _match_image(bbox, image_info, number, z):
        best = None
        for info in image_info:
            if info.get("xref", 0) <= 0:
                continue
            if all(abs(a - b) < 1.0 for a, b in zip(info["bbox"], bbox)):
                best = info
                break
        if best is None:
            return None
        a, b, c, d, _e, _f = best["transform"]
        movable = abs(b) < 1e-3 and abs(c) < 1e-3 and a > 0 and d > 0
        return {"id": f"I{number}", "kind": "image", "xref": best["xref"],
                "bbox": list(bbox), "z": z, "movable": movable, "source": "pdf"}

    def _text_block(self, block, bi, fonts, z, page_text=None):
        lines = []
        for li, line in enumerate(block["lines"]):
            spans = [s for s in line["spans"] if s["text"]]
            if not spans or not "".join(s["text"] for s in spans).strip():
                continue
            text = "".join(s["text"] for s in spans)
            main = max(spans, key=lambda s: len(s["text"].strip()))
            style = self._style(main, fonts, line.get("dir", (1, 0)))
            info = self._span_font(page_text, main)
            if info is not None:  # nombre y objeto reales de la fuente del PDF
                style["font"], style["font_xref"] = info.name, info.objnum
                style["font_objnum"] = info.objnum
            uid = f"L{bi}_{li}"
            records = []
            for s in spans:
                records.append({
                    "text": s["text"], "gids": s.get("gids", [None] * len(s["text"])),
                    "font": _clean_font_name(s["font"]), "size": s["size"],
                    "color": _hex(s["color"]), "flags": s["flags"], "bbox": list(s["bbox"]),
                    "origin": list(s["origin"]), "space_w": _space_width(s),
                })
                span_font = self._span_font(page_text, s)
                if span_font is not None:
                    records[-1]["font"], records[-1]["font_objnum"] = span_font.name, span_font.objnum
                if page_text is not None:
                    for gid in records[-1]["gids"]:
                        if gid is not None:
                            page_text.glyphs[gid].owner = uid
            lines.append({
                "id": uid, "kind": "text", "level": "line", "block_id": f"B{bi}",
                "bbox": list(line["bbox"]), "text": text.rstrip(),
                "lines": [{"text": text.rstrip(), "origin": list(spans[0]["origin"]),
                           "bbox": list(line["bbox"])}],
                "spans": records, "style": style, "source": "pdf", "z": z,
            })
        if not lines:
            return None
        bbox = [min(l["bbox"][0] for l in lines), min(l["bbox"][1] for l in lines),
                max(l["bbox"][2] for l in lines), max(l["bbox"][3] for l in lines)]
        style = dict(lines[0]["style"])
        style["align"] = _detect_alignment([l["bbox"] for l in lines], bbox)
        baselines = [l["lines"][0]["origin"][1] for l in lines]
        gaps = [b - a for a, b in zip(baselines, baselines[1:]) if b > a]
        style["line_height"] = sorted(gaps)[len(gaps) // 2] if gaps else style["size"] * 1.2
        block_unit = {
            "id": f"B{bi}", "kind": "text", "level": "block", "bbox": bbox,
            "text": "\n".join(l["text"] for l in lines),
            "lines": [l["lines"][0] for l in lines], "line_ids": [l["id"] for l in lines],
            "style": style, "source": "pdf", "z": z,
        }
        for line in lines:
            line["style"]["align"] = "left"
            line["style"]["line_height"] = style["line_height"]
        return {"block": block_unit, "lines": lines}

    @staticmethod
    def _style(span, fonts, direction):
        flags = span["flags"]
        name = _clean_font_name(span["font"])
        return {
            "font": name, "font_xref": fonts.get(name, 0), "size": span["size"],  # sin redondear
            "color": _hex(span["color"]), "bold": bool(flags & F_BOLD) or "bold" in name.lower(),
            "italic": bool(flags & F_ITALIC) or any(k in name.lower() for k in ("italic", "oblique")),
            "serif": bool(flags & F_SERIF), "mono": bool(flags & F_MONO),
            "family": windows_family_for(name, flags), "requested_family": "Original",
            "orig_bold": bool(flags & F_BOLD) or "bold" in name.lower(),
            "orig_italic": bool(flags & F_ITALIC) or any(k in name.lower() for k in ("italic", "oblique")),
            "dir": [round(direction[0], 4), round(direction[1], 4)],
            "align": "left", "line_height": span["size"] * 1.2,
        }

    # ------------------------------------------------------------------ OCR

    def _ocr_page(self, index, model, lang):
        from .ocr import tesseract_paths
        from PIL import Image
        import pytesseract

        exe, tessdata = tesseract_paths()
        pytesseract.pytesseract.tesseract_cmd = exe
        if tessdata:
            os.environ["TESSDATA_PREFIX"] = tessdata
        with self.lock:
            pix = self.doc[index].get_pixmap(matrix=fitz.Matrix(OCR_DPI / 72, OCR_DPI / 72),
                                             alpha=False)
        image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        try:
            hocr = pytesseract.image_to_pdf_or_hocr(image, extension="hocr", lang=lang,
                                                    config=f"--dpi {OCR_DPI}").decode("utf-8")
        except Exception as exc:
            raise RuntimeError(f"No se pudo analizar la página escaneada: {exc}") from exc
        units = parse_hocr(hocr, image, OCR_DPI / 72)
        model["ocr"] = True
        model["lines"], model["blocks"] = units["lines"], units["blocks"]
        for unit in model["lines"] + model["blocks"]:
            unit["z"] = len(model["images"]) + 1  # el texto escaneado va sobre la imagen

    # ------------------------------------------------------------------ fuentes

    def resolve_font(self, style: dict, text: str):
        """Fuente para el camino de dibujo con TextWriter (texto nuevo, OCR, o cuando el
        usuario pide otra fuente). Devuelve (fitz.Font, nombre usado, sustituida, nombre
        original). Nunca cae en una fuente por defecto de PyMuPDF: el orden es la fuente
        incrustada, la misma familia instalada o de la biblioteca y, solo al final, una
        parecida por clasificación."""
        original = style.get("font") or "?"
        requested = style.get("requested_family", "Original")
        bold, italic = bool(style.get("bold")), bool(style.get("italic"))
        needed = {ch for ch in text if ch not in "\n\r\t"}
        key = (original, style.get("font_objnum"), requested, bold, italic, frozenset(needed))
        if key in self._font_cache:
            return self._font_cache[key]

        def covers(font):
            return all(font.has_glyph(ord(ch)) for ch in needed if ch not in " \xa0")

        info = self._fonts.get(style.get("font_objnum") or -1)
        weight_changed = (bold != style.get("orig_bold", bold)) or (italic != style.get("orig_italic", italic))
        result = None
        if requested == "Original" and info is not None and info.buffer and not weight_changed:
            try:
                font = fitz.Font(fontbuffer=info.buffer)
                if covers(font):
                    result = (font, original, False, original)
                    self._log(f"«{original}»: fuente incrustada (tiene todos los caracteres)")
            except Exception:
                errors.record(f"No se pudo cargar la fuente incrustada {original}")
        if result is None:
            if requested != "Original":
                desc = font_finder.FontDescription(name=requested, family=requested,
                                                   weight=700 if bold else 400, italic=italic)
            elif info is not None:
                desc = copy.copy(info.description)
            else:   # texto escaneado o sin fuente del PDF: lo que se ve en la imagen
                desc = font_finder.describe(style.get("family") or original)
            if weight_changed or requested != "Original":
                desc.weight, desc.italic = (700 if bold else 400), italic
            choice = font_finder.find_same(desc)
            substitute = False
            if choice is None:
                choice = font_finder.find_similar(desc)
                substitute = requested == "Original"
            if choice is not None:
                font = fitz.Font(fontbuffer=font_finder.static_buffer(choice, text))
                if covers(font):
                    result = (font, choice.label, substitute, original)
            if result is None:   # último recurso, pero nunca una serif por defecto
                path = _font_dir() / WINDOWS_FONTS["Arial"][_variant(bold, italic)]
                result = (fitz.Font(fontfile=str(path)), "Arial", True, original)
            self._log(f"«{original}» ({'pedida: ' + requested if requested != 'Original' else 'original'})"
                      f" -> {result[1]}{' (sustituta)' if result[2] else ''}")
        self._font_cache[key] = result
        return result

    def _log(self, message: str) -> None:
        self.font_log.append(message)
        font_finder.log.info(message)

    def font_report(self, style: dict, text: str) -> str | None:
        """Texto para la barra de propiedades si la fuente se sustituye."""
        _font, used, substituted, original = self.resolve_font(style, text)
        return f"Fuente sustituida: {original} → {used}" if substituted else None

    # ------------------------------------------------------------------ estilo cercano

    def nearby_style(self, index: int, x: float, y: float, max_distance: float = 60):
        """Estilo de la línea de texto más cercana a (x, y), para 'Añadir texto'."""
        model = self.analyze(index)
        best, best_d = None, max_distance
        for line in model["lines"]:
            x0, y0, x1, y1 = line["bbox"]
            dx = 0 if x0 <= x <= x1 else min(abs(x - x0), abs(x - x1))
            dy = 0 if y0 <= y <= y1 else min(abs(y - y0), abs(y - y1))
            d = (dx * dx + dy * dy) ** 0.5
            if d < best_d:
                best, best_d = line, d
        return copy.deepcopy(best["style"]) if best else None

    # ------------------------------------------------------------------ vista previa y guardado

    def render_page(self, index: int, state: dict | None, zoom: float, verify=(), kept_out=None):
        """Renderiza la página con las ediciones aplicadas.

        Devuelve (imagen PIL, {id: bbox actual}, avisos de fuentes). Un texto que solo se
        podría escribir con una fuente parecida (sustitución) se muestra ORIGINAL hasta
        que el usuario la acepte (`font_ok` en sus cambios). Los textos de `verify` (recién
        confirmados) se comprueban: si el texto nuevo no está o no se ve, EditFailed.
        `kept_out` (lista) recibe los textos que siguen como el original (falta aceptar
        la fuente): esos todavía no se pueden comprobar."""
        from PIL import Image

        doc = fitz.open(stream=self.data, filetype="pdf")
        try:
            layout, notes, kept = {}, [], []
            if state:
                layout, notes = self._apply_page(doc, index, state, kept)
            pix = doc[index].get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
            image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            if kept_out is not None:
                kept_out.extend(item["uid"] for item in kept)
            if state and verify:
                self._verify(doc, index, state, layout, {item["uid"] for item in kept}, verify,
                             image, zoom)
        finally:
            doc.close()
        return image, layout, notes

    def _verify(self, doc, index, state, layout, kept, uids, image=None, zoom=1.0) -> None:
        """Comprueba que cada texto editado de `uids` quedó en la página (texto extraíble)
        y que se ve (hay tinta en su caja). Lanza EditFailed con el motivo de cada uno."""
        if image is None:
            from PIL import Image

            zoom = 1.0
            pix = doc[index].get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
            image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        model = self.analyze(index)
        originals = {u["id"]: u for u in model["blocks"] + model["lines"]}
        added = {a["id"]: a for a in state.get("added", [])}
        overrides = state.get("overrides", {})
        page_text = None
        failures = {}
        before = []   # la página sin cambios (solo si hace falta comparar)

        def visible_before(unit) -> bool:
            """¿El texto original se veía? (texto invisible a propósito, p. ej. una capa de
            OCR, no cuenta como edición que desapareció)."""
            if not before:
                from PIL import Image

                with fitz.open(stream=self.data, filetype="pdf") as original:
                    pix = original[index].get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
                    before.append(Image.frombytes("RGB", (pix.width, pix.height), pix.samples))
            return _has_ink(before[0], unit["bbox"], zoom, len(_norm_text(unit["text"])))

        for uid in uids:
            if uid in kept:          # espera a que se acepte la fuente: sigue el original
                continue
            if uid in originals:
                element = _effective(originals[uid], overrides.get(uid, {}))
            else:
                element = added.get(uid)
            if not element or element.get("kind") != "text" or element.get("deleted"):
                continue
            wanted = _no_hyphens(element.get("text", ""))   # al partir una palabra se agrega un guion
            if not wanted:
                continue
            if page_text is None:
                page_text = _no_hyphens(doc[index].get_text("text"))
            if wanted not in page_text:
                failures[uid] = "el texto nuevo no quedó en la página"
                continue
            box = layout.get(uid) or element.get("bbox")
            if (box and uid in originals and not _has_ink(image, box, zoom, len(wanted))
                    and visible_before(originals[uid])):
                failures[uid] = "el texto nuevo no se ve en la página"
        if failures:
            raise EditFailed(failures)

    def pending_substitutions(self, index: int, state: dict) -> list[dict]:
        """Textos editados de la página que necesitan una fuente parecida y todavía no se
        aceptaron: [{"page", "uid", "font", "missing", "substitute", "text", ...}]."""
        pending: list[dict] = []
        doc = fitz.open(stream=self.data, filetype="pdf")
        try:
            self._apply_page(doc, index, state, pending)
        finally:
            doc.close()
        return pending

    def save(self, output, states: dict[int, dict], page_order: list[int],
             rotations: dict[int, int], progress=None,
             allow_substitution: bool = False) -> tuple[Path, list[str]]:
        """Guarda el PDF editado. `page_order` = índices originales que quedan,
        en su nuevo orden. Devuelve (ruta, avisos de fuentes).

        Si algún texto editado solo se puede escribir con una fuente parecida (ni la
        incrustada, ni la misma familia instalada o de la biblioteca tienen todos los
        caracteres) y no se aceptó antes, no se guarda nada y se lanza
        SubstitutionNeeded, salvo con `allow_substitution`."""
        progress = progress or (lambda *_a: None)
        if not page_order:
            raise ValueError("No quedan páginas para guardar.")
        doc = fitz.open(stream=self.data, filetype="pdf")
        notes: list[str] = []
        substitutions: list[dict] = []
        try:
            edited = [i for i in range(doc.page_count) if states.get(i)]
            for n, index in enumerate(edited):
                progress(0.8 * n / max(1, len(edited)), f"Aplicando cambios en la página {index + 1}…")
                before = len(substitutions)
                layout, page_notes = self._apply_page(doc, index, states[index], substitutions,
                                                      accept_all=allow_substitution)
                notes += [f"Página {index + 1}: {note}" for note in page_notes]
                if not substitutions:   # nunca se guarda un texto editado que no quedó
                    state = states[index]
                    edited_text = [uid for uid, ov in state.get("overrides", {}).items()
                                   if ov.get("text") is not None]
                    edited_text += [a["id"] for a in state.get("added", []) if a.get("kind") == "text"]
                    self._verify(doc, index, state, layout,
                                 {item["uid"] for item in substitutions[before:]}, edited_text)
            if substitutions:
                raise SubstitutionNeeded(substitutions)
            for index, degrees in rotations.items():
                if degrees % 360:
                    doc[index].set_rotation(degrees % 360)
            if page_order != list(range(doc.page_count)):
                doc.select(page_order)
            progress(0.9, "Guardando…")
            doc.save(str(output), garbage=3, deflate=True)
        finally:
            doc.close()
        progress(1.0, None)
        return Path(output), sorted(set(notes))

    # ------------------------------------------------------------------ aplicar una página

    def _apply_page(self, doc, index: int, state: dict, substitutions: list | None = None,
                    accept_all: bool = False):
        """Aplica los cambios de una página. Las sustituciones de fuente que falta aceptar
        se agregan a `substitutions` y esos textos quedan como estaban."""
        model = self.analyze(index)
        page = doc[index]
        overrides = state.get("overrides", {})
        added = state.get("added", [])
        originals = {u["id"]: u for u in model["blocks"] + model["lines"] + model["images"]}
        substitutions = [] if substitutions is None else substitutions

        default_order = self.default_order(model, state)
        order = state.get("z") or default_order
        edited = {uid for uid, ov in overrides.items() if uid in originals and _is_change(ov)}
        lifted = _lifted(order, default_order, originals)
        changed = edited | lifted
        covered = self._covered_by_lower(order, changed, originals, overrides, added)
        changed |= covered

        notes: list[str] = []
        layout: dict[str, list] = {}
        base = [uid for uid in order if uid in originals and uid not in changed]
        # sin originales intactos todo va arriba (debajo quedaría tapado por gráficos no rastreados)
        lowest_base = min((order.index(uid) for uid in base), default=-1)

        # 1) texto original ubicado en el contenido: cambios mínimos con su propia fuente
        page_text = self._texts.get(index)
        rewriter = tops.Rewriter(page_text) if page_text is not None and page_text.ok else None
        raw_done: set[str] = set()      # resuelto del todo en el contenido
        raw_removed: set[str] = set()   # quitado con precisión; se dibuja como antes (estilo nuevo)
        kept: set[str] = set()          # sustitución sin aceptar: queda el original
        layer_runs: dict[str, list] = {}  # texto que cambia de capa: se dibuja en su lugar del orden
        fonts_added: dict = {}
        for uid in [u for u in order if u in changed]:
            unit = originals[uid]
            if (rewriter is None or unit["kind"] != "text" or unit["source"] != "pdf"
                    or not self._mapped(unit, index)):
                continue
            ov = overrides.get(uid, {})
            effective = _effective(unit, ov)
            if effective.get("deleted"):
                rewriter.remove(self._unit_gids(unit, index))
                raw_done.add(uid)
                continue
            if _font_style_changed(unit, ov):   # el usuario pidió otra fuente, tamaño o color
                rewriter.remove(self._unit_gids(unit, index))
                raw_removed.add(uid)
                continue
            placement = ("inplace" if uid not in lifted and uid not in covered
                         else "top" if order.index(uid) > lowest_base else "bottom")
            try:
                bbox, unit_notes = self._raw_unit(doc, page, rewriter, page_text, unit, effective, ov,
                                                  placement, index, uid, substitutions, fonts_added,
                                                  layer_runs, accept_all)
            except _Pending:
                kept.add(uid)
                layout[uid] = list(unit["bbox"])
                continue
            except _Unsupported:
                continue
            raw_done.add(uid)
            layout[uid] = bbox
            notes += unit_notes
        if rewriter is not None and rewriter.changed:
            with self._ops_lock:
                rewriter.apply(doc, index)

        # 2) redacciones del usuario: borran todo lo que hay debajo
        user_redactions = [a for a in added if a["kind"] == "redact"]
        if user_redactions:
            for el in user_redactions:
                page.add_redact_annot(fitz.Rect(el["bbox"]), fill=(0, 0, 0))
            page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_PIXELS)
            for el in user_redactions:
                layout[el["id"]] = list(el["bbox"])

        # 3) texto original que no se pudo ubicar en el contenido: se quita solo su caja
        #    (si hiciera falta una fuente parecida sin aceptar, queda como está)
        legacy_text = []
        for uid in changed:
            unit = originals[uid]
            if unit["kind"] != "text" or unit["source"] != "pdf" or uid in raw_done \
                    or uid in raw_removed or uid in kept:
                continue
            ov = overrides.get(uid, {})
            element = _effective(unit, ov)
            if not element.get("deleted") and not _font_style_changed(unit, ov):
                _font, used, substituted, original = self.resolve_font(element["style"], element["text"])
                if substituted and not accept_all and ov.get("font_ok") != used:
                    substitutions.append(self._substitution_info(index, uid, element, original, used))
                    kept.add(uid)
                    layout[uid] = list(unit["bbox"])
                    continue
            legacy_text.append(uid)
        for uid in legacy_text:
            for rect in _text_rects(originals[uid]):
                page.add_redact_annot(rect, fill=False)  # sin relleno: el fondo queda intacto
        if legacy_text:
            page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE,
                                  graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                                  text=fitz.PDF_REDACT_TEXT_REMOVE)

        # 4) imágenes originales movidas/borradas: se quita solo esa colocación
        for uid in [u for u in changed if originals[u]["kind"] == "image"]:
            _remove_image_placement(doc, page, originals[uid])

        # 5) texto escaneado (OCR) modificado: se tapa con el color de fondo
        for uid in [u for u in changed if originals[u]["kind"] == "text"
                    and originals[u]["source"] == "ocr"]:
            unit = originals[uid]
            shape = page.new_shape()
            shape.draw_rect(fitz.Rect(unit["bbox"]) + (-1, -1, 1, 1))
            shape.finish(color=None, fill=_rgb(unit.get("bg", "#FFFFFF")), width=0)
            shape.commit(overlay=True)

        # 6) dibujar los elementos "flotantes" según el orden de capas
        floating = [uid for uid in order if (uid in changed and uid not in kept
                                             and (uid not in raw_done or uid in layer_runs))
                    or uid.startswith("N")]
        below = [uid for uid in floating if order.index(uid) < lowest_base]
        above = [uid for uid in floating if order.index(uid) > lowest_base]
        added_by_id = {a["id"]: a for a in added}
        for group, overlay in ((list(reversed(below)), False), (above, True)):
            for uid in group:
                if uid in layer_runs:
                    _add_content(doc, page, rewriter.layer_stream(layer_runs[uid]), overlay)
                    continue
                if uid in originals:
                    element = _effective(originals[uid], overrides.get(uid, {}))
                else:
                    element = added_by_id.get(uid)
                if not element or element.get("deleted") or element["kind"] == "redact":
                    continue
                bbox, note = self._draw(doc, page, element, overlay)
                layout[uid] = bbox
                if note:
                    notes.append(note)

        # cajas actuales de todo lo que no se redibujó
        for uid, unit in originals.items():
            if uid not in layout and uid not in changed:
                layout[uid] = list(unit["bbox"])
        return layout, notes

    def _substitution_info(self, index, uid, element, original, substitute, missing=None,
                           choice=None) -> dict:
        info = {"page": index, "uid": uid, "font": original, "substitute": substitute,
                "missing": sorted(set(missing or [])), "text": element.get("text", ""),
                "original_display": None, "substitute_display": None}
        objnum = element.get("style", {}).get("font_objnum")
        try:
            info["original_display"] = self.display_font({"font_objnum": objnum}) if objnum else None
            if choice is not None:
                info["substitute_display"] = _display_tuple(choice)
        except Exception:
            errors.record("No se pudo preparar la muestra de la fuente")
        self._log(f"página {index + 1}, {uid}: «{original}» no tiene "
                  f"{' '.join(info['missing']) or 'todos los caracteres'}; parecida propuesta: "
                  f"{substitute} (falta confirmar)")
        return info

    # ------------------------------------------------------------------ edición en el contenido

    def _unit_lines(self, unit, index):
        if unit.get("spans") is not None:
            return [unit]
        ids = unit.get("line_ids", [])
        return [line for line in self.analyze(index)["lines"] if line["id"] in ids]

    def _unit_gids(self, unit, index):
        return [g for line in self._unit_lines(unit, index) for span in line["spans"]
                for g in span["gids"] if g is not None]

    def _raw_unit(self, doc, page, rewriter, page_text, unit, effective, ov, placement, index, uid,
                  substitutions, fonts_added, layer_runs, accept_all=False):
        """Prepara y aplica los cambios de una unidad de texto en el contenido. Devuelve
        (caja nueva, avisos). Todo se calcula antes de tocar nada: _Pending deja la unidad
        como estaba (falta aceptar una fuente parecida); _Unsupported la deja para el
        camino anterior."""
        dx, dy = ov.get("dx", 0.0), ov.get("dy", 0.0)
        plans = self._text_plans(page_text, unit, effective, ov, dx, dy, placement, index)

        notes, boxes, pending, unaccepted = [], [], [], []
        for plan in plans:
            boxes += plan["boxes"]
            for op, run, resolved, text in plan["runs"]:
                if op.stream != "page" and placement != "inplace":
                    raise _Unsupported()   # capas aparte: solo texto de la página
                if resolved is not None and resolved.substitute:
                    if accept_all or ov.get("font_ok") == resolved.label:
                        notes.append(f"Fuente sustituida (aceptada): {op.state.font.name} → "
                                     f"{resolved.label}")
                    else:
                        font = op.state.font
                        missing = [c for c in text if c not in " \xa0" and font.code_for(c) is None]
                        unaccepted.append((font.name, resolved, missing))
                pending.append((op, run, resolved, text))
        if unaccepted:
            font_name, resolved, missing = unaccepted[0]
            missing = sorted({c for _f, _r, m in unaccepted for c in m})
            substitutions.append(self._substitution_info(index, uid, effective, font_name,
                                                         resolved.label, missing, resolved.choice))
            raise _Pending()
        for plan in plans:
            rewriter.remove(plan["remove"])
        for op, run, resolved, text in pending:
            if resolved is not None:
                run.font_res = self._register_font(doc, page, op.stream, resolved, text, fonts_added)
                self._log(f"página {index + 1}, {uid}: «{text.strip()}» con {resolved.label} "
                          f"({'sustituta' if resolved.substitute else 'misma familia, ' + resolved.source})")
            if placement == "inplace":
                rewriter.emit_after(op, run)
            else:
                layer_runs.setdefault(uid, []).append(run)
        bbox = [min(b[0] for b in boxes), min(b[1] for b in boxes),
                max(b[2] for b in boxes), max(b[3] for b in boxes)] if boxes else list(unit["bbox"])
        return bbox, notes

    def _register_font(self, doc, page, stream, resolved, text, fonts_added) -> str:
        """Agrega un subconjunto de la fuente completa (con sus números de glifo, que son
        los códigos del texto) a la página y, si el texto está en un formulario, también a
        los recursos de ese formulario. Devuelve el nombre del recurso."""
        key = (resolved.label, id(resolved.buffer), text, stream)
        if key in fonts_added:
            return fonts_added[key]
        used = {entry[4] for entry in page.get_fonts(full=True)} | set(fonts_added.values())
        n = 1
        while f"PliegueF{n}" in used:
            n += 1
        name = f"PliegueF{n}"
        xref = page.insert_font(fontname=name, fontbuffer=resolved.subset_buffer(set(text)))
        if stream != "page":
            _add_form_font(doc, stream, name, xref)
        fonts_added[key] = name
        return name

    @staticmethod
    def _first_glyph(page_text, span):
        gid = next((g for g in span["gids"] if g is not None), None)
        if gid is None:
            return None, None
        glyph = page_text.glyphs[gid]
        return glyph, page_text.op_of[gid]

    def full_font(self, info, text: str):
        """La fuente completa para escribir `text` cuando la incrustada no alcanza: la misma
        familia (instalada o de la biblioteca) o, si no existe, una parecida. None si nada
        sirve."""
        desc = info.description
        key = (info.objnum, frozenset(text))
        if key not in self._full_fonts:
            resolved = None
            choice = font_finder.find_same(desc)
            if choice is not None:
                candidate = tops.ResolvedFont(choice, text)
                if candidate.covers(text):
                    resolved = candidate
            if resolved is None:
                choice = font_finder.find_similar(desc)
                if choice is not None:
                    candidate = tops.ResolvedFont(choice, text)
                    if candidate.covers(text):
                        resolved = candidate
            self._full_fonts[key] = resolved
            self._log(f"«{info.name}» ({desc.family} {desc.weight}{' cursiva' if desc.italic else ''},"
                      f" {desc.kind}) -> {resolved.label + (' [sustituta]' if resolved.substitute else ' [misma familia, ' + resolved.source + ']') if resolved else 'ninguna'}")
        return self._full_fonts[key]

    def _segments(self, op, span, text, scale):
        """Reparte `text` en tramos para escribir con el estado de `op`: cada palabra va con
        la fuente del PDF si esta tiene todos sus caracteres; si no, con la fuente completa
        (misma familia o parecida). Devuelve [(items, ResolvedFont o None, avance, texto)]."""
        st, font = op.state, op.state.font
        guess = (span["space_w"] / scale) if span.get("space_w") else 0.25 * st.size * st.th
        words = re.findall(r"\S+|\s+", text)
        missing = [w for w in words if w.strip() and font.encode(w)[1]]
        resolved = self.full_font(font, text) if missing else None
        if missing and resolved is None:
            raise _Unsupported()
        segments: list[list] = []
        for word in words:
            use_full = resolved is not None and word.strip() and font.encode(word)[1]
            target = resolved if use_full else None
            if not word.strip() and segments:
                target = segments[-1][1]          # los espacios van con el tramo anterior
            if segments and segments[-1][1] is target:
                segments[-1][3] += word
            else:
                segments.append([[], target, 0.0, word])
        out = []
        for _items, target, _adv, seg_text in segments:
            items, advance = [], 0.0
            if target is None:
                codes, _missing = font.encode(seg_text)
                for code in codes:
                    if isinstance(code, tops.Space):
                        width = ((code.width / 1000 * st.size + st.tc) * st.th
                                 if code.width is not None else guess)
                        items.append(width)
                        advance += width
                    else:
                        items.append(font.code_bytes(code))
                        advance += _code_advance(st, font, code)
            else:
                for char in seg_text:
                    gid = target.code_for(char)
                    items.append(bytes([gid >> 8, gid & 0xFF]))
                    advance += (target.width_char(char) / 1000 * st.size + st.tc) * st.th
            out.append((items, target, advance, seg_text))
        return out

    def _plan_line(self, page_text, line, new_text, dx, dy, placement):
        """Cambios mínimos de una línea: solo los fragmentos cuyo texto cambió se tocan, y
        dentro de un fragmento se conserva intacto todo lo anterior a la primera palabra
        que cambió (salvo que la línea se mueva o cambie de capa)."""
        spans = line["spans"]
        old_texts = [s["text"] for s in spans]
        old = "".join(old_texts)
        target = new_text + old[len(old.rstrip()):]
        eligible = [any(g is not None for g in s["gids"]) for s in spans]
        new_texts = _distribute(old_texts, target, eligible)
        move = abs(dx) > LIFT_EPS or abs(dy) > LIFT_EPS
        plan = {"remove": [], "runs": [], "boxes": []}
        for span, old_t, new_t in zip(spans, old_texts, new_texts):
            gids = [g for g in span["gids"] if g is not None]
            if new_t == old_t and not move and placement == "inplace":
                plan["boxes"].append(list(span["bbox"]))
                continue
            if not gids:
                continue
            if new_t == old_t:      # fragmento intacto que se mueve: los mismos códigos
                plan["remove"] += gids
                for op, glyphs in _by_op(page_text, gids):
                    run = _chunk_run(op, glyphs, page_text, dx, dy)
                    plan["runs"].append((op, run, None, ""))
                    plan["boxes"].append(_run_box(op, run, span, page_text,
                                                  glyphs[-1].x + glyphs[-1].adv - glyphs[0].x))
                continue
            # desde qué carácter se reescribe: el inicio de la primera palabra que cambió
            keep = 0
            if not move and placement == "inplace":
                p = 0
                while p < min(len(old_t), len(new_t)) and old_t[p] == new_t[p]:
                    p += 1
                keep = old_t.rfind(" ", 0, p) + 1 if p < len(old_t) or p < len(new_t) else p
                while keep > 0 and (keep >= len(span["gids"]) or span["gids"][keep] is None):
                    keep = old_t.rfind(" ", 0, keep - 1) + 1 if keep > 1 else 0
            plan["remove"] += [g for g in span["gids"][keep:] if g is not None]
            if keep:
                kept_end = page_text.glyphs[span["gids"][keep]]
                plan["boxes"].append([span["bbox"][0], span["bbox"][1],
                                      _glyph_page_x(kept_end, page_text), span["bbox"][3]])
            write = new_t[keep:]
            if not write:
                continue
            anchor_gid = next(g for g in span["gids"][keep:] if g is not None)
            first = page_text.glyphs[anchor_gid]
            op = page_text.op_of[anchor_gid]
            scale = _scale(op, first, page_text)
            cursor = first.x
            for items, resolved, advance, seg_text in self._segments(op, span, write, scale):
                tm = _moved(fitz.Matrix(1, 0, 0, 1, cursor, 0) * op.tm, op.state.ctm,
                            page_text.page_tm, dx, dy)
                run = tops.Run(op.state, tm, items)
                plan["runs"].append((op, run, resolved, seg_text))
                plan["boxes"].append(_run_box(op, run, span, page_text, advance))
                cursor += advance
        return plan

    def _text_plans(self, page_text, unit, effective, ov, dx, dy, placement, index):
        """Qué escribir: solo lo que cambió. Con la misma cantidad de renglones y alineado a la
        izquierda, cada línea conserva su inicio y sus fragmentos; si no, los renglones que
        cambiaron se reescriben con el ancho del párrafo (pdf_flow)."""
        lines = self._unit_lines(unit, index)
        if abs((unit["style"].get("dir") or [1, 0])[1]) > 1e-3:   # texto girado: sin reacomodar
            texts = effective["text"].split("\n")
            if len(texts) != len(lines):
                texts = [effective["text"]] + [""] * (len(lines) - 1)
            return [self._plan_line(page_text, line, text, dx, dy, placement)
                    for line, text in zip(lines, texts)]
        zone = self._zone(index, unit, effective, ov)
        if zone is None:
            return [self._plan_line(page_text, line, line["text"], dx, dy, placement) for line in lines]
        if len(zone.rows) == zone.old_count == 1 and unit.get("level") != "block":
            return [self._plan_line(page_text, unit, effective["text"], dx, dy, placement)]
        outside = [line for n, line in enumerate(lines) if not zone.first <= n < zone.stop]
        plans = []
        if abs(dx) > LIFT_EPS or abs(dy) > LIFT_EPS or placement != "inplace":
            plans += [self._plan_line(page_text, line, line["text"], dx, dy, placement) for line in outside]
        if len(zone.rows) == zone.old_count and zone.align == "left" \
                and not any(row.hyphen for row in zone.rows):
            for line, row in zip(lines[zone.first:zone.stop], zone.rows):
                plans.append(self._plan_line(page_text, line, zone.row_text(row), dx, dy, placement))
        elif zone.rows or zone.old_count:
            plans.append(self._plan_zone(zone, dx, dy))
        return plans

    # ------------------------------------------------------------------ información para la UI

    def font_note(self, index: int, uid: str, text: str | None = None) -> str:
        """Fuente y tamaño reales del texto elegido y, si el texto nuevo no entra en la
        fuente incrustada, con qué fuente se escribirá."""
        model = self.analyze(index)
        unit = next((u for u in model["lines"] + model["blocks"] if u["id"] == uid), None)
        if unit is None:
            return ""
        spans = [s for line in self._unit_lines(unit, index) for s in line.get("spans") or []]
        page_text = self._texts.get(index)
        labels = []
        for span in spans:
            info = self._span_font(page_text, span)
            label = f"{info.name if info else span['font']} {_fmt_size(span['size'])} pt"
            if label not in labels:
                labels.append(label)
        note = "Fuente original: " + " · ".join(labels[:3]) + (" …" if len(labels) > 3 else "")
        if not spans or text is None or text == unit["text"] or not self._mapped(unit, index):
            return note
        main = max(spans, key=lambda s: len(s["text"].strip()))
        info = self._span_font(page_text, main)
        if info is None:
            return note
        _codes, missing = info.encode(text)
        if not missing:
            return note
        resolved = self.full_font(info, text)
        if resolved is not None and not resolved.substitute:
            where = {"sistema": "instalada", "usuario": "instalada", "biblioteca": "incluida"}.get(
                resolved.source, resolved.source)
            return f"{note} · las letras nuevas usan {resolved.label} ({where})"
        alternative = f"; la más parecida es {resolved.label}" if resolved else ""
        return (f"La fuente original ({info.name}) no incluye: {' '.join(sorted(set(missing)))}"
                f"{alternative} — te preguntaré antes de usarla")

    def display_font(self, style: dict):
        """(archivo, familia, negrita, cursiva) para mostrar el texto mientras se edita con
        la MISMA fuente que se va a usar: la familia completa (instalada o de la biblioteca)
        si existe; si no, la incrustada (si Windows puede cargarla)."""
        info = self._fonts.get(style.get("font_objnum") or -1)
        if info is None:
            return None
        choice = font_finder.find_same(info.description)
        if choice is not None:
            try:
                return _display_tuple(choice)
            except Exception:
                errors.record(f"No se pudo preparar {choice.label} para mostrarla")
        if not info.buffer or info.font_kind not in ("FontFile2", "FontFile3"):
            return None
        try:
            import tempfile

            folder = Path(tempfile.gettempdir()) / "pliegue_fonts"
            folder.mkdir(exist_ok=True)
            suffix = ".otf" if info.buffer[:4] == b"OTTO" else ".ttf"
            path = folder / f"{os.getpid()}_{info.objnum}{suffix}"
            if not path.exists():
                path.write_bytes(info.buffer)
            return (str(path),) + _tk_names(info.buffer)
        except Exception:
            errors.record(f"No se pudo preparar la fuente incrustada {info.name} para mostrarla")
            return None

    @staticmethod
    def _covered_by_lower(order, changed, originals, overrides, added) -> set[str]:
        """Originales intactos que quedan por encima de un elemento redibujado y lo
        tapan: también se redibujan, para respetar el orden de capas pedido."""
        boxes = {a["id"]: a.get("bbox") for a in added}
        kinds = {a["id"]: a["kind"] for a in added}
        kinds.update({uid: unit["kind"] for uid, unit in originals.items()})
        for uid in changed:
            if uid in originals:
                boxes[uid] = _effective(originals[uid], overrides.get(uid, {}))["bbox"]
        extra: set[str] = set()
        pending = [uid for uid in order if uid in boxes]
        while pending:
            uid = pending.pop()
            if not boxes.get(uid):
                continue
            rect = fitz.Rect(boxes[uid])
            for above in order[order.index(uid) + 1:]:
                if kinds.get(uid) == "text" and kinds.get(above) == "text":
                    continue  # texto sobre texto no se tapa: no hace falta redibujar
                if (above in originals and above not in changed and above not in extra
                        and (fitz.Rect(originals[above]["bbox"]) & rect).get_area() > 1):
                    extra.add(above)
                    boxes[above] = originals[above]["bbox"]
                    pending.append(above)
        return extra

    def default_order(self, model, state=None) -> list[str]:
        """Orden de capas por defecto (de abajo hacia arriba): originales en el orden
        del contenido de la página y después los elementos agregados."""
        state = state or {}
        overrides = state.get("overrides", {})
        block_mode = {uid for uid in overrides if uid.startswith("B")}
        units = list(model["images"])
        for block in model["blocks"]:
            if block["id"] in block_mode:
                units.append(block)
            else:
                units.extend(l for l in model["lines"] if l.get("block_id") == block["id"])
        if not model["blocks"]:  # OCR sin bloques agrupados
            units.extend(model["lines"])
        units.sort(key=lambda u: u["z"])
        return [u["id"] for u in units] + [a["id"] for a in state.get("added", [])]

    # ------------------------------------------------------------------ dibujo

    def _draw(self, doc, page, element, overlay):
        kind = element["kind"]
        if kind == "text":
            return self._draw_text(page, element, overlay)
        if kind == "image":
            rect = fitz.Rect(element["bbox"])
            if element.get("xref"):
                page.insert_image(rect, xref=element["xref"], keep_proportion=False, overlay=overlay)
            else:
                page.insert_image(rect, filename=element["path"], keep_proportion=False,
                                  overlay=overlay)
            return list(element["bbox"]), None
        shape = page.new_shape()
        if kind == "highlight":
            shape.draw_rect(fitz.Rect(element["bbox"]))
            shape.finish(color=None, fill=_rgb(element["color"]), fill_opacity=0.35, width=0)
        elif kind == "draw":
            points = [fitz.Point(p) for p in element["points"]]
            if len(points) < 2:
                return list(element.get("bbox", [0, 0, 0, 0])), None
            shape.draw_polyline(points)
            shape.finish(color=_rgb(element["color"]), width=element.get("width", 2),
                         closePath=False, lineCap=1, lineJoin=1)
        shape.commit(overlay=overlay)
        return list(element.get("bbox", [0, 0, 0, 0])), None

    def _draw_text(self, page, unit, overlay):
        style = unit["style"]
        text = unit["text"]
        font, used, substituted, original = self.resolve_font(style, text)
        size = float(style["size"])
        writer = fitz.TextWriter(page.rect, color=_rgb(style["color"]))
        if unit.get("level") == "block" and unit.get("lines"):
            first = unit["lines"][0]["origin"]
            x0 = unit["bbox"][0]
            width = unit["bbox"][2] - unit["bbox"][0]
            lines = _wrap(text, font, size, width)
            line_height = float(style.get("line_height") or size * 1.2)
            for n, (line, is_last) in enumerate(lines):
                y = first[1] + n * line_height
                _append_aligned(writer, line, font, size, x0, y, width,
                                style.get("align", "left"), is_last)
        else:
            origin = unit["lines"][0]["origin"] if unit.get("lines") else unit["origin"]
            # un texto que llegaría hasta el borde de la página sigue en el renglón siguiente
            room = page.rect.x1 - max(36.0, min(72.0, origin[0])) - origin[0]
            line_height = float(style.get("line_height") or size * 1.2)
            rows = _wrap(text, font, size, room) if room > size * 4 else [(t, True) for t in text.split("\n")]
            for n, (line, _is_last) in enumerate(rows):
                _append_words(writer, origin[0], origin[1] + n * line_height, line, font, size)
        direction = style.get("dir") or [1, 0]
        morph = None
        if abs(direction[1]) > 1e-3:
            import math
            angle = -math.degrees(math.atan2(direction[1], direction[0]))
            pivot = unit["lines"][0]["origin"] if unit.get("lines") else unit["origin"]
            morph = (fitz.Point(pivot), fitz.Matrix(1, 1).prerotate(angle))
        writer.write_text(page, overlay=overlay, morph=morph)
        rect = writer.text_rect
        note = f"Fuente sustituida: {original} → {used}" if substituted else None
        return [rect.x0, rect.y0, rect.x1, rect.y1], note


# --------------------------------------------------------------------------- utilidades

def _space_width(span) -> float | None:
    """Ancho (en la página) de los espacios que PyMuPDF sintetiza entre palabras cuando la
    fuente no dibuja un glifo de espacio: sirve para emular el espacio al reescribir."""
    widths = [ch["bbox"][2] - ch["bbox"][0] for ch, gid in zip(span["chars"], span.get("gids", []))
              if gid is None and ch["c"] in " \xa0" and ch["bbox"][2] > ch["bbox"][0]]
    return sorted(widths)[len(widths) // 2] if widths else None


FONT_STYLE_KEYS = ("requested_family", "size", "color", "bold", "italic")


def _font_style_changed(unit: dict, ov: dict) -> bool:
    """¿El usuario cambió explícitamente la fuente, el tamaño, el color o el peso?"""
    style = ov.get("style") or {}
    original = unit.get("style", {})
    for key in FONT_STYLE_KEYS:
        if key in style and style[key] != original.get(key, "Original" if key == "requested_family"
                                                       else None):
            return True
    return False


def _fmt_size(size: float) -> str:
    return f"{size:.2f}".rstrip("0").rstrip(".").replace(".", ",")


def _distribute(old_texts: list[str], new: str, eligible: list[bool]) -> list[str]:
    """Reparte el texto nuevo de una línea entre sus fragmentos: solo cambian los que
    tocan la parte editada (prefijo y sufijo comunes intactos)."""
    old = "".join(old_texts)
    if old == new:
        return list(old_texts)
    limit = min(len(old), len(new))
    p = 0
    while p < limit and old[p] == new[p]:
        p += 1
    s = 0
    while s < limit - p and old[-1 - s] == new[-1 - s]:
        s += 1
    a, b = p, len(old) - s
    inserted = new[p:len(new) - s]
    starts, pos = [], 0
    for text in old_texts:
        starts.append(pos)
        pos += len(text)
    ends = [st + len(t) for st, t in zip(starts, old_texts)]
    affected = [i for i in range(len(old_texts)) if starts[i] < b and ends[i] > a]
    if not affected:   # inserción pura: en el fragmento del carácter anterior (o el siguiente)
        before = [i for i in range(len(old_texts)) if starts[i] < a <= ends[i] and old_texts[i]]
        after = [i for i in range(len(old_texts)) if starts[i] <= a < ends[i]]
        affected = before[-1:] or after[:1] or [len(old_texts) - 1]
    first, last = affected[0], affected[-1]
    while first > 0 and not eligible[first]:
        first -= 1
    if not eligible[first]:
        first = next((i for i in range(len(old_texts)) if eligible[i]), first)
    last = max(last, first)
    result = list(old_texts)
    head = old[starts[first]:a] if a >= starts[first] else ""
    tail = old[b:ends[last]] if b <= ends[last] else ""
    result[first] = head + inserted + tail
    for i in range(first + 1, last + 1):
        result[i] = ""
    return result


def _code_advance(st, font, code) -> float:
    return (font.width(code) / 1000 * st.size + st.tc
            + (st.tw if not font.two_byte and code == 32 else 0)) * st.th


def _scale(op, glyph, page_text) -> float:
    """Unidades de página por unidad de espacio de texto en la dirección del texto."""
    m = fitz.Matrix(1, 0, 0, 1, glyph.x, 0) * op.tm * op.state.ctm * page_text.page_tm
    p0, p1 = fitz.Point(0, 0) * m, fitz.Point(1, 0) * m
    return max(1e-6, abs(p1 - p0))


def _moved(tm, ctm, page_tm, dx: float, dy: float):
    """Matriz de texto desplazada (dx, dy) en coordenadas de la página (PyMuPDF)."""
    if abs(dx) < 1e-9 and abs(dy) < 1e-9:
        return fitz.Matrix(tm)
    inverse = ~page_tm
    delta = fitz.Point(dx, dy) * inverse - fitz.Point(0, 0) * inverse
    user = tm * ctm * fitz.Matrix(1, 0, 0, 1, delta.x, delta.y)
    return user * ~ctm


def _by_op(page_text, gids):
    groups = []
    for gid in gids:
        op = page_text.op_of[gid]
        if groups and groups[-1][0] is op:
            groups[-1][1].append(page_text.glyphs[gid])
        else:
            groups.append((op, [page_text.glyphs[gid]]))
    return groups


def _chunk_run(op, glyphs, page_text, dx, dy):
    """Los mismos códigos con las mismas separaciones, desde la posición (movida) del primero."""
    items, cur = [], glyphs[0].x
    for glyph in glyphs:
        if abs(glyph.x - cur) > 1e-6:
            items.append(glyph.x - cur)
        items.append(op.state.font.code_bytes(glyph.code))
        cur = glyph.x + glyph.adv
    tm = _moved(fitz.Matrix(1, 0, 0, 1, glyphs[0].x, 0) * op.tm, op.state.ctm, page_text.page_tm,
                dx, dy)
    return tops.Run(op.state, tm, items)


def _run_box(op, run, span, page_text, advance) -> list[float]:
    """Caja aproximada del texto escrito (para la selección en el editor)."""
    m = run.tm * op.state.ctm * page_text.page_tm
    origin = fitz.Point(0, op.state.ts) * m
    end = fitz.Point(advance, op.state.ts) * m
    top = span["bbox"][1] - span["origin"][1]
    bottom = span["bbox"][3] - span["origin"][1]
    return [min(origin.x, end.x), min(origin.y, end.y) + top,
            max(origin.x, end.x), max(origin.y, end.y) + bottom]


def _add_content(doc, page, data: bytes, overlay: bool) -> None:
    """Agrega un flujo de contenido encima (o debajo) de lo que ya tiene la página."""
    page.wrap_contents()
    xref = doc.get_new_xref()
    doc.update_object(xref, "<<>>")
    doc.update_stream(xref, data)
    refs = [f"{x} 0 R" for x in page.get_contents()]
    refs = refs + [f"{xref} 0 R"] if overlay else [f"{xref} 0 R"] + refs
    doc.xref_set_key(page.xref, "Contents", "[" + " ".join(refs) + "]")


def _glyph_page_x(glyph, _page_text) -> float:
    return glyph.origin[0]


def _tk_names(buffer: bytes) -> tuple[str, bool, bool]:
    """(familia, negrita, cursiva) con que Windows registra un archivo de fuente."""
    from fontTools.ttLib import TTFont

    font = TTFont(BytesIO(buffer), lazy=True)
    names = {r.nameID: r.toUnicode() for r in font["name"].names if r.nameID in (1, 2)}
    style = names.get(2, "Regular")
    return names.get(1, ""), "Bold" in style, "Italic" in style or "Oblique" in style


def _display_tuple(choice):
    path, _family = font_finder.display_file(choice)
    return (path,) + _tk_names(font_finder.static_buffer(choice))


def _add_form_font(doc, form_xref: int, name: str, font_xref: int) -> None:
    """Agrega una fuente a los recursos de un Form XObject (el texto de muchos PDF unidos o
    exportados vive dentro de formularios, que no ven las fuentes de la página)."""
    kind, value = doc.xref_get_key(form_xref, "Resources")
    if kind == "null":
        return                                   # usa los recursos de la página
    holder, prefix = (int(value.split()[0]), "") if kind == "xref" else (form_xref, "Resources/")
    kind, value = doc.xref_get_key(holder, prefix + "Font")
    if kind == "xref":
        doc.xref_set_key(int(value.split()[0]), name, f"{font_xref} 0 R")
    elif kind == "dict":
        doc.xref_set_key(holder, prefix + "Font/" + name, f"{font_xref} 0 R")
    else:
        doc.xref_set_key(holder, prefix + "Font", f"<</{name} {font_xref} 0 R>>")


def _justify(items, text, extra):
    out = []
    for item, char in zip(items, text):
        out.append(item)
        if char == " ":
            out.append(extra)
    return out



def _is_change(ov: dict) -> bool:
    return bool(ov.get("deleted") or "text" in ov or "style" in ov
                or abs(ov.get("dx", 0)) > LIFT_EPS or abs(ov.get("dy", 0)) > LIFT_EPS
                or "bbox" in ov)


def _lifted(order, default_order, originals) -> set[str]:
    """Originales cuyo lugar relativo entre los originales cambió: se redibujan."""
    orig_now = [u for u in order if u in originals]
    orig_default = [u for u in default_order if u in originals and u in orig_now]
    if orig_now == orig_default:
        return set()
    # mínimo conjunto razonable: los que no están en la subsecuencia común más larga
    keep = set(_lcs(orig_default, orig_now))
    return {u for u in orig_now if u not in keep}


def _lcs(a, b):
    table = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(len(a) - 1, -1, -1):
        for j in range(len(b) - 1, -1, -1):
            table[i][j] = table[i + 1][j + 1] + 1 if a[i] == b[j] else max(table[i + 1][j], table[i][j + 1])
    out, i, j = [], 0, 0
    while i < len(a) and j < len(b):
        if a[i] == b[j]:
            out.append(a[i]); i += 1; j += 1
        elif table[i + 1][j] >= table[i][j + 1]:
            i += 1
        else:
            j += 1
    return out


def _effective(unit: dict, ov: dict) -> dict:
    """Unidad original con sus cambios aplicados (texto, estilo, posición)."""
    el = copy.deepcopy(unit)
    if ov.get("deleted"):
        el["deleted"] = True
        return el
    if "text" in ov:
        el["text"] = ov["text"]
    if "style" in ov:
        el["style"].update(ov["style"])
    if el["kind"] == "image":
        if "bbox" in ov:
            el["bbox"] = list(ov["bbox"])
        else:
            dx, dy = ov.get("dx", 0), ov.get("dy", 0)
            el["bbox"] = [el["bbox"][0] + dx, el["bbox"][1] + dy, el["bbox"][2] + dx, el["bbox"][3] + dy]
        return el
    dx, dy = ov.get("dx", 0), ov.get("dy", 0)
    if dx or dy:
        el["bbox"] = [el["bbox"][0] + dx, el["bbox"][1] + dy, el["bbox"][2] + dx, el["bbox"][3] + dy]
        for line in el["lines"]:
            line["origin"] = [line["origin"][0] + dx, line["origin"][1] + dy]
    return el


def _norm_text(text: str) -> str:
    """Texto comparable: sin espacios ni saltos y con ligaduras separadas (ﬁ -> fi)."""
    return "".join(unicodedata.normalize("NFKC", text).split())


def _no_hyphens(text: str) -> str:
    return _norm_text(text).replace("-", "").replace("\u2010", "")


def _has_ink(image, box, zoom: float, chars: int) -> bool:
    """¿Se ve algo en la caja? Píxeles que se apartan claramente del fondo (el tono más
    frecuente de la caja)."""
    x0, y0 = max(0, int(box[0] * zoom) - 1), max(0, int(box[1] * zoom) - 1)
    x1, y1 = min(image.width, int(box[2] * zoom) + 2), min(image.height, int(box[3] * zoom) + 2)
    if x1 - x0 < 2 or y1 - y0 < 2:
        return False
    histogram = image.crop((x0, y0, x1, y1)).convert("L").histogram()
    background = max(range(256), key=histogram.__getitem__)
    ink = sum(n for value, n in enumerate(histogram) if abs(value - background) > 40)
    return ink >= max(3, min(chars, 20))


def _text_rects(unit: dict):
    """Rectángulos a redactar para quitar una unidad de texto: una caja por línea,
    apenas reducida en vertical para no tocar las líneas vecinas."""
    for line in unit["lines"]:
        x0, y0, x1, y1 = line["bbox"]
        inset = (y1 - y0) * 0.15
        yield fitz.Rect(x0 - 0.5, y0 + inset, x1 + 0.5, y1 - inset)


def _remove_image_placement(doc, page, image: dict) -> None:
    """Quita una colocación concreta de una imagen del flujo de contenido."""
    xref = image["xref"]
    page.clean_contents()  # puede renombrar recursos: los nombres se leen después
    names = [entry[7] for entry in page.get_images(full=True) if entry[0] == xref]
    contents = page.get_contents()
    if names and len(contents) == 1:
        stream = doc.xref_stream(contents[0])
        pattern = re.compile(rb"/" + re.escape(names[0].encode()) + rb"\s+Do\b")
        if len(pattern.findall(stream)) == 1:
            doc.update_stream(contents[0], pattern.sub(b"", stream))
            return
    # respaldo: quitar las imágenes de esa zona sin tocar texto ni gráficos
    page.add_redact_annot(fitz.Rect(image["bbox"]))
    page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_REMOVE,
                          graphics=fitz.PDF_REDACT_LINE_ART_NONE, text=fitz.PDF_REDACT_TEXT_NONE)


def _detect_alignment(line_boxes, block_bbox) -> str:
    if len(line_boxes) < 2:
        return "left"
    bx0, _by0, bx1, _by1 = block_bbox
    tol = 2.0
    lefts = [abs(b[0] - bx0) < tol for b in line_boxes]
    rights = [abs(b[2] - bx1) < tol for b in line_boxes]
    centers = [abs((b[0] + b[2]) / 2 - (bx0 + bx1) / 2) < tol for b in line_boxes]
    if all(lefts[:-1]) and all(rights[:-1]) and len(line_boxes) > 2:
        return "justify"
    if all(lefts):
        return "left"
    if all(rights):
        return "right"
    if all(centers):
        return "center"
    return "left"


def _wrap(text: str, font, size: float, width: float) -> list[tuple[str, bool]]:
    """Reparte el texto en líneas que entran en `width`. Devuelve (línea, es_última
    de su párrafo)."""
    out = []
    for paragraph in text.split("\n"):
        words = paragraph.split(" ")
        line = ""
        for word in words:
            trial = f"{line} {word}" if line else word
            if line and font.text_length(trial, fontsize=size) > width:
                out.append((line, False))
                line = word
            else:
                line = trial
        out.append((line, True))
    return out


def _append_aligned(writer, line, font, size, x0, y, width, align, is_last):
    line_width = font.text_length(line, fontsize=size)
    if align == "justify" and not is_last and " " in line.strip():
        words = line.split(" ")
        gaps = len(words) - 1
        extra = (width - font.text_length("".join(words), fontsize=size)) / gaps
        x = x0
        for word in words:
            writer.append((x, y), word, font=font, fontsize=size)
            x += font.text_length(word, fontsize=size) + extra
        return
    if align == "center":
        x = x0 + (width - line_width) / 2
    elif align == "right":
        x = x0 + width - line_width
    else:
        x = x0
    _append_words(writer, x, y, line, font, size)


def _append_words(writer, x, y, line, font, size):
    """Escribe palabra por palabra: si se escribe el glifo del espacio, MuPDF lo
    mapea a U+00A0 en el ToUnicode y al copiar o buscar el texto falla."""
    space = font.text_length(" ", fontsize=size)
    for word in line.split(" "):
        if word:
            writer.append((x, y), word, font=font, fontsize=size)
            x += font.text_length(word, fontsize=size)
        x += space


# --------------------------------------------------------------------------- OCR (hOCR)

_TITLE = re.compile(r"(\w+) ([^;]+)")


def _title_props(title: str) -> dict:
    return {m.group(1): m.group(2).strip() for m in _TITLE.finditer(title or "")}


OCR_SIZE_FACTOR = 1.24   # x_size de Tesseract ≈ 0,81 del tamaño de fuente (calibrado con Arial y Times)


def parse_hocr(hocr: str, image, scale: float) -> dict:
    """Convierte el hOCR de Tesseract en líneas y bloques editables (en puntos)."""
    import xml.etree.ElementTree as ET

    root = ET.fromstring(re.sub(r'\sxmlns="[^"]+"', "", hocr, count=1))
    lines, blocks = [], []
    for pi, par in enumerate(root.iter("p")):
        par_lines = []
        for li, span in enumerate(el for el in par.iter("span")
                                  if "ocr_line" in el.get("class", "")
                                  or "ocr_caption" in el.get("class", "")
                                  or "ocr_header" in el.get("class", "")):
            props = _title_props(span.get("title"))
            words = [w for w in span.iter("span") if w.get("class") == "ocrx_word"
                     and "".join(w.itertext()).strip()]
            if not words:
                continue
            text = " ".join("".join(w.itertext()).strip() for w in words)
            x0, y0, x1, y1 = (int(v) for v in props["bbox"].split())
            slope, const = (float(v) for v in props.get("baseline", "0 0").split())
            baseline_px = y1 + const + slope * 0
            size_px = float(props.get("x_size", y1 - y0)) * OCR_SIZE_FACTOR
            color, bg = _sample_colors(image, [tuple(int(v) for v in _title_props(
                w.get("title"))["bbox"].split()) for w in words], (x0, y0, x1, y1))
            serif = _looks_serif(image, (x0, y0, x1, y1))
            family = "Times New Roman" if serif else "Arial"
            size = round(size_px / scale, 1)
            line = {
                "id": f"L{pi}_{li}", "kind": "text", "level": "line", "block_id": f"B{pi}",
                "bbox": [x0 / scale, y0 / scale, x1 / scale, y1 / scale], "text": text,
                "lines": [{"text": text, "origin": [x0 / scale, baseline_px / scale],
                           "bbox": [x0 / scale, y0 / scale, x1 / scale, y1 / scale]}],
                "style": {"font": family, "font_xref": 0, "size": size, "color": color,
                          "bold": False, "italic": False, "orig_bold": False, "orig_italic": False,
                          "serif": serif, "mono": False,
                          "family": family, "requested_family": "Original", "dir": [1, 0],
                          "align": "left", "line_height": size * 1.2},
                "source": "ocr", "bg": bg, "z": 0,
            }
            par_lines.append(line)
        if not par_lines:
            continue
        lines.extend(par_lines)
        bbox = [min(l["bbox"][0] for l in par_lines), min(l["bbox"][1] for l in par_lines),
                max(l["bbox"][2] for l in par_lines), max(l["bbox"][3] for l in par_lines)]
        style = dict(par_lines[0]["style"])
        baselines = [l["lines"][0]["origin"][1] for l in par_lines]
        gaps = [b - a for a, b in zip(baselines, baselines[1:]) if b > a]
        style["line_height"] = sorted(gaps)[len(gaps) // 2] if gaps else style["size"] * 1.2
        style["align"] = _detect_alignment([l["bbox"] for l in par_lines], bbox)
        blocks.append({"id": f"B{pi}", "kind": "text", "level": "block", "bbox": bbox,
                       "text": "\n".join(l["text"] for l in par_lines),
                       "lines": [l["lines"][0] for l in par_lines],
                       "line_ids": [l["id"] for l in par_lines], "style": style,
                       "source": "ocr", "bg": par_lines[0]["bg"], "z": 0})
    return {"lines": lines, "blocks": blocks}


def _sample_colors(image, word_boxes, line_box):
    """(color del texto, color de fondo) en hex. El texto: mediana del 15 % más
    oscuro dentro de las palabras; el fondo: mediana de un anillo alrededor."""
    px = image.load()
    dark = []
    for x0, y0, x1, y1 in word_boxes:
        samples = [px[x, y] for x in range(x0, x1, max(1, (x1 - x0) // 40))
                   for y in range(y0, y1, max(1, (y1 - y0) // 20))]
        samples.sort(key=lambda c: sum(c))
        dark.extend(samples[:max(1, len(samples) * 15 // 100)])
    x0, y0, x1, y1 = line_box
    ring = []
    w, h = image.size
    for x in range(max(0, x0 - 4), min(w, x1 + 4), 3):
        for y in (max(0, y0 - 4), min(h - 1, y1 + 4)):
            ring.append(px[x, y])
    return _median_hex(dark), _median_hex(ring or [(255, 255, 255)])


def _median_hex(colors):
    channels = list(zip(*colors))
    med = [sorted(c)[len(c) // 2] for c in channels]
    return "#" + "".join(f"{v:02X}" for v in med[:3])


def _looks_serif(image, box) -> bool:
    """Heurística simple: las serifas tienen trazos horizontales mucho más finos
    que los verticales (alto contraste)."""
    x0, y0, x1, y1 = box
    crop = image.crop(box).convert("L").point(lambda v: 0 if v < 128 else 255)
    w, h = crop.size
    data = crop.load()

    def runs(lines):
        lengths = []
        for line in lines:
            run = 0
            for value in line:
                if value == 0:
                    run += 1
                elif run:
                    lengths.append(run)
                    run = 0
            if run:
                lengths.append(run)
        return lengths

    horizontal = runs([[data[x, y] for x in range(w)] for y in range(0, h, 2)])   # ancho de verticales
    vertical = runs([[data[x, y] for y in range(h)] for x in range(0, w, 2)])     # grosor de horizontales
    if len(horizontal) < 10 or len(vertical) < 10:
        return False
    hv = sorted(horizontal)[len(horizontal) // 2]
    vv = sorted(vertical)[len(vertical) // 4]  # cuartil inferior: trazos horizontales finos
    return hv / max(1, vv) > 1.6
