from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pymupdf

from app.clean_edit_session import CleanEditSession
from app.pdf_engine import PdfEngine


def make_table_document():
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=250)

    rows = (
        (60, ((40, "Header A"), (180, "Header B"), (300, "Header C"))),
        (85, ((40, "Row A"), (180, "Row B"), (300, "Row C"))),
        (110, ((40, "Total A"), (180, "Total B"), (300, "Total C"))),
    )

    for y, cells in rows:
        for x, text in cells:
            page.insert_text((x, y), text, fontname="tiro", fontsize=10)

    for y in (70, 95, 120):
        page.draw_line(
            (40, y),
            (360, y),
            color=(0.2, 0.2, 0.2),
            width=0.4,
        )

    # Unrelated vector artwork must not be touched by table-rule reflow.
    page.draw_line((20, 45), (20, 140), color=(1, 0, 0), width=1)
    page.draw_line((250, 160), (280, 160), color=(0, 0, 1), width=1)

    raw = doc.tobytes(deflate=True, no_new_id=True)
    doc.close()
    return pymupdf.open(stream=raw, filetype="pdf")


def top_level_lines(page):
    output = []
    for drawing in page.get_drawings(extended=True):
        if int(drawing.get("level") or 0) > 1:
            continue
        items = list(drawing.get("items") or [])
        if len(items) != 1 or items[0][0] != "l":
            continue
        output.append((pymupdf.Point(items[0][1]), pymupdf.Point(items[0][2])))
    return output


def prepare_table_delete(doc, *, remove_rule=False):
    engine = PdfEngine()
    page = doc[0]
    spans = engine.extract_spans(page)
    header = next(span for span in spans if span.text == "Header A")
    selected = engine.full_line_spans(spans, header, page=page)
    plan = engine.plan_delete_line_reflow(spans, selected, page.rect)
    rule_plan = engine.plan_table_rule_reflow(
        page,
        selected,
        plan["shifted_items"],
        plan["shift_dy"],
        remove_deleted_row_rule=remove_rule,
    )
    return engine, page, selected, plan, rule_plan


def test_table_row_rules_move_with_text_and_unrelated_art_stays_put():
    doc = make_table_document()
    try:
        engine, page, selected, plan, rule_plan = prepare_table_delete(doc)

        assert [span.text for span in selected] == [
            "Header A",
            "Header B",
            "Header C",
        ]
        assert len(rule_plan["actions"]) == 3
        assert all(action["action"] == "move" for action in rule_plan["actions"])

        result = engine.delete_full_line_with_reflow(
            page,
            selected,
            plan["shifted_items"],
            table_rule_plan=rule_plan,
        )

        assert len(result["moved_table_rules"]) == 3
        assert result["removed_table_rules"] == []

        expected_rule_ys = sorted(
            round(float(action["target_y"]), 2)
            for action in rule_plan["actions"]
        )
        actual_rule_ys = sorted(
            round(float(rule["y"]), 2)
            for rule in engine.horizontal_table_rules(page)
            if float(rule["x1"]) - float(rule["x0"]) > 300
        )
        assert actual_rule_ys == expected_rule_ys

        page_text = page.get_text("text")
        assert "Header A" not in page_text
        assert "Header B" not in page_text
        assert "Header C" not in page_text
        assert "Row A" in page_text
        assert "Total C" in page_text

        lines = top_level_lines(page)
        assert any(
            abs(p1.x - 20) < 0.1
            and abs(p2.x - 20) < 0.1
            and abs(p1.y - 45) < 0.1
            and abs(p2.y - 140) < 0.1
            for p1, p2 in lines
        )
        assert any(
            abs(p1.x - 250) < 0.1
            and abs(p2.x - 280) < 0.1
            and abs(p1.y - 160) < 0.1
            and abs(p2.y - 160) < 0.1
            for p1, p2 in lines
        )
    finally:
        doc.close()


def test_optional_deleted_row_separator_is_removed():
    doc = make_table_document()
    try:
        engine, page, selected, plan, rule_plan = prepare_table_delete(
            doc,
            remove_rule=True,
        )

        assert rule_plan["actions"][0]["action"] == "remove"
        assert [action["action"] for action in rule_plan["actions"][1:]] == [
            "move",
            "move",
        ]

        result = engine.delete_full_line_with_reflow(
            page,
            selected,
            plan["shifted_items"],
            table_rule_plan=rule_plan,
        )

        assert len(result["removed_table_rules"]) == 1
        assert len(result["moved_table_rules"]) == 2

        expected_rule_ys = sorted(
            round(float(action["target_y"]), 2)
            for action in rule_plan["actions"]
            if action["action"] == "move"
        )
        actual_rule_ys = sorted(
            round(float(rule["y"]), 2)
            for rule in engine.horizontal_table_rules(page)
            if float(rule["x1"]) - float(rule["x0"]) > 300
        )
        assert actual_rule_ys == expected_rule_ys
    finally:
        doc.close()


def test_long_paragraph_divider_is_not_treated_as_a_table_rule():
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=220)
    page.insert_text((40, 70), "Ordinary paragraph", fontname="tiro", fontsize=10)
    page.insert_text((145, 70), "continued", fontname="tiro", fontsize=10)
    page.insert_text((40, 95), "Later paragraph", fontname="tiro", fontsize=10)
    page.draw_line((40, 80), (360, 80), color=(0.3, 0.3, 0.3), width=0.5)

    raw = doc.tobytes(deflate=True, no_new_id=True)
    doc.close()
    doc = pymupdf.open(stream=raw, filetype="pdf")

    try:
        engine = PdfEngine()
        page = doc[0]
        spans = engine.extract_spans(page)
        selected = engine.full_line_spans(spans, spans[0], page=page)
        plan = engine.plan_delete_line_reflow(spans, selected, page.rect)
        rule_plan = engine.plan_table_rule_reflow(
            page,
            selected,
            plan["shifted_items"],
            plan["shift_dy"],
            remove_deleted_row_rule=True,
        )
        assert rule_plan["actions"] == []
    finally:
        doc.close()


def test_short_decorative_line_cannot_expand_a_multi_column_selection():
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=220)
    for x, text in ((40, "Cell A"), (180, "Cell B"), (300, "Cell C")):
        page.insert_text((x, 70), text, fontname="tiro", fontsize=10)
    page.draw_line((40, 80), (70, 80), color=(0, 0, 0), width=0.5)

    raw = doc.tobytes(deflate=True, no_new_id=True)
    doc.close()
    doc = pymupdf.open(stream=raw, filetype="pdf")

    try:
        engine = PdfEngine()
        page = doc[0]
        spans = engine.extract_spans(page)
        selected = engine.full_line_spans(spans, spans[0], page=page)
        assert [span.text for span in selected] == ["Cell A"]
    finally:
        doc.close()


def make_close_rule_document():
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=220)
    page.insert_text(
        (45, 74),
        "Searchable text remains",
        fontname="tiro",
        fontsize=10,
    )
    page.draw_line((40, 70.0), (360, 70.0), color=(0.2, 0.2, 0.2), width=0.2)
    page.draw_line((40, 70.6), (360, 70.6), color=(0.2, 0.2, 0.2), width=0.2)
    page.draw_line((20, 40), (20, 150), color=(1, 0, 0), width=1)
    page.draw_line((250, 160), (280, 160), color=(0, 0, 1), width=1)
    page.draw_rect(pymupdf.Rect(300, 175, 350, 195), color=(0, 0.5, 0), width=1)
    raw = doc.tobytes(deflate=True, no_new_id=True)
    doc.close()
    return pymupdf.open(stream=raw, filetype="pdf")


def test_standalone_close_rules_delete_one_by_one_without_text_change():
    doc = make_close_rule_document()
    try:
        engine = PdfEngine()
        page = doc[0]
        text_before = page.get_text("text")
        words_before = page.get_text("words")

        first = engine.delete_horizontal_table_rule_at(page, 200, 70.0)
        assert first["immediate_validation_passed"] is True
        remaining = [
            round(float(rule["y"]), 1)
            for rule in engine.horizontal_table_rules(page)
            if float(rule["x1"]) - float(rule["x0"]) > 300
        ]
        assert remaining == [70.6]
        assert page.get_text("text") == text_before
        assert page.get_text("words") == words_before

        second = engine.delete_horizontal_table_rule_at(page, 200, 70.6)
        assert second["immediate_validation_passed"] is True
        remaining = [
            rule
            for rule in engine.horizontal_table_rules(page)
            if float(rule["x1"]) - float(rule["x0"]) > 300
        ]
        assert remaining == []
        assert page.get_text("text") == text_before
        assert page.get_text("words") == words_before

        lines = top_level_lines(page)
        assert any(
            abs(p1.x - 20) < 0.1
            and abs(p2.x - 20) < 0.1
            and abs(p1.y - 40) < 0.1
            and abs(p2.y - 150) < 0.1
            for p1, p2 in lines
        )
        assert any(
            abs(p1.x - 250) < 0.1
            and abs(p2.x - 280) < 0.1
            and abs(p1.y - 160) < 0.1
            and abs(p2.y - 160) < 0.1
            for p1, p2 in lines
        )
    finally:
        doc.close()


def test_standalone_rule_hit_test_rejects_midpoint_and_miss_without_mutation():
    engine = PdfEngine()
    doc = make_close_rule_document()
    try:
        page = doc[0]
        before = doc.tobytes(garbage=0, clean=False, deflate=False, no_new_id=True)

        for x, y, expected_message in (
            (200, 70.3, "equally close"),
            (200, 110, "No independently stroked"),
            (390, 70, "No independently stroked"),
        ):
            try:
                engine.delete_horizontal_table_rule_at(page, x, y)
            except ValueError as exc:
                assert expected_message in str(exc)
            else:
                raise AssertionError("Unsafe table-line click unexpectedly succeeded.")

        after = doc.tobytes(garbage=0, clean=False, deflate=False, no_new_id=True)
        assert after == before
    finally:
        doc.close()


def test_standalone_rule_delete_clean_export_and_audit():
    with tempfile.TemporaryDirectory(prefix="table_rule_delete_") as tmp:
        root = Path(tmp)
        source = root / "source.pdf"
        output = root / "output.pdf"

        fixture = make_close_rule_document()
        fixture.save(source, garbage=0, deflate=True, no_new_id=True)
        fixture.close()

        session = CleanEditSession(str(source))
        doc = pymupdf.open(source)
        engine = PdfEngine()
        result = engine.delete_horizontal_table_rule_at(doc[0], 200, 70.0)
        session.record_table_rule_delete_operation(
            page_number=1,
            bbox=result["bbox"],
            rule=result["rule"],
            click=result["click"],
        )
        _, audit_path, validation_path = session.export_clean(doc, str(output))
        doc.close()

        validation = json.loads(Path(validation_path).read_text(encoding="utf-8"))
        assert validation["ok"] is True
        assert validation["text_ok"] is True
        assert validation["structure_ok"] is True

        audit = json.loads(Path(audit_path).read_text(encoding="utf-8"))
        assert audit["edits"][0]["operation_type"] == "table_rule_delete"
        assert audit["edits"][0]["immediate_validation_passed"] is True

        check = pymupdf.open(output)
        try:
            assert "Searchable text remains" in check[0].get_text("text")
            remaining = [
                round(float(rule["y"]), 1)
                for rule in engine.horizontal_table_rules(check[0])
                if float(rule["x1"]) - float(rule["x0"]) > 300
            ]
            assert remaining == [70.6]
        finally:
            check.close()


def test_web_standalone_delete_after_line_reflow_supports_undo_and_export():
    # Import locally so the core engine tests stay lightweight when this helper
    # is reused from another module.
    from web_app import WebEditorSession

    with tempfile.TemporaryDirectory(prefix="web_table_rule_delete_") as tmp:
        root = Path(tmp)
        source = root / "source.pdf"
        output = root / "output.pdf"

        fixture = make_table_document()
        fixture.save(source, garbage=0, deflate=True, no_new_id=True)
        fixture.close()

        session = WebEditorSession(
            source,
            source.name,
            root,
            "test-owner",
            "tester",
        )
        try:
            header = next(
                span for span in session.spans if span.text == "Header A"
            )
            session.select_span(header)
            try:
                session.delete_table_line_at(200, 200)
            except ValueError as exc:
                assert "No independently stroked" in str(exc)
            else:
                raise AssertionError("A missed rule click unexpectedly succeeded.")
            assert session.selected_span is header
            assert session.undo_stack == []
            assert session.edit_session.edits == []

            session.select_full_line()
            session.delete_selected_line(remove_table_rule=False)

            def long_rule_ys():
                return [
                    round(float(rule["y"]), 2)
                    for rule in session.engine.horizontal_table_rules(session.doc[0])
                    if float(rule["x1"]) - float(rule["x0"]) > 300
                ]

            rules_after_reflow = long_rule_ys()
            assert len(rules_after_reflow) == 3
            assert session.doc[0].get_xobjects()

            target_y = rules_after_reflow[0]
            text_before = session.doc[0].get_text("text")
            session.delete_table_line_at(200, target_y)
            assert len(long_rule_ys()) == 2
            assert session.doc[0].get_xobjects() == []
            assert session.doc[0].get_text("text") == text_before

            session.undo_last()
            assert long_rule_ys() == rules_after_reflow
            assert session.doc[0].get_xobjects()

            session.delete_table_line_at(200, target_y)
            _, _, validation_path = session.edit_session.export_clean(
                session.doc,
                str(output),
            )
            validation = json.loads(
                Path(validation_path).read_text(encoding="utf-8")
            )
            assert validation["ok"] is True
            assert validation["structure_ok"] is True

            check = pymupdf.open(output)
            try:
                assert check[0].get_xobjects() == []
                assert check[0].get_text("text") == text_before
            finally:
                check.close()
        finally:
            session.close()


if __name__ == "__main__":
    test_table_row_rules_move_with_text_and_unrelated_art_stays_put()
    test_optional_deleted_row_separator_is_removed()
    test_long_paragraph_divider_is_not_treated_as_a_table_rule()
    test_short_decorative_line_cannot_expand_a_multi_column_selection()
    test_standalone_close_rules_delete_one_by_one_without_text_change()
    test_standalone_rule_hit_test_rejects_midpoint_and_miss_without_mutation()
    test_standalone_rule_delete_clean_export_and_audit()
    test_web_standalone_delete_after_line_reflow_supports_undo_and_export()
    print("Table-rule reflow tests: PASS")
