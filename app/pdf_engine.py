import hashlib
import math
import re
from pathlib import Path
from statistics import median

import pymupdf

from .font_utils import (
    rgb_from_int,
    rotation_from_direction,
    font_text_width_from_bytes,
    font_text_width_from_path,
)
from .models import TextSpan, FontCandidate
from .cff_subset_extender import (
    CffSubsetExtensionError,
    append_raw_text_stream,
    extend_and_encode,
)
from .glyph_outline import draw_text_as_outline


class PdfEngine:
    def __init__(self):
        try:
            pymupdf.TOOLS.set_small_glyph_heights(True)
        except Exception:
            pass

    def extract_spans(self, page):
        spans = []
        data = page.get_text("dict")

        for block_index, block in enumerate(data.get("blocks", [])):
            if block.get("type", 0) != 0:
                continue

            for line_index, line in enumerate(block.get("lines", [])):
                direction = tuple(line.get("dir", (1.0, 0.0)))

                for span_index, span in enumerate(line.get("spans", [])):
                    text = span.get("text", "")
                    if not text.strip():
                        continue

                    bbox = tuple(span.get("bbox", (0, 0, 0, 0)))
                    origin = tuple(
                        span.get(
                            "origin",
                            (bbox[0], bbox[3]),
                        )
                    )

                    spans.append(
                        TextSpan(
                            text=text,
                            bbox=bbox,
                            origin=origin,
                            font=span.get("font", ""),
                            size=float(span.get("size", 11.0)),
                            flags=int(span.get("flags", 0)),
                            color=int(span.get("color", 0)),
                            alpha=int(span.get("alpha", 255)),
                            direction=direction,
                            block_index=block_index,
                            line_index=line_index,
                            span_index=span_index,
                        )
                    )

        return spans

    def replacement_width(self, candidate: FontCandidate, text: str, size: float):
        if not text:
            return 0.0

        if candidate.glyph_candidates:
            total = 0.0
            previous = None
            for char in text:
                subcandidate = candidate.glyph_candidates.get(char)
                if subcandidate is None and char.isspace():
                    subcandidate = previous or next(
                        iter(candidate.glyph_candidates.values()),
                        None,
                    )
                if subcandidate is None:
                    return None
                width = self.replacement_width(subcandidate, char, size)
                if width is None:
                    return None
                total += float(width)
                previous = subcandidate
            return total

        if candidate.pdf_refname and candidate.pdf_unicode_widths is not None:
            try:
                total = 0.0
                for ch in text:
                    if ch in ("\n", "\r"):
                        return None
                    if ch in candidate.pdf_unicode_widths:
                        total += float(candidate.pdf_unicode_widths[ch])
                        continue

                    # A verified Type1C/CFF subset-extension candidate may have
                    # been prepared before another edit updated the shared font
                    # object.  In that case its cached PDF-width map can be
                    # incomplete even though the exact donor font was already
                    # verified to contain the character.  Measure only that
                    # missing character from the same donor; existing characters
                    # continue to use the original PDF widths above.
                    if not candidate.extend_original_cff:
                        return None

                    donor_width = None
                    donor_bytes = candidate.subset_extension_font_bytes
                    donor_path = candidate.subset_extension_font_path or candidate.path
                    if donor_bytes:
                        donor_width = font_text_width_from_bytes(
                            donor_bytes,
                            ch,
                            1.0,
                        )
                    elif donor_path:
                        donor_width = font_text_width_from_path(
                            donor_path,
                            ch,
                            1.0,
                        )
                    if donor_width is None:
                        return None
                    total += float(donor_width)
                return total * float(size)
            except Exception:
                return None

        if candidate.pdf_refname and candidate.pdf_char_widths:
            try:
                total = 0.0
                for ch in text:
                    if ch in ("\n", "\r"):
                        return None
                    code = ord(ch)
                    if code >= len(candidate.pdf_char_widths):
                        return None
                    item = candidate.pdf_char_widths[code]
                    width = float(item[1])
                    total += width
                return total * float(size)
            except Exception:
                return None

        if candidate.embedded_bytes:
            return font_text_width_from_bytes(
                candidate.embedded_bytes,
                text,
                size,
            )

        if candidate.path:
            return font_text_width_from_path(
                candidate.path,
                text,
                size,
            )

        if candidate.base14_name:
            try:
                return pymupdf.get_text_length(
                    text,
                    fontname=candidate.base14_name,
                    fontsize=size,
                )
            except Exception:
                return None

        return None

    @staticmethod
    def _same_span(a: TextSpan, b: TextSpan):
        if a is b:
            return True

        return (
            a.text == b.text
            and all(abs(x - y) < 0.05 for x, y in zip(a.bbox, b.bbox))
            and all(abs(x - y) < 0.05 for x, y in zip(a.origin, b.origin))
        )

    @staticmethod
    def _same_line(a: TextSpan, b: TextSpan):
        baseline_tolerance = max(1.25, min(a.size, b.size) * 0.32)

        if abs(a.origin[1] - b.origin[1]) <= baseline_tolerance:
            return True

        ay0, ay1 = a.bbox[1], a.bbox[3]
        by0, by1 = b.bbox[1], b.bbox[3]
        overlap = max(0.0, min(ay1, by1) - max(ay0, by0))
        min_height = max(0.1, min(ay1 - ay0, by1 - by0))

        return overlap / min_height >= 0.65


    @staticmethod
    def _drawing_line_cap(value):
        """Return the single line-cap value accepted by ``Page.draw_line``."""
        if isinstance(value, (tuple, list)):
            value = value[0] if value else 0
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    def horizontal_table_rules(self, page):
        """
        Return horizontal rules that can be edited without touching other art.

        A rule is considered safe only when it is a *single stroked path* made
        from one horizontal line command.  Redacting that path therefore cannot
        remove a border, logo, or other drawing that merely shares its content
        stream.  More complicated paths are intentionally ignored.
        """
        if page is None:
            return []

        try:
            drawings = page.get_drawings(extended=True)
        except TypeError:
            drawings = page.get_drawings()
        except Exception:
            return []

        rules = []
        active_clips = {}

        for drawing_index, drawing in enumerate(drawings or []):
            drawing_type = str(drawing.get("type") or "")
            try:
                level = max(0, int(drawing.get("level") or 0))
            except (TypeError, ValueError):
                level = 0

            # Extended drawing output is hierarchical. A new item at a lower
            # level exits clips from the previous branch.
            for stale_level in [key for key in active_clips if key >= level]:
                del active_clips[stale_level]

            if drawing_type == "clip":
                scissor = drawing.get("scissor")
                if scissor is not None:
                    try:
                        active_clips[level] = pymupdf.Rect(scissor)
                    except Exception:
                        pass
                continue

            # Nested Form XObjects created by the exact text-copy reflow carry
            # a full source-page display list behind a small clip. Even when a
            # renderer hides those paths, redacting them can rewrite the whole
            # form. Only native / top-level paths are safe rule-edit targets.
            if level > 1:
                continue

            items = list(drawing.get("items") or [])

            # ``s`` is stroke-only and ``fs`` is fill + stroke. A one-command
            # fill + stroke path is unusual but still safe when it is a line.
            if "s" not in drawing_type or len(items) != 1:
                continue

            item = items[0]
            if not item or item[0] != "l" or len(item) < 3:
                continue

            try:
                p1 = pymupdf.Point(item[1])
                p2 = pymupdf.Point(item[2])
                stroke_width = max(0.01, float(drawing.get("width") or 0.0))
            except Exception:
                continue

            horizontal_tolerance = max(0.20, stroke_width * 0.35)
            if abs(float(p1.y) - float(p2.y)) > horizontal_tolerance:
                continue

            x0 = min(float(p1.x), float(p2.x))
            x1 = max(float(p1.x), float(p2.x))
            y = (float(p1.y) + float(p2.y)) / 2.0

            # ``show_pdf_page`` embeds a complete source page behind a tiny
            # clip. ``get_drawings(extended=True)`` reports the hidden paths as
            # well. Ignore any path outside, or materially truncated by, its
            # effective clip so those invisible copies cannot be edited later.
            if active_clips:
                effective_clip = None
                for clip_level, clip_rect in sorted(active_clips.items()):
                    if clip_level >= level:
                        continue
                    if effective_clip is None:
                        effective_clip = pymupdf.Rect(clip_rect)
                    else:
                        effective_clip &= clip_rect

                if effective_clip is not None:
                    y_pad = max(0.20, stroke_width * 0.75)
                    if not (
                        effective_clip.y0 - y_pad
                        <= y
                        <= effective_clip.y1 + y_pad
                    ):
                        continue

                    visible_x0 = max(x0, float(effective_clip.x0))
                    visible_x1 = min(x1, float(effective_clip.x1))
                    if visible_x1 - visible_x0 < (x1 - x0) - 0.50:
                        continue

            # Tiny strokes are much more likely to be decoration / glyph art
            # than a table boundary.
            if x1 - x0 < 24.0:
                continue

            color = drawing.get("color")
            if color is None:
                continue

            try:
                color = tuple(float(component) for component in color)
            except Exception:
                continue

            rules.append(
                {
                    "drawing_index": int(drawing_index),
                    "seqno": int(drawing.get("seqno", -1)),
                    "x0": x0,
                    "x1": x1,
                    "y": y,
                    "width": stroke_width,
                    "color": color,
                    "dashes": drawing.get("dashes"),
                    "line_cap": self._drawing_line_cap(drawing.get("lineCap")),
                    "line_join": int(float(drawing.get("lineJoin") or 0)),
                    "stroke_opacity": float(
                        drawing.get("stroke_opacity")
                        if drawing.get("stroke_opacity") is not None
                        else 1.0
                    ),
                }
            )

        rules.sort(key=lambda rule: (rule["y"], rule["x0"], rule["x1"]))
        return rules

    def nearest_horizontal_table_rule(self, page, x, y, tolerance=6.0):
        """Return the nearest safely editable horizontal rule to a PDF point.

        ``horizontal_table_rules()`` deliberately exposes only independently
        stroked, one-command paths.  Keeping hit testing on top of that filtered
        list prevents a click from selecting one edge of a rectangle, a logo,
        or a compound table-border path whose other edges would also disappear.
        """
        try:
            x = float(x)
            y = float(y)
            tolerance = float(tolerance)
        except (TypeError, ValueError):
            raise ValueError("The table-line position must use PDF coordinates.")

        if not all(math.isfinite(value) for value in (x, y, tolerance)):
            raise ValueError("The table-line position must use finite PDF coordinates.")

        # Six PDF points is a forgiving browser-click target around a hairline
        # while remaining local enough for closely spaced double rules.
        tolerance = min(6.0, max(1.0, tolerance))
        candidates = []

        for rule in self.horizontal_table_rules(page):
            x0 = float(rule["x0"])
            x1 = float(rule["x1"])
            rule_y = float(rule["y"])
            stroke_width = max(0.01, float(rule.get("width") or 0.0))
            vertical_distance = abs(y - rule_y)
            vertical_tolerance = max(tolerance, stroke_width / 2.0 + 2.0)

            if x < x0:
                horizontal_distance = x0 - x
            elif x > x1:
                horizontal_distance = x - x1
            else:
                horizontal_distance = 0.0

            if (
                vertical_distance > vertical_tolerance
                or horizontal_distance > tolerance
            ):
                continue

            distance = math.hypot(horizontal_distance, vertical_distance)
            candidate = dict(rule)
            candidate["hit_distance"] = float(distance)
            candidates.append(
                (
                    float(distance),
                    float(vertical_distance),
                    float(horizontal_distance),
                    -float(x1 - x0),
                    candidate,
                )
            )

        if not candidates:
            return None

        candidates.sort(key=lambda item: item[:-1])

        # Do not make an arbitrary destructive choice halfway between two
        # neighbouring rules. The UI can ask the user to click closer to one.
        if len(candidates) > 1:
            first = candidates[0]
            second = candidates[1]
            if (
                abs(float(first[0]) - float(second[0])) <= 0.25
                and abs(float(first[1]) - float(second[1])) <= 0.25
            ):
                raise ValueError(
                    "More than one horizontal table line is equally close. "
                    "Click closer to the line you want to remove."
                )

        return candidates[0][-1]

    @staticmethod
    def _horizontal_rule_redaction_rect(rule, bounds, vertical_pad=None):
        """Build a rectangle that fully covers one stroked rule path."""
        stroke_width = max(0.01, float(rule.get("width") or 0.0))
        endpoint_pad = max(0.35, stroke_width * 1.5)
        if vertical_pad is None:
            vertical_pad = max(0.35, stroke_width * 1.5)
        else:
            vertical_pad = max(0.05, float(vertical_pad))
        rect = pymupdf.Rect(
            float(rule["x0"]) - endpoint_pad,
            float(rule["y"]) - vertical_pad,
            float(rule["x1"]) + endpoint_pad,
            float(rule["y"]) + vertical_pad,
        ) & pymupdf.Rect(bounds)
        rect.normalize()
        return rect

    @staticmethod
    def _same_horizontal_rule_geometry(observed, expected):
        # Serialization retains path coordinates far more accurately than a
        # quarter point. Keep this tighter than a normal double-rule gap so a
        # surviving neighbour cannot masquerade as the deleted target.
        tolerance = max(
            0.08,
            min(0.25, float(expected.get("width") or 0.0) * 0.50),
        )
        return (
            abs(float(observed["y"]) - float(expected["y"])) <= tolerance
            and abs(float(observed["x0"]) - float(expected["x0"])) <= 0.75
            and abs(float(observed["x1"]) - float(expected["x1"])) <= 0.75
        )

    @classmethod
    def _same_horizontal_rule_signature(cls, observed, expected):
        """Compare geometry and stroke style for an untouched neighbour."""
        if not cls._same_horizontal_rule_geometry(observed, expected):
            return False

        expected_color = tuple(float(value) for value in (expected.get("color") or ()))
        observed_color = tuple(float(value) for value in (observed.get("color") or ()))
        if len(expected_color) != len(observed_color):
            return False
        if any(
            abs(left - right) > 0.0005
            for left, right in zip(expected_color, observed_color)
        ):
            return False

        return (
            abs(
                float(observed.get("width") or 0.0)
                - float(expected.get("width") or 0.0)
            ) <= 0.005
            and str(observed.get("dashes") or "")
            == str(expected.get("dashes") or "")
            and int(observed.get("line_cap") or 0)
            == int(expected.get("line_cap") or 0)
            and int(observed.get("line_join") or 0)
            == int(expected.get("line_join") or 0)
            and abs(
                float(observed.get("stroke_opacity", 1.0))
                - float(expected.get("stroke_opacity", 1.0))
            ) <= 0.0005
        )

    def delete_horizontal_table_rule_at(self, page, x, y, tolerance=6.0):
        """Delete one standalone horizontal table rule selected by PDF point.

        The operation removes graphics only: text and images are explicitly
        excluded from the redaction.  A rule is eligible only when
        :meth:`horizontal_table_rules` proves it is an independently stroked,
        single-command path.  The edited document is then serialized and
        reopened before success is reported.
        """
        if page is None:
            raise ValueError("The PDF page is no longer available.")

        doc = page.parent
        if doc is None:
            raise ValueError("The edited page is no longer attached to a PDF.")

        rule = self.nearest_horizontal_table_rule(
            page,
            x,
            y,
            tolerance=tolerance,
        )
        if rule is None:
            try:
                shown_tolerance = min(6.0, max(1.0, float(tolerance)))
            except (TypeError, ValueError):
                shown_tolerance = 6.0
            raise ValueError(
                "No independently stroked horizontal table line was found "
                f"within {shown_tolerance:.1f} PDF points of that position."
            )

        page_number = int(page.number)
        rules_before = self.horizontal_table_rules(page)
        target_index = int(rule.get("drawing_index", -1))
        other_rules = [
            item
            for item in rules_before
            if int(item.get("drawing_index", -1)) != target_index
        ]

        # Keep the graphics-only redaction band strictly inside the midpoint to
        # the nearest overlapping horizontal rule. If the target stroke itself
        # cannot fit in that isolated band, refusing is safer than erasing both.
        overlapping_rule_gaps = []
        for other in other_rules:
            horizontal_overlap = max(
                0.0,
                min(float(rule["x1"]), float(other["x1"]))
                - max(float(rule["x0"]), float(other["x0"])),
            )
            if horizontal_overlap <= 0.0:
                continue
            overlapping_rule_gaps.append(
                abs(float(other["y"]) - float(rule["y"]))
            )

        stroke_width = max(0.01, float(rule.get("width") or 0.0))
        required_vertical_pad = max(0.10, stroke_width / 2.0 + 0.05)
        preferred_vertical_pad = max(0.35, stroke_width * 1.5)
        vertical_pad = preferred_vertical_pad
        if overlapping_rule_gaps:
            nearest_gap = min(overlapping_rule_gaps)
            maximum_isolated_pad = nearest_gap / 2.0 - 0.05
            if maximum_isolated_pad < required_vertical_pad:
                raise ValueError(
                    "This table line is too close to another horizontal rule "
                    "to remove either one safely."
                )
            vertical_pad = min(vertical_pad, maximum_isolated_pad)

        redaction_rect = self._horizontal_rule_redaction_rect(
            rule,
            page.rect,
            vertical_pad=vertical_pad,
        )
        if redaction_rect.is_empty:
            raise ValueError("The selected table line is outside the PDF page.")

        try:
            drawings = page.get_drawings(extended=True)
        except TypeError:
            drawings = page.get_drawings()

        # COVERED is safer than TOUCHED, but it would still remove a second tiny
        # path wholly enclosed by the same narrow strip. Refuse that ambiguous
        # case instead of changing unrelated artwork.
        for drawing_index, drawing in enumerate(drawings or []):
            if drawing_index == target_index:
                continue
            drawing_type = str(drawing.get("type") or "")
            try:
                drawing_level = max(0, int(drawing.get("level") or 0))
            except (TypeError, ValueError):
                drawing_level = 0
            if drawing_level > 1:
                continue
            if drawing_type in {"clip", "group"}:
                continue
            if not (
                "s" in drawing_type
                or "f" in drawing_type
            ):
                continue
            try:
                drawing_rect = pymupdf.Rect(drawing.get("rect"))
                drawing_rect.normalize()
            except Exception:
                continue
            if drawing_rect.is_empty:
                continue
            fully_covered = (
                float(drawing_rect.x0) >= float(redaction_rect.x0)
                and float(drawing_rect.y0) >= float(redaction_rect.y0)
                and float(drawing_rect.x1) <= float(redaction_rect.x1)
                and float(drawing_rect.y1) <= float(redaction_rect.y1)
            )
            if fully_covered:
                raise ValueError(
                    "This table line overlaps another independent graphic, so "
                    "it cannot be removed without risking that artwork."
                )

        text_before = page.get_text("text")
        words_before = [
            (
                round(float(word[0]), 3),
                round(float(word[1]), 3),
                round(float(word[2]), 3),
                round(float(word[3]), 3),
                str(word[4]),
            )
            for word in page.get_text("words")
            if len(word) >= 5
        ]
        page.add_redact_annot(
            redaction_rect,
            fill=None,
            cross_out=False,
        )
        page.apply_redactions(
            images=pymupdf.PDF_REDACT_IMAGE_NONE,
            graphics=pymupdf.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED,
            text=pymupdf.PDF_REDACT_TEXT_NONE,
        )

        # Validate the actual serialized form, not merely MuPDF's current
        # in-memory display list.
        try:
            check_raw = doc.tobytes(
                garbage=0,
                clean=False,
                deflate=False,
                no_new_id=True,
            )
        except TypeError:
            check_raw = doc.tobytes(no_new_id=True)

        probe = pymupdf.open(stream=check_raw, filetype="pdf")
        try:
            check_page = probe[page_number]
            check_rules = self.horizontal_table_rules(check_page)

            if any(
                self._same_horizontal_rule_geometry(item, rule)
                for item in check_rules
            ):
                raise ValueError(
                    "Table-line deletion failed: the selected rule remains in "
                    "the serialized PDF."
                )

            for expected in other_rules:
                if not any(
                    self._same_horizontal_rule_signature(item, expected)
                    for item in check_rules
                ):
                    raise ValueError(
                        "Table-line deletion cancelled: an unrelated horizontal "
                        "rule changed during the operation."
                    )

            text_after = check_page.get_text("text")
            words_after = [
                (
                    round(float(word[0]), 3),
                    round(float(word[1]), 3),
                    round(float(word[2]), 3),
                    round(float(word[3]), 3),
                    str(word[4]),
                )
                for word in check_page.get_text("words")
                if len(word) >= 5
            ]
            if text_after != text_before or words_after != words_before:
                raise ValueError(
                    "Table-line deletion cancelled: searchable page text changed "
                    "during the graphics-only operation."
                )
        finally:
            probe.close()

        visible_half_width = max(0.005, stroke_width / 2.0)
        bbox = [
            float(rule["x0"]),
            float(rule["y"]) - visible_half_width,
            float(rule["x1"]),
            float(rule["y"]) + visible_half_width,
        ]
        public_rule = {
            "x0": float(rule["x0"]),
            "x1": float(rule["x1"]),
            "y": float(rule["y"]),
            "width": stroke_width,
            "color": [float(value) for value in (rule.get("color") or ())],
            "dashes": rule.get("dashes"),
            "line_cap": int(rule.get("line_cap") or 0),
            "line_join": int(rule.get("line_join") or 0),
            "stroke_opacity": float(
                rule.get("stroke_opacity")
                if rule.get("stroke_opacity") is not None
                else 1.0
            ),
            "bbox": list(bbox),
        }

        return {
            "deleted": True,
            "page_index": page_number,
            "page_number": page_number + 1,
            "click": [float(x), float(y)],
            "hit_distance": float(rule.get("hit_distance") or 0.0),
            "bbox": bbox,
            "redaction_bbox": [float(value) for value in redaction_rect],
            "rule": public_rule,
            "immediate_validation_passed": True,
        }

    @staticmethod
    def _rule_spans_band(rule, x0, x1, minimum_ratio=0.78):
        band_width = max(1.0, float(x1) - float(x0))
        overlap = max(
            0.0,
            min(float(rule["x1"]), float(x1))
            - max(float(rule["x0"]), float(x0)),
        )
        # Measure against the requested row band, not the shorter object. A
        # small decorative stroke wholly inside a wide table row must never be
        # accepted merely because 100% of that tiny stroke overlaps the row.
        return overlap / band_width >= float(minimum_ratio)

    @staticmethod
    def _has_table_column_layout(spans):
        ordered = sorted(
            list(spans or []),
            key=lambda span: (float(span.bbox[0]), float(span.bbox[1])),
        )
        if len(ordered) < 2:
            return False

        typical_size = max(
            1.0,
            float(median([float(span.size) for span in ordered])),
        )
        column_gap = max(24.0, typical_size * 3.0)

        return any(
            float(current.bbox[0]) - float(previous.bbox[2]) > column_gap
            for previous, current in zip(ordered, ordered[1:])
        )

    def full_line_spans(self, all_spans, selected_span: TextSpan, page=None):
        """
        Return every extracted span that visually belongs to the selected line.

        PDFs often store a list marker ("•", "3.") and the line body in
        different text spans / blocks with slightly different baselines.
        The normal visual-line grouper is used first, then the result is
        conservatively augmented with nearby prefix/body spans so deleting a
        list item does not leave an orphan bullet / number behind.
        """
        if selected_span is None:
            return []

        spans = list(all_spans or [])
        result = []

        for line in self._group_visual_lines(spans):
            if any(self._same_span(span, selected_span) for span in line["spans"]):
                result = list(line["spans"])
                break

        if not result:
            result = [
                span
                for span in spans
                if self._same_line(span, selected_span)
            ]

        if not result:
            return []

        def union_bbox(items):
            return (
                min(float(s.bbox[0]) for s in items),
                min(float(s.bbox[1]) for s in items),
                max(float(s.bbox[2]) for s in items),
                max(float(s.bbox[3]) for s in items),
            )

        def is_prefix_only(value):
            value = str(value or "")
            return bool(
                re.fullmatch(r"\s*\d+\s*[.)]\s*", value)
                or re.fullmatch(r"\s*[•◦▪▫‣⁃∙·●○■□◆◇–—-]\s*", value)
            )

        # A few PDF producers position the bullet / list number on a slightly
        # different baseline. Pull such a nearby prefix into the selection.
        changed = True
        while changed:
            changed = False
            x0, y0, x1, y1 = union_bbox(result)
            line_height = max(1.0, y1 - y0)
            line_mid = (y0 + y1) / 2.0

            for span in spans:
                if any(self._same_span(span, existing) for existing in result):
                    continue

                sx0, sy0, sx1, sy1 = map(float, span.bbox)
                smid = (sy0 + sy1) / 2.0
                vertical_close = abs(smid - line_mid) <= max(
                    2.25,
                    line_height * 0.65,
                    float(span.size) * 0.50,
                )

                if not vertical_close:
                    continue

                # Prefix just to the left of the body.
                prefix_gap = x0 - sx1
                if is_prefix_only(span.text) and -2.0 <= prefix_gap <= 36.0:
                    result.append(span)
                    changed = True
                    break

                # Or body just to the right when the selected span itself is
                # only the prefix.
                if any(is_prefix_only(item.text) for item in result):
                    body_gap = sx0 - x1
                    if -2.0 <= body_gap <= 36.0 and not is_prefix_only(span.text):
                        result.append(span)
                        changed = True
                        break

        # A tabular row is commonly stored as several independent visual lines
        # because its cells have large horizontal gaps.  Expand the selection
        # across those cells only when a safe, long horizontal rule proves that
        # they belong to one table row. This retains the conservative column
        # splitting used for ordinary multi-column documents.
        if page is not None:
            baseline_tolerance = max(1.25, float(selected_span.size) * 0.34)
            row_candidates = [
                span
                for span in spans
                if abs(
                    float(span.origin[1]) - float(selected_span.origin[1])
                ) <= baseline_tolerance
            ]

            if (
                len(row_candidates) > len(result)
                and self._has_table_column_layout(row_candidates)
            ):
                row_bbox = self.line_bbox(row_candidates)
                row_size = max(float(span.size) for span in row_candidates)
                max_rule_distance = max(18.0, row_size * 3.0)
                matching_rule = any(
                    self._rule_spans_band(
                        rule,
                        float(row_bbox[0]),
                        float(row_bbox[2]),
                        minimum_ratio=0.84,
                    )
                    and min(
                        abs(float(rule["y"]) - float(row_bbox[1])),
                        abs(float(rule["y"]) - float(row_bbox[3])),
                    ) <= max_rule_distance
                    for rule in self.horizontal_table_rules(page)
                )

                if matching_rule:
                    result = row_candidates

        result.sort(
            key=lambda span: (
                float(span.bbox[0]),
                float(span.bbox[1]),
                int(span.block_index),
                int(span.line_index),
                int(span.span_index),
            )
        )
        return result

    @staticmethod
    def line_bbox(spans):
        if not spans:
            return None
        return (
            min(span.bbox[0] for span in spans),
            min(span.bbox[1] for span in spans),
            max(span.bbox[2] for span in spans),
            max(span.bbox[3] for span in spans),
        )

    @staticmethod
    def _line_text(spans):
        return " ".join(
            span.text.strip()
            for span in sorted(spans, key=lambda item: (item.bbox[0], item.span_index))
            if span.text.strip()
        ).strip()

    @staticmethod
    def _numbered_prefix(text: str):
        """Return (number, punctuation) for a numbered-list line such as '2.' or '2)'."""
        match = re.match(r"^\s*(\d+)\s*([.)])(?:\s+|$)", text or "")
        if not match:
            return None
        return int(match.group(1)), match.group(2)

    @staticmethod
    def _bullet_prefix(text: str):
        """Return the visible bullet marker for a bulleted-list line, if present."""
        match = re.match(
            r"^\s*([•◦▪▫‣⁃∙·●○■□◆◇–—-])(?:\s+|$)",
            text or "",
        )
        if not match:
            return None
        return match.group(1)

    def _group_visual_lines(self, all_spans):
        """
        Group spans by their *visual* baseline, not only PDF block/line indexes.

        Many PDFs store a list prefix ("3.") and the item body in separate
        blocks. First cluster spans that share a visual baseline, then split
        very large horizontal gaps so unrelated columns are not merged.
        """
        spans = list(all_spans or [])
        if not spans:
            return []

        spans.sort(
            key=lambda span: (
                float(span.origin[1]),
                float(span.bbox[0]),
                int(span.block_index),
                int(span.line_index),
                int(span.span_index),
            )
        )

        baseline_groups = []

        for span in spans:
            placed = False

            # Try the most recent few groups first because spans are y-sorted.
            for group in reversed(baseline_groups[-4:]):
                representative = group["spans"][0]
                tolerance = max(
                    1.25,
                    min(float(span.size), float(representative.size)) * 0.34,
                )

                if abs(float(span.origin[1]) - float(group["baseline"])) <= tolerance:
                    group["spans"].append(span)
                    values = [float(item.origin[1]) for item in group["spans"]]
                    group["baseline"] = float(median(values))
                    placed = True
                    break

            if not placed:
                baseline_groups.append(
                    {
                        "baseline": float(span.origin[1]),
                        "spans": [span],
                    }
                )

        lines = []
        visual_index = 0

        for baseline_group in baseline_groups:
            row_spans = sorted(
                baseline_group["spans"],
                key=lambda span: (
                    float(span.bbox[0]),
                    int(span.block_index),
                    int(span.line_index),
                    int(span.span_index),
                ),
            )

            if not row_spans:
                continue

            # Split separate columns / distant fields on the same baseline.
            segments = []
            current = [row_spans[0]]

            for span in row_spans[1:]:
                prev = current[-1]
                gap = float(span.bbox[0]) - float(prev.bbox[2])
                typical_size = max(
                    1.0,
                    median([float(item.size) for item in current] + [float(span.size)]),
                )
                split_gap = max(42.0, typical_size * 5.5)

                if gap > split_gap:
                    segments.append(current)
                    current = [span]
                else:
                    current.append(span)

            if current:
                segments.append(current)

            for segment in segments:
                bbox = self.line_bbox(segment)
                if not bbox:
                    continue

                baseline = median([float(span.origin[1]) for span in segment])

                lines.append(
                    {
                        "key": ("visual", visual_index),
                        "spans": segment,
                        "bbox": bbox,
                        "baseline": float(baseline),
                        "text": self._line_text(segment),
                        "blocks": {
                            int(span.block_index)
                            for span in segment
                        },
                    }
                )
                visual_index += 1

        lines.sort(key=lambda item: (item["baseline"], item["bbox"][0]))
        return lines

    @staticmethod
    def _renumber_first_prefix(spans, new_number: int):
        """Return a span-id -> replacement-text map for a numbered list prefix."""
        replacements = {}
        for span in sorted(spans, key=lambda item: (item.bbox[0], item.span_index)):
            text = span.text or ""
            match = re.match(r"^(\s*)(\d+)(\s*[.)])", text)
            if not match:
                continue
            replacements[id(span)] = (
                text[: match.start(2)]
                + str(int(new_number))
                + text[match.end(2) :]
            )
            break
        return replacements


    def plan_delete_line_reflow(
        self,
        all_spans,
        selected_line_spans,
        page_rect=None,
        page=None,
    ):
        """
        Build a Word-like upward-flow plan after deleting one visual line.

        The previous implementation moved only lines from the same PDF block or
        the same detected list. That is too conservative for many generated
        PDFs because each paragraph / list item / heading may live in a separate
        PDF block. The result was a visible blank gap even though the deletion
        itself succeeded.

        This version closes exactly ONE line of vertical space and moves the
        later text in the same broad content column upward, while preserving
        the original spacing between all later paragraphs / headings.

        Numbered list items immediately following the deleted item are
        renumbered when their number prefix can be handled safely by the
        low-level reflow routine.
        """
        selected = list(selected_line_spans or [])
        if not selected:
            raise ValueError("No full line is selected.")

        selected_bbox = self.line_bbox(selected)
        selected_baseline = median([float(span.origin[1]) for span in selected])
        selected_text = self._line_text(selected)
        selected_prefix = self._numbered_prefix(selected_text)

        selected_x0 = float(selected_bbox[0])
        selected_x1 = float(selected_bbox[2])
        selected_size = max(float(span.size) for span in selected)
        selected_height = max(1.0, float(selected_bbox[3]) - float(selected_bbox[1]))

        selected_ids = {id(span) for span in selected}

        visual_lines = self._group_visual_lines(all_spans)

        later_lines = [
            line
            for line in visual_lines
            if not any(id(span) in selected_ids for span in line["spans"])
            and float(line["baseline"]) > float(selected_baseline) + 0.25
        ]

        if not later_lines:
            return {
                "ok": True,
                "selected_bbox": selected_bbox,
                "shift_dy": 0.0,
                "shifted_items": [],
                "renumbered": False,
                "reason": "Line deleted. There is no later text on this page to move.",
            }

        # ------------------------------------------------------------------
        # Detect the broad text-flow column.
        #
        # Do not require the exact same PDF block or indentation. Headings,
        # paragraph bodies, bullets and numbered items are commonly stored in
        # separate PDF blocks even though they are one visual document flow.
        # ------------------------------------------------------------------
        x_tolerance = max(115.0, selected_size * 14.0)

        def same_flow_column(line):
            lx0, _, lx1, _ = map(float, line["bbox"])

            # Wide horizontal overlap with the selected content band.
            overlap = max(0.0, min(lx1, selected_x1) - max(lx0, selected_x0))
            selected_width = max(1.0, selected_x1 - selected_x0)
            line_width = max(1.0, lx1 - lx0)

            if overlap / min(selected_width, line_width) >= 0.12:
                return True

            # Also allow normal indentation changes used by headings / bullets.
            return abs(lx0 - selected_x0) <= x_tolerance

        flow_lines = [line for line in later_lines if same_flow_column(line)]

        # A table row must never pull a fixed footer / later unrelated section
        # into the reflow merely because that content overlaps the table's wide
        # horizontal band.  This was also the main performance trap in the web
        # editor: each unrelated footer line became another clipped full-page
        # Form XObject, so rebuilding the selectable-text dictionary could take
        # minutes on a small VPS.
        #
        # When the selected line has a proven multi-column layout, use the
        # table's own independently stroked horizontal rules as a structural
        # boundary.  Only the contiguous rule cluster around the selected row
        # is considered; a second table farther down the page is not joined to
        # it.  One normal row of padding after the last rule keeps an unruled
        # total row (a common bank-statement layout) in the same reflow.
        table_row_step = None
        if page is not None and self._has_table_column_layout(selected):
            selected_width = max(1.0, selected_x1 - selected_x0)
            matching_rules = [
                dict(rule)
                for rule in self.horizontal_table_rules(page)
                if float(rule["x1"]) - float(rule["x0"])
                >= max(48.0, selected_width * 0.72)
                and self._rule_spans_band(
                    rule,
                    selected_x0,
                    selected_x1,
                    minimum_ratio=0.82,
                )
            ]
            matching_rules.sort(key=lambda rule: float(rule["y"]))

            if matching_rules:
                selected_mid_y = (
                    float(selected_bbox[1]) + float(selected_bbox[3])
                ) / 2.0
                anchor_index = min(
                    range(len(matching_rules)),
                    key=lambda index: abs(
                        float(matching_rules[index]["y"]) - selected_mid_y
                    ),
                )
                anchor_distance = abs(
                    float(matching_rules[anchor_index]["y"])
                    - selected_mid_y
                )
                cluster_gap_limit = max(72.0, selected_size * 6.0)
                row_rule_distance = max(24.0, selected_size * 3.25)

                if anchor_distance <= row_rule_distance:
                    start = anchor_index
                    while start > 0:
                        gap = (
                            float(matching_rules[start]["y"])
                            - float(matching_rules[start - 1]["y"])
                        )
                        if gap > cluster_gap_limit:
                            break
                        start -= 1

                    end = anchor_index
                    while end + 1 < len(matching_rules):
                        gap = (
                            float(matching_rules[end + 1]["y"])
                            - float(matching_rules[end]["y"])
                        )
                        if gap > cluster_gap_limit:
                            break
                        end += 1

                    rule_cluster = matching_rules[start : end + 1]
                    rule_ys = [float(rule["y"]) for rule in rule_cluster]
                    rule_steps = [
                        later - earlier
                        for earlier, later in zip(rule_ys, rule_ys[1:])
                        if 1.0 <= later - earlier <= cluster_gap_limit
                    ]

                    nearby_table_baselines = sorted(
                        {
                            float(line["baseline"])
                            for line in flow_lines
                            if float(line["baseline"])
                            <= max(rule_ys) + cluster_gap_limit
                        }
                    )
                    baseline_steps = [
                        later - earlier
                        for earlier, later in zip(
                            [float(selected_baseline)] + nearby_table_baselines,
                            nearby_table_baselines,
                        )
                        if 1.0 <= later - earlier <= cluster_gap_limit
                    ]
                    normal_table_step = float(
                        median(rule_steps or baseline_steps)
                        if (rule_steps or baseline_steps)
                        else max(selected_height, selected_size * 1.2)
                    )
                    # A proven table's separator spacing is the authoritative
                    # row height.  Do not later replace it with the median
                    # baseline spacing from the whole page: compact body text,
                    # footers, or another table can have a smaller leading.
                    # Repeated row deletes otherwise under-shift each remaining
                    # row and accumulate a visible blank gap.
                    if rule_steps:
                        table_row_step = float(median(rule_steps))
                    elif baseline_steps:
                        table_row_step = float(median(baseline_steps))
                    table_bottom = max(rule_ys) + max(
                        normal_table_step * 1.35,
                        selected_height * 1.5,
                        selected_size * 2.0,
                    )
                    flow_lines = [
                        line
                        for line in flow_lines
                        if float(line["bbox"][1]) <= table_bottom
                    ]

        if page_rect is not None:
            try:
                page_bottom = float(page_rect.y1)
                # Keep a likely fixed footer in place. Ordinary body content
                # above this band still flows upward.
                footer_start = page_bottom * 0.94
                flow_lines = [
                    line
                    for line in flow_lines
                    if float(line["bbox"][1]) < footer_start
                ]
            except Exception:
                pass

        if not flow_lines:
            return {
                "ok": True,
                "selected_bbox": selected_bbox,
                "shift_dy": 0.0,
                "shifted_items": [],
                "renumbered": False,
                "reason": (
                    "Line deleted. No later text in the same visual content "
                    "column needed to move."
                ),
            }

        flow_lines.sort(key=lambda item: (float(item["baseline"]), float(item["bbox"][0])))

        # ------------------------------------------------------------------
        # Close one line of space, but preserve intentional paragraph spacing.
        #
        # For a normal list, raw_gap is already one line, so the next item lands
        # exactly on the deleted item's baseline. If the next object is a new
        # section after a larger paragraph-after gap, move it upward by one
        # normal line step rather than collapsing the whole paragraph gap.
        # ------------------------------------------------------------------
        first_baseline = float(flow_lines[0]["baseline"])
        raw_gap = first_baseline - float(selected_baseline)

        nearby_baselines = [
            float(line["baseline"])
            for line in visual_lines
            if abs(float(line["bbox"][0]) - selected_x0) <= x_tolerance
        ]
        nearby_baselines.sort()

        candidate_steps = []
        for a, b in zip(nearby_baselines, nearby_baselines[1:]):
            d = b - a
            if 0.75 <= d <= max(24.0, selected_size * 2.25):
                candidate_steps.append(d)

        if table_row_step is not None and table_row_step >= max(
            1.0,
            selected_height * 0.70,
        ):
            normal_step = float(table_row_step)
        elif candidate_steps:
            normal_step = float(median(candidate_steps))
        else:
            normal_step = max(
                selected_height * 1.15,
                selected_size * 1.20,
            )

        move_amount = min(
            max(raw_gap, 0.0),
            max(normal_step, selected_height),
        )

        if move_amount < 0.25:
            return {
                "ok": True,
                "selected_bbox": selected_bbox,
                "shift_dy": 0.0,
                "shifted_items": [],
                "renumbered": False,
                "reason": "Line deleted. Following text was already aligned.",
            }

        shift_dy = -float(move_amount)

        # ------------------------------------------------------------------
        # Identify only the sequential numbered-list continuation that should
        # have its visible numbers decremented. Later headings such as
        # "2. ACCOUNT" must move upward but must NOT be renumbered.
        # ------------------------------------------------------------------
        renumber_keys = set()

        if selected_prefix:
            expected = int(selected_prefix[0]) + 1
            previous_baseline = float(selected_baseline)
            first_list_gap = None

            for line in flow_lines:
                gap = float(line["baseline"]) - previous_baseline
                if gap <= 0:
                    continue

                max_gap = (
                    max(selected_size * 3.4, 18.0)
                    if first_list_gap is None
                    else max(first_list_gap * 1.70, selected_size * 2.6, 16.0)
                )

                if gap > max_gap:
                    break

                prefix = self._numbered_prefix(line["text"])
                x_close = abs(float(line["bbox"][0]) - selected_x0) <= max(
                    28.0,
                    selected_size * 3.5,
                )

                if not prefix or not x_close or int(prefix[0]) != expected:
                    break

                renumber_keys.add(line["key"])

                if first_list_gap is None:
                    first_list_gap = gap

                previous_baseline = float(line["baseline"])
                expected += 1

        shifted_items = []

        for line in flow_lines:
            replacements = {}

            if line["key"] in renumber_keys:
                original_prefix = self._numbered_prefix(line["text"])
                if original_prefix:
                    replacements = self._renumber_first_prefix(
                        line["spans"],
                        max(1, int(original_prefix[0]) - 1),
                    )

            for span in line["spans"]:
                new_text = replacements.get(id(span), span.text)

                shifted_items.append(
                    {
                        "span": span,
                        "text": new_text,
                        "origin": (
                            float(span.origin[0]),
                            float(span.origin[1]) + shift_dy,
                        ),
                        "final_bbox": (
                            float(span.bbox[0]),
                            float(span.bbox[1]) + shift_dy,
                            float(span.bbox[2]),
                            float(span.bbox[3]) + shift_dy,
                        ),
                        "renumbered": new_text != span.text,
                        "line_key": line["key"],
                    }
                )

        return {
            "ok": True,
            "selected_bbox": selected_bbox,
            "shift_dy": float(shift_dy),
            "shifted_items": shifted_items,
            "renumbered": bool(renumber_keys),
            "reason": (
                "Deleted the selected line and moved the later text in the "
                f"same content column upward by {move_amount:.2f} pt."
            ),
        }

    def plan_table_rule_reflow(
        self,
        page,
        selected_line_spans,
        shifted_items,
        shift_dy,
        remove_deleted_row_rule=False,
    ):
        """
        Plan safe vector-rule changes for a table-row deletion.

        Only independently stroked, single-command horizontal paths are ever
        included. Rules spanning the selected row and the affected vertical
        content range move by the same delta as the text. When explicitly
        requested, the first separator belonging to the deleted row is removed
        instead of moved.
        """
        selected = list(selected_line_spans or [])
        shifted = list(shifted_items or [])

        if not selected:
            raise ValueError("No full line is selected.")

        selected_bbox = self.line_bbox(selected)
        if not selected_bbox:
            return {
                "actions": [],
                "moved_count": 0,
                "removed_count": 0,
                "detected_count": 0,
            }

        # A long divider under an ordinary paragraph is not sufficient table
        # evidence. Require multiple selected cells with at least one genuine
        # column-sized gap. It is safer to leave an unusual one-cell table rule
        # untouched than to reposition unrelated page artwork.
        if not self._has_table_column_layout(selected):
            return {
                "actions": [],
                "moved_count": 0,
                "removed_count": 0,
                "detected_count": 0,
            }

        shift_dy = float(shift_dy or 0.0)
        selected_x0 = float(selected_bbox[0])
        selected_x1 = float(selected_bbox[2])
        selected_y1 = float(selected_bbox[3])
        selected_width = max(1.0, selected_x1 - selected_x0)
        selected_size = max(float(span.size) for span in selected)

        original_shifted_rects = []
        for item in shifted:
            span = item.get("span")
            if span is None:
                continue
            try:
                original_shifted_rects.append(
                    pymupdf.Rect(*(float(value) for value in span.bbox))
                )
            except Exception:
                continue

        if original_shifted_rects:
            first_shifted_y0 = min(rect.y0 for rect in original_shifted_rects)
            last_shifted_y1 = max(rect.y1 for rect in original_shifted_rects)
            vertical_padding = max(
                8.0,
                abs(shift_dy) * 1.25,
                selected_size * 1.5,
            )
            maximum_rule_y = last_shifted_y1 + vertical_padding
        else:
            first_shifted_y0 = None
            maximum_rule_y = selected_y1 + max(18.0, selected_size * 3.0)

        candidates = []
        for rule in self.horizontal_table_rules(page):
            rule_width = float(rule["x1"]) - float(rule["x0"])
            if rule_width < max(48.0, selected_width * 0.72):
                continue
            if not self._rule_spans_band(
                rule,
                selected_x0,
                selected_x1,
                minimum_ratio=0.82,
            ):
                continue
            if float(rule["y"]) < selected_y1 - max(0.75, rule["width"]):
                continue
            if float(rule["y"]) > maximum_rule_y:
                continue
            candidates.append(dict(rule))

        candidates.sort(key=lambda rule: (rule["y"], rule["x0"]))

        # The removable separator must be the nearest rule immediately after
        # the deleted row. With later text, require it to sit before that next
        # row; this avoids deleting a boundary that actually belongs below it.
        removable_index = None
        if remove_deleted_row_rule and candidates:
            first_rule = candidates[0]
            distance = float(first_rule["y"]) - selected_y1
            close_enough = distance <= max(18.0, selected_size * 2.5)
            before_next_row = (
                first_shifted_y0 is None
                or float(first_rule["y"])
                <= float(first_shifted_y0) + max(1.0, first_rule["width"] * 2.0)
            )
            if distance >= -max(0.75, first_rule["width"]) and close_enough and before_next_row:
                removable_index = 0

        actions = []

        for index, rule in enumerate(candidates):
            if index == removable_index:
                action_name = "remove"
                target_y = None
            elif abs(shift_dy) >= 0.01:
                action_name = "move"
                target_y = float(rule["y"]) + shift_dy
            else:
                continue

            actions.append(
                {
                    **rule,
                    "action": action_name,
                    "target_y": target_y,
                }
            )

        return {
            "actions": actions,
            "moved_count": sum(
                1 for action in actions if action["action"] == "move"
            ),
            "removed_count": sum(
                1 for action in actions if action["action"] == "remove"
            ),
            "detected_count": len(candidates),
        }

    @staticmethod
    def _table_cell_groups(spans):
        """Group one selected table row into visual cells.

        A PDF has no native concept of a table cell.  We therefore keep spans
        that are separated by normal word spacing together and split only on
        a genuine column-sized gap.  The returned groups are also the exact
        clips used when cloning a row, so a row may contain two, five, or any
        other number of columns without a fixed schema.
        """
        ordered = sorted(
            list(spans or []),
            key=lambda span: (
                float(span.bbox[0]),
                float(span.bbox[1]),
                int(span.block_index),
                int(span.line_index),
                int(span.span_index),
            ),
        )
        if not ordered:
            return []

        sizes = [max(1.0, float(span.size)) for span in ordered]
        split_gap = max(24.0, float(median(sizes)) * 3.0)
        groups = [[ordered[0]]]

        for span in ordered[1:]:
            previous = groups[-1][-1]
            gap = float(span.bbox[0]) - float(previous.bbox[2])
            if gap > split_gap:
                groups.append([span])
            else:
                groups[-1].append(span)

        return groups

    def plan_insert_table_row(
        self,
        page,
        all_spans,
        selected_line_spans,
        position="below",
    ):
        """Plan a template-clone table-row insertion.

        The selected row supplies the exact existing PDF glyphs, font
        resources, cell positions, and separator style.  Later page content is
        moved down by one proven row step.  This is deliberately limited to a
        row whose multi-column layout and horizontal separator can both be
        established safely; guessing on ordinary paragraph text would risk
        moving unrelated page artwork.
        """
        if str(position or "below").lower() != "below":
            raise ValueError("Only insertion below the selected table row is supported.")

        selected = list(selected_line_spans or [])
        if not selected:
            raise ValueError("Select a full table row before adding a row.")
        if not self._has_table_column_layout(selected):
            raise ValueError(
                "The selected line is not a proven multi-column table row. "
                "Select a row containing at least two separated columns."
            )

        cell_groups = self._table_cell_groups(selected)
        if len(cell_groups) < 2:
            raise ValueError(
                "At least two table columns are required before a row can be cloned."
            )

        selected_bbox = self.line_bbox(selected)
        if not selected_bbox:
            raise ValueError("The selected table row has no usable PDF bounds.")

        selected_baseline = float(
            median([float(span.origin[1]) for span in selected])
        )
        selected_size = max(float(span.size) for span in selected)
        selected_height = max(
            1.0,
            float(selected_bbox[3]) - float(selected_bbox[1]),
        )
        selected_x0 = float(selected_bbox[0])
        selected_x1 = float(selected_bbox[2])

        # Only independently stroked rules spanning nearly the whole selected
        # row may be copied or moved.
        matching_rules = []
        for rule in self.horizontal_table_rules(page):
            if not self._rule_spans_band(
                rule,
                selected_x0,
                selected_x1,
                minimum_ratio=0.82,
            ):
                continue
            if float(rule["x1"]) - float(rule["x0"]) < max(
                48.0,
                (selected_x1 - selected_x0) * 0.72,
            ):
                continue
            matching_rules.append(dict(rule))

        matching_rules.sort(key=lambda rule: (float(rule["y"]), float(rule["x0"])))
        separator_candidates = [
            rule
            for rule in matching_rules
            if float(rule["y"])
            >= float(selected_bbox[3]) - max(0.75, float(rule.get("width") or 0.0))
            and float(rule["y"])
            <= float(selected_bbox[3]) + max(24.0, selected_size * 3.0)
        ]
        if not separator_candidates:
            raise ValueError(
                "A safe horizontal separator below the selected table row "
                "could not be found. The row was not changed."
            )
        separator = min(separator_candidates, key=lambda rule: float(rule["y"]))

        # Derive row height from the table's own geometry.  Separator spacing
        # is strongest evidence; adjacent row baselines are the fallback.
        rule_step_candidates = []
        separator_y = float(separator["y"])
        rule_ys = [float(rule["y"]) for rule in matching_rules]
        later_rule_ys = [value for value in rule_ys if value > separator_y + 0.25]
        earlier_rule_ys = [value for value in rule_ys if value < separator_y - 0.25]
        # The nearest rule above and the selected row's bottom separator bound
        # this exact row, so that distance has priority when it exists.
        if earlier_rule_ys:
            rule_step_candidates.append(separator_y - max(earlier_rule_ys))
        if later_rule_ys:
            rule_step_candidates.append(min(later_rule_ys) - separator_y)

        visual_lines = self._group_visual_lines(all_spans)
        later_baselines = sorted(
            {
                round(float(line["baseline"]), 4)
                for line in visual_lines
                if float(line["baseline"]) > selected_baseline + 0.25
                and max(
                    0.0,
                    min(float(line["bbox"][2]), selected_x1)
                    - max(float(line["bbox"][0]), selected_x0),
                )
                > 0.0
            }
        )
        baseline_step_candidates = []
        if later_baselines:
            baseline_step_candidates.append(
                float(later_baselines[0]) - selected_baseline
            )

        plausible_rule_steps = [
            float(value)
            for value in rule_step_candidates
            if max(1.0, selected_height * 0.70)
            <= float(value)
            <= max(72.0, selected_size * 6.0)
        ]
        plausible_baseline_steps = [
            float(value)
            for value in baseline_step_candidates
            if max(1.0, selected_height * 0.70)
            <= float(value)
            <= max(72.0, selected_size * 6.0)
        ]
        if not plausible_rule_steps and not plausible_baseline_steps:
            raise ValueError(
                "The table row height could not be established safely from "
                "its neighboring rows or separators."
            )

        # Rule geometry is authoritative.  The closest plausible rule spacing
        # represents the current data-row slot and avoids adopting a larger
        # total/section gap below the table. Baselines are used only when the
        # table has no second safe separator.
        shift_dy = float(
            min(plausible_rule_steps)
            if plausible_rule_steps
            else min(plausible_baseline_steps)
        )
        if shift_dy < 0.5:
            raise ValueError("The detected table row height is too small to insert safely.")

        # Reuse the conservative text-flow selection from line deletion, but
        # reverse its direction and use the table-proven row height.
        flow_plan = self.plan_delete_line_reflow(
            all_spans,
            selected,
            page.rect,
            page=page,
        )
        shifted_items = []
        for item in flow_plan.get("shifted_items", []) or []:
            span = item.get("span")
            if span is None:
                continue
            shifted_items.append(
                {
                    **dict(item),
                    "text": span.text,
                    "origin": (
                        float(span.origin[0]),
                        float(span.origin[1]) + shift_dy,
                    ),
                    "final_bbox": (
                        float(span.bbox[0]),
                        float(span.bbox[1]) + shift_dy,
                        float(span.bbox[2]),
                        float(span.bbox[3]) + shift_dy,
                    ),
                    "renumbered": False,
                }
            )

        inserted_bbox = (
            float(selected_bbox[0]),
            float(selected_bbox[1]) + shift_dy,
            float(selected_bbox[2]),
            float(selected_bbox[3]) + shift_dy,
        )

        # Moving the table down may collide with the paragraph immediately
        # following it even when that paragraph is outside the table's rule
        # cluster. Propagate the same row-height shift through only the
        # contiguous, physically colliding text below the table. The first
        # non-overlapping line ends the cascade, so a later section / footer is
        # left at its original coordinates whenever the existing whitespace is
        # sufficient. This preserves the original gaps inside a multi-line
        # paragraph and prevents the inserted row from painting over it.
        selected_ids = {id(span) for span in selected}
        shifted_ids = {
            id(item["span"])
            for item in shifted_items
            if item.get("span") is not None
        }
        affected_x0 = min(
            [float(inserted_bbox[0])]
            + [float(item["final_bbox"][0]) for item in shifted_items]
        )
        affected_x1 = max(
            [float(inserted_bbox[2])]
            + [float(item["final_bbox"][2]) for item in shifted_items]
        )
        cascade_bottom = max(
            [float(inserted_bbox[3])]
            + [float(item["final_bbox"][3]) for item in shifted_items]
        )

        for line in visual_lines:
            if float(line["baseline"]) <= selected_baseline + 0.25:
                continue

            remaining = [
                span
                for span in line["spans"]
                if id(span) not in selected_ids and id(span) not in shifted_ids
            ]
            if not remaining:
                continue

            line_bbox = self.line_bbox(remaining)
            if not line_bbox:
                continue

            horizontal_overlap = max(
                0.0,
                min(float(line_bbox[2]), affected_x1)
                - max(float(line_bbox[0]), affected_x0),
            )
            if horizontal_overlap <= 0.0:
                continue

            # Once a line in the affected horizontal flow starts below the
            # shifted content, every later line starts lower still and cannot
            # collide with this insertion.
            if float(line_bbox[1]) >= cascade_bottom - 0.10:
                break

            for span in remaining:
                shifted_items.append(
                    {
                        "span": span,
                        "text": span.text,
                        "origin": (
                            float(span.origin[0]),
                            float(span.origin[1]) + shift_dy,
                        ),
                        "final_bbox": (
                            float(span.bbox[0]),
                            float(span.bbox[1]) + shift_dy,
                            float(span.bbox[2]),
                            float(span.bbox[3]) + shift_dy,
                        ),
                        "renumbered": False,
                        "line_key": line["key"],
                    }
                )
                shifted_ids.add(id(span))

            cascade_bottom = max(
                cascade_bottom,
                float(line_bbox[3]) + shift_dy,
            )

        # Never create a row or move body content into the fixed footer band.
        footer_limit = float(page.rect.y1) * 0.94
        target_bottoms = [float(inserted_bbox[3])]
        target_bottoms.extend(float(item["final_bbox"][3]) for item in shifted_items)
        if target_bottoms and max(target_bottoms) >= footer_limit:
            raise ValueError(
                "There is not enough room below this table row. Adding it "
                "would move content into the page footer or off the page."
            )

        original_shifted_bottom = max(
            [float(item["span"].bbox[3]) for item in shifted_items]
            or [float(selected_bbox[3])]
        )
        maximum_rule_y = original_shifted_bottom + max(
            8.0,
            shift_dy * 0.75,
            selected_size * 1.5,
        )

        rule_actions = []
        for rule in matching_rules:
            rule_y = float(rule["y"])
            if rule_y <= separator_y + max(0.20, float(rule.get("width") or 0.0)):
                continue
            if rule_y > maximum_rule_y:
                continue
            rule_actions.append(
                {
                    **dict(rule),
                    "action": "move",
                    "target_y": rule_y + shift_dy,
                }
            )

        inserted_rule = {
            **dict(separator),
            "action": "insert",
            "source_y": separator_y,
            "target_y": separator_y + shift_dy,
        }
        table_rule_plan = {
            "actions": rule_actions,
            "insertions": [inserted_rule],
            "moved_count": len(rule_actions),
            "inserted_count": 1,
            "detected_count": len(matching_rules),
        }

        template_cells = []
        for group in cell_groups:
            bbox = self.line_bbox(group)
            template_cells.append(
                {
                    "text": self._line_text(group),
                    "bbox": tuple(float(value) for value in bbox),
                }
            )

        return {
            "ok": True,
            "position": "below",
            "column_count": len(template_cells),
            "shift_dy": shift_dy,
            "shifted_items": shifted_items,
            "table_rule_plan": table_rule_plan,
            "template_bbox": tuple(float(value) for value in selected_bbox),
            "template_text": self._line_text(selected),
            "template_cells": template_cells,
            "inserted_bbox": inserted_bbox,
            "reason": (
                f"Detected {len(template_cells)} table column(s) and a "
                f"{shift_dy:.2f} pt row step."
            ),
        }

    def insert_table_row_with_reflow(self, page, line_spans, plan):
        """Clone a selected table row and move later content down exactly.

        Existing PDF content is copied from an in-memory pre-edit snapshot via
        ``show_pdf_page``.  No font is substituted or synthesized, so the cloned
        row retains the template row's searchable text, glyph shapes, weight,
        spacing, and resource identity.  The caller may then edit each cloned
        cell through the normal clean text replacement workflow.
        """
        selected = list(line_spans or [])
        plan = dict(plan or {})
        shifted = list(plan.get("shifted_items") or [])
        table_rule_plan = dict(plan.get("table_rule_plan") or {})
        rule_actions = list(table_rule_plan.get("actions") or [])
        rule_insertions = list(table_rule_plan.get("insertions") or [])
        shift_dy = float(plan.get("shift_dy") or 0.0)

        if not selected:
            raise ValueError("Select a full table row before adding a row.")
        if str(plan.get("position") or "below") != "below":
            raise ValueError("Only insertion below the selected table row is supported.")
        if shift_dy <= 0.0:
            raise ValueError("The table-row insertion plan has no positive row step.")

        doc = page.parent
        if doc is None:
            raise ValueError("The edited page is no longer attached to a PDF.")
        page_number = int(page.number)

        try:
            source_raw = doc.tobytes(
                garbage=0,
                clean=False,
                deflate=False,
                no_new_id=True,
            )
        except TypeError:
            source_raw = doc.tobytes(no_new_id=True)
        source_doc = pymupdf.open(stream=source_raw, filetype="pdf")

        def _rect(values):
            rect = pymupdf.Rect(*(float(value) for value in values))
            rect.normalize()
            return rect

        def _union_rects(rects):
            values = [pymupdf.Rect(rect) for rect in rects if rect is not None]
            if not values:
                return None
            result = pymupdf.Rect(values[0])
            for rect in values[1:]:
                result |= rect
            result.normalize()
            return result

        def _expanded(rect, xpad=4.0, ypad=4.0):
            rect = pymupdf.Rect(rect)
            return pymupdf.Rect(
                rect.x0 - float(xpad),
                rect.y0 - float(ypad),
                rect.x1 + float(xpad),
                rect.y1 + float(ypad),
            )

        def _compact(value):
            return re.sub(r"\s+", "", str(value or "").replace("\u00a0", " "))

        def _show_exact_clip(source_page, source_rect, target_rect, label):
            source_rect = _rect(source_rect) & source_page.rect
            target_rect = _rect(target_rect) & page.rect
            if source_rect.is_empty or target_rect.is_empty:
                raise ValueError(
                    f"The {label} could not be copied because its PDF clip is empty."
                )
            page.show_pdf_page(
                target_rect,
                source_doc,
                page_number,
                clip=source_rect,
                keep_proportion=False,
                overlay=True,
            )

        def _rule_rect(action, bounds):
            width = max(0.01, float(action.get("width") or 0.0))
            endpoint_pad = max(0.35, width * 1.5)
            vertical_pad = max(0.35, width * 1.5)
            rect = pymupdf.Rect(
                float(action["x0"]) - endpoint_pad,
                float(action["y"]) - vertical_pad,
                float(action["x1"]) + endpoint_pad,
                float(action["y"]) + vertical_pad,
            ) & bounds
            rect.normalize()
            return rect

        def _draw_rule(action):
            target_y = float(action["target_y"])
            page.draw_line(
                pymupdf.Point(float(action["x0"]), target_y),
                pymupdf.Point(float(action["x1"]), target_y),
                color=tuple(action.get("color") or (0.0,)),
                dashes=action.get("dashes"),
                width=max(0.01, float(action.get("width") or 0.0)),
                lineCap=self._drawing_line_cap(action.get("line_cap")),
                lineJoin=int(action.get("line_join") or 0),
                overlay=True,
                stroke_opacity=float(
                    action.get("stroke_opacity")
                    if action.get("stroke_opacity") is not None
                    else 1.0
                ),
            )

        # Build exact whole-line move specs before mutating the page.
        shifted_groups = {}
        for item in shifted:
            span = item.get("span")
            if span is None:
                continue
            key = tuple(
                item.get("line_key")
                or (int(span.block_index), int(span.line_index))
            )
            shifted_groups.setdefault(key, []).append(item)

        move_specs = []
        for items in shifted_groups.values():
            items.sort(
                key=lambda item: (
                    float(item["span"].bbox[0]),
                    int(item["span"].span_index),
                )
            )
            spans = [item["span"] for item in items]
            source_bbox = _rect(self.line_bbox(spans))
            target_bbox = _union_rects(
                [_rect(item["final_bbox"]) for item in items]
            )
            move_specs.append(
                {
                    "source_bbox": source_bbox,
                    "target_bbox": target_bbox,
                    "text": self._line_text(spans),
                }
            )
        move_specs.sort(
            key=lambda spec: (
                float(spec["source_bbox"].y0),
                float(spec["source_bbox"].x0),
            )
        )

        template_cells = []
        selected_groups = self._table_cell_groups(selected)
        for group in selected_groups:
            source_bbox = _rect(self.line_bbox(group))
            target_bbox = pymupdf.Rect(
                source_bbox.x0,
                source_bbox.y0 + shift_dy,
                source_bbox.x1,
                source_bbox.y1 + shift_dy,
            )
            template_cells.append(
                {
                    "source_bbox": source_bbox,
                    "target_bbox": target_bbox,
                    "text": self._line_text(group),
                }
            )

        try:
            source_page = source_doc[page_number]

            # Keep moved/copied clips free of the old horizontal rule paths.
            for action in rule_actions:
                rect = _rule_rect(action, source_page.rect)
                if not rect.is_empty:
                    source_page.add_redact_annot(rect, fill=None, cross_out=False)
            if rule_actions:
                source_page.apply_redactions(
                    images=pymupdf.PDF_REDACT_IMAGE_NONE,
                    graphics=pymupdf.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED,
                    text=pymupdf.PDF_REDACT_TEXT_NONE,
                )

            # Remove only later text that will be copied down. The selected
            # template remains untouched at its original position.
            seen = set()
            for item in shifted:
                span = item.get("span")
                if span is None:
                    continue
                rect = _rect(span.bbox)
                rect = pymupdf.Rect(
                    rect.x0 - 0.15,
                    rect.y0 - 0.10,
                    rect.x1 + 0.15,
                    rect.y1 + 0.10,
                ) & page.rect
                signature = tuple(round(float(value), 4) for value in rect)
                if rect.is_empty or signature in seen:
                    continue
                seen.add(signature)
                page.add_redact_annot(rect, fill=None, cross_out=False)
            if seen:
                page.apply_redactions(
                    images=pymupdf.PDF_REDACT_IMAGE_NONE,
                    graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                    text=pymupdf.PDF_REDACT_TEXT_REMOVE,
                )

            # Move existing safe table rules, then add a separator with exactly
            # the template rule's visual style below the cloned row.
            for action in rule_actions:
                rect = _rule_rect(action, page.rect)
                if not rect.is_empty:
                    page.add_redact_annot(rect, fill=None, cross_out=False)
            if rule_actions:
                page.apply_redactions(
                    images=pymupdf.PDF_REDACT_IMAGE_NONE,
                    graphics=pymupdf.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED,
                    text=pymupdf.PDF_REDACT_TEXT_NONE,
                )
            for action in rule_actions:
                _draw_rule(action)
            for action in rule_insertions:
                _draw_rule(action)

            for spec in move_specs:
                _show_exact_clip(
                    source_page,
                    spec["source_bbox"],
                    spec["target_bbox"],
                    "following line",
                )
            for spec in template_cells:
                _show_exact_clip(
                    source_page,
                    spec["source_bbox"],
                    spec["target_bbox"],
                    "table cell",
                )
        finally:
            source_doc.close()

        # Serialize and reopen immediately.  A failed check is raised to the
        # caller, which restores its Undo snapshot transactionally.
        try:
            check_raw = doc.tobytes(
                garbage=0,
                clean=False,
                deflate=False,
                no_new_id=True,
            )
        except TypeError:
            check_raw = doc.tobytes(no_new_id=True)
        probe = pymupdf.open(stream=check_raw, filetype="pdf")
        inserted_lines = []
        moved_lines = []
        try:
            check_page = probe[page_number]

            def _require_text(spec, label):
                expected = str(spec.get("text") or "")
                target = _expanded(
                    spec["target_bbox"],
                    xpad=4.0,
                    ypad=max(4.0, float(spec["target_bbox"].height) * 0.8),
                ) & check_page.rect
                observed = check_page.get_text("text", clip=target) or ""
                present = bool(
                    _compact(expected)
                    and _compact(expected) in _compact(observed)
                )
                if not present and expected:
                    try:
                        present = any(
                            pymupdf.Rect(hit).intersects(target)
                            for hit in check_page.search_for(expected)
                        )
                    except Exception:
                        present = False
                if not present:
                    raise ValueError(
                        f"Table-row insertion cancelled: the {label} is not "
                        f"searchable at its new position. Expected {expected!r}."
                    )
                return {
                    "text": expected,
                    "bbox": [
                        float(spec["target_bbox"].x0),
                        float(spec["target_bbox"].y0),
                        float(spec["target_bbox"].x1),
                        float(spec["target_bbox"].y1),
                    ],
                }

            inserted_lines = [
                _require_text(spec, "cloned table cell")
                for spec in template_cells
            ]
            moved_lines = [
                _require_text(spec, "moved following line")
                for spec in move_specs
            ]

            check_rules = self.horizontal_table_rules(check_page)

            def _has_rule(action, target=True):
                expected_y = float(
                    action["target_y"] if target else action["y"]
                )
                tolerance = max(
                    0.45,
                    float(action.get("width") or 0.0) * 1.75,
                )
                return any(
                    abs(float(rule["y"]) - expected_y) <= tolerance
                    and abs(float(rule["x0"]) - float(action["x0"])) <= 0.75
                    and abs(float(rule["x1"]) - float(action["x1"])) <= 0.75
                    for rule in check_rules
                )

            all_targets = [
                float(action["target_y"])
                for action in rule_actions + rule_insertions
            ]
            for action in rule_actions:
                if not _has_rule(action, target=True):
                    raise ValueError(
                        "Table-row insertion cancelled: a moved separator is missing."
                    )
                old_is_new_target = any(
                    abs(float(action["y"]) - target) <= max(
                        0.45,
                        float(action.get("width") or 0.0) * 1.75,
                    )
                    for target in all_targets
                )
                if not old_is_new_target and _has_rule(action, target=False):
                    raise ValueError(
                        "Table-row insertion cancelled: a separator remains at "
                        "its original position."
                    )
            for action in rule_insertions:
                if not _has_rule(action, target=True):
                    raise ValueError(
                        "Table-row insertion cancelled: the cloned row separator "
                        "is missing."
                    )
        finally:
            probe.close()

        inserted_bbox = _union_rects(
            [_rect(item["bbox"]) for item in inserted_lines]
        )
        moved_table_rules = [
            {
                "from": [
                    float(action["x0"]), float(action["y"]),
                    float(action["x1"]), float(action["y"]),
                ],
                "to": [
                    float(action["x0"]), float(action["target_y"]),
                    float(action["x1"]), float(action["target_y"]),
                ],
            }
            for action in rule_actions
        ]
        inserted_table_rules = [
            [
                float(action["x0"]), float(action["target_y"]),
                float(action["x1"]), float(action["target_y"]),
            ]
            for action in rule_insertions
        ]
        return {
            "position": "below",
            "column_count": len(template_cells),
            "shift_dy": shift_dy,
            "inserted_bbox": (
                tuple(float(value) for value in inserted_bbox)
                if inserted_bbox is not None
                else tuple(float(value) for value in plan.get("inserted_bbox", ()))
            ),
            "inserted_lines": inserted_lines,
            "moved_lines": moved_lines,
            "shifted_count": len(shifted),
            "moved_table_rules": moved_table_rules,
            "inserted_table_rules": inserted_table_rules,
            "moved_table_rule_count": len(moved_table_rules),
            "inserted_table_rule_count": len(inserted_table_rules),
            "immediate_validation_passed": True,
        }


    def delete_full_line_with_reflow(
        self,
        page,
        line_spans,
        shifted_items,
        table_rule_plan=None,
    ):
        """
        Delete the selected visual line and close the gap by COPYING the
        following PDF line content upward from a pre-edit snapshot.

        This path intentionally avoids the font resolver for text that already
        exists in the PDF. That prevents custom/subset font failures while
        preserving the original searchable content.

        Numbered lists are renumbered without synthesizing new glyphs:
        the prefix for each destination slot is copied from the line that
        originally occupied that slot, while the following item's body is
        copied upward from its original location.
        """
        selected = list(line_spans or [])
        shifted = list(shifted_items or [])
        table_rule_actions = list(
            (table_rule_plan or {}).get("actions") or []
        )

        if not selected:
            raise ValueError("No full line is selected.")

        doc = page.parent
        if doc is None:
            raise ValueError("The edited page is no longer attached to a PDF.")

        page_number = int(page.number)
        selected_line_text = self._line_text(selected)
        selected_line_bbox = self.line_bbox(selected)

        if not selected_line_bbox:
            raise ValueError("The selected line has no usable PDF bounds.")

        # Snapshot BEFORE any redaction. All moved content is copied from here.
        try:
            source_raw = doc.tobytes(
                garbage=0,
                clean=False,
                deflate=False,
                no_new_id=True,
            )
        except TypeError:
            source_raw = doc.tobytes(no_new_id=True)

        source_doc = pymupdf.open(stream=source_raw, filetype="pdf")

        def _rect(values):
            rect = pymupdf.Rect(
                float(values[0]),
                float(values[1]),
                float(values[2]),
                float(values[3]),
            )
            rect.normalize()
            return rect

        def _union_rects(rects):
            rects = [pymupdf.Rect(r) for r in rects if r is not None]
            if not rects:
                return None
            x0 = min(r.x0 for r in rects)
            y0 = min(r.y0 for r in rects)
            x1 = max(r.x1 for r in rects)
            y1 = max(r.y1 for r in rects)
            rect = pymupdf.Rect(x0, y0, x1, y1)
            rect.normalize()
            return rect

        def _union_item_rect(items):
            return _union_rects(
                [_rect(item["final_bbox"]) for item in items]
            )

        def _expanded(rect, xpad=3.0, ypad=3.0):
            rect = pymupdf.Rect(rect)
            rect.normalize()
            return pymupdf.Rect(
                rect.x0 - float(xpad),
                rect.y0 - float(ypad),
                rect.x1 + float(xpad),
                rect.y1 + float(ypad),
            )

        def _compact(value):
            value = (value or "").replace("\u00a0", " ")
            return re.sub(r"\s+", "", value)

        def _clip_to_page(rect, bounds):
            rect = pymupdf.Rect(rect)
            rect.normalize()
            bounds = pymupdf.Rect(bounds)
            bounds.normalize()
            rect &= bounds
            rect.normalize()
            if rect.is_empty or rect.width <= 0.001 or rect.height <= 0.001:
                return None
            return rect

        def _local_hits(source_page, needle, near_rect):
            if not needle:
                return []

            try:
                hits = list(source_page.search_for(needle))
            except Exception:
                hits = []

            if not hits:
                return []

            near = pymupdf.Rect(near_rect)
            near.normalize()

            search_band = _expanded(
                near,
                xpad=max(10.0, near.width * 0.15),
                ypad=max(6.0, near.height * 1.1),
            ) & source_page.rect

            local = []
            for hit in hits:
                hit = pymupdf.Rect(hit)
                hit.normalize()
                if hit.intersects(search_band):
                    local.append(hit)

            local.sort(
                key=lambda hit: (
                    abs(float(hit.y0) - float(near.y0)),
                    abs(float(hit.x0) - float(near.x0)),
                )
            )
            return local

        def _prefix_text(line_text):
            prefix = self._numbered_prefix(line_text)
            if not prefix:
                return ""
            return f"{int(prefix[0])}{prefix[1]}"

        def _prefix_rect_for_line(source_page, spans, line_text, line_bbox):
            """
            Find the actual rectangle of the list-number prefix in this line.
            Prefer a local PDF text-search hit; otherwise use a dedicated
            prefix-only span when the producer stored the number separately.
            """
            prefix = _prefix_text(line_text)
            if not prefix:
                return None

            local_hits = _local_hits(source_page, prefix, line_bbox)
            if local_hits:
                # Choose the left-most local hit on the same baseline.
                same_band = [
                    hit
                    for hit in local_hits
                    if abs(hit.y0 - float(line_bbox[1])) <= max(5.0, hit.height)
                ]
                pool = same_band or local_hits
                pool.sort(key=lambda hit: (hit.x0, hit.y0))
                return pymupdf.Rect(pool[0])

            ordered = sorted(
                spans,
                key=lambda span: (float(span.bbox[0]), int(span.span_index)),
            )

            for span in ordered:
                raw = (span.text or "").strip()
                if not re.match(r"^\d+\s*[.)](?:\s*|$)", raw):
                    continue

                rect = _rect(span.bbox)

                # If this span contains only the prefix, its bbox is exact.
                compact_raw = re.sub(r"\s+", "", raw)
                compact_prefix = re.sub(r"\s+", "", prefix)

                if compact_raw == compact_prefix:
                    return rect

                # Last-resort geometry fallback. Keep it conservative and only
                # use the left side of this span, never the whole body.
                char_count = max(1, len(raw))
                fraction = min(0.45, max(0.12, len(prefix) / char_count))
                width = max(2.0, rect.width * fraction)

                fallback = pymupdf.Rect(
                    rect.x0,
                    rect.y0,
                    min(rect.x1, rect.x0 + width),
                    rect.y1,
                )
                fallback.normalize()
                return fallback

            # Final fallback: use a narrow left strip of the line bbox.
            line_rect = _rect(line_bbox)
            if line_rect.width <= 0.01:
                return None

            estimated_width = min(
                max(5.0, line_rect.height * 1.7),
                max(5.0, line_rect.width * 0.20),
            )

            fallback = pymupdf.Rect(
                line_rect.x0,
                line_rect.y0,
                min(line_rect.x1, line_rect.x0 + estimated_width),
                line_rect.y1,
            )
            fallback.normalize()
            return fallback

        def _body_text(line_text):
            text = line_text or ""
            return re.sub(r"^\s*\d+\s*[.)]\s*", "", text, count=1).strip()

        def _body_rect_for_line(source_page, spans, line_text, line_bbox, prefix_rect):
            """
            Return one source rectangle containing the whole list-item body.
            Prefer the actual non-prefix spans; fall back to local text search;
            finally use the geometric remainder of the line.
            """
            body = _body_text(line_text)
            if not body:
                return None

            ordered = sorted(
                spans,
                key=lambda span: (float(span.bbox[0]), int(span.span_index)),
            )

            prefix_right = (
                float(prefix_rect.x1)
                if prefix_rect is not None
                else float(line_bbox[0])
            )

            candidate_rects = []
            for span in ordered:
                rect = _rect(span.bbox)
                raw = (span.text or "").strip()

                # Skip a dedicated number-only span.
                if re.fullmatch(r"\d+\s*[.)]", raw):
                    continue

                # Any span clearly to the right of the number is body content.
                if rect.x1 > prefix_right + 0.15:
                    candidate_rects.append(rect)

            if candidate_rects:
                union = _union_rects(candidate_rects)
                if union is not None and union.width > 0.05:
                    return union

            # Search for the body itself inside this exact line band.
            body_hits = _local_hits(source_page, body, line_bbox)
            if body_hits:
                line_rect = _rect(line_bbox)
                local = [
                    hit
                    for hit in body_hits
                    if hit.intersects(_expanded(line_rect, 4.0, 3.0))
                ]
                if local:
                    union = _union_rects(local)
                    if union is not None and union.width > 0.05:
                        return union

            # Geometry fallback: everything after the prefix.
            line_rect = _rect(line_bbox)
            x0 = min(
                line_rect.x1,
                max(
                    line_rect.x0,
                    prefix_right + 0.05,
                ),
            )

            if line_rect.x1 - x0 <= 0.01:
                return None

            fallback = pymupdf.Rect(
                x0,
                line_rect.y0,
                line_rect.x1,
                line_rect.y1,
            )
            fallback.normalize()
            return fallback

        def _show_exact_clip(source_page, source_rect, target_rect, label="line"):
            """
            Copy exact existing PDF content from snapshot -> edited page.

            Normalize rectangles first. The previous implementation rejected
            perfectly valid very-small rectangles at <= 0.2 pt; PyMuPDF can
            handle much smaller non-empty clips, so only truly empty clips fail.
            """
            source_rect = _clip_to_page(source_rect, source_page.rect)
            target_rect = _clip_to_page(target_rect, page.rect)

            if source_rect is None or target_rect is None:
                raise ValueError(
                    f"The {label} could not be moved because its PDF clip "
                    "rectangle is empty after page-boundary normalization."
                )

            page.show_pdf_page(
                target_rect,
                source_doc,
                page_number,
                clip=source_rect,
                keep_proportion=False,
                overlay=True,
            )

        def _table_rule_redaction_rect(action, bounds):
            stroke_width = max(0.01, float(action.get("width") or 0.0))
            endpoint_pad = max(0.35, stroke_width * 1.5)
            vertical_pad = max(0.35, stroke_width * 1.5)
            rect = pymupdf.Rect(
                float(action["x0"]) - endpoint_pad,
                float(action["y"]) - vertical_pad,
                float(action["x1"]) + endpoint_pad,
                float(action["y"]) + vertical_pad,
            ) & bounds
            rect.normalize()
            return rect

        try:
            source_page = source_doc[page_number]

            # ---------------------------------------------------------
            # Group moving spans by their ORIGINAL visual line.
            # ---------------------------------------------------------
            shifted_line_groups = {}

            for item in shifted:
                span = item["span"]
                key = tuple(
                    item.get("line_key")
                    or (
                        int(span.block_index),
                        int(span.line_index),
                    )
                )
                shifted_line_groups.setdefault(key, []).append(item)

            group_records = []

            for _, items in shifted_line_groups.items():
                items = sorted(
                    items,
                    key=lambda value: (
                        float(value["span"].bbox[0]),
                        int(value["span"].span_index),
                    ),
                )

                original_spans = [item["span"] for item in items]
                original_bbox = _rect(self.line_bbox(original_spans))
                final_bbox = _union_item_rect(items)
                original_text = self._line_text(original_spans)
                replacement_text = " ".join(
                    str(item.get("text") or "").strip()
                    for item in items
                    if str(item.get("text") or "").strip()
                ).strip()

                group_records.append(
                    {
                        "items": items,
                        "original_spans": original_spans,
                        "original_bbox": original_bbox,
                        "final_bbox": final_bbox,
                        "original_text": original_text,
                        "replacement_text": replacement_text,
                    }
                )

            group_records.sort(
                key=lambda item: (
                    float(item["original_bbox"].y0),
                    float(item["original_bbox"].x0),
                )
            )

            line_specs = []

            # For numbered reflow, the desired prefix for the first moved line
            # comes from the deleted slot; for every later moved line it comes
            # from the immediately previous original line. This removes the
            # unreliable "search anywhere on page for 2./3./4." behavior.
            previous_slot_spans = selected
            previous_slot_text = selected_line_text
            previous_slot_bbox = _rect(selected_line_bbox)

            for record in group_records:
                original_spans = record["original_spans"]
                original_bbox = record["original_bbox"]
                final_bbox = record["final_bbox"]
                original_text = record["original_text"]
                replacement_text = record["replacement_text"]

                old_prefix = self._numbered_prefix(original_text)
                new_prefix = self._numbered_prefix(replacement_text)

                spec = {
                    "original_text": original_text,
                    "replacement_text": replacement_text,
                    "original_bbox": original_bbox,
                    "final_bbox": final_bbox,
                    "kind": "whole",
                }

                if old_prefix and new_prefix and old_prefix != new_prefix:
                    # Clean renumbering by clipping is safe only when the PDF
                    # producer stored the list number as its own text span.
                    # If number + body live in one text object, clipping only the
                    # body still leaves the original number in the searchable
                    # text object. In that case, move the exact whole line upward
                    # unchanged rather than failing the deletion.
                    dedicated_prefix_span = any(
                        re.fullmatch(
                            r"\s*\d+\s*[.)]\s*",
                            str(span.text or ""),
                        )
                        for span in original_spans
                    )

                    if not dedicated_prefix_span:
                        spec["replacement_text"] = original_text
                        spec["renumber_skipped"] = True
                        line_specs.append(spec)

                        previous_slot_spans = original_spans
                        previous_slot_text = original_text
                        previous_slot_bbox = original_bbox
                        continue

                    old_prefix_rect = _prefix_rect_for_line(
                        source_page,
                        original_spans,
                        original_text,
                        original_bbox,
                    )

                    desired_prefix_rect = _prefix_rect_for_line(
                        source_page,
                        previous_slot_spans,
                        previous_slot_text,
                        previous_slot_bbox,
                    )

                    if old_prefix_rect is None or desired_prefix_rect is None:
                        raise ValueError(
                            "The numbered line could not be reflowed because its "
                            "list-number prefix could not be located safely."
                        )

                    body_source = _body_rect_for_line(
                        source_page,
                        original_spans,
                        original_text,
                        original_bbox,
                        old_prefix_rect,
                    )

                    dy = float(final_bbox.y0) - float(original_bbox.y0)

                    body_target = None
                    if body_source is not None:
                        body_target = pymupdf.Rect(
                            body_source.x0,
                            body_source.y0 + dy,
                            body_source.x1,
                            body_source.y1 + dy,
                        )

                    spec.update(
                        {
                            "kind": "numbered",
                            "prefix_source": pymupdf.Rect(desired_prefix_rect),
                            # The desired prefix already exists in the snapshot
                            # at the exact destination slot.
                            "prefix_target": pymupdf.Rect(desired_prefix_rect),
                            "body_source": body_source,
                            "body_target": body_target,
                            "body_text": _body_text(original_text),
                        }
                    )

                line_specs.append(spec)

                # This original line becomes the source of the next target-slot
                # prefix (e.g. 3. supplies the prefix for old 4. -> new 3.).
                previous_slot_spans = original_spans
                previous_slot_text = original_text
                previous_slot_bbox = original_bbox

            # ``show_pdf_page`` copies the complete source display list behind
            # each text clip. Remove the acted-on rules from this *temporary*
            # snapshot first; otherwise every copied cell would embed a hidden
            # duplicate of each rule even though the clip makes it invisible.
            if table_rule_actions:
                for action in table_rule_actions:
                    rule_rect = _table_rule_redaction_rect(
                        action,
                        source_page.rect,
                    )
                    if rule_rect.is_empty:
                        continue
                    source_page.add_redact_annot(
                        rule_rect,
                        fill=None,
                        cross_out=False,
                    )

                source_page.apply_redactions(
                    images=pymupdf.PDF_REDACT_IMAGE_NONE,
                    graphics=pymupdf.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED,
                    text=pymupdf.PDF_REDACT_TEXT_NONE,
                )

            # ---------------------------------------------------------
            # Destructively remove the selected line AND every original line
            # that is going to be moved. Apply redactions only once.
            # ---------------------------------------------------------
            seen_rects = set()

            for span in selected + [item["span"] for item in shifted]:
                rect = _rect(span.bbox)

                # Tiny padding catches text whose glyph ink extends fractionally
                # outside the extracted bbox, while staying safely inside the
                # same line band.
                rect = pymupdf.Rect(
                    rect.x0 - 0.15,
                    rect.y0 - 0.10,
                    rect.x1 + 0.15,
                    rect.y1 + 0.10,
                ) & page.rect

                rect_tuple = tuple(round(float(v), 4) for v in rect)
                if rect_tuple in seen_rects or rect.is_empty:
                    continue

                seen_rects.add(rect_tuple)

                page.add_redact_annot(
                    rect,
                    fill=None,
                    cross_out=False,
                )

            page.apply_redactions(
                images=pymupdf.PDF_REDACT_IMAGE_NONE,
                graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                text=pymupdf.PDF_REDACT_TEXT_REMOVE,
            )

            # ---------------------------------------------------------
            # Remove only the independently stroked table-rule paths in the
            # plan. This is a separate redaction pass so the text rectangles
            # above can never delete unrelated vector artwork. ``COVERED`` is
            # used instead of ``TOUCHED`` as an additional safety boundary.
            # ---------------------------------------------------------
            if table_rule_actions:
                for action in table_rule_actions:
                    rule_rect = _table_rule_redaction_rect(action, page.rect)

                    if rule_rect.is_empty:
                        continue

                    page.add_redact_annot(
                        rule_rect,
                        fill=None,
                        cross_out=False,
                    )

                page.apply_redactions(
                    images=pymupdf.PDF_REDACT_IMAGE_NONE,
                    graphics=pymupdf.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED,
                    text=pymupdf.PDF_REDACT_TEXT_NONE,
                )

                for action in table_rule_actions:
                    if action.get("action") != "move":
                        continue

                    target_y = float(action["target_y"])
                    page.draw_line(
                        pymupdf.Point(float(action["x0"]), target_y),
                        pymupdf.Point(float(action["x1"]), target_y),
                        color=tuple(action.get("color") or (0.0,)),
                        dashes=action.get("dashes"),
                        width=max(0.01, float(action.get("width") or 0.0)),
                        lineCap=self._drawing_line_cap(action.get("line_cap")),
                        lineJoin=int(action.get("line_join") or 0),
                        overlay=True,
                        stroke_opacity=float(
                            action.get("stroke_opacity")
                            if action.get("stroke_opacity") is not None
                            else 1.0
                        ),
                    )

            # ---------------------------------------------------------
            # Copy original PDF content upward. No font resolver involved.
            # ---------------------------------------------------------
            for spec in line_specs:
                if spec["kind"] == "numbered":
                    _show_exact_clip(
                        source_page,
                        spec["prefix_source"],
                        spec["prefix_target"],
                        label="list-number prefix",
                    )

                    if spec["body_source"] is not None:
                        _show_exact_clip(
                            source_page,
                            spec["body_source"],
                            spec["body_target"],
                            label="list-item body",
                        )
                    elif spec.get("body_text"):
                        raise ValueError(
                            "The numbered line body could not be located in the "
                            "original PDF content."
                        )
                else:
                    _show_exact_clip(
                        source_page,
                        spec["original_bbox"],
                        spec["final_bbox"],
                        label="line",
                    )

        finally:
            source_doc.close()

        # -------------------------------------------------------------
        # IMMEDIATE VALIDATION
        # -------------------------------------------------------------
        try:
            check_raw = doc.tobytes(
                garbage=0,
                clean=False,
                deflate=False,
                no_new_id=True,
            )
        except TypeError:
            check_raw = doc.tobytes(no_new_id=True)

        probe = pymupdf.open(stream=check_raw, filetype="pdf")

        try:
            check_page = probe[page_number]

            deleted_rect = _expanded(
                selected_line_bbox,
                xpad=4.0,
                ypad=max(
                    4.0,
                    max(float(span.size) for span in selected) * 0.8,
                ),
            ) & check_page.rect

            deleted_region = check_page.get_text("text", clip=deleted_rect)

            # If the next row is an identical clone, correct upward reflow
            # deliberately places the same searchable text in the deleted
            # slot. In that case text absence is the wrong invariant. Require
            # exactly the planned successor content in the slot instead; the
            # moved-line checks below still verify every copied cell.
            slot_specs = []
            selected_rect = _rect(selected_line_bbox)
            for spec in line_specs:
                final_rect = pymupdf.Rect(spec["final_bbox"])
                intersection = final_rect & selected_rect
                if intersection.is_empty:
                    continue
                overlap_height = max(0.0, float(intersection.height))
                minimum_height = max(
                    0.01,
                    min(float(final_rect.height), float(selected_rect.height)),
                )
                if overlap_height / minimum_height >= 0.50:
                    slot_specs.append(spec)

            slot_specs.sort(
                key=lambda spec: (
                    float(spec["final_bbox"].x0),
                    float(spec["final_bbox"].y0),
                )
            )
            slot_text = " ".join(
                str(spec.get("replacement_text") or "").strip()
                for spec in slot_specs
                if str(spec.get("replacement_text") or "").strip()
            ).strip()
            identical_successor_in_deleted_slot = bool(
                _compact(selected_line_text)
                and _compact(slot_text) == _compact(selected_line_text)
            )

            if (
                _compact(selected_line_text)
                and _compact(selected_line_text) in _compact(deleted_region)
                and not identical_successor_in_deleted_slot
            ):
                raise ValueError(
                    "Line deletion cancelled: the complete original line is still "
                    "searchable in the deleted line position."
                )

            if identical_successor_in_deleted_slot:
                region_compact = _compact(deleted_region)
                expected_counts = {}
                for spec in slot_specs:
                    value = _compact(spec.get("replacement_text") or "")
                    if value:
                        expected_counts[value] = expected_counts.get(value, 0) + 1
                for value, expected_count in expected_counts.items():
                    if region_compact.count(value) > expected_count:
                        raise ValueError(
                            "Line deletion cancelled: duplicate searchable copies "
                            "remain in the deleted row position."
                        )

            moved_lines = []

            for spec in line_specs:
                expected = spec["replacement_text"]
                final_rect = _expanded(
                    spec["final_bbox"],
                    xpad=5.0,
                    ypad=max(
                        5.0,
                        float(spec["final_bbox"].height) * 0.8,
                    ),
                ) & check_page.rect

                region_text = check_page.get_text("text", clip=final_rect)

                expected_compact = _compact(expected)
                region_compact = _compact(region_text)

                present = bool(
                    expected_compact
                    and expected_compact in region_compact
                )

                if not present and expected:
                    try:
                        hits = check_page.search_for(expected)
                    except Exception:
                        hits = []

                    present = any(
                        hit.intersects(final_rect)
                        for hit in hits
                    )

                if not present:
                    raise ValueError(
                        "Line reflow cancelled: the moved line is not searchable "
                        "at its new position. "
                        f"Expected line: {expected!r}"
                    )

                moved_lines.append(
                    {
                        "text": expected,
                        "bbox": [
                            float(spec["final_bbox"].x0),
                            float(spec["final_bbox"].y0),
                            float(spec["final_bbox"].x1),
                            float(spec["final_bbox"].y1),
                        ],
                    }
                )

            # Confirm every requested rule move is present and that an old
            # position is gone unless another moved rule intentionally lands
            # on that same coordinate.
            check_rules = self.horizontal_table_rules(check_page)
            moved_targets = [
                action
                for action in table_rule_actions
                if action.get("action") == "move"
            ]

            def _same_rule_position(rule, action, target=False):
                expected_y = (
                    float(action["target_y"])
                    if target
                    else float(action["y"])
                )
                tolerance = max(
                    0.45,
                    float(action.get("width") or 0.0) * 1.75,
                )
                return (
                    abs(float(rule["y"]) - expected_y) <= tolerance
                    and abs(float(rule["x0"]) - float(action["x0"])) <= 0.75
                    and abs(float(rule["x1"]) - float(action["x1"])) <= 0.75
                )

            for action in table_rule_actions:
                if action.get("action") == "move" and not any(
                    _same_rule_position(rule, action, target=True)
                    for rule in check_rules
                ):
                    raise ValueError(
                        "Line reflow cancelled: a table rule was not restored "
                        "at its moved position."
                    )

                target_occupies_old_position = any(
                    abs(float(target["target_y"]) - float(action["y"]))
                    <= max(
                        0.45,
                        float(action.get("width") or 0.0) * 1.75,
                    )
                    and abs(float(target["x0"]) - float(action["x0"])) <= 0.75
                    and abs(float(target["x1"]) - float(action["x1"])) <= 0.75
                    for target in moved_targets
                )

                if not target_occupies_old_position and any(
                    _same_rule_position(rule, action, target=False)
                    for rule in check_rules
                ):
                    raise ValueError(
                        "Line reflow cancelled: an original table rule remains "
                        "at its old position."
                    )

        finally:
            probe.close()

        moved_table_rules = [
            {
                "from": [
                    float(action["x0"]),
                    float(action["y"]),
                    float(action["x1"]),
                    float(action["y"]),
                ],
                "to": [
                    float(action["x0"]),
                    float(action["target_y"]),
                    float(action["x1"]),
                    float(action["target_y"]),
                ],
            }
            for action in table_rule_actions
            if action.get("action") == "move"
        ]
        removed_table_rules = [
            [
                float(action["x0"]),
                float(action["y"]),
                float(action["x1"]),
                float(action["y"]),
            ]
            for action in table_rule_actions
            if action.get("action") == "remove"
        ]

        return {
            "deleted_bbox": tuple(float(v) for v in selected_line_bbox),
            "deleted_text": selected_line_text,
            "shifted_count": len(shifted),
            "moved_lines": moved_lines,
            "renumbered_count": sum(
                1
                for spec in line_specs
                if spec.get("kind") == "numbered"
            ),
            "moved_table_rules": moved_table_rules,
            "removed_table_rules": removed_table_rules,
        }

    def delete_full_line(self, page, line_spans):
        """Destructively remove all text spans in one visual line without reflowing other lines."""
        spans = list(line_spans or [])
        if not spans:
            raise ValueError("No full line is selected.")

        for span in spans:
            page.add_redact_annot(
                pymupdf.Rect(span.bbox),
                fill=None,
                cross_out=False,
            )

        page.apply_redactions(
            images=pymupdf.PDF_REDACT_IMAGE_NONE,
            graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
            text=pymupdf.PDF_REDACT_TEXT_REMOVE,
        )

        # Validate every removed span immediately. Empty replacement means delete-only.
        for span in spans:
            self._validate_replacement_now(
                page,
                original_text=span.text,
                replacement_text="",
                original_bbox=span.bbox,
                final_bbox=span.bbox,
                font_size=span.size,
            )

        return self.line_bbox(spans)

    def plan_horizontal_expansion(
        self,
        page,
        all_spans,
        selected_span: TextSpan,
        new_width: float,
        expansion_direction: str = "right",
    ):
        """
        Plan a single-line width expansion.

        ``right`` (default):
            keep the original left edge fixed and grow to the right.

        ``left``:
            keep the original right edge fixed and grow to the left.

        Tightly connected spans on the chosen expansion side may be shifted by
        the same amount. If the movement would collide with a separate field or
        leave the physical page, the plan is rejected.
        """
        direction = str(expansion_direction or "right").strip().lower()
        if direction not in {"right", "left"}:
            direction = "right"

        old_x0, old_y0, old_x1, old_y1 = selected_span.bbox
        old_width = old_x1 - old_x0

        if new_width is None:
            new_width = old_width

        expanded_width = max(old_width, float(new_width))
        extra = max(0.0, expanded_width - old_width)

        if direction == "left":
            expanded_bbox = (
                old_x1 - expanded_width,
                old_y0,
                old_x1,
                old_y1,
            )
            replacement_origin = (
                selected_span.origin[0] - extra,
                selected_span.origin[1],
            )
        else:
            expanded_bbox = (
                old_x0,
                old_y0,
                old_x0 + expanded_width,
                old_y1,
            )
            replacement_origin = selected_span.origin

        if extra <= 0.01:
            return {
                "ok": True,
                "expanded_bbox": expanded_bbox,
                "replacement_origin": replacement_origin,
                "extra_width": 0.0,
                "shifts": [],
                "direction": direction,
                "reason": "Replacement fits inside the original width.",
            }

        cluster_gap_limit = max(16.0, selected_span.size * 2.25)
        safe_gap = 1.0

        if direction == "left":
            left_spans = []
            for span in all_spans:
                if self._same_span(span, selected_span):
                    continue
                if not self._same_line(span, selected_span):
                    continue
                if span.bbox[2] > old_x0 + 0.35:
                    continue
                left_spans.append(span)

            # Closest span on the left first.
            left_spans.sort(
                key=lambda s: (s.bbox[2], s.bbox[1]),
                reverse=True,
            )

            continuation = []
            next_separate = None
            previous_old_start = old_x0

            for span in left_spans:
                gap = previous_old_start - span.bbox[2]

                if gap <= cluster_gap_limit:
                    continuation.append(span)
                    previous_old_start = min(
                        previous_old_start,
                        span.bbox[0],
                    )
                else:
                    next_separate = span
                    break

            shifts = [
                (span, -extra)
                for span in continuation
            ]

            if continuation:
                cluster_new_start = min(
                    span.bbox[0] - extra
                    for span in continuation
                )
            else:
                cluster_new_start = expanded_bbox[0]

            if next_separate is not None:
                if cluster_new_start - safe_gap < next_separate.bbox[2]:
                    return {
                        "ok": False,
                        "fallback": "none",
                        "expanded_bbox": expanded_bbox,
                        "replacement_origin": replacement_origin,
                        "extra_width": extra,
                        "shifts": [],
                        "direction": direction,
                        "reason": (
                            "Left expansion reaches another text field. "
                            "Choose Move Right or use a shorter replacement."
                        ),
                        "blocking_span": next_separate,
                    }

            page_left = float(page.rect.x0)
            left_margin = 3.0
            if cluster_new_start < page_left + left_margin:
                return {
                    "ok": False,
                    "fallback": "none",
                    "expanded_bbox": expanded_bbox,
                    "replacement_origin": replacement_origin,
                    "extra_width": extra,
                    "shifts": [],
                    "direction": direction,
                    "reason": (
                        "Left expansion reaches the page edge. "
                        "Choose Move Right or use a shorter replacement."
                    ),
                }

            if continuation:
                reason = (
                    f"Expanded left by {extra:.2f} pt and shifted "
                    f"{len(continuation)} preceding span(s) left by the same amount."
                )
            else:
                reason = (
                    f"Expanded left by {extra:.2f} pt into available blank space. "
                    "The original right edge stayed fixed."
                )

            return {
                "ok": True,
                "expanded_bbox": expanded_bbox,
                "replacement_origin": replacement_origin,
                "extra_width": extra,
                "shifts": shifts,
                "direction": direction,
                "reason": reason,
            }

        # Default / Move Right behaviour.
        right_spans = []
        for span in all_spans:
            if self._same_span(span, selected_span):
                continue
            if not self._same_line(span, selected_span):
                continue
            if span.bbox[0] < old_x1 - 0.35:
                continue
            right_spans.append(span)

        right_spans.sort(key=lambda s: (s.bbox[0], s.bbox[1]))

        continuation = []
        next_separate = None
        previous_old_end = old_x1

        for span in right_spans:
            gap = span.bbox[0] - previous_old_end

            if gap <= cluster_gap_limit:
                continuation.append(span)
                previous_old_end = max(previous_old_end, span.bbox[2])
            else:
                next_separate = span
                break

        shifts = [(span, extra) for span in continuation]

        if continuation:
            cluster_new_end = max(
                span.bbox[2] + extra
                for span in continuation
            )
        else:
            cluster_new_end = expanded_bbox[2]

        if next_separate is not None:
            if cluster_new_end + safe_gap > next_separate.bbox[0]:
                return {
                    "ok": False,
                    "fallback": "multiline",
                    "expanded_bbox": expanded_bbox,
                    "replacement_origin": replacement_origin,
                    "extra_width": extra,
                    "shifts": [],
                    "direction": direction,
                    "reason": (
                        "Horizontal expansion reaches another text field. "
                        "Multiline paragraph reflow is required."
                    ),
                    "blocking_span": next_separate,
                }

        page_right = float(page.rect.x1)
        right_margin = 3.0
        if cluster_new_end > page_right - right_margin:
            return {
                "ok": False,
                "fallback": "multiline",
                "expanded_bbox": expanded_bbox,
                "replacement_origin": replacement_origin,
                "extra_width": extra,
                "shifts": [],
                "direction": direction,
                "reason": (
                    "Horizontal expansion reaches the page edge. "
                    "Multiline paragraph reflow is required."
                ),
            }

        if continuation:
            reason = (
                f"Expanded right by {extra:.2f} pt and shifted "
                f"{len(continuation)} following span(s) right by the same amount."
            )
        else:
            reason = (
                f"Expanded right by {extra:.2f} pt into available blank space. "
                "The original left edge stayed fixed."
            )

        return {
            "ok": True,
            "expanded_bbox": expanded_bbox,
            "replacement_origin": replacement_origin,
            "extra_width": extra,
            "shifts": shifts,
            "direction": direction,
            "reason": reason,
        }

    # ---------------------------------------------------------------------
    # MULTILINE REFLOW
    # ---------------------------------------------------------------------

    def _paragraph_reflow_spans(self, all_spans, selected_span):
        """
        Collect the selected span plus all later spans from the same PDF text block.

        This is intentionally block-based. PDF->text extraction normally groups
        a paragraph into one block, so this is much safer than guessing only by
        visual proximity.
        """
        if selected_span.block_index < 0:
            return []

        result = []

        for span in all_spans:
            if span.block_index != selected_span.block_index:
                continue

            if span.line_index < selected_span.line_index:
                continue

            if span.line_index == selected_span.line_index:
                if span.span_index < selected_span.span_index:
                    continue

            result.append(span)

        result.sort(
            key=lambda s: (
                s.line_index,
                s.span_index,
                s.bbox[0],
            )
        )
        return result

    @staticmethod
    def _block_spans(all_spans, block_index):
        result = [s for s in all_spans if s.block_index == block_index]
        result.sort(key=lambda s: (s.line_index, s.span_index, s.bbox[0]))
        return result

    @staticmethod
    def _line_spacing(spans, selected_span):
        baselines = []

        by_line = {}
        for span in spans:
            by_line.setdefault(span.line_index, []).append(span.origin[1])

        for line_index in sorted(by_line):
            values = by_line[line_index]
            if values:
                baselines.append(sum(values) / len(values))

        diffs = []
        for a, b in zip(baselines, baselines[1:]):
            delta = b - a
            if delta > 0.5:
                diffs.append(delta)

        if diffs:
            return max(selected_span.size * 1.05, median(diffs))

        return selected_span.size * 1.22

    @staticmethod
    def _tokenize_span_text(text):
        """
        Split into word-ish chunks while keeping spaces attached.
        Attached spaces make text reflow look natural while preserving the
        distinction between differently styled spans.
        """
        if not text:
            return []

        tokens = re.findall(r"\S+\s*|\s+", text)
        return tokens or [text]

    def build_multiline_reflow_plan(
        self,
        page,
        all_spans,
        selected_span,
        replacement,
        selected_candidate,
        candidate_by_span_key,
    ):
        """
        Reflow selected text + the rest of its PDF text block.

        It preserves each span's own font/color/style while allowing words to
        wrap onto new lines. If the paragraph grows vertically, later text in
        the same column is shifted down automatically.

        candidate_by_span_key:
          dictionary keyed by (block_index, line_index, span_index)
          for all non-selected spans included in reflow.
        """
        paragraph_spans = self._paragraph_reflow_spans(
            all_spans,
            selected_span,
        )

        if not paragraph_spans:
            return {
                "ok": False,
                "reason": (
                    "The PDF did not expose enough paragraph structure for "
                    "safe multiline reflow."
                ),
            }

        whole_block = self._block_spans(
            all_spans,
            selected_span.block_index,
        )

        # Paragraph / column boundaries come from the original PDF block.
        block_left = min(s.bbox[0] for s in whole_block)
        block_right = max(s.bbox[2] for s in whole_block)

        # For the very first reflow line, preserve the selected span's current X.
        first_line_left = selected_span.bbox[0]

        # Avoid using the page edge itself as text width.
        page_right = float(page.rect.x1)
        block_right = min(block_right, page_right - 3.0)

        if block_right - block_left < selected_span.size * 5:
            return {
                "ok": False,
                "reason": "The detected paragraph column is too narrow to reflow safely.",
            }

        line_spacing = self._line_spacing(
            whole_block,
            selected_span,
        )

        # Candidate lookup helper.
        def key_for(span):
            return (
                span.block_index,
                span.line_index,
                span.span_index,
            )

        # Build styled token stream.
        stream = []

        for span in paragraph_spans:
            if self._same_span(span, selected_span):
                text = replacement
                candidate = selected_candidate
            else:
                text = span.text
                candidate = candidate_by_span_key.get(key_for(span))

            if candidate is None:
                return {
                    "ok": False,
                    "reason": (
                        "Could not resolve an exact font for paragraph continuation: "
                        f"{span.text[:80]}"
                    ),
                }

            tokens = self._tokenize_span_text(text)

            for token in tokens:
                stream.append({
                    "text": token,
                    "source_span": span,
                    "candidate": candidate,
                    "selected": self._same_span(span, selected_span),
                })

        if not stream:
            return {
                "ok": False,
                "reason": "There is no text to reflow.",
            }

        # Start at the selected baseline.
        current_baseline = selected_span.origin[1]
        current_x = first_line_left
        line_left = first_line_left
        line_number = 0

        laid_out = []
        max_x = current_x
        max_baseline = current_baseline

        for item in stream:
            token = item["text"]
            candidate = item["candidate"]
            span = item["source_span"]

            # Remove pure leading whitespace at a wrapped line start.
            if current_x <= line_left + 0.01 and token.isspace():
                continue

            width = self.replacement_width(
                candidate,
                token,
                span.size,
            )

            if width is None:
                return {
                    "ok": False,
                    "reason": f"Could not measure text token: {token!r}",
                }

            # If this token does not fit, move it to a new line.
            if (
                current_x > line_left + 0.01
                and current_x + width > block_right
            ):
                line_number += 1
                current_baseline += line_spacing
                line_left = block_left
                current_x = line_left

                # Trim leading spaces after wrapping.
                token = token.lstrip()
                if not token:
                    continue

                width = self.replacement_width(
                    candidate,
                    token,
                    span.size,
                )
                if width is None:
                    return {
                        "ok": False,
                        "reason": f"Could not measure wrapped token: {token!r}",
                    }

            # A single word itself may be wider than the full line.
            if current_x + width > block_right + 0.5:
                return {
                    "ok": False,
                    "reason": (
                        "One replacement word is wider than the paragraph column "
                        "at the exact original font size."
                    ),
                }

            laid_out.append({
                "text": token,
                "origin": (
                    current_x,
                    current_baseline,
                ),
                "source_span": span,
                "candidate": candidate,
                "selected": item["selected"],
            })

            current_x += width
            max_x = max(max_x, current_x)
            max_baseline = max(max_baseline, current_baseline)

        original_bottom = max(s.bbox[3] for s in paragraph_spans)
        original_last_baseline = max(s.origin[1] for s in paragraph_spans)

        # Use the original descent / bottom relationship.
        descent = max(
            0.5,
            original_bottom - original_last_baseline,
        )
        new_bottom = max_baseline + descent
        growth = max(0.0, new_bottom - original_bottom)

        # Find all text below this paragraph that belongs to the same visual column.
        # It only moves when the paragraph actually grows.
        shift_below = []

        if growth > 0.05:
            column_left = block_left - 2.0
            column_right = block_right + 2.0

            for span in all_spans:
                if span.block_index == selected_span.block_index:
                    continue

                if span.bbox[1] < original_bottom - 0.25:
                    continue

                overlap = max(
                    0.0,
                    min(column_right, span.bbox[2])
                    - max(column_left, span.bbox[0]),
                )

                if overlap <= 0:
                    continue

                # Do not move tiny page numbers / far-right footer items unless
                # they genuinely lie inside the paragraph column.
                span_width = max(0.1, span.bbox[2] - span.bbox[0])
                if overlap / span_width < 0.35:
                    continue

                shift_below.append(span)

            # Verify shifted text stays on the physical page.
            if shift_below:
                shifted_bottom = max(
                    s.bbox[3] + growth
                    for s in shift_below
                )
            else:
                shifted_bottom = new_bottom

            if shifted_bottom > float(page.rect.y1) - 3.0:
                return {
                    "ok": False,
                    "reason": (
                        "The paragraph needs an extra line, but there is not enough "
                        "vertical space before the bottom of the page."
                    ),
                }

        selected_positions = [
            item for item in laid_out
            if item["selected"] and item["text"].strip()
        ]

        if selected_positions:
            sx0 = min(item["origin"][0] for item in selected_positions)
            sy_baselines = [item["origin"][1] for item in selected_positions]
            sx1 = max(
                item["origin"][0]
                + (
                    self.replacement_width(
                        item["candidate"],
                        item["text"],
                        item["source_span"].size,
                    )
                    or 0.0
                )
                for item in selected_positions
            )

            # Selection box covers all wrapped replacement lines.
            selected_top = selected_span.bbox[1]
            selected_bottom = (
                max(sy_baselines)
                + max(0.5, selected_span.bbox[3] - selected_span.origin[1])
            )
            selected_bbox = (
                sx0,
                selected_top,
                sx1,
                selected_bottom,
            )
        else:
            selected_bbox = selected_span.bbox

        return {
            "ok": True,
            "paragraph_spans": paragraph_spans,
            "laid_out": laid_out,
            "growth": growth,
            "shift_below": shift_below,
            "selected_bbox": selected_bbox,
            "line_spacing": line_spacing,
            "reason": (
                f"Wrapped paragraph to {line_number + 1} line(s) from the edit point. "
                f"Container height increased by {growth:.2f} pt."
            ),
        }

    # ---------------------------------------------------------------------
    # FONT INSERTION / EDIT OPERATIONS
    # ---------------------------------------------------------------------

    def _attach_existing_pdf_font_resource(self, page, candidate, alias=None):
        """Ensure an existing PDF font xref is addressable from this page.

        PyMuPDF's ``insert_text`` first looks up ``fontname`` in the page's
        current /Resources/Font dictionary. After redaction / page rewriting,
        some PDFs can lose the symbolic resource name even though the font xref
        itself is still valid. In that situation PyMuPDF falls through to
        creating a new font and raises ``need font file or buffer`` because we
        intentionally do not have a TTF/OTF file.

        Re-attaching the already-existing font xref is exact: no font
        substitution or re-embedding is performed.
        """
        refname = (alias or candidate.pdf_refname or "").strip().lstrip("/")
        font_xref = int(candidate.pdf_xref or 0)
        if not refname or font_xref <= 0:
            return None

        doc = page.parent
        if doc is None:
            return None

        # If the exact resource name is already present, nothing to repair.
        try:
            for item in page.get_fonts(full=True):
                if len(item) >= 5 and str(item[4]).lstrip("/") == refname:
                    return refname
        except Exception:
            pass

        target = f"{font_xref} 0 R"

        try:
            resources_type, resources_value = doc.xref_get_key(
                page.xref,
                "Resources",
            )

            if resources_type == "xref":
                resources_xref = int(str(resources_value).split()[0])
                font_type, font_value = doc.xref_get_key(
                    resources_xref,
                    "Font",
                )

                if font_type == "xref":
                    font_dict_xref = int(str(font_value).split()[0])
                    doc.xref_set_key(font_dict_xref, refname, target)
                elif font_type == "dict":
                    doc.xref_set_key(
                        resources_xref,
                        f"Font/{refname}",
                        target,
                    )
                else:
                    doc.xref_set_key(
                        resources_xref,
                        "Font",
                        f"<< /{refname} {target} >>",
                    )

            elif resources_type == "dict":
                font_type, font_value = doc.xref_get_key(
                    page.xref,
                    "Resources/Font",
                )

                if font_type == "xref":
                    font_dict_xref = int(str(font_value).split()[0])
                    doc.xref_set_key(font_dict_xref, refname, target)
                elif font_type == "dict":
                    doc.xref_set_key(
                        page.xref,
                        f"Resources/Font/{refname}",
                        target,
                    )
                else:
                    doc.xref_set_key(
                        page.xref,
                        "Resources/Font",
                        f"<< /{refname} {target} >>",
                    )
            else:
                # Do not replace an inherited /Resources dictionary blindly;
                # that could drop images, graphics states, color spaces, etc.
                return None

            # Verify the alias is now visible to PyMuPDF's font lookup.
            for item in page.get_fonts(full=True):
                if len(item) >= 5 and str(item[4]).lstrip("/") == refname:
                    return refname
        except Exception:
            return None

        return None

    def _insert_text(
        self,
        page,
        span: TextSpan,
        text: str,
        candidate: FontCandidate,
        origin=None,
    ):
        if not text:
            return

        if candidate.glyph_candidates:
            insert_origin = origin if origin is not None else span.origin
            current_x, current_y = map(float, insert_origin)
            direction_x, direction_y = map(float, span.direction or (1.0, 0.0))
            magnitude = (direction_x * direction_x + direction_y * direction_y) ** 0.5
            if magnitude <= 1e-9:
                direction_x, direction_y, magnitude = 1.0, 0.0, 1.0
            direction_x /= magnitude
            direction_y /= magnitude

            runs = []
            previous_candidate = None
            for char in text:
                subcandidate = candidate.glyph_candidates.get(char)
                if subcandidate is None and char.isspace():
                    subcandidate = previous_candidate or next(
                        iter(candidate.glyph_candidates.values()),
                        None,
                    )
                if subcandidate is None:
                    raise ValueError(
                        f"Composite PDF font has no verified resource for {char!r}."
                    )
                if runs and runs[-1][0] is subcandidate:
                    runs[-1] = (subcandidate, runs[-1][1] + char)
                else:
                    runs.append((subcandidate, char))
                previous_candidate = subcandidate

            for subcandidate, run_text in runs:
                self._insert_text(
                    page,
                    span,
                    run_text,
                    subcandidate,
                    origin=(current_x, current_y),
                )
                run_width = self.replacement_width(
                    subcandidate,
                    run_text,
                    span.size,
                )
                if run_width is None:
                    raise ValueError(
                        "Composite PDF font width could not be measured safely."
                    )
                current_x += direction_x * float(run_width)
                current_y += direction_y * float(run_width)
            return

        color = rgb_from_int(span.color)
        opacity = max(0.0, min(1.0, span.alpha / 255.0))
        rotation = rotation_from_direction(span.direction)

        if getattr(candidate, "extend_original_cff", False):
            if opacity < 0.999:
                raise CffSubsetExtensionError(
                    "Original-CFF subset extension currently requires fully opaque text."
                )

            # Extend only the missing glyphs / byte mappings inside the original
            # Type1C font object, keep its existing /F# resource, then emit a raw
            # one-byte Tj operation. This avoids PyMuPDF re-embedding the font as
            # a new Type0/CID resource.
            extension = extend_and_encode(page.parent, candidate, text)
            candidate.subset_added_chars = list(extension.added_chars)
            candidate.subset_added_codes = dict(extension.added_codes)
            repaired_name = self._attach_existing_pdf_font_resource(
                page,
                candidate,
                alias=extension.refname,
            )
            refname = repaired_name or extension.refname
            insert_origin = origin if origin is not None else span.origin
            append_raw_text_stream(
                page,
                refname=refname,
                encoded=extension.encoded,
                origin=insert_origin,
                size=span.size,
                color=span.color,
                rotation=rotation,
            )
            return

        if candidate.pdf_refname:
            # Reuse the exact symbolic font resource already present on the page.
            # Redaction can rewrite page resources in some PDFs, so make sure the
            # original font xref is still addressable before inserting text.
            repaired_name = self._attach_existing_pdf_font_resource(
                page,
                candidate,
            )
            fontname = f"/{(repaired_name or candidate.pdf_refname).lstrip('/')}"
        elif candidate.base14_name:
            fontname = candidate.base14_name
        else:
            if candidate.embedded_bytes is not None:
                font_data = candidate.embedded_bytes
            else:
                font_data = Path(candidate.path).read_bytes()

            digest = hashlib.sha1(font_data).hexdigest()[:12]
            fontname = f"ExactV39_{digest}"

            if candidate.embedded_bytes is not None:
                page.insert_font(
                    fontname=fontname,
                    fontbuffer=candidate.embedded_bytes,
                    set_simple=False,
                )
            else:
                page.insert_font(
                    fontname=fontname,
                    fontfile=candidate.path,
                    set_simple=False,
                )

        insert_origin = origin if origin is not None else span.origin

        try:
            page.insert_text(
                pymupdf.Point(*insert_origin),
                text,
                fontsize=span.size,
                fontname=fontname,
                color=color,
                rotate=rotation,
                fill_opacity=opacity,
                overlay=True,
            )
        except Exception as exc:
            # V3.9 could verify an existing page font successfully, then fail
            # here with "need font file or buffer" if the symbolic font resource
            # disappeared during redaction. Re-attach the same font xref under a
            # fresh alias and retry. This is still the exact original PDF font.
            if (
                candidate.pdf_refname
                and candidate.pdf_xref
                and "need font file or buffer" in str(exc).casefold()
            ):
                fallback_alias = f"V310F{int(candidate.pdf_xref)}"
                repaired_name = self._attach_existing_pdf_font_resource(
                    page,
                    candidate,
                    alias=fallback_alias,
                )
                if repaired_name:
                    page.insert_text(
                        pymupdf.Point(*insert_origin),
                        text,
                        fontsize=span.size,
                        fontname=f"/{repaired_name}",
                        color=color,
                        rotate=rotation,
                        fill_opacity=opacity,
                        overlay=True,
                    )
                    return
            raise


    @staticmethod
    def _norm_validation_text(value: str) -> str:
        value = (value or "").replace("\u00a0", " ")
        return re.sub(r"\s+", " ", value).strip()

    def _validate_replacement_now(
        self,
        page,
        original_text: str,
        replacement_text: str,
        original_bbox,
        final_bbox,
        font_size: float,
    ):
        """
        Validate a replacement immediately after it is applied.

        The current in-memory document is serialized and reopened, so this check
        sees the same searchable text layer that a later Save As validation sees.
        If this method raises, the caller should restore its pre-edit snapshot.
        """
        doc = page.parent
        if doc is None:
            raise ValueError(
                "Replacement cancelled: the edited page is no longer attached to a PDF."
            )

        page_number = int(page.number)

        try:
            raw = doc.tobytes(
                garbage=0,
                clean=False,
                deflate=False,
                no_new_id=True,
            )
        except TypeError:
            raw = doc.tobytes(no_new_id=True)

        probe = pymupdf.open(stream=raw, filetype="pdf")
        try:
            check_page = probe[page_number]

            ob = tuple(original_bbox)
            fb = tuple(final_bbox)

            rect = pymupdf.Rect(
                min(ob[0], fb[0]) - 2.0,
                min(ob[1], fb[1]) - 3.0,
                max(ob[2], fb[2]) + 8.0,
                max(ob[3], fb[3]) + max(6.0, float(font_size) * 1.5),
            ) & check_page.rect

            region_text = check_page.get_text("text", clip=rect)

            norm_region = self._norm_validation_text(region_text)
            norm_old = self._norm_validation_text(original_text)
            norm_new = self._norm_validation_text(replacement_text)

            same_text = bool(norm_old) and norm_old == norm_new
            old_is_part_of_new = (
                bool(norm_old)
                and bool(norm_new)
                and norm_old in norm_new
            )

            old_present = (
                (not same_text)
                and (not old_is_part_of_new)
                and bool(norm_old)
                and norm_old in norm_region
            )

            replacement_occurrences = (
                norm_region.count(norm_new)
                if norm_new
                else 0
            )

            new_present = (
                (not norm_new)
                or replacement_occurrences >= 1
            )

            single_replacement = (
                (not norm_new)
                or replacement_occurrences == 1
            )

            old_intersections = []
            if norm_old and not same_text and not old_is_part_of_new:
                try:
                    for hit in check_page.search_for(original_text):
                        if hit.intersects(rect):
                            old_intersections.append(tuple(hit))
                except Exception:
                    pass

            reasons = []

            if old_present or old_intersections:
                reasons.append("original text is still searchable")

            if not new_present:
                reasons.append("replacement text is not searchable after the edit")

            if not single_replacement:
                reasons.append(
                    f"replacement appears {replacement_occurrences} times in the edited region"
                )

            if reasons:
                raise ValueError(
                    "Replacement cancelled: " + "; ".join(reasons) + "."
                )

            return True

        finally:
            probe.close()

    def replace_with_horizontal_reflow(
        self,
        page,
        selected_span: TextSpan,
        replacement: str,
        selected_candidate: FontCandidate,
        expanded_bbox,
        shifted_items,
        replacement_origin=None,
    ):
        page.add_redact_annot(
            pymupdf.Rect(selected_span.bbox),
            fill=None,
            cross_out=False,
        )

        for item in shifted_items:
            page.add_redact_annot(
                pymupdf.Rect(item["span"].bbox),
                fill=None,
                cross_out=False,
            )

        page.apply_redactions(
            images=pymupdf.PDF_REDACT_IMAGE_NONE,
            graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
            text=pymupdf.PDF_REDACT_TEXT_REMOVE,
        )

        if replacement_origin is None:
            replacement_origin = selected_span.origin

        self._insert_text(
            page,
            selected_span,
            replacement,
            selected_candidate,
            origin=replacement_origin,
        )

        for item in shifted_items:
            span = item["span"]
            dx = float(item["dx"])
            new_origin = (
                span.origin[0] + dx,
                span.origin[1],
            )

            self._insert_text(
                page,
                span,
                span.text,
                item["candidate"],
                origin=new_origin,
            )

        # Transaction guard: reject the edit now instead of waiting for Save As.
        self._validate_replacement_now(
            page,
            original_text=selected_span.text,
            replacement_text=replacement,
            original_bbox=selected_span.bbox,
            final_bbox=expanded_bbox,
            font_size=selected_span.size,
        )

        return TextSpan(
            text=replacement,
            bbox=tuple(expanded_bbox),
            origin=tuple(replacement_origin),
            font=selected_span.font,
            size=selected_span.size,
            flags=selected_span.flags,
            color=selected_span.color,
            alpha=selected_span.alpha,
            direction=selected_span.direction,
            block_index=selected_span.block_index,
            line_index=selected_span.line_index,
            span_index=selected_span.span_index,
        )

    def replace_with_outline_reflow(
        self,
        page,
        selected_span: TextSpan,
        replacement: str,
        outline_candidate: FontCandidate,
        expanded_bbox,
        shifted_items,
        replacement_origin=None,
    ):
        """Redact selected text and draw the replacement as vector contours.

        This is an explicit last-resort mode. It adds no replacement font
        resource and no searchable replacement text. Unchanged neighbouring
        spans are still reinserted with their individually verified fonts.
        """
        page.add_redact_annot(
            pymupdf.Rect(selected_span.bbox),
            fill=None,
            cross_out=False,
        )

        for item in shifted_items:
            page.add_redact_annot(
                pymupdf.Rect(item["span"].bbox),
                fill=None,
                cross_out=False,
            )

        page.apply_redactions(
            images=pymupdf.PDF_REDACT_IMAGE_NONE,
            graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
            text=pymupdf.PDF_REDACT_TEXT_REMOVE,
        )

        if replacement_origin is None:
            replacement_origin = selected_span.origin

        outline_bbox = draw_text_as_outline(
            page,
            replacement,
            outline_candidate,
            origin=replacement_origin,
            size=selected_span.size,
            color=selected_span.color,
            alpha=selected_span.alpha,
            direction=selected_span.direction,
        )

        for item in shifted_items:
            neighbour = item["span"]
            dx = float(item["dx"])
            self._insert_text(
                page,
                neighbour,
                neighbour.text,
                item["candidate"],
                origin=(neighbour.origin[0] + dx, neighbour.origin[1]),
            )

        self._validate_outline_now(
            page,
            original_text=selected_span.text,
            replacement_text=replacement,
            original_bbox=selected_span.bbox,
            outline_bbox=outline_bbox,
            font_size=selected_span.size,
        )

        final_bbox = pymupdf.Rect(expanded_bbox) | pymupdf.Rect(outline_bbox)
        return TextSpan(
            text=replacement,
            bbox=tuple(final_bbox),
            origin=tuple(replacement_origin),
            font=selected_span.font,
            size=selected_span.size,
            flags=selected_span.flags,
            color=selected_span.color,
            alpha=selected_span.alpha,
            direction=selected_span.direction,
            block_index=selected_span.block_index,
            line_index=selected_span.line_index,
            span_index=selected_span.span_index,
        ), tuple(outline_bbox)

    def _validate_outline_now(
        self,
        page,
        *,
        original_text: str,
        replacement_text: str,
        original_bbox,
        outline_bbox,
        font_size: float,
    ):
        """Serialize/reopen and verify removal plus vector-path presence."""
        doc = page.parent
        if doc is None:
            raise ValueError("Outline replacement cancelled: page is detached.")

        try:
            raw = doc.tobytes(
                garbage=0,
                clean=False,
                deflate=False,
                no_new_id=True,
            )
        except TypeError:
            raw = doc.tobytes(no_new_id=True)

        probe = pymupdf.open(stream=raw, filetype="pdf")
        try:
            check_page = probe[int(page.number)]
            source_rect = pymupdf.Rect(original_bbox)
            outline_rect = pymupdf.Rect(outline_bbox)
            clip = pymupdf.Rect(
                min(source_rect.x0, outline_rect.x0) - 2.0,
                min(source_rect.y0, outline_rect.y0) - 3.0,
                max(source_rect.x1, outline_rect.x1) + 4.0,
                max(source_rect.y1, outline_rect.y1) + max(4.0, float(font_size)),
            ) & check_page.rect

            old_present = False
            normalized_old = self._norm_validation_text(original_text)
            replacement_present = False
            normalized_replacement = self._norm_validation_text(replacement_text)
            if normalized_old:
                region = self._norm_validation_text(
                    check_page.get_text("text", clip=clip) or ""
                )
                old_present = normalized_old in region
                if not old_present:
                    try:
                        old_present = any(
                            pymupdf.Rect(hit).intersects(clip)
                            for hit in check_page.search_for(original_text)
                        )
                    except Exception:
                        pass

            if normalized_replacement:
                outline_text = self._norm_validation_text(
                    check_page.get_text("text", clip=outline_rect) or ""
                )
                replacement_present = normalized_replacement in outline_text
                if not replacement_present:
                    try:
                        replacement_present = any(
                            pymupdf.Rect(hit).intersects(outline_rect)
                            for hit in check_page.search_for(replacement_text)
                        )
                    except Exception:
                        pass

            drawing_found = False
            for drawing in check_page.get_drawings(extended=True):
                drawing_rect = pymupdf.Rect(drawing.get("rect") or (0, 0, 0, 0))
                if drawing_rect.is_empty or not drawing_rect.intersects(outline_rect):
                    continue
                overlap = drawing_rect & outline_rect
                ratio = overlap.get_area() / max(0.01, min(
                    drawing_rect.get_area(), outline_rect.get_area()
                ))
                if ratio >= 0.50:
                    drawing_found = True
                    break

            if old_present or replacement_present or not drawing_found:
                reasons = []
                if old_present:
                    reasons.append("original text is still searchable")
                if replacement_present:
                    reasons.append("replacement was added to the searchable text layer")
                if not drawing_found:
                    reasons.append("replacement vector outline is missing")
                raise ValueError(
                    "Outline replacement cancelled: " + "; ".join(reasons) + "."
                )
        finally:
            probe.close()

    def replace_with_multiline_reflow(
        self,
        page,
        selected_span,
        replacement,
        plan,
        shifted_below_items,
    ):
        """
        Apply a multiline paragraph reflow.

        shifted_below_items:
          [{"span": TextSpan, "candidate": FontCandidate, "dy": float}, ...]
        """
        # Redact all paragraph spans being reflowed.
        for span in plan["paragraph_spans"]:
            page.add_redact_annot(
                pymupdf.Rect(span.bbox),
                fill=None,
                cross_out=False,
            )

        # Redact any later content that must shift down.
        for item in shifted_below_items:
            page.add_redact_annot(
                pymupdf.Rect(item["span"].bbox),
                fill=None,
                cross_out=False,
            )

        page.apply_redactions(
            images=pymupdf.PDF_REDACT_IMAGE_NONE,
            graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
            text=pymupdf.PDF_REDACT_TEXT_REMOVE,
        )

        # Insert reflowed paragraph tokens.
        for item in plan["laid_out"]:
            self._insert_text(
                page,
                item["source_span"],
                item["text"],
                item["candidate"],
                origin=item["origin"],
            )

        # Reinsert later text at shifted Y positions.
        for item in shifted_below_items:
            span = item["span"]
            dy = float(item["dy"])

            self._insert_text(
                page,
                span,
                span.text,
                item["candidate"],
                origin=(
                    span.origin[0],
                    span.origin[1] + dy,
                ),
            )

        # Transaction guard: reject the edit now instead of waiting for Save As.
        self._validate_replacement_now(
            page,
            original_text=selected_span.text,
            replacement_text=replacement,
            original_bbox=selected_span.bbox,
            final_bbox=plan["selected_bbox"],
            font_size=selected_span.size,
        )

        return TextSpan(
            text=replacement,
            bbox=tuple(plan["selected_bbox"]),
            origin=selected_span.origin,
            font=selected_span.font,
            size=selected_span.size,
            flags=selected_span.flags,
            color=selected_span.color,
            alpha=selected_span.alpha,
            direction=selected_span.direction,
            block_index=selected_span.block_index,
            line_index=selected_span.line_index,
            span_index=selected_span.span_index,
        )

    def replace_text(
        self,
        page,
        span: TextSpan,
        replacement: str,
        candidate: FontCandidate,
    ):
        page.add_redact_annot(
            pymupdf.Rect(span.bbox),
            fill=None,
            cross_out=False,
        )

        page.apply_redactions(
            images=pymupdf.PDF_REDACT_IMAGE_NONE,
            graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
            text=pymupdf.PDF_REDACT_TEXT_REMOVE,
        )

        self._insert_text(
            page,
            span,
            replacement,
            candidate,
        )

        # The replacement may be wider than the selected source span. Validate
        # against its measured writing direction instead of clipping the probe
        # to the old bbox (which produced false failures for longer text).
        final_rect = pymupdf.Rect(span.bbox)
        measured_width = self.replacement_width(
            candidate,
            replacement,
            span.size,
        )
        if measured_width is not None and measured_width > 0:
            ox, oy = map(float, span.origin)
            rotation = rotation_from_direction(span.direction)
            if rotation == 90:
                replacement_rect = pymupdf.Rect(
                    ox - float(span.size),
                    oy - float(measured_width),
                    ox + float(span.size),
                    oy + float(span.size) * 0.35,
                )
            elif rotation == 180:
                replacement_rect = pymupdf.Rect(
                    ox - float(measured_width),
                    float(span.bbox[1]),
                    ox,
                    float(span.bbox[3]),
                )
            elif rotation == 270:
                replacement_rect = pymupdf.Rect(
                    ox - float(span.size) * 0.35,
                    oy - float(span.size),
                    ox + float(span.size),
                    oy + float(measured_width),
                )
            else:
                replacement_rect = pymupdf.Rect(
                    ox,
                    float(span.bbox[1]),
                    ox + float(measured_width),
                    float(span.bbox[3]),
                )
            final_rect |= replacement_rect

        # Transaction guard for direct single-span replacement.
        self._validate_replacement_now(
            page,
            original_text=span.text,
            replacement_text=replacement,
            original_bbox=span.bbox,
            final_bbox=tuple(final_rect),
            font_size=span.size,
        )
