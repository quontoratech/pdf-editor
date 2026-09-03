import re
from io import BytesIO
from pathlib import Path
from typing import Optional

import pymupdf
from fontTools.ttLib import TTFont


BASE14_MAP = {
    "helvetica": "helv",
    "helveticabold": "hebo",
    "helveticaoblique": "heit",
    "helveticaboldoblique": "hebi",
    "timesroman": "tiro",
    "timesbold": "tibo",
    "timesitalic": "tiit",
    "timesbolditalic": "tibi",
    "courier": "cour",
    "courierbold": "cobo",
    "courieroblique": "coit",
    "courierboldoblique": "cobi",
    "symbol": "symb",
    "zapfdingbats": "zapfdingbats",
}


def strip_subset_prefix(name: str) -> str:
    if not name:
        return ""
    value = name.strip().lstrip("/")
    if "+" in value:
        prefix, rest = value.split("+", 1)
        if 1 <= len(prefix) <= 8 and prefix.isalpha():
            return rest
    return value


def normalize_font_name(name: Optional[str]) -> str:
    if not name:
        return ""
    value = strip_subset_prefix(str(name))
    return "".join(ch.lower() for ch in value if ch.isalnum())


def normalize_text(value: str) -> str:
    return " ".join((value or "").split()).strip().casefold()


def weight_from_flags(flags: int) -> int:
    return 700 if flags & 16 else 400


def italic_from_flags(flags: int) -> bool:
    return bool(flags & 2)


def font_flags_text(flags: int) -> str:
    parts = []
    if flags & 16:
        parts.append("Bold")
    if flags & 2:
        parts.append("Italic")
    if flags & 4:
        parts.append("Serif")
    if flags & 8:
        parts.append("Monospace")
    return ", ".join(parts) if parts else "Regular"


def weight_label(weight: Optional[int]) -> str:
    if weight is None:
        return "Unknown"

    mapping = [
        (150, "Thin"),
        (250, "Extra Light"),
        (350, "Light"),
        (450, "Regular"),
        (550, "Medium"),
        (650, "Semi Bold"),
        (750, "Bold"),
        (850, "Extra Bold"),
        (950, "Black"),
    ]

    for upper, label in mapping:
        if weight < upper:
            return f"{weight} ({label})"
    return f"{weight} (Black)"


def get_name_record(font: TTFont, ids: list[int]) -> str:
    if "name" not in font:
        return ""

    for name_id in ids:
        for record in font["name"].names:
            if record.nameID != name_id:
                continue
            try:
                value = record.toUnicode().strip()
            except Exception:
                continue
            if value:
                return value
    return ""


def read_ttfont_metadata(font: TTFont) -> dict:
    family = get_name_record(font, [16, 1])
    subfamily = get_name_record(font, [17, 2])
    full_name = get_name_record(font, [4])
    postscript = get_name_record(font, [6])

    weight = None
    if "OS/2" in font:
        try:
            weight = int(font["OS/2"].usWeightClass)
        except Exception:
            pass

    italic = False
    if "head" in font:
        try:
            italic = bool(font["head"].macStyle & 0x02)
        except Exception:
            pass

    if not italic and subfamily:
        italic = "italic" in subfamily.casefold() or "oblique" in subfamily.casefold()

    return {
        "family": family or None,
        "subfamily": subfamily or None,
        "full_name": full_name or None,
        "postscript": postscript or None,
        "weight": weight,
        "italic": italic,
    }


def read_font_metadata_from_path(path: str, font_number: int = 0) -> dict:
    try:
        font = TTFont(path, fontNumber=font_number, lazy=True)
        result = read_ttfont_metadata(font)
        font.close()
        return result
    except Exception:
        return {}


def read_font_metadata_from_bytes(data: bytes) -> dict:
    try:
        font = TTFont(BytesIO(data), lazy=True)
        result = read_ttfont_metadata(font)
        font.close()
        return result
    except Exception:
        return {}


def rgb_from_int(color_value: int):
    try:
        return pymupdf.sRGB_to_pdf(color_value)
    except Exception:
        r = (color_value >> 16) & 255
        g = (color_value >> 8) & 255
        b = color_value & 255
        return r / 255.0, g / 255.0, b / 255.0


def rotation_from_direction(direction):
    if not direction or len(direction) < 2:
        return 0

    x, y = float(direction[0]), float(direction[1])
    candidates = [
        (0, (1.0, 0.0)),
        (90, (0.0, -1.0)),
        (180, (-1.0, 0.0)),
        (270, (0.0, 1.0)),
    ]

    best_rotation = 0
    best_distance = 999.0

    for rotation, (cx, cy) in candidates:
        distance = (x - cx) ** 2 + (y - cy) ** 2
        if distance < best_distance:
            best_distance = distance
            best_rotation = rotation

    return best_rotation if best_distance < 0.08 else 0


def font_bytes_have_text(data: bytes, text: str):
    try:
        font = pymupdf.Font(fontbuffer=data)
    except Exception:
        return False, ["<font load failed>"]

    missing = []
    seen = set()

    for char in text:
        if char.isspace() or char in "\r\n\t":
            continue

        codepoint = ord(char)
        if codepoint in seen:
            continue
        seen.add(codepoint)

        try:
            glyph = font.has_glyph(codepoint, fallback=False)
        except TypeError:
            glyph = font.has_glyph(codepoint)

        if not glyph:
            missing.append(char)

    return not missing, missing


def font_path_has_text(path: str, text: str):
    try:
        data = Path(path).read_bytes()
    except Exception:
        return False, ["<font file unreadable>"]
    return font_bytes_have_text(data, text)


def font_text_width_from_bytes(data: bytes, text: str, size: float):
    try:
        font = pymupdf.Font(fontbuffer=data)
        return font.text_length(text, fontsize=size)
    except Exception:
        return None


def font_text_width_from_path(path: str, text: str, size: float):
    try:
        font = pymupdf.Font(fontfile=path)
        return font.text_length(text, fontsize=size)
    except Exception:
        return None


def safe_file_component(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]+", "_", value or "font")
    return value[:100] or "font"
