from collections import Counter
from difflib import SequenceMatcher
from typing import Optional
from xml.etree import ElementTree as ET
import unicodedata

from docx import Document
from docx.oxml.ns import qn
from docx.text.paragraph import Paragraph

from .models import DocxFontReference


A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
WP_NS = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
EMU_PER_POINT = 12700.0


def match_normalize(value: str) -> str:
    """Normalize Word/PDF text for matching without destroying meaningful punctuation."""
    value = unicodedata.normalize("NFKC", value or "")
    translations = {
        ord("\u00a0"): " ",
        ord("\u2007"): " ",
        ord("\u202f"): " ",
        ord("\u2010"): "-",
        ord("\u2011"): "-",
        ord("\u2012"): "-",
        ord("\u2013"): "-",
        ord("\u2014"): "-",
        ord("\u2212"): "-",
        ord("\u2018"): "'",
        ord("\u2019"): "'",
        ord("\u201c"): '"',
        ord("\u201d"): '"',
        ord("\u200b"): None,
        ord("\ufeff"): None,
    }
    value = value.translate(translations)
    return " ".join(value.split()).strip().casefold()


def _style_chain(style):
    seen = set()
    current = style
    while current is not None:
        marker = id(current)
        if marker in seen:
            break
        seen.add(marker)
        yield current
        try:
            current = current.base_style
        except Exception:
            break


def _rfonts_values(rfonts):
    if rfonts is None:
        return [], []
    explicit, theme = [], []
    for key in ("ascii", "hAnsi", "eastAsia", "cs"):
        try:
            value = rfonts.get(qn(f"w:{key}"))
        except Exception:
            value = None
        if value:
            explicit.append(value)
    for key in ("asciiTheme", "hAnsiTheme", "eastAsiaTheme", "cstheme"):
        try:
            value = rfonts.get(qn(f"w:{key}"))
        except Exception:
            value = None
        if value:
            theme.append(value)
    return explicit, theme


def _element_rfonts(element):
    try:
        rpr = element.rPr
        if rpr is None:
            return [], []
        return _rfonts_values(rpr.rFonts)
    except Exception:
        return [], []


def _theme_font_map(document):
    result = {}
    try:
        theme_part = None
        for part in document.part.package.parts:
            name = str(getattr(part, "partname", ""))
            if name.endswith("/word/theme/theme1.xml") or name.endswith("/theme/theme1.xml"):
                theme_part = part
                break
        if theme_part is None:
            return result
        root = ET.fromstring(theme_part.blob)
        ns = {"a": A_NS}
        major_latin = root.find(".//a:themeElements/a:fontScheme/a:majorFont/a:latin", ns)
        minor_latin = root.find(".//a:themeElements/a:fontScheme/a:minorFont/a:latin", ns)
        major = major_latin.attrib.get("typeface") if major_latin is not None else None
        minor = minor_latin.attrib.get("typeface") if minor_latin is not None else None
        if major:
            for key in ("majorAscii", "majorHAnsi", "majorEastAsia", "majorBidi", "majorCs"):
                result[key.casefold()] = major
        if minor:
            for key in ("minorAscii", "minorHAnsi", "minorEastAsia", "minorBidi", "minorCs"):
                result[key.casefold()] = minor
    except Exception:
        pass
    return result


def _font_alias_map(document):
    """Read Word fontTable.xml alternate-name mappings (e.g. declared font -> Word alternate)."""
    result = {}
    try:
        font_part = None
        for part in document.part.package.parts:
            name = str(getattr(part, "partname", ""))
            if name.endswith("/word/fontTable.xml") or name.endswith("/fontTable.xml"):
                font_part = part
                break
        if font_part is None:
            return result
        root = ET.fromstring(font_part.blob)
        ns = {"w": W_NS}
        for font in root.findall(".//w:font", ns):
            declared = font.attrib.get(f"{{{W_NS}}}name")
            alt = font.find("w:altName", ns)
            alternate = alt.attrib.get(f"{{{W_NS}}}val") if alt is not None else None
            if declared and alternate:
                result[declared.casefold()] = alternate
    except Exception:
        pass
    return result


def _resolve_theme_name(theme_names, theme_map):
    for name in theme_names:
        if name:
            resolved = theme_map.get(str(name).casefold())
            if resolved:
                return resolved
    return None


def _resolve_alias(name, alias_map):
    if not name:
        return None, None
    alternate = alias_map.get(str(name).casefold())
    return (alternate or name), (name if alternate else None)


def _effective_font_name(run, paragraph, document, theme_map, alias_map):
    """Return (effective_family, declared_family_if_alias_was_used)."""
    candidates = []
    try:
        if run.font.name:
            candidates.append(run.font.name)
    except Exception:
        pass

    explicit, theme = _element_rfonts(run._r)
    candidates.extend(explicit)
    resolved_theme = _resolve_theme_name(theme, theme_map)
    if resolved_theme:
        candidates.append(resolved_theme)

    try:
        run_style = run.style
    except Exception:
        run_style = None
    if run_style is not None:
        for style in _style_chain(run_style):
            try:
                if style.font.name:
                    candidates.append(style.font.name)
            except Exception:
                pass
            explicit, theme = _element_rfonts(style._element)
            candidates.extend(explicit)
            resolved_theme = _resolve_theme_name(theme, theme_map)
            if resolved_theme:
                candidates.append(resolved_theme)

    try:
        paragraph_style = paragraph.style
    except Exception:
        paragraph_style = None
    if paragraph_style is not None:
        for style in _style_chain(paragraph_style):
            try:
                if style.font.name:
                    candidates.append(style.font.name)
            except Exception:
                pass
            explicit, theme = _element_rfonts(style._element)
            candidates.extend(explicit)
            resolved_theme = _resolve_theme_name(theme, theme_map)
            if resolved_theme:
                candidates.append(resolved_theme)

    try:
        normal = document.styles["Normal"]
        for style in _style_chain(normal):
            try:
                if style.font.name:
                    candidates.append(style.font.name)
            except Exception:
                pass
            explicit, theme = _element_rfonts(style._element)
            candidates.extend(explicit)
            resolved_theme = _resolve_theme_name(theme, theme_map)
            if resolved_theme:
                candidates.append(resolved_theme)
    except Exception:
        pass

    for value in candidates:
        if value:
            return _resolve_alias(value, alias_map)
    return None, None


def _effective_size(run, paragraph, document):
    candidates = []
    try: candidates.append(run.font.size)
    except Exception: pass
    try:
        if run.style is not None:
            for style in _style_chain(run.style): candidates.append(style.font.size)
    except Exception: pass
    try:
        if paragraph.style is not None:
            for style in _style_chain(paragraph.style): candidates.append(style.font.size)
    except Exception: pass
    try:
        for style in _style_chain(document.styles["Normal"]): candidates.append(style.font.size)
    except Exception: pass
    for value in candidates:
        if value is not None:
            try: return float(value.pt)
            except Exception: pass
    return None


def _effective_bool(run, paragraph, document, attr):
    fonts = []
    try: fonts.append(run.font)
    except Exception: pass
    try:
        if run.style is not None:
            for style in _style_chain(run.style): fonts.append(style.font)
    except Exception: pass
    try:
        if paragraph.style is not None:
            for style in _style_chain(paragraph.style): fonts.append(style.font)
    except Exception: pass
    try:
        for style in _style_chain(document.styles["Normal"]): fonts.append(style.font)
    except Exception: pass
    for font in fonts:
        try:
            value = getattr(font, attr)
            if value is not None: return bool(value)
        except Exception:
            continue
    return None


def _iter_table_paragraphs(table):
    for row in table.rows:
        for cell in row.cells:
            for paragraph in cell.paragraphs:
                yield paragraph
            for nested in cell.tables:
                yield from _iter_table_paragraphs(nested)


def _element_key(element):
    try:
        return element.getroottree().getpath(element)
    except Exception:
        return str(id(element))


def _all_paragraphs(document):
    """
    HYBRID iterator. V3.3 replaced python-docx traversal and caused a regression
    (e.g. 1641 indexed runs became 10). V3.4 keeps ALL V3.2 paragraphs/tables,
    then adds raw XML-only paragraphs such as floating text boxes.
    """
    seen = set()

    # First: preserve the proven V3.2 indexing path.
    for paragraph in document.paragraphs:
        key = _element_key(paragraph._p)
        if key not in seen:
            seen.add(key)
            yield paragraph

    for table in document.tables:
        for paragraph in _iter_table_paragraphs(table):
            key = _element_key(paragraph._p)
            if key not in seen:
                seen.add(key)
                yield paragraph

    # Second: add XML paragraphs not surfaced by python-docx (not replace them).
    try:
        for element in document.element.body.iter(qn("w:p")):
            key = _element_key(element)
            if key in seen:
                continue
            seen.add(key)
            yield Paragraph(element, document)
    except Exception:
        pass


def _normalize_with_raw_map(text):
    normalized, raw_map, in_space = [], [], False
    text = unicodedata.normalize("NFKC", text or "")
    dash_chars = {"\u2010", "\u2011", "\u2012", "\u2013", "\u2014", "\u2212"}
    quote_map = {"\u2018":"'", "\u2019":"'", "\u201c":'"', "\u201d":'"'}
    for raw_index, char in enumerate(text):
        if char in ("\u200b", "\ufeff"):
            continue
        if char in dash_chars:
            char = "-"
        elif char in quote_map:
            char = quote_map[char]
        if char.isspace() or char in ("\u00a0", "\u2007", "\u202f"):
            if normalized and not in_space:
                normalized.append(" ")
                raw_map.append(raw_index)
            in_space = True
            continue
        in_space = False
        for folded in char.casefold():
            normalized.append(folded)
            raw_map.append(raw_index)
    while normalized and normalized[0] == " ": normalized.pop(0); raw_map.pop(0)
    while normalized and normalized[-1] == " ": normalized.pop(); raw_map.pop()
    return "".join(normalized), raw_map


def _dominant_value(overlaps, key, unknown=None):
    counter = Counter()
    for entry, overlap in overlaps:
        value = entry.get(key)
        if value is None or value == "": continue
        counter[value] += overlap
    return counter.most_common(1)[0][0] if counter else unknown


def _ancestor(element, namespace, local_name):
    node = element
    tag = f"{{{namespace}}}{local_name}"
    while node is not None:
        try:
            if node.tag == tag: return node
            node = node.getparent()
        except Exception:
            break
    return None


def _paragraph_anchor_position(paragraph):
    try:
        anchor = _ancestor(paragraph._p, WP_NS, "anchor")
        if anchor is None: return None, None
        def get_offset(parent_name):
            parent = anchor.find(f".//{{{WP_NS}}}{parent_name}")
            if parent is None: return None
            child = parent.find(f"{{{WP_NS}}}posOffset")
            return child.text if child is not None else None
        x_raw, y_raw = get_offset("positionH"), get_offset("positionV")
        x_pt = float(x_raw)/EMU_PER_POINT if x_raw is not None else None
        y_pt = float(y_raw)/EMU_PER_POINT if y_raw is not None else None
        return x_pt, y_pt
    except Exception:
        return None, None


def _page_map(document):
    result, page = {}, 1
    try:
        body = document.element.body
        for element in body.iter():
            local = element.tag.split("}")[-1] if "}" in element.tag else element.tag
            if local == "p":
                result[_element_key(element)] = page
            elif local == "lastRenderedPageBreak":
                page += 1
            elif local == "br":
                try:
                    if element.get(qn("w:type")) == "page": page += 1
                except Exception: pass
    except Exception:
        pass
    return result


class DocxReferenceIndex:
    def __init__(self):
        self.path = None
        self.document = None
        self.entries = []
        self.paragraphs = []
        self.theme_map = {}
        self.alias_map = {}

    @property
    def loaded(self):
        return self.document is not None

    def clear(self):
        self.path = None; self.document = None; self.entries = []; self.paragraphs = []
        self.theme_map = {}; self.alias_map = {}

    def load(self, path):
        self.path = path
        self.document = Document(path)
        self.entries = []; self.paragraphs = []
        self.theme_map = _theme_font_map(self.document)
        self.alias_map = _font_alias_map(self.document)
        page_map = _page_map(self.document)

        for paragraph_index, paragraph in enumerate(_all_paragraphs(self.document)):
            raw_text, run_entries = "", []
            x_pt, y_pt = _paragraph_anchor_position(paragraph)
            page_number = page_map.get(_element_key(paragraph._p))

            for run_index, run in enumerate(paragraph.runs):
                run_text = run.text or ""
                if not run_text: continue
                start = len(raw_text); raw_text += run_text; end = len(raw_text)
                family, declared = _effective_font_name(
                    run, paragraph, self.document, self.theme_map, self.alias_map
                )
                entry = {
                    "paragraph_index": paragraph_index, "run_index": run_index,
                    "text": run_text, "norm": match_normalize(run_text),
                    "raw_start": start, "raw_end": end,
                    "family": family, "declared_family": declared,
                    "size_pt": _effective_size(run, paragraph, self.document),
                    "bold": _effective_bool(run, paragraph, self.document, "bold"),
                    "italic": _effective_bool(run, paragraph, self.document, "italic"),
                    "page_number": page_number, "x_pt": x_pt, "y_pt": y_pt,
                }
                self.entries.append(entry); run_entries.append(entry)

            if raw_text.strip():
                norm_text, norm_map = _normalize_with_raw_map(raw_text)
                self.paragraphs.append({
                    "paragraph_index": paragraph_index, "raw_text": raw_text,
                    "norm_text": norm_text, "norm_map": norm_map, "runs": run_entries,
                    "page_number": page_number, "x_pt": x_pt, "y_pt": y_pt,
                })
        return len(self.entries)

    def find(self, selected_text, pdf_size_pt=None, pdf_page_number=None, pdf_x_pt=None, pdf_y_pt=None):
        if not self.loaded: return None
        target = match_normalize(selected_text)
        if not target: return None

        exact_candidates = self._exact_candidates(target)
        if exact_candidates:
            for ref in exact_candidates:
                ref.score, ref.match_reason = self._score_reference(
                    ref, pdf_size_pt, pdf_page_number, pdf_x_pt, pdf_y_pt
                )
            exact_candidates = self._prefer_size_compatible(exact_candidates, pdf_size_pt)
            exact_candidates.sort(key=lambda r: r.score if r.score is not None else 1e9)
            best = exact_candidates[0]
            best.confidence = self._confidence_for(best, pdf_size_pt, pdf_page_number, pdf_x_pt, pdf_y_pt)
            return best

        # Robust fuzzy fallback for conversion punctuation/spacing differences.
        fuzzy = []
        for paragraph in self.paragraphs:
            pnorm = paragraph["norm_text"]
            if not pnorm: continue
            similarity = SequenceMatcher(None, target, pnorm).ratio()
            contains = target in pnorm or pnorm in target
            if similarity < 0.76 and not contains: continue
            overlaps = [(run, max(1, len(run["norm"]))) for run in paragraph["runs"] if run["norm"]]
            if not overlaps: continue
            ref = self._reference_from_overlaps(overlaps, paragraph["raw_text"], paragraph)
            ref.score, ref.match_reason = self._score_reference(
                ref, pdf_size_pt, pdf_page_number, pdf_x_pt, pdf_y_pt
            )
            ref.score += (1.0-similarity)*500.0
            ref.match_reason += f"; fuzzy text similarity {similarity:.2f}"
            ref.confidence = "Medium - fuzzy text + formatting"
            fuzzy.append(ref)
        if fuzzy:
            fuzzy = self._prefer_size_compatible(fuzzy, pdf_size_pt)
            fuzzy.sort(key=lambda r: r.score if r.score is not None else 1e9)
            return fuzzy[0]

        # Final identification fallback: dominant DOCX family at the same size.
        fallback = self._dominant_family_at_size(pdf_size_pt)
        if fallback:
            return fallback
        return None

    def _exact_candidates(self, target):
        result = []
        for paragraph in self.paragraphs:
            norm_text = paragraph["norm_text"]
            start_norm = norm_text.find(target)
            while start_norm != -1:
                end_norm = start_norm + len(target); raw_map = paragraph["norm_map"]
                if start_norm < len(raw_map) and end_norm-1 < len(raw_map):
                    raw_start = raw_map[start_norm]; raw_end = raw_map[end_norm-1]+1
                    overlaps = []
                    for run in paragraph["runs"]:
                        os_ = max(raw_start, run["raw_start"]); oe = min(raw_end, run["raw_end"])
                        overlap = max(0, oe-os_)
                        if overlap > 0:
                            seg = run["text"][os_-run["raw_start"]:oe-run["raw_start"]]
                            visible = sum(1 for c in seg if not c.isspace())
                            overlaps.append((run, max(visible, overlap)))
                    if overlaps:
                        result.append(self._reference_from_overlaps(
                            overlaps, paragraph["raw_text"][raw_start:raw_end], paragraph
                        ))
                start_norm = norm_text.find(target, start_norm+1)
        return result

    @staticmethod
    def _prefer_size_compatible(candidates, pdf_size_pt):
        if pdf_size_pt is None: return candidates
        close = [r for r in candidates if r.size_pt is not None and abs(r.size_pt-pdf_size_pt) <= 1.75]
        return close if close else candidates

    @staticmethod
    def _score_reference(ref, pdf_size_pt, pdf_page_number, pdf_x_pt, pdf_y_pt):
        score, reasons = 0.0, []
        if pdf_size_pt is not None and ref.size_pt is not None:
            d = abs(ref.size_pt-pdf_size_pt); score += d*60.0; reasons.append(f"size Δ {d:.2f}pt")
            if d > 4: score += 900; reasons.append("large size mismatch penalty")
        else:
            score += 120; reasons.append("size unavailable")
        if pdf_page_number is not None and ref.page_number is not None:
            if ref.page_number == pdf_page_number: score -= 100; reasons.append("same page")
            else: score += 650 + 100*abs(ref.page_number-pdf_page_number); reasons.append("page mismatch")
        if None not in (pdf_x_pt, pdf_y_pt, ref.x_pt, ref.y_pt):
            dx,dy = abs(ref.x_pt-pdf_x_pt), abs(ref.y_pt-pdf_y_pt)
            score += dx*.35 + dy*.8; reasons.append(f"position Δx {dx:.1f}/Δy {dy:.1f}pt")
            if dx<=72 and dy<=36: score -= 150; reasons.append("close position")
        return score, "; ".join(reasons)

    @staticmethod
    def _confidence_for(ref, pdf_size_pt, pdf_page_number, pdf_x_pt, pdf_y_pt):
        size_close = pdf_size_pt is not None and ref.size_pt is not None and abs(ref.size_pt-pdf_size_pt)<=1.25
        same_page = pdf_page_number is not None and ref.page_number is not None and ref.page_number==pdf_page_number
        pos_close = None not in (pdf_x_pt,pdf_y_pt,ref.x_pt,ref.y_pt) and abs(ref.x_pt-pdf_x_pt)<=72 and abs(ref.y_pt-pdf_y_pt)<=36
        if size_close and same_page and pos_close: return "Verified - text + size + page + position"
        if size_close and pos_close: return "High - exact text + size + position"
        if size_close and same_page: return "High - exact text + size + page"
        if size_close: return "High - exact text + close size"
        return "Medium - exact text, size/location not verified"

    def _dominant_family_at_size(self, pdf_size_pt):
        if pdf_size_pt is None: return None
        candidates = [e for e in self.entries if e.get("family") and e.get("size_pt") is not None and abs(e["size_pt"]-pdf_size_pt)<=0.75]
        if not candidates: return None
        counts = Counter()
        for e in candidates:
            counts[e["family"]] += max(1, len(match_normalize(e["text"])))
        family, weight = counts.most_common(1)[0]
        total = sum(counts.values())
        ratio = weight/total if total else 0
        if ratio < 0.65: return None
        matching = [e for e in candidates if e["family"] == family]
        sizes = Counter(round(e["size_pt"],4) for e in matching)
        size = sizes.most_common(1)[0][0] if sizes else pdf_size_pt
        bold = Counter(e["bold"] for e in matching if e["bold"] is not None).most_common(1)
        italic = Counter(e["italic"] for e in matching if e["italic"] is not None).most_common(1)
        declared = Counter(e["declared_family"] for e in matching if e.get("declared_family")).most_common(1)
        return DocxFontReference(
            matched_text="", family=family, size_pt=size,
            bold=bold[0][0] if bold else None, italic=italic[0][0] if italic else None,
            source="DOCX dominant-font fallback", confidence="Medium - dominant DOCX family at same size",
            match_reason=f"No exact text occurrence; {family} represents {ratio:.0%} of DOCX text near {pdf_size_pt:.2f}pt",
            declared_family=declared[0][0] if declared else None,
        )

    def _reference_from_overlaps(self, overlaps, matched_text, paragraph):
        return DocxFontReference(
            matched_text=matched_text,
            family=_dominant_value(overlaps,"family"),
            size_pt=_dominant_value(overlaps,"size_pt"),
            bold=_dominant_value(overlaps,"bold"),
            italic=_dominant_value(overlaps,"italic"),
            source="DOCX exact character-range reference",
            confidence="Exact text candidate",
            page_number=paragraph.get("page_number"), x_pt=paragraph.get("x_pt"), y_pt=paragraph.get("y_pt"),
            declared_family=_dominant_value(overlaps,"declared_family"),
        )
