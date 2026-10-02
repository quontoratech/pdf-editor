"""Extend an existing single-byte Type1/CFF PDF font resource in-place.

This module is intentionally conservative. It is used only when:
- the selected PDF font is an embedded Type1C/CFF simple font,
- an exact full TTF/OTF source or a geometry-calibrated glyph plan has already
  been verified by the resolver, and
- the original PDF subset lacks one or more characters required by new text.

Instead of adding a new Type0/CID font resource, we append only the missing
glyphs to the original CFF program, add unused byte codes to the existing PDF
Encoding / ToUnicode maps, and then write text with the original /F# resource.
Existing byte codes and widths are preserved, so untouched text using the same
font xref keeps its original layout.
"""

from __future__ import annotations

import io
import difflib
import math
import re
import statistics
from ctypes import byref
from dataclasses import dataclass
from typing import Dict, Iterable, Tuple

import pymupdf
from fontTools.agl import toUnicode as agl_to_unicode
from fontTools.cffLib import CFFFontSet
from fontTools.pens.areaPen import AreaPen
from fontTools.pens.boundsPen import BoundsPen
from fontTools.pens.momentsPen import MomentsPen
from fontTools.pens.recordingPen import RecordingPen
from fontTools.pens.t2CharStringPen import T2CharStringPen
from fontTools.pens.transformPen import TransformPen
from fontTools.ttLib import TTFont


class CffSubsetExtensionError(RuntimeError):
    pass


@dataclass
class ExtensionResult:
    refname: str
    encoded: bytes
    added_chars: list[str]
    added_codes: dict[str, int]


def _parse_differences(obj: str) -> Dict[int, str]:
    match = re.search(r"/Differences\s*\[(.*?)\]", obj or "", re.S)
    if not match:
        return {}

    tokens = re.findall(r"/[^\s\[\]<>]+|[-+]?\d+", match.group(1))
    result: Dict[int, str] = {}
    code = None

    for token in tokens:
        if token.startswith("/"):
            if code is not None:
                result[int(code)] = token[1:]
                code += 1
        else:
            code = int(token)

    return result


def _glyph_unicode(name: str):
    if name == "space":
        return 0x20

    match = re.fullmatch(r"uni([0-9A-Fa-f]{4,6})", name or "")
    if match:
        return int(match.group(1), 16)

    match = re.fullmatch(r"u([0-9A-Fa-f]{5,6})", name or "")
    if match:
        return int(match.group(1), 16)

    common = {
        "bar": 0x7C,
        "comma": 0x2C,
        "numbersign": 0x23,
        "period": 0x2E,
        "hyphen": 0x2D,
        "slash": 0x2F,
    }
    if name in common:
        return common[name]

    # Many valid CFF programs use Adobe Glyph List names (A, U, zero,
    # quotedbl, etc.) rather than uniXXXX. Supporting those names is essential
    # when the donor came from a different PDF producer.
    try:
        value = agl_to_unicode(str(name or ""))
    except Exception:
        value = ""
    return ord(value) if len(value) == 1 else None


def _glyph_name(codepoint: int) -> str:
    if codepoint == 0x20:
        return "space"
    if codepoint <= 0xFFFF:
        return f"uni{codepoint:04X}"
    return f"u{codepoint:06X}"


def _existing_glyph_name(char_strings, codepoint: int):
    """Return the target CFF's own name for *codepoint*, if present."""
    preferred = _glyph_name(codepoint)
    names = getattr(char_strings, "charStrings", char_strings)
    if preferred in names:
        return preferred
    for name in names:
        if _glyph_unicode(str(name)) == codepoint:
            return str(name)
    return None


def _parse_tounicode(data: bytes) -> Dict[int, int]:
    text = (data or b"").decode("latin1", "ignore")
    result: Dict[int, int] = {}

    # Handles the bfchar form used by the statement PDFs. Also accepts simple
    # 1-byte source codes from other producers.
    for source, target in re.findall(
        r"<([0-9A-Fa-f]{2,4})>\s*<([0-9A-Fa-f]{4,8})>",
        text,
    ):
        try:
            source_code = int(source, 16)
            if source_code > 255:
                continue

            target_bytes = bytes.fromhex(target)
            try:
                decoded = target_bytes.decode("utf-16-be")
                if decoded:
                    result[source_code] = ord(decoded[0])
            except Exception:
                if len(target) <= 6:
                    result[source_code] = int(target, 16)
        except Exception:
            continue

    return result


def _build_tounicode(mapping: Dict[int, int]) -> bytes:
    pairs = sorted(mapping.items())
    lines = [
        "/CIDInit /ProcSet findresource",
        "begin",
        "12 dict",
        "begin",
        "begincmap",
        "/CIDSystemInfo 3 dict",
        "dup",
        "begin",
        "/Registry (Adobe) def",
        "/Ordering (UCS) def",
        "/Supplement 0 def",
        "end",
        "def",
        "/CMapName /ExtendedExact def",
        "/CMapType 2 def",
        "1 begincodespacerange",
        "<00> <FF>",
        "endcodespacerange",
    ]

    for start in range(0, len(pairs), 100):
        chunk = pairs[start : start + 100]
        lines.append(f"{len(chunk)} beginbfchar")
        for byte_code, codepoint in chunk:
            if codepoint <= 0xFFFF:
                target = f"{codepoint:04X}"
            else:
                value = codepoint - 0x10000
                high = 0xD800 + (value >> 10)
                low = 0xDC00 + (value & 0x3FF)
                target = f"{high:04X}{low:04X}"
            lines.append(f"<{byte_code:02X}> <{target}>")
        lines.append("endbfchar")

    lines.extend(
        [
            "endcmap",
            "CMapName",
            "currentdict",
            "/CMap defineresource",
            "pop",
            "end",
            "end",
            "",
        ]
    )
    return "\n".join(lines).encode("ascii")


def _xref_number(value: str) -> int:
    try:
        return int(str(value).split()[0])
    except Exception as exc:
        raise CffSubsetExtensionError(f"Invalid PDF xref value: {value!r}") from exc


def _font_components(doc, font_xref: int):
    font_obj = doc.xref_object(font_xref, compressed=False)
    if "/Subtype /Type1" not in font_obj:
        raise CffSubsetExtensionError("Selected PDF font is not a Type1 simple font.")

    fd_type, fd_value = doc.xref_get_key(font_xref, "FontDescriptor")
    enc_type, enc_value = doc.xref_get_key(font_xref, "Encoding")
    tu_type, tu_value = doc.xref_get_key(font_xref, "ToUnicode")

    if fd_type != "xref" or enc_type != "xref" or tu_type != "xref":
        raise CffSubsetExtensionError(
            "Subset extension requires xref-based FontDescriptor, Encoding and ToUnicode objects."
        )

    descriptor_xref = _xref_number(fd_value)
    encoding_xref = _xref_number(enc_value)
    tounicode_xref = _xref_number(tu_value)

    ff_type, ff_value = doc.xref_get_key(descriptor_xref, "FontFile3")
    if ff_type != "xref":
        raise CffSubsetExtensionError("The Type1 font does not contain an embedded FontFile3.")

    fontfile_xref = _xref_number(ff_value)
    fontfile_obj = doc.xref_object(fontfile_xref, compressed=False)
    if "/Subtype /Type1C" not in fontfile_obj:
        raise CffSubsetExtensionError(
            "The embedded font is not a Type1C/CFF program; in-place CFF extension is not applicable."
        )

    first_match = re.search(r"/FirstChar\s+(\d+)", font_obj)
    last_match = re.search(r"/LastChar\s+(\d+)", font_obj)
    widths_match = re.search(r"/Widths\s*\[(.*?)\]", font_obj, re.S)
    if not first_match or not last_match or not widths_match:
        raise CffSubsetExtensionError("The simple font has no usable FirstChar/LastChar/Widths array.")

    first_char = int(first_match.group(1))
    last_char = int(last_match.group(1))
    widths = [
        float(value)
        for value in re.findall(r"[-+]?\d+(?:\.\d+)?", widths_match.group(1))
    ]

    expected = last_char - first_char + 1
    if len(widths) < expected:
        widths.extend([300.0] * (expected - len(widths)))
    elif len(widths) > expected:
        widths = widths[:expected]

    return {
        "font_obj": font_obj,
        "descriptor_xref": descriptor_xref,
        "encoding_xref": encoding_xref,
        "tounicode_xref": tounicode_xref,
        "fontfile_xref": fontfile_xref,
        "first_char": first_char,
        "last_char": last_char,
        "widths": widths,
    }


def _full_font_source(candidate):
    if getattr(candidate, "subset_extension_font_path", None):
        return TTFont(candidate.subset_extension_font_path)
    if getattr(candidate, "subset_extension_font_bytes", None):
        return TTFont(io.BytesIO(candidate.subset_extension_font_bytes))
    raise CffSubsetExtensionError("No verified full font source is attached to this extension candidate.")


def _format_width(value: float) -> str:
    if abs(value - round(value)) < 1e-6:
        return str(int(round(value)))
    return f"{value:.6f}".rstrip("0").rstrip(".")


def _cff_charstring_width(top, glyph_name: str):
    """Return a CFF glyph's intrinsic advance, even when it is unencoded."""
    try:
        charstring = top.CharStrings[glyph_name]
        # Decompiling/drawing resolves defaultWidthX / nominalWidthX and sets
        # ``width`` on T2CharString. No PDF or font bytes are mutated.
        charstring.draw(RecordingPen())
        width = getattr(charstring, "width", None)
        return float(width) if width is not None else None
    except Exception:
        return None


def verify_donor_compatibility(
    doc,
    candidate,
    reference_text: str = "",
) -> dict:
    """Compare a proposed full-font donor with the original embedded CFF.

    Family names and OS/2 weight metadata are not enough to prove that two
    font files are the same face.  This comparison uses glyphs present in both
    programs and checks scale-independent ink density plus normalized bounds
    and advance widths.  A donor is rejected before any PDF object is changed
    when it looks like a neighboring weight or a different font revision.
    """
    if not getattr(candidate, "pdf_xref", None):
        return {
            "ok": False,
            "reason": "The donor has no original PDF font xref to compare.",
            "common_glyphs": [],
        }

    full_font = None
    try:
        parts = _font_components(doc, int(candidate.pdf_xref))
        encoding = _parse_differences(
            doc.xref_object(parts["encoding_xref"], compressed=False)
        )
        cff_bytes = doc.xref_stream(parts["fontfile_xref"])
        ot_font = TTFont(recalcBBoxes=False, recalcTimestamp=False)
        cff_set = CFFFontSet()
        cff_set.decompile(io.BytesIO(cff_bytes), ot_font)
        top = cff_set[cff_set.fontNames[0]]
        target_glyphs = top.CharStrings

        full_font = _full_font_source(candidate)
        donor_glyphs = full_font.getGlyphSet()
        donor_cmap = full_font.getBestCmap() or {}
        donor_upem = float(full_font["head"].unitsPerEm)
        donor_hmtx = full_font["hmtx"].metrics

        name_to_width = {}
        for code, name in encoding.items():
            if parts["first_char"] <= code <= parts["last_char"]:
                index = code - parts["first_char"]
                if 0 <= index < len(parts["widths"]):
                    name_to_width.setdefault(name, float(parts["widths"][index]))

        # Prefer glyphs from the selected source text, then broaden the probe
        # so a short field still has enough evidence for a safe decision.
        probe_text = (
            str(reference_text or "")
            + "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
            + "abcdefghijklmnopqrstuvwxyz"
            + "0123456789"
        )
        probe_chars = []
        seen = set()
        for char in probe_text:
            if char.isspace() or char in seen:
                continue
            seen.add(char)
            target_name = _existing_glyph_name(target_glyphs, ord(char))
            donor_name = donor_cmap.get(ord(char))
            if (
                target_name is not None
                and target_name in target_glyphs.charStrings
                and donor_name in donor_glyphs
                and target_name in name_to_width
            ):
                probe_chars.append((char, target_name, donor_name))
            if len(probe_chars) >= 16:
                break

        if len(probe_chars) < 3:
            return {
                "ok": False,
                "reason": (
                    "The donor and original subset do not share enough measurable "
                    "glyphs to prove that they are the same face."
                ),
                "common_glyphs": [item[0] for item in probe_chars],
            }

        area_deltas = []
        advance_deltas = []
        bounds_deltas = []
        measured_chars = []

        for char, target_name, donor_name in probe_chars:
            target_bounds_pen = BoundsPen(None)
            target_area_pen = AreaPen(None)
            target_glyphs[target_name].draw(target_bounds_pen)
            target_glyphs[target_name].draw(target_area_pen)

            donor_bounds_pen = BoundsPen(donor_glyphs)
            donor_area_pen = AreaPen(donor_glyphs)
            donor_glyphs[donor_name].draw(donor_bounds_pen)
            donor_glyphs[donor_name].draw(donor_area_pen)

            target_bounds = target_bounds_pen.bounds
            donor_bounds = donor_bounds_pen.bounds
            if not target_bounds or not donor_bounds:
                continue

            target_box_area = (
                max(0.0, float(target_bounds[2] - target_bounds[0]))
                * max(0.0, float(target_bounds[3] - target_bounds[1]))
            )
            donor_box_area = (
                max(0.0, float(donor_bounds[2] - donor_bounds[0]))
                * max(0.0, float(donor_bounds[3] - donor_bounds[1]))
            )
            if target_box_area <= 0.0 or donor_box_area <= 0.0:
                continue

            target_density = abs(float(target_area_pen.value)) / target_box_area
            donor_density = abs(float(donor_area_pen.value)) / donor_box_area
            area_deltas.append(
                abs(target_density - donor_density)
                / max(target_density, donor_density, 1e-9)
            )

            target_advance = float(name_to_width[target_name]) / 1000.0
            donor_advance = (
                float(donor_hmtx[donor_name][0]) / donor_upem
            )
            advance_deltas.append(
                abs(target_advance - donor_advance)
                / max(target_advance, donor_advance, 1e-9)
            )

            target_normalized = [float(value) / 1000.0 for value in target_bounds]
            donor_normalized = [float(value) / donor_upem for value in donor_bounds]
            bounds_deltas.append(
                max(
                    abs(target_value - donor_value)
                    for target_value, donor_value in zip(
                        target_normalized,
                        donor_normalized,
                    )
                )
            )
            measured_chars.append(char)

        if len(measured_chars) < 3:
            return {
                "ok": False,
                "reason": "Too few shared glyph outlines could be measured safely.",
                "common_glyphs": measured_chars,
            }

        median_area_delta = float(statistics.median(area_deltas))
        median_advance_delta = float(statistics.median(advance_deltas))
        median_bounds_delta = float(statistics.median(bounds_deltas))
        max_area_delta = float(max(area_deltas))
        max_advance_delta = float(max(advance_deltas))
        max_bounds_delta = float(max(bounds_deltas))

        # Exact subset/full-font pairs are normally within rounding noise.  The
        # relaxed maxima tolerate a single unusually hinted glyph, while the
        # medians reject systematically lighter/heavier neighboring faces.
        ok = bool(
            median_area_delta <= 0.05
            and median_advance_delta <= 0.05
            and median_bounds_delta <= 0.04
            and max_area_delta <= 0.12
            and max_advance_delta <= 0.12
            and max_bounds_delta <= 0.10
        )
        metrics = {
            "median_area_delta": median_area_delta,
            "median_advance_delta": median_advance_delta,
            "median_bounds_delta": median_bounds_delta,
            "max_area_delta": max_area_delta,
            "max_advance_delta": max_advance_delta,
            "max_bounds_delta": max_bounds_delta,
        }
        if ok:
            reason = (
                f"Donor outline fingerprint matched {len(measured_chars)} "
                "existing glyphs in the original CFF subset."
            )
        else:
            reason = (
                "The saved font has the same family name but its shared glyph "
                "outlines/weight do not match the original embedded subset "
                f"(median ink delta {median_area_delta:.1%})."
            )
        return {
            "ok": ok,
            "reason": reason,
            "common_glyphs": measured_chars,
            **metrics,
        }
    except Exception as exc:
        return {
            "ok": False,
            "reason": f"The donor/original glyph fingerprint could not be verified: {exc}",
            "common_glyphs": [],
        }
    finally:
        if full_font is not None:
            try:
                full_font.close()
            except Exception:
                pass


def _freetype_module():
    try:
        import freetype

        return freetype
    except ImportError as exc:
        raise CffSubsetExtensionError(
            "Adaptive glyph weight matching requires the freetype-py package."
        ) from exc


def _outline_metrics(drawable):
    """Return scale-independent metrics for a drawable glyph outline."""
    area_pen = AreaPen(None)
    bounds_pen = BoundsPen(None)
    moments_pen = MomentsPen(None)
    if hasattr(drawable, "replay"):
        drawable.replay(area_pen)
        drawable.replay(bounds_pen)
        drawable.replay(moments_pen)
    else:
        drawable.draw(area_pen)
        drawable.draw(bounds_pen)
        drawable.draw(moments_pen)

    bounds = bounds_pen.bounds
    signed_area = float(moments_pen.area)
    if not bounds or abs(signed_area) < 1e-6:
        return None
    width = float(bounds[2] - bounds[0])
    height = float(bounds[3] - bounds[1])
    if width <= 1e-6 or height <= 1e-6:
        return None

    center_x = float(moments_pen.momentX) / signed_area
    center_y = float(moments_pen.momentY) / signed_area
    signature = (
        (center_x - float(bounds[0])) / width,
        (center_y - float(bounds[1])) / height,
        (
            float(moments_pen.momentXX) / signed_area
            - center_x * center_x
        )
        / (width * width),
        (
            float(moments_pen.momentYY) / signed_area
            - center_y * center_y
        )
        / (height * height),
        (
            float(moments_pen.momentXY) / signed_area
            - center_x * center_y
        )
        / (width * height),
    )
    return {
        "area": abs(float(area_pen.value)),
        "bounds": tuple(float(value) for value in bounds),
        "density": abs(float(area_pen.value)) / (width * height),
        "signature": signature,
    }


def _cff_outline_source(doc, font_xref: int):
    """Open an embedded PDF CFF as a read-only adaptive-glyph source."""
    parts = _font_components(doc, int(font_xref))
    cff_bytes = doc.xref_stream(parts["fontfile_xref"])
    if not cff_bytes:
        raise CffSubsetExtensionError("Embedded donor CFF stream is empty.")

    ot_font = TTFont(recalcBBoxes=False, recalcTimestamp=False)
    cff_set = CFFFontSet()
    cff_set.decompile(io.BytesIO(cff_bytes), ot_font)
    if not cff_set.fontNames:
        raise CffSubsetExtensionError("Embedded donor CFF could not be read.")
    top = cff_set[cff_set.fontNames[0]]
    glyph_order = list(top.charset)
    unicode_to_name = {}
    for name in glyph_order:
        codepoint = _glyph_unicode(name)
        if codepoint is not None:
            unicode_to_name.setdefault(codepoint, name)

    freetype = _freetype_module()
    face = freetype.Face(io.BytesIO(cff_bytes))
    upem = float(face.units_per_EM or 1000)
    name_to_index = {name: index for index, name in enumerate(glyph_order)}
    widths = {
        codepoint: _cff_charstring_width(top, name)
        for codepoint, name in unicode_to_name.items()
    }
    return {
        "kind": "pdf_cff",
        "font_xref": int(font_xref),
        "face": face,
        "upem": upem,
        "unicode_to_name": unicode_to_name,
        "name_to_index": name_to_index,
        "widths": widths,
        "top": top,
    }


def _pdf_font_identity(doc, font_xref: int):
    """Return a coarse family/style identity used before geometric matching."""
    obj = doc.xref_object(int(font_xref), compressed=False)
    match = re.search(r"/BaseFont\s*/([^\s<>\[\]()]+)", obj or "")
    raw = match.group(1) if match else ""
    if "+" in raw:
        prefix, remainder = raw.split("+", 1)
        if prefix.isalpha() and 1 <= len(prefix) <= 8:
            raw = remainder
    # Statement generators often add a technical suffix after the face name.
    raw = re.sub(r"_CZEX[0-9A-Za-z]+$", "", raw, flags=re.I)
    lowered = raw.casefold()
    italic = bool(re.search(r"italic|oblique|ita$", lowered))
    lowered = re.sub(r"ita$", "", lowered)
    family = re.sub(
        r"black|extrabold|ultrabold|semibold|demibold|bold|medium|regular|"
        r"book|light|thin|italic|oblique",
        "",
        lowered,
    )
    family = "".join(char for char in family if char.isalnum())
    return family, italic, raw


def _external_outline_source(candidate):
    """Open an attached TTF/OTF as a read-only adaptive-glyph source."""
    path = getattr(candidate, "subset_extension_font_path", None) or getattr(
        candidate, "path", None
    )
    font_bytes = getattr(candidate, "subset_extension_font_bytes", None) or getattr(
        candidate, "embedded_bytes", None
    )
    font_number = int(getattr(candidate, "font_number", 0) or 0)
    if path:
        tt_font = TTFont(path, fontNumber=font_number)
        face_input = path
    elif font_bytes:
        tt_font = TTFont(io.BytesIO(font_bytes), fontNumber=font_number)
        face_input = io.BytesIO(font_bytes)
    else:
        raise CffSubsetExtensionError("No external donor font is attached.")

    try:
        cmap = tt_font.getBestCmap() or {}
        glyph_order = tt_font.getGlyphOrder()
        name_to_index = {name: index for index, name in enumerate(glyph_order)}
        hmtx = tt_font["hmtx"].metrics
        upem = float(tt_font["head"].unitsPerEm)
        widths = {
            int(codepoint): float(hmtx[name][0]) * 1000.0 / upem
            for codepoint, name in cmap.items()
            if name in hmtx
        }
        unicode_to_name = {int(codepoint): name for codepoint, name in cmap.items()}
    finally:
        tt_font.close()

    freetype = _freetype_module()
    face = freetype.Face(face_input, index=font_number)
    return {
        "kind": "external_font",
        "face": face,
        "upem": float(face.units_per_EM or upem or 1000),
        "unicode_to_name": unicode_to_name,
        "name_to_index": name_to_index,
        "widths": widths,
    }


def _exact_external_glyph_recording(candidate, codepoint: int):
    """Copy one exact donor glyph into a 1000-unit recording without leaks."""
    font = _full_font_source(candidate)
    try:
        cmap = font.getBestCmap() or {}
        source_name = cmap.get(int(codepoint))
        if source_name is None:
            raise CffSubsetExtensionError(
                f"Verified full font does not contain U+{int(codepoint):04X}."
            )
        glyph_set = font.getGlyphSet()
        upem = float(font["head"].unitsPerEm)
        advance = float(font["hmtx"].metrics[source_name][0]) * 1000.0 / upem
        recording = RecordingPen()
        glyph_set[source_name].draw(
            TransformPen(
                recording,
                (1000.0 / upem, 0.0, 0.0, 1000.0 / upem, 0.0, 0.0),
            )
        )
        return advance, recording
    finally:
        font.close()


def _emboldened_recording(source, glyph_index: int, strength_x: float, strength_y: float):
    """Decompose a FreeType-emboldened glyph into a 1000-unit recording."""
    freetype = _freetype_module()
    face = source["face"]
    flags = (
        freetype.FT_LOAD_NO_SCALE
        | freetype.FT_LOAD_NO_HINTING
        | freetype.FT_LOAD_NO_BITMAP
    )
    face.load_glyph(int(glyph_index), flags)
    outline = face.glyph.outline
    donor_upem = float(source.get("upem") or 1000.0)
    x_strength = int(round(float(strength_x) * donor_upem / 1000.0))
    y_strength = int(round(float(strength_y) * donor_upem / 1000.0))
    if x_strength or y_strength:
        error = freetype.FT_Outline_EmboldenXY(
            byref(outline._FT_Outline),
            x_strength,
            y_strength,
        )
        if error:
            raise CffSubsetExtensionError(
                f"FreeType could not embolden donor outline (error {error})."
            )

    recording = RecordingPen()
    contour_open = [False]

    def point(vector):
        return (
            float(vector.x) * 1000.0 / donor_upem,
            float(vector.y) * 1000.0 / donor_upem,
        )

    def move_to(vector, _context):
        if contour_open[0]:
            recording.closePath()
        recording.moveTo(point(vector))
        contour_open[0] = True

    def line_to(vector, _context):
        recording.lineTo(point(vector))

    def conic_to(control, vector, _context):
        recording.qCurveTo(point(control), point(vector))

    def cubic_to(control1, control2, vector, _context):
        recording.curveTo(point(control1), point(control2), point(vector))

    outline.decompose(None, move_to, line_to, conic_to, cubic_to)
    if contour_open[0]:
        recording.closePath()
    return recording


def _target_cff_profile(doc, font_xref: int):
    source = _cff_outline_source(doc, int(font_xref))
    top = source["top"]
    parts = _font_components(doc, int(font_xref))
    encoding = _parse_differences(
        doc.xref_object(parts["encoding_xref"], compressed=False)
    )
    encoded_widths = {}
    for code, name in encoding.items():
        if parts["first_char"] <= code <= parts["last_char"]:
            index = code - parts["first_char"]
            if 0 <= index < len(parts["widths"]):
                encoded_widths.setdefault(name, float(parts["widths"][index]))

    unicode_to_name = source["unicode_to_name"]
    widths = {}
    metrics = {}
    for codepoint, name in unicode_to_name.items():
        width = encoded_widths.get(name)
        if width is None:
            width = _cff_charstring_width(top, name)
        if width is not None:
            widths[codepoint] = float(width)
        try:
            current = _outline_metrics(top.CharStrings[name])
        except Exception:
            current = None
        if current:
            metrics[codepoint] = current
    source["widths"] = widths
    source["metrics"] = metrics
    return source


def _probe_codepoints(target, donor, reference_text: str, limit: int = 20):
    order = (
        str(reference_text or "")
        + "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        + "abcdefghijklmnopqrstuvwxyz"
        + "0123456789"
    )
    result = []
    seen = set()
    for char in order:
        codepoint = ord(char)
        if char.isspace() or codepoint in seen:
            continue
        seen.add(codepoint)
        donor_name = donor["unicode_to_name"].get(codepoint)
        if (
            codepoint in target["metrics"]
            and codepoint in target["widths"]
            and donor_name in donor["name_to_index"]
            and donor["widths"].get(codepoint) is not None
        ):
            result.append(codepoint)
        if len(result) >= limit:
            break
    return result


def _adaptive_trial(target, donor, probes, strength_x: int, strength_y: int, cache):
    rows = []
    for codepoint in probes:
        donor_name = donor["unicode_to_name"][codepoint]
        glyph_index = donor["name_to_index"][donor_name]
        cache_key = (glyph_index, int(strength_x), int(strength_y))
        donor_metrics = cache.get(cache_key)
        if donor_metrics is None:
            donor_metrics = _outline_metrics(
                _emboldened_recording(
                    donor,
                    glyph_index,
                    strength_x,
                    strength_y,
                )
            )
            cache[cache_key] = donor_metrics
        if donor_metrics:
            rows.append((target["metrics"][codepoint], donor_metrics))
    if len(rows) < 8:
        return None

    scale_x = statistics.median(
        (target_metrics["bounds"][2] - target_metrics["bounds"][0])
        / (donor_metrics["bounds"][2] - donor_metrics["bounds"][0])
        for target_metrics, donor_metrics in rows
    )
    scale_y = statistics.median(
        (target_metrics["bounds"][3] - target_metrics["bounds"][1])
        / (donor_metrics["bounds"][3] - donor_metrics["bounds"][1])
        for target_metrics, donor_metrics in rows
    )
    if scale_x <= 0.0 or scale_y <= 0.0:
        return None
    translate_x = statistics.median(
        target_metrics["bounds"][0] - scale_x * donor_metrics["bounds"][0]
        for target_metrics, donor_metrics in rows
    )
    translate_y = statistics.median(
        target_metrics["bounds"][1] - scale_y * donor_metrics["bounds"][1]
        for target_metrics, donor_metrics in rows
    )

    area_deltas = []
    density_deltas = []
    bounds_deltas = []
    moment_deltas = []
    for target_metrics, donor_metrics in rows:
        adjusted_area = donor_metrics["area"] * abs(scale_x * scale_y)
        area_deltas.append(
            abs(target_metrics["area"] - adjusted_area)
            / max(target_metrics["area"], adjusted_area, 1e-9)
        )
        density_deltas.append(
            abs(target_metrics["density"] - donor_metrics["density"])
            / max(target_metrics["density"], donor_metrics["density"], 1e-9)
        )
        donor_bounds = donor_metrics["bounds"]
        adjusted_bounds = (
            scale_x * donor_bounds[0] + translate_x,
            scale_y * donor_bounds[1] + translate_y,
            scale_x * donor_bounds[2] + translate_x,
            scale_y * donor_bounds[3] + translate_y,
        )
        bounds_deltas.append(
            max(
                abs(left - right)
                for left, right in zip(target_metrics["bounds"], adjusted_bounds)
            )
            / 1000.0
        )
        moment_deltas.append(
            max(
                abs(left - right)
                for left, right in zip(
                    target_metrics["signature"], donor_metrics["signature"]
                )
            )
        )

    median_area = float(statistics.median(area_deltas))
    median_density = float(statistics.median(density_deltas))
    median_bounds = float(statistics.median(bounds_deltas))
    median_moments = float(statistics.median(moment_deltas))
    score = (
        1.2 * median_area
        + 1.5 * median_density
        + 2.0 * median_bounds
        + 3.0 * median_moments
        + 0.15 * abs(math.log(scale_x))
        + 0.15 * abs(math.log(scale_y))
    )
    return {
        "score": float(score),
        "strength_x": int(strength_x),
        "strength_y": int(strength_y),
        "scale_x": float(scale_x),
        "scale_y": float(scale_y),
        "translate_x": float(translate_x),
        "translate_y": float(translate_y),
        "median_area_delta": median_area,
        "median_density_delta": median_density,
        "median_bounds_delta": median_bounds,
        "median_moment_delta": median_moments,
        "max_bounds_delta": float(max(bounds_deltas)),
        "max_moment_delta": float(max(moment_deltas)),
        "probe_count": len(rows),
    }


def _robust_advance_map(target, donor, probes):
    pairs = [
        (float(donor["widths"][cp]), float(target["widths"][cp]))
        for cp in probes
        if donor["widths"].get(cp) is not None and target["widths"].get(cp) is not None
    ]
    slopes = []
    for index, (source_x, target_y) in enumerate(pairs):
        for other_x, other_y in pairs[index + 1 :]:
            if abs(other_x - source_x) > 1e-6:
                slopes.append((other_y - target_y) / (other_x - source_x))
    if slopes:
        scale = float(statistics.median(slopes))
        offset = float(
            statistics.median(target_y - scale * source_x for source_x, target_y in pairs)
        )
    else:
        ratios = [target_y / source_x for source_x, target_y in pairs if source_x]
        scale = float(statistics.median(ratios or [1.0]))
        offset = 0.0
    scale = min(1.35, max(0.75, scale))
    offset = min(150.0, max(-150.0, offset))
    return scale, offset


def _calibrate_adaptive_source(target, donor, required_text: str, reference_text: str):
    required = [char for char in dict.fromkeys(required_text) if char not in "\r\n"]
    unavailable = [
        char
        for char in required
        if ord(char) not in donor["unicode_to_name"]
        or donor["unicode_to_name"][ord(char)] not in donor["name_to_index"]
    ]
    if unavailable:
        raise CffSubsetExtensionError(
            "Donor does not contain " + " ".join(repr(char) for char in unavailable)
        )

    probes = _probe_codepoints(target, donor, reference_text)
    if len(probes) < 8:
        raise CffSubsetExtensionError(
            "Donor and target do not share enough measurable glyphs for safe weight calibration."
        )

    cache = {}
    trials = []
    for strength_x in range(0, 121, 10):
        for strength_y in range(0, 81, 10):
            trial = _adaptive_trial(
                target,
                donor,
                probes,
                strength_x,
                strength_y,
                cache,
            )
            if trial:
                trials.append(trial)
    if not trials:
        raise CffSubsetExtensionError("No usable adaptive weight calibration was produced.")
    coarse = min(trials, key=lambda item: item["score"])
    for strength_x in range(
        max(0, coarse["strength_x"] - 10),
        min(120, coarse["strength_x"] + 10) + 1,
        2,
    ):
        for strength_y in range(
            max(0, coarse["strength_y"] - 10),
            min(80, coarse["strength_y"] + 10) + 1,
            2,
        ):
            trial = _adaptive_trial(
                target,
                donor,
                probes,
                strength_x,
                strength_y,
                cache,
            )
            if trial:
                trials.append(trial)
    best = min(trials, key=lambda item: item["score"])

    accepted = bool(
        best["score"] <= 0.16
        and best["median_area_delta"] <= 0.06
        and best["median_density_delta"] <= 0.05
        and best["median_bounds_delta"] <= 0.035
        and best["median_moment_delta"] <= 0.012
        and best["max_bounds_delta"] <= 0.11
        and best["max_moment_delta"] <= 0.06
        and 0.80 <= best["scale_x"] <= 1.20
        and 0.80 <= best["scale_y"] <= 1.20
    )
    if not accepted:
        raise CffSubsetExtensionError(
            "Donor glyph design could not be calibrated closely enough to the target "
            f"subset (adaptive score {best['score']:.3f})."
        )

    advance_scale, advance_offset = _robust_advance_map(target, donor, probes)
    source_plan = {
        key: value
        for key, value in best.items()
        if key not in {"probe_count"}
    }
    source_plan.update(
        {
            "kind": donor["kind"],
            "font_xref": donor.get("font_xref"),
            "advance_scale": float(advance_scale),
            "advance_offset": float(advance_offset),
            "probe_count": int(best["probe_count"]),
        }
    )
    glyphs = {}
    widths = {}
    for char in required:
        codepoint = ord(char)
        donor_name = donor["unicode_to_name"][codepoint]
        source_width = float(donor["widths"][codepoint])
        advance = advance_scale * source_width + advance_offset
        advance = min(2000.0, max(1.0, advance))
        glyphs[char] = {
            "glyph_name": donor_name,
            "glyph_index": int(donor["name_to_index"][donor_name]),
            "advance": float(advance),
        }
        widths[char] = float(advance) / 1000.0
    return source_plan, glyphs, widths


def build_adaptive_external_glyph_plan(
    doc,
    target_font_xref: int,
    candidate,
    required_text: str,
    reference_text: str = "",
):
    """Calibrate a TTF/OTF donor to the selected PDF CFF without mutation."""
    target = _target_cff_profile(doc, int(target_font_xref))
    donor = _external_outline_source(candidate)
    source_plan, glyphs, widths = _calibrate_adaptive_source(
        target,
        donor,
        required_text,
        reference_text,
    )
    source_plan["kind"] = "external_font"
    return {
        "version": 1,
        "target_font_xref": int(target_font_xref),
        "sources": [source_plan],
        "glyphs": {
            char: {**entry, "source": 0}
            for char, entry in glyphs.items()
        },
        "unicode_widths": widths,
        "report": (
            f"Calibrated {len(glyphs)} missing glyph(s) against "
            f"{source_plan['probe_count']} existing target glyphs; "
            f"adaptive score {source_plan['score']:.3f}."
        ),
    }


def build_adaptive_pdf_glyph_plan(
    doc,
    target_font_xref: int,
    required_text: str,
    reference_text: str = "",
):
    """Find and calibrate missing glyphs from other embedded PDF CFF resources."""
    required = [char for char in dict.fromkeys(required_text) if char not in "\r\n"]
    if not required:
        raise CffSubsetExtensionError("No missing glyphs were requested.")
    target = _target_cff_profile(doc, int(target_font_xref))
    target_family, target_italic, _target_name = _pdf_font_identity(
        doc, int(target_font_xref)
    )

    donor_xrefs = []
    seen = {int(target_font_xref)}
    for page in doc:
        for record in page.get_fonts(full=True):
            xref = int(record[0] or 0)
            font_type = str(record[2] or "").casefold()
            extension = str(record[1] or "").casefold()
            if (
                xref > 0
                and xref not in seen
                and font_type == "type1"
                and extension in {"cff", "cid", "type1c"}
            ):
                seen.add(xref)
                donor_xrefs.append(xref)

    calibrated = []
    rejected = []
    for donor_xref in donor_xrefs:
        try:
            donor_family, donor_italic, donor_name = _pdf_font_identity(
                doc, donor_xref
            )
            if donor_italic != target_italic:
                raise CffSubsetExtensionError(
                    f"{donor_name or 'donor'} has a different italic/upright style."
                )
            if target_family and donor_family:
                family_similarity = difflib.SequenceMatcher(
                    None, target_family, donor_family
                ).ratio()
                if family_similarity < 0.70:
                    raise CffSubsetExtensionError(
                        f"{donor_name or 'donor'} is a different font family "
                        f"(similarity {family_similarity:.3f})."
                    )
            donor = _cff_outline_source(doc, donor_xref)
            covered = "".join(
                char for char in required if ord(char) in donor["unicode_to_name"]
            )
            if not covered:
                continue
            source_plan, glyphs, widths = _calibrate_adaptive_source(
                target,
                donor,
                covered,
                reference_text,
            )
            source_plan["kind"] = "pdf_cff"
            source_plan["font_xref"] = int(donor_xref)
            calibrated.append((source_plan, glyphs, widths))
        except Exception as exc:
            rejected.append(f"xref {donor_xref}: {exc}")

    if not calibrated:
        raise CffSubsetExtensionError(
            "No embedded PDF font containing the missing glyphs could be safely "
            "weight-calibrated."
            + (" Checked " + "; ".join(rejected[:3]) if rejected else "")
        )

    chosen = {}
    for char in required:
        options = [item for item in calibrated if char in item[1]]
        if not options:
            continue
        chosen[char] = min(options, key=lambda item: item[0]["score"])
    missing = [char for char in required if char not in chosen]
    if missing:
        raise CffSubsetExtensionError(
            "No safe adaptive PDF glyph source was found for "
            + " ".join(repr(char) for char in missing)
        )

    sources = []
    source_indexes = {}
    glyphs = {}
    widths = {}
    for char in required:
        source_plan, source_glyphs, source_widths = chosen[char]
        identity = int(source_plan["font_xref"])
        if identity not in source_indexes:
            source_indexes[identity] = len(sources)
            sources.append(source_plan)
        glyphs[char] = {
            **source_glyphs[char],
            "source": source_indexes[identity],
        }
        widths[char] = source_widths[char]

    return {
        "version": 1,
        "target_font_xref": int(target_font_xref),
        "sources": sources,
        "glyphs": glyphs,
        "unicode_widths": widths,
        "report": (
            f"Borrowed {len(glyphs)} glyph(s) from {len(sources)} compatible PDF "
            "font resource(s) and calibrated their outlines to the selected target weight."
        ),
    }


def _adaptive_source_from_plan(doc, candidate, source_plan):
    if source_plan.get("kind") == "pdf_cff":
        return _cff_outline_source(doc, int(source_plan["font_xref"]))
    if source_plan.get("kind") == "external_font":
        return _external_outline_source(candidate)
    raise CffSubsetExtensionError("Adaptive glyph plan contains an unknown source type.")


def extend_and_encode(doc, candidate, text: str) -> ExtensionResult:
    """Extend candidate.pdf_xref as needed and encode *text* to one-byte codes."""
    if not getattr(candidate, "pdf_xref", None) or not getattr(candidate, "pdf_refname", None):
        raise CffSubsetExtensionError("Extension candidate has no original PDF font resource.")

    font_xref = int(candidate.pdf_xref)
    parts = _font_components(doc, font_xref)

    encoding_obj = doc.xref_object(parts["encoding_xref"], compressed=False)
    encoding = _parse_differences(encoding_obj)
    tounicode = _parse_tounicode(doc.xref_stream(parts["tounicode_xref"]))

    # Reuse an existing byte code only if both the visual glyph name and the
    # existing ToUnicode mapping agree. If an old PDF intentionally maps a
    # vertical-bar glyph to "!", allocate a fresh byte code for new text so the
    # replacement extracts as the character the user actually typed.
    unicode_to_code: Dict[int, int] = {}
    for byte_code, name in encoding.items():
        cp = _glyph_unicode(name)
        if cp is None:
            continue
        if byte_code in tounicode and tounicode[byte_code] != cp:
            continue
        unicode_to_code.setdefault(cp, byte_code)

    adaptive_plan = getattr(candidate, "adaptive_glyph_plan", None) or {}
    adaptive_glyphs = adaptive_plan.get("glyphs", {})
    adaptive_sources = adaptive_plan.get("sources", [])
    adaptive_source_cache = {}
    if adaptive_plan and int(adaptive_plan.get("target_font_xref", -1)) != font_xref:
        raise CffSubsetExtensionError(
            "Adaptive glyph plan does not belong to the selected PDF font resource."
        )

    cff_bytes = doc.xref_stream(parts["fontfile_xref"])
    if not cff_bytes:
        raise CffSubsetExtensionError("Original embedded CFF stream is empty.")

    ot_font = TTFont(recalcBBoxes=False, recalcTimestamp=False)
    cff_set = CFFFontSet()
    cff_set.decompile(io.BytesIO(cff_bytes), ot_font)
    if not cff_set.fontNames:
        raise CffSubsetExtensionError("Could not read the original CFF font program.")
    top = cff_set[cff_set.fontNames[0]]

    used_codes = set(encoding)
    internal_encoding = getattr(top, "Encoding", None)

    def _internally_unused(code):
        if isinstance(internal_encoding, (list, tuple)) and 0 <= code < len(internal_encoding):
            return str(internal_encoding[code]) in {".notdef", ""}
        return True

    free_codes = [
        code
        for code in range(parts["first_char"], parts["last_char"] + 1)
        if code not in used_codes and _internally_unused(code)
    ]

    # If the producer used almost the entire original range, extend the simple
    # font's byte range toward 255. This remains a one-byte Type1 font.
    if not free_codes and parts["last_char"] < 255:
        old_last = parts["last_char"]
        parts["widths"].extend([300.0] * (255 - old_last))
        parts["last_char"] = 255
        free_codes.extend(
            code for code in range(old_last + 1, 256) if _internally_unused(code)
        )

    # Existing glyph-name widths let us allocate a fresh byte code for a glyph
    # whose old ToUnicode mapping is semantically wrong (for example a visual
    # vertical bar mapped to U+0021) without requiring the external full font.
    existing_name_width = {}
    for byte_code, name in encoding.items():
        if parts["first_char"] <= byte_code <= parts["last_char"]:
            index = byte_code - parts["first_char"]
            if 0 <= index < len(parts["widths"]):
                existing_name_width.setdefault(name, float(parts["widths"][index]))

    added_chars: list[str] = []
    added_codes: dict[str, int] = {}
    cff_changed = False
    mappings_changed = False

    for char in dict.fromkeys(text):
        if char in ("\n", "\r"):
            raise CffSubsetExtensionError("Single-byte CFF extension inserts one text line at a time.")

        cp = ord(char)
        if cp in unicode_to_code:
            continue

        if not free_codes:
            raise CffSubsetExtensionError(
                "The original simple font has no unused one-byte encoding slots left."
            )

        byte_code = free_codes.pop(0)
        pdf_glyph_name = (
            _existing_glyph_name(top.CharStrings, cp)
            or _glyph_name(cp)
        )

        if pdf_glyph_name in top.CharStrings.charStrings:
            advance = existing_name_width.get(pdf_glyph_name)
            if advance is None:
                # A glyph can exist in the original CFF charset without a PDF
                # byte mapping. Its own Type2 charstring is authoritative for
                # the advance; borrowing a donor width can change spacing even
                # when the original outline itself is reused.
                advance = _cff_charstring_width(top, pdf_glyph_name)
                if advance is None:
                    raise CffSubsetExtensionError(
                        f"Could not determine the width for existing glyph {char!r}."
                    )
        else:
            adaptive_entry = adaptive_glyphs.get(char)
            if adaptive_entry:
                source_index = int(adaptive_entry.get("source", -1))
                if not (0 <= source_index < len(adaptive_sources)):
                    raise CffSubsetExtensionError(
                        f"Adaptive glyph source is invalid for {char!r}."
                    )
                source_plan = adaptive_sources[source_index]
                source = adaptive_source_cache.get(source_index)
                if source is None:
                    source = _adaptive_source_from_plan(doc, candidate, source_plan)
                    adaptive_source_cache[source_index] = source
                advance = float(adaptive_entry["advance"])
                recording = _emboldened_recording(
                    source,
                    int(adaptive_entry["glyph_index"]),
                    float(source_plan["strength_x"]),
                    float(source_plan["strength_y"]),
                )
                pen = T2CharStringPen(advance, None)
                recording.replay(
                    TransformPen(
                        pen,
                        (
                            float(source_plan["scale_x"]),
                            0.0,
                            0.0,
                            float(source_plan["scale_y"]),
                            float(source_plan["translate_x"]),
                            float(source_plan["translate_y"]),
                        ),
                    )
                )
            else:
                if adaptive_plan:
                    raise CffSubsetExtensionError(
                        f"Adaptive glyph plan does not contain the missing character {char!r}."
                    )
                # A genuinely missing glyph normally requires an exact verified
                # full font. Adaptive plans use the branch above and modify only
                # explicitly measured / approved characters.
                advance, recording = _exact_external_glyph_recording(
                    candidate, cp
                )
                pen = T2CharStringPen(advance, None)
                recording.replay(pen)
            charstring = pen.getCharString(
                private=top.Private,
                globalSubrs=top.GlobalSubrs,
            )
            index = len(top.CharStrings.charStringsIndex)
            top.CharStrings.charStringsIndex.append(charstring)
            top.CharStrings.charStrings[pdf_glyph_name] = index
            top.charset.append(pdf_glyph_name)
            cff_changed = True

        encoding[byte_code] = pdf_glyph_name
        tounicode[byte_code] = cp
        unicode_to_code[cp] = byte_code
        parts["widths"][byte_code - parts["first_char"]] = advance
        mappings_changed = True
        added_chars.append(char)
        added_codes[char] = byte_code

    if cff_changed:
        buffer = io.BytesIO()
        cff_set.compile(buffer, ot_font)
        doc.update_stream(parts["fontfile_xref"], buffer.getvalue(), compress=True)

    if mappings_changed:
        differences = " ".join(
            f"{code} /{encoding[code]}" for code in sorted(encoding)
        )
        doc.update_object(
            parts["encoding_xref"],
            f"<< /Type /Encoding /Differences [ {differences} ] >>",
        )
        doc.update_stream(
            parts["tounicode_xref"],
            _build_tounicode(tounicode),
            compress=True,
        )
        widths_value = " ".join(_format_width(value) for value in parts["widths"])
        doc.xref_set_key(font_xref, "FirstChar", str(parts["first_char"]))
        doc.xref_set_key(font_xref, "LastChar", str(parts["last_char"]))
        doc.xref_set_key(font_xref, "Widths", f"[ {widths_value} ]")

    try:
        encoded = bytes(unicode_to_code[ord(char)] for char in text)
    except KeyError as exc:
        missing = chr(exc.args[0]) if exc.args else "?"
        raise CffSubsetExtensionError(
            f"Could not assign a byte code to {missing!r}."
        ) from exc

    return ExtensionResult(
        refname=str(candidate.pdf_refname).lstrip("/"),
        encoded=encoded,
        added_chars=added_chars,
        added_codes=added_codes,
    )


def append_raw_text_stream(
    page,
    refname: str,
    encoded: bytes,
    origin: Tuple[float, float],
    size: float,
    color: int,
    rotation: int = 0,
):
    """Append a Tj operation using the existing simple-font resource."""
    if not encoded:
        return

    rotation = int(rotation or 0) % 360
    matrices = {
        0: (1, 0, 0, 1),
        90: (0, 1, -1, 0),
        180: (-1, 0, 0, -1),
        270: (0, -1, 1, 0),
    }
    if rotation not in matrices:
        raise CffSubsetExtensionError(
            f"Raw original-font insertion supports 0/90/180/270 degree text, not {rotation}."
        )

    point = pymupdf.Point(*origin) * ~page.transformation_matrix
    a, b, c, d = matrices[rotation]

    red = ((int(color) >> 16) & 255) / 255.0
    green = ((int(color) >> 8) & 255) / 255.0
    blue = (int(color) & 255) / 255.0

    content = (
        "q\n"
        "BT\n"
        f"/{str(refname).lstrip('/')} {float(size):.8f} Tf\n"
        f"{red:.8f} {green:.8f} {blue:.8f} rg\n"
        f"{a} {b} {c} {d} {point.x:.8f} {point.y:.8f} Tm\n"
        f"<{encoded.hex().upper()}> Tj\n"
        "ET\n"
        "Q\n"
    ).encode("ascii")

    doc = page.parent
    previous = list(page.get_contents())
    stream_xref = doc.get_new_xref()
    doc.update_object(stream_xref, "<<>>")
    doc.update_stream(stream_xref, content, compress=True)

    refs = " ".join(f"{xref} 0 R" for xref in previous + [stream_xref])
    doc.xref_set_key(page.xref, "Contents", f"[ {refs} ]")


def inspect_existing_cff(doc, font_xref: int, text: str):
    """Return (can_encode, missing_chars, unicode_widths) without mutating the PDF.

    A character is considered available if the original CFF already contains
    the corresponding glyph name, even when the existing ToUnicode map is wrong;
    a fresh unused byte code can be assigned later without changing the glyph.
    """
    try:
        parts = _font_components(doc, int(font_xref))
        encoding = _parse_differences(
            doc.xref_object(parts["encoding_xref"], compressed=False)
        )
        cff_bytes = doc.xref_stream(parts["fontfile_xref"])
        ot_font = TTFont(recalcBBoxes=False, recalcTimestamp=False)
        cff_set = CFFFontSet()
        cff_set.decompile(io.BytesIO(cff_bytes), ot_font)
        top = cff_set[cff_set.fontNames[0]]
        charset = set(top.CharStrings.charStrings)

        name_to_width = {}
        for code, name in encoding.items():
            if parts["first_char"] <= code <= parts["last_char"]:
                index = code - parts["first_char"]
                if 0 <= index < len(parts["widths"]):
                    name_to_width.setdefault(name, float(parts["widths"][index]))

        missing = []
        widths = {}
        for char in dict.fromkeys(text):
            if char in ("\n", "\r"):
                missing.append(char)
                continue
            name = _existing_glyph_name(top.CharStrings, ord(char))
            if name is None or name not in charset:
                missing.append(char)
                continue
            if name in name_to_width:
                widths[char] = name_to_width[name] / 1000.0
            else:
                intrinsic_width = _cff_charstring_width(top, name)
                if intrinsic_width is None:
                    missing.append(char)
                    continue
                widths[char] = intrinsic_width / 1000.0

        return (not missing), missing, widths
    except Exception:
        return False, list(dict.fromkeys(text)), {}


def extension_unicode_widths(doc, candidate, text: str) -> Dict[str, float]:
    """Measure a pending subset extension without mutating the PDF.

    Existing characters use the original PDF font's widths. Only characters
    absent from that subset use the donor font's advance. Values are returned
    in em units for ``PdfEngine.replacement_width``.
    """
    if not getattr(candidate, "pdf_xref", None):
        raise CffSubsetExtensionError("Extension candidate has no PDF font xref.")

    _can_encode, _missing, widths = inspect_existing_cff(
        doc,
        int(candidate.pdf_xref),
        text,
    )
    widths = dict(widths or {})
    needed = [
        char
        for char in dict.fromkeys(text)
        if char not in ("\n", "\r") and char not in widths
    ]
    if not needed:
        return widths

    adaptive_plan = getattr(candidate, "adaptive_glyph_plan", None) or {}
    if adaptive_plan:
        if int(adaptive_plan.get("target_font_xref", -1)) != int(candidate.pdf_xref):
            raise CffSubsetExtensionError(
                "Adaptive glyph plan does not belong to the selected PDF font resource."
            )
        adaptive_widths = adaptive_plan.get("unicode_widths", {})
        unavailable = [char for char in needed if char not in adaptive_widths]
        if unavailable:
            raise CffSubsetExtensionError(
                "Adaptive glyph plan has no measured width for "
                + " ".join(repr(char) for char in unavailable)
            )
        for char in needed:
            widths[char] = float(adaptive_widths[char])
        return widths

    full_font = _full_font_source(candidate)
    try:
        cmap = full_font.getBestCmap() or {}
        hmtx = full_font["hmtx"].metrics
        upem = float(full_font["head"].unitsPerEm)
        for char in needed:
            glyph_name = cmap.get(ord(char))
            if glyph_name is None:
                raise CffSubsetExtensionError(
                    f"Verified donor font does not contain {char!r}."
                )
            widths[char] = float(hmtx[glyph_name][0]) / upem
    finally:
        full_font.close()

    return widths
