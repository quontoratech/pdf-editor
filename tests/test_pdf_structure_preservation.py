from __future__ import annotations

import json
import re
import tempfile
import zlib
from pathlib import Path

import pymupdf

from app.clean_edit_session import CleanEditSession
from app.font_resolver import SystemFontIndex
from app.font_utils import font_path_has_text
from app.models import FontCandidate
from app.pdf_engine import PdfEngine


XMP = (
    '<?xpacket begin="\ufeff" id="W5M0MpCehiHzreSzNTczkc9d"?>'
    '<x:xmpmeta xmlns:x="adobe:ns:meta/">'
    '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
    '<rdf:Description rdf:about="" '
    'xmlns:pdf="http://ns.adobe.com/pdf/1.3/" '
    'xmlns:xmpMM="http://ns.adobe.com/xap/1.0/mm/" '
    'xmlns:custom="urn:pdf-editor-structure-test">'
    '<pdf:Producer>Original Producer</pdf:Producer>'
    '<xmpMM:DocumentID>uuid:keep-this-document-id</xmpMM:DocumentID>'
    '<custom:PrivateValue>keep-this-private-value</custom:PrivateValue>'
    '</rdf:Description></rdf:RDF></x:xmpmeta>'
    '<?xpacket end="w"?>'
)


def make_source(path: Path, *, xref_stream: bool) -> None:
    doc = pymupdf.open()
    page1 = doc.new_page(width=400, height=300)
    page1.insert_text((50, 80), "Original line", fontname="tiro", fontsize=11)
    page2 = doc.new_page(width=400, height=300)
    page2.insert_text((50, 80), "Untouched line", fontname="tiro", fontsize=11)
    doc.set_metadata(
        {
            "title": "Structure Test",
            "author": "Original Author",
            "creator": "Original Creator",
            "producer": "Original Producer",
            "creationDate": "D:20260102030405+00'00'",
            "modDate": "D:20260203040506+00'00'",
        }
    )
    info_type, info_value = doc.xref_get_key(-1, "Info")
    assert info_type == "xref"
    info_xref = int(info_value.split()[0])
    doc.xref_set_key(
        info_xref,
        "PrivateKey",
        pymupdf.get_pdf_str("keep-this-info-value"),
    )
    doc.set_xml_metadata(XMP)
    # Match the reported PDFs: source page streams use FlateDecode. Export must
    # not leave an uncompressed redaction stream plus a compressed overlay.
    doc.save(path, deflate=True, use_objstms=1 if xref_stream else 0)
    doc.close()


def make_direct_xref_source(path: Path) -> None:
    """Create a minimal direct-object source with tail content/identity slots."""
    streams = {
        5: zlib.compress(b"BT /tiro 11 Tf 50 220 Td (Original line) Tj ET\n"),
        6: zlib.compress(b"BT /tiro 11 Tf 50 220 Td (Untouched line) Tj ET\n"),
    }
    objects = {
        1: b"<</Type/Pages/Count 2/Kids[2 0 R 3 0 R]>>",
        2: (
            b"<</Type/Page/Parent 1 0 R/MediaBox[0 0 400 300]"
            b"/Resources<</Font<</tiro 4 0 R>>>>/Contents 5 0 R>>"
        ),
        3: (
            b"<</Type/Page/Parent 1 0 R/MediaBox[0 0 400 300]"
            b"/Resources<</Font<</tiro 4 0 R>>>>/Contents 6 0 R>>"
        ),
        4: b"<</Type/Font/Subtype/Type1/BaseFont/Times-Roman/Encoding/WinAnsiEncoding>>",
        5: (
            f"<</Length {len(streams[5])}/Filter/FlateDecode>>\nstream\n".encode()
            + streams[5]
            + b"\nendstream"
        ),
        6: (
            f"<</Length {len(streams[6])}/Filter/FlateDecode>>\nstream\n".encode()
            + streams[6]
            + b"\nendstream"
        ),
        7: b"<</Title(Structure Test)/Author(Original Author)/Producer(Original Producer)>>",
        8: b"<</Pages 1 0 R/Type/Catalog>>",
    }
    body = bytearray(b"%PDF-1.7\n%\xC2\xB5\xC2\xB6\n")
    offsets = {0: 0}
    for number in range(1, 9):
        offsets[number] = len(body)
        body.extend(f"{number} 0 obj\n".encode())
        body.extend(objects[number])
        body.extend(b"\nendobj\n")
    offsets[9] = len(body)
    decoded = bytearray()
    for number in range(10):
        kind = 0 if number == 0 else 1
        generation = 255 if number == 0 else 0
        decoded.extend(bytes((kind,)))
        decoded.extend(int(offsets.get(number, 0)).to_bytes(4, "big"))
        decoded.extend(bytes((generation,)))
    compressed = zlib.compress(bytes(decoded))
    body.extend(
        b"9 0 obj\n"
        + (
            f"<</Size 10/Info 7 0 R/Root 8 0 R/W[1 4 1]/Type/XRef"
            f"/Filter/FlateDecode/Length {len(compressed)}>>\nstream\n"
        ).encode()
        + compressed
        + b"\nendstream\nendobj\n\n"
        + f"startxref\n{offsets[9]}\n%%EOF\n".encode()
    )
    path.write_bytes(body)
    check = pymupdf.open(path)
    try:
        assert check.page_count == 2
        assert check[0].get_text().strip() == "Original line"
        assert CleanEditSession._xref_format(check) == "stream"
    finally:
        check.close()


def make_compact_classic_edit(path: Path) -> None:
    """Stage the same file after replacing object 5 with new object 8."""
    untouched = zlib.compress(
        b"BT /tiro 11 Tf 50 220 Td (Untouched line) Tj ET\n"
    )
    changed = zlib.compress(
        b"BT /tiro 11 Tf 50 220 Td (Changed line) Tj ET\n"
    )
    objects = {
        1: b"<</Type/Pages/Count 2/Kids[2 0 R 3 0 R]>>",
        2: (
            b"<</Type/Page/Parent 1 0 R/MediaBox[0 0 400 300]"
            b"/Resources<</Font<</tiro 4 0 R>>>>/Contents 8 0 R>>"
        ),
        3: (
            b"<</Type/Page/Parent 1 0 R/MediaBox[0 0 400 300]"
            b"/Resources<</Font<</tiro 4 0 R>>>>/Contents 5 0 R>>"
        ),
        4: b"<</Type/Font/Subtype/Type1/BaseFont/Times-Roman/Encoding/WinAnsiEncoding>>",
        5: (
            f"<</Length {len(untouched)}/Filter/FlateDecode>>\nstream\n".encode()
            + untouched
            + b"\nendstream"
        ),
        6: b"<</Title(Structure Test)/Author(Original Author)/Producer(Original Producer)>>",
        7: b"<</Pages 1 0 R/Type/Catalog>>",
        8: (
            f"<</Length {len(changed)}/Filter/FlateDecode>>\nstream\n".encode()
            + changed
            + b"\nendstream"
        ),
    }
    body = bytearray(b"%PDF-1.7\n%\xC2\xB5\xC2\xB6\n")
    offsets = {0: 0}
    for number in range(1, 9):
        offsets[number] = len(body)
        body.extend(f"{number} 0 obj\n".encode())
        body.extend(objects[number])
        body.extend(b"\nendobj\n")
    xref_offset = len(body)
    body.extend(b"xref\n0 9\n")
    body.extend(b"0000000000 65535 f \n")
    for number in range(1, 9):
        body.extend(f"{offsets[number]:010d} 00000 n \n".encode())
    body.extend(
        b"trailer\n<</Size 9/Info 6 0 R/Root 7 0 R>>\n"
        + f"startxref\n{xref_offset}\n%%EOF\n".encode()
    )
    path.write_bytes(body)


def put_pages_before_type_in_catalog(path: Path) -> None:
    """Give a fixture the same valid Catalog ordering as the reported PDF."""
    raw = path.read_bytes()
    pattern = re.compile(rb"/Type/Catalog(/Pages\s+\d+\s+\d+\s+R)")
    matches = list(pattern.finditer(raw))
    assert len(matches) == 1
    match = matches[0]
    replacement = match.group(1) + b"/Type/Catalog"
    assert len(replacement) == len(match.group(0))
    path.write_bytes(raw[:match.start()] + replacement + raw[match.end():])

    check = pymupdf.open(path)
    try:
        assert check.xref_get_keys(check.pdf_catalog())[:2] == ["Pages", "Type"]
    finally:
        check.close()


def edit_and_export(source: Path, output: Path):
    session = CleanEditSession(str(source))
    doc = pymupdf.open(source)
    engine = PdfEngine()
    candidate = FontCandidate(
        path=None,
        family="Times-Roman",
        full_name="Times-Roman",
        weight=400,
        source="Base14",
        base14_name="tiro",
    )

    page = doc[0]
    span = next(item for item in engine.extract_spans(page) if item.text == "Original line")
    engine.replace_text(page, span, "Changed line", candidate)
    final_span = next(
        item for item in engine.extract_spans(page) if item.text == "Changed line"
    )
    session.record_edit(
        page_number=1,
        original_text=span.text,
        replacement_text=final_span.text,
        original_bbox=span.bbox,
        final_bbox=final_span.bbox,
        origin=span.origin,
        font_family=candidate.family,
        font_face=candidate.display_name,
        font_source=candidate.source,
        font_size=span.size,
        color=span.color,
        mode="structure-regression",
    )
    live_root_before = session._trailer_ref(doc, "Root")
    live_info_before = session._trailer_ref(doc, "Info")
    live_xref_length_before = doc.xref_length()
    result = session.export_clean(doc, str(output))

    # Compact export must run on a detached document. Otherwise the first Save
    # As renumbers the still-open session and the second Save As is rejected as
    # a false Catalog/Root mismatch.
    assert session._trailer_ref(doc, "Root") == live_root_before
    assert session._trailer_ref(doc, "Info") == live_info_before
    assert doc.xref_length() == live_xref_length_before
    repeat_output = output.with_name(f"{output.stem}-repeat.pdf")
    session.export_clean(doc, str(repeat_output))
    assert session._trailer_ref(doc, "Root") == live_root_before
    assert session._trailer_ref(doc, "Info") == live_info_before
    assert doc.xref_length() == live_xref_length_before
    doc.close()
    return session, result


def assert_preserved(source: Path, output: Path, report_path: Path, xref_format: str):
    session = CleanEditSession(str(source))
    src = pymupdf.open(source)
    out = pymupdf.open(output)
    try:
        actual = session._capture_structure(out)
        source_structure = session.source_structure

        assert actual["xref_format"] == xref_format
        assert actual["trailer_id"] == source_structure["trailer_id"]
        # Compact garbage collection may renumber indirect references. The
        # referenced Info/XMP content and the Catalog type must survive.
        assert actual["root_entries"]["Type"] == source_structure["root_entries"]["Type"]
        assert actual["info_object"] == source_structure["info_object"]
        assert actual["logical_metadata"] == source_structure["logical_metadata"]
        assert actual["metadata_object"] == source_structure["metadata_object"]
        assert (
            actual["metadata_decoded_sha256"]
            == source_structure["metadata_decoded_sha256"]
        )
        assert actual["metadata_raw_sha256"] == source_structure["metadata_raw_sha256"]
        assert actual["edit_artifacts"] == {
            "redaction_records": 0,
            "fitz_a0_identifiers": 0,
            "free_xrefs": 0,
        }

        assert "Original line" not in out[0].get_text("text")
        assert "Changed line" in out[0].get_text("text")
        assert "Untouched line" in out[1].get_text("text")

        # Rendering outside the edited text rectangle must remain pixel-exact.
        matrix = pymupdf.Matrix(2, 2)
        src_page1 = src[0].get_pixmap(matrix=matrix, alpha=False)
        out_page1 = out[0].get_pixmap(matrix=matrix, alpha=False)
        assert (src_page1.width, src_page1.height, src_page1.n) == (
            out_page1.width,
            out_page1.height,
            out_page1.n,
        )
        old_rect = src[0].search_for("Original line")[0]
        new_rect = out[0].search_for("Changed line")[0]
        edit_rect = old_rect | new_rect
        x0 = max(0, int(edit_rect.x0 * 2) - 4)
        y0 = max(0, int(edit_rect.y0 * 2) - 4)
        x1 = min(src_page1.width, int(edit_rect.x1 * 2) + 5)
        y1 = min(src_page1.height, int(edit_rect.y1 * 2) + 5)
        src_samples = src_page1.samples
        out_samples = out_page1.samples
        stride = src_page1.width * src_page1.n
        for y in range(src_page1.height):
            row_start = y * stride
            if y0 <= y < y1:
                left_end = row_start + x0 * src_page1.n
                right_start = row_start + x1 * src_page1.n
                assert src_samples[row_start:left_end] == out_samples[row_start:left_end]
                assert (
                    src_samples[right_start:row_start + stride]
                    == out_samples[right_start:row_start + stride]
                )
            else:
                assert (
                    src_samples[row_start:row_start + stride]
                    == out_samples[row_start:row_start + stride]
                )

        src_page2 = src[1].get_pixmap(matrix=matrix, alpha=False)
        out_page2 = out[1].get_pixmap(matrix=matrix, alpha=False)
        assert src_page2.samples == out_page2.samples
    finally:
        src.close()
        out.close()

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["ok"] is True
    assert report["structure_ok"] is True
    pages = report["structure"]["pages"]
    assert pages[0]["status"] == "edited"
    assert pages[0]["content_streams_same"] is False
    assert pages[0]["stale_unreachable_source_streams"] == []
    assert pages[0]["compression_matches_source"] is True
    assert pages[0]["source_stream_filters"] == ["/FlateDecode"]
    assert pages[0]["output_stream_filters"] == ["/FlateDecode"]
    assert pages[1]["status"] == "untouched"
    assert pages[1]["content_streams_same"] is True


def main():
    with tempfile.TemporaryDirectory(prefix="pdf_structure_test_") as td:
        root = Path(td)
        for xref_stream, expected_format in ((False, "table"), (True, "stream")):
            label = expected_format
            source = root / f"source-{label}.pdf"
            output = root / f"output-{label}.pdf"
            make_source(source, xref_stream=xref_stream)
            _, (_, _, report) = edit_and_export(source, output)
            assert_preserved(source, output, Path(report), expected_format)

        # A compressed xref stream with direct objects (no /ObjStm) must keep
        # every existing object number. New edited page streams reuse the old
        # content slots, and an absent optional /Index stays absent.
        source = root / "source-direct-xref-stream.pdf"
        output = root / "output-direct-xref-stream.pdf"
        make_direct_xref_source(source)
        make_compact_classic_edit(output)
        direct_session = CleanEditSession(str(source))
        direct_session.edits.append(
            {
                "page": 1,
                "operation_type": "text",
                "inactive": False,
            }
        )
        rewrite = direct_session._rewrite_classic_xref_as_source_stream(
            str(output)
        )
        assert rewrite["object_numbers_restored"] is True
        direct_report = direct_session._validate_preserved_structure(str(output))
        assert direct_report["ok"] is True, direct_report["failures"]
        source_doc = pymupdf.open(source)
        output_doc = pymupdf.open(output)
        try:
            source_profile = direct_session._xref_serialization_profile(source_doc)
            output_profile = direct_session._xref_serialization_profile(output_doc)
            assert direct_session._trailer_ref(output_doc, "Root") == (
                direct_session._trailer_ref(source_doc, "Root")
            )
            assert direct_session._trailer_ref(output_doc, "Info") == (
                direct_session._trailer_ref(source_doc, "Info")
            )
            for key in (
                "size",
                "final_xref_object",
                "index",
                "compression",
                "encoded_size",
                "decoded_size",
            ):
                assert output_profile[key] == source_profile[key]
        finally:
            source_doc.close()
            output_doc.close()

        # PDF dictionaries are unordered, but MuPDF always writes /Type first
        # in the Catalog. The reported document validly stores /Pages first.
        # This must not trigger a false "Catalog changed" save failure when
        # every key, value type, value, and the Root reference are unchanged.
        source = root / "catalog-order-source.pdf"
        output = root / "catalog-order-output.pdf"
        make_source(source, xref_stream=False)
        put_pages_before_type_in_catalog(source)
        order_session, (_, audit_path, report_path) = edit_and_export(source, output)
        report = json.loads(Path(report_path).read_text(encoding="utf-8"))
        checks = {item["name"]: item for item in report["structure"]["checks"]}
        assert report["ok"] is True
        assert checks["catalog /Root reference"]["required"] is False
        assert checks["catalog /Root object"]["required"] is False
        assert checks["catalog /Root serialized form"]["passed"] is False
        assert checks["catalog /Root serialized form"]["required"] is False

        output_doc = pymupdf.open(output)
        try:
            actual = order_session._capture_structure(output_doc)
            assert actual["root_entries"]["Type"] == {
                "type": "name",
                "value": "/Catalog",
            }
            assert actual["root_object"] != order_session.source_structure["root_object"]
            assert output_doc.version_count == 1
        finally:
            output_doc.close()

        audit = json.loads(Path(audit_path).read_text(encoding="utf-8"))
        assert audit["structure_preservation"]["identity_repaired_after_save"] == []

        # With no edits, export is a verbatim copy. This is the only way to
        # preserve every historical xref section and every serialized byte.
        source = root / "no-edit-source.pdf"
        output = root / "no-edit-output.pdf"
        make_source(source, xref_stream=False)
        doc = pymupdf.open(source)
        try:
            session = CleanEditSession(str(source))
            session.export_clean(doc, str(output))
        finally:
            doc.close()
        assert output.read_bytes() == source.read_bytes()

        # Deleting a page necessarily changes the page tree and xref cardinality,
        # but pages after the deletion must still be matched to their original
        # source objects rather than compared by their shifted page number.
        source = root / "page-delete-source.pdf"
        output = root / "page-delete-output.pdf"
        make_source(source, xref_stream=True)
        delete_session = CleanEditSession(str(source))
        delete_doc = pymupdf.open(source)
        deleted_rect = delete_doc[0].rect
        delete_doc.delete_page(0)
        delete_session.shift_for_page_delete(1)
        delete_session.record_page_operation(
            action="delete",
            page_number=1,
            width=float(deleted_rect.width),
            height=float(deleted_rect.height),
        )
        _, _, delete_report_path = delete_session.export_clean(
            delete_doc,
            str(output),
        )
        delete_doc.close()
        delete_report = json.loads(Path(delete_report_path).read_text())
        assert delete_report["ok"] is True
        delete_pages = delete_report["structure"]["pages"]
        assert delete_pages[0]["status"] == "page-removed"
        assert delete_pages[0]["passed"] is True
        assert delete_pages[1]["status"] == "untouched"
        assert delete_pages[1]["output_page"] == 1
        assert delete_pages[1]["content_streams_same"] is True

        # Full-line reflow uses show_pdf_page() while editing. Export must inline
        # the generated fzFrm wrappers and collect copied duplicate resources.
        source = root / "line-delete-source.pdf"
        output = root / "line-delete-output.pdf"
        line_doc = pymupdf.open()
        line_page = line_doc.new_page(width=400, height=300)
        for y, text in (
            (70, "First removable line"),
            (90, "Second moved line"),
            (110, "Third moved line"),
            (130, "Fourth moved line"),
        ):
            line_page.insert_text((50, y), text, fontname="tiro", fontsize=11)
        line_doc.save(source, deflate=True)
        line_doc.close()

        line_session = CleanEditSession(str(source))
        line_doc = pymupdf.open(source)
        line_engine = PdfEngine()
        line_spans = line_engine.extract_spans(line_doc[0])
        selected = line_engine.full_line_spans(line_spans, line_spans[0])
        plan = line_engine.plan_delete_line_reflow(
            line_spans,
            selected,
            line_doc[0].rect,
        )
        line_result = line_engine.delete_full_line_with_reflow(
            line_doc[0],
            selected,
            [dict(item) for item in plan["shifted_items"]],
        )
        line_session.record_line_reflow_operation(
            page_number=1,
            deleted_text=line_result["deleted_text"],
            deleted_bbox=line_result["deleted_bbox"],
            moved_lines=line_result["moved_lines"],
        )
        _, line_audit_path, line_report_path = line_session.export_clean(
            line_doc,
            str(output),
        )
        line_doc.close()
        line_report = json.loads(Path(line_report_path).read_text())
        assert line_report["ok"] is True
        line_audit = json.loads(Path(line_audit_path).read_text())
        flattening = line_audit["structure_preservation"][
            "line_reflow_form_flattening"
        ]
        assert flattening
        assert flattening[0]["wrapper_forms_inlined"] == 3
        assert flattening[0]["rendering_preserved"] is True
        line_check = pymupdf.open(output)
        try:
            assert "First removable line" not in line_check[0].get_text()
            assert not any(
                name.startswith("fzFrm")
                for _xref, name, _parent, _bbox in line_check[0].get_xobjects()
            )
        finally:
            line_check.close()

        # Replacing the same field several times must validate only its latest
        # requested state.  Returning to an earlier intermediate value is
        # legitimate and must not make that historical edit fail Save As.
        source = root / "repeated-replacement-source.pdf"
        output = root / "repeated-replacement-output.pdf"
        repeated = pymupdf.open()
        page = repeated.new_page(width=400, height=220)
        page.insert_text((45, 90), "Original Person", fontname="tiro", fontsize=9)
        repeated.save(source, deflate=True)
        repeated.close()

        session = CleanEditSession(str(source))
        doc = pymupdf.open(source)
        engine = PdfEngine()
        candidate = FontCandidate(
            path=None,
            family="Times-Roman",
            full_name="Times-Roman",
            weight=400,
            source="Base14",
            base14_name="tiro",
        )
        replacement_chain = [
            ("Original Person", "Herman Adale Cadoree"),
            ("Herman Adale Cadoree", "Herman Adale Cadoreee"),
            ("Herman Adale Cadoreee", "Herman Adale Cadoreeee"),
            ("Herman Adale Cadoreeee", "Herman Adale Cadoreeeee"),
            ("Herman Adale Cadoreeeee", "Herman Adale Cadoreee"),
            ("Herman Adale Cadoreee", "Herman Adale Cadoree"),
            ("Herman Adale Cadoree", "Herman Adale Cadoreeeeee"),
        ]
        selected_span = next(
            item for item in engine.extract_spans(doc[0])
            if item.text == replacement_chain[0][0]
        )
        for old_text, new_text in replacement_chain:
            page = doc[0]
            span = selected_span
            assert span.text == old_text
            new_width = engine.replacement_width(
                candidate,
                new_text,
                span.size,
            )
            plan = engine.plan_horizontal_expansion(
                page,
                engine.extract_spans(page),
                span,
                new_width,
                expansion_direction="right",
            )
            assert plan["ok"] is True
            final_span = engine.replace_with_horizontal_reflow(
                page,
                span,
                new_text,
                candidate,
                plan["expanded_bbox"],
                [],
                replacement_origin=plan["replacement_origin"],
            )
            session.record_edit(
                page_number=1,
                original_text=old_text,
                replacement_text=new_text,
                original_bbox=span.bbox,
                final_bbox=final_span.bbox,
                origin=span.origin,
                font_family=candidate.family,
                font_face=candidate.display_name,
                font_source=candidate.source,
                font_size=span.size,
                color=span.color,
                mode="repeated-replacement-regression",
            )
            selected_span = final_span

        _, _, validation_path = session.export_clean(doc, str(output))
        doc.close()
        validation = json.loads(Path(validation_path).read_text(encoding="utf-8"))
        text_checks = validation["checks"]
        assert validation["text_ok"] is True
        assert text_checks[0]["superseded"] is True
        assert text_checks[1]["superseded"] is True
        assert text_checks[1]["passed"] is True
        assert all(item["superseded"] for item in text_checks[:-1])
        assert text_checks[-1]["superseded"] is False
        repeated_check = pymupdf.open(output)
        try:
            final_text = repeated_check[0].get_text("text")
            assert "Original Person" not in final_text
            assert final_text.count("Herman Adale Cadoreeeeee") == 1
        finally:
            repeated_check.close()

        # Equal text on another part of the same page is not a supersession.
        # Geometry must also identify the same physical field.
        source = root / "same-text-different-field-source.pdf"
        output = root / "same-text-different-field-output.pdf"
        separate = pymupdf.open()
        page = separate.new_page(width=400, height=220)
        page.insert_text((45, 70), "First Source", fontname="tiro", fontsize=9)
        page.insert_text((45, 140), "Shared Value", fontname="tiro", fontsize=9)
        separate.save(source, deflate=True)
        separate.close()

        session = CleanEditSession(str(source))
        doc = pymupdf.open(source)
        operations = [
            ("First Source", "Shared Value"),
            ("Shared Value", "Final Value"),
        ]
        for old_text, new_text in operations:
            spans = engine.extract_spans(doc[0])
            if old_text == "Shared Value":
                span = max(
                    (item for item in spans if item.text == old_text),
                    key=lambda item: item.bbox[1],
                )
            else:
                span = next(item for item in spans if item.text == old_text)
            engine.replace_text(doc[0], span, new_text, candidate)
            final_span = min(
                (
                    item for item in engine.extract_spans(doc[0])
                    if item.text == new_text
                ),
                key=lambda item: abs(item.bbox[1] - span.bbox[1]),
            )
            session.record_edit(
                page_number=1,
                original_text=old_text,
                replacement_text=new_text,
                original_bbox=span.bbox,
                final_bbox=final_span.bbox,
                origin=span.origin,
                font_family=candidate.family,
                font_face=candidate.display_name,
                font_source=candidate.source,
                font_size=span.size,
                color=span.color,
                mode="separate-field-regression",
            )

        _, _, validation_path = session.export_clean(doc, str(output))
        doc.close()
        validation = json.loads(Path(validation_path).read_text(encoding="utf-8"))
        text_checks = validation["checks"]
        assert validation["text_ok"] is True
        assert text_checks[0]["superseded"] is False
        assert text_checks[1]["superseded"] is False

        # Explicit missing-glyph fallback: replace searchable source text with
        # filled vector contours from a local/DB-style TTF without embedding a
        # replacement font resource. The validator checks both source removal
        # and the reopened vector drawing.
        source = root / "outline-source.pdf"
        output = root / "outline-output.pdf"
        make_source(source, xref_stream=False)
        outline_candidate = next(
            candidate
            for candidate in SystemFontIndex().iter_candidates()
            if candidate.path
            and font_path_has_text(candidate.path, "Vector Omega Ω")[0]
        )
        session = CleanEditSession(str(source))
        doc = pymupdf.open(source)
        page = doc[0]
        span = next(
            item for item in engine.extract_spans(page)
            if item.text == "Original line"
        )
        width = engine.replacement_width(
            outline_candidate,
            "Vector Omega Ω",
            span.size,
        )
        plan = engine.plan_horizontal_expansion(
            page,
            engine.extract_spans(page),
            span,
            width,
            expansion_direction="right",
        )
        assert plan["ok"] is True
        updated_span, outline_bbox = engine.replace_with_outline_reflow(
            page,
            span,
            "Vector Omega Ω",
            outline_candidate,
            plan["expanded_bbox"],
            [],
            replacement_origin=plan["replacement_origin"],
        )
        session.record_outline_edit(
            page_number=1,
            original_text=span.text,
            replacement_text="Vector Omega Ω",
            original_bbox=span.bbox,
            final_bbox=updated_span.bbox,
            outline_bbox=outline_bbox,
            origin=updated_span.origin,
            font_family=outline_candidate.family,
            font_face=outline_candidate.display_name,
            font_source="test database outline source",
            font_size=span.size,
            color=span.color,
        )
        _, audit_path, validation_path = session.export_clean(doc, str(output))
        doc.close()

        outline_check = pymupdf.open(output)
        try:
            assert "Original line" not in outline_check[0].get_text("text")
            assert "Vector Omega" not in outline_check[0].get_text("text")
            assert outline_check[0].get_drawings()
            assert "Untouched line" in outline_check[1].get_text("text")
        finally:
            outline_check.close()

        validation = json.loads(Path(validation_path).read_text(encoding="utf-8"))
        assert validation["text_ok"] is True
        outline_validation = next(
            item for item in validation["checks"]
            if item.get("operation_type") == "outline"
        )
        assert outline_validation["matching_vector_drawings"] >= 1
        assert outline_validation["passed"] is True
        audit = json.loads(Path(audit_path).read_text(encoding="utf-8"))
        assert audit["edits"][0]["searchable_replacement"] is False

        # Missing structural metadata must stay missing: export must not create
        # an Info dictionary, XMP packet, or trailer ID as a side effect.
        source = root / "minimal-source.pdf"
        output = root / "minimal-output.pdf"
        minimal = pymupdf.open()
        page = minimal.new_page(width=300, height=200)
        page.insert_text((40, 70), "Minimal old", fontsize=11)
        minimal.save(source, no_new_id=True)
        minimal.close()

        session = CleanEditSession(str(source))
        assert session.source_structure["info_xref"] is None
        assert session.source_structure["metadata_xref"] is None
        assert session.source_structure["trailer_id"]["type"] == "null"

        doc = pymupdf.open(source)
        # Reproduce the save failure that prompted this regression: a read-only
        # probe omitted no_new_id=True and mutated the live trailer; another
        # transient Catalog key represents writer/probe drift.
        doc.tobytes(garbage=0, deflate=False)
        assert doc.xref_get_key(-1, "ID")[0] == "array"
        doc.xref_set_key(doc.pdf_catalog(), "TransientProbe", "true")
        engine = PdfEngine()
        candidate = FontCandidate(
            path=None,
            family="Helvetica",
            full_name="Helvetica",
            weight=400,
            source="Base14",
            base14_name="helv",
        )
        span = next(
            item for item in engine.extract_spans(doc[0])
            if item.text == "Minimal old"
        )
        engine.replace_text(doc[0], span, "Minimal new", candidate)
        new_span = next(
            item for item in engine.extract_spans(doc[0])
            if item.text == "Minimal new"
        )
        session.record_edit(
            page_number=1,
            original_text=span.text,
            replacement_text=new_span.text,
            original_bbox=span.bbox,
            final_bbox=new_span.bbox,
            origin=span.origin,
            font_family=candidate.family,
            font_face=candidate.display_name,
            font_source=candidate.source,
            font_size=span.size,
            color=span.color,
            mode="minimal-structure-regression",
        )
        _, audit_path, _ = session.export_clean(doc, str(output))
        doc.close()

        check = pymupdf.open(output)
        try:
            assert check.xref_get_key(-1, "Info")[0] == "null"
            assert check.xref_xml_metadata() == 0
            assert check.xref_get_key(-1, "ID")[0] == "null"
            catalog = check.xref_object(check.pdf_catalog(), compressed=False)
            assert "/Type /Catalog" in catalog
            assert "/TransientProbe" not in catalog
        finally:
            check.close()

        audit = json.loads(Path(audit_path).read_text(encoding="utf-8"))
        restored = audit["structure_preservation"]["identity_restored_before_save"]
        assert "trailer /ID" in restored
        assert "Catalog/Root object" in restored

    print("PDF structural preservation test: PASS")


if __name__ == "__main__":
    main()
