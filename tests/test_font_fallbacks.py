from __future__ import annotations

import tempfile
from io import BytesIO
from pathlib import Path

import pymupdf
from fontTools import subset
from fontTools.ttLib import TTFont

from app.font_resolver import SystemFontIndex
from app.font_utils import font_path_has_text, font_text_width_from_path
from app.models import FontCandidate
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
):
    session.approve_database_fallback(
        candidate=candidate,
        replacement=replacement,
        record={"_id": "test-font"},
        score={
            "weight_delta": abs(target_weight - candidate_weight),
            "target_weight": target_weight,
            "candidate_weight": candidate_weight,
            "italic_match": True,
            "family_similarity": 0.5,
        },
        mode=mode,
    )
    return session.state()["resolution"]["fallback_token"]


def main():
    replacement = "Fallback Omega Ω"
    candidate = compatible_candidate(replacement)

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
            assert "No full-line font substitution was applied" in str(exc)
        else:
            raise AssertionError("Unsafe whole-line database substitution was accepted.")
        finally:
            assert "Original" in session.doc[0].get_text()
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
