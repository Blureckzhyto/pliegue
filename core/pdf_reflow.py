"""Texto que ya no cabe (núcleo, sin UI): acomodar en renglones y desplazar lo de abajo.

Tres piezas independientes de la sesión del editor:

* `wrap_rows`: reparte un texto en renglones por palabras (parte con guion solo las palabras
  que no caben solas en un renglón).
* `detect_columns` y `place`: columnas de la página y dónde cae cada renglón y cada franja de
  contenido, también si hay que seguir en una página nueva.
* `scan` y `Partition`: el contenido de la página como lista de objetos pintados y sus copias
  filtradas.

Por qué se parte el flujo de contenido y no la página entera con `show_pdf_page` y recortes: un
recorte no quita nada, así que el texto oculto seguiría en el archivo (duplicado al buscar o
copiar), y al usar el mismo formulario dos veces el editor ya no podría modificarlo. Aquí cada
copia del contenido conserva SOLO sus objetos: el resto se quita (los glifos con ajustes que dejan
a los demás en su lugar, los trazados y las imágenes sin dibujarse) y todo el estado gráfico,
los recortes y las capas opcionales siguen intactos. La copia que baja se envuelve en una
traslación: nada se redibuja, así que fuentes, vectores e imágenes quedan idénticos y el texto
sigue siendo texto normal de la página.

Coordenadas: las de PyMuPDF (origen arriba a la izquierda).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import pdf_textops as tops

try:
    import pymupdf as fitz
except ImportError:  # PyMuPDF < 1.24
    import fitz

PAINT = {"S", "s", "f", "F", "f*", "B", "B*", "b", "b*"}
CONSTRUCT = {"m", "l", "c", "v", "y", "re", "h"}
STROKING = {"S", "s", "B", "B*", "b", "b*"}
WIDTH_TOL = 1.01          # holgura al medir (los anchos reales rara vez coinciden al milímetro)
EXACT_TOL = 1.003         # con un ancho elegido por el usuario: casi nada (solo el redondeo)
SPLIT_TOL = 2.0           # pt: lo que empieza más arriba que esto cruza la línea de corte
GUTTER_MIN = 10.0         # pt: separación mínima entre columnas
HYPHENS = "-‐"


# --------------------------------------------------------------------------- renglones

@dataclass
class Row:
    start: int
    end: int
    hyphen: bool = False
    last: bool = False       # último renglón de su párrafo (no se justifica)


def wrap_rows(text: str, measure, first_width: float, rest_width: float, start: int = 0,
              hyphen_width: float = 0.0, converge=None,
              tolerance: float = WIDTH_TOL) -> tuple[list[Row], int | None]:
    """Reparte `text[start:]` en renglones. `measure(a, b)` da el ancho de `text[a:b]`.

    `converge(pos)` se consulta al empezar cada renglón: si devuelve un número, el resto del
    texto ya es idéntico al original desde esa línea y no hace falta seguir. Devuelve (renglones,
    línea original donde convergió o None)."""
    rows: list[Row] = []
    pos, n = start, len(text)

    def width():
        return first_width if not rows else rest_width

    while True:
        stop = text.find("\n", pos)
        stop = n if stop < 0 else stop
        row_start = pos
        fit_end = None
        i = pos
        while i < stop:
            while i < stop and text[i] == " ":
                i += 1
            if i >= stop:
                break
            word_end = i
            while word_end < stop and text[word_end] != " ":
                word_end += 1
            if measure(row_start, word_end) <= width() * tolerance:
                fit_end, i = word_end, word_end
                continue
            if fit_end is not None:      # el renglón termina antes de esta palabra
                rows.append(Row(row_start, fit_end))
                row_start, fit_end = i, None
                if converge is not None and (found := converge(row_start)) is not None:
                    return rows, found
                continue
            # la palabra sola no cabe: se parte con guion donde alcance
            cut = i + 1
            low, high = i + 1, word_end
            limit = width() * tolerance - hyphen_width
            while low <= high:
                middle = (low + high) // 2
                if measure(row_start, middle) <= limit:
                    cut, low = middle, middle + 1
                else:
                    high = middle - 1
            if cut >= word_end:
                fit_end, i = word_end, word_end
                continue
            rows.append(Row(row_start, cut, hyphen=True))
            row_start, i = cut, cut
            if converge is not None and (found := converge(row_start)) is not None:
                return rows, found
        rows.append(Row(row_start, fit_end if fit_end is not None else stop, last=True))
        if stop >= n:
            return rows, None
        pos = stop + 1
        if converge is not None and (found := converge(pos)) is not None:
            return rows, found


# --------------------------------------------------------------------------- columnas

def detect_columns(line_boxes, page_width: float) -> list[tuple[float, float]]:
    """Columnas de texto [(x0, x1)]: se buscan franjas verticales casi vacías entre los
    renglones; un título que las cruce no las rompe. Con una sola columna, devuelve una."""
    boxes = [b for b in line_boxes if b[2] > b[0]]
    if not boxes:
        return [(0.0, page_width)]
    left, right = min(b[0] for b in boxes), max(b[2] for b in boxes)
    step = 2.0
    bins = [0] * (int((right - left) / step) + 2)
    for b in boxes:
        for k in range(int((b[0] - left) / step), int((b[2] - left) / step) + 1):
            bins[k] += 1
    allowed = max(1, len(boxes) // 20)     # títulos o pies que cruzan el canal
    gutters, k = [], 0
    while k < len(bins) - 1:
        if bins[k] <= allowed:
            end = k
            while end < len(bins) - 1 and bins[end + 1] <= allowed:
                end += 1
            x0, x1 = left + k * step, left + (end + 1) * step
            if x1 - x0 >= GUTTER_MIN and x0 > left + 20 and x1 < right - 20:
                gutters.append((x0, x1))
            k = end + 1
        else:
            k += 1
    edges = [left] + [v for g in gutters for v in g] + [right]
    columns = [(edges[i], edges[i + 1]) for i in range(0, len(edges), 2)]
    # cada columna debe tener texto propio; si no, el "canal" era solo un hueco
    real = [c for c in columns if sum(1 for b in boxes if b[0] >= c[0] - 2 and b[2] <= c[1] + 2) >= 3]
    return real if len(real) >= 2 else [(left, right)]


# --------------------------------------------------------------------------- colocación

@dataclass
class Band:
    """Franja horizontal de contenido que no se parte (una línea de texto, una fila de tabla,
    una imagen con lo que tiene al lado)."""
    y0: float
    y1: float
    objects: list = field(default_factory=list)


def make_bands(objects) -> list[Band]:
    """Une en franjas los objetos cuyas alturas se tocan. `objects`: con atributo `box`."""
    bands: list[Band] = []
    for obj in sorted(objects, key=lambda o: o.box[1]):
        if bands and obj.box[1] < bands[-1].y1 - 0.01:
            bands[-1].y1 = max(bands[-1].y1, obj.box[3])
            bands[-1].objects.append(obj)
        else:
            bands.append(Band(obj.box[1], obj.box[3], [obj]))
    return bands


@dataclass
class Placement:
    rows: list            # [(página relativa, línea base)] de cada renglón
    bands: list           # [(página relativa, desplazamiento)] de cada franja
    pages: int            # páginas nuevas
    shift: float          # desplazamiento de lo que se queda en la misma página


def place(baseline: float, count: int, line_height: float, descent: float, ascent: float,
          old_bottom: float, shift: float, bands, limit: float, top: float,
          band_top: float | None = None) -> Placement:
    """Reparte renglones y franjas. Una línea o franja que pasa de `limit` sigue arriba de la
    página siguiente: nunca se parte. `band_top`: dónde empieza arriba una franja (las de texto
    se miden sin el aire de la fuente)."""
    band_top = top if band_top is None else band_top
    positions, slot, y = [], 0, baseline
    for n in range(count):
        if n and y + descent > limit:
            slot, y = slot + 1, top + ascent
        positions.append((slot, y))
        y += line_height
    end = positions[-1][1] + descent if positions else baseline
    out, offset, current = [], shift, slot
    for n, band in enumerate(bands):
        if n == 0 and slot > 0:           # sigue a los renglones, con la misma separación
            offset = end + max(0.0, band.y0 - old_bottom) - band.y0
        if band.y1 + offset > limit and band.y0 + offset > band_top + 0.5:
            current += 1
            offset = band_top - band.y0
        out.append((current, offset))
    return Placement(positions, out, current, shift)


# --------------------------------------------------------------------------- objetos pintados

@dataclass
class Item:
    """Un objeto dibujado en un flujo de contenido."""
    kind: str             # path, image, shade, inline, form, text
    stream: object        # "page" o número de objeto del formulario
    index: int            # instrucción que pinta
    start: int            # primera instrucción (en trazados, la de su construcción)
    box: tuple
    clips: bool = False   # además de pintar, recorta: la construcción no se puede quitar
    gids: tuple = ()      # glifos (texto)
    form: int | None = None    # formulario en el que se entró
    name: str = ""        # nombre del recurso en `Do`


class Scan:
    def __init__(self, page_text):
        self.page_text = page_text
        self.items: list[Item] = []
        self.children: dict[int, list[Item]] = {}   # formulario -> sus objetos
        self.depth: dict[object, tuple[int, int]] = {}   # flujo -> (q sin cerrar, Q de más)
        self.ok = True


def _bbox(points):
    xs, ys = [p.x for p in points], [p.y for p in points]
    return (min(xs), min(ys), max(xs), max(ys))


def _meet(a, b):
    return (max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3]))


def scan(fdoc, pk, index: int, page_text, can_enter) -> Scan:
    """Objetos de la página en orden de dibujo. `can_enter(número)`: si se puede entrar a un
    formulario (dibujado una sola vez); si no, se trata como un objeto entero."""
    result = Scan(page_text)
    if not page_text.ok:
        result.ok = False
        return result
    page = fdoc[index]
    resources = tops._inherited(pk.pages[index].obj, "/Resources")
    shows = {(op.stream, op.index): op for op in page_text.ops}
    _scan(result, "page", page_text.streams["page"], resources, fitz.Matrix(1, 0, 0, 1, 0, 0),
          tuple(page.rect), page_text.page_tm, shows, can_enter, 0)
    return result


def _scan(result, key, data, resources, ctm, clip, page_tm, shows, can_enter, depth):
    instructions = tops.parse(data)
    xobjects = resources.get("/XObject") if resources is not None else None
    stack, width = [], 1.0
    start, points, clipping = None, [], False
    unclosed, extra_q = 0, 0
    out = result.children.setdefault(key, []) if key != "page" else result.items

    def corners(x0, y0, x1, y1, m):
        return [fitz.Point(x, y) * m for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))]

    def scale(m):
        return max(abs(m.a) + abs(m.b), abs(m.c) + abs(m.d))

    def reset():
        nonlocal start, points, clipping
        start, points, clipping = None, [], False

    for i, ins in enumerate(instructions):
        op = str(getattr(ins, "operator", ""))
        args = list(getattr(ins, "operands", []))
        m = ctm * page_tm
        if op == "q":
            stack.append((ctm, clip, width))
            unclosed += 1
        elif op == "Q":
            if stack:
                ctm, clip, width = stack.pop()
                unclosed -= 1
            else:
                extra_q += 1
        elif op == "cm" and len(args) == 6:
            ctm = tops._matrix(args) * ctm
        elif op == "w" and args:
            width = tops._num(args[0])
        elif op in CONSTRUCT:
            if start is None:
                start = i
            v = [tops._num(a) for a in args]
            if op == "re" and len(v) == 4:
                points += corners(v[0], v[1], v[0] + v[2], v[1] + v[3], m)
            else:
                points += [fitz.Point(v[n], v[n + 1]) * m for n in range(0, len(v) - 1, 2)]
        elif op in ("W", "W*"):
            clipping = True
        elif op in PAINT or op == "n":
            if points:
                box = _bbox(points)
                if op in STROKING:
                    pad = width * scale(m) / 2
                    box = (box[0] - pad, box[1] - pad, box[2] + pad, box[3] + pad)
                if op != "n":
                    seen = _meet(box, clip)
                    if seen[2] >= seen[0] and seen[3] >= seen[1]:
                        out.append(Item("path", key, i, start, seen, clips=clipping))
                if clipping:
                    clip = _meet(clip, box)
            reset()
        elif op in ("Tj", "TJ", "'", '"'):
            show = shows.get((key, i))
            if show is not None and show.glyphs:
                out.append(Item("text", key, i, i, clip, gids=tuple(g.gid for g in show.glyphs)))
            elif show is not None:     # fuente que no se puede leer (Type3): el operador entero
                size = abs(show.state.size) * scale(show.tm * m) or 12
                p = fitz.Point(0, 0) * show.tm * m
                out.append(Item("text", key, i, i, (p.x, p.y - size, p.x + size, p.y + size * 0.25)))
        elif op == "sh":
            out.append(Item("shade", key, i, i, clip))
        elif op == "INLINE IMAGE":
            out.append(Item("inline", key, i, i, _meet(_bbox(corners(0, 0, 1, 1, m)), clip)))
        elif op == "Do" and args and xobjects is not None and args[0] in xobjects:
            name = str(args[0])
            xobject = xobjects[args[0]]
            if str(xobject.get("/Subtype", "")) == "/Image":
                out.append(Item("image", key, i, i, _meet(_bbox(corners(0, 0, 1, 1, m)), clip), name=name))
            elif str(xobject.get("/Subtype", "")) == "/Form":
                inner = tops._matrix(xobject.get("/Matrix", [1, 0, 0, 1, 0, 0])) * ctm
                bbox = [tops._num(v) for v in xobject.get("/BBox", [0, 0, 0, 0])]
                box = _meet(_bbox(corners(*bbox, inner * page_tm)), clip)
                objnum = xobject.objgen[0]
                item = Item("form", key, i, i, box, name=name)
                if depth < 8 and can_enter(objnum) and objnum in result.page_text.streams:
                    item.form = objnum
                    _scan(result, objnum, result.page_text.streams[objnum],
                          xobject.get("/Resources", resources), inner, box, page_tm, shows,
                          can_enter, depth + 1)
                out.append(item)
    result.depth[key] = (unclosed, extra_q)


def flatten(result: Scan):
    """Todos los objetos, también los de adentro de los formularios en los que se entró."""
    pending = list(result.items)
    while pending:
        item = pending.pop(0)
        if item.kind == "form" and item.form is not None:
            pending = result.children.get(item.form, []) + pending
        else:
            yield item


# --------------------------------------------------------------------------- copias filtradas

class Partition:
    """Copias del contenido de la página con solo los objetos de cada destino.

    `dest_of(item)` dice a qué destino va cada objeto (los de texto no se preguntan: van por
    glifo, con `glyph_dest`); None = no va a ninguno (el texto que se reescribe)."""

    def __init__(self, fdoc, result: Scan, dest_of, glyph_dest: dict[int, object]):
        self.doc = fdoc
        self.scan = result
        self.dest_of = dest_of
        self.glyph_dest = glyph_dest
        self.instructions = {}
        self.page_holder = None    # dónde agregar recursos en la página (resources_holder)
        self.counter = 0

    def _item_dests(self, item) -> set:
        if item.kind == "form" and item.form is not None:
            out = set()
            for child in self.scan.children.get(item.form, []):
                out |= self._item_dests(child)
            return out
        if item.kind == "text" and item.gids:
            return {self.glyph_dest.get(g) for g in item.gids}
        return {self.dest_of(item)}

    def stream(self, key, dest) -> bytes:
        return tops.unparse(self._filtered(key, dest))

    def _filtered(self, key, dest):
        if key not in self.instructions:
            self.instructions[key] = tops.parse(self.scan.page_text.streams[key])
        source = self.instructions[key]
        rewriter = tops.Rewriter(self.scan.page_text)
        items = self.scan.items if key == "page" else self.scan.children.get(key, [])
        rewriter.removed = {g for g, d in self.glyph_dest.items() if d != dest}
        shows = {op.index: op for op in self.scan.page_text.ops if op.stream == key}
        drop: set[int] = set()
        replace: dict[int, list] = {}
        no_paint = tops.Rewriter._ins([], "n")
        for item in items:
            dests = self._item_dests(item)
            if item.kind == "text":
                if item.gids:
                    if dests != {dest}:
                        replace[item.index] = rewriter._rebuilt(shows[item.index])
                elif dest not in dests:
                    drop.add(item.index)
            elif item.kind == "path":
                if dest not in dests:
                    if item.clips:
                        replace[item.index] = [no_paint]
                    else:
                        drop.update(range(item.start, item.index + 1))
            elif item.kind == "form" and item.form is not None:
                if dest not in dests:
                    drop.add(item.index)
                elif len(dests) > 1:      # el formulario se reparte: una copia para este destino
                    replace[item.index] = [self._clone(key, item, dest)]
            elif dest not in dests:
                drop.add(item.index)
        out = []
        for i, ins in enumerate(source):
            if i in drop:
                continue
            out.extend(replace.get(i, [ins]))
        return out

    def _clone(self, key, item, dest):
        import pikepdf

        data = self.stream(item.form, dest)      # antes de copiar: los anidados agregan recursos
        xref = self.doc.get_new_xref()
        self.doc.update_object(xref, "<<>>")
        self.doc.xref_copy(item.form, xref)
        self.doc.update_stream(xref, data)
        self.counter += 1
        name = f"PliegueX{self.counter}"
        prune_xobjects(self.doc, xref, data)     # cada copia ve solo sus propios formularios
        holder = self.page_holder
        if key != "page" and self.doc.xref_get_key(key, "Resources")[0] != "null":
            holder = resources_holder(self.doc, key)
        add_resource(self.doc, holder, "XObject", name, f"{xref} 0 R")
        return tops.Rewriter._ins([pikepdf.Name("/" + name)], "Do")

    def balance(self, key="page") -> tuple[bytes, bytes]:
        """(prefijo, sufijo) que dejan equilibrados los q/Q del flujo, para que una copia no
        altere a la siguiente."""
        unclosed, extra = self.scan.depth.get(key, (0, 0))
        return b"q\n" * extra, b"\nQ" * unclosed


def prune_xobjects(doc, xref: int, data: bytes) -> None:
    """Deja en los recursos propios de `xref` solo los XObject que dibuja `data`. Así dos copias
    de un mismo contenido no comparten listas, y el editor sabe que cada formulario se dibuja
    una sola vez (y que por lo tanto se puede modificar)."""
    kind, value = doc.xref_get_key(xref, "Resources")
    if kind == "null":
        return
    if kind == "xref":
        value = doc.xref_object(int(value.split()[0]), compressed=True)
        doc.xref_set_key(xref, "Resources", value)
    kind, value = doc.xref_get_key(xref, "Resources/XObject")
    if kind == "xref":
        value = doc.xref_object(int(value.split()[0]), compressed=True)
    elif kind != "dict":
        return
    used = {m.group(1).decode("latin-1") for m in re.finditer(rb"/([^\s/\[\]<>()]+)\s+Do\b", data)}
    kept = [(m.group(1), m.group(2)) for m in re.finditer(r"/([^\s/]+)\s+(\d+ \d+ R)", value)
            if m.group(1) in used]
    doc.xref_set_key(xref, "Resources/XObject", "<<" + "".join(f"/{n} {v}" for n, v in kept) + ">>")


def add_resource(doc, holder: tuple, category: str, name: str, value: str) -> None:
    """Agrega `name` a la categoría de recursos de `holder` = (xref, prefijo de la clave)."""
    xref, prefix = holder
    kind, current = doc.xref_get_key(xref, prefix + category)
    if kind == "xref":
        doc.xref_set_key(int(current.split()[0]), name, value)
    elif kind == "dict":
        doc.xref_set_key(xref, f"{prefix}{category}/{name}", value)
    else:
        doc.xref_set_key(xref, prefix + category, f"<</{name} {value}>>")


def resources_holder(doc, page_xref: int) -> tuple:
    """Dónde están los recursos propios de una página o un formulario: (xref, prefijo). Si la
    página los hereda, primero los copia a la propia página (así agregar no afecta a otras)."""
    kind, value = doc.xref_get_key(page_xref, "Resources")
    if kind == "xref":
        return int(value.split()[0]), ""
    if kind == "dict":
        return page_xref, "Resources/"
    node = page_xref
    for _ in range(32):
        kind, value = doc.xref_get_key(node, "Parent")
        if kind != "xref":
            break
        node = int(value.split()[0])
        kind, value = doc.xref_get_key(node, "Resources")
        if kind in ("xref", "dict"):
            doc.xref_set_key(page_xref, "Resources", value)
            return resources_holder(doc, page_xref)
    doc.xref_set_key(page_xref, "Resources", "<<>>")
    return page_xref, "Resources/"
