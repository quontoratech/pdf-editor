from __future__ import annotations

from io import BytesIO

import pymupdf
from fontTools.pens.basePen import BasePen
from fontTools.ttLib import TTFont

from .font_utils import rgb_from_int


class GlyphOutlineError(ValueError):
    pass


class _PdfShapePen(BasePen):
    """Translate fontTools contours into one filled PyMuPDF shape."""

    def __init__(self, glyph_set, shape, transform):
        super().__init__(glyph_set)
        self.shape = shape
        self.transform = transform
        self.current = None
        self.start = None

    def _point(self, value):
        x, y = self.transform(float(value[0]), float(value[1]))
        return pymupdf.Point(x, y)

    def _moveTo(self, point):
        self.current = self._point(point)
        self.start = self.current

    def _lineTo(self, point):
        end = self._point(point)
        if self.current is not None:
            self.shape.draw_line(self.current, end)
        self.current = end

    def _curveToOne(self, point1, point2, point3):
        end = self._point(point3)
        if self.current is not None:
            self.shape.draw_bezier(
                self.current,
                self._point(point1),
                self._point(point2),
                end,
            )
        self.current = end

    def _qCurveToOne(self, point1, point2):
        end = self._point(point2)
        if self.current is None:
            self.current = end
            return

        control = self._point(point1)
        control1 = pymupdf.Point(
            self.current.x + (2.0 / 3.0) * (control.x - self.current.x),
            self.current.y + (2.0 / 3.0) * (control.y - self.current.y),
        )
        control2 = pymupdf.Point(
            end.x + (2.0 / 3.0) * (control.x - end.x),
            end.y + (2.0 / 3.0) * (control.y - end.y),
        )
        self.shape.draw_bezier(self.current, control1, control2, end)
        self.current = end

    def _closePath(self):
        if self.current is not None and self.start is not None:
            if abs(self.current.x - self.start.x) > 1e-6 or abs(self.current.y - self.start.y) > 1e-6:
                self.shape.draw_line(self.current, self.start)
        self.current = None
        self.start = None

    def _endPath(self):
        self.current = None
        self.start = None


def _open_font(candidate) -> TTFont:
    try:
        if getattr(candidate, "embedded_bytes", None):
            return TTFont(BytesIO(candidate.embedded_bytes), lazy=False)
        if getattr(candidate, "path", None):
            return TTFont(
                str(candidate.path),
                fontNumber=int(getattr(candidate, "font_number", 0) or 0),
                lazy=False,
            )
    except Exception as exc:
        raise GlyphOutlineError(f"Could not open the outline font: {exc}") from exc
    raise GlyphOutlineError("The selected outline source has no usable TTF/OTF program.")


def draw_text_as_outline(
    page,
    text: str,
    candidate,
    *,
    origin,
    size: float,
    color: int,
    alpha: int = 255,
    direction=(1.0, 0.0),
):
    """Draw one line of Unicode text as filled vector glyph contours.

    The returned rectangle is in PyMuPDF page coordinates. No font resource or
    searchable text operator is added to the PDF.
    """
    if not text:
        point = pymupdf.Point(float(origin[0]), float(origin[1]))
        return pymupdf.Rect(point, point)
    if "\n" in text or "\r" in text:
        raise GlyphOutlineError("Outline fallback currently supports one visual line only.")

    font = _open_font(candidate)
    try:
        cmap = font.getBestCmap() or {}
        missing = []
        for char in text:
            if char.isspace():
                continue
            if ord(char) not in cmap and char not in missing:
                missing.append(char)
        if missing:
            preview = " ".join(repr(char) for char in missing[:12])
            raise GlyphOutlineError(f"Outline source is missing glyphs: {preview}")

        units_per_em = float(font["head"].unitsPerEm)
        hmtx = font["hmtx"].metrics
        glyph_set = font.getGlyphSet()
        scale = float(size) / units_per_em
        base_x, base_y = float(origin[0]), float(origin[1])
        direction_x, direction_y = map(float, direction or (1.0, 0.0))
        magnitude = (direction_x * direction_x + direction_y * direction_y) ** 0.5
        if magnitude <= 1e-9:
            direction_x, direction_y, magnitude = 1.0, 0.0, 1.0
        direction_x /= magnitude
        direction_y /= magnitude
        up_x, up_y = direction_y, -direction_x

        shape = page.new_shape()
        advance_units = 0.0

        for char in text:
            glyph_name = cmap.get(ord(char))
            if glyph_name is None:
                # Whitespace without a cmap entry uses the font's space width.
                glyph_name = cmap.get(32)
            if glyph_name is None:
                advance_units += units_per_em * 0.25
                continue

            glyph_origin = advance_units

            def transform(x_value, y_value):
                along = (glyph_origin + x_value) * scale
                upward = y_value * scale
                return (
                    base_x + direction_x * along + up_x * upward,
                    base_y + direction_y * along + up_y * upward,
                )

            if not char.isspace():
                glyph_set[glyph_name].draw(
                    _PdfShapePen(glyph_set, shape, transform)
                )

            advance_units += float(hmtx.get(glyph_name, (units_per_em, 0))[0])

        if shape.rect is None or shape.rect.is_empty:
            raise GlyphOutlineError("The outline source produced no visible vector paths.")

        shape.finish(
            width=0,
            color=None,
            fill=rgb_from_int(int(color)),
            even_odd=True,
            closePath=False,
            fill_opacity=max(0.0, min(1.0, int(alpha) / 255.0)),
        )
        outline_rect = pymupdf.Rect(shape.rect)
        shape.commit(overlay=True)
        return outline_rect
    finally:
        font.close()
