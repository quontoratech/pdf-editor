from app.models import TextSpan
from app.pdf_engine import PdfEngine
from web_app import WebEditorSession


def _span(text, x0, x1, span_index):
    return TextSpan(
        text=text,
        bbox=(float(x0), 40.0, float(x1), 50.0),
        origin=(float(x0), 49.0),
        font="Helvetica",
        size=10.0,
        flags=0,
        color=0,
        span_index=int(span_index),
    )


def _metadata_for(spans):
    # This helper is deliberately independent of disk/session setup: it tests
    # the cheap state metadata used to enable and label the browser button.
    session = WebEditorSession.__new__(WebEditorSession)
    session.engine = PdfEngine()
    session.selected_line_spans = list(spans)
    return session._selected_table_row_info()


def test_row_insert_metadata_counts_two_through_six_columns():
    for expected_columns in range(2, 7):
        spans = [
            _span(f"Cell {index + 1}", index * 80, index * 80 + 32, index)
            for index in range(expected_columns)
        ]
        info = _metadata_for(spans)
        assert info["eligible"] is True
        assert info["column_count"] == expected_columns
        assert info["cell_text"] == [
            f"Cell {index + 1}" for index in range(expected_columns)
        ]


def test_row_insert_metadata_keeps_adjacent_runs_in_one_cell():
    info = _metadata_for(
        [
            _span("Account", 10, 42, 0),
            _span(" number", 44, 78, 1),
            _span("0.03", 145, 165, 2),
            _span("9,818.65", 250, 294, 3),
        ]
    )
    assert info["eligible"] is True
    assert info["column_count"] == 3
    assert info["cell_text"] == ["Account number", "0.03", "9,818.65"]


def test_row_insert_metadata_rejects_a_normal_single_column_line():
    info = _metadata_for([_span("Ordinary paragraph", 10, 110, 0)])
    assert info["eligible"] is False
    assert info["column_count"] == 0
    assert "multi-column" in info["reason"]


if __name__ == "__main__":
    test_row_insert_metadata_counts_two_through_six_columns()
    test_row_insert_metadata_keeps_adjacent_runs_in_one_cell()
    test_row_insert_metadata_rejects_a_normal_single_column_line()
    print("Table-row browser metadata tests: PASS")
