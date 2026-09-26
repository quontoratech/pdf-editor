from dataclasses import dataclass, field
from typing import Optional, Tuple


@dataclass
class TextSpan:
    text: str
    bbox: Tuple[float, float, float, float]
    origin: Tuple[float, float]
    font: str
    size: float
    flags: int
    color: int
    alpha: int = 255
    direction: Tuple[float, float] = (1.0, 0.0)
    block_index: int = -1
    line_index: int = -1
    span_index: int = -1


@dataclass
class DocxFontReference:
    matched_text: str
    family: Optional[str]
    size_pt: Optional[float]
    bold: Optional[bool]
    italic: Optional[bool]
    source: str = "DOCX"
    confidence: str = "unknown"
    page_number: Optional[int] = None
    x_pt: Optional[float] = None
    y_pt: Optional[float] = None
    score: Optional[float] = None
    match_reason: str = ""
    declared_family: Optional[str] = None


@dataclass
class FontCandidate:
    path: Optional[str]
    font_number: int = 0
    family: Optional[str] = None
    subfamily: Optional[str] = None
    full_name: Optional[str] = None
    postscript: Optional[str] = None
    weight: Optional[int] = None
    italic: bool = False
    source: str = "system"
    embedded_bytes: Optional[bytes] = field(default=None, repr=False)
    base14_name: Optional[str] = None

    # Re-use an existing font resource already present on the PDF page.
    # PyMuPDF accepts these resources by their symbolic name prefixed with "/".
    # This is especially useful for valid non-embedded fonts, where
    # Document.extract_font() correctly returns no font program bytes.
    pdf_refname: Optional[str] = None
    pdf_xref: Optional[int] = None
    pdf_char_widths: Optional[list] = field(default=None, repr=False)
    pdf_unicode_widths: Optional[dict] = field(default=None, repr=False)

    # V3.12: when an exact full font is available but the original PDF uses a
    # single-byte Type1C/CFF subset, extend that original font object in-place
    # instead of adding a new Type0/CID font resource.
    extend_original_cff: bool = False
    subset_extension_font_path: Optional[str] = None
    subset_extension_font_bytes: Optional[bytes] = field(default=None, repr=False)

    # Explicit same-family composite assembled from verified PDF resources.
    # Each replacement character maps to the resource that visibly rendered it.
    glyph_candidates: Optional[dict[str, "FontCandidate"]] = field(
        default=None,
        repr=False,
    )

    @property
    def display_name(self) -> str:
        return (
            self.full_name
            or self.postscript
            or " ".join(x for x in (self.family, self.subfamily) if x)
            or "Unknown font"
        )


@dataclass
class FontResolution:
    ok: bool
    candidate: Optional[FontCandidate] = None
    reason: str = ""
    exact_family: bool = False
    exact_weight: bool = False
    exact_style: bool = False
    glyphs_ok: bool = False
    source_note: str = ""
    missing_chars: list[str] = field(default_factory=list)
