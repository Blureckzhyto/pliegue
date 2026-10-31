"""Editar PDF: el texto que no cabe pasa al renglón siguiente y desplaza lo de abajo.

Se mezcla en EditorSession (pdf_editor.py). Reglas:

* Ancho disponible: el del párrafo (todas sus líneas, no solo la editada); un texto corto y
  solo usa la columna de la página, sin chocar con lo que tenga a la derecha (otra celda, una
  imagen).
* Solo se reescribe lo que cambió: las líneas anteriores al cambio no se tocan y, en cuanto el
  texto vuelve a coincidir con el original, las líneas restantes bajan como contenido, intactas.
* Lo de abajo: se parte el contenido por objetos (ver pdf_reflow.py). Baja lo que está en la
  columna y empieza por debajo de la línea de corte.
* Lo que CRUZA la línea de corte (una imagen al lado del párrafo que empieza más arriba, un fondo
  o una regla vertical de toda la página) se queda en su lugar: moverlo la separaría del
  contenido de arriba y partirla la dañaría.
* Lo que ocupa más que la columna (un pie a todo el ancho debajo de dos columnas) tampoco baja:
  es un tope, y lo que no quepa antes de él se trata como fin de página. Igual el pie de página.
* El resultado se comprueba antes de aceptarlo (el texto nuevo está, y no se perdió ninguna
  palabra de las demás). Si no, EditFailed: la página queda como estaba.
"""
from __future__ import annotations

import dataclasses
import re
from collections import Counter
from dataclasses import dataclass, field

from . import errors, pdf_reflow as flow
from . import pdf_textops as tops

try:
    import pymupdf as fitz
except ImportError:  # PyMuPDF < 1.24
    import fitz

MIN_SHRINK = 0.85           # el párrafo se puede reducir hasta el 85 %
BULLETS = ("•", "·", "-", "–", "—", "*", "▪", "◦")


def _pe():
    from . import pdf_editor

    return pdf_editor


def _norm(text: str) -> str:
    return text.replace("\n", " ")


@dataclass
class Zone:
    """Lo que se reescribe: las líneas lines[first:stop] pasan a ser `rows`."""
    index: int
    lines: list
    first: int
    stop: int
    rows: list
    text: str
    src: list                # por carácter de `text`: de qué fragmento original toma el estilo
    spans: list
    area: dict
    line_height: float
    align: str
    font: object = None      # fuente de PyMuPDF si no se escribe con la del PDF
    font_scale: float = 1.0
    style: dict | None = None

    def row_text(self, row) -> str:
        return self.text[row.start:row.end] + ("-" if row.hyphen else "")

    @property
    def old_count(self) -> int:
        return self.stop - self.first

    @property
    def base(self) -> float:
        if self.first < len(self.lines):
            return self.lines[self.first]["lines"][0]["origin"][1]
        return self.lines[-1]["lines"][0]["origin"][1] + self.line_height

    @property
    def removed_ids(self) -> list:
        return [line["id"] for line in self.lines[self.first:self.stop]]

    def row_x(self, n: int) -> float:
        j = self.first + n
        if j < len(self.lines):
            return self.lines[j]["lines"][0]["origin"][0]
        return self.area["rest_x"]

    def row_width(self, n: int) -> float:
        return max(1.0, self.area["right"] - self.row_x(n))

    @property
    def last_bottom(self) -> float:
        """Parte de abajo de la última línea reemplazada (la línea de corte)."""
        line = self.lines[min(self.stop, len(self.lines)) - 1]
        return line["bbox"][3]

    @property
    def last_base(self) -> float:
        return self.lines[min(self.stop, len(self.lines)) - 1]["lines"][0]["origin"][1]


@dataclass
class Obj:
    box: tuple
    gids: tuple = ()
    item: object = None


@dataclass
class ReflowResult:
    status: str                    # done, overflow, none, pending
    data: bytes | None = None
    added_pages: int = 0
    extra_lines: int = 0
    shift: float = 0.0
    message: str = ""
    policy: str = ""
    previews: list = field(default_factory=list)
    candidates: dict = field(default_factory=dict)
    pending: list = field(default_factory=list)


class FlowMixin:
    # ------------------------------------------------------------------ geometría

    def columns(self, index: int) -> list:
        model = self.analyze(index)
        if "columns" not in model:
            width = model["width"]
            columns = flow.detect_columns([l["bbox"] for l in model["lines"]], width)
            if len(columns) == 1:
                # con poco texto en la página, sus líneas no marcan el margen derecho: se usa
                # el mismo margen que a la izquierda (si no, una letra más ya partiría la línea)
                x0, x1 = columns[0]
                margin = min(x0, width - x1)
                margin = 72.0 if margin > width * 0.2 else margin
                columns = [(min(x0, margin), max(x1, width - margin))]
            model["columns"] = columns
        return model["columns"]

    def _column_of(self, index: int, box) -> tuple:
        columns = self.columns(index)
        center = (box[0] + box[2]) / 2
        for column in columns:
            if column[0] - 2 <= box[0] and box[2] <= column[1] + 2:
                return column
        for column in columns:
            if column[0] <= center <= column[1]:
                return column
        return (min(c[0] for c in columns), max(c[1] for c in columns))

    def text_area(self, index: int, unit: dict) -> dict:
        """Dónde puede ir el texto: {left, right, rest_x, column, align}. Ver el inicio del módulo."""
        model = self.analyze(index)
        own = self._unit_lines(unit, index)
        block_id = own[0].get("block_id") if own and own[0].get("block_id") else unit.get("id")
        block = [l for l in model["lines"] if l.get("block_id") == block_id] or own
        box = [min(l["bbox"][0] for l in block), min(l["bbox"][1] for l in block),
               max(l["bbox"][2] for l in block), max(l["bbox"][3] for l in block)]
        column = self._column_of(index, box)
        wide = len(block) >= 2 and box[2] - box[0] >= 0.6 * (column[1] - box[0])
        right = box[2] if wide else max(column[1], box[2])
        line_height = float(own[0]["style"].get("line_height") or own[0]["style"]["size"] * 1.2)
        ids = {l["id"] for l in block}
        low = box[3] + 2 * line_height
        for other in [l for l in model["lines"] if l["id"] not in ids] + model["images"]:
            b = other["bbox"]
            if b[1] < low and b[3] > box[1] and b[0] >= box[2] - 1:   # está a la derecha, a esa altura
                right = min(right, b[0] - 6)
        right = max(right, box[2])
        x0 = own[0]["lines"][0]["origin"][0]
        if len(block) >= 2:
            pos = next((n for n, l in enumerate(block) if l["id"] == own[-1]["id"]), 0)
            neighbour = block[pos + 1] if pos + 1 < len(block) else block[pos - 1 if pos else 0]
            rest_x = neighbour["lines"][0]["origin"][0]
        else:
            rest_x = self._hanging_x(own[0]) or x0
        block_style = next((b["style"] for b in model["blocks"] if b["id"] == block_id), {})
        align = block_style.get("align", "left")
        if len(block) == 1:
            left_gap = box[0] - column[0]
            right_gap = column[1] - box[2]
            if left_gap > 12 and abs(left_gap - right_gap) < 3:
                align = "center"
            elif left_gap > 12 and right_gap < 2:
                align = "right"
        return {"left": min(box[0], x0), "right": right, "rest_x": rest_x, "column": column,
                "align": align, "block": block}

    @staticmethod
    def _hanging_x(line) -> float | None:
        """Una viñeta sola: los renglones siguientes se alinean con el texto, no con la viñeta."""
        for span in line.get("spans") or []:
            words = list(re.finditer(r"\S+", span["text"]))
            if len(words) >= 2 and (words[0].group() in BULLETS or re.fullmatch(r"\d+[.)]", words[0].group())):
                gids = span.get("gids") or []
                return None if not gids else _char_x(span, words[1].start())
            break
        return None

    # ------------------------------------------------------------------ texto del párrafo

    def _old_flow(self, lines, area):
        """Texto original como corre de verdad: el salto al final de una línea llena es solo
        donde el renglón se acabó (va como espacio); el de una línea corta es un salto real.
        Devuelve (texto, fragmento de cada carácter, inicio de cada línea, fragmentos)."""
        chars, src, starts, spans = [], [], [], []
        right = max(l["bbox"][2] for l in lines) if len(lines) > 1 else area["right"]
        for j, line in enumerate(lines):
            starts.append(len(chars))
            text, position = line["text"], 0
            for span in line.get("spans") or []:
                take = span["text"][:max(0, len(text) - position)]
                spans.append(span)
                chars += take
                src += [len(spans) - 1] * len(take)
                position += len(take)
            if j + 1 == len(lines):
                break
            following = lines[j + 1]["text"]
            first_word = following.split(" ")[0] if following else ""
            per_char = (line["bbox"][2] - line["bbox"][0]) / max(1, len(text))
            room = right - line["bbox"][2]
            soft = bool(first_word) and per_char * (len(first_word) + 1) >= room - 1 \
                and first_word not in BULLETS and not re.fullmatch(r"\d+[.)]", first_word)
            if soft and chars and chars[-1] in flow.HYPHENS and first_word[:1].islower():
                chars.pop()            # palabra partida con guion al final del renglón
                src.pop()
            elif chars:
                chars.append(" " if soft else "\n")
                src.append(src[-1])
        return "".join(chars), src, starts, spans

    def flow_text(self, index: int, uid: str) -> str | None:
        """Texto de un párrafo para editarlo como corrido (sin los saltos de renglón)."""
        model = self.analyze(index)
        unit = next((b for b in model["blocks"] if b["id"] == uid), None)
        if unit is None:
            return None
        lines = self._unit_lines(unit, index)
        if not lines or any(not line.get("spans") for line in lines):
            return None
        return self._old_flow(lines, self.text_area(index, unit))[0]

    def edit_geometry(self, index: int, uid: str) -> dict | None:
        """Para el cuadro de edición en el lugar: dónde se puede extender el texto."""
        model = self.analyze(index)
        unit = next((u for u in model["blocks"] + model["lines"] if u["id"] == uid), None)
        if unit is None or model["ocr"]:
            return None
        area = self.text_area(index, unit)
        return {"right": area["right"], "rest_x": area["rest_x"], "align": area["align"]}

    # ------------------------------------------------------------------ renglones nuevos

    def _anchor(self, spans, k, page_text):
        """(fragmento, glifo, operador) de referencia para escribir con el estilo de spans[k]."""
        order = [k] + [n for n in range(len(spans)) if n != k]
        for n in order:
            glyph, op = self._first_glyph(page_text, spans[n])
            if glyph is not None:
                return spans[n], glyph, op
        raise _pe()._Unsupported()

    def _span_width(self, spans, k, text, page_text, scale_factor, memo) -> float:
        key = (k, text, scale_factor)
        if key not in memo:
            pe = _pe()
            span, glyph, op = self._anchor(spans, k, page_text)
            op, span = _scaled(op, span, scale_factor)
            scale = pe._scale(op, glyph, page_text)
            memo[key] = sum(seg[2] for seg in self._segments(op, span, text, scale)) * scale
        return memo[key]

    def _zone(self, index, unit, effective, ov, font_scale=1.0, line_height=None) -> Zone | None:
        """Reparte el texto editado. None si en verdad no cambió nada."""
        pe = _pe()
        lines = self._unit_lines(unit, index)
        if not lines:
            return None
        area = self.text_area(index, unit)
        style = effective["style"]
        new = effective["text"]
        size = float(style["size"]) * font_scale
        height = line_height or float(style.get("line_height") or size * 1.2)
        align = (ov.get("style") or {}).get("align") or (
            style.get("align") if unit.get("level") == "block" else area["align"])
        use_font = not self._mapped(unit, index) or pe._font_style_changed(unit, ov)
        spans_ok = all(line.get("spans") for line in lines)
        if spans_ok:
            old, src, starts, spans = self._old_flow(lines, area)
        else:
            old, src, starts, spans = "\n".join(l["text"] for l in lines), [], [0], []
            for line in lines[:-1]:
                starts.append(starts[-1] + len(line["text"]) + 1)
        old_n, new_n = _norm(old), _norm(new)
        p = 0
        limit = min(len(old_n), len(new_n))
        while p < limit and old_n[p] == new_n[p]:
            p += 1
        if p == len(old_n) == len(new_n) and old.count("\n") == new.count("\n"):
            return None
        s = 0
        while s < limit - p and old_n[-1 - s] == new_n[-1 - s]:
            s += 1
        new_src = []
        if src:     # lo insertado toma el estilo del carácter anterior
            inherited = src[p - 1] if p else src[0]
            new_src = src[:p] + [inherited] * (len(new) - p - s) + (src[len(src) - s:] if s else [])
        whole = font_scale != 1.0 or line_height is not None   # reducir: el párrafo entero
        first = 0 if whole else max((j for j, start in enumerate(starts) if start <= p), default=0)
        font = None
        page_text = self._texts.get(index)
        if use_font:
            font = self.resolve_font(style, new)[0]
            measure = lambda a, b: font.text_length(new[a:b], fontsize=size)   # noqa: E731
        else:
            memo: dict = {}

            def measure(a, b):
                total, i = 0.0, a
                while i < b:
                    j = i
                    while j < b and new_src[j] == new_src[i]:
                        j += 1
                    total += self._span_width(spans, new_src[i], new[i:j], page_text, font_scale, memo)
                    i = j
                return total

        suffixes = {old_n[starts[j]:]: j for j in range(first + 1, len(lines))}

        def converge(position):
            return None if whole else suffixes.get(new_n[position:])

        x_first = lines[first]["lines"][0]["origin"][0]
        x_rest = lines[first + 1]["lines"][0]["origin"][0] if first + 1 < len(lines) else area["rest_x"]
        hyphen = measure_hyphen(font, size, spans, page_text, self, font_scale)
        rows, stop = flow.wrap_rows(new, measure, area["right"] - x_first, area["right"] - x_rest,
                                    starts[first], hyphen, converge)
        stop = len(lines) if stop is None else stop
        zone = Zone(index, lines, first, stop, rows, new, new_src, spans, area, height, align, font,
                    font_scale, style)
        if not whole:
            while zone.rows and zone.first < zone.stop \
                    and zone.row_text(zone.rows[0]) == lines[zone.first]["text"]:
                zone.rows.pop(0)
                zone.first += 1
            while zone.rows and zone.stop > zone.first \
                    and zone.row_text(zone.rows[-1]) == lines[zone.stop - 1]["text"]:
                zone.rows.pop()
                zone.stop -= 1
        return zone

    def _plan_zone(self, zone: Zone, dx=0.0, dy=0.0, positions=None) -> dict:
        """Cómo escribir los renglones: {remove, runs, boxes, by_slot}. `positions`: página
        relativa y línea base de cada renglón (si no, uno debajo del otro)."""
        pe = _pe()
        page_text = self._texts.get(zone.index)
        plan = {"remove": [g for line in zone.lines[zone.first:zone.stop] for span in line["spans"]
                           for g in span["gids"] if g is not None],
                "runs": [], "boxes": [], "by_slot": {}}
        memo: dict = {}
        for n, row in enumerate(zone.rows):
            slot, base = positions[n] if positions else (0, zone.base + n * zone.line_height)
            pieces, i = [], row.start
            while i < row.end:
                j = i
                while j < row.end and zone.src[j] == zone.src[i]:
                    j += 1
                pieces.append([zone.src[i], zone.text[i:j]])
                i = j
            if row.hyphen and pieces:
                pieces[-1][1] += "-"
            if not pieces:
                continue
            total = sum(self._span_width(zone.spans, k, t, page_text, zone.font_scale, memo)
                        for k, t in pieces)
            room = zone.row_width(n)
            text = "".join(t for _k, t in pieces)
            x = zone.row_x(n)
            extra = 0.0
            if zone.align == "center":
                x = (zone.area["left"] + zone.area["right"] - total) / 2 if n or x <= zone.area["left"] + 1 \
                    else x + (room - total) / 2
            elif zone.align == "right":
                x = zone.area["right"] - total
            elif zone.align == "justify" and not row.last and text.count(" "):
                extra = (room - total) / text.count(" ")
            for k, piece in pieces:
                span, glyph, op = self._anchor(zone.spans, k, page_text)
                run_op, run_span = _scaled(op, span, zone.font_scale)
                scale = pe._scale(run_op, glyph, page_text)
                start = fitz.Matrix(1, 0, 0, 1, glyph.x, 0) * op.tm
                origin = fitz.Point(0, op.state.ts) * start * op.state.ctm * page_text.page_tm
                own_base = _own_base(zone.lines, span)
                for items, resolved, advance, seg_text in self._segments(run_op, run_span, piece, scale):
                    if extra:
                        items = pe._justify(items, seg_text, extra / scale)
                        advance += extra / scale * seg_text.count(" ")
                    target_y = base + (span["origin"][1] - own_base)
                    tm = pe._moved(start, op.state.ctm, page_text.page_tm,
                                   x + dx - origin.x, target_y + dy - origin.y)
                    run = tops.Run(run_op.state, tm, items)
                    entry = (op, run, resolved, seg_text)
                    plan["runs"].append(entry)
                    plan["by_slot"].setdefault(slot, []).append(entry)
                    plan["boxes"].append(pe._run_box(run_op, run, span, page_text, advance))
                    x += advance * scale
        return plan

    # ------------------------------------------------------------------ ¿hace falta desplazar?

    def needs_reflow(self, index: int, uid: str, ov: dict, push: bool, adjust: bool) -> bool:
        """¿La edición cambia la cantidad de renglones de forma que haya que mover contenido?
        Los textos arrastrados a otro lugar y las páginas escaneadas no empujan nada."""
        pe = _pe()
        model = self.analyze(index)
        unit = next((u for u in model["blocks"] + model["lines"] if u["id"] == uid), None)
        if unit is None or model["ocr"] or abs(ov.get("dx", 0)) > pe.LIFT_EPS or abs(ov.get("dy", 0)) > pe.LIFT_EPS:
            return False
        if abs((unit["style"].get("dir") or [1, 0])[1]) > 1e-3:
            return False
        try:
            zone = self._zone(index, unit, pe._effective(unit, ov), ov)
        except pe._Unsupported:
            return False
        if zone is None:
            return False
        extra = len(zone.rows) - zone.old_count
        if extra < 0:
            return adjust
        if extra == 0:
            return False
        if push:
            return True
        bottom = zone.base + (len(zone.rows) - 1) * zone.line_height
        return bottom > self._limits(index, zone)[0]    # sin desplazar, pero se sale de la página

    # ------------------------------------------------------------------ límites de la página

    def _limits(self, index, zone) -> tuple:
        """(límite inferior, arriba de la página, línea del pie de página o None)."""
        model = self.analyze(index)
        height = model["height"]
        boxes = [l["bbox"] for l in model["lines"]] + [i["bbox"] for i in model["images"]]
        ordered = sorted(model["lines"], key=lambda l: l["bbox"][1])
        footer = None
        for n, line in enumerate(ordered):
            if line["bbox"][1] < height * 0.88 or n == 0:
                continue
            gap = line["bbox"][1] - ordered[n - 1]["bbox"][3]
            if gap >= 1.5 * zone.line_height and line["id"] not in zone.removed_ids:
                footer = line["bbox"][1]
                break
        body = [b for b in boxes if footer is None or b[1] < footer - 0.5]
        left = min((b[0] for b in boxes), default=36)
        top = min((b[1] for b in body), default=36)
        margin = min(72.0, max(28.0, min(left, top)))
        limit = max(height - margin, max((b[3] for b in body), default=0))
        if footer is not None:
            limit = min(limit, footer - zone.line_height * 0.5)
        return limit, top, footer

    # ------------------------------------------------------------------ colocar

    def _objects(self, index, zone):
        """Lo que hay en la página, sin lo que se reescribe: líneas de texto enteras y los
        demás objetos dibujados."""
        model = self.analyze(index)
        page_text = self._texts[index]
        self._editable_stream(-1)         # prepara el conteo de usos de los formularios
        uses = self._form_uses or {}
        with self.lock:
            result = flow.scan(self.doc, self._pk, index, page_text,
                               lambda n: page_text.forms.get(n, 0) == 1 and uses.get(n, 0) <= 1)
        removed = set(zone.removed_ids)
        lines, owned = [], set()
        for line in model["lines"]:
            gids = tuple(g for span in line["spans"] for g in span["gids"] if g is not None)
            owned.update(gids)
            if line["id"] in removed:
                continue
            base, size = line["lines"][0]["origin"][1], float(line["style"]["size"])
            lines.append(Obj((line["bbox"][0], base - 0.8 * size, line["bbox"][2], base + 0.25 * size), gids))
        others = []
        for item in flow.flatten(result):
            if item.kind == "text" and item.gids:
                loose = [g for g in item.gids if g not in owned]
                if loose:       # glifos que PyMuPDF no ubicó en ninguna línea
                    points = [page_text.glyphs[g].origin for g in loose]
                    size = abs(page_text.op_of[loose[0]].state.size) or 10
                    others.append(Obj((min(p[0] for p in points), min(p[1] for p in points) - size * 0.8,
                                       max(p[0] for p in points) + size,
                                       max(p[1] for p in points) + size * 0.25), tuple(loose)))
            else:
                others.append(Obj(item.box, item=item))
        return result, lines, others

    def _flow(self, index, zone, push, adjust, policy=None) -> dict | None:
        """Calcula dónde cae todo. Devuelve el plan o None si no cabe con esa política."""
        limit, top, footer = self._limits(index, zone)
        column = zone.area["column"]
        lines_count = len(zone.rows)
        result, lines, others = self._objects(index, zone)
        split_base, split_y = zone.last_base, zone.last_bottom
        moved, barrier = [], None
        descent = max(zone.lines[0]["bbox"][3] - zone.lines[0]["lines"][0]["origin"][1], 1.0)
        ascent = max(zone.lines[0]["lines"][0]["origin"][1] - zone.lines[0]["bbox"][1], 1.0)
        if zone.font_scale != 1.0:
            descent, ascent = descent * zone.font_scale, ascent * zone.font_scale

        def inside(box):
            width = box[2] - box[0]
            overlap = min(box[2], column[1]) - max(box[0], column[0])
            if overlap <= 0 and width > 0:
                return False
            return width <= (column[1] - column[0]) + 6 and overlap >= width * 0.5

        def touches(box):
            return min(box[2], column[1]) - max(box[0], column[0]) > 0

        for obj in lines + others:
            box = obj.box
            is_line = obj.item is None and bool(obj.gids)
            below = (box[3] - 0.25 * (box[3] - box[1]) > split_base + 0.5 if is_line
                     else box[1] >= split_y - flow.SPLIT_TOL)
            if not below or (footer is not None and box[1] >= footer - 1):
                continue
            if inside(box):
                moved.append(obj)
            elif touches(box):                 # más ancho que la columna: no baja, es un tope
                barrier = box[1] if barrier is None else min(barrier, box[1])
        if not push:
            moved = []
        if barrier is not None:
            limit = min(limit, barrier - zone.line_height * 0.25)
        original = float(zone.style.get("line_height") or zone.style["size"] * 1.2)
        extra = lines_count * zone.line_height - zone.old_count * original
        if extra < 0 and not adjust:
            extra = 0.0
        bands = flow.make_bands(moved)
        size = float(zone.style["size"])
        placed = flow.place(zone.base, lines_count, zone.line_height, descent, ascent, split_y, extra,
                            bands, limit, top, top + max(0.0, ascent / zone.font_scale - 0.8 * size))
        fits = placed.pages == 0
        if policy in (None, "shrink", "tighten") and not fits:
            return None
        return {"zone": zone, "scan": result, "lines": lines, "others": others, "bands": bands,
                "placed": placed, "extra": extra, "limit": limit, "top": top, "split": split_y,
                "column": column, "footer": footer}

    # ------------------------------------------------------------------ aplicar

    def _bake(self, index, plan, accept_all=False, font_ok=None) -> ReflowResult:
        """Aplica el plan sobre una copia del documento y comprueba el resultado."""
        pe = _pe()
        zone, placed, bands = plan["zone"], plan["placed"], plan["bands"]
        page_text = self._texts[index]
        written = None
        if zone.font is None:
            written = self._plan_zone(zone, positions=placed.rows)
            pending = []
            for op, _run, resolved, seg_text in written["runs"]:
                if resolved is not None and resolved.substitute and not accept_all and font_ok != resolved.label:
                    font = op.state.font
                    pending.append((font.name, resolved, [c for c in seg_text if c not in " \xa0"
                                                          and font.code_for(c) is None]))
            if pending:
                name, resolved, _missing = pending[0]
                missing = sorted({c for _n, _r, m in pending for c in m})
                unit_style = {"text": zone.text, "style": zone.style}
                return ReflowResult("pending", pending=[self._substitution_info(
                    index, zone.lines[0]["id"], unit_style, name, resolved.label, missing, resolved.choice)])
        stay = (0, 0.0)
        glyph_dest = {g.gid: stay for g in page_text.glyphs}
        item_dest: dict[int, tuple] = {}
        for line in zone.lines[zone.first:zone.stop]:
            for span in line["spans"]:
                for gid in span["gids"]:
                    if gid is not None:
                        glyph_dest[gid] = None
        for band, where in zip(bands, placed.bands):
            for obj in band.objects:
                dest = (where[0], round(where[1], 4))
                for gid in obj.gids:
                    glyph_dest[gid] = dest
                if obj.item is not None:
                    item_dest[id(obj.item)] = dest
        doc = fitz.open(stream=self.data, filetype="pdf")
        try:
            page = doc[index]
            rect = page.rect
            holder = flow.resources_holder(doc, page.xref)
            partition = flow.Partition(doc, plan["scan"], lambda item: item_dest.get(id(item), stay),
                                       glyph_dest)
            partition.page_holder = holder
            dests = sorted({stay} | {(w[0], round(w[1], 4)) for w in placed.bands})
            copies: dict[int, list[tuple]] = {}
            prefix, suffix = partition.balance()
            for dest in dests:
                data = prefix + partition.stream("page", dest) + suffix
                copies.setdefault(dest[0], []).append((dest[1], data))
            layers = self._row_layers(doc, page, page_text, written) if written else {}
            parts = {}
            for slot in sorted(set(copies) | set(layers) | set(p[0] for p in placed.rows)):
                body = []
                groups = sorted(copies.get(slot, []), key=lambda g: abs(g[0]) > 1e-6)
                fixed = [g for g in groups if abs(g[0]) <= 1e-6]
                shifted = [g for g in groups if abs(g[0]) > 1e-6]
                for offset, data in fixed:
                    body.append(_wrapped(data, offset, page_text.page_tm))
                if slot in layers:
                    body.append(layers[slot] + b"\n")
                for offset, data in shifted:
                    body.append(_wrapped(data, offset, page_text.page_tm))
                parts[slot] = b"\n".join(body)
            contents = page.get_contents()
            if contents:
                doc.update_stream(contents[0], parts.get(0, b""))
                for other in contents[1:]:
                    doc.update_stream(other, b"")
            else:
                pe._add_content(doc, page, parts.get(0, b""), True)
            page_xref = page.xref
            for slot in range(1, placed.pages + 1):   # insertar páginas invalida los objetos Page
                new = doc.new_page(index + slot, width=rect.width, height=rect.height)
                _copy_page_setup(doc, page_xref, new, holder)
                _set_contents(doc, new, parts.get(slot, b""))
            pages = {slot: doc[index + slot] for slot in range(placed.pages + 1)}
            if partition.counter or placed.pages:
                for slot, target in pages.items():
                    flow.prune_xobjects(doc, target.xref, parts.get(slot, b""))
            self._move_annots(doc, pages[0], pages, plan, placed, page_text)
            if zone.font is not None:
                self._font_rows(doc, pages, plan, placed)
            data = doc.tobytes(garbage=1, deflate=True)
            self._check(doc, index, placed.pages, zone, page_text)
        finally:
            doc.close()
        return ReflowResult("done", data=data, added_pages=placed.pages,
                            extra_lines=len(zone.rows) - zone.old_count, shift=placed.shift)

    def _row_layers(self, doc, page, page_text, written) -> dict:
        """Flujo con los renglones nuevos de cada página. Va a nivel de página aunque el texto
        original estuviera en un formulario: su fuente se agrega a los recursos de la página."""
        rewriter = tops.Rewriter(page_text)
        layers = {}
        fonts_added: dict = {}
        names: dict = {}
        for slot, entries in written["by_slot"].items():
            runs = []
            for op, run, resolved, seg_text in entries:
                if resolved is not None:
                    run.font_res = self._register_font(doc, page, "page", resolved, seg_text, fonts_added)
                elif op.stream != "page":
                    run.font_res = self._page_font(doc, page, op, names)
                if op.stream != "page":
                    _share_state(doc, page, op)
                runs.append(run)
            layers[slot] = rewriter.layer_stream(runs)
        return layers

    @staticmethod
    def _page_font(doc, page, op, names) -> str:
        objnum = op.state.font.objnum
        if objnum not in names:
            used = {entry[4] for entry in page.get_fonts(full=True)}
            n = len(names) + 1
            while f"PliegueP{n}" in used:
                n += 1
            names[objnum] = f"PliegueP{n}"
            flow.add_resource(doc, flow.resources_holder(doc, page.xref), "Font", names[objnum],
                              f"{objnum} 0 R")
        return names[objnum]

    def _font_rows(self, doc, pages, plan, placed) -> None:
        """Renglones con otra fuente (la que pidió el usuario, o texto que no se pudo ubicar en
        el contenido, que primero se quita de su caja)."""
        pe = _pe()
        zone = plan["zone"]
        page = pages[0]
        if any(not any(g is not None for s in l.get("spans") or [] for g in s["gids"])
               for l in zone.lines[zone.first:zone.stop]):
            for line in zone.lines[zone.first:zone.stop]:
                for rect in pe._text_rects(line):
                    page.add_redact_annot(rect, fill=False)
            page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE,
                                  graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                                  text=fitz.PDF_REDACT_TEXT_REMOVE)
        size = float(zone.style["size"]) * zone.font_scale
        for slot in sorted({p[0] for p in placed.rows}):
            target = pages[slot]
            writer = fitz.TextWriter(target.rect, color=pe._rgb(zone.style["color"]))
            for n, row in enumerate(zone.rows):
                if placed.rows[n][0] == slot:
                    pe._append_aligned(writer, zone.row_text(row), zone.font, size, zone.row_x(n),
                                       placed.rows[n][1], zone.row_width(n), zone.align, row.last)
            writer.write_text(target, overlay=True)

    def _move_annots(self, doc, page, pages, plan, placed, page_text) -> None:
        """Enlaces, notas y campos de formulario siguen al contenido que estaba debajo."""
        split, column, footer = plan["split"], plan["column"], plan["footer"]
        bands = plan["bands"]
        moved_away: dict[int, list] = {}
        inverse = ~page_text.page_tm
        for annot in page.annot_xrefs():
            xref = annot[0]
            kind, value = doc.xref_get_key(xref, "Rect")
            if kind != "array":
                continue
            numbers = [float(v) for v in value.strip("[]").split()]
            box = fitz.Rect(numbers) * page_text.page_tm
            box.normalize()
            overlap = min(box.x1, column[1]) - max(box.x0, column[0])
            if box.y0 < split - flow.SPLIT_TOL or (footer is not None and box.y0 >= footer - 1) \
                    or overlap < box.width * 0.5:      # un resaltado sobresale un poco de su texto
                continue
            where = None
            for band, spot in zip(bands, placed.bands):
                if band.y0 - 2 <= box.y0:
                    where = spot
            if where is None and placed.bands:
                where = placed.bands[0]
            if where is None:
                continue
            slot, offset = where
            delta = fitz.Point(0, offset) * inverse - fitz.Point(0, 0) * inverse
            numbers = [numbers[0] + delta.x, numbers[1] + delta.y, numbers[2] + delta.x, numbers[3] + delta.y]
            doc.xref_set_key(xref, "Rect", "[" + " ".join(f"{v:g}" for v in numbers) + "]")
            kind, value = doc.xref_get_key(xref, "QuadPoints")
            if kind == "array":
                quad = [float(v) for v in value.strip("[]").split()]
                quad = [v + (delta.x if n % 2 == 0 else delta.y) for n, v in enumerate(quad)]
                doc.xref_set_key(xref, "QuadPoints", "[" + " ".join(f"{v:g}" for v in quad) + "]")
            if slot:
                moved_away.setdefault(slot, []).append(xref)
        for slot, xrefs in moved_away.items():
            keep = [a[0] for a in page.annot_xrefs() if a[0] not in xrefs]
            doc.xref_set_key(page.xref, "Annots", "[" + " ".join(f"{x} 0 R" for x in keep) + "]")
            target = pages[slot]
            doc.xref_set_key(target.xref, "Annots", "[" + " ".join(f"{x} 0 R" for x in xrefs) + "]")
            for xref in xrefs:
                doc.xref_set_key(xref, "P", f"{target.xref} 0 R")

    def _check(self, doc, index, added, zone, page_text) -> None:
        """No se acepta si el texto nuevo no quedó o si se perdió cualquier otra palabra."""
        pe = _pe()
        uid = zone.lines[0]["id"]

        def plain(text):
            return pe._norm_text(text).replace("-", "").replace("‐", "")

        with fitz.open(stream=self.data, filetype="pdf") as original:
            before = Counter(w[4] for w in original[index].get_text("words"))
        for line in zone.lines[zone.first:zone.stop]:
            for word in line["text"].split():
                before[word] -= 1
        after_words: Counter = Counter()
        joined = ""
        for k in range(index, index + added + 1):
            after_words.update(w[4] for w in doc[k].get_text("words"))
            joined += doc[k].get_text("text")
        missing = [w for w, n in before.items() if n > 0 and after_words[w] < n]
        found = plain(joined)
        if any(plain(zone.row_text(row)) not in found for row in zone.rows):
            raise pe.EditFailed({uid: "el texto nuevo no quedó en la página"})
        if missing:
            errors.log.error("Texto perdido al desplazar: %s", ", ".join(missing[:20]))
            raise pe.EditFailed({uid: "al desplazar el contenido se perdería texto de la página"})

    # ------------------------------------------------------------------ operación completa

    def _clean_session(self, index, state, uid):
        """Sesión sobre la que aplicar: si la página tiene otros cambios sin integrar, primero
        se aplican (lo que se desplaza incluye lo que el usuario haya movido o agregado).
        Devuelve (sesión, unidad, cambios pendientes de confirmar)."""
        pe = _pe()
        model = self.analyze(index)
        unit = next(u for u in model["blocks"] + model["lines"] if u["id"] == uid)
        others = {k: v for k, v in state.get("overrides", {}).items() if k != uid and pe._is_change(v)}
        if not others and not state.get("added") and not state.get("z"):
            return self, unit, []
        rest = {"overrides": others, "added": state.get("added", []), "z": state.get("z")}
        if rest["z"]:
            rest["z"] = [u for u in rest["z"] if u != uid] or None
        doc = fitz.open(stream=self.data, filetype="pdf")
        pending: list = []
        try:
            self._apply_page(doc, index, rest, pending)
            data = doc.tobytes(garbage=1, deflate=True)
        finally:
            doc.close()
        if pending:
            return None, None, pending
        session = pe.EditorSession.from_bytes(self.src, data)
        fresh = session.analyze(index)
        wanted = [line["text"] for line in self._unit_lines(unit, index)]
        origin = self._unit_lines(unit, index)[0]["lines"][0]["origin"]
        candidates = [l for l in fresh["lines"] if l["text"] == wanted[0]]
        if not candidates:
            session.close()
            raise pe.EditFailed({uid: "no se encontró el texto después de los otros cambios"})
        found = min(candidates, key=lambda l: abs(l["lines"][0]["origin"][1] - origin[1])
                    + abs(l["lines"][0]["origin"][0] - origin[0]))
        if unit.get("level") == "block":
            block = next(b for b in fresh["blocks"] if b["id"] == found["block_id"])
            return session, block, []
        return session, found, []

    def reflow(self, index, state, uid, push=True, adjust=False, accept_all=False) -> ReflowResult:
        """Aplica una edición que cambia la cantidad de renglones. Si no cabe en la página,
        devuelve `overflow` con el resultado de cada opción ya calculado (para la vista previa).
        No cambia la sesión: el resultado se adopta con `adopt`."""
        pe = _pe()
        ov = state["overrides"][uid]
        session, unit, pending = self._clean_session(index, state, uid)
        if pending:
            return ReflowResult("pending", pending=pending)
        try:
            return session._reflow(index, unit, ov, uid, push, adjust, accept_all)
        finally:
            if session is not self:
                session.close()

    def _reflow(self, index, unit, ov, uid, push, adjust, accept_all) -> ReflowResult:
        pe = _pe()
        effective = pe._effective(unit, ov)
        zone = self._zone(index, unit, effective, ov)
        if zone is None:
            return ReflowResult("none")
        font_ok = None if accept_all else ov.get("font_ok")
        plan = self._flow(index, zone, push, adjust)
        if plan is not None:
            result = self._bake(index, plan, accept_all, font_ok)
            if result.status == "done":
                result.message = _message(result, zone, index)
            for item in result.pending:
                item["uid"] = uid
            return result
        candidates = {}
        newpage = self._flow(index, zone, push, adjust, "newpage")
        result = self._bake(index, newpage, accept_all, font_ok)
        if result.status == "pending":
            for item in result.pending:
                item["uid"] = uid
            return result
        result.policy = "newpage"
        result.message = _message(result, zone, index)
        candidates["newpage"] = result
        shrink = self._try_shrink(index, unit, effective, ov, zone, push, adjust, accept_all, font_ok)
        if shrink is not None:
            candidates["shrink"] = shrink
        tighten = self._try_tighten(index, unit, effective, ov, zone, push, adjust, accept_all, font_ok)
        if tighten is not None:
            candidates["tighten"] = tighten
        for candidate in candidates.values():
            candidate.previews = _previews(candidate.data, index, candidate.added_pages)
        return ReflowResult("overflow", extra_lines=len(zone.rows) - zone.old_count,
                            candidates=candidates)

    def _try_shrink(self, index, unit, effective, ov, zone, push, adjust, accept_all, font_ok):
        """Letra un poco menor, solo en el párrafo editado, hasta el 85 %."""
        pe = _pe()
        size = float(effective["style"]["size"])
        height = float(effective["style"].get("line_height") or size * 1.2)
        percent = 99
        while percent >= int(MIN_SHRINK * 100):
            factor = percent / 100
            attempt = self._zone(index, unit, effective, ov, font_scale=factor, line_height=height * factor)
            plan = self._flow(index, attempt, push, adjust, "shrink") if attempt else None
            if plan is not None:
                result = self._bake(index, plan, accept_all, font_ok)
                if result.status != "done":
                    return None
                result.policy = "shrink"
                result.message = f"El párrafo quedó en {pe._fmt_size(size * factor)} pt ({percent} %) y cabe en la página."
                return result
            percent -= 1
        return None

    def _try_tighten(self, index, unit, effective, ov, zone, push, adjust, accept_all, font_ok):
        """Menos interlineado, solo en el párrafo editado, hasta 1,0 × el tamaño de letra."""
        pe = _pe()
        size = float(effective["style"]["size"])
        height = float(effective["style"].get("line_height") or size * 1.2)
        step = 0
        while height - step * 0.25 >= size:
            current = height - step * 0.25
            attempt = self._zone(index, unit, effective, ov, line_height=current)
            plan = self._flow(index, attempt, push, adjust, "tighten") if attempt else None
            if plan is not None:
                result = self._bake(index, plan, accept_all, font_ok)
                if result.status != "done":
                    return None
                result.policy = "tighten"
                result.message = f"El interlineado del párrafo quedó en {pe._fmt_size(current)} pt y cabe en la página."
                return result
            step += 1
        return None


# --------------------------------------------------------------------------- utilidades

def _scaled(op, span, factor):
    if factor == 1.0:
        return op, span
    state = op.state.copy()
    state.size *= factor
    new_span = dict(span)
    if span.get("space_w"):
        new_span["space_w"] = span["space_w"] * factor
    return dataclasses.replace(op, state=state), new_span


def _own_base(lines, span) -> float:
    for line in lines:
        if any(s is span for s in line.get("spans") or []):
            return line["lines"][0]["origin"][1]
    return span["origin"][1]


def _char_x(span, position: int) -> float | None:
    box = span["bbox"]
    if position >= len(span["text"]):
        return None
    return box[0] + (box[2] - box[0]) * position / max(1, len(span["text"]))


def measure_hyphen(font, size, spans, page_text, session, factor) -> float:
    if font is not None:
        return font.text_length("-", fontsize=size)
    try:
        return session._span_width(spans, len(spans) - 1, "-", page_text, factor, {})
    except Exception:
        return size * 0.35


def _wrapped(data: bytes, offset: float, page_tm) -> bytes:
    """El contenido entre q/Q y, si hace falta, trasladado `offset` puntos hacia abajo."""
    if abs(offset) <= 1e-6:
        return b"q\n" + data + b"\nQ\n"
    inverse = ~page_tm
    delta = fitz.Point(0, offset) * inverse - fitz.Point(0, 0) * inverse
    return b"q\n1 0 0 1 %s %s cm\n" % (_num(delta.x), _num(delta.y)) + data + b"\nQ\n"


def _num(value: float) -> bytes:
    return f"{round(value, 4):.4f}".rstrip("0").rstrip(".").encode() or b"0"


def _copy_page_setup(doc, page_xref, new, holder) -> None:
    """La página nueva con el mismo tamaño y los mismos recursos que la original."""
    for key in ("MediaBox", "CropBox"):
        kind, value = doc.xref_get_key(page_xref, key)
        if kind != "null":
            doc.xref_set_key(new.xref, key, value)
    if holder[1]:
        doc.xref_set_key(new.xref, "Resources", doc.xref_get_key(page_xref, "Resources")[1])
    else:
        doc.xref_set_key(new.xref, "Resources", f"{holder[0]} 0 R")


def _set_contents(doc, page, data: bytes) -> None:
    xref = doc.get_new_xref()
    doc.update_object(xref, "<<>>")
    doc.update_stream(xref, data)
    doc.xref_set_key(page.xref, "Contents", f"{xref} 0 R")


def _share_state(doc, page, op) -> None:
    """Colores y estados gráficos con nombre que usa un texto de un formulario: la capa nueva
    va a nivel de página y tiene que encontrarlos ahí."""
    instructions = list(op.state.gstate) + list(op.state.fill) + list(op.state.stroke)
    for ins in instructions:
        operator = str(ins.operator)
        category = {"gs": "ExtGState", "cs": "ColorSpace", "CS": "ColorSpace"}.get(operator)
        if not category or not ins.operands:
            continue
        name = str(ins.operands[0]).lstrip("/")
        kind, value = doc.xref_get_key(op.stream, f"Resources/{category}/{name}")
        if kind != "null":
            flow.add_resource(doc, flow.resources_holder(doc, page.xref), category, name, value)


def _previews(data: bytes, index: int, added: int, zoom: float = 0.55) -> list:
    from PIL import Image

    images = []
    with fitz.open(stream=data, filetype="pdf") as doc:
        for k in range(index, index + added + 1):
            pix = doc[k].get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
            images.append(Image.frombytes("RGB", (pix.width, pix.height), pix.samples))
    return images


def _message(result: ReflowResult, zone: Zone, index: int) -> str:
    pe = _pe()
    if result.added_pages:
        first = index + 2
        if result.added_pages == 1:
            return f"Se creó la página {first} con el contenido sobrante."
        return f"Se crearon las páginas {first} a {first + result.added_pages - 1} con el contenido sobrante."
    lines = result.extra_lines
    if lines > 0:
        word = "línea más" if lines == 1 else "líneas más"
        if abs(result.shift) > 0.01:
            return f"El texto ocupa {lines} {word}; el contenido de abajo se desplazó {pe._fmt_size(result.shift)} pt."
        return f"El texto ocupa {lines} {word}."
    if lines < 0 and abs(result.shift) > 0.01:
        word = "línea menos" if lines == -1 else "líneas menos"
        return f"El texto ocupa {-lines} {word}; el contenido de abajo subió {pe._fmt_size(-result.shift)} pt."
    return "Cambio aplicado."
