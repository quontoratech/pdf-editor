from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pymupdf

from web_app import WebEditorSession


def _make_table_with_fixed_footer(path: Path) -> None:
    doc = pymupdf.open()
    page = doc.new_page(width=520, height=360)
    xs = (35.0, 185.0, 335.0, 455.0)
    for baseline, prefix in (
        (60.0, "Header"),
        (84.0, "Data"),
        (108.0, "Total"),
    ):
        for column, x in enumerate(xs, start=1):
            page.insert_text(
                (x, baseline),
                f"{prefix}{column}",
                fontname="tiro",
                fontsize=9,
            )
    for y in (69.0, 93.0, 117.0):
        page.draw_line((35, y), (490, y), color=(0.2,), width=0.3)

    # These lines deliberately share the table's broad horizontal band.  A
    # geometry-unaware reflow used to copy every one of them into a separate
    # full-page Form, which made one delete take minutes on a small VPS.
    for index in range(12):
        page.insert_text(
            (95, 220 + index * 9),
            f"Fixed footer line {index + 1}",
            fontname="tiro",
            fontsize=7,
        )

    doc.save(path, garbage=0, deflate=True, no_new_id=True)
    doc.close()


def _web_session(source: Path, root: Path) -> WebEditorSession:
    return WebEditorSession(
        source,
        source.name,
        root,
        "performance-test-owner",
        "performance-tester",
    )


def _select_row(session: WebEditorSession, cell_text: str) -> None:
    session.selected_span = next(
        span for span in session.spans if span.text == cell_text
    )
    session.select_full_line()


def test_table_delete_stops_before_fixed_footer_and_skips_full_rescan():
    with tempfile.TemporaryDirectory(prefix="table_delete_perf_") as tmp:
        root = Path(tmp)
        source = root / "source.pdf"
        _make_table_with_fixed_footer(source)
        session = _web_session(source, root)
        try:
            _select_row(session, "Header1")
            plan = session.engine.plan_delete_line_reflow(
                session.spans,
                session.selected_line_spans,
                session.doc[0].rect,
                page=session.doc[0],
            )
            shifted_text = {
                item["span"].text for item in plan.get("shifted_items", [])
            }
            assert "Data1" in shifted_text
            assert "Total4" in shifted_text
            assert not any(text.startswith("Fixed footer") for text in shifted_text)

            footer_before = {
                span.text: tuple(span.origin)
                for span in session.spans
                if span.text.startswith("Fixed footer")
            }

            def forbidden_rescan(*_args, **_kwargs):
                raise AssertionError(
                    "Structural delete rebuilt the recursive full-page text dictionary."
                )

            session.engine.extract_spans = forbidden_rescan
            session.delete_selected_line(remove_table_rule=False)

            footer_after = {
                span.text: tuple(span.origin)
                for span in session.spans
                if span.text.startswith("Fixed footer")
            }
            assert footer_after == footer_before
            assert not any(span.text.startswith("Header") for span in session.spans)
            assert sum(span.text.startswith("Data") for span in session.spans) == 4
        finally:
            session.close()


def test_repeated_table_deletes_close_the_exact_separator_height():
    """Two deletions must not accumulate a paragraph-leading sized gap."""
    with tempfile.TemporaryDirectory(prefix="table_delete_spacing_") as tmp:
        root = Path(tmp)
        source = root / "source.pdf"
        _make_table_with_fixed_footer(source)
        session = _web_session(source, root)
        try:
            footer_before = {
                span.text: tuple(span.origin)
                for span in session.spans
                if span.text.startswith("Fixed footer")
            }

            for label in ("Header1", "Data1"):
                _select_row(session, label)
                plan = session.engine.plan_delete_line_reflow(
                    session.spans,
                    session.selected_line_spans,
                    session.doc[0].rect,
                    page=session.doc[0],
                )
                # The table's rules are 24 pt apart. Compact 9 pt footer text
                # must not contaminate the row-height calculation.
                assert abs(float(plan["shift_dy"]) + 24.0) <= 0.05
                session.delete_selected_line(remove_table_rule=True)

            remaining = [
                span
                for span in session.spans
                if span.text.startswith("Total")
            ]
            assert len(remaining) == 4
            assert {
                round(float(span.origin[1]), 2) for span in remaining
            } == {60.0}

            footer_after = {
                span.text: tuple(span.origin)
                for span in session.spans
                if span.text.startswith("Fixed footer")
            }
            assert footer_after == footer_before

            long_rule_ys = sorted(
                round(float(rule["y"]), 2)
                for rule in session.engine.horizontal_table_rules(session.doc[0])
                if float(rule["x1"]) - float(rule["x0"]) > 400
            )
            assert long_rule_ys == [69.0]
        finally:
            session.close()


def test_row_insert_updates_variable_cell_index_without_full_rescan():
    with tempfile.TemporaryDirectory(prefix="table_insert_perf_") as tmp:
        root = Path(tmp)
        source = root / "source.pdf"
        _make_table_with_fixed_footer(source)
        session = _web_session(source, root)
        try:
            _select_row(session, "Data1")
            assert session.selected_row_info["column_count"] == 4

            footer_before = {
                span.text: tuple(span.origin)
                for span in session.spans
                if span.text.startswith("Fixed footer")
            }

            def forbidden_rescan(*_args, **_kwargs):
                raise AssertionError(
                    "Row insertion rebuilt the recursive full-page text dictionary."
                )

            session.engine.extract_spans = forbidden_rescan
            result = session.add_selected_row_below()
            assert result["column_count"] == 4
            for column in range(1, 5):
                copies = [
                    span for span in session.spans
                    if span.text == f"Data{column}"
                ]
                assert len(copies) == 2
                assert sorted(round(float(span.origin[1]), 1) for span in copies) == [
                    84.0,
                    108.0,
                ]

            footer_after = {
                span.text: tuple(span.origin)
                for span in session.spans
                if span.text.startswith("Fixed footer")
            }
            assert footer_after == footer_before
        finally:
            session.close()


def test_row_insert_cascades_only_through_colliding_following_paragraph():
    with tempfile.TemporaryDirectory(prefix="table_insert_collision_") as tmp:
        root = Path(tmp)
        source = root / "source.pdf"

        doc = pymupdf.open()
        page = doc.new_page(width=520, height=360)
        for baseline, prefix in (
            (60.0, "Header"),
            (84.0, "Data"),
            (108.0, "Service"),
            (132.0, "Ending"),
        ):
            page.insert_text((35, baseline), f"{prefix} label", fontname="tiro", fontsize=9)
            page.insert_text((430, baseline), f"{prefix} value", fontname="tiro", fontsize=9)
        for y in (69.0, 93.0, 117.0):
            page.draw_line((35, y), (490, y), color=(0.2,), width=0.3)

        # The first paragraph begins outside the table-rule cluster but would
        # overlap the shifted unruled Ending row after insertion. Its second
        # line must follow it; the later section has enough whitespace and must
        # remain fixed.
        page.insert_text((35, 159), "Following paragraph line 1", fontname="tiro", fontsize=9)
        page.insert_text((35, 171), "Following paragraph line 2", fontname="tiro", fontsize=9)
        page.insert_text((35, 220), "Distant next section", fontname="tiro", fontsize=9)
        doc.save(source, garbage=0, deflate=True, no_new_id=True)
        doc.close()

        session = _web_session(source, root)
        try:
            _select_row(session, "Data label")
            result = session.add_selected_row_below()
            assert result["column_count"] == 2

            positions = {
                span.text: round(float(span.origin[1]), 2)
                for span in session.spans
                if span.text in {
                    "Following paragraph line 1",
                    "Following paragraph line 2",
                    "Distant next section",
                }
            }
            assert positions == {
                "Following paragraph line 1": 183.0,
                "Following paragraph line 2": 195.0,
                "Distant next section": 220.0,
            }

            ending = next(span for span in session.spans if span.text == "Ending label")
            paragraph = next(
                span
                for span in session.spans
                if span.text == "Following paragraph line 1"
            )
            assert float(ending.bbox[3]) < float(paragraph.bbox[1])
        finally:
            session.close()


def test_cloned_cell_can_be_edited_and_exported_without_extra_structure():
    with tempfile.TemporaryDirectory(prefix="table_insert_edit_") as tmp:
        root = Path(tmp)
        source = root / "source.pdf"
        _make_table_with_fixed_footer(source)
        session = _web_session(source, root)
        try:
            _select_row(session, "Data1")
            session.add_selected_row_below()
            cloned = sorted(
                [span for span in session.spans if span.text == "Data1"],
                key=lambda span: float(span.origin[1]),
            )[1]
            session.selected_span = cloned
            session.selected_line_spans = []
            session.selected_row_info = None
            session.replace_selected_text("Entry1")

            output = session.export()
            validation = json.loads(
                Path(str(output) + ".validation.json").read_text(
                    encoding="utf-8"
                )
            )
            assert validation["ok"] is True
            assert validation["text_ok"] is True
            assert validation["structure_ok"] is True

            check = pymupdf.open(output)
            try:
                assert check[0].get_xobjects() == []
                text = check[0].get_text("text")
                assert text.count("Data1") == 1
                assert text.count("Entry1") == 1
            finally:
                check.close()
        finally:
            session.close()


def test_cloned_row_can_be_deleted_without_matching_adjacent_template():
    with tempfile.TemporaryDirectory(prefix="table_insert_delete_") as tmp:
        root = Path(tmp)
        source = root / "source.pdf"
        _make_table_with_fixed_footer(source)
        session = _web_session(source, root)
        try:
            _select_row(session, "Data1")
            session.add_selected_row_below()
            cloned = sorted(
                [span for span in session.spans if span.text == "Data1"],
                key=lambda span: float(span.origin[1]),
            )[1]
            session.selected_span = cloned
            session.select_full_line()
            session.delete_selected_line(remove_table_rule=False)

            output = session.export()
            validation = json.loads(
                Path(str(output) + ".validation.json").read_text(
                    encoding="utf-8"
                )
            )
            assert validation["ok"] is True
            assert validation["structure_ok"] is True
            check = pymupdf.open(output)
            try:
                assert check[0].get_xobjects() == []
                assert check[0].get_text("text").count("Data1") == 1
            finally:
                check.close()
        finally:
            session.close()


def test_original_template_row_can_be_deleted_before_identical_clone():
    with tempfile.TemporaryDirectory(prefix="table_insert_delete_original_") as tmp:
        root = Path(tmp)
        source = root / "source.pdf"
        _make_table_with_fixed_footer(source)
        session = _web_session(source, root)
        try:
            _select_row(session, "Data1")
            session.add_selected_row_below()

            original = sorted(
                [span for span in session.spans if span.text == "Data1"],
                key=lambda span: float(span.origin[1]),
            )[0]
            session.selected_span = original
            session.select_full_line()
            session.delete_selected_line(remove_table_rule=True)

            remaining = [span for span in session.spans if span.text == "Data1"]
            assert len(remaining) == 1
            assert round(float(remaining[0].origin[1]), 2) == 84.0

            output = session.export()
            validation = json.loads(
                Path(str(output) + ".validation.json").read_text(
                    encoding="utf-8"
                )
            )
            assert validation["ok"] is True
            assert validation["text_ok"] is True
            assert validation["structure_ok"] is True

            check = pymupdf.open(output)
            try:
                assert check[0].get_xobjects() == []
                assert check[0].get_text("text").count("Data1") == 1
            finally:
                check.close()
        finally:
            session.close()


def test_repeated_identical_top_row_deletes_retire_stale_absence_check():
    """Only the newest operation owns a visual slot reused by identical rows."""
    with tempfile.TemporaryDirectory(prefix="table_identical_reflow_save_") as tmp:
        root = Path(tmp)
        source = root / "source.pdf"
        _make_table_with_fixed_footer(source)
        session = _web_session(source, root)
        try:
            # Produce three identical adjacent rows, then delete the row in the
            # top slot twice. One valid clone must remain in that same slot.
            for _ in range(2):
                _select_row(session, "Data1")
                session.add_selected_row_below()
            for _ in range(2):
                top = sorted(
                    [span for span in session.spans if span.text == "Data1"],
                    key=lambda span: float(span.origin[1]),
                )[0]
                session.selected_span = top
                session.select_full_line()
                session.delete_selected_line(remove_table_rule=True)

            remaining = [span for span in session.spans if span.text == "Data1"]
            assert len(remaining) == 1
            assert round(float(remaining[0].origin[1]), 2) == 84.0

            line_edits = [
                edit
                for edit in session.edit_session.edits
                if edit.get("operation_type") == "line_reflow"
            ]
            assert len(line_edits) == 2
            assert line_edits[0]["deleted_check_active"] is False
            assert line_edits[1]["deleted_check_active"] is True

            output = session.export()
            validation = json.loads(
                Path(str(output) + ".validation.json").read_text(
                    encoding="utf-8"
                )
            )
            assert validation["ok"] is True
            assert validation["text_ok"] is True
            assert validation["structure_ok"] is True

            check = pymupdf.open(output)
            try:
                assert check[0].get_xobjects() == []
                assert check[0].get_text("text").count("Data1") == 1
            finally:
                check.close()
        finally:
            session.close()


if __name__ == "__main__":
    test_table_delete_stops_before_fixed_footer_and_skips_full_rescan()
    test_repeated_table_deletes_close_the_exact_separator_height()
    test_row_insert_updates_variable_cell_index_without_full_rescan()
    test_row_insert_cascades_only_through_colliding_following_paragraph()
    test_cloned_cell_can_be_edited_and_exported_without_extra_structure()
    test_cloned_row_can_be_deleted_without_matching_adjacent_template()
    test_original_template_row_can_be_deleted_before_identical_clone()
    test_repeated_identical_top_row_deletes_retire_stale_absence_check()
    print("Structural-edit performance tests: PASS")
