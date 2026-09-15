import hashlib
import os
import re
import sys
import tempfile
from pathlib import Path
from statistics import mean
from typing import Optional

import pymupdf
from fontTools.ttLib import TTCollection, TTFont

from .font_utils import (
    BASE14_MAP,
    font_bytes_have_text,
    font_path_has_text,
    normalize_font_name,
    read_font_metadata_from_bytes,
    read_font_metadata_from_path,
    read_ttfont_metadata,
    safe_file_component,
    strip_subset_prefix,
    weight_from_flags,
    italic_from_flags,
)
from .models import FontCandidate, FontResolution, TextSpan, DocxFontReference
from .cff_subset_extender import inspect_existing_cff
from .online_font_resolver import OnlineFontResolver


def humanize_pdf_font_name(name: str) -> str:
    """
    Convert a PDF font name into a conservative exact-family lookup name.

    V3.9 keeps the V3.8 rule against metric-only substitution, but it no longer
    throws away a readable family prefix just because the PDF producer appended
    an internal encoding / subset suffix such as ``_CZEX0`` or ``_C2EX0A8U``.
    """
    value = strip_subset_prefix(name or "")

    # Bank / statement generators commonly append private encoding identifiers
    # to an otherwise meaningful family name. Strip only the known tail shapes;
    # do not guess an unrelated family from metrics.
    value = re.sub(
        r"_(?:CZEX|CZE_X|C2EX|C2E_X|CZ|C2)[A-Z0-9_]*$",
        "",
        value,
        flags=re.I,
    )

    value = value.replace("_", " ").strip()
    return value


def _font_candidate_from_path(path: str, source: str = "Installed system font"):
    meta = read_font_metadata_from_path(path)
    if not meta:
        return None

    return FontCandidate(
        path=path,
        family=meta.get("family"),
        subfamily=meta.get("subfamily"),
        full_name=meta.get("full_name"),
        postscript=meta.get("postscript"),
        weight=meta.get("weight"),
        italic=bool(meta.get("italic")),
        source=source,
    )


class SystemFontIndex:
    def __init__(self):
        self.records = []
        self.scanned = False
        self.temp_dir = Path(tempfile.mkdtemp(prefix="pdf_text_editor_fonts_"))

    def font_dirs(self):
        home = Path.home()

        if sys.platform == "darwin":
            candidates = [
                Path("/System/Library/Fonts"),
                Path("/System/Library/Fonts/Supplemental"),
                Path("/Library/Fonts"),
                home / "Library/Fonts",
            ]
        elif os.name == "nt":
            windir = Path(os.environ.get("WINDIR", r"C:\Windows"))
            localapp = Path(os.environ.get("LOCALAPPDATA", ""))
            candidates = [
                windir / "Fonts",
                localapp / "Microsoft" / "Windows" / "Fonts",
            ]
        else:
            candidates = [
                Path("/usr/share/fonts"),
                Path("/usr/local/share/fonts"),
                home / ".fonts",
                home / ".local/share/fonts",
            ]

        return [path for path in candidates if path and path.exists()]

    def scan(self, force=False):
        if self.scanned and not force:
            return

        self.records = []
        self.scanned = True
        seen = set()

        font_paths = []
        for directory in self.font_dirs():
            for pattern in ("*.ttf", "*.otf", "*.ttc", "*.otc"):
                try:
                    font_paths.extend(directory.rglob(pattern))
                except Exception:
                    pass

        for path in font_paths:
            key = str(path)
            if key in seen:
                continue
            seen.add(key)

            suffix = path.suffix.lower()

            if suffix in (".ttc", ".otc"):
                self._scan_collection(path)
            else:
                meta = read_font_metadata_from_path(str(path))
                if meta:
                    self.records.append({
                        "path": str(path),
                        "font_number": 0,
                        "collection": False,
                        "meta": meta,
                    })

    def _scan_collection(self, path: Path):
        try:
            collection = TTCollection(str(path), lazy=True)
        except Exception:
            return

        try:
            for index, font in enumerate(collection.fonts):
                try:
                    meta = read_ttfont_metadata(font)
                    self.records.append({
                        "path": str(path),
                        "font_number": index,
                        "collection": True,
                        "meta": meta,
                    })
                except Exception:
                    continue
        finally:
            try:
                collection.close()
            except Exception:
                pass

    def _standalone_collection_face(self, record):
        source = record["path"]
        font_number = record["font_number"]
        meta = record.get("meta") or {}

        basename = (
            meta.get("postscript")
            or meta.get("full_name")
            or meta.get("family")
            or Path(source).stem
        )
        out = self.temp_dir / (
            f"{safe_file_component(basename)}_{font_number}.ttf"
        )

        if out.exists():
            return str(out)

        try:
            font = TTFont(source, fontNumber=font_number)
            if "CFF " in font or "CFF2" in font:
                out = out.with_suffix(".otf")
            font.save(str(out))
            font.close()
            return str(out)
        except Exception:
            return None

    def candidate_from_record(self, record):
        path = record["path"]

        if record.get("collection"):
            path = self._standalone_collection_face(record)
            if not path:
                return None

        meta = record.get("meta") or {}

        return FontCandidate(
            path=path,
            font_number=0,
            family=meta.get("family"),
            subfamily=meta.get("subfamily"),
            full_name=meta.get("full_name"),
            postscript=meta.get("postscript"),
            weight=meta.get("weight"),
            italic=bool(meta.get("italic")),
            source="Installed system font",
        )

    def find_best(
        self,
        family: str,
        target_weight: int,
        target_italic: bool,
    ) -> Optional[FontCandidate]:
        if not family:
            return None

        self.scan()
        family_norm = normalize_font_name(family)
        candidates = []

        for record in self.records:
            meta = record.get("meta") or {}

            names = [
                meta.get("family"),
                meta.get("full_name"),
                meta.get("postscript"),
            ]

            exact_family = any(
                normalize_font_name(name) == family_norm
                for name in names
                if name
            )

            if not exact_family:
                family_name_norm = normalize_font_name(meta.get("family"))
                exact_family = family_name_norm == family_norm

            if not exact_family:
                continue

            weight = meta.get("weight")
            weight_delta = abs((weight or 400) - target_weight)

            italic = bool(meta.get("italic"))
            italic_penalty = 0 if italic == target_italic else 1000

            score = italic_penalty + weight_delta
            candidates.append((score, record))

        if not candidates:
            return None

        candidates.sort(key=lambda item: item[0])
        return self.candidate_from_record(candidates[0][1])

    def iter_candidates(self):
        self.scan()

        for record in self.records:
            candidate = self.candidate_from_record(record)
            if candidate is not None and candidate.path:
                yield candidate


class FontResolver:
    def __init__(self):
        self.system_fonts = SystemFontIndex()
        self.manual_fonts = {}
        self.manual_candidates = []
        self._fingerprint_cache = {}
        self.online_fonts = OnlineFontResolver()
        self.last_online_note = ""

    def add_manual_font(self, path: str):
        meta = read_font_metadata_from_path(path)

        candidate = FontCandidate(
            path=path,
            family=meta.get("family"),
            subfamily=meta.get("subfamily"),
            full_name=meta.get("full_name"),
            postscript=meta.get("postscript"),
            weight=meta.get("weight"),
            italic=bool(meta.get("italic")),
            source="Manually loaded font",
        )

        keys = {
            normalize_font_name(candidate.family),
            normalize_font_name(candidate.full_name),
            normalize_font_name(candidate.postscript),
        }

        for key in keys:
            if key:
                self.manual_fonts[key] = candidate

        self.manual_candidates.append(candidate)
        return candidate

    def find_page_fonts(self, page, span: TextSpan):
        """Return likely PDF font resource records, best match first."""
        target = normalize_font_name(span.font)
        matches = []

        try:
            items = page.get_fonts(full=True)
        except Exception:
            items = []

        for item in items:
            if len(item) < 5:
                continue

            record = {
                "xref": int(item[0]),
                "ext": str(item[1]),
                "type": str(item[2]),
                "basefont": str(item[3]),
                "refname": str(item[4]),
                "encoding": str(item[5]) if len(item) > 5 else "",
                "referencer": int(item[6]) if len(item) > 6 else 0,
            }

            current = normalize_font_name(record["basefont"])
            exact = bool(target and current == target)
            fuzzy = bool(
                target
                and current
                and (target in current or current in target)
            )

            if not exact and not fuzzy:
                continue

            # Prefer an exact name match, then a font directly referenced by the
            # page (safe to reuse by /refname), then a valid xref / extractable
            # extension. This also avoids stopping on the wrong duplicate font
            # record when a page contains Form XObjects.
            score = (
                0 if exact else 1,
                0 if record["referencer"] == 0 else 1,
                0 if record["xref"] > 0 else 1,
                0 if record["ext"].casefold() != "n/a" else 1,
            )
            matches.append((score, record))

        matches.sort(key=lambda item: item[0])
        return [record for _, record in matches]

    def find_page_font(self, page, span: TextSpan):
        records = self.find_page_fonts(page, span)
        return records[0] if records else None

    def extract_embedded(self, doc, page, span: TextSpan):
        # Try every plausible resource record. Some PDFs reference the same base
        # font through more than one resource / XObject and only one xref may
        # contain an extractable font program.
        for record in self.find_page_fonts(page, span):
            if record["xref"] <= 0:
                continue

            try:
                extracted = doc.extract_font(record["xref"])
            except Exception:
                continue

            if not extracted or len(extracted) < 4:
                continue

            basename, ext, font_type, content = extracted[:4]

            if not content:
                continue

            meta = read_font_metadata_from_bytes(content)

            candidate = FontCandidate(
                path=None,
                family=meta.get("family"),
                subfamily=meta.get("subfamily"),
                full_name=meta.get("full_name"),
                postscript=meta.get("postscript"),
                weight=meta.get("weight"),
                italic=bool(meta.get("italic")),
                source="Exact embedded PDF font",
                embedded_bytes=content,
            )

            # Attach non-dataclass helper attributes for resolver decisions.
            candidate._pdf_ext = ext
            candidate._pdf_type = font_type
            candidate._pdf_basename = basename
            return candidate

        return None

    @staticmethod
    def _attach_probe_font_xref(doc, page, font_xref: int, alias: str) -> bool:
        """Attach an existing font xref to a newly-created probe page."""
        if font_xref <= 0 or not alias:
            return False

        target = f"{int(font_xref)} 0 R"
        alias = alias.strip().lstrip("/")

        try:
            resources_type, resources_value = doc.xref_get_key(page.xref, "Resources")

            if resources_type == "xref":
                resources_xref = int(str(resources_value).split()[0])
                font_type, font_value = doc.xref_get_key(resources_xref, "Font")

                if font_type == "xref":
                    font_dict_xref = int(str(font_value).split()[0])
                    doc.xref_set_key(font_dict_xref, alias, target)
                elif font_type == "dict":
                    doc.xref_set_key(resources_xref, f"Font/{alias}", target)
                else:
                    doc.xref_set_key(
                        resources_xref,
                        "Font",
                        f"<< /{alias} {target} >>",
                    )
            elif resources_type == "dict":
                font_type, font_value = doc.xref_get_key(page.xref, "Resources/Font")
                if font_type == "xref":
                    font_dict_xref = int(str(font_value).split()[0])
                    doc.xref_set_key(font_dict_xref, alias, target)
                elif font_type == "dict":
                    doc.xref_set_key(page.xref, f"Resources/Font/{alias}", target)
                else:
                    doc.xref_set_key(
                        page.xref,
                        "Resources/Font",
                        f"<< /{alias} {target} >>",
                    )
            else:
                return False

            return any(
                len(item) >= 5 and str(item[4]).lstrip("/") == alias
                for item in page.get_fonts(full=True)
            )
        except Exception:
            return False

    @staticmethod
    def _pixmap_ink_signature(pix):
        """Return a stable signature for visible dark ink inside a pixmap."""
        try:
            samples = pix.samples
            n = int(pix.n)
            width = int(pix.width)
            height = int(pix.height)
        except Exception:
            return None

        if not samples or n < 3 or width <= 0 or height <= 0:
            return None

        xs = []
        ys = []
        dark = set()

        pixel_index = 0
        for offset in range(0, len(samples), n):
            rgb = samples[offset:offset + 3]
            if len(rgb) < 3:
                break
            if min(rgb) < 242:
                x = pixel_index % width
                y = pixel_index // width
                xs.append(x)
                ys.append(y)
                dark.add((x, y))
            pixel_index += 1

        # A period / comma at 64 pt still has many pixels. Anything below this
        # threshold is effectively blank / rendering noise.
        if len(dark) < 6:
            return None

        x0, x1 = min(xs), max(xs)
        y0, y1 = min(ys), max(ys)
        box_w = x1 - x0 + 1
        box_h = y1 - y0 + 1

        # Normalize away absolute placement. Identical .notdef boxes then get
        # the same signature even if their baseline positions differ slightly.
        payload = bytearray()
        for y in range(y0, y1 + 1):
            for x in range(x0, x1 + 1):
                payload.append(1 if (x, y) in dark else 0)

        digest = hashlib.sha1(bytes(payload)).hexdigest()
        return (box_w, box_h, len(dark), digest)

    def _matching_pdf_font_records(self, doc, current_page, span: TextSpan):
        """Find same-family font resources across the whole PDF, best first."""
        target_raw = normalize_font_name(span.font)
        target_family = normalize_font_name(humanize_pdf_font_name(span.font))
        current_number = int(current_page.number)
        best_by_xref = {}

        for page_number in range(len(doc)):
            try:
                items = doc[page_number].get_fonts(full=True)
            except Exception:
                continue

            for item in items:
                if len(item) < 5:
                    continue

                xref = int(item[0] or 0)
                if xref <= 0:
                    continue

                basefont = str(item[3] or "")
                base_raw = normalize_font_name(basefont)
                base_family = normalize_font_name(humanize_pdf_font_name(basefont))

                exact_raw = bool(target_raw and base_raw == target_raw)
                exact_family = bool(
                    target_family and base_family and target_family == base_family
                )
                fuzzy_raw = bool(
                    target_raw
                    and base_raw
                    and (target_raw in base_raw or base_raw in target_raw)
                )

                if not (exact_raw or exact_family or fuzzy_raw):
                    continue

                record = {
                    "xref": xref,
                    "ext": str(item[1]),
                    "type": str(item[2]),
                    "basefont": basefont,
                    "refname": str(item[4]),
                    "encoding": str(item[5]) if len(item) > 5 else "",
                    "referencer": int(item[6]) if len(item) > 6 else 0,
                    "page_number": page_number,
                }

                score = (
                    0 if page_number == current_number else 1,
                    0 if exact_raw else 1,
                    0 if exact_family else 1,
                    0 if record["referencer"] == 0 else 1,
                    page_number,
                )

                old = best_by_xref.get(xref)
                if old is None or score < old[0]:
                    best_by_xref[xref] = (score, record)

        result = sorted(best_by_xref.values(), key=lambda value: value[0])
        return [record for _, record in result]

    def _probe_pdf_font_xref(self, doc, xref: int, replacement_text: str):
        """Verify Unicode mapping *and visible glyph outlines* for one PDF font.

        V3.10 only proved that inserted Unicode could be extracted again. Some
        subset/custom fonts can pass that text-layer test while rendering blank
        glyphs. V3.12 creates a blank temporary page, attaches the exact font xref,
        writes each required character separately, and rasterizes every glyph.
        Blank glyphs and a repeated .notdef placeholder are rejected.
        """
        visible_chars = []
        seen = set()
        for char in replacement_text:
            if char.isspace() or char in "\r\n\t":
                continue
            if char in seen:
                continue
            seen.add(char)
            visible_chars.append(char)

        if not visible_chars:
            return True, [], "No visible replacement glyphs are required."

        try:
            raw = doc.tobytes(garbage=0, deflate=False)
            probe_doc = pymupdf.open(stream=raw, filetype="pdf")
        except Exception as exc:
            return False, visible_chars, f"Could not create glyph probe: {exc}"

        try:
            row_h = 105.0
            width = 260.0
            # Row 0 is a deliberately unassigned Unicode sentinel. If missing
            # glyphs render a .notdef box, they normally match this signature.
            sentinel = "\u0378"
            rows = [sentinel] + visible_chars
            probe_page = probe_doc.new_page(
                width=width,
                height=max(180.0, 20.0 + row_h * len(rows)),
            )
            alias = f"V311Probe{xref}"
            if not self._attach_probe_font_xref(probe_doc, probe_page, xref, alias):
                return False, visible_chars, "Could not attach PDF font to probe page."

            insert_failed = set()
            baselines = []
            for index, char in enumerate(rows):
                baseline = 78.0 + index * row_h
                baselines.append(baseline)
                try:
                    probe_page.insert_text(
                        pymupdf.Point(28.0, baseline),
                        char,
                        fontsize=64.0,
                        fontname=f"/{alias}",
                        color=(0, 0, 0),
                        overlay=True,
                    )
                except Exception:
                    insert_failed.add(index)

            # Check what the PDF text layer reports for each row.
            recovered = {}
            try:
                data = probe_page.get_text("dict")
                for block in data.get("blocks", []):
                    if block.get("type", 0) != 0:
                        continue
                    for line in block.get("lines", []):
                        for item in line.get("spans", []):
                            origin = item.get("origin", (0, -9999))
                            y = float(origin[1])
                            nearest = min(
                                range(len(baselines)),
                                key=lambda i: abs(baselines[i] - y),
                            )
                            if abs(baselines[nearest] - y) <= 2.0:
                                recovered[nearest] = item.get("text", "")
            except Exception:
                pass

            signatures = {}
            matrix = pymupdf.Matrix(2.5, 2.5)
            for index in range(len(rows)):
                top = index * row_h + 4.0
                bottom = top + row_h - 8.0
                try:
                    pix = probe_page.get_pixmap(
                        matrix=matrix,
                        clip=pymupdf.Rect(8.0, top, width - 8.0, bottom),
                        alpha=False,
                    )
                    signatures[index] = self._pixmap_ink_signature(pix)
                except Exception:
                    signatures[index] = None

            sentinel_signature = signatures.get(0)
            missing = []

            for char_index, char in enumerate(visible_chars, start=1):
                if char_index in insert_failed:
                    missing.append(char)
                    continue

                recovered_char = recovered.get(char_index, "")
                if recovered_char.replace("\u00a0", " ") != char.replace("\u00a0", " "):
                    missing.append(char)
                    continue

                signature = signatures.get(char_index)
                if signature is None:
                    missing.append(char)
                    continue

                # A missing glyph may render as the font's .notdef box instead
                # of blank. Reject an exact match with our unassigned sentinel.
                if sentinel_signature is not None and signature == sentinel_signature:
                    missing.append(char)
                    continue

            # Also test the complete replacement as one string. This catches
            # fonts whose per-character map works but whose PDF encoding cannot
            # safely serialize the requested sequence.
            if not missing:
                try:
                    line_page = probe_doc.new_page(width=5000, height=180)
                    line_alias = f"V311Line{xref}"
                    if not self._attach_probe_font_xref(
                        probe_doc, line_page, xref, line_alias
                    ):
                        return False, visible_chars, "Could not attach font for line probe."
                    line_page.insert_text(
                        pymupdf.Point(25, 90),
                        replacement_text,
                        fontsize=18,
                        fontname=f"/{line_alias}",
                        color=(0, 0, 0),
                        overlay=True,
                    )
                    extracted = line_page.get_text("text").replace("\r\n", "\n").rstrip("\n")
                    expected = replacement_text.replace("\r\n", "\n").rstrip("\n")
                    if extracted.replace("\u00a0", " ") != expected.replace("\u00a0", " "):
                        missing = list(visible_chars)
                except Exception:
                    missing = list(visible_chars)

            if missing:
                # Preserve order and uniqueness for a useful UI message.
                unique_missing = []
                seen_missing = set()
                for ch in missing:
                    if ch not in seen_missing:
                        seen_missing.add(ch)
                        unique_missing.append(ch)
                return False, unique_missing, (
                    "PDF subset failed visible-glyph verification."
                )

            return True, [], (
                "Unicode mapping and per-character raster glyph verification passed."
            )
        finally:
            probe_doc.close()

    def _existing_pdf_resource_candidate(
        self,
        doc,
        page,
        span: TextSpan,
        replacement_text: str,
    ):
        """Find an exact usable PDF font resource, scanning the whole document.

        Unlike V3.10, a resource is never accepted only because text extraction
        round-trips. Every non-space character must visibly rasterize. The scan
        also considers matching resources on other pages because another subset
        of the same family may contain the required glyphs.
        """
        if not replacement_text:
            return None, "Replacement text is empty.", []

        if "\n" in replacement_text or "\r" in replacement_text:
            return None, "Existing-resource probe is limited to single-line text.", []

        best_missing = []
        best_missing_count = 10 ** 9
        tried = 0

        for record in self._matching_pdf_font_records(doc, page, span):
            xref = int(record.get("xref", 0) or 0)
            if xref <= 0:
                continue
            tried += 1

            glyphs_ok, missing, probe_note = self._probe_pdf_font_xref(
                doc,
                xref,
                replacement_text,
            )

            if not glyphs_ok:
                if missing and len(missing) < best_missing_count:
                    best_missing = list(missing)
                    best_missing_count = len(missing)
                continue

            try:
                max_code = max(ord(ch) for ch in replacement_text)
                if max_code > 65535:
                    continue
                widths = doc.get_char_widths(
                    xref,
                    limit=max(256, max_code + 1),
                )
            except Exception:
                continue

            if not widths:
                continue

            family = humanize_pdf_font_name(record.get("basefont", ""))
            alias = f"V311F{xref}"
            candidate = FontCandidate(
                path=None,
                family=family or humanize_pdf_font_name(span.font),
                full_name=strip_subset_prefix(record.get("basefont", "")) or strip_subset_prefix(span.font),
                postscript=strip_subset_prefix(record.get("basefont", "")) or strip_subset_prefix(span.font),
                weight=weight_from_flags(span.flags),
                italic=italic_from_flags(span.flags),
                source="Verified PDF font resource",
                pdf_refname=alias,
                pdf_xref=xref,
                pdf_char_widths=widths,
            )

            origin_note = (
                "this page"
                if int(record.get("page_number", -1)) == int(page.number)
                else f"page {int(record.get('page_number', 0)) + 1}"
            )
            return candidate, (
                f"Reused exact font xref {xref} found on {origin_note}; "
                f"{probe_note}"
            ), []

        if tried:
            note = (
                f"Checked {tried} matching PDF font resource(s) across the document; "
                "none could visibly render every required character."
            )
        else:
            note = "No matching PDF font resource was found anywhere in the document."

        return None, note, best_missing



    def _original_cff_candidate_if_encodable(self, doc, page, span: TextSpan, replacement_text: str):
        """Return the original Type1C /F# resource when its CFF already has every glyph.

        V3.11 sometimes re-embedded the same CFF bytes as a new Type0 font even
        though the original simple-font resource could represent the replacement.
        V3.12 writes the original one-byte codes directly instead.
        """
        record = self.find_page_font(page, span)
        if not record:
            return None
        if str(record.get("type", "")).casefold() != "type1":
            return None
        if str(record.get("ext", "")).casefold() not in {"cff", "cid", "type1c"}:
            return None
        xref = int(record.get("xref", 0) or 0)
        refname = str(record.get("refname", "") or "").lstrip("/")
        if xref <= 0 or not refname:
            return None

        can_encode, missing, widths = inspect_existing_cff(doc, xref, replacement_text)
        if not can_encode:
            return None

        return FontCandidate(
            path=None,
            family=humanize_pdf_font_name(record.get("basefont", "")) or humanize_pdf_font_name(span.font),
            full_name=strip_subset_prefix(record.get("basefont", "")) or strip_subset_prefix(span.font),
            postscript=strip_subset_prefix(record.get("basefont", "")) or strip_subset_prefix(span.font),
            weight=weight_from_flags(span.flags),
            italic=italic_from_flags(span.flags),
            source="Original PDF Type1C resource",
            pdf_refname=refname,
            pdf_xref=xref,
            pdf_unicode_widths=widths,
            extend_original_cff=True,
        )

    def _cff_subset_extension_candidate(self, page, span: TextSpan, full_candidate):
        """Wrap a verified full font so V3.12 extends the original Type1C resource.

        This keeps the page's existing /F# resource name, Type1 font dictionary,
        BaseFont name and CFF embedding model. Only missing glyphs / byte codes are
        appended to that original subset. If the selected page font is not an
        embedded Type1C simple font, return the full candidate unchanged.
        """
        if not full_candidate:
            return full_candidate

        # Automatically downloaded open fonts are used as normal verified font
        # resources. Never splice their glyphs into an existing PDF subset.
        # This keeps online lookup useful for ordinary/synthetic PDFs without
        # pretending a same-named public font is the original embedded program.
        if getattr(full_candidate, "_online_download", False):
            return full_candidate

        record = self.find_page_font(page, span)
        if not record:
            return full_candidate

        if str(record.get("type", "")).casefold() != "type1":
            return full_candidate
        if str(record.get("ext", "")).casefold() not in {"cff", "cid", "type1c"}:
            return full_candidate
        if int(record.get("xref", 0) or 0) <= 0 or not record.get("refname"):
            return full_candidate

        # The extender currently consumes a real full font file or buffer.
        if not getattr(full_candidate, "path", None) and not getattr(full_candidate, "embedded_bytes", None):
            return full_candidate

        return FontCandidate(
            path=getattr(full_candidate, "path", None),
            font_number=getattr(full_candidate, "font_number", 0),
            family=getattr(full_candidate, "family", None),
            subfamily=getattr(full_candidate, "subfamily", None),
            full_name=getattr(full_candidate, "full_name", None),
            postscript=getattr(full_candidate, "postscript", None),
            weight=getattr(full_candidate, "weight", None),
            italic=bool(getattr(full_candidate, "italic", False)),
            source="Original PDF Type1C subset extension",
            pdf_refname=str(record.get("refname", "")).lstrip("/"),
            pdf_xref=int(record.get("xref", 0) or 0),
            extend_original_cff=True,
            subset_extension_font_path=getattr(full_candidate, "path", None),
            subset_extension_font_bytes=getattr(full_candidate, "embedded_bytes", None),
        )

    @staticmethod
    def _fonttools_path_has_text(path: str, text: str):
        """
        Verify replacement glyph coverage directly from the font's Unicode cmap.

        This is used when a manually loaded full TTF/OTF will be used to extend
        the original PDF Type1C/CFF subset. In that path, fontTools is the
        authoritative source because the CFF subset extender consumes the full
        font through TTFont rather than through PyMuPDF's has_glyph() check.
        """
        if text == "":
            return True, []

        font = None
        try:
            font = TTFont(path, lazy=True)

            codepoints = set()

            # Prefer every Unicode cmap subtable instead of relying only on
            # getBestCmap(). Some valid fonts expose useful mappings in a Unicode
            # subtable that getBestCmap() does not choose.
            if "cmap" in font:
                for table in font["cmap"].tables:
                    try:
                        if table.isUnicode():
                            codepoints.update((table.cmap or {}).keys())
                    except Exception:
                        continue

            # Fall back to getBestCmap() when needed.
            if not codepoints:
                best = font.getBestCmap() or {}
                codepoints.update(best.keys())

            missing = []
            seen = set()

            for char in text:
                if char.isspace() or char in "\r\n\t":
                    continue

                cp = ord(char)
                if cp in seen:
                    continue
                seen.add(cp)

                if cp not in codepoints:
                    missing.append(char)

            return not missing, missing

        except Exception:
            # Do not falsely approve an unreadable font.
            missing = []
            seen = set()
            for char in text:
                if char.isspace() or char in "\r\n\t" or char in seen:
                    continue
                seen.add(char)
                missing.append(char)
            return False, missing

        finally:
            if font is not None:
                try:
                    font.close()
                except Exception:
                    pass

    def _candidate_text_check(self, page, span: TextSpan, candidate, replacement_text: str):
        """
        Prepare a candidate for its real write path and verify glyph coverage
        using the same font technology that path will use.

        Returns:
            (resolved_candidate, glyphs_ok, missing_chars)
        """
        resolved = self._cff_subset_extension_candidate(page, span, candidate)

        if getattr(resolved, "extend_original_cff", False) and getattr(candidate, "path", None):
            glyphs_ok, missing = self._fonttools_path_has_text(
                candidate.path,
                replacement_text,
            )
        elif getattr(candidate, "path", None):
            glyphs_ok, missing = font_path_has_text(
                candidate.path,
                replacement_text,
            )
        else:
            glyphs_ok = False
            missing = [
                ch for ch in dict.fromkeys(replacement_text)
                if not ch.isspace() and ch not in "\r\n\t"
            ]

        return resolved, glyphs_ok, missing

    def _online_match(self, family, weight, italic, replacement_text):
        """
        Search approved catalogs and the broad web index, download/cache an exact family
        face, then convert it into the same FontCandidate used by the rest of the
        editor. Provider failure is non-fatal; the resolver simply continues.
        """
        result, note = self.online_fonts.find_exact(
            family=family,
            weight=weight,
            italic=italic,
            required_text=replacement_text,
        )
        self.last_online_note = note or ""

        if not result:
            return None, self.last_online_note

        candidate = _font_candidate_from_path(
            result.path,
            source=f"Auto-downloaded verified font ({result.provider})",
        )
        if not candidate:
            return None, (
                self.last_online_note
                + " Downloaded font metadata could not be converted to a candidate."
            ).strip()

        # Non-dataclass helper attributes are intentional and already used by
        # this resolver for PDF-specific candidate decisions.
        candidate._online_download = True
        candidate._online_provider = result.provider
        candidate._online_font_id = result.font_id
        candidate._online_subset = result.subset
        candidate._online_source_url = result.source_url
        candidate._online_sha256 = result.sha256

        return candidate, self.last_online_note

    def _manual_match(self, family, weight, italic):
        family_norm = normalize_font_name(family)

        if not family_norm:
            return None

        seen = set()
        candidates = []

        for candidate in self.manual_fonts.values():
            if id(candidate) in seen:
                continue
            seen.add(id(candidate))

            names = [
                candidate.family,
                candidate.full_name,
                candidate.postscript,
            ]

            if not any(
                normalize_font_name(name) == family_norm
                for name in names
                if name
            ):
                continue

            weight_delta = abs((candidate.weight or 400) - weight)
            italic_penalty = 0 if candidate.italic == italic else 1000
            candidates.append((weight_delta + italic_penalty, candidate))

        if not candidates:
            return None

        candidates.sort(key=lambda item: item[0])
        return candidates[0][1]

    @staticmethod
    def _is_obfuscated_pdf_font(span: TextSpan, embedded: Optional[FontCandidate]):
        raw = strip_subset_prefix(span.font).casefold()

        if "connections" in raw or "higherstandards" in raw:
            return True

        if embedded is None:
            return True

        meaningful = any([
            embedded.family,
            embedded.full_name,
            embedded.postscript,
        ])

        # Raw Type1/CFF subset fonts commonly have no useful OpenType name table.
        if not meaningful:
            return True

        return False

    @staticmethod
    def _collect_pdf_sample(page, span: TextSpan):
        target = normalize_font_name(span.font)
        texts = []

        try:
            data = page.get_text("dict")
        except Exception:
            data = {}

        for block in data.get("blocks", []):
            if block.get("type", 0) != 0:
                continue

            for line in block.get("lines", []):
                for item in line.get("spans", []):
                    if normalize_font_name(item.get("font", "")) != target:
                        continue
                    value = item.get("text", "")
                    if value:
                        texts.append(value)

        # Selected text first, then other same-font text on the page.
        full = (span.text or "") + " " + " ".join(texts)

        # Unique useful characters. Prefer letters/numbers because punctuation
        # often has similar metrics across unrelated fonts.
        ordered = []
        seen = set()

        for ch in full:
            if ch.isspace() or ch in seen:
                continue
            if ch.isalnum():
                seen.add(ch)
                ordered.append(ch)

        for ch in full:
            if ch.isspace() or ch in seen:
                continue
            seen.add(ch)
            ordered.append(ch)

        return "".join(ordered[:48])

    @staticmethod
    def _fingerprint_metrics(font, sample_chars: str):
        advances = {}

        for ch in sample_chars:
            cp = ord(ch)

            try:
                if not font.has_glyph(cp, fallback=False):
                    continue
            except TypeError:
                if not font.has_glyph(cp):
                    continue
            except Exception:
                continue

            try:
                advances[ch] = float(font.glyph_advance(cp))
            except Exception:
                continue

        return advances

    @staticmethod
    def _sample_words(page, span: TextSpan):
        target = normalize_font_name(span.font)
        words = []

        try:
            data = page.get_text("dict")
        except Exception:
            data = {}

        for block in data.get("blocks", []):
            if block.get("type", 0) != 0:
                continue

            for line in block.get("lines", []):
                for item in line.get("spans", []):
                    if normalize_font_name(item.get("font", "")) != target:
                        continue

                    for word in re.findall(r"[A-Za-z0-9][A-Za-z0-9.,:#/$%&'’-]*", item.get("text", "")):
                        if 2 <= len(word) <= 24:
                            words.append(word)

        unique = []
        seen = set()

        for word in words:
            key = word.casefold()
            if key in seen:
                continue
            seen.add(key)
            unique.append(word)
            if len(unique) >= 12:
                break

        return unique

    def _score_candidate_fingerprint(
        self,
        embedded_font,
        candidate: FontCandidate,
        sample_chars,
        sample_words,
        target_weight,
        target_italic,
        replacement_text,
    ):
        if not candidate.path:
            return None

        try:
            candidate_font = pymupdf.Font(fontfile=candidate.path)
        except Exception:
            return None

        # The full candidate must contain the replacement characters.
        missing = []
        for ch in replacement_text:
            if ch.isspace():
                continue
            cp = ord(ch)
            try:
                has = candidate_font.has_glyph(cp, fallback=False)
            except TypeError:
                has = candidate_font.has_glyph(cp)
            except Exception:
                has = 0

            if not has and ch not in missing:
                missing.append(ch)

        if missing:
            return None

        embedded_adv = self._fingerprint_metrics(
            embedded_font,
            sample_chars,
        )
        candidate_adv = self._fingerprint_metrics(
            candidate_font,
            sample_chars,
        )

        common = [
            ch for ch in sample_chars
            if ch in embedded_adv and ch in candidate_adv
        ]

        if len(common) < 6:
            return None

        char_diffs = [
            abs(embedded_adv[ch] - candidate_adv[ch])
            for ch in common
        ]
        char_score = mean(char_diffs)

        word_diffs = []

        for word in sample_words:
            try:
                emb_len = float(embedded_font.text_length(word, fontsize=1.0))
                can_len = float(candidate_font.text_length(word, fontsize=1.0))
            except Exception:
                continue

            if emb_len > 0:
                word_diffs.append(abs(emb_len - can_len) / emb_len)

        word_score = mean(word_diffs) if word_diffs else char_score

        weight_penalty = min(
            abs((candidate.weight or 400) - target_weight) / 1000.0,
            0.30,
        )
        italic_penalty = 0.0 if candidate.italic == target_italic else 0.35

        total = (
            char_score
            + (word_score * 0.55)
            + weight_penalty
            + italic_penalty
        )

        return {
            "candidate": candidate,
            "score": total,
            "char_score": char_score,
            "word_score": word_score,
            "common_count": len(common),
        }

    @staticmethod
    def _embedded_roundtrip_safe(embedded_bytes: bytes, text: str):
        """
        Prove that the exact extracted PDF font program can safely accept and
        reproduce the requested Unicode text.

        This is much stricter than checking glyph widths. It creates a temporary
        PDF, embeds the same font program, writes the requested text, then reads
        the text back. If the round-trip is not exact, the font is rejected for
        editing.

        This prevents custom/subset fonts with unusual encodings from silently
        producing the wrong characters.
        """
        if embedded_bytes is None:
            return False, "Embedded font bytes are unavailable."

        # Do not trust has_glyph() alone for custom PDF subset encodings.
        # Some Type1/CFF PDF fonts expose unusual character maps. The actual
        # write/read round-trip below is the authoritative safety test.

        # Empty replacement is always safe: it means delete only.
        if text == "":
            return True, "Empty replacement requires no new glyphs."

        try:
            test_doc = pymupdf.open()
            test_page = test_doc.new_page(width=1200, height=300)

            test_font_name = "ExactEmbeddedSafetyTest"
            test_page.insert_font(
                fontname=test_font_name,
                fontbuffer=embedded_bytes,
                set_simple=False,
            )

            test_page.insert_text(
                pymupdf.Point(36, 120),
                text,
                fontsize=12,
                fontname=test_font_name,
                color=(0, 0, 0),
            )

            # Re-open serialized bytes so extraction uses the actual written PDF
            # structures rather than only the in-memory display list.
            try:
                raw = test_doc.tobytes(
                    garbage=2,
                    deflate=True,
                )
            finally:
                test_doc.close()

            check_doc = pymupdf.open(
                stream=raw,
                filetype="pdf",
            )
            try:
                extracted = check_doc[0].get_text("text")
            finally:
                check_doc.close()

            # Normal PDF extraction usually adds a trailing newline.
            extracted = extracted.replace("\r\n", "\n").rstrip("\n")
            expected = text.replace("\r\n", "\n").rstrip("\n")

            # PDF subset fonts often encode a normal typed space as NBSP while
            # rendering it identically. Treat those two whitespace characters
            # as equivalent, but never ignore missing/null glyphs.
            extracted_compare = extracted.replace("\u00a0", " ")
            expected_compare = expected.replace("\u00a0", " ")

            if extracted_compare == expected_compare:
                return True, (
                    "Exact embedded PDF font passed Unicode write/read safety test."
                )

            return False, (
                "The exact embedded PDF font did not reproduce the requested text "
                "correctly in the safety test. "
                f"Expected {expected!r}, extracted {extracted!r}."
            )

        except Exception as exc:
            return False, (
                "The exact embedded PDF font could not pass the write/read safety "
                f"test: {exc}"
            )

    def _fingerprint_match(
        self,
        page,
        span,
        embedded,
        replacement_text,
        target_weight,
        target_italic,
    ):
        """
        V3.8 safety rule:
        Never auto-select an unrelated installed system font from metric
        similarity alone.

        Fingerprinting is now used only to VERIFY a font that the user manually
        selected with "Load Exact TTF/OTF Font...".
        """
        if not embedded or not embedded.embedded_bytes:
            return None, None

        if not self.manual_candidates:
            return None, (
                "Automatic system-font fingerprint substitution is disabled in V3.8 "
                "because metric similarity alone cannot prove the exact font family."
            )

        try:
            embedded_font = pymupdf.Font(
                fontbuffer=embedded.embedded_bytes
            )
        except Exception:
            return None, (
                "The embedded PDF font could not be opened for manual-font verification."
            )

        sample_chars = self._collect_pdf_sample(page, span)
        sample_words = self._sample_words(page, span)

        if len(sample_chars) < 6:
            return None, (
                "Not enough distinct embedded glyphs are available to verify the "
                "manually selected font."
            )

        ranked = []

        for candidate in self.manual_candidates:
            result = self._score_candidate_fingerprint(
                embedded_font,
                candidate,
                sample_chars,
                sample_words,
                target_weight,
                target_italic,
                replacement_text,
            )
            if result:
                ranked.append(result)

        if not ranked:
            return None, (
                "The manually selected font could not be compared reliably with "
                "the embedded PDF font."
            )

        ranked.sort(key=lambda item: item["score"])
        best = ranked[0]

        # Much stricter than V3.7. This is only a verification path, not an
        # automatic installed-font discovery path.
        if (
            best["char_score"] <= 0.010
            and best["word_score"] <= 0.015
            and best["score"] <= 0.035
        ):
            candidate = best["candidate"]
            return candidate, (
                f"Manually selected font verified against the embedded PDF font: "
                f"{candidate.display_name}. Glyph score "
                f"{best['char_score']:.4f}, word score "
                f"{best['word_score']:.4f}, {best['common_count']} glyphs compared."
            )

        return None, (
            "The manually selected font does not match the embedded PDF font "
            f"strictly enough (best score {best['score']:.4f})."
        )

    def resolve(
        self,
        doc,
        page,
        span: TextSpan,
        replacement_text: str,
        docx_ref: Optional[DocxFontReference],
    ) -> FontResolution:
        pdf_name = strip_subset_prefix(span.font)
        pdf_norm = normalize_font_name(pdf_name)
        target_weight = weight_from_flags(span.flags)
        target_italic = italic_from_flags(span.flags)

        base14 = BASE14_MAP.get(pdf_norm)

        if base14:
            candidate = FontCandidate(
                path=None,
                family=pdf_name,
                full_name=pdf_name,
                postscript=pdf_name,
                weight=target_weight,
                italic=target_italic,
                source="Exact PDF Base-14 font",
                base14_name=base14,
            )

            return FontResolution(
                ok=True,
                candidate=candidate,
                exact_family=True,
                exact_weight=True,
                exact_style=True,
                glyphs_ok=True,
                source_note="PDF-only/Base-14 exact font.",
            )

        embedded = self.extract_embedded(
            doc,
            page,
            span,
        )

        # ==============================================================
        # MODE 1: DOCX REFERENCE MODE
        # If a DOCX match exists, this is intentionally the first priority.
        # ==============================================================
        if docx_ref and docx_ref.family:
            target_weight = (
                700 if docx_ref.bold is True
                else 400 if docx_ref.bold is False
                else weight_from_flags(span.flags)
            )
            target_italic = (
                bool(docx_ref.italic)
                if docx_ref.italic is not None
                else italic_from_flags(span.flags)
            )

            manual = self._manual_match(
                docx_ref.family,
                target_weight,
                target_italic,
            )

            if manual and manual.path:
                resolved_manual, glyphs_ok, missing = self._candidate_text_check(
                    page,
                    span,
                    manual,
                    replacement_text,
                )

                if glyphs_ok:
                    return FontResolution(
                        ok=True,
                        candidate=resolved_manual,
                        exact_family=True,
                        exact_weight=abs((manual.weight or 400) - target_weight) <= 100,
                        exact_style=manual.italic == target_italic,
                        glyphs_ok=True,
                        source_note=(
                            "DOCX Reference Mode: matched the DOCX family to a "
                            "manually loaded exact font file. "
                            + (
                                "Glyph coverage was verified from the full font cmap "
                                "for Type1C/CFF subset extension."
                                if getattr(resolved_manual, "extend_original_cff", False)
                                else ""
                            )
                        ),
                    )

            system = self.system_fonts.find_best(
                docx_ref.family,
                target_weight,
                target_italic,
            )

            if system and system.path:
                glyphs_ok, missing = font_path_has_text(
                    system.path,
                    replacement_text,
                )

                weight_ok = abs((system.weight or 400) - target_weight) <= 100
                italic_ok = system.italic == target_italic

                if glyphs_ok and weight_ok and italic_ok:
                    resolved_system = self._cff_subset_extension_candidate(page, span, system)
                    return FontResolution(
                        ok=True,
                        candidate=resolved_system,
                        exact_family=True,
                        exact_weight=True,
                        exact_style=True,
                        glyphs_ok=True,
                        source_note=(
                            "DOCX Reference Mode: DOCX family matched to the "
                            "installed font family, weight and style."
                        ),
                    )

            online, online_note = self._online_match(
                docx_ref.family,
                target_weight,
                target_italic,
                replacement_text,
            )

            if online and online.path:
                glyphs_ok, missing = font_path_has_text(
                    online.path,
                    replacement_text,
                )
                weight_ok = abs((online.weight or 400) - target_weight) <= 100
                italic_ok = online.italic == target_italic

                if glyphs_ok and weight_ok and italic_ok:
                    return FontResolution(
                        ok=True,
                        candidate=online,
                        exact_family=True,
                        exact_weight=True,
                        exact_style=True,
                        glyphs_ok=True,
                        source_note=(
                            "DOCX Reference Mode: exact open font family was not "
                            "available locally, so it was automatically downloaded, "
                            "verified and cached. "
                            + (online_note or "")
                        ),
                    )

            # DOCX identified the family, but local and approved online exact-face
            # lookup both failed.
            return FontResolution(
                ok=False,
                reason=(
                    f"DOCX Reference Mode identified '{docx_ref.family}', but the "
                    "matching full font face could not be verified locally or from "
                    "the approved open-font provider. "
                    + (online_note or "")
                    + " Install it or use 'Load Exact TTF/OTF Font...'."
                ),
            )

        # ==============================================================
        # MODE 2: PDF-ONLY EXACT FONT MODE
        # No DOCX match is available.
        # ==============================================================

        # First try the original simple Type1C resource directly. This avoids
        # re-embedding the same CFF program as a new Type0/CID font when the
        # original subset already contains every required glyph.
        direct_cff = self._original_cff_candidate_if_encodable(
            doc, page, span, replacement_text
        )
        if direct_cff:
            return FontResolution(
                ok=True,
                candidate=direct_cff,
                exact_family=True,
                exact_weight=True,
                exact_style=True,
                glyphs_ok=True,
                source_note=(
                    "PDF-Only Exact Font Mode: using the original Type1C/CFF "
                    "page font resource directly. No new font resource will be "
                    "added."
                ),
            )

        # V3.12 scans matching resources across the whole PDF and accepts one
        # only after both Unicode and visible per-glyph raster verification.
        resource_candidate, resource_note, resource_missing = self._existing_pdf_resource_candidate(
            doc,
            page,
            span,
            replacement_text,
        )

        if resource_candidate:
            return FontResolution(
                ok=True,
                candidate=resource_candidate,
                exact_family=True,
                exact_weight=True,
                exact_style=True,
                glyphs_ok=True,
                source_note=(
                    "PDF-Only Exact Font Mode: using an exact PDF font resource "
                    "that passed visible-glyph verification. " + resource_note
                ),
            )

        embedded_note = ""
        embedded_safe = False
        embedded_missing = []

        # If a font program is embedded, require real cmap/glyph coverage before
        # the older Unicode round-trip safety test. This prevents an incomplete
        # subset from being accepted merely because text extraction succeeds.
        if embedded and embedded.embedded_bytes:
            glyphs_present, embedded_missing = font_bytes_have_text(
                embedded.embedded_bytes,
                replacement_text,
            )
            if glyphs_present:
                embedded_safe, embedded_note = self._embedded_roundtrip_safe(
                    embedded.embedded_bytes,
                    replacement_text,
                )
            else:
                embedded_note = (
                    "Embedded subset does not contain all required Unicode glyphs."
                )

            if embedded_safe:
                exact_embedded = FontCandidate(
                    path=None,
                    family=f"Embedded PDF: {pdf_name}",
                    subfamily=embedded.subfamily,
                    full_name=pdf_name,
                    postscript=embedded.postscript or pdf_name,
                    weight=(
                        embedded.weight
                        if embedded.weight is not None
                        else target_weight
                    ),
                    italic=(
                        embedded.italic
                        if embedded.italic is not None
                        else target_italic
                    ),
                    source="Exact embedded PDF font program",
                    embedded_bytes=embedded.embedded_bytes,
                )

                return FontResolution(
                    ok=True,
                    candidate=exact_embedded,
                    exact_family=True,
                    exact_weight=True,
                    exact_style=True,
                    glyphs_ok=True,
                    source_note=(
                        "PDF-Only Exact Font Mode: using the actual embedded PDF "
                        "font program. No installed font substitution was performed. "
                        + embedded_note
                    ),
                )

        # A readable family prefix is also useful when the PDF's font program is
        # not embedded or is only a subset. V3.9 performs an *exact family-name*
        # lookup only; it still never chooses Arial / Sukhumvit / etc. just because
        # their metrics happen to look similar.
        readable_pdf_family = humanize_pdf_font_name(pdf_name)
        online_note = ""

        if readable_pdf_family:
            manual = self._manual_match(
                readable_pdf_family,
                target_weight,
                target_italic,
            )

            if manual and manual.path:
                resolved_manual, glyphs_ok, missing = self._candidate_text_check(
                    page,
                    span,
                    manual,
                    replacement_text,
                )

                if glyphs_ok:
                    return FontResolution(
                        ok=True,
                        candidate=resolved_manual,
                        exact_family=True,
                        exact_weight=(
                            abs((manual.weight or 400) - target_weight) <= 100
                        ),
                        exact_style=manual.italic == target_italic,
                        glyphs_ok=True,
                        source_note=(
                            "PDF-Only Exact Font Mode: PDF family name "
                            f"'{readable_pdf_family}' matched a manually loaded "
                            "full font file. "
                            + (
                                "The full font Unicode cmap verified the replacement "
                                "glyphs. V3.12 will extend the original Type1C/CFF "
                                "subset in-place and keep the original PDF /F# resource."
                                if getattr(resolved_manual, "extend_original_cff", False)
                                else "The replacement glyphs passed the normal font check."
                            )
                        ),
                    )

            system = self.system_fonts.find_best(
                readable_pdf_family,
                target_weight,
                target_italic,
            )

            if system and system.path:
                glyphs_ok, missing = font_path_has_text(
                    system.path,
                    replacement_text,
                )
                weight_ok = abs((system.weight or 400) - target_weight) <= 100
                italic_ok = system.italic == target_italic

                if glyphs_ok and weight_ok and italic_ok:
                    resolved_system = self._cff_subset_extension_candidate(page, span, system)
                    return FontResolution(
                        ok=True,
                        candidate=resolved_system,
                        exact_family=True,
                        exact_weight=True,
                        exact_style=True,
                        glyphs_ok=True,
                        source_note=(
                            "PDF-Only Exact Font Mode: PDF family name "
                            f"'{readable_pdf_family}' matched the installed full "
                            "font family, weight and style. "
                            + (
                                "V3.12 will extend the original Type1C/CFF subset in-place "
                                "and keep the original PDF /F# resource."
                                if getattr(resolved_system, "extend_original_cff", False)
                                else ""
                            )
                        ),
                    )

            online, online_note = self._online_match(
                readable_pdf_family,
                target_weight,
                target_italic,
                replacement_text,
            )

            if online and online.path:
                glyphs_ok, missing = font_path_has_text(
                    online.path,
                    replacement_text,
                )
                weight_ok = abs((online.weight or 400) - target_weight) <= 100
                italic_ok = online.italic == target_italic

                if glyphs_ok and weight_ok and italic_ok:
                    return FontResolution(
                        ok=True,
                        candidate=online,
                        exact_family=True,
                        exact_weight=True,
                        exact_style=True,
                        glyphs_ok=True,
                        source_note=(
                            "PDF-Only Exact Font Mode: the readable PDF family name "
                            f"'{readable_pdf_family}' matched an approved open font "
                            "that was automatically downloaded, verified and cached. "
                            "It will be embedded as a normal font resource; online "
                            "glyphs are not spliced into the original PDF subset. "
                            + (online_note or "")
                        ),
                    )

        # If an embedded subset exists but cannot write the new characters, a
        # manually loaded full font may still be fingerprint-verified. Metric-only
        # automatic system-font substitution remains disabled.
        fingerprint_candidate = None
        fingerprint_note = ""

        if embedded and embedded.embedded_bytes:
            fingerprint_candidate, fingerprint_note = self._fingerprint_match(
                page,
                span,
                embedded,
                replacement_text,
                target_weight,
                target_italic,
            )

        if fingerprint_candidate:
            resolved_fingerprint = self._cff_subset_extension_candidate(page, span, fingerprint_candidate)
            return FontResolution(
                ok=True,
                candidate=resolved_fingerprint,
                exact_family=True,
                exact_weight=(
                    abs((fingerprint_candidate.weight or 400) - target_weight) <= 100
                ),
                exact_style=fingerprint_candidate.italic == target_italic,
                glyphs_ok=True,
                source_note=(
                    "PDF-Only Exact Font Mode: manually selected font verified "
                    "against the embedded PDF font. "
                    + (fingerprint_note or "")
                    + (
                        " V3.12 will extend the original Type1C/CFF subset in-place "
                        "and keep the original PDF /F# resource."
                        if getattr(resolved_fingerprint, "extend_original_cff", False)
                        else ""
                    )
                ),
            )

        # Prefer the most precise missing-character report we obtained.
        missing_options = [
            value for value in (resource_missing, embedded_missing)
            if value
        ]
        missing = min(missing_options, key=len) if missing_options else []

        # Give a precise explanation instead of the old blanket "could not
        # extract" error. In particular, Connections_* names keep their readable
        # family prefix, so the user knows which full font is required.
        family_hint = readable_pdf_family or pdf_name

        if embedded and embedded.embedded_bytes:
            reason = (
                "PDF-Only Exact Font Mode could not safely reproduce the requested "
                "replacement with the page font or its embedded subset. "
                + (embedded_note + " " if embedded_note else "")
            )
        else:
            reason = (
                "The PDF font is not extractable as a full font program, and the "
                "existing page font resource could not reproduce this replacement "
                "exactly. "
            )

        reason += (
            f"The exact full font family appears to be '{family_hint}'. "
            "Install that exact font on this computer or use 'Load Exact TTF/OTF "
            "Font...'. No unrelated system-font substitution will be used."
        )

        if resource_note:
            reason += " " + resource_note
        if online_note:
            reason += " " + online_note
        if fingerprint_note:
            reason += " " + fingerprint_note

        return FontResolution(
            ok=False,
            reason=reason,
            missing_chars=missing,
            glyphs_ok=False,
        )

