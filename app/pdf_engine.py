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

        if candidate.pdf_refname and candidate.pdf_unicode_widths:
            try:
                total = 0.0
                for ch in text:
                    if ch in ("\n", "\r"):
                        return None
                    if ch not in candidate.pdf_unicode_widths:
                        return None
                    total += float(candidate.pdf_unicode_widths[ch])
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
            raw = doc.tobytes()

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

        # Transaction guard for direct single-span replacement.
        self._validate_replacement_now(
            page,
            original_text=span.text,
            replacement_text=replacement,
            original_bbox=span.bbox,
            final_bbox=span.bbox,
            font_size=span.size,
        )
