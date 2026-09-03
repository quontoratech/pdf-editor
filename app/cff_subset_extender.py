"""Extend an existing single-byte Type1/CFF PDF font resource in-place.

This module is intentionally conservative. It is used only when:
- the selected PDF font is an embedded Type1C/CFF simple font,
- an exact full TTF/OTF source has already been verified by the resolver, and
- the original PDF subset lacks one or more characters required by new text.

Instead of adding a new Type0/CID font resource, we append only the missing
glyphs to the original CFF program, add unused byte codes to the existing PDF
Encoding / ToUnicode maps, and then write text with the original /F# resource.
Existing byte codes and widths are preserved, so untouched text using the same
font xref keeps its original layout.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from typing import Dict, Iterable, Tuple

import pymupdf
from fontTools.cffLib import CFFFontSet
from fontTools.pens.t2CharStringPen import T2CharStringPen
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
    return common.get(name)


def _glyph_name(codepoint: int) -> str:
    if codepoint == 0x20:
        return "space"
    if codepoint <= 0xFFFF:
        return f"uni{codepoint:04X}"
    return f"u{codepoint:06X}"


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

    full_font = None
    cmap = {}
    glyph_set = None
    upem = 1000.0
    hmtx = {}

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
        pdf_glyph_name = _glyph_name(cp)

        if pdf_glyph_name in top.CharStrings.charStrings:
            advance = existing_name_width.get(pdf_glyph_name)
            if advance is None:
                # Rare case: a glyph is present in the CFF charset but had no
                # byte code in the PDF Encoding. Use the verified full font only
                # to obtain its advance width if available.
                try:
                    if full_font is None:
                        full_font = _full_font_source(candidate)
                        cmap = full_font.getBestCmap() or {}
                        glyph_set = full_font.getGlyphSet()
                        upem = float(full_font["head"].unitsPerEm)
                        hmtx = full_font["hmtx"].metrics
                    source_glyph = cmap.get(cp)
                    if source_glyph is None:
                        raise KeyError(cp)
                    advance = float(hmtx[source_glyph][0]) * 1000.0 / upem
                except Exception as exc:
                    raise CffSubsetExtensionError(
                        f"Could not determine the width for existing glyph {char!r}."
                    ) from exc
        else:
            # A genuinely missing glyph requires the exact verified full font.
            if full_font is None:
                full_font = _full_font_source(candidate)
                cmap = full_font.getBestCmap() or {}
                glyph_set = full_font.getGlyphSet()
                upem = float(full_font["head"].unitsPerEm)
                hmtx = full_font["hmtx"].metrics

            source_glyph = cmap.get(cp)
            if source_glyph is None:
                raise CffSubsetExtensionError(
                    f"Verified full font does not contain {char!r} (U+{cp:04X})."
                )
            advance = float(hmtx[source_glyph][0]) * 1000.0 / upem
            pen = T2CharStringPen(advance, glyph_set)
            glyph_set[source_glyph].draw(pen)
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
            name = _glyph_name(ord(char))
            if name not in charset:
                missing.append(char)
                continue
            if name in name_to_width:
                widths[char] = name_to_width[name] / 1000.0

        return (not missing), missing, widths
    except Exception:
        return False, list(dict.fromkeys(text)), {}
