from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pymupdf

from app.clean_edit_session import CleanEditSession
from app.pdf_engine import PdfEngine


def make_variable_table_document(column_count: int):
    doc = pymupdf.open()
    page = doc.new_page(width=520, height=300)
    left = 35.0
    right = 485.0
    step = (right - left) / max(1, column_count - 1)
    xs = [left + step * index for index in range(column_count)]

    for baseline, prefix in ((60, "Template"), (84, "Existing"), (108, "Total")):
        for index, x in enumerate(xs, start=1):
            page.insert_text(
                (x, baseline),
                f"{prefix}{index}",
                fontname="tiro",
                fontsize=9,
            )

    for y in (69, 93, 117):
        page.draw_line(
            (left, y),
            (right + 25, y),
            color=(0.15, 0.15, 0.15),
            width=0.35,
        )

    # Unrelated artwork that must not move or be cloned.
    page.draw_line((18, 35), (18, 160), color=(1, 0, 0), width=1)
    raw = doc.tobytes(deflate=True, no_new_id=True)
    doc.close()
    return pymupdf.open(stream=raw, filetype="pdf")


def long_rule_ys(engine: PdfEngine, page):
    return sorted(
        round(float(rule["y"]), 2)
        for rule in engine.horizontal_table_rules(page)
        if float(rule["x1"]) - float(rule["x0"]) > 400
    )


def test_template_row_clone_supports_variable_column_counts(column_count):
    doc = make_variable_table_document(column_count)
    try:
        engine = PdfEngine()
        page = doc[0]
        spans = engine.extract_spans(page)
        first = next(span for span in spans if span.text == "Template1")
        selected = engine.full_line_spans(spans, first, page=page)

        assert len(selected) == column_count
        plan = engine.plan_insert_table_row(page, spans, selected)
        assert plan["column_count"] == column_count
        assert abs(float(plan["shift_dy"]) - 24.0) <= 0.05

        result = engine.insert_table_row_with_reflow(page, selected, plan)
        assert result["column_count"] == column_count
        assert len(result["inserted_lines"]) == column_count
        assert len(result["moved_table_rules"]) == 2
        assert len(result["inserted_table_rules"]) == 1
        assert long_rule_ys(engine, page) == [69.0, 93.0, 117.0, 141.0]

        extracted = engine.extract_spans(page)
        for index in range(1, column_count + 1):
            template_occurrences = [
                span for span in extracted if span.text == f"Template{index}"
            ]
            assert len(template_occurrences) == 2
            assert sorted(round(float(span.origin[1]), 1) for span in template_occurrences) == [
                60.0,
                84.0,
            ]

            moved = next(
                span for span in extracted if span.text == f"Existing{index}"
            )
            assert abs(float(moved.origin[1]) - 108.0) <= 0.1

        # The unrelated vertical drawing remains exactly where it started.
        paths = page.get_drawings(extended=True)
        assert any(
            len(path.get("items") or []) == 1
            and path["items"][0][0] == "l"
            and abs(float(path["items"][0][1].x) - 18.0) < 0.1
            and abs(float(path["items"][0][2].x) - 18.0) < 0.1
            and abs(float(path["items"][0][1].y) - 35.0) < 0.1
            and abs(float(path["items"][0][2].y) - 160.0) < 0.1
            for path in paths
        )
    finally:
        doc.close()


def test_non_table_line_is_rejected_without_mutation():
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=220)
    page.insert_text((40, 70), "Ordinary paragraph", fontname="tiro", fontsize=10)
    page.insert_text((40, 95), "Later paragraph", fontname="tiro", fontsize=10)
    raw = doc.tobytes(deflate=True, no_new_id=True)
    doc.close()
    doc = pymupdf.open(stream=raw, filetype="pdf")
    try:
        engine = PdfEngine()
        page = doc[0]
        spans = engine.extract_spans(page)
        selected = engine.full_line_spans(spans, spans[0], page=page)
        before = doc.tobytes(garbage=0, clean=False, deflate=False, no_new_id=True)
        try:
            engine.plan_insert_table_row(page, spans, selected)
        except ValueError as exc:
            assert "multi-column table row" in str(exc)
        else:
            raise AssertionError("Ordinary paragraph was accepted as a table row.")
        after = doc.tobytes(garbage=0, clean=False, deflate=False, no_new_id=True)
        assert after == before
    finally:
        doc.close()


def test_table_row_clone_clean_export_and_audit():
    with tempfile.TemporaryDirectory(prefix="table_row_insert_") as tmp:
        root = Path(tmp)
        source = root / "source.pdf"
        output = root / "output.pdf"

        fixture = make_variable_table_document(5)
        fixture.save(source, garbage=0, deflate=True, no_new_id=True)
        fixture.close()

        doc = pymupdf.open(source)
        engine = PdfEngine()
        page = doc[0]
        spans = engine.extract_spans(page)
        selected = engine.full_line_spans(
            spans,
            next(span for span in spans if span.text == "Template1"),
            page=page,
        )
        plan = engine.plan_insert_table_row(page, spans, selected)
        result = engine.insert_table_row_with_reflow(page, selected, plan)

        session = CleanEditSession(str(source))
        session.record_table_row_insert_operation(
            page_number=1,
            template_text=plan["template_text"],
            inserted_bbox=result["inserted_bbox"],
            inserted_lines=result["inserted_lines"],
            moved_lines=result["moved_lines"],
            moved_table_rules=result["moved_table_rules"],
            inserted_table_rules=result["inserted_table_rules"],
            column_count=result["column_count"],
            position="below",
        )
        _, audit_path, validation_path = session.export_clean(doc, str(output))
        doc.close()

        validation = json.loads(Path(validation_path).read_text(encoding="utf-8"))
        assert validation["ok"] is True
        assert validation["text_ok"] is True
        assert validation["structure_ok"] is True

        audit = json.loads(Path(audit_path).read_text(encoding="utf-8"))
        row_edit = audit["edits"][0]
        assert row_edit["operation_type"] == "table_row_insert"
        assert row_edit["column_count"] == 5
        assert row_edit["immediate_validation_passed"] is True

        check = pymupdf.open(output)
        try:
            assert check[0].get_xobjects() == []
            assert long_rule_ys(engine, check[0]) == [69.0, 93.0, 117.0, 141.0]
            for index in range(1, 6):
                assert check[0].get_text("text").count(f"Template{index}") == 2
        finally:
            check.close()


if __name__ == "__main__":
    for _column_count in (2, 3, 4, 5, 6):
        test_template_row_clone_supports_variable_column_counts(_column_count)
    test_non_table_line_is_rejected_without_mutation()
    test_table_row_clone_clean_export_and_audit()
    print("Table-row insertion tests: PASS")
