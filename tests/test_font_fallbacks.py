from __future__ import annotations

import tempfile
from copy import copy
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import pymupdf
from fontTools import subset
from fontTools.cffLib import CFFFontSet
from fontTools.fontBuilder import FontBuilder
from fontTools.pens.t2CharStringPen import T2CharStringPen
from fontTools.ttLib import TTFont

from app.cff_subset_extender import (
    CffSubsetExtensionError,
    _build_tounicode,
    build_adaptive_external_glyph_plan,
    build_adaptive_pdf_glyph_plan,
    extend_and_encode,
    extension_unicode_widths,
)
from app.font_resolver import SystemFontIndex
from app.font_utils import font_path_has_text, font_text_width_from_path
from app.models import FontCandidate, FontResolution
from app.pdf_engine import PdfEngine
from web_app import WebEditorSession


def compatible_candidate(text: str):
    return next(
        candidate
        for candidate in SystemFontIndex().iter_candidates()
        if candidate.path and font_path_has_text(candidate.path, text)[0]
    )


def subsettable_candidate(text: str):
    for candidate in SystemFontIndex().iter_candidates():
        if not candidate.path or not font_path_has_text(candidate.path, text)[0]:
            continue
        try:
            font = TTFont(candidate.path)
            options = subset.Options()
            subsetter = subset.Subsetter(options=options)
            subsetter.populate(text=text)
            subsetter.subset(font)
            font.save(BytesIO())
            font.close()
        except Exception:
            continue
        return candidate
    raise AssertionError("No subsettable test font covers the requested text.")


def make_source(path: Path):
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=220)
    page.insert_text((45, 90), "Original", fontsize=11, fontname="helv")
    doc.save(path, deflate=True)
    doc.close()


def make_subset_font(source: str, destination: Path, text: str):
    font = TTFont(source)
    try:
        options = subset.Options()
        options.name_IDs = [1, 2, 3, 4, 6]
        options.name_legacy = True
        options.name_languages = [0x409]
        subsetter = subset.Subsetter(options=options)
        subsetter.populate(text=text)
        subsetter.subset(font)
        font.save(destination)
    finally:
        font.close()


def make_multi_subset_source(path: Path, root: Path, font_path: str):
    first_font = root / "subset-ab.ttf"
    second_font = root / "subset-omega.ttf"
    make_subset_font(font_path, first_font, "AB ")
    make_subset_font(font_path, second_font, "Ω ")

    doc = pymupdf.open()
    first_page = doc.new_page(width=400, height=220)
    first_page.insert_text(
        (45, 90),
        "AB",
        fontsize=11,
        fontname="SubsetAB",
        fontfile=str(first_font),
        set_simple=False,
    )
    second_page = doc.new_page(width=400, height=220)
    second_page.insert_text(
        (45, 90),
        "Ω",
        fontsize=11,
        fontname="SubsetOmega",
        fontfile=str(second_font),
        set_simple=False,
    )
    doc.save(path, deflate=True)
    doc.close()


def _draw_box(pen, x0, y0, x1, y1, clockwise=True):
    points = (
        [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
        if clockwise
        else [(x0, y0), (x0, y1), (x1, y1), (x1, y0)]
    )
    pen.moveTo(points[0])
    for point in points[1:]:
        pen.lineTo(point)
    pen.closePath()


def synthetic_cff(
    stroke: int,
    text: str,
    style: str,
    *,
    full_font: bool = False,
) -> bytes:
    """Build a tiny raw CFF with AGL glyph names for adaptive regressions."""
    codepoints = [ord(char) for char in text if char != " "]
    unicode_names = {codepoint: chr(codepoint) for codepoint in codepoints}
    glyph_order = [".notdef", "space", *unicode_names.values()]
    builder = FontBuilder(1000, isTTF=False)
    builder.setupGlyphOrder(glyph_order)
    builder.setupCharacterMap({32: "space", **unicode_names})

    char_strings = {}
    metrics = {}
    for index, name in enumerate(glyph_order):
        advance = 650 + (index % 4) * 20
        pen = T2CharStringPen(advance, None)
        if name != "space":
            width = 430 + (index % 5) * 25
            height = 620 + (index % 3) * 30
            _draw_box(pen, 60, 0, 60 + width, height)
            _draw_box(
                pen,
                60 + stroke,
                stroke,
                60 + width - stroke,
                height - stroke,
                clockwise=False,
            )
        char_strings[name] = pen.getCharString(
            private=None,
            globalSubrs=None,
        )
        metrics[name] = (advance, 0)

    family = "SyntheticConnections"
    builder.setupHorizontalMetrics(metrics)
    builder.setupHorizontalHeader(ascent=800, descent=-200)
    builder.setupNameTable(
        {
            "familyName": family,
            "styleName": style,
            "uniqueFontIdentifier": f"{family}-{style}",
            "fullName": f"{family} {style}",
            "psName": f"{family}-{style}",
        }
    )
    builder.setupOS2(
        usWeightClass=700 if style == "Bold" else 400,
        sTypoAscender=800,
        sTypoDescender=-200,
        sTypoLineGap=0,
        usWinAscent=800,
        usWinDescent=200,
    )
    builder.setupPost()
    builder.setupCFF(
        f"{family}-{style}",
        {"FullName": f"{family} {style}", "FamilyName": family, "Weight": style},
        char_strings,
        {},
    )
    font = builder.font
    if full_font:
        output = BytesIO()
        font.save(output)
        return output.getvalue()
    return font["CFF "].compile(font)


class _SyntheticFontPage:
    def __init__(self, records):
        self.records = records

    def get_fonts(self, full=True):
        return self.records


class SyntheticCffDocument:
    """Minimal PyMuPDF-like object graph needed by the CFF extender."""

    def __init__(self):
        target = synthetic_cff(110, "ABCDEFGHIJKL ", "Bold")
        donor = synthetic_cff(50, "ABCDEFGHIJKLUV ", "Regular")
        widths = " ".join(["650"] * 55)
        self.objects = {
            1: (
                "<< /Subtype /Type1 /BaseFont /SyntheticConnections-Bold "
                f"/FirstChar 32 /LastChar 86 /Widths [ {widths} ] >>"
            ),
            2: "<< /FontFile3 5 0 R >>",
            3: (
                "<< /Type /Encoding /Differences [ 32 /space "
                "65 /A /B /C /D /E /F /G /H /I /J /K /L ] >>"
            ),
            4: "<<>>",
            5: "<< /Subtype /Type1C >>",
            6: (
                "<< /Subtype /Type1 /BaseFont /SyntheticConnections-Regular "
                f"/FirstChar 32 /LastChar 86 /Widths [ {widths} ] >>"
            ),
            7: "<< /FontFile3 10 0 R >>",
            8: (
                "<< /Type /Encoding /Differences [ 32 /space "
                "65 /A /B /C /D /E /F /G /H /I /J /K /L 85 /U /V ] >>"
            ),
            9: "<<>>",
            10: "<< /Subtype /Type1C >>",
        }
        target_map = {32: 32, **{ord(char): ord(char) for char in "ABCDEFGHIJKL"}}
        donor_map = {
            32: 32,
            **{ord(char): ord(char) for char in "ABCDEFGHIJKLUV"},
        }
        self.streams = {
            4: _build_tounicode(target_map),
            5: target,
            9: _build_tounicode(donor_map),
            10: donor,
        }
        self.pages = [
            _SyntheticFontPage(
                [
                    (1, "cff", "Type1", "SyntheticConnections-Bold", "F1", "", 0),
                    (6, "cff", "Type1", "SyntheticConnections-Regular", "F2", "", 0),
                ]
            )
        ]

    def __iter__(self):
        return iter(self.pages)

    def xref_object(self, xref, compressed=False):
        return self.objects[xref]

    def xref_stream(self, xref):
        return self.streams[xref]

    def xref_get_key(self, xref, key):
        values = {
            (1, "FontDescriptor"): ("xref", "2 0 R"),
            (1, "Encoding"): ("xref", "3 0 R"),
            (1, "ToUnicode"): ("xref", "4 0 R"),
            (2, "FontFile3"): ("xref", "5 0 R"),
            (6, "FontDescriptor"): ("xref", "7 0 R"),
            (6, "Encoding"): ("xref", "8 0 R"),
            (6, "ToUnicode"): ("xref", "9 0 R"),
            (7, "FontFile3"): ("xref", "10 0 R"),
        }
        return values.get((xref, key), ("null", "null"))

    def update_stream(self, xref, data, compress=True):
        self.streams[xref] = data

    def update_object(self, xref, data):
        self.objects[xref] = data

    def xref_set_key(self, xref, key, value):
        # The byte-level outline assertions below do not need the rewritten
        # dictionary values; production PyMuPDF persists these keys normally.
        return None


def cff_charstring_bytes(data: bytes):
    font = TTFont(recalcBBoxes=False, recalcTimestamp=False)
    cff = CFFFontSet()
    cff.decompile(BytesIO(data), font)
    top = cff[cff.fontNames[0]]
    return {
        name: bytes(top.CharStrings[name].bytecode)
        for name in top.charset
    }


def selected_session(source: Path, root: Path):
    session = WebEditorSession(source, source.name, root, "test", "test")
    span = next(item for item in session.spans if item.text == "Original")
    session.select_span(span)
    return session


def approve(
    session,
    candidate,
    replacement: str,
    mode: str,
    *,
    target_weight: int = 400,
    candidate_weight: int = 400,
    family_similarity: float = 0.5,
    italic_match: bool = True,
):
    session.approve_database_fallback(
        candidate=candidate,
        replacement=replacement,
        record={"_id": "test-font"},
        score={
            "weight_delta": abs(target_weight - candidate_weight),
            "target_weight": target_weight,
            "candidate_weight": candidate_weight,
            "italic_match": italic_match,
            "family_similarity": family_similarity,
        },
        mode=mode,
    )
    return session.state()["resolution"]["fallback_token"]


def main():
    replacement = "Fallback Omega Ω"
    candidate = compatible_candidate(replacement)

    # A lighter same-family CFF may donate only missing glyphs after its
    # outlines are calibrated against the target's existing bold glyphs. This
    # regression deliberately uses standard AGL names (A/U/V), not uniXXXX.
    adaptive_doc = SyntheticCffDocument()
    original_programs = cff_charstring_bytes(adaptive_doc.streams[5])
    plan = build_adaptive_pdf_glyph_plan(
        adaptive_doc,
        1,
        "UV",
        "ABCDEFGHIJKL",
    )
    assert plan["sources"][0]["font_xref"] == 6
    assert plan["sources"][0]["strength_x"] > 0
    assert plan["sources"][0]["probe_count"] >= 8
    assert plan["sources"][0]["score"] < 0.16

    external_bytes = synthetic_cff(
        50,
        "ABCDEFGHIJKLUV ",
        "Regular",
        full_font=True,
    )
    external_candidate = FontCandidate(
        path=None,
        embedded_bytes=external_bytes,
        subset_extension_font_bytes=external_bytes,
        pdf_refname="F1",
        pdf_xref=1,
        extend_original_cff=True,
    )
    external_plan = build_adaptive_external_glyph_plan(
        adaptive_doc,
        1,
        external_candidate,
        "UV",
        "ABCDEFGHIJKL",
    )
    assert external_plan["sources"][0]["kind"] == "external_font"
    assert external_plan["sources"][0]["strength_x"] > 0
    assert set(external_plan["glyphs"]) == {"U", "V"}

    adaptive_candidate = FontCandidate(
        path=None,
        family="SyntheticConnections",
        full_name="SyntheticConnections Bold",
        weight=700,
        pdf_refname="F1",
        pdf_xref=1,
        extend_original_cff=True,
        adaptive_glyph_plan=plan,
    )
    measured_widths = extension_unicode_widths(
        adaptive_doc,
        adaptive_candidate,
        "AUV",
    )
    assert measured_widths["U"] == plan["unicode_widths"]["U"]
    result = extend_and_encode(adaptive_doc, adaptive_candidate, "AUV")
    assert result.added_chars == ["U", "V"]
    completed_programs = cff_charstring_bytes(adaptive_doc.streams[5])
    assert all(
        completed_programs[name] == bytecode
        for name, bytecode in original_programs.items()
    )
    assert "uni0055" in completed_programs
    assert "uni0056" in completed_programs

    # The exact-full-font branch still copies missing glyphs and closes its
    # TTFont cleanly after the adaptive implementation was added.
    exact_doc = SyntheticCffDocument()
    exact_bytes = synthetic_cff(
        110,
        "ABCDEFGHIJKLUV ",
        "Bold",
        full_font=True,
    )
    exact_candidate = FontCandidate(
        path=None,
        embedded_bytes=exact_bytes,
        subset_extension_font_bytes=exact_bytes,
        pdf_refname="F1",
        pdf_xref=1,
        extend_original_cff=True,
    )
    exact_result = extend_and_encode(exact_doc, exact_candidate, "AUV")
    assert exact_result.added_chars == ["U", "V"]
    exact_programs = cff_charstring_bytes(exact_doc.streams[5])
    assert "uni0055" in exact_programs and "uni0056" in exact_programs

    # Similar-looking outlines from an unrelated embedded family must be
    # rejected before calibration can mutate the target font stream.
    unrelated_doc = SyntheticCffDocument()
    unrelated_doc.objects[6] = unrelated_doc.objects[6].replace(
        "/SyntheticConnections-Regular",
        "/UnrelatedSans-Regular",
    )
    try:
        build_adaptive_pdf_glyph_plan(
            unrelated_doc,
            1,
            "UV",
            "ABCDEFGHIJKL",
        )
    except CffSubsetExtensionError as exc:
        assert "different font family" in str(exc)
    else:
        raise AssertionError("An unrelated PDF donor family was accepted.")

    # A cached CFF width map can be incomplete after another edit extended the
    # same PDF font object. Width planning must retain original mapped widths
    # and obtain only the missing character's advance from the verified donor.
    metric_candidate = FontCandidate(
        path=candidate.path,
        pdf_refname="F1",
        pdf_unicode_widths={"A": 0.625},
        extend_original_cff=True,
        subset_extension_font_path=candidate.path,
    )
    donor_omega = font_text_width_from_path(candidate.path, "Ω", 1.0)
    assert donor_omega is not None
    measured = PdfEngine().replacement_width(metric_candidate, "AΩ", 12.0)
    assert measured is not None
    assert abs(measured - ((0.625 + donor_omega) * 12.0)) < 0.001

    with tempfile.TemporaryDirectory(prefix="font_fallback_test_") as td:
        root = Path(td)

        # A database face must never replace the whole line when the selected
        # PDF font cannot be completed in-place. Base-14 has no extendable CFF
        # subset, so the strict donor action must reject it.
        source = root / "closest-source.pdf"
        make_source(source)
        session = selected_session(source, root)
        try:
            approve(
                session,
                candidate,
                replacement,
                "closest_font",
                target_weight=700,
                candidate_weight=500,
            )
        except ValueError as exc:
            assert "original PDF was not changed" in str(exc)
        else:
            raise AssertionError("Unsafe whole-line database substitution was accepted.")
        finally:
            assert "Original" in session.doc[0].get_text()
            session.close()

        # A same-family font with glyph coverage is still unsafe when its
        # weight differs from the selected PDF face. This used to append thin
        # glyphs into a bold CFF subset while merely reporting exact_weight=False.
        source = root / "weight-mismatch-source.pdf"
        make_source(source)
        session = selected_session(source, root)
        try:
            approve(
                session,
                candidate,
                replacement,
                "closest_font",
                target_weight=700,
                candidate_weight=500,
                family_similarity=1.0,
            )
        except ValueError as exc:
            assert "target weight 700" in str(exc)
            assert "candidate weight 500" in str(exc)
            assert "original PDF was not changed" in str(exc)
        else:
            raise AssertionError("A neighboring font weight was accepted as an exact donor.")
        finally:
            assert "Original" in session.doc[0].get_text()
            session.close()

        # An explicitly calibrated DB donor is allowed despite neighboring
        # weight metadata because the plan already passed shared-outline
        # geometry checks. The plan survives the resolver wrapper and the UI
        # reports the effective target geometry as exact.
        source = root / "adaptive-db-source.pdf"
        make_source(source)
        session = selected_session(source, root)
        adaptive_db_candidate = copy(candidate)
        adaptive_db_candidate.adaptive_glyph_plan = {
            "version": 1,
            "target_font_xref": 1,
            "sources": [],
            "glyphs": {},
            "unicode_widths": {"Ω": 0.7},
            "report": "Calibrated against 12 existing target glyphs.",
        }
        prepared = copy(adaptive_db_candidate)
        prepared.extend_original_cff = True
        prepared.pdf_refname = "F1"
        prepared.pdf_xref = 1
        with patch.object(
            session.font_resolver,
            "_cff_subset_extension_candidate",
            return_value=prepared,
        ), patch(
            "web_app.extension_unicode_widths",
            return_value={char: 0.7 for char in replacement},
        ):
            token = approve(
                session,
                adaptive_db_candidate,
                replacement,
                "adaptive_font",
                target_weight=700,
                candidate_weight=400,
                family_similarity=0.85,
            )
        adaptive_state = session.state()["resolution"]
        assert token
        assert adaptive_state["fallback_mode"] == "adaptive_font"
        assert "calibrated" in adaptive_state["source"].casefold()
        assert session.current_resolution.exact_weight
        assert session.current_resolution.candidate.weight == 700
        assert session.current_resolution.candidate.adaptive_glyph_plan
        session.close()

        # A PDF-backed adaptive plan has no external path; the CFF wrapper must
        # still preserve it and bind it to the selected original /F# resource.
        source = root / "adaptive-plan-wrapper.pdf"
        make_source(source)
        session = selected_session(source, root)
        pdf_plan_candidate = FontCandidate(
            path=None,
            family="SyntheticConnections",
            adaptive_glyph_plan={"target_font_xref": 42},
        )
        with patch.object(
            session.font_resolver,
            "find_page_font",
            return_value={
                "type": "Type1",
                "ext": "cff",
                "xref": 42,
                "refname": "F42",
            },
        ):
            wrapped = session.font_resolver._cff_subset_extension_candidate(
                session.doc[0],
                session.selected_span,
                pdf_plan_candidate,
            )
        assert wrapped.extend_original_cff
        assert wrapped.pdf_xref == 42
        assert wrapped.pdf_refname == "F42"
        assert wrapped.adaptive_glyph_plan is pdf_plan_candidate.adaptive_glyph_plan
        session.close()

        # Metadata can lie. Even a same-name/same-weight donor must pass the
        # embedded-vs-donor outline fingerprint before subset preparation.
        source = root / "outline-fingerprint-source.pdf"
        make_source(source)
        session = selected_session(source, root)
        prepared = copy(candidate)
        prepared.extend_original_cff = True
        prepared.pdf_refname = "F1"
        prepared.pdf_xref = 1
        try:
            with patch.object(
                session.font_resolver,
                "_cff_subset_extension_candidate",
                return_value=prepared,
            ), patch(
                "web_app.verify_donor_compatibility",
                return_value={
                    "ok": False,
                    "reason": "outline fingerprint mismatch",
                },
            ):
                approve(
                    session,
                    candidate,
                    replacement,
                    "closest_font",
                    target_weight=700,
                    candidate_weight=700,
                    family_similarity=1.0,
                )
        except ValueError as exc:
            assert "outline fingerprint mismatch" in str(exc)
        else:
            raise AssertionError("A donor with mismatched outlines was accepted.")
        finally:
            assert "Original" in session.doc[0].get_text()
            session.close()

        # Normal/manual font resolution must apply the same outline fingerprint
        # gate before a full font can donate glyphs to an original CFF subset.
        # A same-name/same-weight result from FontResolver is not sufficient.
        source = root / "manual-donor-fingerprint-source.pdf"
        make_source(source)
        session = selected_session(source, root)
        manual_donor = copy(candidate)
        manual_donor.extend_original_cff = True
        manual_donor.pdf_refname = "F1"
        manual_donor.pdf_xref = 1
        manual_donor.subset_extension_font_path = candidate.path
        resolver_result = FontResolution(
            ok=True,
            candidate=manual_donor,
            exact_family=True,
            exact_weight=True,
            exact_style=True,
            glyphs_ok=True,
            source_note="Manually loaded full font.",
        )
        original_text = session.doc[0].get_text()
        with patch.object(
            session.font_resolver,
            "resolve",
            return_value=resolver_result,
        ), patch(
            "web_app.verify_donor_compatibility",
            return_value={
                "ok": False,
                "reason": "manual donor outline fingerprint mismatch",
            },
        ) as verify:
            rejected = session.resolve_selected_font(replacement)
        assert rejected is not None and not rejected.ok
        assert session.current_resolution is rejected
        assert "manual donor outline fingerprint mismatch" in session.match_status
        assert "original PDF was not changed" in session.match_status
        assert session.match_tone == "error"
        assert session.doc[0].get_text() == original_text
        verify.assert_called_once_with(
            session.doc,
            manual_donor,
            session.selected_span.text,
        )
        session.close()

        # Outline mode adds vector paths rather than a replacement font/text
        # object and receives its own final-save validation record.
        source = root / "outline-source.pdf"
        output = root / "outline-output.pdf"
        make_source(source)
        session = selected_session(source, root)
        fallback_token = approve(
            session,
            candidate,
            replacement,
            "outline",
            target_weight=700,
            candidate_weight=500,
        )
        assert session.state()["resolution"]["fallback_mode"] == "outline"
        session.replace_selected_text(replacement, fallback_token=fallback_token)
        assert session.selected_span is None
        assert session.edit_session.edits[-1]["operation_type"] == "outline"
        _, _, validation_path = session.edit_session.export_clean(
            session.doc,
            str(output),
        )
        check = pymupdf.open(output)
        try:
            assert "Original" not in check[0].get_text()
            assert replacement not in check[0].get_text()
            assert check[0].get_drawings()
            assert not any(
                (candidate.family or "").casefold()
                in str(item[3] or "").casefold()
                for item in check[0].get_fonts(full=True)
            )
        finally:
            check.close()
            session.close()
        assert Path(validation_path).exists()

        # A replacement can be assembled as searchable text from multiple
        # same-family subsets already present on different PDF pages. Each
        # character is raster-probed before the resource is approved.
        source = root / "multi-subset-source.pdf"
        output = root / "multi-subset-output.pdf"
        subset_candidate = subsettable_candidate("ABΩ")
        make_multi_subset_source(source, root, subset_candidate.path)
        session = WebEditorSession(source, source.name, root, "test", "test")
        span = next(item for item in session.spans if item.text == "AB")
        session.select_span(span)
        session.find_glyph_elsewhere_in_pdf("ABΩ")
        resolution = session.state()["resolution"]
        assert resolution["fallback_mode"] == "pdf_glyph_resource"
        assert "Combined 2 verified" in resolution["source"]
        session.replace_selected_text(
            "ABΩ",
            fallback_token=resolution["fallback_token"],
        )
        session.edit_session.export_clean(session.doc, str(output))
        check = pymupdf.open(output)
        try:
            assert "ABΩ" in check[0].get_text()
            assert "Ω" in check[1].get_text()
        finally:
            check.close()
            session.close()

    print("Font fallback workflow test: PASS")


if __name__ == "__main__":
    main()
